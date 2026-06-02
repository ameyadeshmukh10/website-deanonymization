---
name: update-high-intent-paths
description: Update which URL paths count as high-intent for Step 2 filtering (the `HIGH_INTENT_PATHS` tuple in config.py). Use when configuring the customer's pricing, demo, signup, contact, or product pages, customizing which paths reduce the IP candidate pool before PDL enrichment, or onboarding a customer with different URL structure. Edits config.py.
---

# Update high-intent paths

## What this controls

`HIGH_INTENT_PATHS` in `config.py` is a tuple of URL path prefixes that signal commercial intent. Step 2 (`filter_intent.py`) keeps an IP iff `visit_count >= MIN_VISITS_FOR_INTENT` (default 2) OR any of its `unique_pages` starts with one of these patterns.

See `ARCHITECTURE.md` §5 Step 2 and §6.6.

Step 2's purpose is cost optimization: filtering before Step 3's per-IP PDL enrichment saves ~85% of PDL credits per run. The filter doesn't gate Step 5/6 anymore (Round 5 made Step 5 require PDL person inference) — high-intent paths are now purely for the Step 2 pre-filter and the `website_visit_intent` annotation written to HubSpot companies.

Current default paths (Everworker-shaped):
```
/pricing
/demo
/sales-ai-workers/sdr-lead-response-worker
/sales-ai-workers/pipeline-reporting-ai-worker
/sales-ai-workers/sdr-ai-worker-full
/lets-talk
/sales-ai-workers
/sales-ai-workers/sdr-ai-worker
/sales-ai-workers/sales-playbook-ai-worker
```

## Steps

1. **Read the current `HIGH_INTENT_PATHS`** from `config.py`.

2. **Ask the user** about the customer's site structure:
   - What's their pricing page URL? (e.g., `/pricing`, `/plans`, `/pricing-and-plans`)
   - Demo / signup / book-a-meeting? (e.g., `/demo`, `/get-started`, `/schedule-demo`)
   - Specific product pages that signal real buying intent? (the equivalent of Everworker's `/sales-ai-workers/*`)
   - Contact / sales pages? (`/contact-sales`, `/talk-to-us`)

3. **Discuss inclusion bar**:
   - Pages that ONLY commercial-intent buyers visit qualify (pricing, demo, product detail pages, sales contact forms)
   - Blog posts, careers pages, company info — usually NOT high-intent (browsers, not buyers)
   - About / team / press — not high-intent

4. **Apply the change** by editing the `HIGH_INTENT_PATHS` tuple in `config.py`. Prefer leading slash, lowercase, no trailing slash.

5. **Show the diff**.

## Verification

```bash
source venv/bin/activate
python -c "import config; print(config.HIGH_INTENT_PATHS)"
```

Run Step 2 to see the new filter ratio:
```bash
python run_pipeline.py --only 1b,2
```

Look for the log line: `filter_intent: wrote N ips to data/high_intent_ips.json` — the survivor count divided by the total `ip_visits.json` count tells you the pass rate.

## Caveats

- Match is PREFIX-based (`startswith`, case-insensitive). `/pricing` matches `/pricing/enterprise` and `/pricing?utm=x`. If you want strict equality, add a trailing-character requirement in `filter_intent._matched_high_intent_paths` (current implementation is intentionally permissive).
- These paths are SITE-SPECIFIC. For a multi-tenant deployment, each customer needs their own values.
- After major changes, the next Step 2 run reshapes the candidate pool. Don't be alarmed if Step 4 writes more/fewer companies — that reflects the new filter, not a bug.
- Adding too many paths defeats the purpose (the filter loses its filtering power). Aim for 5-15 paths.
