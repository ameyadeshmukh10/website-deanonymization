---
name: setup-github-actions
description: Bootstrap a private GitHub repo for the customer's pipeline deployment and configure all required secrets for the scheduled workflows. Use during customer onboarding after the Cloudflare Worker, HubSpot, and Slack pieces are configured. Creates the repo, pushes the code, sets the 6 GitHub Actions secrets via gh CLI, and triggers the first run.
---

# Set up GitHub Actions for a customer

## What this does

Creates a private GitHub repo for the customer's pipeline, pushes the code, sets the 6 required repo secrets, and triggers the first workflow run. After this skill completes, the pipeline runs on the configured cron schedule (default every 4h).

See `ARCHITECTURE.md` §3.3.

## Prerequisites

- `gh` CLI installed (`gh --version`) — if not, `brew install gh` on macOS
- The user authenticated with `gh auth login` (one-time setup)
- Values for all 6 secrets ready to paste:
  - `WORKER_BASE_URL` — from `setup-cloudflare-worker`
  - `WORKER_ADMIN_TOKEN` — from `setup-cloudflare-worker`
  - `PDL_API_KEY` — PDL dashboard
  - `HUBSPOT_API_KEY` — from `setup-hubspot`
  - `HUBSPOT_LINKEDIN_PROPERTY` — typically `hs_linkedin_url`
  - `SLACK_WEBHOOK_URL` — from `setup-slack-webhook`
  - Optional: `HUBSPOT_PORTAL_ID` — visible in any HubSpot URL

## Steps

1. **Verify gh authentication**:
   ```bash
   gh auth status
   ```
   If not logged in: `gh auth login` → GitHub.com → HTTPS → web browser.

2. **Confirm cwd is the repo root** (where `README.md` and `config.py` live).

3. **Stage and commit current state** (if not already committed):
   ```bash
   git add -A
   git commit -m "initial commit"
   ```
   If `git commit` complains about no email configured, the user can set one with `git config --global user.email "<their-email>"`.

4. **Create the private repo and push**:
   ```bash
   gh repo create <customer>-website-deanonymization --private --source=. --remote=origin --push
   ```
   Pick a name that's meaningful to the user — `<customer>-website-deanonymization` is a reasonable default.

5. **Set the 6 (or 7) secrets**. Use `echo -n` piped to `gh secret set` for each — this avoids the trailing-newline paste mistakes that bit us during initial setup:

   ```bash
   echo -n "<WORKER_BASE_URL value>" | gh secret set WORKER_BASE_URL
   echo -n "<WORKER_ADMIN_TOKEN value>" | gh secret set WORKER_ADMIN_TOKEN
   echo -n "<PDL_API_KEY value>" | gh secret set PDL_API_KEY
   echo -n "<HUBSPOT_API_KEY value>" | gh secret set HUBSPOT_API_KEY
   echo -n "hs_linkedin_url" | gh secret set HUBSPOT_LINKEDIN_PROPERTY
   echo -n "<SLACK_WEBHOOK_URL value>" | gh secret set SLACK_WEBHOOK_URL
   # Optional but recommended for clickable Slack links:
   echo -n "<HUBSPOT_PORTAL_ID value>" | gh secret set HUBSPOT_PORTAL_ID
   ```

   For each, ask the user to paste the value. Don't echo them back.

6. **Verify secrets are set**:
   ```bash
   gh secret list
   ```
   Should show all 6 (or 7) secret names with recent timestamps.

7. **Trigger the first workflow run**:
   ```bash
   gh workflow run run-pipeline.yml
   ```

8. **Watch the run**:
   ```bash
   sleep 6 && gh run watch $(gh run list --workflow=run-pipeline.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
   ```

9. **If the run fails**, view the failed step log:
   ```bash
   gh run view <run-id> --log-failed
   ```
   Common failures:
   - `401 unauthorized` on Step 1B → `WORKER_ADMIN_TOKEN` mismatch (re-set the secret cleanly with `echo -n`)
   - `InvalidURL: No host supplied` on Step 1B → `WORKER_BASE_URL` lacks `https://` prefix
   - HubSpot 401 → token scopes missing (see `setup-hubspot`)
   - PDL 401 → bad `PDL_API_KEY`

## Verification

After the first successful run:
- `gh run list --workflow=run-pipeline.yml` shows a green run
- The `pipeline-state` artifact appears under Actions → Artifacts
- Slack message arrives (or "no new contacts this run" if no signal)

## Caveats

- Free GitHub Actions cap for private repos is 2000 min/month. The 4h cron uses ~360 min/month — well under.
- Artifact storage: 500 MB free; `data/` is ~5 MB per artifact, 30-day retention.
- The repo is private — secrets are only readable by GitHub Actions workflows in this repo.
- Anyone with write access to the repo can use the secrets (be cautious about adding collaborators).
- If the customer's PDL or HubSpot key rotates, re-set the secret with `echo -n "<new>" | gh secret set <NAME>` — no other action needed.
