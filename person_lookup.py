"""Step 6 — find buying-committee members at qualified ICP companies via
PDL Person Search.

For each unique company in data/qualified_visitors.json (already ICP-gated
by Step 5), run ONE ES query that ORs all `TARGET_TITLE_CLAUSES` from
config — the explicit buying-committee title list (VP Sales, CRO, CMO,
Head of Sales, RevOps senior+, etc.). US-only.

Replaces the previous size-tiered strategy. Same query regardless of
company size or visitor's function area — the buying-committee list IS
the filter.

Authentication: X-Api-Key header on POST.
Billing: 1 credit per person result returned (per-result, not per-call).
Expected: 3-12 credits per company.

Persistent cache lives in data/pdl_person_cache.json, keyed by
"<domain>|target_titles" so re-runs across days don't burn credits.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import config

logger = logging.getLogger(__name__)


class RetryableHTTPError(Exception):
    """Raised on 429 / 5xx so tenacity retries."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {"X-Api-Key": config.PDL_API_KEY, "Content-Type": "application/json"}
    )
    return s


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------
def _cache_key(domain: str) -> str:
    # Single key per domain — title list is static across visitor function
    # areas, so we don't need to vary the cache key.
    return f"{domain.lower()}|target_titles"


def load_cache() -> dict[str, dict[str, Any]]:
    if not config.PDL_PERSON_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(config.PDL_PERSON_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning(
            "%s is corrupted; ignoring cache for this run",
            config.PDL_PERSON_CACHE_FILE,
        )
        return {}


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    config.PDL_PERSON_CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True)
    )
    logger.info(
        "wrote person cache (%d keys) to %s",
        len(cache), config.PDL_PERSON_CACHE_FILE,
    )


def _cache_lookup(
    cache: dict[str, dict[str, Any]], key: str
) -> list[dict[str, Any]] | None:
    entry = cache.get(key)
    if not entry:
        return None
    expires_at = entry.get("expires_at") or ""
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if datetime.now(timezone.utc) >= exp_dt:
        return None
    return entry.get("results") or []


def _cache_store(
    cache: dict[str, dict[str, Any]], key: str, results: list[dict[str, Any]]
) -> None:
    now = datetime.now(timezone.utc)
    cache[key] = {
        "results": results,
        "cached_at": now.isoformat(),
        "expires_at": (now + timedelta(days=config.PDL_PERSON_CACHE_TTL_DAYS)).isoformat(),
    }


# -----------------------------------------------------------------------------
# Query construction — title-targeted
# -----------------------------------------------------------------------------
def _build_query(domain: str) -> dict[str, Any]:
    """Compose the single ES query: company + US + must include a sales-
    leadership phrase AND must NOT include any sub-segment exclude pattern.

    PDL's ES dialect rejects `minimum_should_match`. When a `bool` query
    contains only `should` clauses (no must / filter / must_not at the same
    level), standard ES defaults to requiring at least one to match — PDL
    follows this convention.
    """
    must_not = [
        {"match_phrase": {"job_title": pattern}}
        for pattern in config.TITLE_EXCLUDE_PATTERNS
    ]
    return {
        "query": {
            "bool": {
                "must": [
                    {"term": {"job_company_website": domain.lower()}},
                    {"term": {"location_country": config.PERSON_SEARCH_COUNTRY}},
                    {"bool": {"should": config.TARGET_TITLE_CLAUSES}},
                ],
                "must_not": must_not,
            }
        },
        "size": config.PERSON_SEARCH_RESULT_CAP,
    }


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------
@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    stop=stop_after_attempt(config.MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=config.BACKOFF_MIN, max=config.BACKOFF_MAX),
    reraise=True,
)
def _post(session: requests.Session, body: dict[str, Any]) -> requests.Response:
    resp = session.post(
        config.PDL_PERSON_SEARCH_ENDPOINT, json=body, timeout=config.HTTP_TIMEOUT
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 75))
            except (TypeError, ValueError):
                pass
        logger.warning("person search -> %s, will retry", resp.status_code)
        raise RetryableHTTPError(f"{resp.status_code} from PDL")
    return resp


def _run_query(
    session: requests.Session, domain: str,
) -> list[dict[str, Any]]:
    """Execute the title-targeted query for one domain. Defensive on errors."""
    body = _build_query(domain)
    resp = _post(session, body)
    if resp.status_code == 402:
        raise RuntimeError("PDL returned 402 (out of credits). Aborting.")
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"PDL Person Search returned {resp.status_code} — check API key scopes."
        )
    if resp.status_code == 404:
        # PDL returns 404 when no person matches the query — not an error.
        return []
    if resp.status_code != 200:
        logger.warning(
            "person search for %s -> %s: %s",
            domain, resp.status_code, resp.text[:300],
        )
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    data = payload.get("data") or []
    logger.info("person search %s: returned %d", domain, len(data))
    return data


# -----------------------------------------------------------------------------
# Per-company orchestration
# -----------------------------------------------------------------------------
def search_people_for_company(
    session: requests.Session, domain: str,
) -> list[dict[str, Any]]:
    """Run the title-targeted Person Search for one domain. PDL handles
    deduplication within a single query, so no client-side dedupe needed.
    """
    return _run_query(session, domain)


# -----------------------------------------------------------------------------
# Public run
# -----------------------------------------------------------------------------
def _string(value: Any) -> str | None:
    """Return value if it's a non-empty string, else None.

    PDL returns booleans (true/false) for fields the caller's plan can't
    access — e.g. `work_email: true` means "we have it, your plan doesn't
    grant access." Treating those as strings would poison downstream writes.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


def _extract_work_email(row: dict[str, Any]) -> str | None:
    """Pull a usable work email or return None. Boolean values are skipped."""
    direct = _string(row.get("work_email"))
    if direct:
        return direct.lower()
    emails = row.get("emails")
    if isinstance(emails, list):
        # Prefer professional addresses, then fall back to the first usable one.
        for e in emails:
            if isinstance(e, dict) and e.get("type") == "professional":
                addr = _string(e.get("address"))
                if addr:
                    return addr.lower()
        for e in emails:
            if isinstance(e, dict):
                addr = _string(e.get("address"))
                if addr:
                    return addr.lower()
            elif isinstance(e, str) and e.strip():
                return e.strip().lower()
    return None


def _shape_person(row: dict[str, Any], company_domain: str) -> dict[str, Any]:
    """Project a PDL person row into the shape we persist + write to HubSpot.

    Boolean-valued fields from PDL (plan-masked) are normalized to None so
    they don't poison HubSpot writes.
    """
    return {
        "pdl_id": _string(row.get("id")),
        "full_name": _string(row.get("full_name")),
        "first_name": _string(row.get("first_name")),
        "last_name": _string(row.get("last_name")),
        "job_title": _string(row.get("job_title")),
        "job_title_role": _string(row.get("job_title_role")),
        "job_title_sub_role": _string(row.get("job_title_sub_role")),
        "job_title_levels": (
            row.get("job_title_levels")
            if isinstance(row.get("job_title_levels"), list)
            else []
        ),
        "linkedin_url": _string(row.get("linkedin_url")),
        "work_email": _extract_work_email(row),
        "location_country": _string(row.get("location_country")),
        "location_region": _string(row.get("location_region")),
        "job_company_name": _string(row.get("job_company_name")),
        "job_company_website": _string(row.get("job_company_website")),
        "company_domain": company_domain,
    }


def _group_companies(
    qualified: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collapse per-IP qualifiers into one entry per company domain.

    With title-targeted Person Search, the query is identical per domain
    regardless of visitor function — we just need to dedupe by domain.
    """
    by_domain: dict[str, dict[str, Any]] = {}
    for ip, record in qualified.items():
        company = (record.get("enrichment") or {}).get("company") or {}
        domain = (company.get("domain") or "").strip().lower()
        if not domain:
            continue
        entry = by_domain.setdefault(domain, {
            "domain": domain,
            "name": company.get("name"),
            "size": company.get("size"),
            "visitor_ips": [],
        })
        entry["visitor_ips"].append(ip)
    return by_domain


def enrich_all(
    qualified: dict[str, dict[str, Any]],
    *,
    limit: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    config.assert_env("PDL_API_KEY")
    session = _session()
    cache = load_cache()

    companies = _group_companies(qualified)
    items = list(companies.items())
    if limit:
        items = items[:limit]
        logger.info("limit=%d in effect; processing %d companies", limit, len(items))

    out: dict[str, list[dict[str, Any]]] = {}
    cache_hits = cache_misses = 0
    try:
        for domain, info in items:
            key = _cache_key(domain)
            cached = _cache_lookup(cache, key)
            if cached is not None:
                cache_hits += 1
                rows = cached
            else:
                rows = search_people_for_company(session, domain)
                _cache_store(cache, key, rows)
                cache_misses += 1

            people = [_shape_person(row, domain) for row in rows]
            out[domain] = people
            logger.info("%s -> %d people", domain, len(people))
            save_cache(cache)  # incremental
    finally:
        save_cache(cache)
    logger.info(
        "person search cache: %d hits, %d misses", cache_hits, cache_misses,
    )
    return out


def load_qualified() -> dict[str, dict[str, Any]]:
    if not config.QUALIFIED_VISITORS_FILE.exists():
        raise FileNotFoundError(
            f"{config.QUALIFIED_VISITORS_FILE} not found. Run Step 5 first."
        )
    return json.loads(config.QUALIFIED_VISITORS_FILE.read_text())


def save_people(people_by_domain: dict[str, list[dict[str, Any]]]) -> None:
    config.PEOPLE_FILE.write_text(
        json.dumps(people_by_domain, indent=2, sort_keys=True)
    )
    total = sum(len(p) for p in people_by_domain.values())
    logger.info(
        "wrote %d people across %d companies to %s",
        total, len(people_by_domain), config.PEOPLE_FILE,
    )


def run(*, limit: int | None = None) -> dict[str, list[dict[str, Any]]]:
    qualified = load_qualified()
    if not qualified:
        logger.info("no qualified visitors; nothing to look up")
        save_people({})
        return {}
    out = enrich_all(qualified, limit=limit)
    save_people(out)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(limit=args.limit)
