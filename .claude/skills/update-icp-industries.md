---
name: update-icp-industries
description: Update which PDL industries qualify as ICP (the `ICP_INDUSTRIES` frozenset in config.py). Use when the user wants to change which industries are considered ideal customer fit, add or remove specific industries like "computer software" or "information technology and services", broaden or narrow the ICP industry gate, or customize Step 4/5 ICP qualification for a customer. Edits config.py and verifies the change is syntactically valid.
---

# Update ICP industries

## What this controls

The `ICP_INDUSTRIES` frozenset in `config.py` gates Steps 4 and 5 of the pipeline. Only matched companies whose `data.company.industry` (returned by PDL IP Enrichment) appears in this set qualify as ICP-fit. Non-ICP companies still get written to HubSpot tagged `is_icp_fit=false`, but they never reach Person Search.

See `ARCHITECTURE.md` §6.1 for the design rationale.

PDL's industry taxonomy is LinkedIn-style — examples: `computer software`, `information technology and services`, `internet`, `computer & network security`, `computer hardware`, `computer networking`, `semiconductors`, `telecommunications`, `marketing and advertising`, `management consulting`. Industry strings are lowercased.

## Steps

1. **Read the current `ICP_INDUSTRIES`** from `config.py`. Look for the frozenset literal. Print the current set so the user knows the starting point.

2. **Ask the user** what change they want:
   - Add industries (which ones?)
   - Remove industries (which ones?)
   - Replace the whole set
   - Discuss what the customer's ICP looks like and propose industries

3. **Discuss tradeoffs** if relevant:
   - Adding `internet` catches more startups but includes B2C overlap
   - Adding `marketing and advertising` includes B2B agencies + their tools
   - Adding `computer & network security` adds cybersec vendors
   - Adding `telecommunications` is risky — most telecoms aren't B2B SaaS buyers (already handled by `ICP_EXCLUDE_TAG_PATTERNS`)

4. **Apply the change** by editing `config.py`. Find the block:
   ```python
   ICP_INDUSTRIES: frozenset[str] = frozenset({
       "computer software",
       "information technology and services",
   })
   ```
   Update it with the user's selection. Keep industries lowercased exactly as PDL returns them.

5. **Show the diff** to the user before saving (the Edit tool naturally does this).

## Verification

After the edit:

```bash
source venv/bin/activate
python -c "import config; print('ICP_INDUSTRIES =', sorted(config.ICP_INDUSTRIES))"
```

This both syntax-checks the file and confirms the new set.

If the user has live data, optionally run:

```bash
python run_pipeline.py --only 5
```

The qualifier log line will show how many companies now match ICP under the new rules.

## Caveats

- Industries are matched exactly (case-insensitive). A typo like `"sotware"` silently filters everything out.
- This is a config edit only — no HubSpot or Worker side effects.
- Round 4 added `ICP_EXCLUDE_TAG_PATTERNS` as a second-stage filter. Use `update-icp-tags` if the goal is to refine WITHIN an industry rather than choose industries.
