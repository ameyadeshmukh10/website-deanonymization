---
name: update-icp-tags
description: Update the ICP exclude tag patterns (the `ICP_EXCLUDE_TAG_PATTERNS` frozenset in config.py). Use when the user wants to filter out telecoms, ISPs, VoIP, managed services, hosting providers, or other sub-segments that PDL classifies under a broad industry bucket. Adds the second-stage ICP filter that runs AFTER the industry check. Edits config.py and verifies.
---

# Update ICP exclude tag patterns

## What this controls

PDL classifies many companies under broad industry buckets like `information technology and services`. That bucket includes legitimate B2B SaaS (good) AND telecoms, ISPs, MSPs, hosting providers (bad — these aren't software buyers). PDL also returns a `tags` array per company with more specific descriptors like `voip`, `hosting`, `telecommunications`, `managed services`.

`ICP_EXCLUDE_TAG_PATTERNS` (frozenset in `config.py`) is the SECOND filter in the ICP gate. If `is_icp_fit` already passed the industry check, but any of these substrings appears in the company's joined tag list (case-insensitive substring match), `is_icp_fit` flips back to `false`.

See `ARCHITECTURE.md` §6.2 for design rationale.

Current default set: `telecommunications`, `voip`, `voice and data`, `isp`, `internet service provider`, `hosting`, `managed services`, `carrier`, `telecom`, `mobile network`, `broadband`.

## Steps

1. **Read the current `ICP_EXCLUDE_TAG_PATTERNS`** from `config.py`. Print the current set.

2. **Ask the user** what change they want:
   - Add exclude patterns (which sub-segments to filter out?)
   - Remove patterns (e.g., they want to include hosting providers as ICP after all)
   - Discuss whether a real-world miss (a company that shouldn't have been ICP-fit but was) suggests a new pattern

3. **Discuss tradeoffs**:
   - Patterns are SUBSTRING-MATCHED, case-insensitive. Adding `"saas"` would match `"saas-platform"` and `"saas software"` — usually too broad.
   - Adding `"healthcare"` may exclude legitimate B2B health-tech if their tags include the word
   - Specific multi-word patterns are safer than single broad words

4. **Apply the change** by editing `config.py`. Find:
   ```python
   ICP_EXCLUDE_TAG_PATTERNS: frozenset[str] = frozenset({
       "telecommunications",
       ...
   })
   ```
   Update with the user's selection. Lowercase each pattern.

## Verification

```bash
source venv/bin/activate
python -c "import config; print('ICP_EXCLUDE_TAG_PATTERNS =', sorted(config.ICP_EXCLUDE_TAG_PATTERNS))"
```

Then test against a known company:
```bash
python -c "
import json, icp_filter, config
data = json.load(open('data/enriched_ips.json'))
matched = [(ip, r) for ip, r in data.items()
           if (r.get('enrichment') or {}).get('status') == 'matched']
print(f'{len(matched)} matched companies; {sum(1 for ip,r in matched if icp_filter.is_icp_fit(r))} pass ICP')
"
```

If `data/enriched_ips.json` doesn't exist yet, that's fine — verification by import is enough.

## Caveats

- Substring match means short patterns can over-match. Prefer specificity.
- This filter does NOT change the company's HubSpot industry value — it only affects our `is_icp_fit` decision.
- Manual override protection (Round 4) still applies — if a sales rep flipped a company's ICP fit in HubSpot, this change doesn't override their edit.
