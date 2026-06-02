---
name: update-schedule
description: Update the GitHub Actions cron schedule for how often the pipeline runs (the `schedule.cron` in .github/workflows/run-pipeline.yml). Use when changing pipeline frequency, running hourly vs daily, adjusting daily summary timing, or customizing the cadence for a customer. Edits the workflow YAML.
---

# Update pipeline schedule

## What this controls

Two GitHub Actions workflows have cron schedules:

1. **`.github/workflows/run-pipeline.yml`** — runs the full pipeline (Steps 1B-7 + notify). Currently `0 */4 * * *` (every 4 hours, 6 runs/day).

2. **`.github/workflows/daily-summary.yml`** — posts the daily digest. Currently `0 11 * * *` (11 UTC = 7am EDT / 6am EST — accept 1hr DST drift).

GitHub Actions cron uses standard cron syntax with UTC. Minimum interval is every 5 minutes for `schedule:` triggers (and not guaranteed — GitHub may delay during peak load).

See `ARCHITECTURE.md` §3.3, §10.2.

## Steps

1. **Read the current schedules** from both workflow files.

2. **Ask the user** what they want:
   - "Run every hour" → cron `0 * * * *`
   - "Twice daily — 7am and 1pm Eastern" → cron `0 11,17 * * *` (or `0 12,18 * * *` depending on DST)
   - "Once a day, business hours" → cron `0 12 * * 1-5` (12 UTC, Mon-Fri)
   - "Don't run on weekends" → add day-of-week constraint
   - Change daily-summary timing

3. **Discuss tradeoffs**:
   - More frequent = fresher signal but more GitHub Actions minutes consumed (~360 min/month at current 4h cadence). Free tier private repo cap is 2000 min/month.
   - Hourly (`0 * * * *`) = 720 runs/month × ~2 min = 1440 min/month — still within free tier.
   - Less frequent = stale signal but lower load. Once-daily (1 run/day) = 60 min/month.
   - PDL cache hits keep marginal cost near zero regardless of frequency.

4. **Apply the change** by editing the `schedule.cron` value in `.github/workflows/run-pipeline.yml`. If changing the digest timing, edit `.github/workflows/daily-summary.yml`.

5. **Validate the cron expression**. Use `https://crontab.guru/` syntax — quick sanity check:
   - `0 */4 * * *` → at minute 0 past every 4th hour
   - `*/30 * * * *` → every 30 minutes
   - `0 9-17 * * 1-5` → hourly during business hours weekdays

6. **Show the diff**.

## Verification

```bash
# YAML syntax check
python -c "import yaml; print(yaml.safe_load(open('.github/workflows/run-pipeline.yml')))" | head -3
python -c "import yaml; print(yaml.safe_load(open('.github/workflows/daily-summary.yml')))" | head -3
```

After commit + push, confirm the schedule appears in GitHub:
```bash
gh workflow view run-pipeline.yml | head -20
```

For an immediate test (not waiting for the next cron):
```bash
gh workflow run run-pipeline.yml
gh run watch
```

## Caveats

- GitHub Actions cron is **UTC only**. Account for DST when picking times: 11 UTC = 6am EST (winter) / 7am EDT (summer). User's "7am customer time" maps to a different UTC depending on month.
- Schedules can be **delayed** during high GitHub load — `0 11 * * *` might fire at 11:07 UTC instead. Not a problem for our use case.
- The minimum schedule interval is 5 minutes — anything tighter gets coalesced.
- Workflow_dispatch (manual trigger) is independent of the schedule.
