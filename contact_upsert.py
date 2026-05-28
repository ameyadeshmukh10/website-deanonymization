"""Step 7 — upsert HubSpot Contacts from data/people.json + associate to companies.

Reads:
  - data/people.json (from Step 6): {domain: [person, ...]}
  - data/hubspot_writes.json (from Step 4): used to look up company_id by domain.

For each person:
  - Skip if no work_email (HubSpot Contact match key is email).
  - Search HubSpot by email; if found → update, else → create.
  - Associate to the matched company.

Output: data/hubspot_contact_writes.json — per-person action log.

Pre-flight: probes the token for contact read/write scope; fails fast if missing.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import config
from hubspot_client import (
    _session,
    associate_contact_to_company,
    canonicalize_linkedin_url,
    create_contact,
    find_contact_by_email,
    find_contact_by_linkedin,
    linkedin_property_exists,
    update_contact,
    verify_contact_access,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# IO
# -----------------------------------------------------------------------------
def load_people() -> dict[str, list[dict[str, Any]]]:
    if not config.PEOPLE_FILE.exists():
        raise FileNotFoundError(
            f"{config.PEOPLE_FILE} not found. Run Step 6 (person_lookup) first."
        )
    return json.loads(config.PEOPLE_FILE.read_text())


def load_company_id_map() -> dict[str, str]:
    """domain -> HubSpot company id, sourced from the most recent Step 4 run."""
    if not config.HUBSPOT_RESULTS_FILE.exists():
        return {}
    results = json.loads(config.HUBSPOT_RESULTS_FILE.read_text())
    out: dict[str, str] = {}
    for r in results:
        cid = r.get("company_id")
        dom = r.get("domain")
        if cid and dom:
            out[dom.lower()] = cid
    return out


def save_results(results: list[dict[str, Any]]) -> None:
    config.HUBSPOT_CONTACT_RESULTS_FILE.write_text(json.dumps(results, indent=2))
    logger.info(
        "wrote %d contact results to %s",
        len(results), config.HUBSPOT_CONTACT_RESULTS_FILE,
    )


# -----------------------------------------------------------------------------
# Person -> HubSpot properties
# -----------------------------------------------------------------------------
def _build_properties(
    person: dict[str, Any],
    canonical_linkedin: str | None,
    *,
    write_linkedin: bool,
) -> dict[str, Any]:
    """Map a PDL person row to HubSpot contact properties.

    Email and LinkedIn are written whenever present and usable. Returns an
    empty dict if neither is present — caller is responsible for skipping.
    """
    props: dict[str, Any] = {}
    email = (person.get("work_email") or "").strip().lower()
    if email:
        props["email"] = email
    if person.get("first_name"):
        props["firstname"] = person["first_name"]
    if person.get("last_name"):
        props["lastname"] = person["last_name"]
    if person.get("job_title"):
        props["jobtitle"] = person["job_title"]
    if write_linkedin and canonical_linkedin:
        props[config.HUBSPOT_LINKEDIN_PROPERTY] = canonical_linkedin
    return props


# -----------------------------------------------------------------------------
# Upsert one person
# -----------------------------------------------------------------------------
def upsert_person(
    session,
    person: dict[str, Any],
    company_id: str | None,
    *,
    dry_run: bool,
    linkedin_enabled: bool,
) -> dict[str, Any]:
    """Upsert one contact, matching by email if present, else by LinkedIn URL.

    `linkedin_enabled` is False when the HubSpot portal lacks the configured
    LinkedIn property — in that case we never search or write to it.
    """
    email = (person.get("work_email") or "").strip().lower() or None
    canonical_linkedin = canonicalize_linkedin_url(person.get("linkedin_url"))

    match_key: str | None = None
    match_value: str | None = None
    if email:
        match_key = "email"
        match_value = email
    elif linkedin_enabled and canonical_linkedin:
        match_key = "linkedin_url"
        match_value = canonical_linkedin

    if not match_key:
        return {
            "pdl_id": person.get("pdl_id"),
            "domain": person.get("company_domain"),
            "action": "skipped",
            "reason": "no_email_no_linkedin",
        }

    props = _build_properties(
        person, canonical_linkedin, write_linkedin=linkedin_enabled,
    )
    if not props:
        return {
            "pdl_id": person.get("pdl_id"),
            "domain": person.get("company_domain"),
            "action": "skipped",
            "reason": "no_writable_properties",
        }

    if dry_run:
        return {
            "pdl_id": person.get("pdl_id"),
            "domain": person.get("company_domain"),
            "match_key": match_key,
            "match_value": match_value,
            "action": "dry_run",
            "properties": props,
            "company_id": company_id,
        }

    if match_key == "email":
        contact_id = find_contact_by_email(session, match_value)
    else:
        contact_id = find_contact_by_linkedin(session, match_value)

    if contact_id:
        update_contact(session, contact_id, props)
        action = "updated"
    else:
        created = create_contact(session, props)
        contact_id = created.get("id")
        action = "created"

    associated = False
    if contact_id and company_id:
        try:
            associate_contact_to_company(session, contact_id, company_id)
            associated = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("association failed for %s: %s", match_value, exc)

    return {
        "pdl_id": person.get("pdl_id"),
        "domain": person.get("company_domain"),
        "match_key": match_key,
        "match_value": match_value,
        "action": action,
        "contact_id": contact_id,
        "company_id": company_id,
        "associated": associated,
    }


# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
def run(*, dry_run: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
    config.assert_env("HUBSPOT_API_KEY")
    session = _session()
    verify_contact_access(session)

    # Decide if LinkedIn-URL fallback is available on this portal.
    linkedin_enabled = linkedin_property_exists(session)
    if linkedin_enabled:
        logger.info(
            "LinkedIn fallback active: matching/writing via `%s`",
            config.HUBSPOT_LINKEDIN_PROPERTY,
        )
    else:
        logger.warning(
            "LinkedIn property `%s` not found on contact object — falling back "
            "to email-only. Create the custom property in HubSpot (Settings → "
            "Properties → Contact properties, type: Single-line text) to "
            "enable LinkedIn-URL matching when PDL doesn't return work_email.",
            config.HUBSPOT_LINKEDIN_PROPERTY,
        )

    people = load_people()
    company_ids = load_company_id_map()

    # Flatten + de-dupe across companies. Use email when present, else the
    # canonical LinkedIn URL, so the same human under two domains writes once.
    flat: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for domain, rows in people.items():
        for p in rows:
            email = (p.get("work_email") or "").strip().lower()
            li = canonicalize_linkedin_url(p.get("linkedin_url"))
            dedupe_key = email or (li if linkedin_enabled else "")
            if dedupe_key and dedupe_key in seen_keys:
                continue
            if dedupe_key:
                seen_keys.add(dedupe_key)
            flat.append(p)

    if limit:
        flat = flat[:limit]
        logger.info("limit=%d in effect; processing %d people", limit, len(flat))

    counts = {"created": 0, "updated": 0, "skipped": 0, "dry_run": 0, "error": 0}
    results: list[dict[str, Any]] = []
    for person in flat:
        domain = (person.get("company_domain") or "").lower()
        company_id = company_ids.get(domain)
        if not company_id:
            logger.warning(
                "no HubSpot company_id for %s; person will be upserted but not "
                "associated", domain,
            )
        try:
            result = upsert_person(
                session, person, company_id,
                dry_run=dry_run, linkedin_enabled=linkedin_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "upsert failed for pdl_id=%s linkedin=%s",
                person.get("pdl_id"), person.get("linkedin_url"),
            )
            result = {
                "pdl_id": person.get("pdl_id"),
                "domain": domain,
                "action": "error",
                "error": str(exc),
            }
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        results.append(result)

    logger.info("contact writes: %s", counts)
    save_results(results)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(dry_run=args.dry_run, limit=args.limit)
