# Cloudflare Worker — Website Deanonymization Capture Layer

This Worker captures visitor IPs and page views from a JS snippet on your site, stores them in Cloudflare KV, and mirrors each visit to HubSpot as a Custom Behavioral Event (best-effort).

## Files

```
01a_cloudflare_worker/
├── wrangler.toml         # Worker config + deployment instructions
├── package.json          # wrangler dev dependency
├── src/
│   └── worker.js         # the Worker itself (/collect, /export, /health)
└── public/
    └── tracking.js       # JS snippet you embed on your site
```

## Endpoints

| Method | Path     | Purpose                                                  | Auth                       |
| ------ | -------- | -------------------------------------------------------- | -------------------------- |
| POST   | /collect | Tracking snippet posts visits here                       | CORS (origin must match)   |
| GET    | /export  | Python pipeline pulls accumulated visits                 | `Bearer <ADMIN_TOKEN>`     |
| GET    | /health  | Smoke test                                               | None                       |

## Storage schema (in KV, key = `ip:<ip>`)

```json
{
  "ip": "72.212.42.169",
  "first_seen": "2025-01-15T10:23:00.000Z",
  "last_seen": "2025-01-20T14:55:00.000Z",
  "visit_count": 7,
  "session_count": 3,
  "pages_visited": [
    {"url":"...", "path":"/pricing", "timestamp":"...", "referrer":"..."}
  ],
  "unique_pages": ["/pricing", "/demo"],
  "session_ids": ["..."],
  "visitor_ids": ["..."],
  "user_agents": ["..."]
}
```

Caps to keep KV values reasonable: 500 page visits, 200 unique paths, 200 session ids, 50 visitor ids, 10 user agents per IP (oldest dropped first).

## HubSpot Custom Behavioral Event

Before deploying, in HubSpot go to **Settings → Data Management → Custom Events → Create event** and create one named `pe_website_visit` with these properties:

- `hs_page_url` (single-line text)
- `hs_page_path` (single-line text)
- `hs_referrer` (single-line text)
- `hs_ip_address` (single-line text)
- `hs_session_id` (single-line text)
- `hs_visitor_id` (single-line text)
- `hs_user_agent` (multi-line text)

Requires **Marketing Hub Enterprise** or **Operations Hub**. If your portal doesn't have that tier, the mirror call will 403 and the rest of the system still works — KV is the source of truth.

## Privacy notes

- The tracking snippet honors `navigator.doNotTrack` and `navigator.globalPrivacyControl`.
- Stores a random UUID in `localStorage` (`deanon_vid`) and a session UUID in `sessionStorage` (`deanon_sid`). If a `hubspotutk` cookie is present, uses it as the visitor id so records align with HubSpot.
- Server-side filters: private/reserved IPs are dropped; common bot user agents are dropped.
- IP addresses are personal data under GDPR/CCPA — update your privacy policy. For EU traffic, gate the snippet behind your consent banner.

## Deployment

See the comments at the top of `wrangler.toml` for the exact command sequence. Quick reference:

```bash
npm install
npx wrangler login
npx wrangler kv namespace create IP_VISITS
# paste the returned id into wrangler.toml under [[kv_namespaces]] id
npx wrangler secret put HUBSPOT_API_KEY
npx wrangler secret put ADMIN_TOKEN
# edit wrangler.toml: vars.ALLOWED_ORIGIN = "https://yourdomain.com"
npx wrangler deploy
```

Then smoke test:

```bash
curl https://<your-worker>.workers.dev/health
# → {"ok":true,"ts":"..."}
```

Add the snippet to your site (in `<head>` or right before `</body>`):

```html
<script>
  window.DEANON_CONFIG = {
    endpoint: "https://<your-worker>.workers.dev/collect"
  };
</script>
<script src="/tracking.js" defer></script>
```

Visit a page on your site, then verify:

```bash
npx wrangler kv key list --binding IP_VISITS
# should show ip:<your_ip>
```

## Testing /export

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  "https://<your-worker>.workers.dev/export?limit=100"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `/export` returns 401 | Wrong or missing `ADMIN_TOKEN` | `npx wrangler secret put ADMIN_TOKEN` again |
| Browser console: CORS error | `ALLOWED_ORIGIN` doesn't match site origin | Edit `wrangler.toml`, redeploy |
| HubSpot send logs 403 | Portal lacks Marketing Hub Enterprise / Ops Hub | Skip the custom event; KV still works |
| HubSpot send logs 400 | Event name or property names don't match | Verify the event exists in HubSpot exactly as `pe_website_visit` |
| Your own IP not appearing in KV | Snippet didn't fire, or you're behind a private IP | Check browser network tab for POST to `/collect`; DNT/GPC may be on |
| KV `list` is empty after a visit | KV is eventually consistent (~60s) | Wait a minute and retry |
