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

    # Build dual lookups: (domain, email) and (domain, linkedin_url).
    # Contacts created via LinkedIn fallback have no email, so we need the
    # second index to find their title for the Slack message.
    from hubspot_client import canonicalize_linkedin_url
    person_by_email: dict[tuple[str, str], dict[str, Any]] = {}
    person_by_linkedin: dict[tuple[str, str], dict[str, Any]] = {}
    for dom, persons in people_by_domain.items():
        dom_lower = (dom or "").lower()
        for p in persons:
            email = (p.get("work_email") or "").strip().lower()
            li = canonicalize_linkedin_url(p.get("linkedin_url"))
            if email:
                person_by_email[(dom_lower, email)] = p
            if li:
                person_by_linkedin[(dom_lower, li)] = p

    alerts: list[dict[str, Any]] = []
    for write in contact_writes:
        if write.get("action") != "created":
            continue
        dom = (write.get("domain") or "").lower()
        meta = company_meta.get(dom) or {}
        if not meta.get("is_icp_fit"):
            continue
        # Look up the person by whichever key was used for the upsert.
        match_key = write.get("match_key") or "email"
        match_value = (write.get("match_value") or write.get("email") or "").strip().lower()
        if match_key == "linkedin_url":
            person = person_by_linkedin.get((dom, match_value)) or {}
        else:
            person = person_by_email.get((dom, match_value)) or {}
        title = person.get("job_title") or ""
        email_for_display = person.get("work_email") or ""
        # No defensive title re-check here — Step 6's PDL query already
        # filtered to TARGET_TITLE_CLAUSES, so anything reaching this point
        # via `created` + ICP-fit is by definition a buying-committee match.
        # A stricter re-check (regex) was rejecting valid titles like
        # "vice president of partner sales" because they used "vice president"
        # instead of the abbreviation "vp".
        alerts.append({
            "name": person.get("full_name") or match_value or "(unknown)",
            "title": title or "(unknown title)",
            "domain": dom,
            "company_name": meta.get("name") or dom,
            "company_id": meta.get("company_id"),
            "linkedin": person.get("linkedin_url"),
            "email": email_for_display,
        })

    if not alerts:
        logger.info("no ICP buying-committee contacts created this run; no Slack post")
        return 0

    # Slack accepts ≤50 blocks per message via webhook; each alert consumes
    # one section block. Cap visible alerts so a battle-test run with 100+
    # creates doesn't get rejected. Header + footer take ~5 blocks → 40
    # leftover is plenty, but stay conservative for readability too.
    MAX_VISIBLE = 20
    visible = alerts[:MAX_VISIBLE]
    hidden = len(alerts) - len(visible)

    header = (
        f":dart: *{len(alerts)} new buying-committee "
        f"contact{'s' if len(alerts) != 1 else ''} identified*"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "New high-intent contacts"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]
    for a in visible:
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

    if hidden > 0:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"_…and {hidden} more in HubSpot. Full list in `data/hubspot_contact_writes.json`._"
        }]})

    fallback = f"{len(alerts)} new buying-committee contact(s): " + ", ".join(
        f"{a['name']} ({a['title']}) @ {a['company_name']}" for a in visible
    ) + (f" (+{hidden} more)" if hidden else "")
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
