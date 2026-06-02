---
name: update-title-excludes
description: Update which job titles are excluded from PDL Person Search even when the inclusion clauses match (the `TITLE_EXCLUDE_PATTERNS` tuple in config.py). Use when filtering out regional VPs, sub-segment specialists like carrier sales or strategic accounts, channel/partner sales, enterprise architects, communications VPs, individual contributors, or any tangential title. Edits config.py.
---

# Update title-level exclude patterns

## What this controls

`TITLE_EXCLUDE_PATTERNS` in `config.py` is the `must_not` block of Step 6's PDL Person Search. Every entry becomes a `match_phrase` clause that excludes anyone whose `job_title` contains that phrase. Applied at PDL-side, so excluded results don't burn credits.

See `ARCHITECTURE.md` §6.5.

Current categories of patterns:
- **Geo / regional**: `regional`, `area`, `north america`, `north american`, `americas`, `emea`, `apac`, `latam`, `international`, `asia`, `mid west`, `midwest`, `northwest`, `southwest`, `southeast`
- **Channel / sub-segment**: `carrier`, `partner sales`, `channel`, `strategic accounts`, `alliances`, `indirect`
- **Tangential**: `enterprise architect`, `communications`, `product marketing`, `field marketing`, `small business`, `smb`, `consumer`, `growth strategy`, `pre-sales`, `sales engineer`, `sales support`, `sales enablement`
- **IC roles**: `account executive`, `account manager`, `representative`, `associate`

## Steps

1. **Read the current `TITLE_EXCLUDE_PATTERNS`** from `config.py`. Print categorized.

2. **Ask the user** what they want to change. Common scenarios:
   - "I want to ALLOW regional VPs (we sell regionally)" → remove geo patterns
   - "Block field marketing too" → already blocked, confirm
   - "Block customer success / CSM" → add `customer success`, `csm`
   - "Block specific role like 'pre-sales engineer'" → add `pre-sales engineer` (note: `pre-sales` is already a pattern)

3. **Discuss the tradeoff**:
   - More excludes = tighter filter, fewer false positives, slightly higher chance of missing a legitimate leader (e.g., "Chief Customer Officer" who actually owns sales)
   - Fewer excludes = more noise in alerts but lower miss rate

4. **Apply the change** by editing the `TITLE_EXCLUDE_PATTERNS` tuple in `config.py`. Preserve the comment grouping by category if possible.

5. **Show the diff**.

## Verification

```bash
source venv/bin/activate
python -c "import config; print(len(config.TITLE_EXCLUDE_PATTERNS), 'exclude patterns'); print(*config.TITLE_EXCLUDE_PATTERNS, sep='\n')"
```

For a live test, run the same script as in `update-buying-committee` — visually inspect that previously-noisy titles (Regional VP, Carrier Sales, etc.) no longer appear in the results.

## Caveats

- Patterns are SUBSTRINGS within the title, matched via PDL's `match_phrase`. Short patterns can over-match. For example, `"area"` would also match a title like "Sales Area Lead" (probably intended) but also any title containing the word "area" in a different sense.
- After significant changes, recommend deleting `data/pdl_person_cache.json` so the next Step 6 re-queries with the new clauses.
- Exclude patterns are applied to EVERY query — they affect all qualifying ICP companies equally.
