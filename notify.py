"""Slack notifications — instant per-run alerts + daily digest.

Two modes, one entry point. Reads pipeline output from `data/` and posts
formatted messages to a Slack incoming webhook.

  python notify.py --mode run     # instant alert when new ICP+buying-committee contact created
  python notify.py --mode digest  # daily summary of recent pipeline activity

Driven by env var SLACK_WEBHOOK_URL. Silently no-ops if unset, so local
runs don't need it.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import requests

import config
import icp_filter

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()

# HubSpot UI base for deep-linking. Region is detected from the API token
# prefix when available; fall back to the US portal.
HUBSPOT_UI_BASE = "https://app.hubspot.com"
HUBSPOT_PORTAL_ID = os.getenv("HUBSPOT_PORTAL_ID", "")  # optional; for direct links


# -----------------------------------------------------------------------------
# Slack transport
# -----------------------------------------------------------------------------
def post_slack(blocks: list[dict[str, Any]], text_fallback: str) -> None:
    """POST blocks to the Slack incoming webhook. No-op when webhook unset."""
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL not set; skipping Slack post (would have sent: %s)", text_fallback)
        return
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": text_fallback, "blocks": blocks},
        timeout=15,
    )
    if resp.status_code >= 400:
        logger.warning(
            "Slack webhook returned %s: %s", resp.status_code, resp.text[:300]
        )
    else:
        logger.info("posted to Slack: %s", text_fallback)


def company_link(company_id: str | None) -> str | None:
    """Build a HubSpot deep-link to the company record, if portal id known."""
    if not company_id or not HUBSPOT_PORTAL_ID:
        return None
    return f"{HUBSPOT_UI_BASE}/contacts/{HUBSPOT_PORTAL_ID}/company/{company_id}"


# -----------------------------------------------------------------------------
# Title-matching for the instant alert (defensive double-check)
# -----------------------------------------------------------------------------
# Quick regex covering the same buying-committee titles as
# config.TARGET_TITLE_CLAUSES. We use a regex here instead of re-running the
# PDL ES query — the data is already fetched.
_BUYING_COMMITTEE_TITLE = re.compile(
    r"\b("
    r"vp\s+(of\s+)?(sales|marketing|growth|revenue|demand|gtm|go.to.market|revenue\s+operations|revops)"
    r"|chief\s+(revenue|commercial|marketing|sales|growth)\s+officer"
    r"|head\s+of\s+(sales|marketing|gtm|go.to.market|demand\s+generation|demand\s+gen|revenue\s+operations|revops|growth)"
    r"|director\s+of\s+(revenue\s+operations|revops|demand\s+generation|demand\s+gen|go.to.market|gtm)"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_buying_committee(title: str | None) -> bool:
    if not isinstance(title, str) or not title.strip():
        return False
    return bool(_BUYING_COMMITTEE_TITLE.search(title))


# -----------------------------------------------------------------------------
# Mode: run — instant alert
# -----------------------------------------------------------------------------
def _load_json(path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("could not parse %s; treating as empty", path)
        return None


def alert_for_run() -> int:
    """Read this run's outputs; Slack any new ICP buying-committee contacts.

    Returns the number of contacts alerted on (0 if nothing qualified).
    """
    contact_writes = _load_json(config.HUBSPOT_CONTACT_RESULTS_FILE) or []
    company_writes = _load_json(config.HUBSPOT_RESULTS_FILE) or []
    people_by_domain = _load_json(config.PEOPLE_FILE) or {}

    # domain -> {is_icp_fit, company_id, name}
    company_meta: dict[str, dict[str, Any]] = {}
    for c in company_writes:
        dom = (c.get("domain") or "").lower()
        if dom:
            company_meta[dom] = {
                "is_icp_fit": bool(c.get("is_icp_fit")),
                "company_id": c.get("company_id"),
                "name": c.get("name"),
            }

    # Build a lookup: (domain, email) -> person row (so we can pull title/levels)
    person_index: dict[tuple[str, str], dict[str, Any]] = {}
    for dom, persons in people_by_domain.items():
        for p in persons:
            email = (p.get("work_email") or "").strip().lower()
            if email:
                person_index[((dom or "").lower(), email)] = p

    alerts: list[dict[str, Any]] = []
    for write in contact_writes:
        if write.get("action") != "created":
            continue
        dom = (write.get("domain") or "").lower()
        meta = company_meta.get(dom) or {}
        if not meta.get("is_icp_fit"):
            continue
        email = (write.get("email") or write.get("match_value") or "").strip().lower()
        person = person_index.get((dom, email)) or {}
        title = person.get("job_title") or ""
        # Defensive double-check on title shape — skip if it doesn't read like
        # buying committee (shouldn't fire given the ES filter, but cheap to verify).
        if not _looks_like_buying_committee(title):
            continue
        alerts.append({
            "name": person.get("full_name") or email or "(unknown)",
            "title": title or "(unknown title)",
            "domain": dom,
            "company_name": meta.get("name") or dom,
            "company_id": meta.get("company_id"),
            "linkedin": person.get("linkedin_url"),
            "email": email,
        })

    if not alerts:
        logger.info("no ICP buying-committee contacts created this run; no Slack post")
        return 0

    # Compose Slack blocks
    header = (
        f":dart: *{len(alerts)} new buying-committee "
        f"contact{'s' if len(alerts) != 1 else ''} identified*"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "New high-intent contact"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]
    for a in alerts:
        lines = [f"*{a['name']}* — {a['title']}"]
        lines.append(f"at *{a['company_name']}* ({a['domain']})")
        if a.get("email"):
            lines.append(f"`{a['email']}`")
        if a.get("linkedin"):
            lines.append(f"<https://{a['linkedin']}|LinkedIn>")
        link = company_link(a.get("company_id"))
        if link:
            lines.append(f"<{link}|Open in HubSpot →>")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    fallback = f"{len(alerts)} new buying-committee contact(s): " + ", ".join(
        f"{a['name']} ({a['title']}) @ {a['company_name']}" for a in alerts
    )
    post_slack(blocks, fallback)
    return len(alerts)


# -----------------------------------------------------------------------------
# Mode: digest — daily summary
# -----------------------------------------------------------------------------
def digest() -> None:
    """Post a daily summary of the most recent pipeline state to Slack."""
    company_writes = _load_json(config.HUBSPOT_RESULTS_FILE) or []
    contact_writes = _load_json(config.HUBSPOT_CONTACT_RESULTS_FILE) or []
    enriched = _load_json(config.ENRICHED_FILE) or {}

    # Company stats
    created_companies = [c for c in company_writes if c.get("action") == "created"]
    updated_companies = [c for c in company_writes if c.get("action") == "updated"]
    icp_companies = [c for c in company_writes if c.get("is_icp_fit")]

    # Contact stats
    created_contacts = [c for c in contact_writes if c.get("action") == "created"]
    updated_contacts = [c for c in contact_writes if c.get("action") == "updated"]

    # Top ICP companies by visit count (from enriched, since visit_count is per-IP)
    matched = [
        r for r in enriched.values()
        if (r.get("enrichment") or {}).get("status") == "matched"
        and icp_filter.is_icp_fit(r)
    ]
    top = sorted(matched, key=lambda r: int(r.get("visit_count") or 0), reverse=True)[:3]

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Daily pipeline summary"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*Companies:* {len(created_companies)} created · "
            f"{len(updated_companies)} updated · "
            f"{len(icp_companies)} ICP-fit\n"
            f"*Contacts:* {len(created_contacts)} created · "
            f"{len(updated_contacts)} updated"
        }},
    ]

    if top:
        top_lines = []
        for r in top:
            company = (r.get("enrichment") or {}).get("company") or {}
            top_lines.append(
                f"• *{company.get('name') or '?'}* — "
                f"{company.get('size') or '?'} · "
                f"{int(r.get('visit_count') or 0)} visits"
            )
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
            "*Top ICP visitors (by visit count):*\n" + "\n".join(top_lines)
        }})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            "_No ICP-fit visitors in current dataset._"
        }]})

    fallback = (
        f"Pipeline summary — "
        f"{len(created_companies)} new companies, "
        f"{len(created_contacts)} new contacts, "
        f"{len(icp_companies)} ICP-fit"
    )
    post_slack(blocks, fallback)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("run", "digest"), required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.mode == "run":
        alert_for_run()
    elif args.mode == "digest":
        digest()
    return 0


if __name__ == "__main__":
    sys.exit(main())
