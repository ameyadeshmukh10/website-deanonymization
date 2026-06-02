---
name: audit-config
description: Read the current customization state across all knobs and produce a customer-readable summary report. Use to audit a deployment, confirm what's configured, generate handover documentation, or check the state of a customer's setup. Read-only — makes no changes.
---

# Audit the current pipeline configuration

## What this does

Reads every customer-customizable surface and produces a one-page summary report. Does NOT change anything.

Useful for:
- Confirming a freshly-customized deployment is fully wired up
- Handover documentation when a different SE takes over an account
- Sanity-checking after a customer-driven config change
- Pre-flight before a `customize-for-customer` wizard re-run

See `ARCHITECTURE.md` §6, §7, and §10.

## What to inspect

Gather data from each source, then present in a structured table:

### 1. Cloudflare Worker

- From `cloudflaredeanonymizationsetup/wrangler.toml`:
  - `name` (worker name)
  - `vars.ALLOWED_ORIGIN`
  - `kv_namespaces[0].id`
- From `.env`: `WORKER_BASE_URL`
- Live test:
  ```bash
  curl -s -w 'HTTP %{http_code}\n' <WORKER_BASE_URL>/health
  ```
  Should return `{"ok":true,...}` with HTTP 200.

### 2. GitHub Actions

```bash
gh repo view --json owner,name,visibility,url
gh secret list
gh workflow view run-pipeline.yml | head -15
```

Report: repo URL, visibility, list of secret names + timestamps, current cron schedule.

### 3. HubSpot

```bash
# Portal id from .env or secret:
gh secret list | grep HUBSPOT_PORTAL_ID

# Region from API key prefix:
grep HUBSPOT_API_KEY .env  # show prefix only, redact rest
```

Live test:
```bash
source venv/bin/activate
python -c "
import config, requests
S = requests.Session()
S.headers.update({'Authorization': f'Bearer {config.HUBSPOT_API_KEY}'})
r = S.get(config.HUBSPOT_COMPANY_PROPERTIES, timeout=15)
existing = {p['name'] for p in r.json().get('results', [])}
required = set(config.TARGET_PROPERTIES)
missing = required - existing
print(f'Region: {config.HUBSPOT_UI_BASE}')
print(f'Portal ID: {config.HUBSPOT_PORTAL_ID}')
print(f'Required properties: {sorted(required)}')
print(f'Missing: {sorted(missing) if missing else \"none\"}')
"
```

### 4. Slack

From `.env` or GH secret: `SLACK_WEBHOOK_URL` (redact all but last 6 chars).

Live test — send a digest:
```bash
python notify.py --mode digest
```

Confirm with the user whether the message arrived in the channel.

### 5. Config knobs (from `config.py`)

```bash
python -c "
import config
print('ICP_INDUSTRIES        :', sorted(config.ICP_INDUSTRIES))
print('ICP_EXCLUDE_TAG_PATTERNS:', sorted(config.ICP_EXCLUDE_TAG_PATTERNS))
print('QUALIFYING_ROLE_PATTERNS:', config.QUALIFYING_ROLE_PATTERNS)
print('TARGET_TITLE_CLAUSES  :', len(config.TARGET_TITLE_CLAUSES), 'clauses')
print('TITLE_EXCLUDE_PATTERNS:', len(config.TITLE_EXCLUDE_PATTERNS), 'patterns')
print('HIGH_INTENT_PATHS     :', config.HIGH_INTENT_PATHS)
print('MIN_VISITS_FOR_INTENT :', config.MIN_VISITS_FOR_INTENT)
"
```

### 6. Recent activity (from data/ if available)

```bash
python -c "
import json, os
files = [
    ('ip_visits.json', 'Total IPs in KV at last pull'),
    ('high_intent_ips.json', 'High-intent IPs after Step 2'),
    ('enriched_ips.json', 'IPs enriched via PDL'),
    ('hubspot_writes.json', 'Companies written/updated'),
    ('hubspot_contact_writes.json', 'Contacts written/updated'),
]
for fname, label in files:
    path = f'data/{fname}'
    if os.path.exists(path):
        d = json.load(open(path))
        n = len(d) if isinstance(d, dict) else len(d) if isinstance(d, list) else 0
        print(f'  {label}: {n}')
    else:
        print(f'  {label}: <no data yet>')
"
```

## Output format

Present everything as a single readable report:

```
=== <Customer Name> — Pipeline Configuration Audit ===

Cloudflare Worker
  URL:               https://website-deanon.<sub>.workers.dev
  Allowed origin:    https://<customer>.com
  KV namespace id:   <id>
  Health:            ✓ HTTP 200

GitHub repo
  URL:               https://github.com/<owner>/<name>
  Visibility:        private
  Secrets configured: 7 (all required + HUBSPOT_PORTAL_ID)
  Schedule:          every 4 hours (cron: 0 */4 * * *)

HubSpot
  Region:            EU (app-eu1.hubspot.com)
  Portal ID:         144358290
  Properties:        ✓ all 5 required exist
  LinkedIn property: hs_linkedin_url

Slack
  Webhook:           configured (...XyZAbC)
  Test message:      ✓ delivered

ICP gates
  Industries:        computer software, information technology and services
  Exclude tags:      11 patterns (telecoms, VoIP, hosting, etc.)
  Role qualifiers:   sales, business_development, marketing, gtm, growth, revenue

Buying committee
  Target titles:     14 clauses (sales-leadership only, sales+RevOps)
  Title excludes:    35 sub-segment patterns

Site
  High-intent paths: 9 paths configured
  Min visits gate:   2

Recent activity
  IPs in KV:         1507
  High-intent:       183
  PDL matched:       81 companies
  HubSpot companies: 77 (ICP-fit: 6)
  HubSpot contacts:  65
```

## Caveats

- Don't print secret VALUES — only counts, last-4-chars, or "configured/not configured".
- If `data/*.json` files don't exist, just report "no data yet" — that means the pipeline hasn't run yet.
- The HubSpot live test may take a few seconds — that's fine.
