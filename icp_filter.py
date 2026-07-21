"""ICP industry gate — used by Step 4 (HubSpot company write) and Step 5
(visitor qualifier) to decide whether a matched company is in the user's
target market.

Single source of truth so the rule stays consistent across the pipeline.
"""
from __future__ import annotations

from typing import Any

import config


def _size_bucket_lower(size: str) -> int | None:
    """Lower bound of a PDL size bucket, e.g. '1001-5000' -> 1001, '10001+' -> 10001."""
    first = size.split("-", 1)[0].replace("+", "").replace(",", "").strip()
    try:
        return int(first)
    except ValueError:
        return None


def _exceeds_size_cap(company: dict[str, Any], max_employees: int) -> bool:
    """True if the company is definitively larger than `max_employees`.

    Prefers PDL's numeric `employee_count`; falls back to the `size` bucket's
    lower bound so we only exclude when even the smallest company in the bucket
    is over the cap. Unknown size returns False — we don't exclude on missing
    data (and PDL reliably sizes the large companies this is meant to catch).

    Guards against PDL plan-masked boolean values (e.g. employee_count == True).
    """
    ec = company.get("employee_count")
    if isinstance(ec, bool):
        ec = None
    if isinstance(ec, (int, float)) and ec > max_employees:
        return True

    size = company.get("size")
    if isinstance(size, str) and size.strip():
        lower = _size_bucket_lower(size)
        if lower is not None and lower > max_employees:
            return True
    return False


def icp_exclusion_reason(record: dict[str, Any]) -> str | None:
    """Return None if the matched company is ICP-fit, else the primary reason
    it was excluded: 'industry' | 'size' | 'tag'.

    Single source of truth for the ICP gate. Three stages, all must pass:
      1. industry — PDL `company.industry` in `config.ICP_INDUSTRIES`.
      2. size     — `employee_count` <= `config.ICP_MAX_EMPLOYEES`; enterprise
                    giants aren't realistic buyers for this product.
      3. tags     — no `config.ICP_EXCLUDE_TAG_PATTERNS` substring in
                    `company.tags` — catches telecoms / ISPs / VoIP / hosting
                    that PDL buckets under "information technology and services".

    Defensive — missing enrichment / non-string industry / missing size or tags
    degrade gracefully rather than raising.
    """
    enrichment = record.get("enrichment") or {}
    company = enrichment.get("company") or {}

    industry = company.get("industry")
    if not isinstance(industry, str) or industry.strip().lower() not in config.ICP_INDUSTRIES:
        return "industry"

    if _exceeds_size_cap(company, config.ICP_MAX_EMPLOYEES):
        return "size"

    tags = company.get("tags")
    if isinstance(tags, list) and tags:
        joined = " | ".join(
            (t or "").strip().lower() for t in tags if isinstance(t, str)
        )
        for pattern in config.ICP_EXCLUDE_TAG_PATTERNS:
            if pattern in joined:
                return "tag"
    return None


def is_icp_fit(record: dict[str, Any]) -> bool:
    """True if the matched company qualifies as ICP-fit (see icp_exclusion_reason)."""
    return icp_exclusion_reason(record) is None
