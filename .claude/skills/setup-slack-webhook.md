---
name: setup-slack-webhook
description: Create a Slack incoming webhook URL for the customer's sales-alert channel and save it as a GitHub secret. Use during customer onboarding to configure the channel where new buying-committee contact alerts will land. Walks through the Slack app + Incoming Webhooks UI and sets the SLACK_WEBHOOK_URL secret.
---

# Set up the Slack webhook for alerts

## What this does

Creates a Slack incoming webhook URL scoped to one specific channel, then registers it as the `SLACK_WEBHOOK_URL` GitHub secret. After this skill completes, every CI run will post instant alerts to that channel when new buying-committee contacts are written to HubSpot.

See `ARCHITECTURE.md` §3.4.

## Prerequisites

- The customer has a Slack workspace
- The user has permission to install Slack apps in that workspace (typically requires admin or "manage apps" permission)
- The target channel exists (e.g., `#sales`, `#deanonymization-alerts`)
- `gh` CLI authenticated

## Steps

1. **Ask the user** which channel should receive alerts. Common choices:
   - `#sales` — high visibility, all reps see new prospects
   - `#sales-alerts` — dedicated low-noise channel
   - `#deanonymization` — purely for this pipeline's output
   - A private channel for early testing, then promote later

2. **Walk the user through the Slack UI**:
   - Visit https://api.slack.com/apps
   - Click **"Create New App"** → **"From scratch"**
   - **App name**: `Website Deanonymization` (or `<Customer> Deanonymization`)
   - **Pick workspace**: the customer's workspace
   - In the app's left sidebar: click **"Incoming Webhooks"**
   - Toggle **"Activate Incoming Webhooks"** to On
   - Scroll down → click **"Add New Webhook to Workspace"**
   - Pick the channel they chose in step 1
   - Click **"Allow"**
   - Copy the webhook URL (looks like `https://hooks.slack.com/services/T0000/B0000/abcdef...`)

3. **Test the webhook** with a single message:
   ```bash
   curl -X POST -H 'Content-Type: application/json' \
     -d '{"text":"Test message from setup-slack-webhook skill — if you see this, the webhook works."}' \
     "<webhook URL>"
   ```
   Confirm the user sees the test message in the channel.

4. **Set the GitHub secret**:
   ```bash
   echo -n "<webhook URL>" | gh secret set SLACK_WEBHOOK_URL
   ```

5. **(Optional) Also save to local `.env`** if the user wants to test `notify.py` locally:
   ```bash
   # Append to .env if not already present:
   echo "SLACK_WEBHOOK_URL=<webhook URL>" >> .env
   ```

## Verification

```bash
gh secret list | grep SLACK_WEBHOOK_URL
```

Should show the secret with a recent timestamp.

To do a full end-to-end test (will post to Slack):
```bash
source venv/bin/activate
python notify.py --mode digest
```

Should post a digest summary to the chosen channel.

## Caveats

- Slack webhooks are channel-scoped — they only post to one channel. To change channels, the user must create a new webhook (revoking the old one).
- Slack rate-limits webhooks to ~1 message/second per webhook. `notify.py` already sleeps 1s between chunked messages (Round 4).
- The webhook URL is a credential — anyone with it can post to the channel. Don't paste it in public channels or commits.
- If the Slack app is uninstalled from the workspace, the webhook stops working silently (no error to the pipeline).
