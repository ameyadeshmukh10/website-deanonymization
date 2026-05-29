"""ICP industry gate — used by Step 4 (HubSpot company write) and Step 5
(visitor qualifier) to decide whether a matched company is in the user's
target market.

Single source of truth so the rule stays consistent across the pipeline.
"""
from __future__ import annotations

from typing import Any

import config


def is_icp_fit(record: dict[str, Any]) -> bool:
    """Return True if the matched company qualifies as ICP-fit.

    Two-stage gate:
      1. PDL `company.industry` must be in `config.ICP_INDUSTRIES`.
      2. PDL `company.tags` must not contain any disqualifying substring from
         `config.ICP_EXCLUDE_TAG_PATTERNS` — catches telecoms / ISPs / VoIP /
         managed services that PDL buckets under "information technology and
         services" but aren't actually B2B SaaS buyers.

    Defensive — missing enrichment / non-string industry / missing tags all
    return False (or proceed with empty tags) rather than raising.
    """
    enrichment = record.get("enrichment") or {}
    company = enrichment.get("company") or {}

    industry = company.get("industry")
    if not isinstance(industry, str):
        return False
    if industry.strip().lower() not in config.ICP_INDUSTRIES:
        return False

    # Tag-based exclusion (B). Older cache entries may lack `tags` — in that
    # case we fall back to industry-only (the previous behavior). The cache
    # migration in pdl_client.load_cache forces matched entries without tags
    # to be re-enriched, so this fallback shouldn't fire after a fresh run.
    tags = company.get("tags")
    if isinstance(tags, list) and tags:
        joined = " | ".join(
            (t or "").strip().lower() for t in tags if isinstance(t, str)
        )
        for pattern in config.ICP_EXCLUDE_TAG_PATTERNS:
            if pattern in joined:
                return False
    return True
