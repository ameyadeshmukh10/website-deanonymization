"""Step 3 — enrich high-intent IPs with People Data Labs.

Reads data/high_intent_ips.json, calls PDL's IP Enrichment API for each IP,
attaches the resolved company info, and writes data/enriched_ips.json.

PDL IP API (v5):
    GET  https://api.peopledatalabs.com/v5/ip/enrich?ip=<ip>
    headers: X-Api-Key: <key>

    200 -> match (full response below)
    400 -> "Cannot Enrich IP" — the IP is a known hosting provider, proxy,
           Tor exit, relay, mobile carrier, etc. Treat as no_match.
    404 -> no record for that IP at all (residential, unknown, etc.)
    402 -> out of credits
    429 -> rate limited
    5xx -> retry

Response shape (relevant fields, all optional in practice):
    {
      "status": 200,
      "ip": { "address": "1.2.3.4", "type": "ipv4", ... },
      "data": {
        "company": {
          "name": "...", "display_name": "...",
          "website": "example.com", "size": "201-500",
          "industry": "...", "type": "...", "linkedin_id": "...",
          "location": { "country": "us", "region": "ca", ... }
        }
      }
    }

We defensively pull domain/name/industry/size and skip IPs that come back
without at least a domain (no domain -> nothing to match in HubSpot).
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


# -----------------------------------------------------------------------------
# Response cache — keyed by IP, with per-status TTL
# -----------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_for(result_status: str) -> timedelta:
    """How long to cache a result of this status before re-asking PDL."""
    if result_status == "matched":
        return timedelta(days=config.PDL_CACHE_TTL_MATCHED_DAYS)
    if result_status in ("no_match", "ineligible", "no_domain"):
        return timedelta(days=config.PDL_CACHE_TTL_NEGATIVE_DAYS)
    # `error` and unknown — don't cache (return zero TTL → immediately expired).
    return timedelta(seconds=0)


def load_cache() -> dict[str, dict[str, Any]]:
    """Load the on-disk PDL cache. Empty dict on first run or corrupted file.

    Migrates legacy entries: any `matched` record that lacks the `person`
    field (written by an older `_extract_company` that ignored data.person)
    is dropped so the next run re-fetches it with full extraction.
    """
    if not config.PDL_CACHE_FILE.exists():
        return {}
    try:
        cache = json.loads(config.PDL_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning(
            "%s is corrupted; ignoring cache for this run", config.PDL_CACHE_FILE,
        )
        return {}

    legacy_keys = [
        ip for ip, entry in cache.items()
        if (entry.get("result") or {}).get("status") == "matched"
        and "person" not in (entry.get("result") or {})
    ]
    for ip in legacy_keys:
        del cache[ip]
    if legacy_keys:
        logger.info(
            "dropped %d legacy cache entries (missing person field) — they will "
            "be re-enriched on this run", len(legacy_keys),
        )

    # Second migration: cache entries written before `tags` extraction was
    # added. Drop matched entries whose company lacks the `tags` field so
    # the next Step 3 picks up fresh PDL data with tags populated.
    no_tags_keys = [
        ip for ip, entry in cache.items()
        if (entry.get("result") or {}).get("status") == "matched"
        and isinstance((entry.get("result") or {}).get("company"), dict)
        and "tags" not in ((entry.get("result") or {}).get("company") or {})
    ]
    for ip in no_tags_keys:
        del cache[ip]
    if no_tags_keys:
        logger.info(
            "dropped %d legacy cache entries (missing tags field) — they will "
            "be re-enriched on this run", len(no_tags_keys),
        )
    return cache


def save_cache(cache: dict[str, dict[str, Any]]) -> None:
    config.PDL_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))
    logger.info(
        "wrote PDL cache (%d entries) to %s", len(cache), config.PDL_CACHE_FILE
    )


def _cache_lookup(
    cache: dict[str, dict[str, Any]], ip: str
) -> dict[str, Any] | None:
    """Return a cached result for `ip` if still fresh, else None."""
    entry = cache.get(ip)
    if not entry:
        return None
    expires_at = entry.get("expires_at")
    if not expires_at:
        return None
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if datetime.now(timezone.utc) >= exp_dt:
        return None
    return entry.get("result")


def _cache_store(
    cache: dict[str, dict[str, Any]], ip: str, result: dict[str, Any]
) -> None:
    """Persist a result into the cache with the appropriate TTL.

    Skip caching for `error` statuses so transient failures aren't sticky.
    """
    ttl = _ttl_for(result.get("status", ""))
    if ttl.total_seconds() <= 0:
        return
    now = datetime.now(timezone.utc)
    cache[ip] = {
        "result": result,
        "cached_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
    }


class RetryableHTTPError(Exception):
    """Raised on 429 / 5xx so tenacity retries."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Api-Key": config.PDL_API_KEY})
    return s


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    stop=stop_after_attempt(config.MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=config.BACKOFF_MIN, max=config.BACKOFF_MAX),
    reraise=True,
)
def _call_pdl(session: requests.Session, ip: str) -> requests.Response:
    resp = session.get(
        config.PDL_IP_ENDPOINT,
        params={"ip": ip},
        timeout=config.HTTP_TIMEOUT,
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        # PDL sends Retry-After on 429 when its per-minute window is sticky;
        # honor it so we don't bash a closed door for 30 more seconds.
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
                logger.warning(
                    "PDL %s -> %s, Retry-After=%ss; sleeping then retrying",
                    ip, resp.status_code, retry_after,
                )
                time.sleep(min(delay, 75))  # cap at 75s as a safety net
            except (TypeError, ValueError):
                pass
        else:
            logger.warning("PDL %s -> %s, will retry", ip, resp.status_code)
        raise RetryableHTTPError(f"{resp.status_code} from PDL")
    return resp


def _extract_match(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Pull (company, person, ip_metadata) from a PDL IP-enrichment response.

    Each element may be None independently. We treat a response as "useful"
    if either company or person resolves to something — but downstream Step 4
    still needs a domain, so callers should check that.
    """
    data = payload.get("data") or {}
    raw_company = data.get("company") or {}
    raw_person = data.get("person") or {}
    raw_ip = data.get("ip") or {}

    domain = (raw_company.get("website") or "").strip().lower() or None
    name = raw_company.get("display_name") or raw_company.get("name")
    company: dict[str, Any] | None
    if not domain and not name:
        company = None
    else:
        company = {
            "domain": domain,
            "name": name,
            "industry": raw_company.get("industry"),
            "size": raw_company.get("size"),
            "linkedin_id": raw_company.get("linkedin_id"),
            "location": raw_company.get("location"),
            "type": raw_company.get("type"),
            "confidence": raw_company.get("confidence"),
            "employee_count": raw_company.get("employee_count"),
            "inferred_revenue": raw_company.get("inferred_revenue"),
            # Tags qualify the broad PDL industry buckets — used by
            # icp_filter.is_icp_fit to exclude telecoms / ISPs / VoIP / etc.
            # that get bucketed under "information technology and services".
            "tags": raw_company.get("tags") or [],
        }

    # PDL's person inference — the role/sub_role/levels of the person they
    # think is behind this IP. Drives the Step 5 qualifier.
    person: dict[str, Any] | None
    if not raw_person:
        person = None
    else:
        person = {
            "confidence": raw_person.get("confidence"),
            "job_title_role": raw_person.get("job_title_role"),
            "job_title_sub_role": raw_person.get("job_title_sub_role"),
            "job_title_levels": raw_person.get("job_title_levels") or [],
        }

    # IP-level flags (mobile/hosting/proxy/vpn/tor/relay). Useful for later
    # analytics on which traffic source produced the visit.
    metadata = raw_ip.get("metadata") if isinstance(raw_ip, dict) else None
    ip_metadata: dict[str, Any] | None = metadata if metadata else None

    return company, person, ip_metadata


def enrich_ip(session: requests.Session, ip: str) -> dict[str, Any]:
    """Call PDL for one IP. Returns a status dict; never raises on a clean miss."""
    try:
        resp = _call_pdl(session, ip)
    except requests.exceptions.RequestException as exc:
        logger.error("PDL request failed for %s: %s", ip, exc)
        return {"status": "error", "error": str(exc)}

    if resp.status_code == 404:
        return {"status": "no_match"}
    if resp.status_code == 400:
        # PDL returns 400 with a structured reason when the IP is a hosting
        # provider / proxy / Tor / mobile carrier. Same outcome (unusable),
        # but worth tagging separately for analytics.
        try:
            reason = resp.json().get("message")
        except ValueError:
            reason = None
        return {"status": "ineligible", "reason": reason}
    if resp.status_code == 402:
        # Out of credits — surface loudly, don't keep burning quota.
        raise RuntimeError("PDL returned 402 (out of credits). Aborting.")
    if resp.status_code == 401 or resp.status_code == 403:
        raise RuntimeError(
            f"PDL returned {resp.status_code} — check PDL_API_KEY scopes."
        )
    if resp.status_code != 200:
        return {
            "status": "error",
            "http_status": resp.status_code,
            "body": resp.text[:500],
        }

    company, person, ip_metadata = _extract_match(resp.json())
    if not company or not company.get("domain"):
        # PDL matched something but didn't return a domain; can't use in HubSpot.
        return {
            "status": "no_domain",
            "raw_company": company,
            "person": person,
            "ip_metadata": ip_metadata,
        }
    return {
        "status": "matched",
        "company": company,
        "person": person,           # may be None if PDL has no person inference
        "ip_metadata": ip_metadata,  # may be None
    }


def enrich_all(
    high_intent: dict[str, dict[str, Any]],
    *,
    limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Enrich every IP in `high_intent`, returning a new dict with `enrichment`.

    Uses a persistent on-disk cache (`data/pdl_cache.json`) so re-runs hit the
    network only for IPs whose cache entry is missing or expired. Between live
    calls we sleep `PDL_MIN_INTERVAL_SEC` to stay under PDL's per-second cap.
    """
    config.assert_env("PDL_API_KEY")
    session = _session()
    cache = load_cache()

    items = list(high_intent.items())
    if limit:
        items = items[:limit]
        logger.info("limit=%d in effect; processing %d ips", limit, len(items))

    out: dict[str, dict[str, Any]] = {}
    matched = no_match = errored = 0
    cache_hits = cache_misses = 0
    last_call_at = 0.0
    try:
        for i, (ip, record) in enumerate(items, 1):
            cached = _cache_lookup(cache, ip)
            if cached is not None:
                result = cached
                cache_hits += 1
            else:
                # Throttle live calls so we don't trip PDL's per-second limit.
                wait = config.PDL_MIN_INTERVAL_SEC - (time.monotonic() - last_call_at)
                if wait > 0:
                    time.sleep(wait)
                result = enrich_ip(session, ip)
                last_call_at = time.monotonic()
                _cache_store(cache, ip, result)
                cache_misses += 1

            merged = dict(record)
            merged["enrichment"] = result
            out[ip] = merged
            if result["status"] == "matched":
                matched += 1
            elif result["status"] in ("no_match", "no_domain", "ineligible"):
                no_match += 1
            else:
                errored += 1
            if i % 25 == 0:
                logger.info(
                    "progress %d/%d (matched=%d no_match=%d error=%d  cache hit=%d miss=%d)",
                    i, len(items), matched, no_match, errored, cache_hits, cache_misses,
                )
                # Periodic save so a crash on the 121st of 122 doesn't lose
                # the first 120 cache entries.
                save_cache(cache)
    finally:
        # Always persist whatever we cached this run, even if the loop above
        # blew up — that's the whole point of the cache.
        save_cache(cache)
    logger.info(
        "pdl cache: %d hits, %d misses (%.0f%% hit rate)",
        cache_hits, cache_misses,
        100.0 * cache_hits / max(1, cache_hits + cache_misses),
    )
    logger.info(
        "pdl done: matched=%d no_match=%d errored=%d (total %d)",
        matched, no_match, errored, len(out),
    )
    return out


def load_high_intent() -> dict[str, dict[str, Any]]:
    if not config.HIGH_INTENT_FILE.exists():
        raise FileNotFoundError(
            f"{config.HIGH_INTENT_FILE} not found. Run Step 2 (filter_intent) first."
        )
    return json.loads(config.HIGH_INTENT_FILE.read_text())


def save_enriched(records: dict[str, dict[str, Any]]) -> None:
    config.ENRICHED_FILE.write_text(json.dumps(records, indent=2, sort_keys=True))
    logger.info("wrote %d ips to %s", len(records), config.ENRICHED_FILE)


def run(*, limit: int | None = None) -> dict[str, dict[str, Any]]:
    high_intent = load_high_intent()
    enriched = enrich_all(high_intent, limit=limit)
    save_enriched(enriched)
    return enriched


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(limit=args.limit)
