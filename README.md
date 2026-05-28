# Website Deanonymization Pipeline

Capture anonymous website visits, identify the companies behind them, and write
the result back to HubSpot.

Two halves:

1. **Capture layer** — a Cloudflare Worker (`cloudflaredeanonymizationsetup/`)
   that records IP + page-view events from a JS snippet on your site, stores
   them in Cloudflare KV, and exposes an authenticated `/export` endpoint.
2. **Python pipeline** (this directory) — pulls visits from `/export`, filters
   for high-intent IPs, enriches them via People Data Labs, and upserts the
   resolved companies into HubSpot.

For the Worker setup, see `cloudflare_deployment_walkthrough.md`.

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in WORKER_BASE_URL, WORKER_ADMIN_TOKEN, PDL_API_KEY, HUBSPOT_API_KEY
```

HubSpot Private App token scopes required:
- `crm.objects.companies.read`
- `crm.objects.companies.write`
- `crm.schemas.companies.read`

---

## Step 0 — Create the HubSpot company properties

The pipeline does a pre-flight check and **exits** if any of these are missing.
Create them in HubSpot (Settings → Properties → Company properties → Create
property):

| Internal name             | Field type            |
| ------------------------- | --------------------- |
| `website_visit_count`     | Single-line text      |
| `website_last_visited`    | Date and time picker  |
| `website_pages_visited`   | Multi-line text       |
| `website_visit_intent`    | Single-line text      |

> `website_visit_count` is written as a string (single-line text) so the field
> can also hold non-numeric annotations later if you want. If you'd rather make
> it a Number field in HubSpot, change `str(bucket["visit_count"])` in
> `hubspot_client.build_properties` accordingly.

---

## Run

```bash
# Full pipeline
python run_pipeline.py

# Don't actually write to HubSpot — preview what would happen
python run_pipeline.py --dry-run

# Process at most 10 IPs through PDL + HubSpot (Step 1B and 2 still process all)
python run_pipeline.py --limit 10

# Only refresh the local export — skip enrichment + HubSpot
python run_pipeline.py --only 1b,2

# Resume from PDL onward (re-using the high_intent file)
python run_pipeline.py --skip 1b,2
```

Each run writes a timestamped DEBUG log into `logs/`. Console output is INFO.

---

## What each step does

| Step | File              | Reads                  | Writes                       |
| ---- | ----------------- | ---------------------- | ---------------------------- |
| 1B   | `worker_client.py` | Worker `/export`       | `data/ip_visits.json`        |
| 2    | `filter_intent.py` | `ip_visits.json`       | `data/high_intent_ips.json`  |
| 3    | `pdl_client.py`    | `high_intent_ips.json` | `data/enriched_ips.json`     |
| 4    | `hubspot_client.py`| `enriched_ips.json`    | `data/hubspot_writes.json` + HubSpot |

### Step 2 filter

Keeps an IP if **either**:
- `visit_count >= MIN_VISITS_FOR_INTENT` (default 2), or
- One of its visited paths starts with any entry in `HIGH_INTENT_PATHS`
  (default: `/pricing`, `/demo`, `/contact`, etc.).

Tune both in `config.py`.

### Step 3 enrichment

Calls PDL's IP Enrichment API (`/v5/ip`). Skips IPs where PDL doesn't return a
domain (residential / unresolvable). Aborts the run on `402 out of credits` so
quota isn't burned without anyone noticing.

### Step 4 HubSpot upsert

Aggregates all IPs resolving to the same domain into a single company write
(summing `visit_count`, merging unique pages and intent reasons, taking the
latest `last_seen`). For each domain, searches HubSpot by `domain`; updates if
found, creates a new company if not.

---

## Layout

```
.
├── cloudflaredeanonymizationsetup/   # Cloudflare Worker (capture layer)
├── data/                              # generated JSON, gitignored
├── logs/                              # timestamped run logs
├── .github/workflows/                 # scheduled GitHub Actions
├── config.py
├── worker_client.py       # Step 1B
├── filter_intent.py       # Step 2
├── pdl_client.py          # Step 3
├── hubspot_client.py      # Step 4 — companies
├── role_filter.py         # Step 5 — visitor qualifier
├── person_lookup.py       # Step 6 — Person Search
├── contact_upsert.py      # Step 7 — contacts
├── icp_filter.py          # shared ICP gate
├── notify.py              # Slack alerts (instant + digest)
├── run_pipeline.py        # orchestrator
├── requirements.txt
├── .env.example
└── README.md
```

---

## Automation: scheduled runs + Slack alerts

The pipeline runs itself every 4 hours via GitHub Actions, with state
persisting between runs through artifacts. New ICP buying-committee contacts
trigger an instant Slack post; a daily digest fires at 11 UTC.

### One-time setup

1. **Install GitHub CLI** (if missing):
   ```bash
   brew install gh           # macOS
   ```

2. **Authenticate**:
   ```bash
   gh auth login             # pick GitHub.com, HTTPS, login via browser
   ```

3. **Create a Slack incoming webhook**:
   - Visit https://api.slack.com/apps → "Create New App" → "From scratch"
   - Pick a workspace; name it something like "Website Deanonymization"
   - In the app's sidebar: "Incoming Webhooks" → toggle on → "Add New Webhook to Workspace"
   - Pick the target channel; copy the resulting `https://hooks.slack.com/services/...` URL

4. **Create the private repo and push the code**:
   ```bash
   cd ~/Documents/sdraiworker/Website-Deanonymization-AI-Worker
   git add -A
   git commit -m "initial commit"
   gh repo create website-deanonymization --private --source=. --remote=origin --push
   ```

5. **Set repo secrets** (six values, paste when prompted):
   ```bash
   gh secret set WORKER_BASE_URL              # https://website-deanon.<sub>.workers.dev
   gh secret set WORKER_ADMIN_TOKEN           # from .env
   gh secret set PDL_API_KEY                  # from .env
   gh secret set HUBSPOT_API_KEY              # from .env
   gh secret set HUBSPOT_LINKEDIN_PROPERTY    # hs_linkedin_url
   gh secret set SLACK_WEBHOOK_URL            # the URL from step 3
   gh secret set HUBSPOT_PORTAL_ID            # optional — enables deep-links in Slack
   ```

6. **Trigger a test run** to confirm everything wires up:
   ```bash
   gh workflow run run-pipeline.yml
   gh run watch                              # streams the logs
   ```

### Workflows

| File | Trigger | Purpose |
| --- | --- | --- |
| `.github/workflows/run-pipeline.yml` | `cron: 0 */4 * * *` + manual | Pulls /export, runs Steps 1B-7, posts instant Slack on new ICP+buying-committee contacts |
| `.github/workflows/daily-summary.yml` | `cron: 0 11 * * *` (7am EDT) + manual | Posts daily digest to Slack (companies/contacts/top ICP visitors) |

State (`data/*.json`) is uploaded after each run as the `pipeline-state`
artifact (retention 30d) and downloaded by the next run. Logs upload as
`pipeline-logs-<run_id>` (retention 7d) for debugging.

### Slack message format

**Instant alert** (when a new ICP buying-committee contact is created):

```
:dart: 1 new buying-committee contact identified
*Lindsay Warren* — VP, Growth
at *People Data Labs* (peopledatalabs.com)
`lindsay.warren@peopledatalabs.com`
<LinkedIn> <Open in HubSpot →>
```

**Daily digest**:

```
Daily pipeline summary
Companies: 2 created · 5 updated · 1 ICP-fit
Contacts:  3 created · 1 updated
Top ICP visitors (by visit count):
  • People Data Labs — 11-50 · 4 visits
```

If `SLACK_WEBHOOK_URL` is unset, `notify.py` silently no-ops — useful when
running locally without wanting to spam the channel.
