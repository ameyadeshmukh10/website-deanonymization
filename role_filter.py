"""Step 5 — qualify IP-enriched visitors for Person Search.

Reads data/enriched_ips.json. Keeps records where the IP resolved to a
company AND **either** of these qualifies the visitor:

  Role path:
    enrichment.person is present, AND
    any QUALIFYING_ROLE_PATTERN substring (sales, marketing,
    business_development, gtm, growth, revenue) appears in role/sub_role.

  Intent path:
    visit_count >= MIN_VISITS_FOR_INTENT, OR
    at least one unique_page starts with a HIGH_INTENT_PATH.

The role path is the tighter signal — when PDL has high-confidence person
inference. The intent path is the fallback that activates the chain when
PDL doesn't infer a person but the visitor's behavior is strong (multiple
visits, hit a /sales-ai-workers/* / /pricing / /demo page).

Each qualified record gets a derived `function_area` ("sales", "marketing",
or "gtm") and `visitor_levels` (empty list when no person inference).

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
      3. EITHER the role-based qualifier OR the intent-based qualifier must
         hit — unless `skip_intent_gate=True` (battle-test / --all-ips mode),
         in which case any matched + ICP visitor qualifies.

    Non-ICP visitors short-circuit BEFORE the qualifier checks so we never
    spend Person Search credits on out-of-ICP companies in Step 6.
    """
    enrichment = record.get("enrichment") or {}
    if enrichment.get("status") != "matched":
        return None

    # ICP gate — drops non-software / non-IT companies (AbbVie, Corewell, etc.)
    if not icp_filter.is_icp_fit(record):
        return None

    # Path 1: role-based qualifier (requires PDL person inference)
    person = enrichment.get("person") or {}
    role_hits: list[str] = []
    if person:
        role_hits = _matches_qualifier(
            person.get("job_title_role"), person.get("job_title_sub_role"),
        )

    # Path 2: intent-based qualifier (works without person inference)
    intent_hits = _intent_hits(record)

    if not skip_intent_gate and not role_hits and not intent_hits:
        return None

    # Function area: prefer role-derived; fall back to "gtm" (union of
    # sales + marketing in Step 6) when only intent qualified.
    if role_hits:
        function_area = derive_function_area(
            person.get("job_title_role"), person.get("job_title_sub_role"),
        )
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
    via_role = via_intent = via_both = 0
    for ip, record in enriched.items():
        enrichment = record.get("enrichment") or {}
        if enrichment.get("status") == "matched":
            matched_total += 1
            if icp_filter.is_icp_fit(record):
                icp_total += 1
            if enrichment.get("person"):
                person_total += 1
        out = classify(record, skip_intent_gate=skip_intent_gate)
        if out is not None:
            qualified[ip] = out
            r = bool(out.get("role_qualifier_hits"))
            i = bool(out.get("intent_qualifier_hits"))
            if r and i: via_both += 1
            elif r: via_role += 1
            elif i: via_intent += 1

    logger.info(
        "qualifier: %d qualified (role=%d, intent=%d, both=%d) / %d ICP "
        "matched / %d matched total / %d had person data",
        len(qualified), via_role, via_intent, via_both,
        icp_total, matched_total, person_total,
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
