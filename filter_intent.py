"""Step 2 — filter raw IP visits down to high-intent IPs.

Reads data/ip_visits.json, applies the rules from config (visit_count threshold
+ key-page hit list), and writes data/high_intent_ips.json. Each surviving IP
gets an `intent_reasons` array explaining *why* it was kept — useful both for
downstream prioritisation and for debugging the filter.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import config

logger = logging.getLogger(__name__)


def _matched_high_intent_paths(unique_pages: list[str]) -> list[str]:
    """Return the subset of HIGH_INTENT_PATHS that match any of the visited paths.

    Prefix-match, case-insensitive. e.g. "/pricing" matches both "/pricing" and
    "/pricing/enterprise".
    """
    if not unique_pages:
        return []
    lowered = [(p or "").lower() for p in unique_pages]
    hits: list[str] = []
    for marker in config.HIGH_INTENT_PATHS:
        m = marker.lower()
        if any(p.startswith(m) for p in lowered):
            hits.append(marker)
    return hits


def classify(record: dict[str, Any]) -> list[str]:
    """Return the list of intent reasons for one IP record, or [] if low-intent."""
    reasons: list[str] = []

    visit_count = int(record.get("visit_count") or 0)
    if visit_count >= config.MIN_VISITS_FOR_INTENT:
        reasons.append(f"multi_visit:{visit_count}")

    unique_pages = record.get("unique_pages") or []
    key_hits = _matched_high_intent_paths(unique_pages)
    for hit in key_hits:
        reasons.append(f"key_page:{hit}")

    return reasons


def filter_visits(
    visits: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the subset of visits that pass the intent filter, with reasons."""
    high_intent: dict[str, dict[str, Any]] = {}
    for ip, record in visits.items():
        reasons = classify(record)
        if not reasons:
            continue
        enriched = dict(record)
        enriched["intent_reasons"] = reasons
        high_intent[ip] = enriched

    logger.info(
        "filter: %d / %d ips passed (%.0f%%)",
        len(high_intent),
        len(visits),
        100.0 * len(high_intent) / max(1, len(visits)),
    )
    return high_intent


def load_visits() -> dict[str, dict[str, Any]]:
    if not config.IP_VISITS_FILE.exists():
        raise FileNotFoundError(
            f"{config.IP_VISITS_FILE} not found. Run Step 1B (worker_client) first."
        )
    return json.loads(config.IP_VISITS_FILE.read_text())


def save_high_intent(records: dict[str, dict[str, Any]]) -> None:
    config.HIGH_INTENT_FILE.write_text(json.dumps(records, indent=2, sort_keys=True))
    logger.info("wrote %d ips to %s", len(records), config.HIGH_INTENT_FILE)


def run() -> dict[str, dict[str, Any]]:
    visits = load_visits()
    high_intent = filter_visits(visits)
    save_high_intent(high_intent)
    return high_intent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
