---
name: setup-cloudflare-worker
description: Deploy the Cloudflare Worker capture layer for a new customer. Use during initial customer onboarding to deploy the IP-capture Worker, configure the customer's allowed origin, create the KV namespace, set the admin token, and generate the tracking pixel snippet for the customer's CMS. Handles `wrangler login`, KV creation, secret setting, deployment, and installation instructions.
---

# Set up Cloudflare Worker for a customer

## What this does

Deploys the visitor-capture layer (`cloudflaredeanonymizationsetup/`) to a fresh Cloudflare account. After this skill completes, the customer's website needs a `<script>` tag in its `<head>` to start sending visits.

See `ARCHITECTURE.md` §3.1 for what the Worker does.

## Prerequisites

- The user has a Cloudflare account (free tier works)
- Node.js 18+ installed (`node --version`)
- A workers.dev subdomain chosen on the Cloudflare dashboard (one-time setup at https://dash.cloudflare.com → Workers & Pages)
- The customer's production domain known (for `ALLOWED_ORIGIN`)

## Steps

1. **Confirm prerequisites** with the user. If they don't have a Cloudflare account or workers.dev subdomain, point them to https://dash.cloudflare.com.

2. **Ask for the customer's origin**:
   - Full URL with `https://`, no trailing slash, no path (e.g., `https://acme.com`)
   - If their main site is `www.acme.com`, use `https://www.acme.com`
   - Mixed www/non-www: pick the canonical version sales uses

3. **Generate a strong admin token**:
   ```bash
   openssl rand -hex 32
   ```
   Save this — the user needs it for the GitHub secret later. Store in `.env` as `WORKER_ADMIN_TOKEN`.

4. **Install Wrangler + log in**:
   ```bash
   cd cloudflaredeanonymizationsetup
   npm install
   npx wrangler login
   ```
   Opens browser → authorize → terminal confirms.

5. **Create the KV namespace**:
   ```bash
   npx wrangler kv namespace create IP_VISITS
   ```
   Copy the `id` value from the output. Update `wrangler.toml`:
   ```toml
   [[kv_namespaces]]
   binding = "IP_VISITS"
   id = "<paste id here>"
   ```

6. **Set `ALLOWED_ORIGIN` in `wrangler.toml`**:
   ```toml
   [vars]
   ALLOWED_ORIGIN = "https://acme.com"  # customer's actual origin
   ```

7. **Set the admin token secret**:
   ```bash
   echo -n "<the openssl-generated token>" | npx wrangler secret put ADMIN_TOKEN
   ```

8. **Deploy**:
   ```bash
   npx wrangler deploy
   ```
   Note the URL printed (e.g., `https://website-deanon.<subdomain>.workers.dev`). Save this as `WORKER_BASE_URL` in `.env`.

9. **Smoke test**:
   ```bash
   curl https://website-deanon.<subdomain>.workers.dev/health
   # Expected: {"ok":true,"ts":"..."}
   curl -H "Authorization: Bearer <token>" \
     "https://website-deanon.<subdomain>.workers.dev/export?limit=10"
   # Expected: {"ips":{},"cursor":null,"complete":true}
   ```

10. **Install the tracking pixel on the customer's site**. Two options:

    **Option A (recommended)**: Host `public/tracking.js` from the customer's own domain. Upload to `https://<customer>.com/tracking.js`, then add to their site header HTML (or equivalent CMS injection point):
    ```html
    <script>
      window.DEANON_CONFIG = {
        endpoint: "https://website-deanon.<subdomain>.workers.dev/collect"
      };
    </script>
    <script src="/tracking.js" defer></script>
    ```

    **Option B**: Inline the entire `tracking.js` contents directly in their site header HTML. Simpler but harder to update.

    For HubSpot CMS specifically: Settings → Website → Pages → Site header HTML.

11. **Update local `.env`** with the values just collected:
    ```
    WORKER_BASE_URL=https://website-deanon.<subdomain>.workers.dev
    WORKER_ADMIN_TOKEN=<the openssl-generated token>
    ```

## Verification

After tracking pixel is installed on the customer's site:
1. Visit any page on the customer's site
2. Open browser DevTools → Network tab → look for POST to `/collect` → confirm `200` status
3. Wait ~60 seconds (KV eventually consistent)
4. Run:
   ```bash
   curl -H "Authorization: Bearer <token>" \
     "https://website-deanon.<subdomain>.workers.dev/export?limit=10"
   ```
5. Should see at least one IP record in the response.

## Caveats

- The Worker's URL is **public** but `/export` requires the bearer token. `/collect` is open (CORS-restricted to the customer's origin).
- Updating `ALLOWED_ORIGIN` requires re-deploying (`npx wrangler deploy`).
- KV is eventually consistent — visits may take a few seconds to appear in `/export`.
- The Worker uses `cf.asOrganization`, `cf.threatScore`, etc. to filter bots/datacenters at capture time (Round 3-B feature). All free on every Cloudflare tier.
