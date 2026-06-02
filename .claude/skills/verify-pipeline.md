---
name: verify-pipeline
description: Run an end-to-end smoke test on the pipeline. Use after any significant config change, after setting up a new customer, or to confirm a deployment is healthy. Pings the Worker, verifies HubSpot properties exist, runs Step 5 dry, and tests the Slack webhook. Read-mostly — only Slack post is a side effect.
---

# Verify pipeline health

## What this does

Runs a series of read-mostly checks to confirm the deployment is healthy:

1. Worker `/health` returns 200
2. Worker `/export` is reachable with the configured admin token
3. HubSpot is reachable with the configured token
4. All 5 required HubSpot company properties exist
5. `config.py` imports without errors
6. `python run_pipeline.py --only 5` runs cleanly (no live writes)
7. Optionally: send a test Slack digest to confirm webhook works

See `ARCHITECTURE.md` §9 (failure modes) and §10.3 (monitoring).

## Steps

### 1. Config import check

```bash
source venv/bin/activate
python -c "import config; print('config.py imported OK')"
```

If this fails, the customization broke `config.py` syntax. Show the user the import error.

### 2. Cloudflare Worker health

```bash
curl -s -w 'HTTP %{http_code} in %{time_total}s\n' \
  $(python -c "import config; print(config.WORKER_BASE_URL)")/health
```

Expected: `{"ok":true,"ts":"..."} HTTP 200`. If 0 (network), the URL is wrong. If 401, the worker exists but admin token isn't being tested by `/health` (that's `/export`).

### 3. Worker `/export` auth check

```bash
python -c "
import config, requests
r = requests.get(
    f'{config.WORKER_BASE_URL}/export',
    params={'limit': 1},
    headers={'Authorization': f'Bearer {config.WORKER_ADMIN_TOKEN}'},
    timeout=10,
)
print(f'HTTP {r.status_code}')
print(r.text[:200])
"
```

Expected: `HTTP 200` with `{"ips":{...}, "cursor":..., "complete":...}`. If 401, the secret doesn't match what's set on the Worker via `wrangler secret put ADMIN_TOKEN` — re-set one or the other.

### 4. HubSpot connectivity + property check

```bash
python -c "
import config, requests
S = requests.Session()
S.headers.update({'Authorization': f'Bearer {config.HUBSPOT_API_KEY}'})
r = S.get(config.HUBSPOT_COMPANY_PROPERTIES, timeout=15)
existing = {p['name'] for p in r.json().get('results', [])}
required = set(config.TARGET_PROPERTIES)
missing = required - existing
print(f'HTTP {r.status_code}')
print(f'Required properties: {sorted(required)}')
print(f'Missing: {sorted(missing) if missing else \"none\"}')
"
```

Expected: `HTTP 200`, missing = none. If properties are missing, advise the user to either:
- Run `python run_pipeline.py --only 4 --dry-run` to trigger auto-creation (works if `crm.schemas.companies.write` scope is granted), OR
- Manually create them in HubSpot UI per `setup-hubspot.md`

### 5. PDL connectivity

```bash
python -c "
import config, requests
r = requests.get(
    config.PDL_IP_ENDPOINT,
    params={'ip': '8.8.8.8'},
    headers={'X-Api-Key': config.PDL_API_KEY},
    timeout=10,
)
print(f'HTTP {r.status_code}')
print(r.text[:200])
"
```

Expected: `HTTP 404` (Google DNS isn't a business IP — PDL returns no match) OR `HTTP 200`. Either is fine. `HTTP 401`/`403` means the PDL key is bad. `HTTP 402` means out of credits.

### 6. Step 5 dry pass

```bash
python run_pipeline.py --only 5
```

Should complete without errors. The log line tells you the qualifier output:
```
qualifier: N qualified / N ICP matched / N had person+role / N matched total
```

If `data/enriched_ips.json` doesn't exist yet (first deployment), the pipeline will fail with a FileNotFoundError — that's fine, just means no data has been pulled yet.

### 7. Slack webhook test (optional — has a side effect)

```bash
python notify.py --mode digest
```

This posts a digest summary to the configured Slack channel. The user should confirm they see the message in Slack.

If they don't see it:
- Check the GH secret: `gh secret list | grep SLACK_WEBHOOK_URL` (should have recent timestamp)
- Check the channel name in the Slack app config
- Re-do `setup-slack-webhook` if needed

## Output format

Present results as a checklist:

```
=== Pipeline Verification ===

[✓] config.py imports cleanly
[✓] Cloudflare Worker /health: HTTP 200 (243ms)
[✓] Worker /export authenticated: HTTP 200
[✓] HubSpot reachable: HTTP 200
[✓] HubSpot properties: 5/5 required exist
[✓] PDL key valid: HTTP 200
[✓] Step 5 dry-run: 0 qualified / 6 ICP / 0 person+role / 81 matched
[✓] Slack digest posted (waiting for user confirmation)

All checks passed. Pipeline is healthy.
```

For each failure, give a specific next step:
```
[✗] HubSpot properties: 1/5 required missing (`is_icp_fit`)
    → Re-run `python run_pipeline.py --only 4 --dry-run` to auto-create.
       If that fails, invoke `setup-hubspot` skill to grant
       `crm.schemas.companies.write` scope.
```

## Caveats

- This skill makes 6 read-only API calls (one each to Worker `/health`, `/export`, HubSpot properties, PDL `/v5/ip/enrich`) + one Slack post (if Step 7 is run). Negligible cost.
- The PDL `/v5/ip/enrich` call burns ~1 credit (for `8.8.8.8`'s no-match response, less because no result returned). Skip it if PDL spend is a concern.
- This skill is safe to invoke repeatedly — idempotent and read-mostly.
- If running on a brand-new deployment with no `data/*.json` yet, Step 5 will FileNotFoundError. That's expected — just means no pipeline runs have happened yet.
