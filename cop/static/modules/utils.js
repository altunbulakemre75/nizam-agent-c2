/* ==========================================================
   cop/static/modules/utils.js — Pure DOM / string / format helpers

   Classic script (not an ES module) so the existing app.js — which is
   loaded as a classic script and uses inline onclick handlers in
   dynamically-generated HTML — can keep working without touching the
   HTML template.

   All helpers are attached to a shared window.NIZAM.utils namespace.
   Extracted verbatim from the monolithic app.js so behaviour is
   bit-identical. No features. No side effects at load time.
   ========================================================== */

(function () {
  "use strict";

  window.NIZAM = window.NIZAM || {};

  const utils = {
    $(sel) {
      return document.querySelector(sel);
    },

    el(tag, attrs = {}, children = []) {
      const n = document.createElement(tag);
      Object.entries(attrs).forEach(([k, v]) => {
        if (k === "style") Object.assign(n.style, v);
        else if (k.startsWith("on") && typeof v === "function") {
          n.addEventListener(k.slice(2), v);
        } else if (v !== undefined && v !== null) {
          n.setAttribute(k, String(v));
        }
      });
      children.forEach(c => {
        n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
      return n;
    },

    safeJsonParse(s) {
      try { return JSON.parse(s); } catch { return null; }
    },

    /** Entity-escape for string injection into innerHTML as text. */
    escHtml(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    },

    /** DOM-based escape — slightly more defensive than escHtml. */
    escText(s) {
      const d = document.createElement("div");
      d.textContent = s == null ? "" : s;
      return d.innerHTML;
    },

    /* ── Number / time formatters (used by metrics and panels) ──── */

    fmtMs(v) { return v == null ? "—" : (+v).toFixed(0) + " ms"; },
    fmtNum(v) { return v == null ? "—" : (+v).toLocaleString(); },

    fmtTime(s) {
      const n = Math.max(0, Math.floor(s || 0));
      const m = Math.floor(n / 60);
      const r = n % 60;
      return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
    },
  };

  window.NIZAM.utils = utils;
})();
