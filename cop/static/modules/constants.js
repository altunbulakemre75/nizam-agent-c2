/* ==========================================================
   cop/static/modules/constants.js — Shared UI constants

   Classic script loaded before app.js. Exposes window.NIZAM.constants
   with colour maps, label dictionaries, and icon metadata the UI uses.

   All values are byte-identical to the previous inline definitions in
   app.js — the refactor rule for this module is "verbatim copy, no
   behaviour change". When a colour or label is edited, change it here,
   not in app.js.
   ========================================================== */

(function () {
  "use strict";

  window.NIZAM = window.NIZAM || {};

  /* ── Threat classification ──────────────────────────────── */
  const THREAT_COLORS = { HIGH: "#e74c3c", MEDIUM: "#f39c12", LOW: "#27ae60" };

  /* ── Intent classification ──────────────────────────────── */
  const INTENT_META = {
    attack:         { color: "#e74c3c", icon: "!", label: "ATTACK" },
    reconnaissance: { color: "#9b59b6", icon: "@", label: "RECON" },
    loitering:      { color: "#e67e22", icon: "O", label: "LOITER" },
    unknown:        { color: "#95a5a6", icon: "?", label: "UNKNOWN" },
  };

  /* ── Rules of Engagement ────────────────────────────────── */
  const ROE_COLORS = {
    WEAPONS_FREE:  "#e74c3c",
    WEAPONS_TIGHT: "#e67e22",
    WEAPONS_HOLD:  "#f39c12",
    WARN:          "#9b59b6",
    TRACK_ONLY:    "#3498db",
    HOLD_FIRE:     "#27ae60",
  };

  const ROE_LABELS = {
    WEAPONS_FREE:  "SERBEST",
    WEAPONS_TIGHT: "KOSITLI",
    WEAPONS_HOLD:  "SAVUNMA",
    WARN:          "UYAR",
    TRACK_ONLY:    "IZLE",
    HOLD_FIRE:     "ATES ETME",
  };

  /* ── Confidence grade ───────────────────────────────────── */
  const CONF_GRADE_COLORS = { HIGH: "#27ae60", MEDIUM: "#f39c12", LOW: "#e74c3c" };

  /* ── Tactical recommendation action colours ─────────────── */
  const ACTION_COLORS = {
    ENGAGE:      "var(--danger)",
    OBSERVE:     "var(--warn)",
    EVADE:       "var(--accent)",
    JAM:         "#f1c40f",
    SPOOF:       "#1abc9c",
    EW_SUPPRESS: "#9b59b6",
  };

  const ACTION_BG = {
    ENGAGE:      "rgba(240,64,64,0.1)",
    OBSERVE:     "rgba(240,128,48,0.1)",
    EVADE:       "rgba(79,127,255,0.1)",
    JAM:         "rgba(241,196,15,0.1)",
    SPOOF:       "rgba(26,188,156,0.1)",
    EW_SUPPRESS: "rgba(155,89,182,0.1)",
  };

  /* ── Effector status / engagement outcome ───────────────── */
  const STATUS_COLORS = {
    READY:    "#27ae60",
    ENGAGED:  "#f39c12",
    COOLDOWN: "#e67e22",
    OFFLINE:  "#7f8c8d",
  };

  const OUTCOME_COLORS = {
    hit:        "#27ae60",
    miss:       "#e74c3c",
    partial:    "#f39c12",
    suppressed: "#9b59b6",
  };

  /* ── Anomaly severity ───────────────────────────────────── */
  const ANOMALY_COLORS = {
    CRITICAL: "#e74c3c",
    HIGH:     "#e67e22",
    MEDIUM:   "#f1c40f",
    LOW:      "#95a5a6",
  };

  /* ── Multi-operator colour palette ──────────────────────── */
  const OP_COLORS = [
    "#4fc3f7", "#a5d6a7", "#ffcc80", "#ef9a9a",
    "#ce93d8", "#80cbc4", "#fff176",
  ];

  /* ── Altitude colour mode (ALT button) ──────────────────── */
  /**
   * Returns a colour based on altitude in metres. Verbatim copy of the
   * original _altColor() function — same breakpoints, same colours.
   */
  function altitudeColor(altM) {
    const a = Math.max(0, Number(altM) || 0);
    if (a < 300)  return "#27ae60"; // green  – low
    if (a < 1500) return "#f1c40f"; // yellow – medium
    if (a < 3000) return "#e67e22"; // orange – high
    if (a < 6000) return "#e74c3c"; // red    – very high
    return "#9b59b6";               // purple – extreme
  }

  window.NIZAM.constants = {
    THREAT_COLORS,
    INTENT_META,
    ROE_COLORS,
    ROE_LABELS,
    CONF_GRADE_COLORS,
    ACTION_COLORS,
    ACTION_BG,
    STATUS_COLORS,
    OUTCOME_COLORS,
    ANOMALY_COLORS,
    OP_COLORS,
    altitudeColor,
  };
})();
