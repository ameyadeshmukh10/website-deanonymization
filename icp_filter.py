"""ICP industry gate — used by Step 4 (HubSpot company write) and Step 5
(visitor qualifier) to decide whether a matched company is in the user's
target market.

Single source of truth so the rule stays consistent across the pipeline.
"""
from __future__ import annotations

from typing import Any

import config


def is_icp_fit(record: dict[str, Any]) -> bool:
    """Return True if `record.enrichment.company.industry` is in ICP_INDUSTRIES.

    Defensive — handles missing enrichment / missing company / non-string
    industry gracefully (returns False rather than raising).
    """
    enrichment = record.get("enrichment") or {}
    company = enrichment.get("company") or {}
    industry = company.get("industry")
    if not isinstance(industry, str):
        return False
    return industry.strip().lower() in config.ICP_INDUSTRIES
