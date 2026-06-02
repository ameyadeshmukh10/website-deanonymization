---
name: update-buying-committee
description: Update which job titles count as buying committee for the PDL Person Search in Step 6 (the `TARGET_TITLE_CLAUSES` list in config.py). Use when changing target sales/marketing/RevOps/CMO/CRO leadership titles, narrowing or broadening which contacts get pulled into the CRM, adding new title phrases, or refining the title gate. Edits config.py.
---

# Update buying-committee target titles

## What this controls

`TARGET_TITLE_CLAUSES` in `config.py` is a list of Elasticsearch `should` clauses used by Step 6's PDL Person Search at qualifying ICP companies. Any person whose title matches at least one clause is a candidate. Combined with `TITLE_EXCLUDE_PATTERNS` (the `must_not` filter), this determines exactly which people get written to HubSpot as contacts.

See `ARCHITECTURE.md` §5 Step 6 and §6.4 for context.

Current default (Round 5): **sales leadership only** — CRO, CSO, CCO, Head of Sales, VP Sales (multiple phrasings), Head of RevOps, RevOps senior+. Marketing leadership (CMO, VP Marketing) is intentionally NOT included.

Each clause is one of:
- `{"match_phrase": {"job_title": "<exact phrase>"}}` — exact-token phrase match (preferred)
- `{"bool": {"must": [{"terms": {"job_title_levels": [...]}}, {"match_phrase": {...}}]}}` — level-gated phrase

Phrase matches are precise. Adding `{"term": {"job_title_role": "sales"}}`-style broad clauses is discouraged — they catch regional and sub-segment VPs (which is exactly why we tightened in Round 5).

## Steps

1. **Read the current `TARGET_TITLE_CLAUSES`** from `config.py`. Walk the user through the current titles (extracting phrase strings is most useful).

2. **Ask the user** what change they want:
   - Add CMO / VP Marketing back? (matches `match_phrase: "chief marketing officer"`, `"vp marketing"`, etc.)
   - Add specific other titles like "Head of Growth", "VP of GTM"
   - Drop a title
   - Replace the whole list

3. **For each title to add**, propose specific phrase variants. PDL is precise about token order:
   - "CRO" → `match_phrase: "chief revenue officer"` (the abbreviation rarely appears verbatim in PDL job_title)
   - "VP Marketing" → likely need: `vp marketing`, `vp of marketing`, `vice president marketing`, `vice president of marketing`
   - "Head of Growth" → `head of growth`
   - "RevOps director" — already covered by the level-gated `revenue operations` / `revops` clause when senior+

4. **Apply the change** by editing the `TARGET_TITLE_CLAUSES` list literal in `config.py`. Preserve the existing comment headers (e.g., `# C-suite (sales / revenue / commercial)`).

5. **Show the user the diff** before saving.

## Verification

```bash
source venv/bin/activate
python -c "
import config, json
print(f'{len(config.TARGET_TITLE_CLAUSES)} clauses:')
for c in config.TARGET_TITLE_CLAUSES:
    print(' ', json.dumps(c))
"
```

Then if PDL credentials are configured, optional live test on one known ICP company:
```bash
python -c "
import config, requests, json
body = {
    'query': {'bool': {
        'must': [
            {'term': {'job_company_website': 'oracle.com'}},
            {'term': {'location_country': 'united states'}},
            {'bool': {'should': config.TARGET_TITLE_CLAUSES}},
        ],
        'must_not': [{'match_phrase': {'job_title': p}}
                     for p in config.TITLE_EXCLUDE_PATTERNS],
    }},
    'size': 10,
}
r = requests.post(config.PDL_PERSON_SEARCH_ENDPOINT,
                  headers={'X-Api-Key': config.PDL_API_KEY,
                           'Content-Type':'application/json'},
                  json=body, timeout=20)
for p in (r.json().get('data') or []):
    print(p.get('full_name'), '—', p.get('job_title'))
"
```

This burns 1 PDL credit per result returned (~10 credits) but validates the clauses actually return the right people.

## Caveats

- Phrase match is exact-token-order. `"vp sales"` matches `"VP Sales – North America"` BUT the `must_not` clause on `"north america"` filters it back out. The two filters work together.
- Adding marketing titles will significantly increase contact volume per qualifying ICP company. Discuss with the user whether their sales team wants marketing leads.
- The `pdl_person_cache.json` may have stale results from prior queries. After significant changes, recommend the user delete it so the next Step 6 re-queries with the new clauses.
