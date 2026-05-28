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
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

import config
import icp_filter

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()

# --- Chunked delivery knobs -------------------------------------------------
# Slack incoming webhooks accept ≤50 blocks per message. Each alert consumes
# one section block; the chunk header/divider add ~4 blocks. 15 alerts per
# chunk = ~19 blocks, well under the limit. Slack also rate-limits webhooks
# to ~1 msg/sec; we sleep 1s between chunks to avoid 429s.
CHUNK_SIZE = 15
CHUNK_DELAY_SEC = 1.0

# Per-alert pages-visited truncation — keeps each section block readable
# and well under Slack's 3000-char-per-block limit.
MAX_PAGES_SHOWN = 5


# -----------------------------------------------------------------------------
# Slack transport
# -----------------------------------------------------------------------------
def post_slack(blocks: list[dict[str, Any]], text_fallback: str) -> bool:
    """POST one Slack message. Returns True on 2xx, False otherwise.

    No-ops (returns True) if SLACK_WEBHOOK_URL is unset, so local runs
    work without configuring the webhook.
    """
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL not set; skipping Slack post (would have sent: %s)", text_fallback)
        return True
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": text_fallback, "blocks": blocks},
        timeout=15,
    )
    if resp.status_code >= 400:
        logger.warning(
            "Slack webhook returned %s: %s", resp.status_code, resp.text[:300]
        )
        return False
    logger.info("posted to Slack: %s", text_fallback)
    return True


# -----------------------------------------------------------------------------
# HubSpot deep-link helpers
# -----------------------------------------------------------------------------
def company_link(company_id: str | None) -> str | None:
    """Modern HubSpot company record URL (CRM v3 format)."""
    if not company_id or not config.HUBSPOT_PORTAL_ID:
        return None
    return (
        f"{config.HUBSPOT_UI_BASE}/contacts/{config.HUBSPOT_PORTAL_ID}"
        f"/record/0-2/{company_id}"
    )


def contact_link(contact_id: str | None) -> str | None:
    """Modern HubSpot contact record URL (CRM v3 format)."""
    if not contact_id or not config.HUBSPOT_PORTAL_ID:
        return None
    return (
        f"{config.HUBSPOT_UI_BASE}/contacts/{config.HUBSPOT_PORTAL_ID}"
        f"/record/0-1/{contact_id}"
    )


# -----------------------------------------------------------------------------
# Time formatting for visit context
# -----------------------------------------------------------------------------
def _humanize_last_visited(iso_str: str | None) -> str:
    """Turn an ISO timestamp into a friendly 'N minutes/hours/days ago' string.

    Falls back to the raw ISO string if parsing fails. Empty/None returns "".
    """
    if not iso_str or not isinstance(iso_str, str):
        return ""
    s = iso_str.replace("Z", "+00:00") if iso_str.endswith("Z") else iso_str
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta_sec = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta_sec < 60:
        return "just now"
    if delta_sec < 3600:
        m = int(delta_sec // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta_sec < 86400:
        h = int(delta_sec // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(delta_sec // 86400)
    return f"{d} day{'s' if d != 1 else ''} ago"


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

    # domain -> {is_icp_fit, company_id, name, last_visited, pages}
    company_meta: dict[str, dict[str, Any]] = {}
    for c in company_writes:
        dom = (c.get("domain") or "").lower()
        if dom:
            company_meta[dom] = {
                "is_icp_fit": bool(c.get("is_icp_fit")),
                "company_id": c.get("company_id"),
                "name": c.get("name"),
                "last_visited": c.get("website_last_visited"),
                "pages": c.get("website_pages_visited") or [],
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
            "contact_id": write.get("contact_id"),
            "linkedin": person.get("linkedin_url"),
            "email": email_for_display,
            "last_visited": meta.get("last_visited"),
            "pages": meta.get("pages") or [],
        })

    if not alerts:
        logger.info("no ICP buying-committee contacts created this run; no Slack post")
        return 0

    # Chunk into Slack-sized messages so all alerts get delivered (no cap).
    total = len(alerts)
    num_chunks = max(1, math.ceil(total / CHUNK_SIZE))
    posted_chunks = 0
    for chunk_idx in range(num_chunks):
        chunk = alerts[chunk_idx * CHUNK_SIZE : (chunk_idx + 1) * CHUNK_SIZE]
        page_label = (
            f" (page {chunk_idx + 1}/{num_chunks})" if num_chunks > 1 else ""
        )
        header_text = (
            f":dart: *{total} new buying-committee "
            f"contact{'s' if total != 1 else ''} identified*{page_label}"
        )
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "New high-intent contacts"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
            {"type": "divider"},
        ]
        for a in chunk:
            lines = [f"*{a['name']}* — {a['title']}"]
            lines.append(f"at *{a['company_name']}* ({a['domain']})")
            if a.get("email"):
                lines.append(f"`{a['email']}`")
            # Visit context — what pages they viewed and when
            if a.get("last_visited"):
                human = _humanize_last_visited(a["last_visited"])
                lines.append(f"*Last visited:* {human}")
            pages = a.get("pages") or []
            if pages:
                shown = pages[:MAX_PAGES_SHOWN]
                extra = len(pages) - len(shown)
                pages_str = "\n".join(f"  • `{p}`" for p in shown)
                if extra > 0:
                    pages_str += f"\n  _…and {extra} more_"
                lines.append("*Pages visited:*\n" + pages_str)
            # Links — LinkedIn + both HubSpot record links
            link_parts: list[str] = []
            if a.get("linkedin"):
                link_parts.append(f"<https://{a['linkedin']}|LinkedIn>")
            c_link = contact_link(a.get("contact_id"))
            if c_link:
                link_parts.append(f"<{c_link}|View contact →>")
            co_link = company_link(a.get("company_id"))
            if co_link:
                link_parts.append(f"<{co_link}|Open company →>")
            if link_parts:
                lines.append(" · ".join(link_parts))
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

        fallback = (
            f"{total} new buying-committee contact(s){page_label}: "
            + ", ".join(f"{a['name']} ({a['title']}) @ {a['company_name']}" for a in chunk)
        )
        ok = post_slack(blocks, fallback)
        if ok:
            posted_chunks += 1
        # Respect Slack's ~1 msg/sec webhook rate limit between chunks
        if chunk_idx < num_chunks - 1:
            time.sleep(CHUNK_DELAY_SEC)

    logger.info(
        "sent %d/%d Slack chunk(s) for %d alerts",
        posted_chunks, num_chunks, total,
    )
    return total


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
