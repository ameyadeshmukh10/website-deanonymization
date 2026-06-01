"""Step 5 — qualify IP-enriched visitors for Person Search.

Reads data/enriched_ips.json. Keeps records where ALL of:

  1. enrichment.status == "matched" (PDL resolved IP to a company)
  2. icp_filter.is_icp_fit(record) (industry in ICP_INDUSTRIES, no
     excluded tag patterns)
  3. enrichment.person is present AND person.job_title_role is populated
     (PDL had person-level inference for this IP, not just a company match)
  4. One of QUALIFYING_ROLE_PATTERNS (sales, marketing, business_development,
     gtm, growth, revenue) appears in person.job_title_role or sub_role

Round 5: dropped the intent-only fallback path. The pipeline no longer
de-anonymizes when PDL has no person-level inference. Intent reasons
(multi-visit / key-page hits) are still computed and stored for context
on the HubSpot company record, but do NOT gate qualification.

Each qualified record gets a derived `function_area` ("sales", "marketing",
or "gtm") and `visitor_levels` (PDL-supplied).

Output: data/qualified_visitors.json.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import config
import icp_filter

logger = logging.getLogger(__name__)


def _matches_qualifier(role: str | None, sub_role: str | None) -> list[str]:
    """Return the list of QUALIFYING_ROLE_PATTERNS that appear in role/sub_role."""
    fields = [(role or "").lower(), (sub_role or "").lower()]
    hits: list[str] = []
    for pattern in config.QUALIFYING_ROLE_PATTERNS:
        p = pattern.lower()
        if any(p in f for f in fields):
            hits.append(pattern)
    return hits


def derive_function_area(role: str | None, sub_role: str | None) -> str:
    """Bucket the visitor into 'sales', 'marketing', or 'gtm'.

    - 'sales' if role is sales OR sub_role mentions business_development.
    - 'marketing' if role is marketing.
    - 'gtm' if the qualifier hit was growth/revenue/gtm — these don't map
      cleanly to one function, so we treat them as the union (Step 6 will
      search both sales + marketing for these).
    """
    role_l = (role or "").lower()
    sub_l = (sub_role or "").lower()
    if "marketing" in role_l:
        return "marketing"
    if "sales" in role_l or "business_development" in sub_l:
        return "sales"
    return "gtm"


def _intent_hits(record: dict[str, Any]) -> list[str]:
    """Same heuristic as filter_intent.py — multi-visit OR key-page hit."""
    hits: list[str] = []
    visit_count = int(record.get("visit_count") or 0)
    if visit_count >= config.MIN_VISITS_FOR_INTENT:
        hits.append(f"multi_visit:{visit_count}")
    unique_pages = record.get("unique_pages") or []
    lowered = [(p or "").lower() for p in unique_pages]
    for marker in config.HIGH_INTENT_PATHS:
        m = marker.lower()
        if any(p.startswith(m) for p in lowered):
            hits.append(f"key_page:{marker}")
    return hits


def classify(
    record: dict[str, Any],
    *,
    skip_intent_gate: bool = False,
) -> dict[str, Any] | None:
    """Return a qualified-visitor record or None if it doesn't qualify.

    Gate order:
      1. Status must be `matched` (PDL resolved IP -> company).
      2. Company industry must be in ICP_INDUSTRIES (B2B tech ICP).
      3. enrichment.person must be present and job_title_role populated
         (Round 5 — no de-anonymization without person-level inference).
      4. role/sub_role must hit one of QUALIFYING_ROLE_PATTERNS.

    `skip_intent_gate=True` (battle-test / --all-ips mode) bypasses checks
    3 and 4 — any matched + ICP visitor qualifies.

    Intent reasons (multi-visit / key-page hits) are still computed for
    context but do NOT gate qualification anymore.
    """
    enrichment = record.get("enrichment") or {}
    if enrichment.get("status") != "matched":
        return None

    # ICP gate — drops non-software / non-IT companies + excluded tags
    if not icp_filter.is_icp_fit(record):
        return None

    # Person-confidence gate (Round 5): require PDL person inference with
    # at least a role populated. Intent-only path is removed.
    person = enrichment.get("person") or {}
    role = person.get("job_title_role") if person else None
    sub_role = person.get("job_title_sub_role") if person else None
    role_hits = _matches_qualifier(role, sub_role) if person else []

    # Intent reasons computed for context only (stored on the qualified
    # record), but no longer required to qualify.
    intent_hits = _intent_hits(record)

    if not skip_intent_gate:
        if not person or not role:
            return None
        if not role_hits:
            return None

    # Function area is derived from PDL person role. When skip_intent_gate
    # is on AND no role data exists, default to "gtm" (Step 6 query is the
    # same regardless of function_area now).
    if role_hits:
        function_area = derive_function_area(role, sub_role)
    else:
        function_area = "gtm"

    out = dict(record)
    out["role_qualifier_hits"] = role_hits
    out["intent_qualifier_hits"] = intent_hits
    out["function_area"] = function_area
    out["visitor_levels"] = (person.get("job_title_levels") or []) if person else []
    return out


def filter_visitors(
    enriched: dict[str, dict[str, Any]],
    *,
    skip_intent_gate: bool = False,
) -> dict[str, dict[str, Any]]:
    qualified: dict[str, dict[str, Any]] = {}
    matched_total = 0
    icp_total = 0
    person_total = 0
    for ip, record in enriched.items():
        enrichment = record.get("enrichment") or {}
        if enrichment.get("status") == "matched":
            matched_total += 1
            if icp_filter.is_icp_fit(record):
                icp_total += 1
            if (enrichment.get("person") or {}).get("job_title_role"):
                person_total += 1
        out = classify(record, skip_intent_gate=skip_intent_gate)
        if out is not None:
            qualified[ip] = out

    logger.info(
        "qualifier: %d qualified / %d ICP matched / %d had person+role / "
        "%d matched total (gate: status=matched AND ICP AND person.role AND "
        "role pattern hit)",
        len(qualified), icp_total, person_total, matched_total,
    )
    return qualified


def load_enriched() -> dict[str, dict[str, Any]]:
    if not config.ENRICHED_FILE.exists():
        raise FileNotFoundError(
            f"{config.ENRICHED_FILE} not found. Run Step 3 (pdl_client) first."
        )
    return json.loads(config.ENRICHED_FILE.read_text())


def save_qualified(records: dict[str, dict[str, Any]]) -> None:
    config.QUALIFIED_VISITORS_FILE.write_text(
        json.dumps(records, indent=2, sort_keys=True)
    )
    logger.info(
        "wrote %d qualified visitors to %s",
        len(records), config.QUALIFIED_VISITORS_FILE,
    )


def run(*, skip_intent_gate: bool = False) -> dict[str, dict[str, Any]]:
    enriched = load_enriched()
    qualified = filter_visitors(enriched, skip_intent_gate=skip_intent_gate)
    save_qualified(qualified)
    return qualified


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
