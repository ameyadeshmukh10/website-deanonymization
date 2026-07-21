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
    """Raised on 429 / 5xx so tenacity retries.

    Carries the HTTP status code so a caller handling an *exhausted* retry
    (tenacity re-raises this after MAX_RETRIES) can tell a rate-limit (429)
    apart from a server error (5xx).
    """

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"{status_code} from PDL")


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
        raise RetryableHTTPError(resp.status_code)
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
    """Call PDL for one IP. Returns a status dict; never raises on a clean miss
    or on exhausted retries."""
    try:
        resp = _call_pdl(session, ip)
    except RetryableHTTPError as exc:
        # Retries were exhausted on a 429/5xx (tenacity re-raises here after
        # MAX_RETRIES). A single persistently rate-limited or flaky IP must not
        # crash the whole run — Step 3 is a cache-backed, resumable job. Mark it
        # errored (error results aren't cached, so it's retried next run) and
        # let the caller keep going. `rate_limited` lets enrich_all's circuit
        # breaker notice sustained throttling.
        logger.warning(
            "PDL %s -> %s after %d attempts; skipping this IP",
            ip, exc.status_code, config.MAX_RETRIES,
        )
        return {
            "status": "error",
            "http_status": exc.status_code,
            "rate_limited": exc.status_code == 429,
        }
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
    rate_limit_strikes = 0    # IPs that returned 429 on every retry
    skipped_rate_limited = 0  # uncached IPs skipped after the breaker tripped
    stop_live_calls = False
    last_call_at = 0.0
    try:
        for i, (ip, record) in enumerate(items, 1):
            cached = _cache_lookup(cache, ip)
            if cached is not None:
                result = cached
                cache_hits += 1
            elif stop_live_calls:
                # Breaker tripped (below): PDL is hard rate-limiting us, so make
                # no new calls. Cached IPs still flow through above; uncached
                # ones are left for the next scheduled run.
                skipped_rate_limited += 1
                continue
            else:
                # Throttle live calls so we don't trip PDL's per-second limit.
                wait = config.PDL_MIN_INTERVAL_SEC - (time.monotonic() - last_call_at)
                if wait > 0:
                    time.sleep(wait)
                result = enrich_ip(session, ip)
                last_call_at = time.monotonic()
                _cache_store(cache, ip, result)
                cache_misses += 1

                # Circuit breaker. A `rate_limited` result means PDL returned
                # 429 on all MAX_RETRIES attempts for this IP — a strong sign
                # its per-minute quota is exhausted. After a few of these, stop
                # making new calls so the run stays inside its wall-clock
                # timeout and the next scheduled run can resume.
                if result.get("rate_limited"):
                    rate_limit_strikes += 1
                    if rate_limit_strikes >= config.PDL_RATE_LIMIT_MAX_STRIKES:
                        stop_live_calls = True
                        logger.warning(
                            "PDL hard rate-limited %d IPs (429 after %d retries "
                            "each); halting live enrichment for this run at "
                            "%d/%d IPs. Cached IPs still process; the rest are "
                            "deferred to the next scheduled run.",
                            rate_limit_strikes, config.MAX_RETRIES, i, len(items),
                        )

                # Per-run live-call budget: bounds a cold-cache backlog so the
                # run finishes green (and saves its cache) within the job's
                # timeout; remaining uncached IPs resume next run.
                if not stop_live_calls and cache_misses >= config.PDL_MAX_LIVE_CALLS_PER_RUN:
                    stop_live_calls = True
                    logger.warning(
                        "reached per-run live PDL call budget (%d) at %d/%d IPs; "
                        "deferring the remaining uncached IPs to the next run",
                        config.PDL_MAX_LIVE_CALLS_PER_RUN, i, len(items),
                    )

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
    if skipped_rate_limited:
        logger.warning(
            "skipped %d uncached IPs (per-run budget reached or PDL rate "
            "limiting); they will be enriched on the next scheduled run",
            skipped_rate_limited,
        )
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


def log_icp_industry_diagnostics(enriched: dict[str, dict[str, Any]]) -> None:
    """Histogram PDL industries among matched companies, split by ICP-gate
    outcome, so `config.ICP_INDUSTRIES` can be tuned from real PDL labels
    instead of guesswork.

    Emitted at INFO so it lands in both the CI console and the run-log artifact.
    Deduplicates to one entry per company domain (many IPs → one company) and,
    for the excluded companies, separates "industry not in the allowlist"
    (widening ICP_INDUSTRIES would recover these) from "tag-excluded despite an
    allowed industry" (the telecom/ISP/hosting filter working as intended).
    """
    import icp_filter  # local import keeps this diagnostic self-contained

    seen: dict[str, dict[str, Any]] = {}
    for record in enriched.values():
        enr = record.get("enrichment") or {}
        if enr.get("status") != "matched":
            continue
        domain = ((enr.get("company") or {}).get("domain") or "").strip().lower()
        if domain and domain not in seen:
            seen[domain] = record

    if not seen:
        logger.info("icp diagnostics: no matched companies this run")
        return

    fit = 0
    excluded_by_industry: dict[str, list[str]] = {}
    excluded_by_size: list[str] = []
    excluded_by_tag: dict[str, int] = {}
    for domain, record in seen.items():
        company = (record.get("enrichment") or {}).get("company") or {}
        industry = company.get("industry")
        norm = (
            industry.strip().lower()
            if isinstance(industry, str) and industry.strip()
            else "(none)"
        )
        reason = icp_filter.icp_exclusion_reason(record)
        if reason is None:
            fit += 1
        elif reason == "industry":
            excluded_by_industry.setdefault(norm, []).append(domain)
        elif reason == "size":
            excluded_by_size.append(domain)
        else:  # tag
            excluded_by_tag[norm] = excluded_by_tag.get(norm, 0) + 1

    total_excl_ind = sum(len(v) for v in excluded_by_industry.values())
    logger.info(
        "icp diagnostics: %d unique matched companies — %d ICP-fit, "
        "%d excluded-by-industry, %d excluded-by-size, %d excluded-by-tag",
        len(seen), fit, total_excl_ind, len(excluded_by_size),
        sum(excluded_by_tag.values()),
    )
    if excluded_by_size:
        logger.info(
            "icp diagnostics   excluded-by-size (> %d employees)  %4d  e.g. %s",
            config.ICP_MAX_EMPLOYEES, len(excluded_by_size),
            ", ".join(sorted(excluded_by_size)[:6]),
        )
    # The actionable list: which PDL industries the gate is dropping, biggest
    # first, with a few example domains so real targets are easy to spot.
    ranked = sorted(
        excluded_by_industry.items(), key=lambda kv: len(kv[1]), reverse=True
    )
    for industry, domains in ranked[:25]:
        examples = ", ".join(sorted(domains)[:4])
        logger.info(
            "icp diagnostics   excluded-by-industry  %4d  %-45s e.g. %s",
            len(domains), industry, examples,
        )
    if excluded_by_tag:
        logger.info(
            "icp diagnostics   excluded-by-tag (allowed industry, tag blocked): %s",
            dict(sorted(excluded_by_tag.items(), key=lambda kv: kv[1], reverse=True)),
        )


def run(*, limit: int | None = None) -> dict[str, dict[str, Any]]:
    high_intent = load_high_intent()
    enriched = enrich_all(high_intent, limit=limit)
    save_enriched(enriched)
    log_icp_industry_diagnostics(enriched)
    return enriched


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(limit=args.limit)
