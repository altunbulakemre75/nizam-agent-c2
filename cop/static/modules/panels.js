/* ==========================================================
   cop/static/modules/panels.js — Pure HTML builders for AI/tactical panels.

   Classic script loaded after constants.js and utils.js.
   Exposes window.NIZAM.panels with one builder per data panel.

   Each builder is a pure function: (data) → HTML string (or small result
   object). No DOM reads or writes, no Leaflet, no module-level state.
   The thin render* wrappers in app.js own the panel element refs and any
   flash/side-effect logic; they call these builders for the inner HTML.

   Dependency map (all resolved from prior modules):
     constants → THREAT_COLORS, ROE_COLORS, ROE_LABELS, CONF_GRADE_COLORS
     utils     → fmtMs, fmtNum, escHtml
   ========================================================== */

(function () {
  "use strict";

  window.NIZAM = window.NIZAM || {};

  /* ── Local constants (not shared elsewhere) ─────────────── */

  /** Icon glyphs for each tactical recommendation type. */
  const TAC_ICONS = {
    INTERCEPT:    "\u2694",   // ⚔  crossed swords
    ZONE_WARNING: "\u26A0",   // ⚠  warning
    ESCALATE:     "\u2B06",   // ⬆  up arrow
    WITHDRAW:     "\u21A9",   // ↩  return arrow
    MONITOR:      "\u{1F441}", // 👁  eye
    REPOSITION:   "\u27A1",   // ➡  right arrow
  };

  /** Colour per tactical recommendation type. */
  const TAC_COLORS = {
    INTERCEPT:    "#e74c3c",
    ZONE_WARNING: "#f39c12",
    ESCALATE:     "#e74c3c",
    WITHDRAW:     "#3498db",
    MONITOR:      "#9b59b6",
    REPOSITION:   "#1abc9c",
  };

  /* ── Internal helpers ───────────────────────────────────── */

  /** Return a CSS colour string for a latency value given warn/crit thresholds. */
  function _barColor(val, warn, crit) {
    if (val >= crit) return "var(--danger)";
    if (val >= warn) return "var(--warn)";
    return "var(--ok)";
  }

  function _k()  { return window.NIZAM.constants; }
  function _u()  { return window.NIZAM.utils; }

  /* ── Panel HTML builders ────────────────────────────────── */

  /**
   * buildBreachHtml(breaches)
   * @param {Array} breaches - array of predicted breach objects
   * @returns {string} innerHTML for the breach panel card
   */
  function buildBreachHtml(breaches) {
    if (!breaches || breaches.length === 0) {
      return "<b>\u26A0 Predictive Breach</b><br><span style='opacity:.5'>No predicted breaches</span>";
    }
    let html = `<b>\u26A0 Predictive Breach</b> <span style="opacity:.6">${breaches.length} warning(s)</span><br>`;
    breaches.slice(0, 6).forEach(b => {
      const sevColor   = b.severity === "CRITICAL" ? "#e74c3c" : "#f39c12";
      const confBadge  = b.confidence === "HIGH"
        ? '<span style="background:#e74c3c;padding:0 4px;border-radius:3px;font-size:8px">CERTAIN</span>'
        : '<span style="background:#f39c12;padding:0 4px;border-radius:3px;font-size:8px">PROBABLE</span>';
      html += `<div style="border-left:3px solid ${sevColor};padding-left:5px;margin:3px 0">
        <span style="color:${sevColor};font-weight:bold">${b.track_id}</span>
        \u2192 ${b.zone_name} ${confBadge}<br>
        <span style="font-size:9px;opacity:.8">\u23F1 ${b.time_to_breach_s}s | ${b.current_distance_m}m | ${b.zone_type}</span>
      </div>`;
    });
    return html;
  }

  /**
   * buildMLHtml(preds, modelAvailable)
   * @param {Object} preds - {track_id: {ml_level, ml_probability, ...}}
   * @param {boolean} modelAvailable
   * @returns {string}
   */
  function buildMLHtml(preds, modelAvailable) {
    const THREAT_COLORS = _k().THREAT_COLORS;
    const entries = Object.entries(preds || {});
    if (entries.length === 0) {
      return '<b style="color:#818cf8">ML Model</b><br><span style="opacity:.5">' +
        (modelAvailable ? 'Veri bekleniyor...' : 'Model yok \u2014 egitim gerekli') + '</span>';
    }
    const sorted = entries
      .map(([tid, p]) => ({ tid, ...p }))
      .sort((a, b) => (b.ml_probability || 0) - (a.ml_probability || 0))
      .slice(0, 8);

    let html = `<b style="color:#818cf8">\u2699 ML Threat</b> <span style="opacity:.5">(${entries.length} track)</span><br>`;
    sorted.forEach(p => {
      const c    = THREAT_COLORS[p.ml_level] || "#6366f1";
      const pct  = p.ml_probability != null ? (p.ml_probability * 100).toFixed(0) : "?";
      const barW = Math.round((p.ml_probability || 0) * 60);
      html += `<div style="margin:2px 0;display:flex;align-items:center;gap:4px">
        <span style="min-width:75px;font-family:monospace;font-size:10px">${p.tid.slice(-10)}</span>
        <span style="background:${c};color:#fff;border-radius:3px;padding:0 4px;font-size:9px;font-weight:bold">${p.ml_level}</span>
        <div style="width:60px;height:6px;background:rgba(255,255,255,0.1);border-radius:3px;overflow:hidden">
          <div style="width:${barW}px;height:100%;background:${c};border-radius:3px"></div>
        </div>
        <span style="font-size:10px;opacity:.7">${pct}%</span>
      </div>`;
    });
    return html;
  }

  /**
   * buildROEHtml(advisories)
   * @param {Array} advisories
   * @returns {{ html: string, hasCritical: boolean }}
   */
  function buildROEHtml(advisories) {
    const { ROE_COLORS, ROE_LABELS } = _k();
    if (!advisories || advisories.length === 0) {
      return {
        html: "<b>\u2694 ROE Advisory</b><br><span style='opacity:.5'>No engagements</span>",
        hasCritical: false,
      };
    }
    let html = `<b>\u2694 ROE Advisory</b> <span style="opacity:.6">${advisories.length} active</span><br>`;
    advisories.slice(0, 8).forEach(a => {
      const color     = ROE_COLORS[a.engagement] || "#aaa";
      const label     = ROE_LABELS[a.engagement] || a.engagement;
      const urgColor  = { CRITICAL:"#e74c3c", HIGH:"#e67e22", MEDIUM:"#f39c12", LOW:"#95a5a6" }[a.urgency] || "#aaa";

      const badges = [];
      if (a.is_coordinated) badges.push('<span style="background:#ff0050;padding:0 3px;border-radius:3px;font-size:7px">KOORD</span>');
      if (a.in_kill_zone)   badges.push('<span style="background:#e74c3c;padding:0 3px;border-radius:3px;font-size:7px">KILL</span>');

      const confPct   = a.confidence != null ? a.confidence : null;
      const confC     = confPct == null ? "#888" : confPct >= 70 ? "#27ae60" : confPct >= 40 ? "#f39c12" : "#e74c3c";
      const confBadge = confPct != null
        ? `<span style="color:${confC};font-size:9px;margin-left:4px">${confPct}%</span>`
        : "";

      html += `<div style="border-left:3px solid ${color};padding-left:5px;margin:3px 0">
        <span style="background:${color};padding:1px 5px;border-radius:3px;font-size:9px;font-weight:bold;color:#fff">${label}</span>
        <span style="font-weight:bold;margin-left:3px">${a.track_id}</span>
        ${badges.join(" ")}${confBadge}
        <span style="float:right;color:${urgColor};font-size:9px;font-weight:bold">${a.urgency}</span><br>
        <span style="font-size:9px;opacity:.75">${(a.reasons || []).join("; ")}</span>
      </div>`;
    });
    return { html, hasCritical: advisories.some(a => a.urgency === "CRITICAL") };
  }

  /**
   * buildConfidenceHtml(scores)
   * @param {Object} scores - {track_id: {confidence, grade}}
   * @returns {string}
   */
  function buildConfidenceHtml(scores) {
    const CONF_GRADE_COLORS = _k().CONF_GRADE_COLORS;
    const entries = Object.entries(scores || {});
    if (entries.length === 0) {
      return '<b style="color:#4fc3f7">\u{1F4CA} Guven Skoru</b><br><span style="opacity:.5">Tehdit yok</span>';
    }
    const sorted = entries
      .map(([tid, s]) => ({ tid, confidence: s.confidence ?? 0, grade: s.grade || "LOW" }))
      .sort((a, b) => b.confidence - a.confidence)
      .slice(0, 8);

    let html = `<b style="color:#4fc3f7">\u{1F4CA} Guven Skoru</b> <span style="opacity:.5">(${entries.length} tehdit)</span><br>`;
    sorted.forEach(s => {
      const c    = CONF_GRADE_COLORS[s.grade] || "#aaa";
      const barW = Math.round(s.confidence * 0.8);
      html += `<div style="margin:2px 0;display:flex;align-items:center;gap:4px">
        <span style="min-width:72px;font-family:monospace;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.tid.slice(-10)}</span>
        <span style="background:${c};color:#fff;border-radius:3px;padding:0 4px;font-size:8px;font-weight:bold;min-width:40px;text-align:center">${s.grade}</span>
        <div style="width:80px;height:6px;background:rgba(255,255,255,0.1);border-radius:3px;overflow:hidden;flex-shrink:0">
          <div style="width:${barW}px;height:100%;background:${c};border-radius:3px"></div>
        </div>
        <span style="font-size:10px;opacity:.8;min-width:28px;text-align:right">${s.confidence}%</span>
      </div>`;
    });
    return html;
  }

  /**
   * buildTacticalHtml(recs)
   * @param {Array} recs - tactical recommendation objects
   * @returns {string}
   */
  function buildTacticalHtml(recs) {
    if (!recs || recs.length === 0) {
      return "<b>AI Tactical</b><br><span style='opacity:.5'>No recommendations</span>";
    }
    let html = `<b>AI Tactical</b> <span style="opacity:.6">${recs.length} active</span><br>`;
    recs.slice(0, 8).forEach(r => {
      const icon  = TAC_ICONS[r.type]  || "\u2022";
      const color = TAC_COLORS[r.type] || "#aaa";
      html += `<div style="border-left:3px solid ${color};padding-left:5px;margin:3px 0">
        <span style="font-size:12px">${icon}</span>
        <span style="color:${color};font-weight:bold"> ${r.type}</span>
        <span style="opacity:.6"> P${r.priority}</span><br>
        <span style="font-size:9px">${r.message || ""}</span>
      </div>`;
    });
    return html;
  }

  /**
   * buildMetricsHtml(m)
   * @param {Object} m - server metrics payload from /api/metrics
   * @returns {string}
   */
  function buildMetricsHtml(m) {
    const { fmtMs, fmtNum } = _u();
    const ing   = m.ingest    || {};
    const tac   = m.tactical  || {};
    const ws    = m.websocket || {};
    const st    = m.state     || {};
    const upMin = ((m.uptime_s || 0) / 60).toFixed(1);
    const rps   = (+ing.per_sec || 0).toFixed(1);
    const p50c  = _barColor(+tac.p50_ms || 0, 500,  2000);
    const p95c  = _barColor(+tac.p95_ms || 0, 500,  2000);
    const p99c  = _barColor(+tac.p99_ms || 0, 1000, 3000);

    const row = (label, val, cls = "") =>
      `<div class="nz-row"><span class="label">${label}</span><span class="val ${cls}">${val}</span></div>`;

    const bar = (label, val, max, color) => {
      const pct = Math.min(100, Math.round(((val || 0) / max) * 100));
      return `<div class="nz-bar-wrap">
        <span class="nz-bar-label">${label}</span>
        <div class="nz-bar-track"><div class="nz-bar-fill" style="width:${pct}%;background:${color}"></div></div>
        <span class="nz-bar-val">${fmtMs(val)}</span>
      </div>`;
    };

    const typesHtml = Object.entries(ing.by_type || {})
      .map(([k, v]) => row(k, fmtNum(v))).join("") || row("\u2013", "\u2013");

    return `
      <div class="nz-section">Server Metrics <span style="float:right;font-weight:400">${upMin}m up</span></div>

      <div class="nz-card" style="margin-bottom:5px">
        <div class="nz-section" style="margin-bottom:4px">Ingest</div>
        ${row("total",      fmtNum(ing.total),        "c-accent")}
        ${row("per second", rps,                       "c-ok")}
        ${row("bad",        fmtNum(ing.bad_request))}
        <div style="margin-top:4px;border-top:1px solid var(--border);padding-top:4px">${typesHtml}</div>
      </div>

      <div class="nz-card" style="margin-bottom:5px">
        <div class="nz-section" style="margin-bottom:4px">Tactical Engine</div>
        ${bar("p50", tac.p50_ms, 3000, p50c)}
        ${bar("p95", tac.p95_ms, 3000, p95c)}
        ${bar("p99", tac.p99_ms, 3000, p99c)}
        ${row("ran / sched", `${fmtNum(tac.ran)} / ${fmtNum(tac.scheduled)}`)}
        ${row("last / max",  `${fmtMs(tac.last_ms)} / ${fmtMs(tac.max_ms)}`)}
        ${Object.keys(tac.module_ms || {}).length > 0
          ? `<div style="margin-top:4px;border-top:1px solid var(--border);padding-top:4px">
               <div style="font-size:9px;opacity:.6;margin-bottom:2px">Modul zamanlamasi</div>
               ${Object.entries(tac.module_ms || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => row(k, fmtMs(v))).join("")}
             </div>`
          : ""}
      </div>

      <div class="nz-card" style="margin-bottom:5px">
        <div class="nz-section" style="margin-bottom:4px">WebSocket</div>
        ${row("clients",     fmtNum(ws.clients),        "c-accent")}
        ${row("broadcasts",  fmtNum(ws.broadcasts))}
        ${row("sent / fail", `${fmtNum(ws.messages_sent)} / ${fmtNum(ws.send_failures)}`)}
      </div>

      <div class="nz-card">
        <div class="nz-section" style="margin-bottom:4px">State</div>
        ${row("tracks",               fmtNum(st.tracks),  "c-accent")}
        ${row("threats",              fmtNum(st.threats),  "c-danger")}
        ${row("assets / zones / tasks", `${fmtNum(st.assets)} / ${fmtNum(st.zones)} / ${fmtNum(st.tasks)}`)}
      </div>
    `;
  }

  /* ── Export ─────────────────────────────────────────────── */
  window.NIZAM.panels = {
    buildBreachHtml,
    buildMLHtml,
    buildROEHtml,
    buildConfidenceHtml,
    buildTacticalHtml,
    buildMetricsHtml,
    /** Exposed for tests — not for production use */
    _TAC_ICONS:  TAC_ICONS,
    _TAC_COLORS: TAC_COLORS,
    _barColor,
  };
})();
