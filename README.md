# Website De-anonymization Pipeline

**Turn anonymous website traffic into named buying-committee contacts in your CRM — automatically, on a schedule, with a Slack ping the moment a real buyer shows up.**

This is an end-to-end GTM engineering system I built to solve one of the hardest problems in outbound: *most of the people evaluating you never fill out a form.* It captures anonymous visits at the edge, resolves the IP to a company **and** a person, gates everything against a strict B2B-SaaS ICP, finds the actual sales-leadership buying committee at qualifying accounts, and writes it all back to HubSpot with an instant Slack alert — untouched by human hands.

It's not a script. It's a full pipeline: an edge capture layer, a 7-stage enrichment engine, a CRM sync with real data-hygiene guarantees, a cost-optimized integration against a per-credit data API, self-hosted automation, and a conversational onboarding layer that lets a solutions engineer stand up a brand-new customer in ~15 minutes.

---

## What a single buyer's journey looks like

```
Anonymous visitor hits everworker.ai/pricing
        │  (privacy-respecting JS pixel — honors Do Not Track / GPC)
        ▼
Cloudflare Worker at the edge
        │  drops bots, datacenter IPs, VPNs, high-threat IPs in real time
        │  stores the visit per-IP in Cloudflare KV
        ▼
Python pipeline (runs every 4h in GitHub Actions)
        │  1. pull new visits        4. upsert company → HubSpot
        │  2. filter to high-intent   5. qualify by inferred role + ICP
        │  3. resolve IP → company    6. find the buying committee (PDL search)
        │     + person (PDL)          7. upsert contacts → HubSpot + associate
        ▼
HubSpot company + contacts, fully enriched
        │
        ▼
📌  Slack:  "1 new buying-committee contact — Sue Quense, CRO at Foo Inc"
            with email, LinkedIn, pages viewed, and deep-links into HubSpot
```

**Visit-to-Slack latency: ~4 hours. Cost: pennies. Human effort: zero.**

---

## Capabilities

### 🛰️ Real-time edge capture (Cloudflare Worker + JS pixel)
- **Sub-request bot & junk-traffic filtering** using signals Cloudflare gives for free — before anything ever hits storage or burns an enrichment credit:
  - Datacenter / cloud / VPN ASNs (AWS, GCP, Azure, OVH, Hetzner, DigitalOcean, NordVPN, ExpressVPN, Tor exit nodes, and ~40 more) dropped by AS-org pattern
  - Cloudflare threat-score gating
  - Cloudflare verified-bot detection (catches spoofed-UA bots)
  - A 35+ pattern user-agent bot regex (search crawlers, AI training bots, uptime monitors, headless browsers, raw HTTP libraries)
  - Private / reserved / link-local IP rejection
- **Per-IP visit accumulation in Cloudflare KV** — visit counts, unique pages, sessions, visitor IDs, user agents, all capped to keep values lean
- **Authenticated, cursor-paginated `/export` endpoint** for the pipeline to pull from (bearer-token gated; parallel-fanned KV reads collapse a 1000-key page from ~30s to ~1-2s)
- **Optional live mirror to HubSpot Custom Behavioral Events** — every pageview also lands as a native HubSpot event, fire-and-forget, when the portal tier supports it
- **Privacy-first tracking pixel** — honors `Do Not Track` and Global Privacy Control, aligns to HubSpot's `hubspotutk` cookie when present, never breaks the host page

### 🧬 Identity resolution & enrichment (People Data Labs)
- **IP → company resolution** with industry, size, tags, employee count, and location
- **IP → person inference** — role, sub-role, and seniority level of who's actually behind the visit
- **Buying-committee discovery** via PDL Person Search: a hand-tuned Elasticsearch query that finds top-of-org sales leadership (CRO, CSO, CCO, VP/SVP Sales, Head of Sales, senior RevOps) at qualifying accounts
- **95%+ targeting precision** from phrase-only inclusion clauses + 35 `must_not` exclusion patterns that strip regional VPs, channel/carrier sales, sales engineers, ICs, and tangential titles — all filtered **PDL-side so excluded results never cost a credit**

### 🎯 ICP & buying-committee modeling
- **Two-stage ICP gate**: a strict industry allow-list (`computer software`, `IT & services`) *then* a tag-based exclusion pass that kicks out telecoms, ISPs, VoIP, MSPs, and hosting providers that PDL lumps into the same broad industry bucket
- **Person-confidence qualifier**: only de-anonymizes when PDL returns an actual person-level signal whose role matches the GTM-relevant set (sales, revenue, growth, GTM, BD, marketing) — no spraying Person Search on company-level noise
- **Every knob is a named constant in one config file** — ICP industries, exclude tags, role qualifiers, target titles, title excludes, intent paths, schedule — tunable without touching pipeline logic

### 🗄️ CRM sync with real data-hygiene guarantees (HubSpot)
- **Fully idempotent** — run it twice, get zero duplicate companies, contacts, or inflated counts
- **Cumulative visit counting via per-IP delta-tracking** — `website_visit_count` accumulates correctly across runs even if the edge KV is purged, without ever double-counting
- **Manual-override protection** — if a human flips `is_icp_fit` in the HubSpot UI, the pipeline detects the divergence and *never* stomps their edit
- **Auto-provisioning & pre-flight** — creates the `is_icp_fit` property on first run, fails fast with a clear message if required properties are missing
- **Company aggregation** — every IP resolving to the same domain collapses into one clean company write (summed visits, merged pages, latest timestamp)
- **Contact matching with graceful fallback** — primary match on work email, automatic fallback to a LinkedIn-URL property when the PDL plan tier masks email, LinkedIn URL canonicalization so the same person never lands twice
- **Contact → company association** and cross-domain dedup baked in
- **Multi-region aware** — auto-detects HubSpot data residency (`na1`/`eu1`/`au1`/`ca1`) so Slack deep-links land on the right portal host

### 💸 Cost engineering (this is the part I'm proudest of)
Running paid data APIs at scale is where naive pipelines hemorrhage money. This one doesn't:
- **~85% PDL spend reduction** by filtering to high-intent IPs (~12-15% of traffic) *before* enrichment
- **Two-tier response cache** with status-aware TTLs (matched companies cached 7d, negatives 1d), sustaining an **~87% cache hit rate** in production
- **Per-result billing awareness** — exclusion filters run PDL-side, so the ~35 title patterns we *don't* want never cost a credit
- **Credit-exhaustion circuit breaker** — a `402 out of credits` aborts the run immediately instead of silently burning quota, and the cache is always persisted on the way out
- **Rate-limit discipline** — client-side throttling, `Retry-After` honoring, exponential backoff on 429/5xx across PDL and HubSpot
- **Runs entirely on free tiers** — Cloudflare Workers/KV, GitHub Actions minutes, and Slack webhooks all stay well within free limits

### ⚙️ Automation & operations
- **Self-hosted on GitHub Actions** — full pipeline every 4 hours, daily digest at 11 UTC, both manually dispatchable
- **State persistence across ephemeral CI runs** — caches and delta-tracking state survive between runs via workflow artifacts, keeping the git repo clean *and* the caches warm
- **Battle-test mode** (`--all-ips`) to sweep the entire IP pool past the intent + role gates for coverage analysis
- **Granular CLI** — `--only` / `--skip` specific steps, `--dry-run`, `--limit`, timestamped debug logs per run

### 🔔 Sales-ready Slack alerting
- **Instant per-run alerts** the moment a new buying-committee contact is created — name, title, company, email, LinkedIn, pages viewed, "last visited N hours ago," and one-click deep-links to both the contact and company records in HubSpot
- **Daily digest** — companies created/updated, contacts created, ICP-fit breakdown, top ICP visitors by volume
- **Chunked, rate-limit-safe delivery** (Slack's 50-block limit respected, 1s between chunks) with a plaintext fallback logged to CI if the webhook ever fails

### 🧑‍🔧 Solutions-engineering enablement layer
This is where the pipeline becomes a *product*, not just my personal tool. It ships with **14 Claude Code skills** so any SE can stand up a new customer conversationally — no code diving:

```
customize this pipeline for Acme Corp        →  full 10-15 min onboarding wizard
add manufacturing to the ICP industries       →  edits + validates config
change the schedule to run every 2 hours       →  edits the cron
update the buying committee to include CMOs     →  edits the title clauses
audit the current configuration for Acme        →  generates a handover report
verify the pipeline is healthy                  →  end-to-end smoke test
```

The skills cover the full lifecycle — Cloudflare Worker deploy, HubSpot connection, Slack webhook, GitHub Actions bootstrap, and every individual ICP / buying-committee / role / schedule / intent-path knob — each one focused, reusable, and auto-discovered when you open the repo in Claude Code. From fresh clone to first Slack alert in ~15 minutes.

---

## Tech stack

| Layer | Tech |
| --- | --- |
| Edge capture | Cloudflare Workers (V8), Cloudflare KV, Wrangler, vanilla JS pixel |
| Enrichment | People Data Labs (IP Enrich + Person Search / Elasticsearch DSL) |
| CRM | HubSpot CRM API v3/v4 (companies, contacts, associations, custom properties, behavioral events) |
| Pipeline | Python 3.12 · `requests` · `tenacity` (retry/backoff) · `python-dotenv` |
| Automation | GitHub Actions (scheduled + dispatch, artifact-based state) |
| Alerting | Slack incoming webhooks (Block Kit) |
| Enablement | 14 Claude Code skills (conversational SE onboarding) |

---

## The pipeline, step by step

| Step | Module | Reads | Writes |
| ---- | ------ | ----- | ------ |
| 1B | `worker_client.py` | Worker `/export` | `data/ip_visits.json` |
| 2 | `filter_intent.py` | `ip_visits.json` | `data/high_intent_ips.json` |
| 3 | `pdl_client.py` | `high_intent_ips.json` | `data/enriched_ips.json` (+ IP cache) |
| 4 | `hubspot_client.py` | `enriched_ips.json` | HubSpot companies + `data/hubspot_writes.json` |
| 5 | `role_filter.py` | `enriched_ips.json` | `data/qualified_visitors.json` |
| 6 | `person_lookup.py` | `qualified_visitors.json` | `data/people.json` (+ person cache) |
| 7 | `contact_upsert.py` | `people.json` | HubSpot contacts + `data/hubspot_contact_writes.json` |
| — | `notify.py` | pipeline output | Slack (instant alerts + daily digest) |

**For the full technical deep-dive — every filter, cache TTL, failure mode, and design decision — see [`ARCHITECTURE.md`](ARCHITECTURE.md).** For the edge layer, see [`cloudflare_deployment_walkthrough.md`](cloudflare_deployment_walkthrough.md).

---

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in WORKER_BASE_URL, WORKER_ADMIN_TOKEN, PDL_API_KEY, HUBSPOT_API_KEY
```

Required HubSpot Private App scopes: `crm.objects.companies.read/write`,
`crm.schemas.companies.read`, `crm.objects.contacts.read/write`.

Create these HubSpot company properties before the first run (the pipeline
pre-flights and exits if any are missing; `is_icp_fit` auto-creates itself):

| Internal name | Field type |
| --- | --- |
| `website_visit_count` | Number (written as text — see [`ARCHITECTURE.md`](ARCHITECTURE.md)) |
| `website_last_visited` | Date and time picker |
| `website_pages_visited` | Multi-line text |
| `website_visit_intent` | Single-line text |

### Run it

```bash
python run_pipeline.py               # full pipeline
python run_pipeline.py --dry-run     # preview HubSpot writes without making them
python run_pipeline.py --limit 10    # cap enrichment + CRM work
python run_pipeline.py --only 1b,2   # just refresh the local export
python run_pipeline.py --skip 1b,2   # resume from enrichment onward
python run_pipeline.py --all-ips     # battle-test: bypass intent + role gates
```

Each run writes a timestamped DEBUG log to `logs/`.

### Onboard a new customer (the fast path)

Open the repo in Claude Code and just say:

```
customize this pipeline for <customer name>
```

Claude runs a 10-15 minute wizard covering the Cloudflare Worker, HubSpot,
Slack, GitHub Actions, ICP, buying-committee titles, exclusions, and schedule,
then verifies the whole thing end-to-end. See `.claude/skills/` for the full
skill catalog.

---

## Automation setup (GitHub Actions + Slack)

The pipeline runs itself every 4 hours via GitHub Actions with state persisting
between runs through artifacts. New buying-committee contacts trigger an instant
Slack post; a daily digest fires at 11 UTC.

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `run-pipeline.yml` | `cron: 0 */4 * * *` + manual | Pull `/export`, run Steps 1B–7, post instant Slack on new ICP buying-committee contacts |
| `daily-summary.yml` | `cron: 0 11 * * *` + manual | Post the daily digest to Slack |

Set six repo secrets — `WORKER_BASE_URL`, `WORKER_ADMIN_TOKEN`, `PDL_API_KEY`,
`HUBSPOT_API_KEY`, `HUBSPOT_LINKEDIN_PROPERTY`, `SLACK_WEBHOOK_URL` (plus
optional `HUBSPOT_PORTAL_ID` for Slack deep-links) — and trigger the first run.
Full walkthrough (including the `setup-*` skills that do this for you) in
[`ARCHITECTURE.md`](ARCHITECTURE.md) §3.3 and §10.2.

---

## Repository layout

```
.
├── cloudflaredeanonymizationsetup/   # Cloudflare Worker + tracking pixel (capture layer)
├── .claude/skills/                    # 14 SE-enablement skills (conversational onboarding)
├── .github/workflows/                 # scheduled GitHub Actions
├── config.py                          # every tunable, in one place
├── worker_client.py                   # Step 1B — pull /export
├── filter_intent.py                   # Step 2 — high-intent gate
├── pdl_client.py                      # Step 3 — IP enrichment + cache
├── hubspot_client.py                  # Step 4 — company upsert
├── role_filter.py                     # Step 5 — ICP + role qualifier
├── person_lookup.py                   # Step 6 — buying-committee search
├── contact_upsert.py                  # Step 7 — contact upsert + association
├── icp_filter.py                      # shared ICP gate
├── notify.py                          # Slack alerts + digest
├── run_pipeline.py                    # orchestrator (CLI)
├── ARCHITECTURE.md                    # full system reference
└── README.md
```

---

*Built by [Ameya Deshmukh](mailto:ameyadeshmukh10@gmail.com) — GTM Engineering: edge infrastructure, data enrichment, CRM automation, and the solutions-engineering enablement to ship it to real customers.*
