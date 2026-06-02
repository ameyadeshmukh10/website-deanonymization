---
name: customize-for-customer
description: Walk a solutions engineer through customizing the entire pipeline for a new customer end-to-end. Use when onboarding a new customer, setting up the pipeline for someone else, customizing the deployment for a specific company's ICP/sales motion/CRM, or configuring everything from scratch. Coordinates operational setup (Cloudflare Worker, GitHub, Slack, HubSpot) AND code configuration (ICP, buying committee, roles, schedule). Runs deep verification at the end.
---

# Customize for a new customer (full lifecycle wizard)

## What this does

End-to-end onboarding wizard. Walks the SE through every dimension of customer customization in a single conversation. Delegates each step to focused skills (`setup-*`, `update-*`, `verify-pipeline`, `audit-config`) so logic is reused, not duplicated.

Designed for 10-15 minutes from "fresh clone" to "first pipeline run posting Slack alerts to the right channel".

## Prerequisites the SE should have ready

Confirm at the start of the conversation:

- Customer name (used in summary)
- Customer's production domain (e.g., `https://acme.com`)
- The customer has a Cloudflare account, HubSpot account, Slack workspace, and GitHub account
- The SE has `gh`, `wrangler`, `python` installed locally
- All access tokens / API keys ready (PDL, HubSpot, etc.) OR the SE will generate them during the wizard

If anything is missing, pause and tell the SE what to gather before re-invoking.

## Flow

### Phase 0 — Greeting and context

1. Ask the customer's name (will go in the summary report).
2. Ask: is this a **new deployment** (no Cloudflare Worker / GitHub repo yet) or an **update to an existing one** (just want to tune config)?

Branch:
- **New deployment** → run Phase 1 (operational setup) then Phase 2 (config tuning) then Phase 3 (verification).
- **Update existing** → skip Phase 1, go straight to Phase 2 then Phase 3.

### Phase 1 — Operational setup (new deployments only)

Walk through each in order. If the SE says "I'll do this later", note it and continue.

1. **Cloudflare Worker** — invoke `setup-cloudflare-worker` skill.
2. **HubSpot** — invoke `setup-hubspot` skill.
3. **Slack** — invoke `setup-slack-webhook` skill.
4. **GitHub** — invoke `setup-github-actions` skill (must come last; needs the secrets from the previous three).

After Phase 1, the customer has:
- A live Worker collecting visits
- A HubSpot account with the right properties
- A Slack channel ready for alerts
- A GitHub repo with all secrets configured

### Phase 2 — Code config tuning

Ask the SE to describe the customer's sales motion in 1-2 sentences. Use that to suggest defaults for each config skill. Walk through each:

1. **ICP industries** — invoke `update-icp-industries`. Ask the SE what industries the customer sells to. Propose the strict default (`computer software` + `information technology and services`) or expand based on their answer.

2. **ICP exclude tags** — invoke `update-icp-tags`. Default works for most B2B SaaS customers. Skip if the SE has no specific exclusions in mind.

3. **Buying committee titles** — invoke `update-buying-committee`. Ask about target buyer roles. Default is sales-leadership-only (Round 5). Add marketing back if customer's product targets marketing buyers.

4. **Title excludes** — invoke `update-title-excludes`. Default is aggressive (regional/sub-segment/channel filtering). Customize if customer DOES sell to regional VPs.

5. **Role qualifiers** — invoke `update-role-qualifiers`. Default includes sales/marketing/business_development/gtm/growth/revenue. Tighten or expand based on customer's typical buyer persona.

6. **High-intent paths** — invoke `update-high-intent-paths`. Ask for the customer's pricing/demo/contact page URLs. Critical — these are SITE-SPECIFIC defaults.

7. **Schedule** — invoke `update-schedule`. Default is every 4h. Adjust if customer wants more/less frequent.

After Phase 2, all `config.py` constants reflect the customer's preferences.

### Phase 3 — Verification

1. Invoke `verify-pipeline` skill. Walk through each check. If any fail, fix in place.
2. Invoke `audit-config` skill. Capture the output for the summary.

### Phase 4 — Customer summary report

Generate a clean handover document the SE can share. Include:

- Customer name + domain
- Cloudflare Worker URL
- GitHub repo URL
- HubSpot portal ID + region
- Slack channel
- Schedule cadence
- Key config choices (ICP industries, key pages, buying-committee scope)
- First-run status from Phase 3 verification
- Next-step guidance:
  - When the next cron fires
  - Where Slack alerts will land
  - How to flip a company's `is_icp_fit` manually in HubSpot (manual overrides are respected — Round 4)
  - How to invoke focused `update-*` skills later for tuning

## Output format for summary

```
=== Customer Onboarding Complete: <Customer Name> ===

Deployment
  Cloudflare Worker:   https://website-deanon.<sub>.workers.dev
  GitHub repo:         https://github.com/<owner>/<name>
  HubSpot portal:      144358290 (EU region)
  Slack channel:       #<channel>
  Cron schedule:       every 4 hours

Customer-specific config
  Industries:          {software, IT services} (strict B2B SaaS)
  Buying committee:    Sales leadership only (CRO/VP Sales/Head of Sales/RevOps)
  Key pages:           [/pricing, /demo, /book-a-call, ...]
  Role qualifiers:     sales, business_development, marketing, gtm, growth, revenue

Status
  First pipeline run:  ✓ green (1m 03s)
  Slack alert path:    ✓ tested digest delivery
  HubSpot properties:  ✓ all 5 present

What happens next
  - First scheduled run: in 3 hours 47 minutes (at <UTC time>)
  - Daily summary:       posted at 11 UTC (7am EDT / 6am EST)
  - First alerts:        when a PDL-person-confident visitor lands at an
                         ICP company and a buying-committee contact gets
                         created

For ongoing tuning
  - Adjust target titles:    invoke `update-buying-committee` skill
  - Adjust ICP industries:   invoke `update-icp-industries` skill
  - Change cron frequency:   invoke `update-schedule` skill
  - Audit current state:     invoke `audit-config` skill
```

## Caveats

- Phase 1 is the longest — accept that 10-15 minutes is realistic for a first-time customer setup.
- If the SE is iterating on customer config (Phase 2 only, not Phase 1), skip the operational setup entirely.
- If verification fails in Phase 3, fix in place — don't push to GitHub or trust an unhealthy deployment.
- The summary report is the SE's handover document — make it concrete (URLs, specific timestamps, exact channel names).
- Each focused skill has its own caveats — don't duplicate them here; let each skill handle its own warnings.
