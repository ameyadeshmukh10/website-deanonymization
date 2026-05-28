// =============================================================================
// Website Deanonymization — Cloudflare Worker
// =============================================================================
//
// Captures visitor IP + page view events from a JS snippet on your site,
// stores them in Cloudflare KV (binding: IP_VISITS), and mirrors each visit
// to HubSpot as a Custom Behavioral Event (best-effort).
//
// Endpoints:
//   POST /collect   — tracking snippet posts visits here
//   GET  /export    — Python pipeline pulls accumulated visits here (auth'd)
//   GET  /health    — smoke test
//
// HubSpot prerequisites:
//   Create a Custom Behavioral Event with internal name matching
//   env.HUBSPOT_EVENT_NAME (default "pe_website_visit") with these properties:
//     hs_page_url, hs_page_path, hs_referrer, hs_ip_address,
//     hs_session_id, hs_visitor_id, hs_user_agent
//   Requires Marketing Hub Enterprise or Operations Hub. If your portal
//   doesn't have that tier, mirror calls 403 — that's fine, KV is canonical.
// =============================================================================

// Catches:
//   - Classic search/social bots: Googlebot, Bingbot, Yandex, DuckDuckBot,
//     facebookexternalhit, LinkedInBot, Slackbot, Twitterbot, etc. (via `bot`)
//   - AI training crawlers that DON'T contain "bot": Google-Extended,
//     Anthropic-AI, meta-externalagent
//   - Uptime / performance monitoring: Pingdom, StatusCake, Datadog,
//     NewRelic, Site24x7, Lighthouse, PageSpeed
//   - Raw HTTP libraries used by scripts: Java, Go-http-client, okhttp,
//     libwww-perl, Apache-HttpClient, httpx, aiohttp, scrapy
//   - Headless browsers: HeadlessChrome (via `headless`), playwright,
//     puppeteer, selenium, phantomjs
const BOT_REGEX = /bot|crawler|spider|crawling|headless|slurp|bingpreview|facebookexternalhit|preview|monitoring|http-client|curl|wget|python-requests|axios|node-fetch|Google-Extended|Anthropic-AI|meta-externalagent|Lighthouse|PageSpeed|Pingdom|StatusCake|Datadog|NewRelic|Site24x7|Java\/|Go-http-client|okhttp|libwww-perl|Apache-HttpClient|httpx|aiohttp|scrapy|playwright|puppeteer|selenium|phantomjs/i;

// -----------------------------------------------------------------------------
// Worker-side ineligibility filters (Step B)
// Use free signals from request.cf to drop datacenter / hosting / VPN / proxy
// traffic and verified bots BEFORE they ever land in KV. Catches the bulk of
// the ~80% of visits PDL would later flag as "ineligible" — saving KV writes
// and downstream PDL credits.
// -----------------------------------------------------------------------------

// AS organization names that are almost certainly not buyers.
// Targeted, not generic — e.g. matches "Amazon Technologies Inc." but not the
// word "hosting" alone (some legit corporate networks contain it).
const DATACENTER_AS_PATTERN = /\b(?:amazon\b|aws\b|google llc|google inc|microsoft corporation|azure|oracle corporation|alibaba|tencent cloud|hetzner|ovh|digitalocean|linode|akamai|fastly|cloudflare,? inc|leaseweb|m247|psychz|choopa|vultr|contabo|netactuate|colocrossing|datacamp|datapacket|quadranet|g-?core labs|stackpath|cdn77|fly\.io|vercel|netlify|render\b|heroku|nordvpn|expressvpn|surfshark|protonvpn|mullvad|tunnelbear|pia|tor exit|tor anonymity|relay\b|psiphon|hidemyass|opera vpn)\b/i;

// Cloudflare's threat score is 0-100. Anything above this is dropped.
// 20 is conservative; bump to 10 once we see real-world false-positive rate.
const MAX_THREAT_SCORE = 20;

// Per-IP value caps to keep KV values manageable
const MAX_PAGES_VISITED = 500;
const MAX_UNIQUE_PAGES = 200;
const MAX_USER_AGENTS = 10;
const MAX_SESSION_IDS = 200;
const MAX_VISITOR_IDS = 50;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return corsResponse(env, null, 204);
    }

    try {
      if (url.pathname === "/collect" && request.method === "POST") {
        return await handleCollect(request, env, ctx);
      }
      if (url.pathname === "/export" && request.method === "GET") {
        return await handleExport(request, env, url);
      }
      if (url.pathname === "/health" && request.method === "GET") {
        return jsonResponse({ ok: true, ts: new Date().toISOString() });
      }
      return jsonResponse({ error: "not_found" }, 404);
    } catch (err) {
      console.error("worker error:", err && err.stack || err);
      return jsonResponse({ error: "internal", message: String(err) }, 500);
    }
  },
};

// -----------------------------------------------------------------------------
// /collect — capture a single visit
// -----------------------------------------------------------------------------
async function handleCollect(request, env, ctx) {
  const ip = request.headers.get("CF-Connecting-IP");
  if (!ip) {
    return corsResponse(env, { error: "no_ip" }, 400);
  }

  // Filter private / reserved IPs early
  if (isPrivateIp(ip)) {
    return corsResponse(env, { ignored: "private_ip" }, 200);
  }

  // --- Cloudflare-signal drops ---------------------------------------------
  // These run BEFORE the JSON body is parsed because the cf object is free
  // and present on every request — no need to look at payload.
  const cf = request.cf || {};

  const asOrg = (cf.asOrganization || "").toString();
  if (asOrg && DATACENTER_AS_PATTERN.test(asOrg)) {
    console.log(`drop datacenter_asn ip=${ip} as="${asOrg}"`);
    return corsResponse(
      env, { ignored: "datacenter_asn", as: asOrg }, 200,
    );
  }

  const threatScore = Number.isFinite(cf.threatScore) ? cf.threatScore : 0;
  if (threatScore > MAX_THREAT_SCORE) {
    console.log(`drop high_threat_score ip=${ip} score=${threatScore}`);
    return corsResponse(
      env, { ignored: "high_threat_score", score: threatScore }, 200,
    );
  }

  // Cloudflare's verified-bot flag (free) catches Googlebot, Bingbot, etc.
  // even when their UA doesn't match BOT_REGEX (e.g. spoofed Chrome UA).
  if (cf.botManagement && cf.botManagement.verifiedBot) {
    console.log(`drop verified_bot ip=${ip} as="${asOrg}"`);
    return corsResponse(env, { ignored: "verified_bot" }, 200);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return corsResponse(env, { error: "invalid_json" }, 400);
  }

  const pageUrl = typeof body.url === "string" ? body.url : null;
  if (!pageUrl) {
    return corsResponse(env, { error: "missing_url" }, 400);
  }

  const pagePath = typeof body.path === "string" ? body.path : safePath(pageUrl);
  const referrer = typeof body.referrer === "string" ? body.referrer : "";
  const sessionId = typeof body.session_id === "string" ? body.session_id : "";
  const visitorId = typeof body.visitor_id === "string" ? body.visitor_id : "";
  const userAgent = typeof body.user_agent === "string"
    ? body.user_agent
    : (request.headers.get("User-Agent") || "");
  const ts = isIsoTimestamp(body.timestamp)
    ? body.timestamp
    : new Date().toISOString();

  // Drop obvious bots based on UA
  if (BOT_REGEX.test(userAgent)) {
    return corsResponse(env, { ignored: "bot" }, 200);
  }

  // Read-modify-write the per-IP record
  const key = `ip:${ip}`;
  const existing = await env.IP_VISITS.get(key, { type: "json" });

  const record = existing || {
    ip,
    first_seen: ts,
    last_seen: ts,
    visit_count: 0,
    session_count: 0,
    pages_visited: [],
    unique_pages: [],
    session_ids: [],
    visitor_ids: [],
    user_agents: [],
  };

  // Update timestamps
  if (ts < record.first_seen) record.first_seen = ts;
  if (ts > record.last_seen) record.last_seen = ts;

  // Visit counter
  record.visit_count = (record.visit_count || 0) + 1;

  // Session tracking
  if (sessionId && !record.session_ids.includes(sessionId)) {
    record.session_ids.push(sessionId);
    record.session_count = (record.session_count || 0) + 1;
    if (record.session_ids.length > MAX_SESSION_IDS) {
      record.session_ids = record.session_ids.slice(-MAX_SESSION_IDS);
    }
  }

  // Visitor tracking
  if (visitorId && !record.visitor_ids.includes(visitorId)) {
    record.visitor_ids.push(visitorId);
    if (record.visitor_ids.length > MAX_VISITOR_IDS) {
      record.visitor_ids = record.visitor_ids.slice(-MAX_VISITOR_IDS);
    }
  }

  // Page visit log
  record.pages_visited.push({
    url: pageUrl,
    path: pagePath,
    timestamp: ts,
    referrer: referrer,
  });
  if (record.pages_visited.length > MAX_PAGES_VISITED) {
    record.pages_visited = record.pages_visited.slice(-MAX_PAGES_VISITED);
  }

  // Unique path set
  if (pagePath && !record.unique_pages.includes(pagePath)) {
    record.unique_pages.push(pagePath);
    if (record.unique_pages.length > MAX_UNIQUE_PAGES) {
      record.unique_pages = record.unique_pages.slice(-MAX_UNIQUE_PAGES);
    }
  }

  // User-agent set
  if (userAgent && !record.user_agents.includes(userAgent)) {
    record.user_agents.push(userAgent);
    if (record.user_agents.length > MAX_USER_AGENTS) {
      record.user_agents = record.user_agents.slice(-MAX_USER_AGENTS);
    }
  }

  await env.IP_VISITS.put(key, JSON.stringify(record));

  // Fire-and-forget mirror to HubSpot
  if (env.HUBSPOT_API_KEY) {
    ctx.waitUntil(
      sendHubSpotEvent(env, {
        ts,
        ip,
        pageUrl,
        pagePath,
        referrer,
        sessionId,
        visitorId,
        userAgent,
      }).catch((e) => console.warn("hubspot mirror failed:", String(e)))
    );
  }

  return corsResponse(env, { ok: true }, 200);
}

// -----------------------------------------------------------------------------
// /export — paginated dump of stored visits, bearer-authenticated
// -----------------------------------------------------------------------------
async function handleExport(request, env, url) {
  const auth = request.headers.get("Authorization") || "";
  const expected = `Bearer ${env.ADMIN_TOKEN || ""}`;
  if (!env.ADMIN_TOKEN || auth !== expected) {
    return jsonResponse({ error: "unauthorized" }, 401);
  }

  const sinceParam = url.searchParams.get("since"); // ISO timestamp
  // Cloudflare KV `list` caps at 1000 keys per call; clamp here so callers
  // passing higher values get a clean paginated response instead of a 500.
  const limit = clampInt(url.searchParams.get("limit"), 1000, 1, 1000);
  const cursor = url.searchParams.get("cursor") || undefined;

  const listResult = await env.IP_VISITS.list({
    prefix: "ip:",
    limit,
    cursor,
  });

  // Fan out the KV reads in parallel. Cloudflare's runtime is happy to
  // multiplex these — running them sequentially on a 1000-key page takes
  // ~30s of wallclock; parallel collapses to ~1-2s.
  const values = await Promise.all(
    listResult.keys.map((k) => env.IP_VISITS.get(k.name, { type: "json" }))
  );

  const ips = {};
  for (const val of values) {
    if (!val) continue;
    if (sinceParam && val.last_seen && val.last_seen < sinceParam) continue;
    ips[val.ip] = val;
  }

  return jsonResponse({
    ips,
    cursor: listResult.list_complete ? null : listResult.cursor,
    complete: !!listResult.list_complete,
  });
}

// -----------------------------------------------------------------------------
// HubSpot Custom Behavioral Events mirror
// -----------------------------------------------------------------------------
async function sendHubSpotEvent(env, v) {
  // The `utk` field on send is HubSpot's user token cookie. If our visitorId
  // looks like a hubspotutk (32-char hex), pass it; otherwise omit.
  const looksLikeUtk = /^[a-f0-9]{32}$/i.test(v.visitorId);

  const payload = {
    eventName: env.HUBSPOT_EVENT_NAME || "pe_website_visit",
    occurredAt: v.ts,
    properties: {
      hs_page_url: v.pageUrl,
      hs_page_path: v.pagePath,
      hs_referrer: v.referrer,
      hs_ip_address: v.ip,
      hs_session_id: v.sessionId,
      hs_visitor_id: v.visitorId,
      hs_user_agent: v.userAgent,
    },
  };
  if (looksLikeUtk) payload.utk = v.visitorId;

  const res = await fetch("https://api.hubapi.com/events/v3/send", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.HUBSPOT_API_KEY}`,
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    console.warn(`hubspot send ${res.status}: ${text.slice(0, 300)}`);
  }
}

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------
function jsonResponse(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}

function corsResponse(env, obj, status = 200) {
  const origin = env.ALLOWED_ORIGIN || "*";
  const headers = {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
  if (obj === null) {
    return new Response(null, { status, headers });
  }
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function isIsoTimestamp(s) {
  if (typeof s !== "string") return false;
  return !isNaN(Date.parse(s));
}

function safePath(u) {
  try {
    return new URL(u).pathname || "/";
  } catch {
    return "/";
  }
}

function clampInt(s, dflt, min, max) {
  const n = parseInt(s, 10);
  if (isNaN(n)) return dflt;
  return Math.max(min, Math.min(max, n));
}

function isPrivateIp(ip) {
  // IPv4
  if (/^10\./.test(ip)) return true;
  if (/^127\./.test(ip)) return true;
  if (/^192\.168\./.test(ip)) return true;
  const m = ip.match(/^172\.(\d+)\./);
  if (m) {
    const n = parseInt(m[1], 10);
    if (n >= 16 && n <= 31) return true;
  }
  if (/^169\.254\./.test(ip)) return true; // link-local
  if (/^0\./.test(ip)) return true;
  // IPv6
  if (ip === "::1") return true;
  if (/^fc/i.test(ip) || /^fd/i.test(ip)) return true; // unique-local fc00::/7
  if (/^fe80/i.test(ip)) return true;
  return false;
}
