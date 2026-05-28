"""Shared configuration for the Website Deanonymization pipeline.

Everything env-driven is loaded here so the rest of the modules can `import config`
and stay agnostic about where values come from.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (the directory this file lives in).
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

IP_VISITS_FILE = DATA_DIR / "ip_visits.json"
HIGH_INTENT_FILE = DATA_DIR / "high_intent_ips.json"
ENRICHED_FILE = DATA_DIR / "enriched_ips.json"
HUBSPOT_RESULTS_FILE = DATA_DIR / "hubspot_writes.json"
QUALIFIED_VISITORS_FILE = DATA_DIR / "qualified_visitors.json"
PEOPLE_FILE = DATA_DIR / "people.json"
HUBSPOT_CONTACT_RESULTS_FILE = DATA_DIR / "hubspot_contact_writes.json"

# Per-IP "visit_count we've already credited to HubSpot" state. Lets Step 4
# write only the delta on each run so visit_count is truly cumulative — no
# double-counting on re-runs, no resets if KV is purged.
CREDITED_STATE_FILE = DATA_DIR / "credited_counts.json"

# -----------------------------------------------------------------------------
# Cloudflare Worker (Step 1B)
# -----------------------------------------------------------------------------
_raw_worker_url = (os.getenv("WORKER_BASE_URL") or "").strip()
# Aggressively clean common paste mistakes: leading slashes (e.g. `//host`
# or `/host`), trailing slashes, surrounding quotes/backticks. Then assume
# https:// if no scheme present.
_raw_worker_url = _raw_worker_url.strip("`\"' ")
while _raw_worker_url.startswith("/"):
    _raw_worker_url = _raw_worker_url[1:]
_raw_worker_url = _raw_worker_url.rstrip("/")
if _raw_worker_url and not _raw_worker_url.startswith(("http://", "https://")):
    _raw_worker_url = "https://" + _raw_worker_url
WORKER_BASE_URL = _raw_worker_url

# One-shot diagnostic log so a CI failure tells us exactly what shape the
# secret arrived in — without leaking the host (which would be masked anyway).
# Logs once at module load.
if os.getenv("CI") or os.getenv("GITHUB_ACTIONS"):
    import urllib.parse as _urlparse
    _parsed = _urlparse.urlparse(WORKER_BASE_URL)
    print(
        f"[config] WORKER_BASE_URL diagnostics: "
        f"raw_len={len(os.getenv('WORKER_BASE_URL') or '')} "
        f"final_len={len(WORKER_BASE_URL)} "
        f"scheme={_parsed.scheme!r} "
        f"has_host={bool(_parsed.hostname)} "
        f"path={_parsed.path!r}",
        flush=True,
    )
WORKER_ADMIN_TOKEN = os.getenv("WORKER_ADMIN_TOKEN") or ""
WORKER_EXPORT_PAGE_LIMIT = 1000  # max allowed by Cloudflare KV `list`

# -----------------------------------------------------------------------------
# Step 2: high-intent filter
# -----------------------------------------------------------------------------
# An IP qualifies as high-intent if EITHER:
#   - visit_count >= MIN_VISITS_FOR_INTENT, OR
#   - at least one of its unique_pages matches a HIGH_INTENT_PATH.
MIN_VISITS_FOR_INTENT = 2

# Paths that signal commercial intent. Match is case-insensitive prefix match
# against `path` (the URL path, e.g. "/pricing"). Edit to suit your site.
HIGH_INTENT_PATHS: tuple[str, ...] = (
    "/pricing",
    "/demo",
    "/sales-ai-workers/sdr-lead-response-worker",
    "/sales-ai-workers/pipeline-reporting-ai-worker",
    "/sales-ai-workers/sdr-ai-worker-full",
    "/lets-talk",
    "/sales-ai-workers",
    "/sales-ai-workers/sdr-ai-worker",
    "/sales-ai-workers/sales-playbook-ai-worker",
)

# -----------------------------------------------------------------------------
# Step 3: People Data Labs
# -----------------------------------------------------------------------------
PDL_API_KEY = os.getenv("PDL_API_KEY") or ""
PDL_IP_ENDPOINT = "https://api.peopledatalabs.com/v5/ip/enrich"
# PDL IP API supports both IPv4 and IPv6. Use GET with ?ip=<ip>; POST is 405.

# Cache PDL responses keyed by IP. Different TTLs by outcome — a `matched`
# company assignment is stable for days; a `no_match` or `ineligible` might
# resolve sooner as PDL improves its IP-to-company graph.
PDL_CACHE_FILE = DATA_DIR / "pdl_cache.json"
PDL_CACHE_TTL_MATCHED_DAYS = 7
PDL_CACHE_TTL_NEGATIVE_DAYS = 1
# Min seconds between PDL calls to stay under their per-second rate limit.
# 12 req/s caused a 429 mid-batch; 0.12s = ~8 req/s keeps comfortable headroom.
PDL_MIN_INTERVAL_SEC = 0.12

# --- ICP industry gate (Steps 4 + 5) ----------------------------------------
# PDL company.industry values that count as B2B tech ICP. Companies outside
# this set are written to HubSpot tagged `is_icp_fit=false` (for visibility)
# but never reach the Person Search step. Strict bundle per user spec —
# expand here if pipeline starts undershooting.
ICP_INDUSTRIES: frozenset[str] = frozenset({
    "computer software",
    "information technology and services",
})

# HubSpot custom company property name. Auto-created on first Step 4 run if
# missing — see hubspot_client.ensure_icp_property.
HUBSPOT_ICP_FIT_PROPERTY = "is_icp_fit"

# --- Step 5: visitor role qualifier -----------------------------------------
# Substrings (case-insensitive) we look for in PDL's inferred person.role and
# person.sub_role from the IP enrichment. Any hit qualifies the IP for Step 6.
QUALIFYING_ROLE_PATTERNS: tuple[str, ...] = (
    "sales",
    "business_development",
    "marketing",
    "gtm",
    "growth",
    "revenue",
)

# --- Step 6: PDL Person Search ----------------------------------------------
PDL_PERSON_SEARCH_ENDPOINT = "https://api.peopledatalabs.com/v5/person/search"
PDL_PERSON_CACHE_FILE = DATA_DIR / "pdl_person_cache.json"
PDL_PERSON_CACHE_TTL_DAYS = 7

# Person Search results are always filtered to US.
PERSON_SEARCH_COUNTRY = "united states"

# Buying-committee titles we want per ICP company. Each entry is an ES `should`
# clause; Step 6 ORs them in a single query. Per-result billing; expect 3-12
# credits per company (most companies populate 5-10 of these clauses).
TARGET_TITLE_CLAUSES: list[dict] = [
    # Sales leadership (level + role taxonomy)
    {"bool": {"must": [
        {"term": {"job_title_levels": "vp"}},
        {"term": {"job_title_role": "sales"}},
    ]}},  # VP Sales
    {"bool": {"must": [
        {"term": {"job_title_levels": "cxo"}},
        {"term": {"job_title_role": "sales"}},
    ]}},  # CSO etc. via role taxonomy
    # CRO / CCO by exact phrase — PDL doesn't always tag CRO under role=sales
    {"bool": {"must": [
        {"term": {"job_title_levels": "cxo"}},
        {"match_phrase": {"job_title": "chief revenue officer"}},
    ]}},
    {"bool": {"must": [
        {"term": {"job_title_levels": "cxo"}},
        {"match_phrase": {"job_title": "chief commercial officer"}},
    ]}},
    # Marketing leadership
    {"bool": {"must": [
        {"term": {"job_title_levels": "vp"}},
        {"term": {"job_title_role": "marketing"}},
    ]}},  # VP Marketing
    {"bool": {"must": [
        {"term": {"job_title_levels": "cxo"}},
        {"term": {"job_title_role": "marketing"}},
    ]}},  # CMO
    # "Head of …" — phrase match catches even when PDL didn't tag a level
    {"match_phrase": {"job_title": "head of sales"}},
    {"match_phrase": {"job_title": "head of marketing"}},
    {"match_phrase": {"job_title": "head of go-to-market"}},
    {"match_phrase": {"job_title": "head of gtm"}},
    {"match_phrase": {"job_title": "head of demand generation"}},
    {"match_phrase": {"job_title": "head of revenue operations"}},
    # Senior+ specialists (RevOps / Demand Gen / GTM) — restrict to
    # cxo/vp/director per user decision; no IC matches.
    {"bool": {"must": [
        {"terms": {"job_title_levels": ["cxo", "vp", "director"]}},
        {"bool": {"should": [
            {"match_phrase": {"job_title": "revenue operations"}},
            {"match_phrase": {"job_title": "revops"}},
        ]}},
    ]}},
    {"bool": {"must": [
        {"terms": {"job_title_levels": ["cxo", "vp", "director"]}},
        {"match_phrase": {"job_title": "demand generation"}},
    ]}},
    {"bool": {"must": [
        {"terms": {"job_title_levels": ["cxo", "vp", "director"]}},
        {"bool": {"should": [
            {"match_phrase": {"job_title": "go-to-market"}},
            {"match_phrase": {"job_title": "gtm"}},
        ]}},
    ]}},
]

PERSON_SEARCH_RESULT_CAP = 20  # max results returned per company query

# -----------------------------------------------------------------------------
# Step 4: HubSpot
# -----------------------------------------------------------------------------
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY") or ""
HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_COMPANY_PROPERTIES = f"{HUBSPOT_BASE}/crm/v3/properties/companies"
HUBSPOT_COMPANY_SEARCH = f"{HUBSPOT_BASE}/crm/v3/objects/companies/search"
HUBSPOT_COMPANIES = f"{HUBSPOT_BASE}/crm/v3/objects/companies"
HUBSPOT_PAGE_LIMIT = 100


# HubSpot Private App tokens encode the data residency region in the prefix
# (`pat-eu1-`, `pat-na1-`, `pat-au1-`). The app UI host varies by region, so
# auto-detect for deep-links that actually land on the user's portal.
def _hubspot_ui_base(api_key: str) -> str:
    if api_key.startswith("pat-eu1-"):
        return "https://app-eu1.hubspot.com"
    if api_key.startswith("pat-au1-"):
        return "https://app-au1.hubspot.com"
    if api_key.startswith("pat-ca1-"):
        return "https://app-ca1.hubspot.com"
    return "https://app.hubspot.com"


HUBSPOT_UI_BASE = _hubspot_ui_base(HUBSPOT_API_KEY)

# Strip non-digits. Catches paste mistakes that prepend "# " or other
# markdown-comment chars (which previously produced `# %20` in Slack URLs).
_raw_portal = (os.getenv("HUBSPOT_PORTAL_ID") or "").strip()
HUBSPOT_PORTAL_ID = "".join(c for c in _raw_portal if c.isdigit())

# --- Step 7: HubSpot contacts -----------------------------------------------
HUBSPOT_CONTACT_SEARCH = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
HUBSPOT_CONTACT_PROPERTIES = f"{HUBSPOT_BASE}/crm/v3/properties/contacts"
HUBSPOT_CONTACTS = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
# Association API (v4) for contact -> company default association
HUBSPOT_CONTACT_TO_COMPANY = (
    f"{HUBSPOT_BASE}/crm/v4/objects/contacts/{{contact_id}}/associations/default"
    f"/companies/{{company_id}}"
)

# Custom contact property used as the LinkedIn-URL match key when work_email
# is masked by PDL's plan tier. If this property doesn't exist on your
# portal, Step 7 logs a warning and falls back to email-only matching.
# Override via env var of the same name if your portal uses a different
# property (e.g. `hs_linkedin_url`).
HUBSPOT_LINKEDIN_PROPERTY = (
    os.getenv("HUBSPOT_LINKEDIN_PROPERTY") or "linkedin_url"
)

# Company properties this pipeline writes. Create these in HubSpot before the
# first run. The pre-flight check in hubspot_client warns on any missing.
TARGET_PROPERTIES: tuple[str, ...] = (
    "website_visit_count",       # number
    "website_last_visited",      # datetime (HubSpot expects ms-epoch)
    "website_pages_visited",     # multi-line text
    "website_visit_intent",      # single-line text
    "is_icp_fit",                # boolean (HubSpot booleancheckbox)
)

# -----------------------------------------------------------------------------
# Retries / HTTP
# -----------------------------------------------------------------------------
HTTP_TIMEOUT = 30  # seconds
MAX_RETRIES = 5
BACKOFF_MIN = 1
BACKOFF_MAX = 16


def assert_env(*names: str) -> None:
    """Raise RuntimeError if any of the named env vars are blank.

    Call this at the top of each step's entrypoint so misconfig fails loudly
    instead of producing a confusing 401 / empty payload mid-run.
    """
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required env var(s): " + ", ".join(missing) +
            f". Set them in {PROJECT_ROOT / '.env'} (see .env.example)."
        )
