/* =============================================================================
 * Website Deanonymization — tracking snippet
 * =============================================================================
 *
 * Usage on your site (e.g. in your <head> or right before </body>):
 *
 *   <script>
 *     window.DEANON_CONFIG = {
 *       endpoint: "https://<your-worker>.workers.dev/collect"
 *     };
 *   </script>
 *   <script src="/tracking.js" defer></script>
 *
 * Privacy:
 *   - Honors navigator.doNotTrack === "1" and navigator.globalPrivacyControl
 *   - Stores a random visitor UUID in localStorage ("deanon_vid")
 *   - Stores a session UUID in sessionStorage ("deanon_sid")
 *   - If a HubSpot `hubspotutk` cookie exists, uses it as the visitor_id so
 *     records stay aligned with HubSpot's UTK
 *   - Make sure your site's privacy policy mentions IP + identifier capture
 * ============================================================================= */
(function () {
  try {
    var cfg = window.DEANON_CONFIG || {};
    var endpoint = cfg.endpoint;
    if (!endpoint) return;

    // Respect Do Not Track / Global Privacy Control
    if (
      navigator.doNotTrack === "1" ||
      window.doNotTrack === "1" ||
      navigator.msDoNotTrack === "1" ||
      navigator.globalPrivacyControl === true
    ) {
      return;
    }

    function uuidv4() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      // RFC4122 v4 fallback
      var b = new Uint8Array(16);
      (window.crypto || window.msCrypto).getRandomValues(b);
      b[6] = (b[6] & 0x0f) | 0x40;
      b[8] = (b[8] & 0x3f) | 0x80;
      var h = [];
      for (var i = 0; i < 16; i++) h.push(b[i].toString(16).padStart(2, "0"));
      return (
        h.slice(0, 4).join("") + "-" +
        h.slice(4, 6).join("") + "-" +
        h.slice(6, 8).join("") + "-" +
        h.slice(8, 10).join("") + "-" +
        h.slice(10, 16).join("")
      );
    }

    function readCookie(name) {
      try {
        var m = document.cookie.match(
          new RegExp("(?:^|; )" + name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1") + "=([^;]*)")
        );
        return m ? decodeURIComponent(m[1]) : null;
      } catch (e) {
        return null;
      }
    }

    // Visitor ID: prefer hubspotutk cookie, else persistent localStorage UUID
    var visitorId = readCookie("hubspotutk");
    if (!visitorId) {
      try {
        visitorId = localStorage.getItem("deanon_vid");
        if (!visitorId) {
          visitorId = uuidv4();
          localStorage.setItem("deanon_vid", visitorId);
        }
      } catch (e) {
        visitorId = uuidv4();
      }
    }

    // Session ID: random UUID for this tab/session
    var sessionId;
    try {
      sessionId = sessionStorage.getItem("deanon_sid");
      if (!sessionId) {
        sessionId = uuidv4();
        sessionStorage.setItem("deanon_sid", sessionId);
      }
    } catch (e) {
      sessionId = uuidv4();
    }

    function send() {
      var payload = {
        url: window.location.href,
        path: window.location.pathname,
        referrer: document.referrer || "",
        session_id: sessionId,
        visitor_id: visitorId,
        timestamp: new Date().toISOString(),
        user_agent: navigator.userAgent || "",
      };

      // Prefer fetch with keepalive (works during unload too)
      try {
        if (window.fetch) {
          fetch(endpoint, {
            method: "POST",
            mode: "cors",
            credentials: "omit",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            keepalive: true,
          }).catch(function () {});
          return;
        }
      } catch (e) {}

      // Fallback: sendBeacon
      try {
        if (navigator.sendBeacon) {
          var blob = new Blob([JSON.stringify(payload)], {
            type: "application/json",
          });
          navigator.sendBeacon(endpoint, blob);
        }
      } catch (e) {}
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", send, { once: true });
    } else {
      send();
    }
  } catch (e) {
    // Tracking failures must never break the host page.
  }
})();
