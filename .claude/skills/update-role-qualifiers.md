---
name: update-role-qualifiers
description: Update which visitor roles qualify for Step 5 de-anonymization (the `QUALIFYING_ROLE_PATTERNS` tuple in config.py). Use when changing which inferred-visitor roles trigger Person Search at their company. Affects who counts as a "buying-relevant" visitor when PDL's IP enrichment returns a person inference. Edits config.py.
---

# Update Step 5 role qualifiers

## What this controls

When PDL IP Enrichment returns a `person` field (with role, sub_role, levels), Step 5 checks whether `person.job_title_role` or `person.job_title_sub_role` contains any of `QUALIFYING_ROLE_PATTERNS`. A hit means the visitor is "buying-relevant" — Step 6 then runs Person Search at their company to find sales leadership peers.

See `ARCHITECTURE.md` §5 Step 5 and §6.3.

Current default: `sales`, `business_development`, `marketing`, `gtm`, `growth`, `revenue`.

**Important nuance**: this gate is about WHO the visitor is. Step 6's `TARGET_TITLE_CLAUSES` is about WHO we pull from their company. A marketing-VP visitor still triggers Person Search for sales leadership — different stages.

## Steps

1. **Read the current `QUALIFYING_ROLE_PATTERNS`** from `config.py`. Print.

2. **Ask the user** what change they want. Common patterns:
   - "Only sales should qualify — drop marketing visitors" → `("sales", "business_development", "revenue")`
   - "Include engineering" → add `engineering` (e.g., a CTO browses, that's signal)
   - "Include operations" → add `operations` (catches RevOps / Sales Ops visitors)
   - "Include finance/CFO" → add `finance` (CFOs evaluating B2B tools)

3. **Discuss the tradeoff**:
   - Tightening means fewer visitors qualify → fewer Slack alerts but higher signal
   - Loosening catches more buying signals at the cost of noise (e.g., engineers visiting just for blog content)
   - These are SUBSTRING matches against PDL's role taxonomy. PDL's standard roles include: `sales`, `marketing`, `engineering`, `operations`, `finance`, `human_resources`, `design`, `customer_service`, `legal`, `media`, `health`, `education`. Standard sub_roles vary widely.

4. **Apply the change** by editing the `QUALIFYING_ROLE_PATTERNS` tuple in `config.py`.

5. **Show the diff**.

## Verification

```bash
source venv/bin/activate
python -c "import config; print(config.QUALIFYING_ROLE_PATTERNS)"
```

Then verify against current data:
```bash
python run_pipeline.py --only 5
```

The log line shows the count of qualified visitors after the change. If a sales rep can name an expected visitor profile, search for their role:
```bash
python -c "
import json
data = json.load(open('data/enriched_ips.json'))
for ip, r in data.items():
    p = (r.get('enrichment') or {}).get('person') or {}
    role = p.get('job_title_role')
    sub = p.get('job_title_sub_role')
    if role or sub:
        print(ip, role, '/', sub)
"
```

## Caveats

- PDL's person inference is sparse — only ~5-15% of matched IPs have any person data. Changing this gate doesn't help if no IPs have person data at all.
- The intent-only path was removed in Round 5. There's no fallback if a visitor's PDL person data is missing — they don't qualify.
- Substring match: `"sales"` matches `"business_development"` only via the `business_development` pattern. If user wants to qualify ALL sales-adjacent roles, the current patterns already do that.
