# Cloudflare Worker Deployment Walkthrough

This guide walks you through deploying the Website Deanonymization capture layer to Cloudflare Workers. Takes about 15 minutes start to finish.

> **Note:** Since you don't have HubSpot Marketing Hub Enterprise / Operations Hub, the HubSpot Custom Behavioral Events mirror is skipped. Cloudflare KV is the source of truth — the Python pipeline pulls directly from the Worker's `/export` endpoint. Everything still works.

---

## Before you start

You need:

- A **Cloudflare account** (free tier is fine) — sign up at https://dash.cloudflare.com/sign-up if you don't have one
- A **workers.dev subdomain** chosen — first-time prompt at https://dash.cloudflare.com → Workers & Pages. Pick something like `yourname.workers.dev`. One-time setup.
- **Node.js 18+** installed locally — verify with `node --version`
- A **long random string** for your admin token. Generate with `openssl rand -hex 32` on Mac/Linux, or use a password generator. 32+ chars.

You do **not** need to click "Create Worker" in the Cloudflare dashboard. `wrangler deploy` creates it automatically the first time.

The five files (`wrangler.toml`, `package.json`, `README.md`, `src/worker.js`, `public/tracking.js`) should already be saved into a folder named `01a_cloudflare_worker/` with this structure:

```
01a_cloudflare_worker/
├── wrangler.toml
├── package.json
├── README.md
├── src/
│   └── worker.js
└── public/
    └── tracking.js
```

---

## Step 1 — Install Wrangler and log in

In your terminal:

```bash
cd 01a_cloudflare_worker
npm install
npx wrangler login
```

The login command opens your browser, you authorize Wrangler to access your Cloudflare account, and the terminal confirms.

---

## Step 2 — Create the KV namespace

```bash
npx wrangler kv namespace create IP_VISITS
```

You'll see output like:

```
🌀 Creating namespace with title "website-deanon-IP_VISITS"
✨ Success!
Add the following to your configuration file:
[[kv_namespaces]]
binding = "IP_VISITS"
id = "abc123def456..."
```

**Copy that `id` value.** Open `wrangler.toml` and replace `PASTE_KV_NAMESPACE_ID_HERE` with it.

> If the command fails with "unknown command", try the old form: `npx wrangler kv:namespace create IP_VISITS` (with a colon).

---

## Step 3 — Set the admin token secret

```bash
npx wrangler secret put ADMIN_TOKEN
```

Paste your long random string when prompted. Hit enter.

**Save this token somewhere safe** — you'll put it in your Python project's `.env` later as `WORKER_ADMIN_TOKEN`.

> Skip setting `HUBSPOT_API_KEY` since we're not mirroring to HubSpot events. The Worker's HubSpot send only runs if that secret is present, so leaving it unset cleanly disables the mirror.

---

## Step 4 — Set your site's origin

Open `wrangler.toml` and change this line to match your actual site:

```toml
ALLOWED_ORIGIN = "https://yourdomain.com"
```

Use the exact origin: scheme + host, no trailing slash, no path. For local testing with `http://localhost:3000` you can temporarily set it to `*`, but tighten before going live.

---

## Step 5 — Deploy

```bash
npx wrangler deploy
```

You'll see:

```
Uploaded website-deanon (X sec)
Published website-deanon (Y sec)
  https://website-deanon.<your-subdomain>.workers.dev
```

**Copy that URL.** That's your Worker's base URL — you'll need it for the tracking snippet and the Python pipeline.

---

## Step 6 — Smoke test the Worker

Health check (no auth required):

```bash
curl https://website-deanon.<your-subdomain>.workers.dev/health
```

Expected response: `{"ok":true,"ts":"..."}`

Export endpoint (requires admin token):

```bash
curl -H "Authorization: Bearer <YOUR_ADMIN_TOKEN>" \
  "https://website-deanon.<your-subdomain>.workers.dev/export?limit=100"
```

Expected response: `{"ips":{},"cursor":null,"complete":true}` — empty because no visits yet.

If you get `401 unauthorized`, double-check that you pasted the same token in `wrangler secret put ADMIN_TOKEN` that you're using in the curl header.

---

## Step 7 — Install the tracking snippet on your site

Since HubSpot is your CMS, do this in HubSpot under **Settings → Website → Pages → Site header HTML** (this injects on every page).

**Option A: Host `tracking.js` from your own site (recommended).** Upload `public/tracking.js` to your site so it's served from `https://yourdomain.com/tracking.js`. Then add this to the site header HTML:

```html
<script>
  window.DEANON_CONFIG = {
    endpoint: "https://website-deanon.<your-subdomain>.workers.dev/collect"
  };
</script>
<script src="/tracking.js" defer></script>
```

**Option B: Inline the whole script.** Paste the entire contents of `tracking.js` directly inside a `<script>` tag in your site header, prefixed with the `DEANON_CONFIG` block. Simpler but harder to update later.

---

## Step 8 — Verify it's working

1. Visit any page on your site.
2. Open browser DevTools → Network tab → look for a POST to `/collect`. Status should be 200.
3. Wait ~60 seconds (KV is eventually consistent).
4. From your terminal:

   ```bash
   npx wrangler kv key list --binding IP_VISITS
   ```

   You should see `ip:<your-public-ip>` in the list.

5. To inspect the record:

   ```bash
   npx wrangler kv key get --binding IP_VISITS "ip:<your-ip>"
   ```

   You should see the visit record JSON with `pages_visited`, `visit_count`, etc.

6. Hit the export endpoint again — your IP should now be there:

   ```bash
   curl -H "Authorization: Bearer <YOUR_ADMIN_TOKEN>" \
     "https://website-deanon.<your-subdomain>.workers.dev/export?limit=100"
   ```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `wrangler deploy` says "no workers.dev subdomain" | First-time account, no subdomain chosen yet | Visit https://dash.cloudflare.com → Workers & Pages and pick a subdomain |
| `/export` returns 401 | Wrong or missing `ADMIN_TOKEN` | Re-run `npx wrangler secret put ADMIN_TOKEN`, redeploy |
| Browser console: CORS error on POST to `/collect` | `ALLOWED_ORIGIN` doesn't match site origin exactly | Edit `wrangler.toml`, redeploy. Origin must be `https://yourdomain.com` with no path or trailing slash |
| `wrangler kv namespace create` fails | Old Wrangler version | Try `npx wrangler kv:namespace create IP_VISITS` (with colon), or upgrade: `npm install wrangler@latest` |
| Your own IP not appearing in KV after a visit | Snippet didn't fire, you're on a private IP, or DNT is on | Check Network tab for the POST, disable DNT, try from mobile data |
| KV `list` is empty immediately after a visit | KV is eventually consistent | Wait ~60 seconds and retry |
| Visit recorded but no `path` field | Snippet served from wrong origin, or page is a redirect | Verify `window.location.pathname` in browser console |

---

## What's next

Once you've confirmed visits are flowing into KV, you're ready for the Python pipeline:

- **Step 1B** pulls `/export` into `data/ip_visits.json`
- **Step 2** filters down to high-intent IPs
- **Step 3** enriches with People Data Labs
- **Step 4** writes the results back to HubSpot company records

Save your Worker's base URL and admin token — they'll go into the Python project's `.env`:

```
WORKER_BASE_URL=https://website-deanon.<your-subdomain>.workers.dev
WORKER_ADMIN_TOKEN=<the long random string from Step 3>
```
