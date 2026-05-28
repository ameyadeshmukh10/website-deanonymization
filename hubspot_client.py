"""Step 4 — write enriched IP visits back to HubSpot company records.

Reads data/enriched_ips.json, groups records by PDL-resolved domain, then:

  1. For each domain, builds a single set of visit properties by aggregating
     across all IPs that resolved to that domain.
  2. Searches HubSpot for a company with matching `domain`. If found, fetches
     its current website_visit_count, adds the *delta* since last successful
     run, and updates. If not, creates a new company with the PDL-supplied
     name + domain and the current cumulative count.
  3. Writes a per-domain summary to data/hubspot_writes.json.
  4. On success, persists credited per-IP counts to data/credited_counts.json
     so the next run only writes the delta — re-running with no new traffic
     is a no-op, not a double-count.

Pre-flight: verifies that the TARGET_PROPERTIES exist on the company object
type. Missing properties are reported but not auto-created — you control the
schema in HubSpot.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import config
import icp_filter

logger = logging.getLogger(__name__)


class RetryableHTTPError(Exception):
    """Raised on 429 / 5xx so tenacity retries."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {config.HUBSPOT_API_KEY}",
            "Content-Type": "application/json",
        }
    )
    return s


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    stop=stop_after_attempt(config.MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=config.BACKOFF_MIN, max=config.BACKOFF_MAX),
    reraise=True,
)
def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    resp = session.request(method, url, timeout=config.HTTP_TIMEOUT, **kwargs)
    if resp.status_code == 429 or resp.status_code >= 500:
        logger.warning("HubSpot %s %s -> %s, will retry", method, url, resp.status_code)
        raise RetryableHTTPError(f"{resp.status_code} from {url}")
    return resp


# -----------------------------------------------------------------------------
# Pre-flight
# -----------------------------------------------------------------------------
def verify_target_properties(session: requests.Session) -> list[str]:
    """Return the subset of TARGET_PROPERTIES missing on the company object."""
    resp = _request(session, "GET", config.HUBSPOT_COMPANY_PROPERTIES)
    resp.raise_for_status()
    existing = {p.get("name") for p in resp.json().get("results", [])}
    return [n for n in config.TARGET_PROPERTIES if n not in existing]


def ensure_icp_property(session: requests.Session) -> None:
    """Auto-create `is_icp_fit` as a boolean company property if it's missing.

    Idempotent: HubSpot returns 409 on duplicate-name create, which we treat
    as success. Keeps deployment friction low — user doesn't have to click
    through Settings → Properties before the first run.
    """
    resp = _request(session, "GET", config.HUBSPOT_COMPANY_PROPERTIES)
    resp.raise_for_status()
    existing = {p.get("name") for p in resp.json().get("results", [])}
    if config.HUBSPOT_ICP_FIT_PROPERTY in existing:
        return

    body = {
        "name": config.HUBSPOT_ICP_FIT_PROPERTY,
        "label": "ICP Fit",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "groupName": "companyinformation",
        "description": (
            "Set by the website-deanonymization pipeline. True if the company's "
            "PDL industry matches the configured ICP allowlist "
            "(B2B software / IT services)."
        ),
        "options": [
            {"label": "True", "value": "true", "displayOrder": 0, "hidden": False},
            {"label": "False", "value": "false", "displayOrder": 1, "hidden": False},
        ],
    }
    resp = _request(session, "POST", config.HUBSPOT_COMPANY_PROPERTIES, json=body)
    if resp.status_code == 409:  # already exists (race or label collision)
        logger.info(
            "company property `%s` already exists; skipping create",
            config.HUBSPOT_ICP_FIT_PROPERTY,
        )
        return
    resp.raise_for_status()
    logger.info(
        "auto-created HubSpot company property `%s` (boolean checkbox)",
        config.HUBSPOT_ICP_FIT_PROPERTY,
    )


# -----------------------------------------------------------------------------
# Aggregation
# -----------------------------------------------------------------------------
def group_by_domain(
    enriched: dict[str, dict[str, Any]],
    credited: dict[str, int],
) -> dict[str, dict[str, Any]]:
    """Collapse per-IP records into one row per resolved company domain.

    For each bucket we track three counters:
      - current_visit_count: sum of visit_count across all IPs in this domain
        as of the current KV snapshot. Used as the cold-start value when
        creating a new HubSpot company.
      - delta_visit_count: sum of (current - already_credited) per IP, never
        negative. Used to *increment* an existing HubSpot company's value.
      - ip_counts: {ip: current_visit_count} so the caller can persist this
        as the new credited baseline after a successful upsert.
    """
    by_domain: dict[str, dict[str, Any]] = {}

    for ip, record in enriched.items():
        enrichment = record.get("enrichment") or {}
        if enrichment.get("status") != "matched":
            continue
        company = enrichment.get("company") or {}
        domain = (company.get("domain") or "").strip().lower()
        if not domain:
            continue

        bucket = by_domain.setdefault(
            domain,
            {
                "domain": domain,
                "name": company.get("name"),
                "industry": company.get("industry"),
                "size": company.get("size"),
                "is_icp_fit": icp_filter.is_icp_fit(record),
                "current_visit_count": 0,
                "delta_visit_count": 0,
                "ip_counts": {},
                "last_seen": None,
                "unique_pages": set(),
                "intent_reasons": set(),
                "ips": [],
            },
        )
        # If any record at this domain qualifies as ICP, the bucket is ICP
        # (PDL is the source of truth — only one industry per domain in
        # practice — but be defensive in case of mixed records).
        if not bucket["is_icp_fit"] and icp_filter.is_icp_fit(record):
            bucket["is_icp_fit"] = True
        current = int(record.get("visit_count") or 0)
        prev_credited = int(credited.get(ip, 0))
        delta = max(0, current - prev_credited)

        bucket["ips"].append(ip)
        bucket["current_visit_count"] += current
        bucket["delta_visit_count"] += delta
        bucket["ip_counts"][ip] = current
        last_seen = record.get("last_seen")
        if last_seen and (bucket["last_seen"] is None or last_seen > bucket["last_seen"]):
            bucket["last_seen"] = last_seen
        for p in record.get("unique_pages") or []:
            bucket["unique_pages"].add(p)
        for r in record.get("intent_reasons") or []:
            bucket["intent_reasons"].add(r)

    # Convert sets to sorted lists for stable output.
    for bucket in by_domain.values():
        bucket["unique_pages"] = sorted(bucket["unique_pages"])
        bucket["intent_reasons"] = sorted(bucket["intent_reasons"])

    total_delta = sum(b["delta_visit_count"] for b in by_domain.values())
    logger.info(
        "aggregated %d ips into %d domains (delta visits this run: %d)",
        len(enriched), len(by_domain), total_delta,
    )
    return by_domain


# -----------------------------------------------------------------------------
# HubSpot property formatting
# -----------------------------------------------------------------------------
def _iso_to_hubspot_datetime(iso_str: str | None) -> int | None:
    """HubSpot datetime properties expect ms-since-epoch (int)."""
    if not iso_str:
        return None
    # Accept trailing 'Z' (Worker emits these) by swapping to +00:00.
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def build_properties(bucket: dict[str, Any], visit_count: int) -> dict[str, Any]:
    """Return the dict of HubSpot company properties to write for one domain.

    `visit_count` is passed in explicitly because the caller computes it
    differently for create vs update (cold cumulative vs existing + delta).
    """
    props: dict[str, Any] = {
        "website_visit_count": str(visit_count),
        "website_pages_visited": "\n".join(bucket["unique_pages"]),
        "website_visit_intent": ", ".join(bucket["intent_reasons"]),
        # HubSpot booleancheckbox properties accept lowercase "true"/"false" strings.
        config.HUBSPOT_ICP_FIT_PROPERTY: "true" if bucket.get("is_icp_fit") else "false",
    }
    last_seen_ms = _iso_to_hubspot_datetime(bucket.get("last_seen"))
    if last_seen_ms is not None:
        props["website_last_visited"] = last_seen_ms
    return props


# -----------------------------------------------------------------------------
# Search / upsert
# -----------------------------------------------------------------------------
def fetch_company_visit_count(session: requests.Session, company_id: str) -> int:
    """Read the current `website_visit_count` from a HubSpot company.

    Returns 0 if the property is absent or non-numeric — keeps the upsert
    path robust to fresh records or manual edits.
    """
    url = f"{config.HUBSPOT_COMPANIES}/{company_id}"
    resp = _request(
        session, "GET", url, params={"properties": "website_visit_count"}
    )
    resp.raise_for_status()
    raw = (resp.json().get("properties") or {}).get("website_visit_count")
    try:
        return int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def find_company_by_domain(session: requests.Session, domain: str) -> str | None:
    """Return the HubSpot company id matching `domain`, or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "domain", "operator": "EQ", "value": domain}
                ]
            }
        ],
        "properties": ["domain", "name"],
        "limit": 1,
    }
    resp = _request(session, "POST", config.HUBSPOT_COMPANY_SEARCH, json=payload)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0]["id"] if results else None


def update_company(
    session: requests.Session, company_id: str, properties: dict[str, Any]
) -> dict[str, Any]:
    url = f"{config.HUBSPOT_COMPANIES}/{company_id}"
    resp = _request(session, "PATCH", url, json={"properties": properties})
    resp.raise_for_status()
    return resp.json()


def create_company(
    session: requests.Session,
    domain: str,
    name: str | None,
    properties: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "properties": {
            "domain": domain,
            **({"name": name} if name else {}),
            **properties,
        }
    }
    resp = _request(session, "POST", config.HUBSPOT_COMPANIES, json=body)
    resp.raise_for_status()
    return resp.json()


# -----------------------------------------------------------------------------
# Contact helpers (Step 7) — reuse the same SESSION pattern as companies.
# -----------------------------------------------------------------------------
def find_contact_by_email(
    session: requests.Session, email: str
) -> str | None:
    """Return the HubSpot contact id matching `email`, or None."""
    payload = {
        "filterGroups": [
            {"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}
        ],
        "properties": ["email"],
        "limit": 1,
    }
    resp = _request(session, "POST", config.HUBSPOT_CONTACT_SEARCH, json=payload)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0]["id"] if results else None


def create_contact(
    session: requests.Session, properties: dict[str, Any]
) -> dict[str, Any]:
    resp = _request(
        session, "POST", config.HUBSPOT_CONTACTS, json={"properties": properties}
    )
    resp.raise_for_status()
    return resp.json()


def update_contact(
    session: requests.Session, contact_id: str, properties: dict[str, Any]
) -> dict[str, Any]:
    url = f"{config.HUBSPOT_CONTACTS}/{contact_id}"
    resp = _request(session, "PATCH", url, json={"properties": properties})
    resp.raise_for_status()
    return resp.json()


def associate_contact_to_company(
    session: requests.Session, contact_id: str, company_id: str
) -> None:
    """Create the default contact→company association (idempotent in HubSpot)."""
    url = config.HUBSPOT_CONTACT_TO_COMPANY.format(
        contact_id=contact_id, company_id=company_id,
    )
    resp = _request(session, "PUT", url)
    # 200 on create, 200 if already exists. 404 if either object is missing.
    if resp.status_code >= 400:
        logger.warning(
            "association %s -> %s failed: %s %s",
            contact_id, company_id, resp.status_code, resp.text[:200],
        )
    resp.raise_for_status()


def verify_contact_access(session: requests.Session) -> None:
    """Smoke-test that the token has contact read+write. Raises on missing scope."""
    # An empty search query hits the read scope. POST with no filters returns
    # all contacts (limit 1) — cheap probe.
    resp = _request(
        session, "POST", config.HUBSPOT_CONTACT_SEARCH,
        json={"limit": 1, "properties": ["email"]},
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "HubSpot token is missing contact scopes. Add "
            "`crm.objects.contacts.read` and `crm.objects.contacts.write` to "
            "the Private App and update HUBSPOT_API_KEY in .env."
        )
    resp.raise_for_status()


def linkedin_property_exists(session: requests.Session) -> bool:
    """Return True if config.HUBSPOT_LINKEDIN_PROPERTY exists on the contact object.

    Used by Step 7 to decide whether LinkedIn-URL fallback matching is
    available. Missing property -> log a warning and fall back to email-only.
    """
    resp = _request(session, "GET", config.HUBSPOT_CONTACT_PROPERTIES)
    resp.raise_for_status()
    existing = {p.get("name") for p in resp.json().get("results", [])}
    return config.HUBSPOT_LINKEDIN_PROPERTY in existing


def canonicalize_linkedin_url(raw: str | None) -> str | None:
    """Normalize a LinkedIn URL so two equivalent forms compare equal.

    PDL returns shapes like:
        linkedin.com/in/akurth430
        https://www.linkedin.com/in/akurth430/
        linkedin.com/in/akurth430?utm_source=x

    Canonical form: lowercase, no scheme, no `www.`, no query/fragment, no
    trailing slash:
        linkedin.com/in/akurth430
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    # Strip scheme
    for scheme in ("https://", "http://"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    # Strip www.
    if s.startswith("www."):
        s = s[4:]
    # Drop query and fragment
    s = s.split("?", 1)[0].split("#", 1)[0]
    # Drop trailing slash
    if s.endswith("/"):
        s = s[:-1]
    # Sanity: must contain linkedin.com/in/
    if "linkedin.com/in/" not in s:
        return None
    return s


def find_contact_by_linkedin(
    session: requests.Session, linkedin_url: str
) -> str | None:
    """Return contact id whose LinkedIn property equals `linkedin_url`, or None.

    The match property name comes from config.HUBSPOT_LINKEDIN_PROPERTY.
    """
    payload = {
        "filterGroups": [
            {"filters": [
                {
                    "propertyName": config.HUBSPOT_LINKEDIN_PROPERTY,
                    "operator": "EQ",
                    "value": linkedin_url,
                }
            ]}
        ],
        "properties": [config.HUBSPOT_LINKEDIN_PROPERTY, "email"],
        "limit": 1,
    }
    resp = _request(session, "POST", config.HUBSPOT_CONTACT_SEARCH, json=payload)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0]["id"] if results else None


def upsert(
    session: requests.Session,
    bucket: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upsert one company. Returns a result dict for the run summary.

    Cumulative semantics for website_visit_count:
      - dry_run:   don't touch HubSpot; report current + delta only.
      - create:    no prior history -> write current_visit_count.
      - update:    fetch existing -> write existing + delta_visit_count.
    """
    domain = bucket["domain"]
    delta = bucket["delta_visit_count"]
    current = bucket["current_visit_count"]

    if dry_run:
        # Show the delta we'd apply. Don't fetch the existing record so a
        # dry-run stays pure-local.
        return {
            "domain": domain,
            "action": "dry_run",
            "ip_count": len(bucket["ips"]),
            "current_visit_count": current,
            "delta_visit_count": delta,
            "properties_preview": build_properties(bucket, visit_count=current),
        }

    existing_id = find_company_by_domain(session, domain)
    if existing_id:
        existing_count = fetch_company_visit_count(session, existing_id)
        new_count = existing_count + delta
        update_company(session, existing_id, build_properties(bucket, visit_count=new_count))
        return {
            "domain": domain,
            "action": "updated",
            "company_id": existing_id,
            "ip_count": len(bucket["ips"]),
            "previous_visit_count": existing_count,
            "delta_visit_count": delta,
            "new_visit_count": new_count,
            "is_icp_fit": bool(bucket.get("is_icp_fit")),
            "name": bucket.get("name"),
        }

    created = create_company(
        session, domain, bucket.get("name"),
        build_properties(bucket, visit_count=current),
    )
    return {
        "domain": domain,
        "action": "created",
        "company_id": created.get("id"),
        "ip_count": len(bucket["ips"]),
        "new_visit_count": current,
        "is_icp_fit": bool(bucket.get("is_icp_fit")),
        "name": bucket.get("name"),
    }


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------
def load_enriched() -> dict[str, dict[str, Any]]:
    if not config.ENRICHED_FILE.exists():
        raise FileNotFoundError(
            f"{config.ENRICHED_FILE} not found. Run Step 3 (pdl_client) first."
        )
    return json.loads(config.ENRICHED_FILE.read_text())


def save_results(results: list[dict[str, Any]]) -> None:
    config.HUBSPOT_RESULTS_FILE.write_text(json.dumps(results, indent=2))
    logger.info("wrote %d results to %s", len(results), config.HUBSPOT_RESULTS_FILE)


def load_credited() -> dict[str, int]:
    """Load the per-IP credited visit_count from disk; empty if first run."""
    if not config.CREDITED_STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(config.CREDITED_STATE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning(
            "%s is malformed; starting fresh (this run will credit full counts).",
            config.CREDITED_STATE_FILE,
        )
        return {}
    return {ip: int(v) for ip, v in raw.items()}


def save_credited(credited: dict[str, int]) -> None:
    config.CREDITED_STATE_FILE.write_text(json.dumps(credited, indent=2, sort_keys=True))
    logger.info(
        "wrote credited state for %d ips to %s",
        len(credited), config.CREDITED_STATE_FILE,
    )


def run(
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    config.assert_env("HUBSPOT_API_KEY")
    session = _session()

    # Auto-create the ICP-fit boolean if it doesn't exist yet, so users
    # aren't blocked on a manual HubSpot click-through.
    ensure_icp_property(session)

    missing = verify_target_properties(session)
    if missing:
        raise RuntimeError(
            "Missing HubSpot company properties: " + ", ".join(missing) +
            ". Create them in HubSpot (Settings -> Properties -> Company "
            "properties) before re-running. See README for field types."
        )

    enriched = load_enriched()
    credited = load_credited()
    by_domain = group_by_domain(enriched, credited)
    domains = list(by_domain.values())
    if limit:
        domains = domains[:limit]
        logger.info("limit=%d in effect; processing %d domains", limit, len(domains))

    results: list[dict[str, Any]] = []
    counts = {"updated": 0, "created": 0, "dry_run": 0, "error": 0}
    for bucket in domains:
        try:
            result = upsert(session, bucket, dry_run=dry_run)
        except Exception as exc:
            logger.exception("upsert failed for %s", bucket["domain"])
            result = {"domain": bucket["domain"], "action": "error", "error": str(exc)}
        results.append(result)
        counts[result["action"]] = counts.get(result["action"], 0) + 1

        # Only credit the IPs once their visit_count has actually been
        # persisted to HubSpot. Failed and dry-run buckets stay un-credited
        # so a re-run picks them up.
        if result["action"] in ("created", "updated"):
            credited.update(bucket["ip_counts"])

    logger.info("hubspot writes: %s", counts)
    save_results(results)
    if not dry_run:
        save_credited(credited)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(dry_run=args.dry_run, limit=args.limit)
