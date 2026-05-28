"""Step 1B — pull the Worker's /export endpoint into data/ip_visits.json.

Handles the cursor pagination protocol implemented by src/worker.js:

    GET /export?limit=N           -> { ips: {...}, cursor: "...", complete: false }
    GET /export?limit=N&cursor=X  -> next page
    ...                           -> { ips: {...}, cursor: null, complete: true }

The merged result is written to data/ip_visits.json as a flat object keyed by IP.
"""
from __future__ import annotations

import json
import logging
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
    s.headers.update({"Authorization": f"Bearer {config.WORKER_ADMIN_TOKEN}"})
    return s


@retry(
    retry=retry_if_exception_type(RetryableHTTPError),
    stop=stop_after_attempt(config.MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=config.BACKOFF_MIN, max=config.BACKOFF_MAX),
    reraise=True,
)
def _get_page(session: requests.Session, cursor: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": config.WORKER_EXPORT_PAGE_LIMIT}
    if cursor:
        params["cursor"] = cursor
    url = f"{config.WORKER_BASE_URL}/export"
    resp = session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    if resp.status_code == 429 or resp.status_code >= 500:
        logger.warning("Worker /export -> %s, will retry", resp.status_code)
        raise RetryableHTTPError(f"{resp.status_code} from {url}")
    if resp.status_code == 401:
        raise RuntimeError(
            "Worker returned 401. Check WORKER_ADMIN_TOKEN matches the secret "
            "set via `wrangler secret put ADMIN_TOKEN`."
        )
    resp.raise_for_status()
    return resp.json()


def fetch_all_visits() -> dict[str, dict[str, Any]]:
    """Paginate /export until complete, return the merged {ip: record} dict."""
    config.assert_env("WORKER_BASE_URL", "WORKER_ADMIN_TOKEN")
    session = _session()

    merged: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        body = _get_page(session, cursor)
        ips = body.get("ips") or {}
        merged.update(ips)
        logger.info(
            "page %d: %d ips (running total %d)", page, len(ips), len(merged)
        )
        if body.get("complete"):
            break
        cursor = body.get("cursor")
        if not cursor:
            # No cursor but not complete — defensive break to avoid an infinite loop.
            logger.warning("export ended without cursor but complete=false; stopping")
            break
    return merged


def save_visits(visits: dict[str, dict[str, Any]]) -> None:
    """Write the merged dict to data/ip_visits.json (pretty-printed)."""
    config.IP_VISITS_FILE.write_text(json.dumps(visits, indent=2, sort_keys=True))
    logger.info("wrote %d ips to %s", len(visits), config.IP_VISITS_FILE)


def run() -> dict[str, dict[str, Any]]:
    """Entry point: fetch + save. Returns the merged visits."""
    visits = fetch_all_visits()
    save_visits(visits)
    return visits


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
