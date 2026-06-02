# Website Deanonymization Pipeline — Architecture

This document explains exactly what this system does, how each piece fits
together, and the design decisions behind each step. Audience: someone
about to extend the pipeline, debug it, or take it over.

---

## 1. What it does (in one paragraph)

Anonymous visitors arrive at everworker.ai. The site fires a tracking pixel
that POSTs each pageview (IP + path + UA + a few flags) to a Cloudflare
Worker, which writes per-IP visit records into Cloudflare KV. On a 4-hour
schedule, a Python pipeline runs in GitHub Actions: it pulls the latest
KV state, filters out bots / datacenter / proxy traffic, calls People
Data Labs (PDL) to resolve each IP to a company and (when available) a
person inference, gates the result against a strict B2B-software ICP,
then queries PDL Person Search at qualifying companies for top-of-org
sales leadership. Any newly-created sales leadership contact gets written
to HubSpot (associated to the matched company) and posted to Slack with
context — name, title, email, LinkedIn, what pages they viewed, and
clickable links to both the company and contact records in HubSpot.

---

## 2. High-level architecture

```
   visitor browser
        │
        │  POST /collect  (JS pixel)
        ▼
  ┌───────────────────────────────────┐
  │  Cloudflare Worker                │
  │  cloudflaredeanonymizationsetup/  │
  │  src/worker.js                    │
  │                                   │
  │  - Drops bot UAs                  │
  │  - Drops datacenter / VPN ASNs    │
  │  - Drops high-threat-score IPs    │
  │  - Drops verified bots            │
  │  - Otherwise: upsert into KV      │
  └───────────────────────────────────┘
        │
        │  KV (ip:<addr> → JSON)
        ▼
  ┌───────────────────────────────────┐
  │  GET /export   (admin-token gated)│
  └───────────────────────────────────┘
        │
        ▼
  ┌───────────────────────────────────┐
  │  Python pipeline                  │
  │  run_pipeline.py + 7 step modules │
  │                                   │
  │  Step 1B → 2 → 3 → 4 → 5 → 6 → 7  │
  │                                   │
  │  Step 3: PDL IP Enrich            │
  │  Step 6: PDL Person Search        │
  │  Step 4/7: HubSpot Companies/     │
  │            Contacts               │
  │                                   │
  │  notify.py: instant Slack alerts  │
  │             + daily digest        │
  └───────────────────────────────────┘
        │                       │
        │                       │
        ▼                       ▼
  ┌─────────────┐         ┌──────────────┐
  │  HubSpot    │         │  Slack       │
  │  Companies, │         │  webhook     │
  │  Contacts   │         │              │
  └─────────────┘         └──────────────┘

  Orchestrated by:
  ┌───────────────────────────────────────────────────────┐
  │  GitHub Actions                                       │
  │  .github/workflows/run-pipeline.yml    every 4h       │
  │  .github/workflows/daily-summary.yml   daily 11 UTC   │
  │                                                       │
  │  State persists between runs via the                  │
  │  `pipeline-state` artifact (30d retention)            │
  └───────────────────────────────────────────────────────┘
```

---

## 3. System components

### 3.1 Capture layer — Cloudflare Worker

**Where**: `cloudflaredeanonymizationsetup/src/worker.js`

**Runtime**: Cloudflare's Workers runtime (V8). Deployed via Wrangler.

**Inputs**: Three HTTP endpoints exposed at
`https://website-deanon.ameya-deshmukh.workers.dev`:

| Endpoint | Method | Auth | Purpose |
| --- | --- | --- | --- |
| `/collect` | POST | none (CORS-gated to `everworker.ai`) | Tracking pixel posts visit events |
| `/export` | GET | `Bearer <admin token>` | Python pipeline pulls accumulated visits |
| `/health` | GET | none | Smoke test |

**`/collect` flow** (per request):

1. Reject if `CF-Connecting-IP` missing → 400
2. Reject if IP is RFC1918 / link-local / loopback → 200 `{ignored: "private_ip"}`
3. Reject if `cf.asOrganization` matches `DATACENTER_AS_PATTERN`
   (Amazon, AWS, Google, Microsoft Azure, OVH, Hetzner, DigitalOcean,
   Linode, Vercel, Netlify, NordVPN, ExpressVPN, etc. — full list in
   `worker.js`). → 200 `{ignored: "datacenter_asn"}`
4. Reject if `cf.threatScore > 20` → 200 `{ignored: "high_threat_score"}`
5. Reject if `cf.botManagement.verifiedBot === true` → 200
   `{ignored: "verified_bot"}`
6. Parse JSON body. Reject if missing `url` field → 400.
7. Reject if UA matches `BOT_REGEX` (Googlebot, Bingbot, GPTBot,
   Anthropic-AI, ClaudeBot, Lighthouse, Pingdom, headless Chrome,
   playwright, scrapy, etc.) → 200 `{ignored: "bot"}`
8. Read-modify-write the per-IP KV record:
   - `ip:<address>` → JSON `{ ip, first_seen, last_seen, visit_count,
     session_count, pages_visited[], unique_pages[], session_ids[],
     visitor_ids[], user_agents[] }`
   - Caps applied: max 500 pages_visited entries, 200 unique_pages,
     etc., to keep individual KV values manageable.

**Why the worker is the first defense line**: It uses signals Cloudflare
gives us for free (`cf.asOrganization`, `cf.threatScore`,
`cf.botManagement`) that the Python pipeline can't easily get. Catching
bots and datacenter IPs at capture time prevents them ever consuming KV
storage or PDL credits downstream.

### 3.2 Enrichment + CRM layer — Python pipeline

**Where**: project root. Files: `config.py`, `worker_client.py`,
`filter_intent.py`, `pdl_client.py`, `hubspot_client.py`,
`role_filter.py`, `person_lookup.py`, `contact_upsert.py`,
`icp_filter.py`, `run_pipeline.py`, `notify.py`.

**Runtime**: Python 3.12 (3.9-compatible via `from __future__ import
annotations`).

**Dependencies** (`requirements.txt`):
- `requests` — HTTP
- `python-dotenv` — `.env` loading
- `tenacity` — retry with exponential backoff on 429/5xx
- `click`, `rich` — unused legacy from the scaffold; safe to remove
  later

**Orchestrator**: `run_pipeline.py` — accepts CLI flags:

| Flag | Effect |
| --- | --- |
| `--only 1b,3,5` | Run just the listed steps |
| `--skip 4` | Run all steps except listed |
| `--dry-run` | Skip writes to HubSpot in Step 4 + Step 7 |
| `--limit N` | Cap how many records Step 3 / Step 4 / Step 6 / Step 7 process |
| `--all-ips` | Bypass Step 2 intent filter AND Step 5 role gate (battle-test mode) |

### 3.3 Automation — GitHub Actions

**Where**: `.github/workflows/`

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `run-pipeline.yml` | cron `0 */4 * * *` (every 4h) + `workflow_dispatch` | Downloads previous `pipeline-state` artifact → runs the pipeline → posts Slack alerts on new contacts → uploads new state artifact |
| `daily-summary.yml` | cron `0 11 * * *` (11 UTC = 7am EDT) + `workflow_dispatch` | Downloads latest state artifact → posts daily digest to Slack (no pipeline run) |

The `run-pipeline.yml` `workflow_dispatch` accepts an `all_ips` boolean
input — when true, the workflow runs `python run_pipeline.py --all-ips`
for a battle-test sweep (no high-intent filter, no role gate, just ICP).

**State persistence between CI runs**: Cache files in `data/`
(`pdl_cache.json`, `pdl_person_cache.json`, `credited_counts.json`,
`icp_fit_state.json`, etc.) need to survive across runs or caches reset
and credits get burned. Strategy:

- After each run: `actions/upload-artifact@v4` saves `data/` as
  `pipeline-state` artifact (retention 30 days).
- Before each run: `dawidd6/action-download-artifact@v3` grabs the most
  recent successful upload and unpacks `data/` from it.

This keeps the git repo clean (no committed cache files) while
preserving cache hits across runs.

### 3.4 Notification — Slack

**Where**: `notify.py`

**Transport**: Slack incoming webhook (free; one URL = one channel).

Two modes:

| Mode | When it fires | What it sends |
| --- | --- | --- |
| `--mode run` | After every pipeline run (in `run-pipeline.yml`) | One Slack message per chunk of 15 new ICP buying-committee contacts. Silent if no `created` contacts at ICP companies. |
| `--mode digest` | Daily at 11 UTC (in `daily-summary.yml`) | One Slack digest summarizing the most recent state: companies updated/created, contacts created, ICP-fit breakdown, top 3 ICP visitors by visit count. |

---

## 4. End-to-end data flow

Tracing a single buyer visit from browser to HubSpot contact + Slack
ping:

```
1. Buyer browses to everworker.ai/sales-ai-workers/sdr-ai-worker
       │
       │  tracking.js (loaded via the site header HTML)
       │  POST /collect
       │  body: { url, path, referrer, session_id, visitor_id,
       │          user_agent, timestamp }
       ▼
2. Cloudflare Worker handleCollect:
   - cf.asOrganization = "AT&T Services Inc." → not datacenter
   - cf.threatScore = 0 → ok
   - UA = real Chrome → not bot
   - KV upsert:
       ip:97.99.123.45 → {
         first_seen: "2026-06-01T15:00:00Z",
         visit_count: 1,
         unique_pages: ["/sales-ai-workers/sdr-ai-worker"],
         ...
       }
       │
       │  (visit stored. Worker returns 200 {ok: true})
       │
3. Three hours later, GitHub Actions run-pipeline.yml fires:
       │
       ▼
4. Step 1B (worker_client.run):
   - GET /export?limit=1000 (auth: Bearer <token>)
   - paginate via cursor until complete
   - write data/ip_visits.json (all IPs + accumulated counts)
       │
       ▼
5. Step 2 (filter_intent.run):
   - keep IPs with visit_count >= 2 OR unique_page hits a HIGH_INTENT_PATH
   - write data/high_intent_ips.json
   - (this visit only has count=1, but it hit /sales-ai-workers/* which
     is in HIGH_INTENT_PATHS → kept)
       │
       ▼
6. Step 3 (pdl_client.run):
   - for each high-intent IP, call PDL /v5/ip/enrich
   - 97.99.123.45 → status=matched, company={name: "Foo Inc",
     domain: "foo.com", industry: "computer software",
     tags: ["saas", "marketing automation"]},
     person={confidence: "high", job_title_role: "sales",
             job_title_sub_role: "business_development",
             job_title_levels: ["vp"]}
   - write data/enriched_ips.json (caches result in pdl_cache.json,
     ~1 credit per uncached IP)
       │
       ▼
7. Step 4 (hubspot_client.run):
   - aggregate enriched matched IPs by domain
   - for each domain, compute is_icp_fit via icp_filter.is_icp_fit:
       industry="computer software" ∈ ICP_INDUSTRIES → true
       tags=["saas","marketing automation"] doesn't hit any
         ICP_EXCLUDE_TAG_PATTERN → true stands
   - search HubSpot for domain=foo.com
   - read existing visit_count + is_icp_fit
   - manual-override check (icp_fit_state.json): if existing differs
     from what we last wrote AND wasn't previously absent, leave
     is_icp_fit alone. Otherwise write.
   - update or create company with:
       website_visit_count (cumulative via delta-tracking)
       website_pages_visited ("\n"-joined unique paths)
       website_visit_intent ("multi_visit:N, key_page:/foo, …")
       website_last_visited (HubSpot ms-epoch)
       is_icp_fit ("true"/"false" if not overridden)
   - write data/hubspot_writes.json
       │
       ▼
8. Step 5 (role_filter.run):
   - status=matched + is_icp_fit + person.job_title_role="sales" present
     + role_pattern hits "sales" → QUALIFIED
   - write data/qualified_visitors.json
       │
       ▼
9. Step 6 (person_lookup.run):
   - for each qualified company (foo.com), POST to
     /v5/person/search with ES query:
       must: company=foo.com, country=us, target title phrase
       must_not: 35 exclude patterns (regional, carrier, communications,
                                       enterprise architect, etc.)
       size: 20
   - PDL returns: e.g., 4 results — Sue Quense (CRO), James Lee (VP
     Sales), Maria Garcia (Head of RevOps), Tom Brown (VP of Sales)
   - shape each row (`_shape_person`) into our normalized format
   - write data/people.json + cache results in pdl_person_cache.json
   - cost: 1 PDL credit per result returned (4 credits this run)
       │
       ▼
10. Step 7 (contact_upsert.run):
    - for each person:
        - if work_email present: find HubSpot contact by email
        - elif LinkedIn fallback enabled + linkedin_url present:
          find by hs_linkedin_url
        - if found → update; else → create
        - associate contact to the company record
    - write data/hubspot_contact_writes.json
       │
       ▼
11. notify.py --mode run:
    - read hubspot_contact_writes.json
    - filter to action="created"
    - cross-reference hubspot_writes.json for is_icp_fit
    - for each qualifying contact, build a Slack section block:
        :dart: *Sue Quense* — chief revenue officer
        at *Foo Inc* (foo.com)
        `sue.quense@foo.com`
        *Last visited:* 3 hours ago
        *Pages visited:*
           • `/sales-ai-workers/sdr-ai-worker`
        <LinkedIn> · <View contact →> · <Open company →>
    - chunk into messages of ≤15 alerts each (Slack's 50-block limit)
    - POST each chunk to webhook, sleep 1s between
       │
       ▼
12. Upload pipeline-state artifact for next run
13. SALES SEES THE SLACK MESSAGE within ~4 hours of the original visit
```

---

## 5. The seven steps in detail

### Step 1B — Pull `/export` from the Worker

**File**: `worker_client.py`

**Reads**: nothing (queries the live Worker)
**Writes**: `data/ip_visits.json`

**Logic**: paginated GET `/export?limit=1000[&cursor=...]` with the
admin bearer token, accumulating IPs into a single dict until the
Worker returns `complete: true`. Cloudflare KV's `list` operation caps
at 1000 keys/page, so for >1000 IPs we follow the cursor.

**Retries**: tenacity exponential 1→16s on 429/5xx.

**Resilience**: explicit `RuntimeError` on 401 with a clear "check
WORKER_ADMIN_TOKEN" message (saved hours of debugging during initial
setup).

### Step 2 — High-intent filter

**File**: `filter_intent.py`

**Reads**: `data/ip_visits.json`
**Writes**: `data/high_intent_ips.json`

**Rule**: keep an IP iff
- `visit_count >= MIN_VISITS_FOR_INTENT` (default 2), OR
- any of its `unique_pages` starts with a `HIGH_INTENT_PATH` (e.g.
  `/pricing`, `/demo`, `/sales-ai-workers`, `/lets-talk` — configured
  per the site)

Each surviving record gets an `intent_reasons` array (e.g.,
`["multi_visit:4", "key_page:/sales-ai-workers"]`) for downstream
context.

**Why this gate exists**: PDL IP-enrich costs ~1 credit per IP.
Filtering to ~12-15% of total traffic at this stage saves ~85% of
Step 3 spend.

**Bypass**: `--all-ips` flag writes `ip_visits.json` straight to
`high_intent_ips.json` (passthrough) for battle-test sweeps.

### Step 3 — PDL IP Enrichment

**File**: `pdl_client.py`

**Reads**: `data/high_intent_ips.json`
**Writes**: `data/enriched_ips.json` and `data/pdl_cache.json`

**Endpoint**: `GET https://api.peopledatalabs.com/v5/ip/enrich?ip=<addr>`
with `X-Api-Key` header.

**Per-IP response shapes**:
- `200` → `data.company` (name, domain, industry, size, tags, employee
  count, confidence, location) and (optionally) `data.person` (role,
  sub_role, levels, confidence). Stored as `{status: "matched",
  company, person, ip_metadata}`.
- `400` "Cannot Enrich IP" → IP is a known hosting / proxy / Tor /
  mobile carrier. Stored as `{status: "ineligible", reason}`.
- `404` → no record. Stored as `{status: "no_match"}`.
- `402` → out of credits. Raises immediately (don't keep burning).
- `401`/`403` → bad key. Raises.
- `429` → tenacity retry; honors `Retry-After` header up to 75s.

**Cache**: `data/pdl_cache.json`, per-IP, with TTL by status:
- `matched` → 7 days (companies don't change overnight)
- `no_match` / `ineligible` → 1 day (PDL's graph might update sooner)
- `error` → not cached

**Cache migrations**: `load_cache` drops legacy entries when the result
shape evolves. Currently two migrations exist (matched-without-person,
matched-without-tags). Dropped entries get re-fetched on the next run.

**Throttle**: 0.12s minimum between live calls to stay under PDL's
per-second rate limit (~8 req/s).

### Step 4 — HubSpot company upsert

**File**: `hubspot_client.py`

**Reads**: `data/enriched_ips.json`, `data/credited_counts.json`,
`data/icp_fit_state.json`
**Writes**: `data/hubspot_writes.json`, updates to
`credited_counts.json` + `icp_fit_state.json`

**Aggregation** (`group_by_domain`): every matched IP collapses into
one bucket per `company.domain`. Bucket fields include
`current_visit_count` (sum of all IP visit_counts at this domain),
`delta_visit_count` (sum of per-IP deltas vs `credited_counts`),
`unique_pages`, `intent_reasons`, `last_seen`, `is_icp_fit`.

**Cumulative-count semantics**: `website_visit_count` must accumulate
across runs without double-counting. Implementation:
- `credited_counts.json` records what visit_count we last credited per
  IP.
- Per IP: `delta = max(0, current_visit_count - credited_count)`.
- For existing companies in HubSpot: read existing
  `website_visit_count` and add the bucket's total delta. Write the
  sum.
- After successful write, update `credited_counts` with the IP's
  current count.

This makes re-runs idempotent: `delta=0` means "everything we'd write
is already counted" → no double-count.

**ICP-fit handling** (`is_icp_fit`):
1. Compute `bucket["is_icp_fit"]` via `icp_filter.is_icp_fit(record)`
   — true iff industry ∈ `ICP_INDUSTRIES` AND no tag matches
   `ICP_EXCLUDE_TAG_PATTERNS`.
2. Read existing `is_icp_fit` from HubSpot.
3. Read `last_written` from `icp_fit_state.json`.
4. If `existing != last_written` (or `last_written` unset and
   `existing != computed`): treat as a **manual override**. Strip
   `is_icp_fit` from the PATCH payload. Don't overwrite sales' edit.
5. Otherwise: write the computed value, record it in
   `icp_fit_state.json`.

**Property auto-creation**: `ensure_icp_property` POSTs to
`/crm/v3/properties/companies` on first run to create
`is_icp_fit` as a boolean checkbox property if missing.
Idempotent — silently skips if already exists.

**Pre-flight**: `verify_target_properties` confirms all
`TARGET_PROPERTIES` exist on the company object. Raises with a clear
message listing missing properties if not.

**Properties written**:

| Property | Source |
| --- | --- |
| `website_visit_count` (number) | Cumulative (existing + delta) |
| `website_last_visited` (datetime) | Bucket's max `last_seen`, converted to ms-epoch |
| `website_pages_visited` (multi-line text) | `"\n".join(sorted(unique_pages))` |
| `website_visit_intent` (single-line text) | `", ".join(intent_reasons)` |
| `is_icp_fit` (boolean checkbox) | Two-stage ICP gate result (skipped if manual override detected) |

**Result row in `hubspot_writes.json`**:
```json
{
  "domain": "foo.com",
  "action": "updated|created|error",
  "company_id": "12345",
  "name": "Foo Inc",
  "is_icp_fit": true,
  "is_icp_fit_action": "updated|respected_override|created",
  "is_icp_fit_existing": "true|false|null",
  "previous_visit_count": 5,
  "delta_visit_count": 2,
  "new_visit_count": 7,
  "website_last_visited": "2026-06-01T15:00:00Z",
  "website_pages_visited": ["/sales-ai-workers/sdr-ai-worker"],
  "ip_count": 1
}
```

### Step 5 — Role qualifier

**File**: `role_filter.py`

**Reads**: `data/enriched_ips.json`
**Writes**: `data/qualified_visitors.json`

**Gate** (all conditions must hold):
1. `enrichment.status == "matched"`
2. `icp_filter.is_icp_fit(record) == true` (industry + tags)
3. `enrichment.person` is non-empty
4. `person.job_title_role` is populated (any value)
5. One of `QUALIFYING_ROLE_PATTERNS` (`sales`, `business_development`,
   `marketing`, `gtm`, `growth`, `revenue`) substring-matches either
   `job_title_role` or `job_title_sub_role`

**Why so strict** (Round 5): the earlier intent-only path qualified
visitors when PDL had ZERO inference about who was behind the IP. That
led to Person Search runs based purely on company-level intent ("a
visit happened from foo.com"), pulling 5–15 contacts per company on
weak signal. The current gate requires PDL person inference — the
pipeline now only de-anonymizes when there's actual person-level signal.

**Bypass**: `--all-ips` flag (`skip_intent_gate=True`) bypasses
conditions 3, 4, 5. ICP gate (1, 2) still applies.

**Each qualified record gets**:
- `role_qualifier_hits` — which patterns matched (e.g., `["sales"]`)
- `intent_qualifier_hits` — intent reasons (computed for context, not
  gating)
- `function_area` — `sales` / `marketing` / `gtm` (informational)
- `visitor_levels` — passthrough of `person.job_title_levels`

### Step 6 — PDL Person Search at qualifying companies

**File**: `person_lookup.py`

**Reads**: `data/qualified_visitors.json`
**Writes**: `data/people.json`, `data/pdl_person_cache.json`

**Endpoint**: `POST https://api.peopledatalabs.com/v5/person/search`
with `X-Api-Key` header. ES query DSL.

**Query** (single query per unique company domain):

```json
{
  "query": {
    "bool": {
      "must": [
        {"term": {"job_company_website": "<domain>"}},
        {"term": {"location_country": "united states"}},
        {"bool": {"should": [
          {"match_phrase": {"job_title": "chief revenue officer"}},
          {"match_phrase": {"job_title": "chief sales officer"}},
          {"match_phrase": {"job_title": "chief commercial officer"}},
          {"match_phrase": {"job_title": "head of sales"}},
          {"match_phrase": {"job_title": "head of revenue operations"}},
          {"match_phrase": {"job_title": "head of revops"}},
          {"match_phrase": {"job_title": "vp sales"}},
          {"match_phrase": {"job_title": "vp of sales"}},
          {"match_phrase": {"job_title": "vice president sales"}},
          {"match_phrase": {"job_title": "vice president of sales"}},
          {"match_phrase": {"job_title": "senior vice president of sales"}},
          {"match_phrase": {"job_title": "senior vice president sales"}},
          {"match_phrase": {"job_title": "svp sales"}},
          {"match_phrase": {"job_title": "svp of sales"}},
          {"bool": {"must": [
            {"terms": {"job_title_levels": ["cxo","vp","director"]}},
            {"bool": {"should": [
              {"match_phrase": {"job_title": "revenue operations"}},
              {"match_phrase": {"job_title": "revops"}}
            ]}}
          ]}}
        ]}}
      ],
      "must_not": [
        {"match_phrase": {"job_title": "regional"}},
        {"match_phrase": {"job_title": "area"}},
        {"match_phrase": {"job_title": "north america"}},
        {"match_phrase": {"job_title": "emea"}},
        {"match_phrase": {"job_title": "apac"}},
        {"match_phrase": {"job_title": "carrier"}},
        {"match_phrase": {"job_title": "partner sales"}},
        {"match_phrase": {"job_title": "channel"}},
        {"match_phrase": {"job_title": "strategic accounts"}},
        {"match_phrase": {"job_title": "enterprise architect"}},
        {"match_phrase": {"job_title": "communications"}},
        {"match_phrase": {"job_title": "product marketing"}},
        {"match_phrase": {"job_title": "field marketing"}},
        {"match_phrase": {"job_title": "small business"}},
        {"match_phrase": {"job_title": "smb"}},
        {"match_phrase": {"job_title": "consumer"}},
        {"match_phrase": {"job_title": "growth strategy"}},
        {"match_phrase": {"job_title": "pre-sales"}},
        {"match_phrase": {"job_title": "sales engineer"}},
        {"match_phrase": {"job_title": "sales enablement"}},
        {"match_phrase": {"job_title": "account executive"}},
        {"match_phrase": {"job_title": "account manager"}}
        // (full list in config.py — 35 patterns)
      ]
    }
  },
  "size": 20
}
```

**Why phrase-only inclusion + aggressive exclusion**: PDL's broad
`job_title_role: "sales"` taxonomy includes Carrier Sales, Strategic
Accounts, Regional VPs — none of whom are buying committee. Phrase
matches on specific top-of-org titles ("vp of sales", "chief revenue
officer", etc.) plus PDL-side `must_not` clauses get us 95%+ precision
without paying credits for excluded results (per-result billing).

**Cache**: `data/pdl_person_cache.json`, keyed by domain, 7-day TTL.

**Result shaping** (`_shape_person`): normalizes PDL's response into
our format — pulls `work_email` defensively (handles cases where PDL
returns a boolean placeholder instead of a string when the plan tier
doesn't grant email access).

### Step 7 — HubSpot contact upsert

**File**: `contact_upsert.py`

**Reads**: `data/people.json`, `data/hubspot_writes.json` (for
`domain → company_id` lookup)
**Writes**: `data/hubspot_contact_writes.json`

**Match strategy** (in order):
1. Primary key: `work_email`. Search HubSpot
   `/crm/v3/objects/contacts/search` by `email EQ <work_email>`.
2. Fallback: if no email but LinkedIn URL present AND the configured
   LinkedIn property (`HUBSPOT_LINKEDIN_PROPERTY`, default
   `hs_linkedin_url`) exists on the portal → search by that property.
   Catches cases where PDL's plan tier doesn't include email but does
   include LinkedIn.
3. If neither: skip with `reason: "no_email_no_linkedin"`.

**Pre-flight**:
- `verify_contact_access` confirms the HubSpot token has
  `crm.objects.contacts.read` + `.write` scopes.
- `linkedin_property_exists` checks the LinkedIn property exists on
  the portal — if not, falls back to email-only matching with a
  warning.

**Properties written**:

| Property | Source |
| --- | --- |
| `email` | PDL `work_email` |
| `firstname` | PDL `first_name` |
| `lastname` | PDL `last_name` |
| `jobtitle` | PDL `job_title` |
| `<HUBSPOT_LINKEDIN_PROPERTY>` | Canonicalized LinkedIn URL (when enabled and present) |

**Association**: after create/update, PUT
`/crm/v4/objects/contacts/<contact_id>/associations/default/companies/<company_id>`
links the contact to the matched company.

**Dedup**: across companies, by email-or-LinkedIn key, so the same
human under two domains writes once.

**LinkedIn URL canonicalization** (`canonicalize_linkedin_url`):
strips scheme, `www.`, query string, trailing slash, lowercases.
Ensures `https://www.linkedin.com/in/foo/?utm_source=x` and
`linkedin.com/in/foo` match the same HubSpot record.

---

## 6. Decision boundaries (where the rules live)

All filters live in `config.py` as named constants. Tuning the
pipeline = editing this file. No other code changes needed.

### 6.1 ICP industries (`ICP_INDUSTRIES`)

```python
ICP_INDUSTRIES = frozenset({
    "computer software",
    "information technology and services",
})
```

The narrowest interpretation of "B2B tech ICP". PDL's industry taxonomy
buckets many telecoms, MSPs, and ISPs under
`information technology and services` — those are filtered out via
tags in the next constant.

### 6.2 ICP exclude tag patterns (`ICP_EXCLUDE_TAG_PATTERNS`)

```python
ICP_EXCLUDE_TAG_PATTERNS = frozenset({
    "telecommunications", "voip", "voice and data", "isp",
    "internet service provider", "hosting", "managed services",
    "carrier", "telecom", "mobile network", "broadband",
})
```

Substring-matched against the joined lowercased tag list. If any
pattern hits, `is_icp_fit` flips to false even if the industry
matched. Catches PDL's broad-industry buckets that lump in telecoms
(e.g., CenturyLink is classified `information technology and services`
but tagged with `voip`, `hosting`, etc.).

### 6.3 Qualifying role patterns (`QUALIFYING_ROLE_PATTERNS`)

```python
QUALIFYING_ROLE_PATTERNS = (
    "sales", "business_development", "marketing",
    "gtm", "growth", "revenue",
)
```

Substring-matched against PDL's `person.job_title_role` or
`person.job_title_sub_role`. Wide net at the qualifier stage —
narrowing happens at Step 6's title search.

### 6.4 Target title clauses (`TARGET_TITLE_CLAUSES`)

The ES `should` block in Step 6. Currently 14 phrase clauses, all
sales-leadership (CRO, CSO, CCO, Head of Sales, VP Sales variants,
RevOps senior+).

**Notable**: marketing leadership is NOT in this list (per Round 5
user decision). Adding back CMO / VP Marketing / Head of Marketing
is a config-only change.

### 6.5 Title exclude patterns (`TITLE_EXCLUDE_PATTERNS`)

The ES `must_not` block in Step 6. ~35 substrings covering geo
(regional, north america, emea, apac, etc.), channel (carrier,
partner sales, strategic accounts), tangential roles (enterprise
architect, communications, product marketing), and IC roles
(account executive, representative).

### 6.6 High-intent paths (`HIGH_INTENT_PATHS`)

Site-specific. Determines what counts as a strong-intent page in
Step 2's filter (does NOT gate Step 5 anymore as of Round 5 — purely
informational and used for filtering at Step 2).

```python
HIGH_INTENT_PATHS = (
    "/pricing", "/demo", "/contact",
    "/sales-ai-workers/sdr-lead-response-worker",
    "/sales-ai-workers/pipeline-reporting-ai-worker",
    "/lets-talk", "/sales-ai-workers", ...
)
```

---

## 7. State files

All in `data/` (gitignored). The `pipeline-state` GitHub Actions
artifact preserves these across runs.

| File | Purpose | TTL |
| --- | --- | --- |
| `ip_visits.json` | Step 1B output: all IPs from KV at last run | overwritten each run |
| `high_intent_ips.json` | Step 2 output: filtered candidate pool | overwritten each run |
| `enriched_ips.json` | Step 3 output: per-IP PDL enrichment | overwritten each run |
| `qualified_visitors.json` | Step 5 output: visitors that passed the qualifier | overwritten each run |
| `people.json` | Step 6 output: Person Search results by domain | overwritten each run |
| `hubspot_writes.json` | Step 4 output: per-company write actions | overwritten each run |
| `hubspot_contact_writes.json` | Step 7 output: per-contact write actions | overwritten each run |
| `pdl_cache.json` | PDL IP-enrich response cache | Per-entry: 7d (matched) / 1d (no_match, ineligible). Schema migrations triggered on `load_cache`. |
| `pdl_person_cache.json` | PDL Person Search response cache | Per-entry: 7d |
| `credited_counts.json` | Per-IP `visit_count` we've already credited to HubSpot (so re-runs don't double-count) | sticky |
| `icp_fit_state.json` | Per-`company_id` `is_icp_fit` value the pipeline last wrote (so manual overrides in HubSpot are respected) | sticky |

---

## 8. Cost & rate-limiting

### 8.1 PDL

- **IP Enrich**: 1 credit per uncached IP per run.
- **Person Search**: 1 credit per result returned. Excluded titles
  (`must_not`) don't count.
- **Rate limit**: ~10 req/s sustained (~8 req/s soft cap in our
  client). Hits a per-minute window above that — we honor
  `Retry-After`.
- **Cache hit rate**: 87% sustained in CI after warm-up. Cold start
  (first artifact-less run) burns 1 credit per IP.

### 8.2 HubSpot

- **API rate limit**: 100 req/10s per Private App token (free /
  Starter tiers) or 150 (Pro / Enterprise). Step 4 + Step 7 stay well
  below: ~80-160 calls per run total.
- **Search consistency**: HubSpot search has minor eventual
  consistency. Not an issue at our scale.

### 8.3 Cloudflare

- **Worker requests**: free tier = 100k/day. We use ~3k/day from real
  traffic.
- **KV ops**: free tier = 1k writes + 100k reads per day. We use
  ~3k writes + ~10 reads per day.
- **All worker.cf.* signals**: free, no separate billing.

### 8.4 GitHub Actions

- **Minutes**: free tier private repo = 2000 min/month. We use ~360
  min/month (6 runs/day × ~2 min × 30 days).
- **Artifact storage**: free tier = 500 MB. We use ~150 MB
  (~5 MB per artifact, 30 in flight).

### 8.5 Slack

- **Webhook**: free. Rate limit ~1 msg/sec/webhook. We sleep 1s
  between chunks.

---

## 9. Failure modes & resilience

### 9.1 Idempotency

Every step is idempotent. Running the pipeline twice in a row produces
no duplicate writes:
- Step 3: cache hits → 0 PDL calls
- Step 4: delta-tracking → `delta=0` → no visit_count inflation
- Step 6: cache hits → 0 PDL calls
- Step 7: existing emails → `updated`, not `created` → no duplicate
  contacts
- notify.py: only alerts on `action=="created"`, so re-runs are silent

### 9.2 Manual override protection

If a human edits `is_icp_fit` (or any visit-property) in the HubSpot
UI after the pipeline wrote it, the pipeline must NOT undo that edit.

For `is_icp_fit` specifically, `hubspot_client._is_manual_override`
checks the existing HubSpot value against `icp_fit_state.json`. If
they differ, the field is stripped from the PATCH payload.
`is_icp_fit_action: "respected_override"` is logged + recorded in
`hubspot_writes.json`.

Other properties (`website_visit_count`, etc.) are managed
cumulatively by delta — manual edits get overwritten on the next run.
Treat them as pipeline-owned.

### 9.3 PDL plan / quota changes

- `402 out of credits` → pipeline aborts immediately with a
  `RuntimeError`. The cache is preserved (try/finally save).
- `401`/`403` → bad key, raises with a clear message about scopes.
- `429` → tenacity retry, honors `Retry-After` up to 75s.
- Plan changes that mask email fields (returning booleans instead of
  strings) → `_shape_person` defensively treats booleans as None;
  Step 7's LinkedIn fallback engages instead.

### 9.4 HubSpot property changes

- `verify_target_properties` fails fast if `is_icp_fit` or any
  `website_*` property is missing.
- `ensure_icp_property` auto-creates `is_icp_fit` on first run if
  missing.
- LinkedIn-property fallback `linkedin_property_exists` degrades to
  email-only if the custom property doesn't exist.

### 9.5 Cloudflare Worker changes

- Worker code lives in `cloudflaredeanonymizationsetup/`. Independent
  release cycle from the Python pipeline.
- `/export` schema is stable: paginated `{ ips, cursor, complete }`.
  Schema change requires updating `worker_client._get_page`.

### 9.6 GitHub Actions artifact loss

- Artifacts are 30-day retention. If the workflow goes offline for
  >30 days, caches are lost on resume. Pipeline still works — just
  burns more PDL credits on the rebuild run.
- Cold start (no artifact) is supported: `dawidd6/action-download-artifact@v3`
  is `continue-on-error: true`.

### 9.7 Slack webhook failure

- `notify.py.post_slack` returns False on >=400 response, logs the
  body, but doesn't raise. The pipeline run still succeeds.
- Slack message contents are also logged to GitHub Actions output —
  fallback discoverability if the webhook is broken.

---

## 10. Operations

### 10.1 Running locally

```bash
cd ~/Documents/sdraiworker/Website-Deanonymization-AI-Worker
source venv/bin/activate
python run_pipeline.py            # full pipeline
python run_pipeline.py --only 3   # just Step 3
python run_pipeline.py --dry-run  # no HubSpot writes
python run_pipeline.py --all-ips  # battle-test mode
```

Requires `.env` with `WORKER_BASE_URL`, `WORKER_ADMIN_TOKEN`,
`PDL_API_KEY`, `HUBSPOT_API_KEY`, optionally `HUBSPOT_LINKEDIN_PROPERTY`,
`SLACK_WEBHOOK_URL`, `HUBSPOT_PORTAL_ID`.

### 10.2 Triggering a CI run

```bash
gh workflow run run-pipeline.yml                    # normal mode
gh workflow run run-pipeline.yml -f all_ips=true    # battle-test mode
gh run watch                                        # stream logs
```

### 10.3 Monitoring

- **Workflow status**: GitHub Actions tab → run history.
- **Run logs**: each run uploads `pipeline-logs-<run_id>` artifact
  (7d retention).
- **Slack**: instant alerts on new buying-committee contacts; daily
  digest at 11 UTC.
- **HubSpot UI**: filter companies by `is_icp_fit=true` to see the
  qualifying pipeline output.

### 10.4 Extending the pipeline

Common changes and where to make them:

| Change | Edit |
| --- | --- |
| Add an ICP industry | `config.py` → `ICP_INDUSTRIES` |
| Exclude a new tag pattern | `config.py` → `ICP_EXCLUDE_TAG_PATTERNS` |
| Add a buying-committee title | `config.py` → `TARGET_TITLE_CLAUSES` |
| Exclude a sub-segment title | `config.py` → `TITLE_EXCLUDE_PATTERNS` |
| Change the role qualifier | `config.py` → `QUALIFYING_ROLE_PATTERNS` |
| Loosen / tighten Step 5 gate | `role_filter.py` → `classify()` |
| Change cron frequency | `.github/workflows/run-pipeline.yml` → `schedule.cron` |
| Add a new HubSpot property to companies | `config.py` → `TARGET_PROPERTIES`; `hubspot_client.build_properties` |
| Change Slack message format | `notify.py` → `alert_for_run` / `digest` |
| Add a new pipeline step | new `step_N.py` + register in `run_pipeline.STEPS` + lazy import in `main()` |

### 10.5 Resetting / recovering

- **Wipe local cache and re-fetch from artifact**:
  `rm -rf data && mkdir -p data && touch data/.gitkeep && gh run download <run-id> -n pipeline-state -D data`
- **Force re-enrichment of all IPs**: delete `data/pdl_cache.json`
  (will burn 1 credit per IP on the next Step 3).
- **Force re-search of all qualifying companies**: delete
  `data/pdl_person_cache.json` (will burn ~3-12 credits per company
  on the next Step 6).
- **Reset the manual-override state**: delete `data/icp_fit_state.json`
  (the next run uses the existing-matches-computed heuristic on first
  encounter).

---

## 11. Source-file map

```
project root/
├── ARCHITECTURE.md                 ← (this document)
├── README.md                       ← user-facing setup
├── config.py                       ← all tunables; central constants
├── icp_filter.py                   ← shared ICP-fit check
├── worker_client.py                ← Step 1B
├── filter_intent.py                ← Step 2
├── pdl_client.py                   ← Step 3 + cache
├── hubspot_client.py               ← Step 4 + 7 + contact + property helpers
├── role_filter.py                  ← Step 5
├── person_lookup.py                ← Step 6 + person cache
├── contact_upsert.py               ← Step 7
├── notify.py                       ← Slack alerts + digest
├── run_pipeline.py                 ← orchestrator (CLI entry)
├── requirements.txt
├── .env.example
├── data/                           ← gitignored; state
│   └── .gitkeep
├── logs/                           ← gitignored; timestamped run logs
│   └── .gitkeep
├── .github/workflows/
│   ├── run-pipeline.yml            ← every 4h schedule
│   └── daily-summary.yml           ← daily 11 UTC digest
└── cloudflaredeanonymizationsetup/
    ├── src/worker.js               ← Cloudflare Worker source
    ├── public/tracking.js          ← Browser tracking pixel
    ├── package.json
    ├── wrangler.toml
    └── README.md
```

---

## 12. Glossary

| Term | Meaning |
| --- | --- |
| ICP | Ideal Customer Profile — the company shape that's a legitimate buyer for our product |
| Buying committee | The set of people at a company involved in a B2B purchase decision (CRO, VP Sales, RevOps, etc.) |
| PDL | People Data Labs — third-party data API for IP-to-company and person enrichment |
| KV | Cloudflare Workers KV — distributed key-value store with eventual consistency |
| ASN | Autonomous System Number — identifies the network operator owning an IP range |
| Cron | Time-based job scheduler; we use it via GitHub Actions schedule |
| Webhook | A URL Slack provides that, when POSTed JSON, posts a message to a channel |
| `match_phrase` | ES query that matches a multi-word phrase as a contiguous token sequence |
| `must_not` | ES bool-query clause: documents matching this are excluded |
| Idempotent | Safe to run twice in a row — second run is a no-op |
| Delta tracking | Cumulative counter pattern: record what we've already credited per source, only add the delta on next run |

---

*Last updated: with Round 5 — person-confidence gate + sales-only
buying-committee tightening.*
