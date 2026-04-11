/* ==========================================================
   NIZAM COP — cop/static/app.js  (Phase 5 + Phase B refactor)
   Phase 1: tracks, threats, zones, zone-breach alerts
   Phase 2: trail polylines, intent badges, EO sensor, ML scoring
   Phase 3: asset management, autonomous task queue, mission waypoints
   Phase 5: AI decision support (predictions, anomalies, tactical, LLM chat)
   Phase B: incremental extraction to /static/modules/*.js
            (utils, constants, store — loaded in index.html before this file)
   ========================================================== */

/* ── Shared modules (loaded before us via index.html) ────── */
const { utils: _U, constants: _K, store: _STORE } = window.NIZAM;
const $              = _U.$;
const el             = _U.el;
const safeJsonParse  = _U.safeJsonParse;
const escHtml        = _U.escHtml;
const _esc           = _U.escText;
const _fmtMs         = _U.fmtMs;
const _fmtNum        = _U.fmtNum;
const fmtTime        = _U.fmtTime;

const THREAT_COLORS     = _K.THREAT_COLORS;
const INTENT_META       = _K.INTENT_META;
const ROE_COLORS        = _K.ROE_COLORS;
const ROE_LABELS        = _K.ROE_LABELS;
const CONF_GRADE_COLORS = _K.CONF_GRADE_COLORS;
const ACTION_COLORS     = _K.ACTION_COLORS;
const ACTION_BG         = _K.ACTION_BG;
const STATUS_COLORS     = _K.STATUS_COLORS;
const OUTCOME_COLORS    = _K.OUTCOME_COLORS;
const ANOMALY_COLORS    = _K.ANOMALY_COLORS;
const _OP_COLORS        = _K.OP_COLORS;
const _altColor         = _K.altitudeColor;

/* ── Global state ───────────────────────────────────────── */
// Multi-operator: my identity + shared state
const MY_OPERATOR_ID = (() => {
  let id = sessionStorage.getItem("nizam_operator_id");
  if (!id) {
    id = "OPS-" + Math.random().toString(36).slice(2,8).toUpperCase();
    sessionStorage.setItem("nizam_operator_id", id);
  }
  return id;
})();
let OPERATORS_STATE = {};   // {operator_id: {joined_at, claimed_tracks}}
let TRACK_CLAIMS    = {};   // {track_id: operator_id}

const UI = {
  mode: "LIVE", ws: null, wsConnected: false,
  buffer: [], bufferMax: 1000,
  tracks:  new Map(),
  threats: new Map(),
  map:     null,
  trackMarkers:   new Map(),
  trackPolylines: new Map(),
  zonePolygons:   new Map(),
  assetMarkers:   new Map(),   // Phase 3
  waypointMarkers: new Map(),  // Phase 3
  waypointLine:   null,        // L.Polyline for mission route
};

/* ── Map init ────────────────────────────────────────────── */
function initMap() {
  let d = $("#map");
  if (!d) {
    d = el("div", { id: "map", style: { position:"fixed", inset:"0", width:"100vw", height:"100vh" } });
    document.body.appendChild(d);
  } else if (getComputedStyle(d).height === "0px") {
    d.style.height = "100vh"; d.style.width = "100%";
  }
  if (typeof window.L === "undefined") throw new Error("Leaflet not loaded");
  UI.map = L.map("map", { zoomControl: true }).setView([41.015, 28.979], 10);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap"
  }).addTo(UI.map);
}

/* ── Status helpers ─────────────────────────────────────── */
let statusEl=null, bufEl=null, modeEl=null;
function setStatus(t) {
  if(statusEl) statusEl.textContent = t;
  // Sync WS dot
  const ok = t.includes("connected") || t.includes("live");
  _topbarSetWS(ok);
}
function setMode(m) {
  UI.mode = m;
  if(modeEl) modeEl.textContent = m;
  _topbarSetMode(m);
}
function setBufferSize(n) { if(bufEl) bufEl.textContent = `Buffer: ${n}`; }

/* ── Top bar ─────────────────────────────────────────────── */
const _topbar = { tracks:null, threats:null, tasks:null, ops:null, wsEl:null, modeEl:null };

function mountTopBar() {
  const bar = document.createElement("div");
  bar.id = "topbar";
  bar.innerHTML = `
    <span class="nz-logo">NIZAM</span>
    <span class="nz-logo-sub">COP</span>
    <div class="nz-topbar-divider"></div>
    <span id="tb-scenario" class="nz-topbar-scenario">STANDBY</span>
    <div class="nz-topbar-kpis">
      <div class="nz-kpi" title="Active tracks">
        <span class="nz-kpi-val c-accent" id="tb-tracks">0</span>
        <span class="nz-kpi-label">Tracks</span>
      </div>
      <div class="nz-kpi" title="Threat assessments">
        <span class="nz-kpi-val c-danger" id="tb-threats">0</span>
        <span class="nz-kpi-label">Threats</span>
      </div>
      <div class="nz-kpi" title="Pending tasks">
        <span class="nz-kpi-val c-warn" id="tb-tasks">0</span>
        <span class="nz-kpi-label">Tasks</span>
      </div>
      <div class="nz-kpi" title="Active operators">
        <span class="nz-kpi-val" id="tb-ops">1</span>
        <span class="nz-kpi-label">Operators</span>
      </div>
    </div>
    <div class="nz-topbar-right">
      <span id="tb-user-badge" style="display:none"></span>
      <button id="tts-btn" title="Toggle voice alerts (TTS)" class="nz-write-ctrl"
        style="padding:2px 8px;font-size:10px;background:rgba(100,100,100,0.2);
               border:1px solid rgba(150,150,150,0.4);border-radius:6px;color:#aaa;
               cursor:pointer;font-weight:600"
        onclick="(function(){const on=_TTS.toggle();const b=document.getElementById('tts-btn');b.style.background=on?'rgba(52,152,219,0.25)':'rgba(100,100,100,0.2)';b.style.borderColor=on?'rgba(52,152,219,0.5)':'rgba(150,150,150,0.4)';b.style.color=on?'#3498db':'#aaa';b.title=on?'Voice alerts ON — click to disable':'Voice alerts OFF — click to enable';if(on)_TTS.speak('Voice alerts enabled','low');})()">🔊</button>
      <button id="alt-btn" title="Toggle altitude colour mode" class="nz-write-ctrl"
        style="padding:2px 8px;font-size:10px;background:rgba(100,100,100,0.2);
               border:1px solid rgba(150,150,150,0.4);border-radius:6px;color:#aaa;
               cursor:pointer;font-weight:600"
        onclick="(function(){_ALT_MODE=!_ALT_MODE;const b=document.getElementById('alt-btn');b.style.background=_ALT_MODE?'rgba(241,196,15,0.25)':'rgba(100,100,100,0.2)';b.style.borderColor=_ALT_MODE?'rgba(241,196,15,0.5)':'rgba(150,150,150,0.4)';b.style.color=_ALT_MODE?'#f1c40f':'#aaa';b.title=_ALT_MODE?'Altitude mode ON — click to disable':'Altitude mode OFF — click to enable';for(const[id,t]of UI.tracks)upsertTrack(t);})()">ALT</button>
      <button id="handover-btn" title="Shift Handover Report" class="nz-write-ctrl"
        style="padding:2px 8px;font-size:10px;background:rgba(39,174,96,0.18);
               border:1px solid rgba(39,174,96,0.4);border-radius:6px;color:#2ecc71;
               cursor:pointer;font-weight:600"
        onclick="openHandoverModal()">HANDOVER</button>
      <button id="audit-panel-btn" title="Operator Audit Log"
        style="padding:2px 8px;font-size:10px;background:rgba(52,152,219,0.18);
               border:1px solid rgba(52,152,219,0.4);border-radius:6px;color:#3498db;
               cursor:pointer;font-weight:600"
        onclick="openAuditModal()">AUDIT</button>
      <button id="weather-btn" title="Weather Overlay"
        style="padding:2px 8px;font-size:10px;background:rgba(100,100,100,0.2);
               border:1px solid rgba(150,150,150,0.4);border-radius:6px;color:#aaa;
               cursor:pointer;font-weight:600"
        onclick="toggleWeatherOverlay()">WX</button>
      <button id="admin-panel-btn" title="User Management"
        style="display:none;padding:2px 8px;font-size:10px;background:rgba(231,76,60,0.2);
               border:1px solid rgba(231,76,60,0.4);border-radius:6px;color:#e74c3c;
               cursor:pointer;font-weight:600"
        onclick="openAdminPanel()">ADMIN</button>
      <span id="tb-mode" class="nz-mode-badge">LIVE</span>
      <div class="nz-ws-indicator">
        <div id="tb-ws-dot" class="nz-ws-dot off"></div>
        <span id="tb-ws-label" style="font-size:10px;color:var(--text-3)">WS</span>
      </div>
    </div>
  `;
  document.body.appendChild(bar);
  _topbar.tracks = document.getElementById("tb-tracks");
  _topbar.threats = document.getElementById("tb-threats");
  _topbar.tasks   = document.getElementById("tb-tasks");
  _topbar.ops     = document.getElementById("tb-ops");
  _topbar.wsEl    = document.getElementById("tb-ws-dot");
  _topbar.modeEl  = document.getElementById("tb-mode");
}

function updateTopBar() {
  if (_topbar.tracks)  _topbar.tracks.textContent  = UI.tracks.size;
  if (_topbar.threats) _topbar.threats.textContent = UI.threats.size;
  if (_topbar.tasks)   _topbar.tasks.textContent   = pendingTasks.size;
  if (_topbar.ops)     _topbar.ops.textContent     = Object.keys(OPERATORS_STATE).length + 1;
}

function _topbarSetWS(ok) {
  if (!_topbar.wsEl) return;
  _topbar.wsEl.className = ok ? "nz-ws-dot" : "nz-ws-dot off";
}

function _topbarSetMode(m) {
  if (!_topbar.modeEl) return;
  _topbar.modeEl.textContent = m;
  const cls = m === "LIVE" ? "nz-mode-badge"
            : m === "REPLAY" ? "nz-mode-badge replay"
            : "nz-mode-badge paused";
  _topbar.modeEl.className = cls;
}

function setTopbarScenario(name) {
  const el = document.getElementById("tb-scenario");
  if (el) el.textContent = name || "COP";
}

/* ── Control panel ─────────────────────────────────────── */
function mountControls() {
  const panel = el("div", {
    id: "left-controls",
    class: "nz-panel",
    style: {
      position: "fixed", top: "52px", left: "12px",
      zIndex: "9999", width: "210px",
    }
  });

  const header = el("div", { class: "nz-panel-header" }, ["SYSTEM"]);

  const body = el("div", { class: "nz-panel-body", style: { display:"flex", flexDirection:"column", gap:"7px" } });

  // WS status row
  const wsDot = el("div", { class: "dot off" });
  statusEl = el("span", { style: { fontSize:"11px", color:"var(--text-2)" } }, ["connecting..."]);
  const wsRow = el("div", { class: "nz-status-row" }, [wsDot, statusEl]);

  // Mode + buffer
  modeEl = el("span", { style: { fontSize:"11px", color:"var(--text-2)", fontFamily:"var(--mono)" } }, [UI.mode]);
  bufEl  = el("span", { style: { fontSize:"10px", color:"var(--text-3)", fontFamily:"var(--mono)" } }, ["buf:0"]);
  const infoRow = el("div", { class: "nz-status-row" }, [
    el("span", { style:{ color:"var(--text-3)", fontSize:"10px" }}, ["MODE "]),
    modeEl,
    el("span", { style:{ color:"var(--text-3)", marginLeft:"8px", fontSize:"10px" }}, [bufEl]),
  ]);

  // Buttons
  const btnRow = el("div", { style: { display:"flex", gap:"4px", flexWrap:"wrap" } });
  btnRow.appendChild(el("button", { class:"nz-btn", style:{flex:"1"}, onclick:()=>CopEngine.pause()    }, ["Pause"]));
  btnRow.appendChild(el("button", { class:"nz-btn c-ok", style:{flex:"1"},
    onclick:()=>CopEngine.resume(fetchCompositeSnapshot) }, ["Resume"]));
  const resetBtn = el("button", { class:"nz-btn c-danger", style:{width:"100%"},
    onclick: async()=>{ CopEngine.clearBuffer(); await hardReset(); } }, ["Clear / Reset"]);

  const logoutBtn = el("button", {
    id: "logout-btn", class: "nz-btn c-danger",
    style: { width:"100%", display:"none" },
    onclick: () => { AUTH_TOKEN=null; localStorage.removeItem("nizam_jwt"); showLoginModal(); },
  }, ["Logout"]);

  const hint = el("div", { style: { fontSize:"10px", color:"var(--text-3)", letterSpacing:"0.04em" } },
    ["P = Pause  |  R = Resume"]);

  Object.defineProperty(window, "_leftWsDot", { value: wsDot, writable:true });

  body.appendChild(wsRow);
  body.appendChild(infoRow);
  body.appendChild(el("div", { style:{ height:"1px", background:"var(--border)", margin:"0 -10px" } }));
  body.appendChild(btnRow);
  body.appendChild(resetBtn);
  body.appendChild(logoutBtn);
  body.appendChild(hint);

  panel.appendChild(header);
  panel.appendChild(body);
  document.body.appendChild(panel);

  // Keep wsDot in sync with connection state
  const _syncDot = () => {
    wsDot.className = UI.wsConnected ? "dot ok" : "dot off";
  };
  setInterval(_syncDot, 1000);

  fetch("/auth/status").then(r=>r.json()).then(d => {
    if (d.auth_enabled) logoutBtn.style.display = "block";
  }).catch(()=>{});

  window.addEventListener("keydown", e => {
    if(e.key==="p"||e.key==="P") CopEngine.pause();
    if(e.key==="r"||e.key==="R") CopEngine.resume(fetchCompositeSnapshot);
  });
}

/* ── Normalizers ─────────────────────────────────────────── */
function normTracks(x) {
  if(!x) return [];
  if(Array.isArray(x)) return x;
  if(typeof x==="object"&&Array.isArray(x.tracks)) return x.tracks;
  if(typeof x==="object"&&x.tracks&&!Array.isArray(x.tracks)) return Object.values(x.tracks);
  if(typeof x==="object"&&x.tracks===undefined) return Object.values(x);
  return [];
}
function getLL(t) {
  const lat=t.lat??t.latitude??t.y, lon=t.lon??t.lng??t.longitude??t.x;
  if(typeof lat!=="number"||typeof lon!=="number") return null;
  return [lat,lon];
}

/* ── TTS (Web Speech API) — Alarm Fatigue Prevention ───── */
const _TTS = (function() {
  let _enabled   = false;
  let _voices    = [];

  // ── Anti-fatigue: throttle + batching ──────────────────
  const WINDOW_MS      = 5000;   // 5-second dedup window
  const MAX_QUEUE      = 3;      // max queued utterances at once
  const _recentKeys    = new Map();  // key → timestamp (dedup)
  const _batchBucket   = { high_threat: 0, zone_breach: 0, ew_critical: 0, ew_high: 0 };
  let   _batchTimer    = null;

  if ("speechSynthesis" in window) {
    speechSynthesis.onvoiceschanged = () => { _voices = speechSynthesis.getVoices(); };
  }

  function _utterNow(text) {
    if (!("speechSynthesis" in window)) return;
    // Drop if queue already saturated
    if (speechSynthesis.pending && speechSynthesis.speaking) {
      const pending = speechSynthesis.pending;
      if (pending && MAX_QUEUE <= 1) return;
    }
    const utt  = new SpeechSynthesisUtterance(text);
    utt.rate   = 1.15;
    utt.volume = 1.0;
    const eng  = _voices.find(v => v.lang && v.lang.startsWith("en"));
    if (eng) utt.voice = eng;
    speechSynthesis.speak(utt);
  }

  function _flushBatch() {
    _batchTimer = null;
    const parts = [];
    if (_batchBucket.high_threat > 1)
      parts.push(`${_batchBucket.high_threat} high threats detected`);
    else if (_batchBucket.high_threat === 1)
      parts.push("High threat detected");

    if (_batchBucket.zone_breach > 1)
      parts.push(`${_batchBucket.zone_breach} zone breaches`);
    else if (_batchBucket.zone_breach === 1)
      parts.push("Zone breach");

    if (_batchBucket.ew_critical > 0)
      parts.push(`Critical EW alert`);
    if (_batchBucket.ew_high > 0)
      parts.push(`EW warning`);

    // Reset buckets
    _batchBucket.high_threat = 0;
    _batchBucket.zone_breach = 0;
    _batchBucket.ew_critical = 0;
    _batchBucket.ew_high     = 0;

    if (parts.length > 0) {
      speechSynthesis.cancel();  // clear stale queue
      _utterNow(parts.join(". "));
    }
  }

  function _scheduleBatch() {
    if (_batchTimer) return;  // already scheduled
    _batchTimer = setTimeout(_flushBatch, 800);  // 800ms collect window
  }

  /**
   * speak(text, priority, category)
   *   priority: "high" | "low"
   *   category: "high_threat" | "zone_breach" | "ew_critical" | "ew_high"
   *
   * Same category within WINDOW_MS is batched into a single announcement.
   * "20 zone breaches" → one utterance, not 20.
   */
  function speak(text, priority, category) {
    if (!_enabled || !("speechSynthesis" in window)) return;

    // Dedup: same category within window → batch
    if (category && _batchBucket.hasOwnProperty(category)) {
      _batchBucket[category]++;
      _scheduleBatch();
      return;
    }

    // Non-categorised: dedup by text within window
    const now = Date.now();
    const key = text.slice(0, 60);
    const last = _recentKeys.get(key);
    if (last && (now - last) < WINDOW_MS) return;
    _recentKeys.set(key, now);
    // Prune old keys
    if (_recentKeys.size > 50) {
      for (const [k, ts] of _recentKeys) {
        if (now - ts > WINDOW_MS * 2) _recentKeys.delete(k);
      }
    }

    if (priority === "high") speechSynthesis.cancel();
    _utterNow(text);
  }

  function toggle() { _enabled = !_enabled; return _enabled; }
  function isEnabled() { return _enabled; }
  return { speak, toggle, isEnabled };
})();

/* ── Altitude colour mode ───────────────────────────────── */
// _altColor moved to modules/constants.js (altitudeColor).
let _ALT_MODE = false;

/* THREAT_COLORS, INTENT_META → modules/constants.js */

function makeThreatIcon(level, intent, overrideColor) {
  const c = overrideColor ?? THREAT_COLORS[level] ?? "#2980b9";
  const im = INTENT_META[intent] ?? INTENT_META.unknown;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="42" viewBox="0 0 32 42">
    <path d="M16 0C7.16 0 0 7.16 0 16C0 28 16 42 16 42S32 28 32 16C32 7.16 24.84 0 16 0Z"
          fill="${c}" stroke="#fff" stroke-width="1.5"/>
    <circle cx="16" cy="16" r="8" fill="#fff" opacity="0.92"/>
    <circle cx="26" cy="6" r="6" fill="${im.color}" stroke="#fff" stroke-width="1"/>
    <text x="26" y="10" text-anchor="middle" font-size="8" font-weight="bold"
          fill="#fff" font-family="sans-serif">${im.icon}</text>
  </svg>`;
  return L.divIcon({ html:svg, className:"", iconSize:[32,42], iconAnchor:[16,42], tooltipAnchor:[0,-42] });
}

let mlPredictions = {}; // {track_id: {ml_level, ml_probability, ml_probabilities}}

function buildTooltip(track, threat) {
  const id    = track.id ?? track.global_track_id ?? "?";
  const kin   = track.kinematics ?? {};
  const cls   = track.classification ?? {};
  const sens  = (track.supporting_sensors ?? []).join(", ") || "-";
  const level = threat?.threat_level ?? track.threat_level ?? "-";
  const score = threat?.score        ?? track.threat_score ?? "-";
  const tti   = threat?.tti_s != null ? `${threat.tti_s}s` : "-";
  const vr    = kin.radial_velocity_mps != null ? `${kin.radial_velocity_mps.toFixed(1)} m/s` : "-";
  const range = kin.range_m != null ? `${Math.round(kin.range_m)} m` : "-";
  const action= threat?.recommended_action ?? "-";
  const intent= track.intent ?? threat?.intent ?? "unknown";
  const iconf = track.intent_conf ?? 0;
  const im    = INTENT_META[intent] ?? INTENT_META.unknown;
  // ML prediction
  const ml = mlPredictions[id];
  const mlLevel = ml?.ml_level ?? "-";
  const mlProb  = ml?.ml_probability != null ? `${(ml.ml_probability*100).toFixed(0)}%` : "-";
  const mlColor = THREAT_COLORS[mlLevel] ?? "#6366f1";
  const annCount = (_ANNOTATIONS.get(id) ?? []).length;
  const annBadge = annCount > 0 ? ` <span style="color:#90caf9">\u{1F4AC}${annCount}</span>` : "";
  const alt_m = kin.altitude_m != null ? `${Math.round(kin.altitude_m)} m` : "-";
  const altStyle = _ALT_MODE ? `color:${_altColor(kin.altitude_m ?? 0)};font-weight:bold` : "";
  return `<div style="font:12px/1.5 monospace;min-width:175px">
    <b style="font-size:13px">${id}</b>${annBadge}<br>
    <span style="color:${THREAT_COLORS[level]??'#aaa'}"> ${level}</span>
    \u00a0score:<b>${score}</b>
    \u00a0<span style="color:${mlColor}">ML:${mlLevel}(${mlProb})</span><br>
    TTI:<b>${tti}</b> Vr:<b>${vr}</b> Range:<b>${range}</b><br>
    Alt:<span style="${altStyle}">${alt_m}</span>
    \u00a0Intent:<span style="color:${im.color}">${im.icon} ${im.label}</span>(${(iconf*100).toFixed(0)}%)<br>
    Label:${cls.label??"?"}(${cls.conf!=null?(cls.conf*100).toFixed(0)+"%":"?"}) Sensors:${sens}<br>
    Trail:${(track.history??[]).length}pts Action:<b>${action}</b>
  </div>`;
}

/* ── Trail polylines ─────────────────────────────────────── */
function upsertTrail(track, threat) {
  const id  = String(track.id ?? track.global_track_id ?? "");
  const hist = (track.history ?? []).filter(h => typeof h.lat==="number"&&typeof h.lon==="number");
  if(!id||!UI.map||hist.length<2) return;
  const lls   = hist.map(h=>[h.lat,h.lon]);
  const color = THREAT_COLORS[threat?.threat_level??track.threat_level??"LOW"] ?? "#2980b9";
  const ex    = UI.trackPolylines.get(id);
  if(!ex) {
    UI.trackPolylines.set(id, L.polyline(lls,{color,weight:2,opacity:0.55,dashArray:"4 3"}).addTo(UI.map));
  } else { ex.setLatLngs(lls); ex.setStyle({color}); }
}
function removeTrail(id) {
  const pl=UI.trackPolylines.get(id); if(pl){try{pl.remove();}catch{}} UI.trackPolylines.delete(id);
}

/* ── Track markers ───────────────────────────────────────── */
function upsertTrack(track) {
  const id = String(track.id??track.track_id??track.uid??""); if(!id) return;
  UI.tracks.set(id, track);
  const ll=getLL(track); if(!ll||!UI.map) return;
  const threat=UI.threats.get(id);
  const level =threat?.threat_level??track.threat_level??"LOW";
  const intent=track.intent??"unknown";
  const altCol=_ALT_MODE ? _altColor(track.kinematics?.altitude_m ?? 0) : null;
  const icon  =makeThreatIcon(level, intent, altCol);
  const tip   =buildTooltip(track, threat);
  const ex    =UI.trackMarkers.get(id);
  if(!ex) {
    const m=L.marker(ll,{icon}).addTo(UI.map);
    m.bindTooltip(tip,{permanent:false,direction:"top",opacity:0.95});
    m.on("click", () => openTimeline(id));
    m.on("contextmenu", (e) => {
      L.DomEvent.preventDefault(e);
      const owner = TRACK_CLAIMS[id];
      if (owner && owner !== MY_OPERATOR_ID) {
        openLineage(id);  // locked by someone else → just show lineage
      } else {
        // Show mini context menu: lineage OR claim/release
        showTrackContextMenu(e.originalEvent, id);
      }
    });
    UI.trackMarkers.set(id,m);
  } else { ex.setLatLng(ll); ex.setIcon(icon); ex.setTooltipContent(tip); }
  upsertTrail(track, threat);
}

/* ── Fire control: track removal + effector impact animation ── */
function removeTrack(id) {
  if(!id) return;
  id = String(id);
  const m = UI.trackMarkers.get(id);
  if(m){ try{ m.remove(); }catch{} UI.trackMarkers.delete(id); }
  const pl = UI.trackPolylines.get(id);
  if(pl){ try{ pl.remove(); }catch{} UI.trackPolylines.delete(id); }
  UI.tracks.delete(id);
  UI.threats.delete(id);
}

function playEffectorImpact(payload) {
  // payload: { target_id, task_id, lat, lon, delay_s }
  if(!UI.map || payload?.lat == null || payload?.lon == null) return;
  const lat = +payload.lat, lon = +payload.lon;
  const delayS = +payload.delay_s || 2.0;

  // Expanding red ring at the impact point. Grows from 20m to ~250m over
  // delayS seconds, then fades out. Pure CSS via Leaflet circle radius
  // animation driven by requestAnimationFrame.
  const ring = L.circle([lat, lon], {
    radius: 20,
    color: "#e74c3c",
    weight: 3,
    fillColor: "#e74c3c",
    fillOpacity: 0.35,
  }).addTo(UI.map);

  // Inner muzzle flash dot
  const flash = L.circleMarker([lat, lon], {
    radius: 6,
    color: "#fff",
    weight: 2,
    fillColor: "#ffdd57",
    fillOpacity: 1.0,
  }).addTo(UI.map);

  const startTs = performance.now();
  const endTs   = startTs + delayS * 1000;
  const maxRadiusM = 250;

  function step(now) {
    const t = (now - startTs) / (endTs - startTs);
    if(t >= 1) {
      // Fade-out phase: hold briefly then clean up
      try { ring.remove(); } catch {}
      try { flash.remove(); } catch {}
      return;
    }
    const radius  = 20 + (maxRadiusM - 20) * t;
    const opacity = 0.55 * (1 - t);
    ring.setRadius(radius);
    ring.setStyle({ fillOpacity: opacity, opacity: 0.9 * (1 - t * 0.5) });
    flash.setStyle({ fillOpacity: 1 - t });
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/**
 * Play a non-lethal effect animation (JAM = yellow, SPOOF = cyan, EW_SUPPRESS = purple).
 * Expanding ring that pulses (unlike ENGAGE's fade-out) to signal suppression, not destruction.
 */
function playNLEffect(payload, color) {
  if (!UI.map || payload?.lat == null || payload?.lon == null) return;
  const lat = +payload.lat, lon = +payload.lon;
  const durS = +payload.duration_s || 10.0;

  const ring = L.circle([lat, lon], {
    radius: 30, color, weight: 2, fillColor: color, fillOpacity: 0.25,
  }).addTo(UI.map);
  const label = L.marker([lat, lon], {
    icon: L.divIcon({
      className: "",
      html: `<div style="background:${color};color:#000;font-size:9px;font-weight:bold;
                         padding:2px 4px;border-radius:3px;white-space:nowrap;opacity:.9">
               ${payload.action || "NL"}
             </div>`,
      iconAnchor: [20, -6],
    }),
  }).addTo(UI.map);

  const startTs = performance.now();
  const endTs   = startTs + durS * 1000;

  function step(now) {
    const t = Math.min((now - startTs) / (endTs - startTs), 1);
    if (t >= 1) {
      try { ring.remove(); } catch {}
      try { label.remove(); } catch {}
      return;
    }
    // Pulse: radius oscillates while fading
    const pulse  = 30 + 120 * t + 20 * Math.sin(t * Math.PI * 8);
    const opFill = 0.25 * (1 - t);
    ring.setRadius(pulse);
    ring.setStyle({ fillOpacity: opFill, opacity: 0.7 * (1 - t) });
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

function upsertThreat(threat) {
  const id=String(threat.id??threat.global_track_id??threat.threat_id??""); if(!id) return;
  UI.threats.set(id,threat);
  const marker=UI.trackMarkers.get(id), track=UI.tracks.get(id);
  if(marker&&track){
    const level=threat.threat_level??"LOW";
    const altCol2=_ALT_MODE ? _altColor(track.kinematics?.altitude_m ?? 0) : null;
    marker.setIcon(makeThreatIcon(level,track.intent??threat.intent??"unknown",altCol2));
    marker.setTooltipContent(buildTooltip(track,threat));
    const pl=UI.trackPolylines.get(id);
    if(pl) pl.setStyle({color:THREAT_COLORS[level]??"#2980b9"});
    // TTS: announce new HIGH threat
    if (level === "HIGH") {
      const prev = UI.threats.get(id);
      if (!prev || prev.threat_level !== "HIGH") {
        _TTS.speak(`High threat detected. Track ${id}`, "high", "high_threat");
      }
    }
  }
  _scheduleRenderThreatList();
  updateThreatIntelCard();
}

/* ── Collapsible panel wrapper ───────────────────────────── */
/**
 * Wrap bodyEl in a collapsible section with a thin header.
 * State is persisted in localStorage under key "nz.col.<key>".
 * Returns the outer wrapper element to append into a container.
 */
function _makeCollapsible(label, bodyEl, key) {
  const lsKey  = `nz.col.${key}`;
  const isOpen = localStorage.getItem(lsKey) !== "0";

  const wrap = el("div", { style: { width: "100%", marginBottom: "2px" } });
  const hdr  = el("div", {
    style: {
      display: "flex", alignItems: "center", gap: "5px",
      padding: "4px 8px", cursor: "pointer", userSelect: "none",
      background: "rgba(255,255,255,0.05)", color: "var(--text-3)",
      borderRadius: isOpen ? "4px 4px 0 0" : "4px",
      borderLeft: "2px solid rgba(255,255,255,0.12)",
      fontSize: "9px", fontWeight: "700", letterSpacing: "0.8px",
      textTransform: "uppercase",
    },
  });
  const arrow = el("span", {
    style: {
      fontSize: "8px", display: "inline-block",
      transition: "transform 0.15s",
      transform: isOpen ? "rotate(0deg)" : "rotate(-90deg)",
    },
  }, ["\u25BC"]);
  hdr.appendChild(arrow);
  hdr.appendChild(el("span", {}, [label]));

  // Seamless join: remove top border-radius of body so header and body merge
  bodyEl.style.borderTopLeftRadius  = "0";
  bodyEl.style.borderTopRightRadius = "0";
  bodyEl.style.marginTop = "0";
  if (!isOpen) bodyEl.style.display = "none";

  hdr.addEventListener("click", () => {
    const nowOpen = bodyEl.style.display === "none";
    bodyEl.style.display = nowOpen ? "" : "none";
    arrow.style.transform = nowOpen ? "rotate(0deg)" : "rotate(-90deg)";
    hdr.style.borderRadius = nowOpen ? "4px 4px 0 0" : "4px";
    localStorage.setItem(lsKey, nowOpen ? "1" : "0");
  });

  wrap.appendChild(hdr);
  wrap.appendChild(bodyEl);
  return wrap;
}

/* ── Threat list panel (1.3) ─────────────────────────────── */
/* ── 2.4 Threat Intelligence Card ───────────────────────── */
let intelCardEl = null;
// Running signal counters updated by each subsystem
const _intel = { high: 0, med: 0, ew: 0, anom: 0, coord: 0 };

function mountThreatIntelCard() {
  intelCardEl = el("div", { id:"threat-intel-card", class:"nz-card", style:{
    padding:"8px 10px", marginBottom:"6px", width:"100%",
  }});
  intelCardEl.innerHTML = `<div class="nz-section" style="margin:0 0 6px 0">TEHDIT ZEKASI</div>
    <div style="color:var(--text-3);font-size:10px">Sinyal bekleniyor...</div>`;
  RIGHT_TABS.threats.insertBefore(intelCardEl, RIGHT_TABS.threats.firstChild);
}

function updateThreatIntelCard() {
  if (!intelCardEl) return;

  // Recalculate from live data
  let high = 0, med = 0;
  UI.threats.forEach(t => {
    if (t.threat_level === "HIGH") high++;
    else if (t.threat_level === "MEDIUM") med++;
  });
  _intel.high  = high;
  _intel.med   = med;
  _intel.ew    = ewLog.length;
  _intel.anom  = anomalyLog.filter(a => a.severity === "CRITICAL" || a.severity === "HIGH").length;

  // Overall risk level
  let risk = "LOW", riskColor = "var(--ok)";
  if (_intel.coord > 0 || ewLog.some(e => e.severity === "CRITICAL")) {
    risk = "CRITICAL"; riskColor = "var(--danger)";
  } else if (_intel.high > 0 || _intel.ew > 0) {
    risk = "HIGH"; riskColor = "var(--warn)";
  } else if (_intel.med > 0 || _intel.anom > 0) {
    risk = "MEDIUM"; riskColor = "#f1c40f";
  }

  const sig = (label, val, color) => val > 0
    ? `<div style="text-align:center;flex:1">
        <div style="font-size:14px;font-weight:700;color:${color};font-family:var(--mono)">${val}</div>
        <div style="font-size:8px;color:var(--text-3);letter-spacing:.04em">${label}</div>
       </div>`
    : `<div style="text-align:center;flex:1;opacity:.35">
        <div style="font-size:14px;font-weight:700;font-family:var(--mono)">0</div>
        <div style="font-size:8px;color:var(--text-3);letter-spacing:.04em">${label}</div>
       </div>`;

  intelCardEl.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:7px">
      <span class="nz-section" style="margin:0">TEHDIT ZEKASI</span>
      <span style="font-size:9px;font-weight:700;color:${riskColor};letter-spacing:.06em;
        background:${riskColor}1a;padding:2px 7px;border-radius:8px;border:1px solid ${riskColor}44">${risk}</span>
    </div>
    <div style="display:flex;gap:2px">
      ${sig("HIGH", _intel.high, "var(--danger)")}
      ${sig("MEDIUM", _intel.med, "var(--warn)")}
      ${sig("EW", _intel.ew, "var(--purple)")}
      ${sig("ANOM", _intel.anom, "#f1c40f")}
      ${sig("COORD", _intel.coord, "#ff0050")}
    </div>`;
}

let threatListEl = null;
let _threatListTimer = null;
function _scheduleRenderThreatList() {
  if (_threatListTimer) return;
  _threatListTimer = setTimeout(() => { _threatListTimer = null; renderThreatList(); }, 200);
}

function mountThreatList() {
  mountThreatIntelCard();
  threatListEl = el("div", { id:"threat-list" });
  threatListEl.innerHTML = `<div style="color:var(--text-3);font-size:11px;padding:8px 0">No threats assessed yet</div>`;
  RIGHT_TABS.threats.insertBefore(threatListEl, intelCardEl ? intelCardEl.nextSibling : RIGHT_TABS.threats.firstChild);
}

function renderThreatList() {
  if (!threatListEl) return;
  const threats = [...UI.threats.values()]
    .sort((a,b) => (b.score??0) - (a.score??0))
    .slice(0, 20);
  if (!threats.length) {
    threatListEl.innerHTML = `<div style="color:var(--text-3);font-size:11px;padding:4px 0">No threats assessed yet</div>`;
    return;
  }
  const IM = INTENT_META;
  let html = `<div class="nz-section">Live Threats (${threats.length})</div>`;
  threats.forEach(t => {
    const level = t.threat_level ?? "LOW";
    const lc    = { HIGH:"var(--danger)", MEDIUM:"var(--warn)", LOW:"var(--ok)" }[level] ?? "var(--text-3)";
    const im    = IM[t.intent ?? "unknown"] ?? IM.unknown;
    const tti   = t.tti_s != null ? `${t.tti_s}s` : "–";
    const score = t.score != null ? (+t.score).toFixed(2) : "–";
    const act   = t.recommended_action ?? "–";
    html += `<div class="nz-threat-card ${level}" onclick="UI.map&&UI.trackMarkers.get('${t.id}')&&UI.map.panTo(UI.trackMarkers.get('${t.id}').getLatLng())">
      <div style="display:flex;align-items:center;gap:5px;margin-bottom:2px">
        <span style="font-weight:600;font-family:var(--mono);font-size:11px">${t.id}</span>
        <span class="nz-level ${level}">${level}</span>
        <span style="margin-left:auto;font-family:var(--mono);font-size:10px;color:${lc}">${score}</span>
      </div>
      <div style="color:var(--text-2);font-size:10px;display:flex;gap:8px">
        <span>TTI <b style="color:var(--text-1)">${tti}</b></span>
        <span style="color:${im.color}">${im.icon} ${im.label}</span>
        <span style="margin-left:auto;color:var(--text-3)">${act}</span>
      </div>
    </div>`;
  });
  threatListEl.innerHTML = html;
}

/* ── Zones ────────────────────────────────────────────────── */
const ZONE_COLORS = {
  restricted:{fill:"#f39c12",stroke:"#e67e22"},
  kill:      {fill:"#e74c3c",stroke:"#c0392b"},
  friendly:  {fill:"#27ae60",stroke:"#1e8449"},
};
function upsertZone(zone) {
  if(!zone?.id||!zone.coordinates) return;
  removeZone(zone.id);
  const c=ZONE_COLORS[zone.type]??{fill:"#8e44ad",stroke:"#6c3483"};
  const p=L.polygon(zone.coordinates,{color:c.stroke,fillColor:c.fill,fillOpacity:0.2,weight:2,
    dashArray:zone.type==="restricted"?"6 4":null}).addTo(UI.map);
  p.bindTooltip(`<b>${zone.name??zone.id}</b><br>Type: ${zone.type}`,{sticky:true,opacity:0.9});
  UI.zonePolygons.set(zone.id,p);
}
function removeZone(id) {
  const p=UI.zonePolygons.get(id); if(p){try{p.remove();}catch{}} UI.zonePolygons.delete(id);
}

/* ── Phase 3: Asset markers ──────────────────────────────── */
const ASSET_META = {
  friendly: {color:"#2980b9", symbol:"F"},
  hostile:  {color:"#e74c3c", symbol:"H"},
  unknown:  {color:"#95a5a6", symbol:"U"},
};

function makeAssetIcon(type) {
  const m = ASSET_META[type] ?? ASSET_META.unknown;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="0 0 30 30">
    <rect x="2" y="2" width="26" height="26" rx="5"
          fill="${m.color}" stroke="#fff" stroke-width="2"/>
    <text x="15" y="21" text-anchor="middle" font-size="14" font-weight="bold"
          fill="#fff" font-family="sans-serif">${m.symbol}</text>
  </svg>`;
  return L.divIcon({ html:svg, className:"", iconSize:[30,30], iconAnchor:[15,15], tooltipAnchor:[0,-15] });
}

function upsertAsset(asset) {
  if(!asset?.id||asset.lat==null||asset.lon==null) return;
  const id=String(asset.id);
  const icon=makeAssetIcon(asset.type);
  const ex=UI.assetMarkers.get(id);
  if(!ex) {
    const m=L.marker([asset.lat,asset.lon],{icon}).addTo(UI.map);
    m.bindTooltip(`<b>${asset.name??id}</b><br>Type: ${asset.type}<br>Status: ${asset.status??"active"}`,
                  {permanent:false,direction:"top",opacity:0.95});
    UI.assetMarkers.set(id,m);
  } else {
    ex.setLatLng([asset.lat,asset.lon]);
    ex.setIcon(icon);
    ex.setTooltipContent(`<b>${asset.name??id}</b><br>Type: ${asset.type}<br>Status: ${asset.status??"active"}`);
  }
}

function removeAsset(id) {
  const m=UI.assetMarkers.get(id); if(m){try{m.remove();}catch{}} UI.assetMarkers.delete(id);
}

/* ── Phase 3: Waypoint markers ──────────────────────────── */
function makeWaypointIcon(order) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 26 26">
    <circle cx="13" cy="13" r="11" fill="#1abc9c" stroke="#fff" stroke-width="2"/>
    <text x="13" y="18" text-anchor="middle" font-size="11" font-weight="bold"
          fill="#fff" font-family="sans-serif">${order}</text>
  </svg>`;
  return L.divIcon({ html:svg, className:"", iconSize:[26,26], iconAnchor:[13,13] });
}

function _refreshWaypointRoute() {
  const wps = [...UI.waypointMarkers.entries()]
    .map(([id,m]) => ({ id, latlng: m.getLatLng(), order: m._wpOrder ?? 0 }))
    .sort((a,b) => a.order - b.order)
    .map(w => [w.latlng.lat, w.latlng.lng]);

  if(UI.waypointLine) { try{UI.waypointLine.remove();}catch{} UI.waypointLine=null; }
  if(wps.length>=2) {
    UI.waypointLine = L.polyline(wps, {color:"#1abc9c", weight:2, dashArray:"6 4", opacity:0.8}).addTo(UI.map);
  }
}

function upsertWaypoint(wp) {
  if(!wp?.id||wp.lat==null||wp.lon==null) return;
  const id=String(wp.id);
  const icon=makeWaypointIcon(wp.order??0);
  const ex=UI.waypointMarkers.get(id);
  if(!ex) {
    const m=L.marker([wp.lat,wp.lon],{icon}).addTo(UI.map);
    m.bindTooltip(`<b>${wp.name??id}</b><br>Order: ${wp.order}`,{permanent:false,opacity:0.9});
    m._wpOrder = wp.order ?? 0;
    UI.waypointMarkers.set(id,m);
  } else {
    ex.setLatLng([wp.lat,wp.lon]);
    ex.setIcon(icon);
    ex.setTooltipContent(`<b>${wp.name??id}</b><br>Order: ${wp.order}`);
    ex._wpOrder = wp.order ?? 0;
  }
  _refreshWaypointRoute();
}

function removeWaypoint(id) {
  const m=UI.waypointMarkers.get(id); if(m){try{m.remove();}catch{}} UI.waypointMarkers.delete(id);
  _refreshWaypointRoute();
}

function clearWaypoints() {
  for(const [id] of UI.waypointMarkers) removeWaypoint(id);
  UI.waypointMarkers.clear();
  _refreshWaypointRoute();
}

/* ── Snapshot ─────────────────────────────────────────────── */
function applySnapshot(payload) {
  const tracksArr = normTracks(payload?.tracks ?? payload);

  UI.tracks.clear();
  for(const[,m] of UI.trackMarkers){try{m.remove();}catch{}} UI.trackMarkers.clear();
  for(const[id] of UI.trackPolylines) removeTrail(id);
  for(const[id] of UI.zonePolygons)   removeZone(id);
  // Keep assets / waypoints across snapshot (they are operator-placed)

  tracksArr.forEach(t => {
    const id=String(t.id??t.track_id??t.uid??""); if(!id) return;
    UI.tracks.set(id,t); upsertTrack(t);
  });
  (payload?.zones     ?? []).forEach(z => upsertZone(z));
  (payload?.assets    ?? []).forEach(a => upsertAsset(a));
  (payload?.waypoints ?? []).forEach(w => upsertWaypoint(w));
  (payload?.tasks     ?? []).forEach(t => pushTask(t));
  renderThreatList();

  // Multi-operator state (if present in the snapshot)
  if (payload?.operators) {
    OPERATORS_STATE = {};
    payload.operators.forEach(op => { OPERATORS_STATE[op.operator_id] = op; });
  }
  if (payload?.claims) {
    TRACK_CLAIMS = {...(payload.claims)};
    // Re-render any task cards that may be locked
    refreshTaskClaimUI();
  }
  renderOperatorPanel();

  // AI state (if present in the snapshot payload)
  if (payload && (payload.predictions || payload.trajectories
      || payload.recommendations || payload.roe_advisories
      || payload.ml_predictions)) {
    applyAIUpdate(payload);
  }
}

/* Apply a WebSocket AI update event (cop.ai_update) */
function applyAIUpdate(payload) {
  if (!payload) return;
  drawPredictions(payload.predictions || {});
  drawTrajectories(payload.trajectories || {});
  drawUncertaintyCones(payload.uncertainty_cones || {});
  renderBreachPanel(payload.pred_breaches || []);
  renderCoordPanel(payload.coord_attacks || []);
  _roeAdvisories = payload.roe_advisories || [];
  renderROEPanel(_roeAdvisories);
  renderConfidencePanel(payload.confidence_scores || {});
  mlPredictions = payload.ml_predictions || {};
  if (typeof payload.ml_available === "boolean") {
    mlModelAvailable = payload.ml_available;
  }
  renderMLPanel(mlPredictions);
  // Anomalies: only push new ones (dedupe against anomalyLog)
  const newAnomalies = (payload.anomalies || []).filter(a =>
    !anomalyLog.some(e => e.time === a.time && e.type === a.type
      && (e.track_id||"") === (a.track_id||""))
  );
  if (newAnomalies.length > 0) pushAnomalies(newAnomalies);
  renderTacticalPanel(payload.recommendations || []);
  renderAssignmentPanel(payload.assignment || {});
  renderBFTPanel(payload.bft_warnings || []);
  renderEffectorStatusPanel(payload.effector_status || {}, payload.effector_outcomes || []);
  renderDriftPanel(payload.drift || {});
  // Auto-refresh open timeline chart
  if (timelineCurrentTrack) fetchAndDrawTimeline(timelineCurrentTrack);
}

/* ── Zone breach alert panel ─────────────────────────────── */
/* ── ROE Escalation banner + Engagement Approval Modal ────── */
let _escalationBannerEl = null;
const _escalationLog = [];
const MAX_ESCALATIONS = 20;
let _roeAdvisories = [];   // latest ROE list from applyAIUpdate

/* ── Engagement approval modal state ── */
const _engModal = {
  el: null, cardEl: null, timerEl: null,
  trackId: null, taskId: null,
  _interval: null, _startTs: null, _durationS: 0,
};

function _buildEngModalEl() {
  const overlay = el("div", {
    id: "eng-modal-overlay",
    style: {
      display: "none",
      position: "fixed", inset: "0", zIndex: "999998",
      background: "rgba(0,0,0,0.72)",
      alignItems: "center", justifyContent: "center",
    },
  });
  const card = el("div", {
    id: "eng-modal-card",
    style: {
      background: "#1a1a2e", border: "2px solid #e74c3c",
      borderRadius: "12px", padding: "20px 24px",
      minWidth: "380px", maxWidth: "460px",
      fontFamily: "var(--mono), monospace", color: "#fff",
      boxShadow: "0 8px 40px rgba(0,0,0,0.8)",
    },
  });
  overlay.appendChild(card);
  document.body.appendChild(overlay);
  _engModal.el     = overlay;
  _engModal.cardEl = card;
}

function _openEngModal(p) {
  if (!_engModal.el) _buildEngModalEl();
  const { el: overlay, cardEl: card } = _engModal;

  const isCrit    = p.level === "CRITICAL";
  const borderCol = isCrit ? "#e74c3c" : "#e67e22";
  const bgCol     = isCrit ? "rgba(231,76,60,0.15)" : "rgba(230,126,34,0.12)";
  _engModal.trackId   = p.track_id;
  _engModal._startTs  = Date.now();
  _engModal._durationS = p.duration_s || 0;

  const task = [...pendingTasks.values()]
    .find(t => t.track_id === p.track_id && t.action === "ENGAGE");
  _engModal.taskId = task?.id || null;

  const adv = _roeAdvisories.find(a => a.track_id === p.track_id);

  card.style.borderColor = borderCol;
  card.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;
                padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,0.1)">
      <span style="font-size:22px">${isCrit ? "\u{1F6A8}" : "\u26A0\uFE0F"}</span>
      <div>
        <div style="font-size:10px;color:${borderCol};letter-spacing:1.5px;font-weight:700">
          ${isCrit ? "KR\u0130T\u0130K" : "UYARI"}</div>
        <div style="font-size:15px;font-weight:700;letter-spacing:.3px">ANGAJMAN ONAYI GEREKL\u0130</div>
      </div>
      <div id="eng-timer" style="margin-left:auto;font-size:24px;font-weight:700;
           color:${borderCol};font-variant-numeric:tabular-nums">--s</div>
    </div>

    <div style="background:${bgCol};border-radius:8px;padding:12px 14px;margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:10px;color:var(--text-3)">HEDEF</span>
        <span style="font-size:18px;font-weight:700">${escHtml(p.track_id)}</span>
        <span style="margin-left:auto;background:${borderCol};padding:2px 8px;
               border-radius:4px;font-size:10px;font-weight:700">${escHtml(p.engagement)}</span>
      </div>
      ${adv ? `
        <div style="font-size:10px;color:var(--text-2);display:flex;gap:12px;flex-wrap:wrap">
          <span>Tehdit: <b style="color:${THREAT_COLORS[adv.threat_level]??'#fff'}">${adv.threat_level??"-"}</b></span>
          <span>Skor: <b>${adv.threat_score??"-"}</b></span>
          <span>Intent: <b>${adv.intent??"-"}</b></span>
          ${adv.in_kill_zone   ? '<span style="color:#e74c3c;font-weight:700">\u26A1 KILL ZONE</span>' : ""}
          ${adv.is_coordinated ? '<span style="color:#ff6600;font-weight:700">\u26A1 KOORD\u0130NELI</span>' : ""}
        </div>
        ${adv.reasons?.length
          ? `<div style="font-size:9px;color:var(--text-3);margin-top:5px">${adv.reasons.join(" \u00b7 ")}</div>`
          : ""}
      ` : `<div style="font-size:10px;color:var(--text-3)">${escHtml(p.message)}</div>`}
    </div>

    ${task ? `
      <div style="font-size:10px;color:var(--text-3);margin-bottom:12px;padding:6px 10px;
                  background:rgba(255,255,255,0.04);border-radius:6px">
        G\u00f6rev: <b style="color:${ACTION_COLORS[task.action]??'#fff'}">${task.action}</b>
        &nbsp; TTI: <b>${task.tti_s??"-"}s</b>
      </div>
    ` : ""}

    <div style="display:flex;gap:10px">
      <button id="eng-approve" class="nz-btn c-ok"
        style="flex:1;padding:10px;font-size:13px;font-weight:700;letter-spacing:1px">
        \u2714 ONAYLA
      </button>
      <button id="eng-reject" class="nz-btn c-danger"
        style="flex:1;padding:10px;font-size:13px;font-weight:700;letter-spacing:1px">
        \u2718 REDDET
      </button>
      <button id="eng-defer" class="nz-btn"
        style="padding:10px 12px;font-size:11px;color:var(--text-3)">
        \u23F8 Ertele
      </button>
    </div>
    <div style="margin-top:10px;font-size:9px;color:var(--text-3);text-align:center">
      Operat\u00f6r: <b>${escHtml(MY_OPERATOR_ID)}</b>
    </div>
  `;

  _engModal.timerEl = card.querySelector("#eng-timer");
  if (_engModal._interval) clearInterval(_engModal._interval);
  _engModal._interval = setInterval(_tickEngTimer, 1000);
  _tickEngTimer();

  card.querySelector("#eng-approve").addEventListener("click", () => _engModalAct("approve"));
  card.querySelector("#eng-reject").addEventListener("click",  () => _engModalAct("reject"));
  card.querySelector("#eng-defer").addEventListener("click",   () => _closeEngModal());

  overlay.style.display = "flex";
}

function _tickEngTimer() {
  if (!_engModal.timerEl) return;
  const elapsed = Math.floor((Date.now() - _engModal._startTs) / 1000);
  const total   = _engModal._durationS + elapsed;
  _engModal.timerEl.textContent = `${total}s`;
  _engModal.timerEl.style.color = total > 60 ? "#e74c3c" : total > 30 ? "#e67e22" : "#f1c40f";
}

async function _engModalAct(action) {
  const { trackId, taskId } = _engModal;
  try {
    if (taskId) {
      await fetch(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ operator: MY_OPERATOR_ID, operator_id: MY_OPERATOR_ID }),
      });
    } else {
      await fetch(`/api/roe/${encodeURIComponent(trackId)}/ack`, {
        method: "POST", headers: authHeaders(),
      });
    }
  } catch (e) { console.warn("[eng-modal]", e); }
  _closeEngModal();
  if (_escalationBannerEl) _escalationBannerEl.style.display = "none";
}

function _closeEngModal() {
  if (_engModal._interval) { clearInterval(_engModal._interval); _engModal._interval = null; }
  if (_engModal.el) _engModal.el.style.display = "none";
}

function mountEscalationBanner() {
  _escalationBannerEl = el("div", {
    id: "esc-banner",
    style: {
      display: "none",
      position: "fixed", top: "0", left: "0", right: "0", zIndex: "99999",
      background: "#c0392b", color: "#fff",
      padding: "6px 16px 6px 16px", fontSize: "12px", fontWeight: "bold",
      textAlign: "center", letterSpacing: "0.5px",
      borderBottom: "2px solid #e74c3c", cursor: "pointer",
    },
  });
  _escalationBannerEl.title = "Tıkla: modalı aç";
  _escalationBannerEl.addEventListener("click", () => {
    const latest = _escalationLog[0];
    if (latest) _openEngModal(latest); else _dismissEscalationBanner();
  });
  document.body.appendChild(_escalationBannerEl);
}

function pushEscalation(p) {
  const t = p.server_time
    ? new Date(p.server_time).toLocaleTimeString()
    : new Date().toLocaleTimeString();
  _escalationLog.unshift({ t, ...p });
  if (_escalationLog.length > MAX_ESCALATIONS) _escalationLog.pop();

  if (_escalationBannerEl) {
    const icon = p.level === "CRITICAL" ? "\u{1F6A8}" : "\u26A0\uFE0F";
    _escalationBannerEl.textContent =
      `${icon} ${p.message}  [${t}]  \u2014 Tıkla: onay modalını aç`;
    _escalationBannerEl.style.display = "block";
    _escalationBannerEl.style.background = p.level === "CRITICAL" ? "#7b241c" : "#c0392b";
    clearTimeout(_escalationBannerEl._timer);
    _escalationBannerEl._timer = setTimeout(
      () => { if (_escalationBannerEl) _escalationBannerEl.style.display = "none"; },
      60000,
    );
  }

  // Open approval modal immediately (opens on top of banner)
  _openEngModal(p);

  if (typeof playAlarm === "function") {
    playAlarm(p.level === "CRITICAL" ? "critical" : "warning");
  }
}

function _dismissEscalationBanner() {
  const latest = _escalationLog[0];
  if (latest?.track_id) {
    fetch(`/api/roe/${encodeURIComponent(latest.track_id)}/ack`, {
      method: "POST", headers: authHeaders(),
    }).catch(() => {});
  }
  if (_escalationBannerEl) _escalationBannerEl.style.display = "none";
}

const MAX_ALERTS = 20;
let alertPanelEl = null;
const alertsLog  = [];

/* ── EW / Electronic Warfare alert state ────────────────── */
const MAX_EW_ALERTS = 30;
let ewPanelEl = null;
const ewLog = [];
let _ewBadgeCount = 0;

function mountAlertPanel() {
  alertPanelEl = el("div", { id:"alert-panel", style:{ width:"100%" } });
  alertPanelEl.innerHTML = `<div class="nz-section">Zone Alerts</div><div style="color:var(--text-3);font-size:11px;padding:4px 0">No breaches yet</div>`;
  RIGHT_TABS.alerts.appendChild(alertPanelEl);
}

function pushAlert(p) {
  const t = p.server_time ? new Date(p.server_time).toLocaleTimeString() : new Date().toLocaleTimeString();
  alertsLog.unshift({t, track_id:p.track_id, zone_name:p.zone_name, zone_type:p.zone_type});
  if(alertsLog.length>MAX_ALERTS) alertsLog.pop();
  const ZC = { kill:"var(--danger)", restricted:"var(--warn)", friendly:"var(--ok)" };
  let html = `<div class="nz-section">Zone Alerts <span style="font-weight:400;color:var(--text-3)">(${alertsLog.length})</span></div>`;
  alertsLog.forEach(a => {
    const c = ZC[a.zone_type] || "var(--danger)";
    html += `<div class="nz-card" style="border-left:3px solid ${c};margin-bottom:3px">
      <div style="display:flex;gap:6px;align-items:center">
        <span style="font-weight:600;color:${c};font-family:var(--mono);font-size:11px">${a.track_id}</span>
        <span style="color:var(--text-2)">${a.zone_name}</span>
        <span style="margin-left:auto;color:var(--text-3);font-size:10px">${a.t}</span>
      </div>
    </div>`;
  });
  if (alertPanelEl) alertPanelEl.innerHTML = html;
  const marker=UI.trackMarkers.get(p.track_id);
  if(marker){ const e=marker.getElement(); if(e){e.style.filter="drop-shadow(0 0 8px red)";setTimeout(()=>{e.style.filter="";},1000);} }
  // TTS: announce zone breach
  _TTS.speak(`Zone breach. Track ${p.track_id} entered ${p.zone_name ?? "restricted zone"}`, "high", "zone_breach");
}

/* ── EW alert panel ──────────────────────────────────────── */
function mountEWPanel() {
  ewPanelEl = el("div", { id:"ew-panel", style: {
    fontFamily:"var(--mono)", fontSize:"11px",
    lineHeight:"1.6", width:"100%",
  }});
  ewPanelEl.innerHTML = "<span style='color:var(--text-3)'>No EW events detected</span>";
  RIGHT_TABS.ew.appendChild(ewPanelEl);
}

function _ewToast(text, color) {
  const t = el("div", { style: {
    position:"fixed", top:"54px", left:"50%", transform:"translateX(-50%)",
    background:color, color:"white", padding:"9px 20px", borderRadius:"6px",
    fontWeight:"600", fontSize:"12px", zIndex:"99999",
    fontFamily:"var(--font)", letterSpacing:"0.03em",
    boxShadow:"0 4px 20px rgba(0,0,0,0.7)", pointerEvents:"none",
    animation:"fadeInDown .2s ease",
  }}, [text]);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

function pushEWAlert(p) {
  const now = new Date();
  const t = p.server_time ? new Date(p.server_time).toLocaleTimeString() : now.toLocaleTimeString();
  ewLog.unshift({ t, type: p.type, severity: p.severity, track_id: p.track_id, detail: p.detail });
  if (ewLog.length > MAX_EW_ALERTS) ewLog.pop();
  updateThreatIntelCard();

  const SEV_COLOR = { CRITICAL:"var(--danger)", HIGH:"var(--warn)", MEDIUM:"#f1c40f", LOW:"var(--ok)" };
  const SEV_BG    = { CRITICAL:"rgba(240,64,64,0.1)", HIGH:"rgba(240,128,48,0.1)", MEDIUM:"rgba(241,196,15,0.08)", LOW:"rgba(48,192,96,0.08)" };
  const TYPE_LABEL = { GPS_SPOOFING:"GPS SPOOF", RADAR_JAMMING:"RADAR JAM", FALSE_INJECTION:"INJECT" };

  if (ewPanelEl) {
    let html = "";
    ewLog.forEach((a, i) => {
      const c  = SEV_COLOR[a.severity]  || "var(--danger)";
      const bg = SEV_BG[a.severity]    || "rgba(240,64,64,0.08)";
      const lbl = TYPE_LABEL[a.type]   || a.type;
      html += `<div style="padding:6px 8px;margin-bottom:4px;border-radius:5px;background:${bg};border-left:3px solid ${c}${i===0?';animation:slide-in-right .2s ease':''}">`;
      html += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">`;
      html += `<span style="color:${c};font-weight:700;font-size:10px;letter-spacing:.06em">${lbl}</span>`;
      html += `<span style="color:${c};font-size:9px;opacity:.7">${a.severity}</span>`;
      if (a.track_id) html += `<span style="color:var(--text-2);font-size:10px">${a.track_id}</span>`;
      html += `<span style="color:var(--text-3);font-size:9px;margin-left:auto">${a.t}</span>`;
      html += `</div>`;
      html += `<div style="color:var(--text-2);font-size:10px;line-height:1.4">${(a.detail||"").slice(0,100)}</div>`;
      html += `</div>`;
    });
    ewPanelEl.innerHTML = html || "<span style='color:var(--text-3)'>No EW events detected</span>";
  }

  // Badge on tab (only when EW tab not active)
  const activeTab = document.querySelector(".nz-tab.active");
  if (!activeTab || activeTab.id !== "rtab-ew") {
    _ewBadgeCount++;
    setTabBadge("ew", _ewBadgeCount);
  }

  // Switch to EW tab automatically for CRITICAL
  if (p.severity === "CRITICAL") {
    const btn = document.getElementById("rtab-ew");
    if (btn) btn.click();
    _ewToast(`EW ALERT: ${p.type} — CRITICAL`, "var(--danger)");
    _TTS.speak(`Critical EW alert. ${(p.type || "").replace("_", " ")} detected`, "high", "ew_critical");
  } else if (p.severity === "HIGH") {
    _ewToast(`EW: ${p.type}`, "var(--warn)");
    _TTS.speak(`EW warning. ${(p.type || "").replace("_", " ")}`, "low", "ew_high");
  }

  // Highlight the affected track on map
  if (p.track_id) {
    const marker = UI.trackMarkers.get(p.track_id);
    if (marker) {
      const e = marker.getElement();
      if (e) {
        e.style.filter = "drop-shadow(0 0 10px #e74c3c)";
        setTimeout(() => { e.style.filter = ""; }, 1500);
      }
    }
  }
}

function pushTrackMerged(p) {
  // Deconfliction: canonical_id absorbed alias_id
  // Show a brief info toast; no separate panel needed
  const msg = `🔀 Merged ${p.alias_id || "?"} → ${p.canonical_id || "?"} (score ${(p.score||0).toFixed(2)})`;
  _ewToast(msg, "#2980b9");
}

/* ── Phase 3: Task / action-queue panel ─────────────────── */
let taskPanelEl = null;
const pendingTasks = new Map();  // task_id -> task

function mountTaskPanel() {
  taskPanelEl = el("div", { id:"task-panel", style:{ width:"100%" } });
  taskPanelEl.innerHTML = `<div class="nz-section">Task Queue</div><div style="color:var(--text-3);font-size:11px;padding:4px 0">No pending tasks</div>`;
  RIGHT_TABS.tasks.appendChild(taskPanelEl);
}

/* ACTION_COLORS, ACTION_BG → modules/constants.js */

function _renderTaskPanel() {
  if(!taskPanelEl) return;
  const tasks=[...pendingTasks.values()];
  if(tasks.length===0){
    taskPanelEl.innerHTML=`<div class="nz-section">Task Queue</div><div style="color:var(--text-3);font-size:11px;padding:4px 0">No pending tasks</div>`;
    return;
  }
  let html=`<div class="nz-section">Task Queue <span style="font-weight:400;color:var(--text-3)">(${tasks.length})</span></div>`;
  tasks.slice(0,8).forEach(t => {
    const c   = ACTION_COLORS[t.action] ?? "var(--text-2)";
    const bg  = ACTION_BG[t.action]    ?? "rgba(255,255,255,0.03)";
    const tti = t.tti_s!=null ? `TTI ${t.tti_s}s` : "";
    const intent = INTENT_META[t.intent]?.label ?? t.intent ?? "?";
    const owner  = TRACK_CLAIMS[t.track_id];
    const locked = owner && owner !== MY_OPERATOR_ID;
    const lockIcon = locked ? " \uD83D\uDD12" : "";
    html+=`<div class="nz-task-card" style="border-left:3px solid ${c};background:${bg}">
      <div style="display:flex;align-items:center;gap:5px;margin-bottom:3px">
        <span class="action-badge" style="background:${bg};color:${c};border:1px solid ${c}">${t.action}</span>
        <span style="font-weight:600;font-family:var(--mono);font-size:11px">${t.track_id}</span>
        ${owner?`<span style="font-size:9px;color:${_opColor(owner)}">${lockIcon} ${owner}</span>`:""}
        <span style="margin-left:auto;font-size:10px;color:var(--text-3)">${t.score??""}</span>
      </div>
      <div style="font-size:10px;color:var(--text-2);margin-bottom:4px">${t.threat_level ?? ""} · ${intent} · ${tti}</div>
      <div style="display:flex;gap:4px">
        <button class="nz-btn c-ok" data-id="${t.id}" data-act="approve" ${locked?"disabled":""} style="flex:1;opacity:${locked?'0.3':'1'}">Approve</button>
        <button class="nz-btn c-danger" data-id="${t.id}" data-act="reject" ${locked?"disabled":""} style="flex:1;opacity:${locked?'0.3':'1'}">Reject</button>
      </div>
    </div>`;
  });
  taskPanelEl.innerHTML=html;

  taskPanelEl.querySelectorAll("button[data-id]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if(btn.disabled) return;
      const tid=btn.dataset.id, act=btn.dataset.act;
      await fetch(`/api/tasks/${tid}/${act}`,{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({operator: MY_OPERATOR_ID, operator_id: MY_OPERATOR_ID})});
    });
  });
}

function pushTask(task) {
  if(!task?.id) return;
  if(task.status==="PENDING") pendingTasks.set(task.id, task);
  _renderTaskPanel();
}

function updateTask(task) {
  if(!task?.id) return;
  if(task.status!=="PENDING") {
    pendingTasks.delete(task.id);
    // Auto-close approval modal if it was showing this task
    if (_engModal.taskId === task.id) _closeEngModal();
  } else {
    pendingTasks.set(task.id, task);
  }
  _renderTaskPanel();

  // Flash task panel briefly
  if(taskPanelEl){
    const c=task.status==="APPROVED"?"#27ae60":"#e74c3c";
    taskPanelEl.style.outline=`2px solid ${c}`;
    setTimeout(()=>{taskPanelEl.style.outline="none";},600);
  }
}

/* ── Phase 3: Asset panel ────────────────────────────────── */
let assetPanelEl=null;
let assetPlacingType=null;

function mountAssetPanel() {
  assetPanelEl = el("div", { id:"asset-panel", style:{ width:"100%" } });

  const mkBtn=(label,type,colorClass)=>el("button",{
    class:`nz-btn ${colorClass}`, style:{width:"100%",marginBottom:"4px"},
    onclick:()=>startAssetPlace(type)
  },[label]);

  const header = el("div", { class:"nz-section" }, ["Place Asset"]);
  const hint = el("div",{style:{marginTop:"4px",color:"var(--text-3)",fontSize:"10px"}},["Click map to place"]);

  const writeSect = el("div", { class: "nz-write-ctrl" });
  writeSect.appendChild(mkBtn("+ Friendly","friendly","c-ok"));
  writeSect.appendChild(mkBtn("+ Hostile","hostile","c-danger"));
  writeSect.appendChild(el("button",{class:"nz-btn",style:{width:"100%",marginBottom:"4px"},onclick:()=>startAssetPlace("unknown")},["+ Unknown"]));
  writeSect.appendChild(hint);

  assetPanelEl.appendChild(header);
  assetPanelEl.appendChild(writeSect);
  RIGHT_TABS.assets.appendChild(assetPanelEl);

  // Map click → place asset
  UI.map.on("click", async e => {
    if(!assetPlacingType) return;
    const {lat,lng}=e.latlng;
    const type=assetPlacingType;
    assetPlacingType=null;
    UI.map.getContainer().style.cursor="";
    hint.textContent="Click map to place";
    await fetch("/api/assets",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({type,lat,lon:lng,name:`${type[0].toUpperCase()}-${Date.now()%10000}`})});
  });

  function startAssetPlace(type) {
    assetPlacingType=type;
    UI.map.getContainer().style.cursor="crosshair";
    hint.textContent=`Click map to place ${type}`;
  }
}

/* ── Phase 3: Mission planning panel ────────────────────── */
let missionPanelEl=null, missionDrawing=false, missionOrder=0;

function mountMissionPanel() {
  missionPanelEl = el("div",{id:"mission-panel",style:{ width:"100%", marginTop:"6px" }});

  const hint=el("div",{style:{color:"var(--text-3)",fontSize:"10px",marginTop:"4px"}},["Click map to add waypoints"]);
  const btnStart=el("button",{class:"nz-btn c-ok",  style:{marginRight:"4px"},onclick:startMission},["Plan Mission"]);
  const btnDone =el("button",{class:"nz-btn",style:{marginRight:"4px",display:"none"},onclick:stopMission},["Done"]);
  const btnClear=el("button",{class:"nz-btn c-danger",onclick:async()=>{
    await fetch("/api/waypoints",{method:"DELETE"}); clearWaypoints(); missionOrder=0;
  }},["Clear"]);

  missionPanelEl.appendChild(el("div",{class:"nz-section"},["Mission Planning"]));
  missionPanelEl.appendChild(el("div",{style:{display:"flex",gap:"4px",flexWrap:"wrap"}},[btnStart,btnDone,btnClear]));
  missionPanelEl.appendChild(hint);
  RIGHT_TABS.assets.appendChild(missionPanelEl);

  UI.map.on("click", async e => {
    if(!missionDrawing) return;
    const {lat,lng}=e.latlng;
    missionOrder++;
    await fetch("/api/waypoints",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({lat,lon:lng,order:missionOrder,name:`WP-${missionOrder}`})});
  });

  function startMission() {
    missionDrawing=true;
    btnStart.style.display="none"; btnDone.style.display="";
    UI.map.getContainer().style.cursor="crosshair";
    hint.textContent="Click map to add waypoints";
  }
  function stopMission() {
    missionDrawing=false;
    btnStart.style.display=""; btnDone.style.display="none";
    UI.map.getContainer().style.cursor="";
    hint.textContent=`${UI.waypointMarkers.size} waypoints`;
  }
}

/* ── Pause/Resume Engine ──────────────────────────────────── */
const CopEngine = (() => {
  let mode="LIVE", buffer=[];
  function setModeInternal(m){mode=m;setMode(m);}
  function route(ev) {
    if(!ev?.event_type) return;
    switch(ev.event_type){
      case "cop.snapshot":        return applySnapshot(ev.payload);
      case "cop.ai_update":       return applyAIUpdate(ev.payload);
      case "cop.ping":            return;  // heartbeat, no-op
      case "cop.track":           return upsertTrack(ev.payload);
      case "cop.threat":          return upsertThreat(ev.payload);
      case "cop.zone":            return upsertZone(ev.payload);
      case "cop.zone_removed":    return removeZone(ev.payload?.id);
      case "cop.alert":           return pushAlert(ev.payload);
      case "cop.asset":           return upsertAsset(ev.payload);
      case "cop.asset_removed":   return removeAsset(ev.payload?.id);
      case "cop.task":            return pushTask(ev.payload);
      case "cop.task_update":     return updateTask(ev.payload);
      case "cop.effector_impact":  return playEffectorImpact(ev.payload);
      case "cop.track_removed":    return removeTrack(ev.payload?.id);
      case "cop.operator_joined":  return applyOperatorJoined(ev.payload);
      case "cop.operator_left":    return applyOperatorLeft(ev.payload);
      case "cop.track_claimed":    return applyTrackClaimed(ev.payload);
      case "cop.track_released":   return applyTrackReleased(ev.payload);
      case "cop.waypoint":        return upsertWaypoint(ev.payload);
      case "cop.waypoint_removed":return removeWaypoint(ev.payload?.id);
      case "cop.waypoints_cleared":return clearWaypoints();
      case "cop.ew_alert":        return pushEWAlert(ev.payload);
      case "cop.escalation":      return pushEscalation(ev.payload);
      case "cop.bft_warning":        return pushBFTWarning(ev.payload);
      case "cop.jam_active":         return playNLEffect({...ev.payload, action:"JAM"}, "#f1c40f");
      case "cop.spoof_active":       return playNLEffect({...ev.payload, action:"SPOOF"}, "#1abc9c");
      case "cop.ew_suppress_active": return playNLEffect({...ev.payload, action:"EW"}, "#9b59b6");
      case "cop.effector_outcome":   return pushEffectorOutcome(ev.payload);
      case "cop.effector_status":    return applyEffectorStatus(ev.payload);
      case "cop.track_merged":       return pushTrackMerged(ev.payload);
      case "cop.annotation":      return _wsAnnotation(ev.payload);
      case "cop.annotation_removed": return _wsAnnotationRemoved(ev.payload);
    }
  }
  function onEvent(ev){
    if(mode==="LIVE"){route(ev);return;}
    buffer.push(ev);
    if(buffer.length>UI.bufferMax) buffer.shift();
    setBufferSize(buffer.length);
  }
  function pause(){if(mode!=="LIVE")return;setModeInternal("PAUSED");setStatus("WS: paused");}
  async function resume(fetchFn){
    if(mode!=="PAUSED")return;
    setModeInternal("RESUMING");setStatus("WS: resuming...");
    try{const s=await fetchFn();if(s)applySnapshot(s);}catch(e){console.warn(e);}
    buffer.forEach(route);buffer=[];setBufferSize(0);
    setModeInternal("LIVE");setStatus("WS: connected (live)");
  }
  function clearBuffer(){buffer=[];setBufferSize(0);}
  return {onEvent,pause,resume,clearBuffer};
})();
window.CopEngine=CopEngine;

/* ── REST helpers ─────────────────────────────────────────── */
async function fetchCompositeSnapshot() {
  const [tracks,threats]=await Promise.all([
    fetch("/api/tracks").then(r=>r.json()),
    fetch("/api/threats").then(r=>r.json()).catch(()=>null),
  ]);
  return {tracks:tracks?.tracks??tracks,threats:threats?.threats??threats};
}
async function hardReset(){
  await fetch("/api/reset",{method:"POST"});
  try{const s=await fetchCompositeSnapshot();applySnapshot(s);}catch{}
}

/* ── WebSocket ────────────────────────────────────────────── */
/* Back-off, reconnect, and message parsing → modules/ws-client.js */
function connectWS() {
  const { wsClient } = window.NIZAM;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url   = `${proto}://${location.host}/ws?operator_id=${encodeURIComponent(MY_OPERATOR_ID)}`;
  wsClient.connect(
    url,
    ev  => CopEngine.onEvent(ev),
    txt => setStatus(txt === "WS: connected"
      ? `WS: connected (live) \u00b7 ${MY_OPERATOR_ID}`
      : txt),
    ok  => { UI.wsConnected = ok; },
  );
}

/* ── Zone draw panel ─────────────────────────────────────── */
let zoneDrawPoints=[],zoneDrawMarkers=[],zoneDrawing=false;
function mountZonePanel(){
  const panel=el("div",{style:{ width:"100%" }});
  const typeSelect=el("select",{class:"nz-select",style:{marginBottom:"6px"}});
  ["restricted","kill","friendly"].forEach(t=>typeSelect.appendChild(el("option",{value:t},[t])));
  const nameInput=el("input",{type:"text",placeholder:"Zone name...",class:"nz-input",style:{marginBottom:"6px"}});
  const btnDraw  =el("button",{class:"nz-btn",  style:{marginRight:"4px"}},["Draw Zone"]);
  const btnSave  =el("button",{class:"nz-btn c-ok", style:{marginRight:"4px",display:"none"}},["Save"]);
  const btnCancel=el("button",{class:"nz-btn c-danger",style:{display:"none"}},["Cancel"]);
  const hint     =el("div",{style:{marginTop:"4px",color:"var(--text-3)",fontSize:"10px"}},[""]);
  btnDraw.addEventListener("click",()=>{
    zoneDrawPoints=[];zoneDrawMarkers=[];zoneDrawing=true;
    btnDraw.style.display="none";btnSave.style.display="";btnCancel.style.display="";
    hint.textContent="Click map to add points (min 3)";
    UI.map.getContainer().style.cursor="crosshair";
  });
  btnCancel.addEventListener("click",cancelDraw);
  btnSave.addEventListener("click",async()=>{
    if(zoneDrawPoints.length<3){hint.textContent="Need at least 3 points!";return;}
    const zone={id:"zone-"+Date.now(),name:nameInput.value||typeSelect.value+"-zone",
      type:typeSelect.value,coordinates:[...zoneDrawPoints]};
    await fetch("/api/zones",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(zone)});
    cancelDraw();
  });
  panel.appendChild(el("div",{class:"nz-section"},["Draw Zone"]));
  panel.appendChild(nameInput);panel.appendChild(typeSelect);
  panel.appendChild(el("div",{class:"nz-write-ctrl",style:{display:"flex",gap:"4px"}},[btnDraw,btnSave,btnCancel]));
  panel.appendChild(hint);
  RIGHT_TABS.zones.appendChild(panel);
  UI.map.on("click",e=>{
    if(!zoneDrawing||assetPlacingType||missionDrawing) return;
    const{lat,lng}=e.latlng;
    zoneDrawPoints.push([lat,lng]);
    const m=L.circleMarker([lat,lng],{radius:5,color:"#f39c12",fillOpacity:1}).addTo(UI.map);
    zoneDrawMarkers.push(m);
    hint.textContent=`${zoneDrawPoints.length} points (Save when done)`;
  });
}
function cancelDraw(){
  zoneDrawing=false;zoneDrawPoints=[];
  zoneDrawMarkers.forEach(m=>{try{m.remove();}catch{}});zoneDrawMarkers=[];
  document.querySelectorAll("button").forEach(b=>{
    if(b.textContent==="Draw Zone")b.style.display="";
    if(b.textContent==="Save"||b.textContent==="Cancel")b.style.display="none";
  });
  if(UI.map)UI.map.getContainer().style.cursor="";
}

/* ── Agent health panel ──────────────────────────────────── */
let agentPanelEl=null;
function mountAgentPanel(){
  agentPanelEl=el("div",{id:"agent-panel",style:{
    position:"fixed",bottom:"12px",left:"12px",zIndex:"9999",
    background:"rgba(0,0,0,0.65)",color:"white",
    padding:"8px 12px",borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial",fontSize:"11px",
    lineHeight:"1.5",minWidth:"180px",
  }});
  agentPanelEl.innerHTML="<b>Agents</b><br><span style='opacity:.6'>connecting...</span>";
  document.body.appendChild(agentPanelEl);
}
async function refreshAgentHealth(){
  if(!agentPanelEl) return;
  try{
    const r=await fetch("/api/orchestrator/health");
    if(!r.ok) throw new Error(r.status);
    const d=await r.json();
    const agents=d.agents??[];
    let html=`<b>Agents</b> <span style='opacity:.6'>${d.alive}/${d.total} alive</span><br>`;
    agents.forEach(a=>{
      const c=a.status==="ALIVE"?"#27ae60":"#e74c3c";
      const met=a.metrics?.events_sent!=null?` (${a.metrics.events_sent} ev)`:"";
      html += '<span style="color:' + c + '">\u25CF</span> ' + a.name + met + '<br>';
    });
    agentPanelEl.innerHTML=html;
  }catch{
    agentPanelEl.innerHTML="<b>Agents</b><br><span style='opacity:.5'>orchestrator offline</span>";
  }
}

/* ==========================================================
   Phase 5: AI Decision Support UI
   ========================================================== */

/* ── AI: Predicted trajectory lines ─────────────────────── */
const predictionLines = new Map(); // track_id -> L.polyline

function drawPredictions(predictions) {
  // predictions: {track_id: [{lat,lon,time_ahead_s}, ...]}
  if(!predictions || !UI.map) return;
  // Clear old
  for(const [id,pl] of predictionLines) {
    if(!predictions[id]) { try{pl.remove();}catch{} predictionLines.delete(id); }
  }
  for(const [tid, pts] of Object.entries(predictions)) {
    if(!pts || !pts.length) continue;
    const track = UI.tracks.get(tid);
    if(!track) continue;
    const trackLL = getLL(track);
    if(!trackLL) continue;
    const lls = [trackLL, ...pts.map(p => [p.lat, p.lon])];
    const ex = predictionLines.get(tid);
    if(!ex) {
      const pl = L.polyline(lls, {
        color: "#00e5ff", weight: 2, opacity: 0.6,
        dashArray: "3 6", className: "prediction-line"
      }).addTo(UI.map);
      pl.bindTooltip(`Predicted: ${tid} (+${pts[pts.length-1]?.time_ahead_s}s)`,
                     {sticky:true, opacity:0.8});
      predictionLines.set(tid, pl);
    } else {
      ex.setLatLngs(lls);
    }
  }
}

/* ── AI: LSTM Trajectory predictions ───────────────────── */
const trajectoryLines = new Map();  // track_id -> L.polyline

function drawTrajectories(trajectories) {
  // trajectories: {track_id: [{lat,lon,step,t}, ...]}
  if (!trajectories || !UI.map) return;

  // Remove stale lines
  for (const [id, pl] of trajectoryLines) {
    if (!trajectories[id]) { try { pl.remove(); } catch {} trajectoryLines.delete(id); }
  }

  for (const [tid, pts] of Object.entries(trajectories)) {
    if (!pts || !pts.length) continue;
    const track = UI.tracks.get(tid);
    if (!track) continue;
    const trackLL = getLL(track);
    if (!trackLL) continue;

    const threat = UI.threats.get(tid);
    const level  = threat?.threat_level ?? track.threat_level ?? "LOW";
    const baseColor = THREAT_COLORS[level] ?? "#2980b9";

    // Build gradient-fading segments (full line + faded tip)
    const lls = [trackLL, ...pts.map(p => [p.lat, p.lon])];

    const ex = trajectoryLines.get(tid);
    if (!ex) {
      const pl = L.polyline(lls, {
        color: baseColor, weight: 2.5, opacity: 0.55,
        dashArray: "6 4",
      }).addTo(UI.map);
      pl.bindTooltip(
        `LSTM Trajectory: ${tid}<br>${pts.length} steps ahead`,
        { sticky: true, opacity: 0.85 }
      );
      trajectoryLines.set(tid, pl);
    } else {
      ex.setLatLngs(lls);
      ex.setStyle({ color: baseColor });
    }
  }
}

/* ── AI: Uncertainty cones ─────────────────────────────── */
const uncertaintyCones = new Map(); // track_id -> L.polygon

function drawUncertaintyCones(cones) {
  // cones: {track_id: [{lat,lon,sigma_lat_m,sigma_lon_m,time_ahead_s}, ...]}
  if(!cones || !UI.map) return;
  // Remove stale cones
  for(const [id, poly] of uncertaintyCones) {
    if(!cones[id]) { try{poly.remove();}catch{} uncertaintyCones.delete(id); }
  }
  for(const [tid, pts] of Object.entries(cones)) {
    if(!pts || pts.length < 2) continue;
    const track = UI.tracks.get(tid);
    if(!track) continue;
    const trackLL = getLL(track);
    if(!trackLL) continue;

    // Build cone polygon: upper edge -> reversed lower edge
    const DEG_PER_M = 1.0 / 111320.0;
    const upper = [trackLL];
    const lower = [trackLL];
    for(const pt of pts) {
      const sLatDeg = (pt.sigma_lat_m || 0) * DEG_PER_M * 2; // 2-sigma
      const cosLat = Math.cos(pt.lat * Math.PI / 180);
      const sLonDeg = (pt.sigma_lon_m || 0) * DEG_PER_M / (cosLat || 1) * 2;
      upper.push([pt.lat + sLatDeg, pt.lon + sLonDeg]);
      lower.push([pt.lat - sLatDeg, pt.lon - sLonDeg]);
    }
    const coneCoords = [...upper, ...lower.reverse()];

    const ex = uncertaintyCones.get(tid);
    if(!ex) {
      const poly = L.polygon(coneCoords, {
        color: "#00e5ff", fillColor: "#00e5ff",
        fillOpacity: 0.08, weight: 1, opacity: 0.25,
        dashArray: "2 4", interactive: false,
      }).addTo(UI.map);
      uncertaintyCones.set(tid, poly);
    } else {
      ex.setLatLngs(coneCoords);
    }
  }
}

/* ── AI: Predictive breach panel ──────────────────────── */
let breachPanelEl = null;

function mountBreachPanel() {
  breachPanelEl = el("div", { id:"breach-panel", class:"nz-card", style:{
    padding:"8px 10px", maxHeight:"180px", overflowY:"auto",
  }});
  breachPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Ihlal tahmini yok</span>`;
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u26A0 Predictive Breach", breachPanelEl, "breach"));
}

function renderBreachPanel(breaches) {
  if (!breachPanelEl) return;
  /* HTML building → modules/panels.js */
  breachPanelEl.innerHTML = NIZAM.panels.buildBreachHtml(breaches);
  if (breaches && breaches.length) {
    breachPanelEl.style.borderColor = "#e74c3c";
    setTimeout(() => { breachPanelEl.style.borderColor = "rgba(231,76,60,0.4)"; }, 1200);
  } else {
    breachPanelEl.style.borderColor = "rgba(231,76,60,0.4)";
  }
}

/* ── AI: ROE Advisory panel ────────────────────────────── */
/* ── ML Threat Panel ────────────────────────────────────── */
let mlPanelEl = null;
let mlModelAvailable = false;

function mountMLPanel() {
  mlPanelEl = el("div", { id:"ml-panel", class:"nz-card", style:{
    padding:"8px 10px", maxHeight:"220px", overflowY:"auto",
  }});
  mlPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Bekleniyor...</span>`;
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u2699 ML Threat", mlPanelEl, "ml"));
}

function renderMLPanel(preds) {
  if (!mlPanelEl) return;
  /* HTML building → modules/panels.js */
  mlPanelEl.innerHTML = NIZAM.panels.buildMLHtml(preds, mlModelAvailable);
}

let roePanelEl = null;

/* ROE_COLORS, ROE_LABELS → modules/constants.js */

function mountROEPanel() {
  roePanelEl = el("div", { id:"roe-panel", class:"nz-card", style:{
    padding:"8px 10px", maxHeight:"200px", overflowY:"auto",
  }});
  roePanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Angajman yok</span>`;
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u2694 ROE Advisory", roePanelEl, "roe"));
}

function renderROEPanel(advisories) {
  if (!roePanelEl) return;
  /* HTML building → modules/panels.js */
  const { html, hasCritical } = NIZAM.panels.buildROEHtml(advisories);
  roePanelEl.innerHTML = html;
  if (hasCritical) {
    roePanelEl.style.borderColor = "#e74c3c";
    setTimeout(() => { roePanelEl.style.borderColor = "rgba(155,89,182,0.4)"; }, 1500);
  }
}

/* ── AI: Confidence Scores panel ──────────────────────── */
let confPanelEl = null;
/* CONF_GRADE_COLORS → modules/constants.js */

function mountConfidencePanel() {
  confPanelEl = el("div", { id:"conf-panel", class:"nz-card", style:{
    padding:"8px 10px", maxHeight:"200px", overflowY:"auto",
  }});
  confPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Bekleniyor...</span>`;
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u{1F4CA} G\u00fcven Skoru", confPanelEl, "conf"));
}

function renderConfidencePanel(scores) {
  if (!confPanelEl) return;
  /* HTML building → modules/panels.js */
  confPanelEl.innerHTML = NIZAM.panels.buildConfidenceHtml(scores);
}

/* ── AI: Coordinated Attack panel + convergence lines ──── */
let coordPanelEl = null;
const convergenceMarkers = new Map(); // key -> L.circleMarker
const convergenceLines   = new Map(); // key -> [L.polyline, ...]

function mountCoordPanel() {
  coordPanelEl = el("div", { id:"coord-panel", class:"nz-card", style:{
    padding:"8px 10px", maxHeight:"180px", overflowY:"auto",
    borderLeft:"3px solid rgba(255,0,80,0.4)",
  }});
  coordPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Koordineli tehdit yok</span>`;
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u2694 Koordineli Sald\u0131r\u0131", coordPanelEl, "coord"));
}

function renderCoordPanel(attacks) {
  if(!coordPanelEl) return;
  // Clean stale map markers/lines
  const activeKeys = new Set();

  if(!attacks || attacks.length === 0) {
    coordPanelEl.innerHTML = "<b>\u2694 Coordinated Attack</b><br><span style='opacity:.5'>No coordinated threats</span>";
    coordPanelEl.style.borderColor = "rgba(255,0,80,0.4)";
    // Remove all convergence visuals
    for(const [k,m] of convergenceMarkers) { try{m.remove();}catch{} }
    convergenceMarkers.clear();
    for(const [k,lines] of convergenceLines) { lines.forEach(l=>{try{l.remove();}catch{}}); }
    convergenceLines.clear();
    return;
  }

  let html = `<b>\u2694 Coordinated Attack</b> <span style="opacity:.6">${attacks.length} warning(s)</span><br>`;
  attacks.slice(0, 5).forEach((a, idx) => {
    const key = `coord-${idx}-${a.track_ids.join(",")}`;
    activeKeys.add(key);

    const subtypeColors = {
      PINCER:"#ff0050", CONVERGENCE:"#ff6600",
      ZONE_PINCER:"#ff0050", ZONE_CONVERGE:"#ff6600",
      ASSET_PINCER:"#ff0050", ASSET_CONVERGE:"#ff6600",
    };
    const color = subtypeColors[a.subtype] || "#ff6600";
    const badge = a.subtype.includes("PINCER")
      ? `<span style="background:#ff0050;padding:0 4px;border-radius:3px;font-size:8px">PINCER</span>`
      : `<span style="background:#ff6600;padding:0 4px;border-radius:3px;font-size:8px">CONVERGE</span>`;

    const targetInfo = a.target_name ? ` \u2192 ${a.target_name}` : "";
    html += `<div style="border-left:3px solid ${color};padding-left:5px;margin:3px 0">
      <span style="color:${color};font-weight:bold">${a.count} tracks${targetInfo}</span> ${badge}<br>
      <span style="font-size:9px;opacity:.8">\u23F1 ${a.time_to_convergence_s}s | ${a.angular_spread_deg}\u00B0 spread | ${a.track_ids.join(", ")}</span>
    </div>`;

    // Draw convergence point marker on map
    if(UI.map && a.convergence_lat && a.convergence_lon) {
      const existing = convergenceMarkers.get(key);
      if(!existing) {
        const marker = L.circleMarker([a.convergence_lat, a.convergence_lon], {
          radius: 12, color: color, fillColor: color,
          fillOpacity: 0.3, weight: 2, dashArray: "4 4",
        }).addTo(UI.map);
        marker.bindTooltip(`Convergence: ${a.count} tracks, ${a.time_to_convergence_s}s`, {permanent:false});
        convergenceMarkers.set(key, marker);
      } else {
        existing.setLatLng([a.convergence_lat, a.convergence_lon]);
      }

      // Draw lines from each track's current position to convergence point
      const lines = [];
      (a.track_ids || []).forEach(tid => {
        const track = UI.tracks.get(tid);
        if(track) {
          const tll = getLL(track);
          if(tll) {
            const line = L.polyline([tll, [a.convergence_lat, a.convergence_lon]], {
              color: color, weight: 1.5, opacity: 0.5,
              dashArray: "6 4", interactive: false,
            }).addTo(UI.map);
            lines.push(line);
          }
        }
      });
      // Remove old lines for this key
      const oldLines = convergenceLines.get(key);
      if(oldLines) oldLines.forEach(l=>{try{l.remove();}catch{}});
      convergenceLines.set(key, lines);
    }
  });

  // Remove stale markers/lines
  for(const [k,m] of convergenceMarkers) {
    if(!activeKeys.has(k)) { try{m.remove();}catch{} convergenceMarkers.delete(k); }
  }
  for(const [k,lines] of convergenceLines) {
    if(!activeKeys.has(k)) { lines.forEach(l=>{try{l.remove();}catch{}}); convergenceLines.delete(k); }
  }

  coordPanelEl.innerHTML = html;
  _intel.coord = attacks.length;
  updateThreatIntelCard();
  // Flash border on new attacks
  coordPanelEl.style.borderColor = "#ff0050";
  setTimeout(() => { coordPanelEl.style.borderColor = "rgba(255,0,80,0.4)"; }, 1200);
}

/* ── AI: Anomaly panel ──────────────────────────────────── */
let anomalyPanelEl = null;
const anomalyLog = [];
const MAX_ANOMALY_LOG = 30;

/* ANOMALY_COLORS → modules/constants.js */

function mountAnomalyPanel() {
  const body = el("div", { class:"nz-card", style:{
    padding:"8px 10px", width:"100%",
  }});

  // Stats header
  const statsRow = el("div", { id:"anomaly-stats", style:{
    display:"flex", gap:"4px", flexWrap:"wrap", marginBottom:"6px",
  }});
  statsRow.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali bekleniyor...</span>`;

  // Event list
  anomalyPanelEl = el("div", { id:"anomaly-panel", style:{
    width:"100%", display:"flex", flexDirection:"column", gap:"3px",
    maxHeight:"200px", overflowY:"auto",
  }});
  anomalyPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali yok</span>`;

  body.appendChild(statsRow);
  body.appendChild(anomalyPanelEl);
  RIGHT_TABS.threats.appendChild(_makeCollapsible("\u{1F6A8} Anomaliler", body, "anomaly"));
}

function renderAnomalyPanel() {
  if (!anomalyPanelEl) return;

  // Update stats row
  const statsEl = document.getElementById("anomaly-stats");
  if (statsEl && anomalyLog.length > 0) {
    const counts = {};
    anomalyLog.forEach(a => { counts[a.severity] = (counts[a.severity] || 0) + 1; });
    const SEV_COLOR = { CRITICAL:"var(--danger)", HIGH:"var(--warn)", MEDIUM:"#f1c40f", LOW:"var(--ok)" };
    statsEl.innerHTML = Object.entries(counts).map(([sev, n]) => {
      const c = SEV_COLOR[sev] || "var(--text-2)";
      return `<span style="background:rgba(255,255,255,0.05);border:1px solid ${c}44;color:${c};
        font-size:9px;padding:1px 6px;border-radius:10px;font-weight:600">${sev} ${n}</span>`;
    }).join("") + `<span style="color:var(--text-3);font-size:9px;margin-left:2px">toplam ${anomalyLog.length}</span>`;
  } else if (statsEl) {
    statsEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali yok</span>`;
  }

  if (anomalyLog.length === 0) {
    anomalyPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali algilandi yogunda burada gorunecek</span>`;
    return;
  }

  const TYPE_ICON = { SPEED_SPIKE:"SPD", HEADING_REVERSAL:"HDG", INTENT_SHIFT:"INT", SWARM_DETECTED:"SWM" };
  const SEV_COLOR = { CRITICAL:"var(--danger)", HIGH:"var(--warn)", MEDIUM:"#f1c40f", LOW:"var(--ok)" };
  const SEV_BG    = { CRITICAL:"rgba(240,64,64,0.10)", HIGH:"rgba(240,128,48,0.10)", MEDIUM:"rgba(241,196,15,0.07)", LOW:"rgba(48,192,96,0.07)" };

  let html = "";
  anomalyLog.slice(0, 20).forEach((a, i) => {
    const c   = SEV_COLOR[a.severity] || "var(--text-2)";
    const bg  = SEV_BG[a.severity]   || "rgba(255,255,255,0.04)";
    const ico = TYPE_ICON[a.type]    || a.type.slice(0, 3);
    const tid = a.track_id || (a.track_ids||[]).join(",") || "";
    html += `<div style="padding:5px 7px;border-radius:5px;background:${bg};border-left:3px solid ${c}${i===0?";animation:slide-in-right .2s ease":""}">
      <div style="display:flex;align-items:center;gap:5px">
        <span style="color:${c};font-size:9px;font-weight:700;letter-spacing:.06em;font-family:var(--mono)">${ico}</span>
        <span style="color:var(--text-2);font-size:10px;flex:1">${a.type.replace(/_/g," ")}</span>
        ${tid ? `<span style="color:var(--text-3);font-size:9px">${escHtml(tid)}</span>` : ""}
        <span style="color:${c};font-size:8px;opacity:.8">${a.severity}</span>
      </div>
      <div style="color:var(--text-3);font-size:10px;margin-top:2px;line-height:1.4">${escHtml((a.detail||a.message||"").slice(0,90))}</div>
    </div>`;
  });
  anomalyPanelEl.innerHTML = html;
}

function pushAnomalies(anomalies) {
  if (!anomalies || !anomalies.length) return;
  for (const a of anomalies) anomalyLog.unshift(a);
  while (anomalyLog.length > MAX_ANOMALY_LOG) anomalyLog.pop();
  renderAnomalyPanel();
  updateThreatIntelCard();
  setTabBadge("threats", anomalyLog.filter(a => a.severity === "CRITICAL" || a.severity === "HIGH").length);
}

/* ── Metrics panel — live /api/metrics polling ──────────── */
let metricsPanelEl = null;
let metricsTimer  = null;

/* _fmtMs, _fmtNum → modules/utils.js (fmtMs, fmtNum) */

function _metricsBar(label, val, max, color) {
  const pct = Math.max(0, Math.min(100, (val / max) * 100));
  return `<div style="margin:3px 0">
    <div style="display:flex;justify-content:space-between;font-size:9px;opacity:.8">
      <span>${label}</span><span>${val == null ? "—" : (+val).toFixed(0)} ms</span>
    </div>
    <div style="background:rgba(255,255,255,0.08);height:5px;border-radius:3px;overflow:hidden">
      <div style="background:${color};height:100%;width:${pct}%;transition:width .3s"></div>
    </div>
  </div>`;
}

function mountMetricsPanel() {
  metricsPanelEl = el("div", { id:"metrics-panel", style:{ width:"100%" } });
  metricsPanelEl.innerHTML = `<div class="nz-section">Server Metrics</div><div style="color:var(--text-3);font-size:11px">Loading...</div>`;
  RIGHT_TABS.metrics.appendChild(metricsPanelEl);

  // Start polling
  refreshMetrics();
  metricsTimer = setInterval(refreshMetrics, 2000);
}

async function refreshMetrics() {
  if (!metricsPanelEl) return;
  try {
    const r = await fetch("/api/metrics", {
      headers: AUTH_TOKEN ? { Authorization: `Bearer ${AUTH_TOKEN}` } : {},
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const m = await r.json();
    renderMetricsPanel(m);
  } catch (e) {
    metricsPanelEl.innerHTML = `<b>📊 Server Metrics</b><br>
      <span style='color:#e74c3c;font-size:9px'>fetch failed: ${e.message}</span>`;
  }
}

function renderMetricsPanel(m) {
  if (!metricsPanelEl) return;
  /* HTML building → modules/panels.js */
  metricsPanelEl.innerHTML = NIZAM.panels.buildMetricsHtml(m);
}

/* ── AI: Tactical recommendations panel ─────────────────── */
/* TAC_ICONS, TAC_COLORS → modules/panels.js */
let tacPanelEl = null;

function mountTacticalPanel() {
  tacPanelEl = el("div", { id:"tac-panel", style:{
    position:"fixed", bottom:"12px", left:"220px", zIndex:"9999",
    background:"rgba(0,0,0,0.78)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", minWidth:"260px", maxWidth:"340px",
    maxHeight:"220px", overflowY:"auto",
  }});
  tacPanelEl.innerHTML = "<b>AI Tactical</b><br><span style='opacity:.5'>No recommendations</span>";
  document.body.appendChild(tacPanelEl);
}

function renderTacticalPanel(recs) {
  if (!tacPanelEl) return;
  /* HTML building → modules/panels.js */
  tacPanelEl.innerHTML = NIZAM.panels.buildTacticalHtml(recs);
}

/* ── AI: LLM Chat panel ───────────────────────────────────── */
const chatHistory = [];

function mountAIAdvisorPanel() {
  const container = el("div", { style: { width:"100%", display:"flex", flexDirection:"column", gap:"6px" } });

  // ── Briefing section ──────────────────────────────────────
  const briefSection = el("div", { class:"nz-card", style:{ padding:"8px 10px" } });
  const briefHeader = el("div", { style:{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:"6px" } }, [
    el("span", { class:"nz-section", style:{ margin:0 } }, ["DURUM BRIFINGi"]),
    el("button", { class:"nz-btn", style:{ fontSize:"10px", padding:"3px 10px" },
      onclick: () => getBriefing(briefText) }, ["Al"]),
  ]);
  const briefText = el("div", { id:"ai-brief-text", style:{
    fontSize:"11px", lineHeight:"1.6", color:"var(--text-2)",
    maxHeight:"120px", overflowY:"auto", whiteSpace:"pre-wrap",
    fontFamily:"var(--mono)",
  }});
  briefText.textContent = "Brifing almak icin \"Al\" butonuna basin.";
  briefSection.appendChild(briefHeader);
  briefSection.appendChild(briefText);

  // ── Chat section ──────────────────────────────────────────
  const chatSection = el("div", { class:"nz-card", style:{ padding:"8px 10px", flex:"1" } });
  const chatLabel = el("div", { class:"nz-section", style:{ margin:"0 0 6px 0" } }, ["OPERATOR CHAT"]);

  const msgArea = el("div", { id:"chat-messages", style:{
    fontSize:"11px", lineHeight:"1.5", maxHeight:"200px", overflowY:"auto",
    marginBottom:"8px", display:"flex", flexDirection:"column", gap:"4px",
  }});
  msgArea.innerHTML = "<span style='color:var(--text-3)'>AI danismana soru sorun...</span>";

  const inputRow = el("div", { style:{ display:"flex", gap:"4px" } });
  const chatInput = el("input", { type:"text", class:"nz-input",
    placeholder:"Soru veya komut...",
    style:{ flex:"1", fontSize:"11px", padding:"5px 8px" } });
  const sendBtn = el("button", { class:"nz-btn", style:{ fontSize:"10px", padding:"5px 10px", whiteSpace:"nowrap" },
    onclick: () => sendChat(chatInput, msgArea) }, ["Gonder"]);

  chatInput.addEventListener("keydown", e => { if (e.key === "Enter") sendChat(chatInput, msgArea); });

  inputRow.appendChild(chatInput);
  inputRow.appendChild(sendBtn);
  chatSection.appendChild(chatLabel);
  chatSection.appendChild(msgArea);
  chatSection.appendChild(inputRow);

  // ── Command parsing section ───────────────────────────────
  const cmdSection = el("div", { class:"nz-card", style:{ padding:"8px 10px" } });
  const cmdLabel = el("div", { class:"nz-section", style:{ margin:"0 0 6px 0" } }, ["DOGAL DIL KOMUT"]);
  const cmdResult = el("div", { id:"ai-cmd-result", style:{
    fontSize:"10px", lineHeight:"1.5", color:"var(--text-2)",
    maxHeight:"80px", overflowY:"auto", marginBottom:"6px",
    fontFamily:"var(--mono)", whiteSpace:"pre-wrap", display:"none",
  }});
  const cmdRow = el("div", { style:{ display:"flex", gap:"4px" } });
  const cmdInput = el("input", { type:"text", class:"nz-input",
    placeholder:"Ornek: Tum hostile assetleri observe et",
    style:{ flex:"1", fontSize:"11px", padding:"5px 8px" } });
  const cmdBtn = el("button", { class:"nz-btn", style:{ fontSize:"10px", padding:"5px 10px", whiteSpace:"nowrap" },
    onclick: () => parseCommand(cmdInput, cmdResult) }, ["Calistir"]);

  cmdInput.addEventListener("keydown", e => { if (e.key === "Enter") parseCommand(cmdInput, cmdResult); });

  cmdRow.appendChild(cmdInput);
  cmdRow.appendChild(cmdBtn);
  cmdSection.appendChild(cmdLabel);
  cmdSection.appendChild(cmdResult);
  cmdSection.appendChild(cmdRow);

  // ── LLM status indicator ──────────────────────────────────
  const statusBar = el("div", { id:"ai-llm-status", style:{
    fontSize:"10px", color:"var(--text-3)", textAlign:"center", paddingTop:"2px",
  }}, ["LLM: kontrol ediliyor..."]);
  checkLLMStatus(statusBar);

  container.appendChild(briefSection);
  container.appendChild(chatSection);
  container.appendChild(cmdSection);
  container.appendChild(statusBar);
  RIGHT_TABS.ai.appendChild(container);
}

async function checkLLMStatus(el) {
  try {
    const r = await fetch("/api/metrics");
    const d = await r.json();
    if (d.llm_enabled) {
      el.textContent = `LLM: AKTIF (${d.llm_provider || "?"})`;
      el.style.color = "var(--ok)";
    } else {
      el.textContent = "LLM: API KEY yok — kural tabanli mod";
      el.style.color = "var(--warn)";
    }
  } catch { el.textContent = "LLM: durum alinamadi"; }
}

function mountChatPanel() { /* replaced by mountAIAdvisorPanel */ }

async function sendChat(input, msgArea) {
  const q = input.value.trim();
  if(!q) return;
  input.value = "";

  // Show user message
  chatHistory.push({role:"user", text:q});
  renderChatMessages(msgArea);

  try {
    const resp = await fetch("/api/ai/chat", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({question: q}),
    });
    const data = await resp.json();
    const answer = data.answer || data.briefing || "Yanit alinamadi.";
    const badge = data.llm_used ? " [LLM]" : " [LOCAL]";
    chatHistory.push({role:"ai", text: answer + badge});
  } catch(e) {
    chatHistory.push({role:"ai", text:"Hata: " + e.message});
  }
  renderChatMessages(msgArea);
}

async function getBriefing(briefTextEl) {
  briefTextEl.textContent = "Aliyor...";
  briefTextEl.style.color = "var(--text-3)";
  try {
    const resp = await fetch("/api/ai/briefing");
    const data = await resp.json();
    const badge = data.llm_used ? " [LLM]" : " [LOCAL]";
    briefTextEl.textContent = (data.briefing || "Brifing alinamadi.") + badge;
    briefTextEl.style.color = "var(--text-1)";
  } catch(e) {
    briefTextEl.textContent = "Hata: " + e.message;
    briefTextEl.style.color = "var(--danger)";
  }
}

async function parseCommand(cmdInput, cmdResult) {
  const cmd = cmdInput.value.trim();
  if (!cmd) return;
  cmdInput.value = "";
  cmdResult.style.display = "block";
  cmdResult.style.color = "var(--text-3)";
  cmdResult.textContent = "Isliyor...";
  try {
    const resp = await fetch("/api/ai/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: cmd }),
    });
    const data = await resp.json();
    if (data.llm_used) {
      const actions = data.actions || [];
      if (actions.length > 0) {
        cmdResult.style.color = "var(--ok)";
        cmdResult.textContent = `[LLM] ${actions.length} aksiyon:\n` +
          actions.map(a => `  ${a.action} ${JSON.stringify(a.params || {})}`).join("\n");
      } else {
        cmdResult.style.color = "var(--warn)";
        cmdResult.textContent = "[LLM] Aksiyon ayristirilamadi:\n" + (data.explanation || "").slice(0, 200);
      }
    } else {
      cmdResult.style.color = "var(--warn)";
      cmdResult.textContent = data.explanation || "LLM API key yapilandirilmamis.";
    }
  } catch(e) {
    cmdResult.style.color = "var(--danger)";
    cmdResult.textContent = "Hata: " + e.message;
  }
}

function renderChatMessages(area) {
  let html = "";
  chatHistory.slice(-20).forEach(m => {
    if (m.role === "user") {
      html += `<div style="text-align:right">
        <span style="background:var(--bg-3);color:var(--text-1);padding:4px 8px;border-radius:8px;display:inline-block;max-width:90%;font-size:11px">${escHtml(m.text)}</span>
      </div>`;
    } else {
      html += `<div style="text-align:left">
        <span style="background:var(--bg-2);color:var(--text-2);padding:4px 8px;border-radius:8px;display:inline-block;max-width:90%;font-size:11px;white-space:pre-wrap;border:1px solid var(--border)">${escHtml(m.text)}</span>
      </div>`;
    }
  });
  area.innerHTML = html || "<span style='color:var(--text-3)'>AI danismana soru sorun...</span>";
  area.scrollTop = area.scrollHeight;
}

/* escHtml → modules/utils.js */

/* ── AI: toggle chat button (floating) ─────────────────── */
function mountChatToggle() { /* replaced by AI sidebar tab */ }

/* ── AI: Threat Timeline chart (canvas popup) ──────────── */
let timelinePopupEl = null;
let timelineCurrentTrack = null;

// Track annotation store: track_id → [annotation, ...]
const _ANNOTATIONS = new Map();

function mountTimelinePopup() {
  timelinePopupEl = el("div", { id:"timeline-popup", style:{
    display:"none", position:"fixed", bottom:"60px", left:"50%",
    transform:"translateX(-50%)", zIndex:"10001",
    background:"rgba(10,10,25,0.92)", color:"white",
    padding:"10px 14px", borderRadius:"12px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"11px",
    border:"1px solid rgba(100,150,255,0.3)",
    boxShadow:"0 4px 20px rgba(0,0,0,0.6)",
    minWidth:"420px", maxWidth:"520px",
  }});
  timelinePopupEl.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <b id="tl-title">Threat Timeline</b>
      <span id="tl-close" style="cursor:pointer;opacity:.6;font-size:14px">\u2715</span>
    </div>
    <canvas id="tl-canvas" width="480" height="140" style="width:100%;height:140px;border-radius:6px;background:rgba(0,0,0,0.3)"></canvas>
    <div id="tl-legend" style="margin-top:4px;opacity:.7;font-size:9px"></div>
    <div id="tl-ann-section" style="margin-top:10px;border-top:1px solid rgba(255,255,255,0.1);padding-top:8px">
      <div style="font-weight:600;margin-bottom:6px;font-size:11px;opacity:.8">&#x1F4AC; Operator Notes</div>
      <div id="tl-ann-list" style="max-height:100px;overflow-y:auto;margin-bottom:6px"></div>
      <div class="nz-write-ctrl" style="display:flex;gap:6px">
        <input id="tl-ann-input" type="text" placeholder="Add a note..." maxlength="500"
          style="flex:1;padding:5px 8px;border-radius:5px;border:1px solid rgba(255,255,255,0.15);
                 background:rgba(255,255,255,0.06);color:white;font-size:11px;outline:none"/>
        <button id="tl-ann-btn" style="padding:5px 10px;border-radius:5px;border:none;
          background:var(--accent);color:white;cursor:pointer;font-size:11px;font-weight:600">Add</button>
      </div>
    </div>
  `;
  document.body.appendChild(timelinePopupEl);
  document.getElementById("tl-close").addEventListener("click", () => {
    timelinePopupEl.style.display = "none";
    timelineCurrentTrack = null;
  });
  document.getElementById("tl-ann-btn").addEventListener("click", _tlAddAnnotation);
  document.getElementById("tl-ann-input").addEventListener("keydown", e => {
    if (e.key === "Enter") _tlAddAnnotation();
  });
}

function _tlRenderList(listEl, anns) {
  if (!listEl) return;
  if (!anns || anns.length === 0) {
    listEl.innerHTML = '<span style="opacity:.4;font-size:10px">No notes yet.</span>';
    return;
  }
  listEl.innerHTML = anns.map(a => `
    <div style="padding:4px 6px;margin-bottom:3px;background:rgba(255,255,255,0.05);border-radius:4px">
      <b style="color:#90caf9">${escHtml(a.author ?? a.username ?? "?")}</b>: ${escHtml(a.text ?? "")}
      <span style="opacity:.4;font-size:9px;margin-left:6px">${a.created_at ? new Date(a.created_at).toLocaleTimeString() : ""}</span>
    </div>`).join("");
  listEl.scrollTop = listEl.scrollHeight;
}

async function _tlRenderAnnotations(trackId) {
  const list = document.getElementById("tl-ann-list");
  if (!list) return;
  try {
    const data = await authFetch(`/api/tracks/${encodeURIComponent(trackId)}/annotations`).then(r => r.json());
    const anns = data.annotations ?? [];
    _ANNOTATIONS.set(String(trackId), anns);
    _tlRenderList(list, anns);
    _updateAnnotationBadge(String(trackId));
  } catch {
    list.innerHTML = '<span style="opacity:.4;font-size:10px">Failed to load notes.</span>';
  }
}

async function _tlAddAnnotation() {
  if (!timelineCurrentTrack) return;
  const inp = document.getElementById("tl-ann-input");
  const text = inp?.value?.trim();
  if (!text) return;
  inp.value = "";
  try {
    await authFetch(`/api/tracks/${encodeURIComponent(timelineCurrentTrack)}/annotations`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    await _tlRenderAnnotations(timelineCurrentTrack);
  } catch (err) {
    console.warn("annotation add failed", err);
  }
}

function _updateAnnotationBadge(trackId) {
  const m = UI.trackMarkers.get(String(trackId));
  if (!m) return;
  const track = UI.tracks.get(String(trackId));
  const threat = UI.threats.get(String(trackId));
  if (track) m.setTooltipContent(buildTooltip(track, threat));
}

function _wsAnnotation(payload) {
  const tid = String(payload?.track_id ?? "");
  if (!tid) return;
  const existing = _ANNOTATIONS.get(tid) ?? [];
  existing.push(payload);
  _ANNOTATIONS.set(tid, existing);
  _updateAnnotationBadge(tid);
  if (timelineCurrentTrack === tid) {
    _tlRenderList(document.getElementById("tl-ann-list"), existing);
  }
}

function _wsAnnotationRemoved(payload) {
  const tid  = String(payload?.track_id ?? "");
  const aid  = payload?.id ?? payload?.annotation_id;
  if (!tid || aid == null) return;
  const existing = (_ANNOTATIONS.get(tid) ?? []).filter(a => a.id !== aid);
  _ANNOTATIONS.set(tid, existing);
  _updateAnnotationBadge(tid);
  if (timelineCurrentTrack === tid) {
    _tlRenderList(document.getElementById("tl-ann-list"), existing);
  }
}

function openTimeline(trackId) {
  if(!timelinePopupEl) return;
  timelineCurrentTrack = trackId;
  document.getElementById("tl-title").textContent = `Timeline: ${trackId}`;
  timelinePopupEl.style.display = "block";
  fetchAndDrawTimeline(trackId);
  _tlRenderAnnotations(trackId);
}

/* ── Decision Lineage Modal ─────────────────────────────── */
let lineageModalEl = null;

function mountLineageModal() {
  lineageModalEl = el("div", { id:"lineage-modal", style:{
    display:"none", position:"fixed", top:"50%", left:"50%",
    transform:"translate(-50%,-50%)", zIndex:"10002",
    background:"rgba(10,10,25,0.96)", color:"white",
    padding:"16px 20px", borderRadius:"12px",
    fontFamily:"ui-monospace,SFMono-Regular,monospace", fontSize:"11px",
    border:"1px solid rgba(100,255,150,0.4)",
    boxShadow:"0 8px 40px rgba(0,0,0,0.8)",
    minWidth:"520px", maxWidth:"680px", maxHeight:"70vh",
    overflowY:"auto",
  }});
  lineageModalEl.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <b id="lin-title" style="color:#4fc3f7;font-size:13px">Decision Lineage</b>
      <span id="lin-close" style="cursor:pointer;opacity:.6;font-size:16px">\u2715</span>
    </div>
    <div id="lin-body" style="line-height:1.6"></div>
  `;
  document.body.appendChild(lineageModalEl);
  document.getElementById("lin-close").addEventListener("click", () => {
    lineageModalEl.style.display = "none";
  });
}

const _stageColors = {
  ingest:       "#78909c",
  threat_assess:"#ff9800",
  ml_threat:    "#e040fb",
  anomaly:      "#f44336",
  coord_attack: "#ff1744",
  tactical:     "#ffeb3b",
  roe:          "#4fc3f7",
  task_proposer:"#66bb6a",
  fire_control: "#ff5252",
};

function _stageIcon(stage) {
  const icons = {
    ingest:"\u{1F4E1}", threat_assess:"\u26A0\uFE0F", ml_threat:"\u{1F9E0}",
    anomaly:"\u{1F6A8}", coord_attack:"\u{1F3AF}", tactical:"\u2694\uFE0F",
    roe:"\u{1F6E1}\uFE0F", task_proposer:"\u{1F4CB}", fire_control:"\u{1F4A5}",
  };
  return icons[stage] || "\u{1F50D}";
}

async function openLineage(trackId) {
  if(!lineageModalEl) return;
  document.getElementById("lin-title").textContent = `Decision Lineage: ${trackId}`;
  document.getElementById("lin-body").innerHTML = '<span style="opacity:.5">Loading...</span>';
  lineageModalEl.style.display = "block";

  try {
    const resp = await fetch(`/api/ai/lineage/${encodeURIComponent(trackId)}`).then(r=>r.json());
    const chain = resp.chain || [];
    if(chain.length === 0) {
      document.getElementById("lin-body").innerHTML = '<span style="opacity:.5">No lineage records for this track.</span>';
      return;
    }

    // Group by stage for a cleaner display, but show chronologically
    let html = '<div style="border-left:2px solid rgba(100,255,150,0.3);padding-left:12px">';
    for(const rec of chain) {
      const color = _stageColors[rec.stage] || "#aaa";
      const icon = _stageIcon(rec.stage);
      const time = rec.timestamp ? rec.timestamp.split("T")[1]?.replace("Z","") || "" : "";
      html += `<div style="margin-bottom:8px;padding:6px 8px;background:rgba(255,255,255,0.04);border-radius:6px;border-left:3px solid ${color}">`;
      html += `<div style="display:flex;justify-content:space-between;align-items:center">`;
      html += `<span>${icon} <b style="color:${color}">${rec.stage.toUpperCase()}</b></span>`;
      html += `<span style="opacity:.4;font-size:9px">${time}</span>`;
      html += `</div>`;
      html += `<div style="margin-top:3px;opacity:.85">${_esc(rec.summary)}</div>`;

      // Show key inputs/outputs compactly
      const details = [];
      if(rec.outputs && Object.keys(rec.outputs).length > 0) {
        const pairs = Object.entries(rec.outputs)
          .filter(([,v]) => v !== null && v !== undefined)
          .slice(0, 5)
          .map(([k,v]) => `<span style="color:${color}">${k}</span>=${typeof v === "object" ? JSON.stringify(v) : v}`);
        if(pairs.length) details.push(pairs.join(" \u2502 "));
      }
      if(rec.rule) details.push(`<span style="opacity:.4">rule: ${_esc(rec.rule)}</span>`);
      if(details.length) {
        html += `<div style="margin-top:2px;font-size:10px;opacity:.7">${details.join(" \u2022 ")}</div>`;
      }
      html += `</div>`;
    }
    html += '</div>';
    html += `<div style="margin-top:8px;opacity:.35;font-size:9px;text-align:right">${chain.length} decision records</div>`;
    document.getElementById("lin-body").innerHTML = html;
  } catch(e) {
    document.getElementById("lin-body").innerHTML = `<span style="color:#f55">Error: ${e.message}</span>`;
  }
}

/* _esc → modules/utils.js (escText) */

/* ── Multi-operator ─────────────────────────────────────── */

let operatorPanelEl = null;

/* _OP_COLORS → modules/constants.js (OP_COLORS) */
const _opColorCache = {};
function _opColor(opId) {
  if (!_opColorCache[opId]) {
    const idx = Object.keys(_opColorCache).length % _OP_COLORS.length;
    _opColorCache[opId] = _OP_COLORS[idx];
  }
  return _opColorCache[opId];
}

function showTrackContextMenu(mouseEvent, trackId) {
  document.querySelectorAll(".track-ctx-menu").forEach(m => m.remove());
  const owner = TRACK_CLAIMS[trackId];
  const isMine = owner === MY_OPERATOR_ID;
  const menu = el("div", {class:"track-ctx-menu", style:{
    position:"fixed", left: mouseEvent.clientX+"px", top: mouseEvent.clientY+"px",
    zIndex:"10010", background:"rgba(10,10,25,0.96)", color:"white",
    borderRadius:"8px", border:"1px solid rgba(100,200,255,0.3)",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    boxShadow:"0 4px 16px rgba(0,0,0,0.6)", minWidth:"160px", overflow:"hidden",
  }});
  const items = [
    {label: "🔍 Decision Lineage", action: () => openLineage(trackId)},
    {label: isMine ? "🔓 Release Claim" : "🔒 Claim Track", action: () => claimTrack(trackId)},
  ];
  items.forEach(item => {
    const row = el("div", {style:{
      padding:"8px 14px", cursor:"pointer",
      borderBottom:"1px solid rgba(255,255,255,0.07)",
    }}, [item.label]);
    row.addEventListener("mouseenter", () => row.style.background="rgba(255,255,255,0.08)");
    row.addEventListener("mouseleave", () => row.style.background="");
    row.addEventListener("click", () => { menu.remove(); item.action(); });
    menu.appendChild(row);
  });
  document.body.appendChild(menu);
  // Auto-close on click outside
  setTimeout(() => {
    document.addEventListener("click", () => menu.remove(), {once:true});
  }, 10);
}

function mountOperatorPanel() {
  operatorPanelEl = el("div", { id:"operator-panel", style:{
    position:"fixed", bottom:"12px", left:"12px", zIndex:"9998",
    background:"rgba(10,10,25,0.88)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"11px",
    border:"1px solid rgba(100,200,255,0.25)",
    minWidth:"160px", maxWidth:"220px",
  }});
  document.body.appendChild(operatorPanelEl);
  renderOperatorPanel();
}

function renderOperatorPanel() {
  if (!operatorPanelEl) return;
  const ops = Object.values(OPERATORS_STATE);
  let html = `<div style="font-weight:bold;margin-bottom:5px;opacity:.7;font-size:10px">OPERATORS (${ops.length})</div>`;
  for (const op of ops) {
    const color = _opColor(op.operator_id);
    const isMe = op.operator_id === MY_OPERATOR_ID;
    const claimed = Object.entries(TRACK_CLAIMS).filter(([,oid]) => oid === op.operator_id).length;
    html += `<div style="margin-bottom:3px;display:flex;align-items:center;gap:6px">`;
    html += `<span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0"></span>`;
    html += `<span style="color:${color};font-weight:${isMe?'bold':'normal'}">${_esc(op.operator_id)}${isMe?' (me)':''}</span>`;
    if (claimed) html += `<span style="opacity:.5;font-size:9px">${claimed}✓</span>`;
    html += `</div>`;
  }
  if (ops.length === 0) html += `<div style="opacity:.4">No operators</div>`;
  operatorPanelEl.innerHTML = html;
}

function applyOperatorJoined(payload) {
  if (payload?.operators) {
    OPERATORS_STATE = {};
    payload.operators.forEach(op => { OPERATORS_STATE[op.operator_id] = op; });
  }
  if (payload?.claims) TRACK_CLAIMS = {...payload.claims};
  renderOperatorPanel();
}

function applyOperatorLeft(payload) {
  if (payload?.operator_id) delete OPERATORS_STATE[payload.operator_id];
  if (payload?.released_tracks) {
    payload.released_tracks.forEach(tid => {
      delete TRACK_CLAIMS[tid];
      refreshMarkerClaim(tid);
    });
  }
  renderOperatorPanel();
}

function applyTrackClaimed(payload) {
  if (!payload?.track_id) return;
  TRACK_CLAIMS[payload.track_id] = payload.operator_id;
  refreshMarkerClaim(payload.track_id);
  refreshTaskClaimUI();
  renderOperatorPanel();
}

function applyTrackReleased(payload) {
  if (!payload?.track_id) return;
  delete TRACK_CLAIMS[payload.track_id];
  refreshMarkerClaim(payload.track_id);
  refreshTaskClaimUI();
  renderOperatorPanel();
}

function refreshMarkerClaim(trackId) {
  const marker = UI.trackMarkers.get(String(trackId));
  if (!marker) return;
  const owner = TRACK_CLAIMS[trackId];
  if (owner) {
    const color = _opColor(owner);
    marker.getElement()?.style.setProperty("outline", `2px solid ${color}`, "important");
    marker.getElement()?.style.setProperty("border-radius", "50%");
  } else {
    marker.getElement()?.style.removeProperty("outline");
  }
}

async function claimTrack(trackId) {
  const owner = TRACK_CLAIMS[trackId];
  if (owner && owner !== MY_OPERATOR_ID) {
    alert(`Track ${trackId} already claimed by ${owner}`); return;
  }
  if (owner === MY_OPERATOR_ID) {
    // Release
    await fetch(`/api/tracks/${encodeURIComponent(trackId)}/claim`, {
      method:"DELETE", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({operator_id: MY_OPERATOR_ID}),
    });
  } else {
    // Claim
    await fetch(`/api/tracks/${encodeURIComponent(trackId)}/claim`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({operator_id: MY_OPERATOR_ID}),
    });
  }
}

// Refresh Approve/Reject button states based on current claims
function refreshTaskClaimUI() {
  // Re-render task panels to reflect claim state
  document.querySelectorAll("[data-task-id]").forEach(card => {
    const taskId = card.dataset.taskId;
    const trackId = card.dataset.trackId;
    if (!trackId) return;
    const owner = TRACK_CLAIMS[trackId];
    const locked = owner && owner !== MY_OPERATOR_ID;
    card.querySelectorAll("button[data-action]").forEach(btn => {
      btn.disabled = locked;
      btn.title = locked ? `Claimed by ${owner}` : "";
      btn.style.opacity = locked ? "0.35" : "1";
      btn.style.cursor = locked ? "not-allowed" : "pointer";
    });
    if (locked) {
      let badge = card.querySelector(".claim-badge");
      if (!badge) {
        badge = el("span", {class:"claim-badge", style:{
          fontSize:"9px", background:"rgba(255,200,0,0.2)",
          color:"#ffcc00", borderRadius:"4px", padding:"1px 4px", marginLeft:"4px",
        }}, []);
        card.querySelector("b")?.appendChild(badge);
      }
      badge.textContent = `🔒 ${owner}`;
    } else {
      card.querySelector(".claim-badge")?.remove();
    }
  });
}

async function fetchAndDrawTimeline(trackId) {
  try {
    const resp = await fetch(`/api/ai/timeline?track_id=${encodeURIComponent(trackId)}`).then(r=>r.json());
    drawTimelineChart(resp.timeline || [], trackId);
  } catch(e) { /* silent */ }
}

function drawTimelineChart(data, trackId) {
  const canvas = document.getElementById("tl-canvas");
  if(!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  if(!data || data.length < 2) {
    ctx.fillStyle = "rgba(255,255,255,0.3)";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for data...", W/2, H/2);
    return;
  }

  const pad = {l:35, r:10, t:10, b:22};
  const cw = W - pad.l - pad.r;
  const ch = H - pad.t - pad.b;

  const tMin = data[0].t;
  const tMax = data[data.length-1].t;
  const tRange = Math.max(tMax - tMin, 1);

  const toX = t => pad.l + ((t - tMin) / tRange) * cw;
  const toY = score => pad.t + ch - (score / 100) * ch;

  // ── Background grid ──
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 0.5;
  for(let s = 0; s <= 100; s += 25) {
    const y = toY(s);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W-pad.r, y); ctx.stroke();
  }

  // ── Y-axis labels ──
  ctx.fillStyle = "rgba(255,255,255,0.4)";
  ctx.font = "8px sans-serif";
  ctx.textAlign = "right";
  for(let s = 0; s <= 100; s += 25) {
    ctx.fillText(s, pad.l - 4, toY(s) + 3);
  }

  // ── Intent color bands (background) ──
  const intentColors = {
    attack:"rgba(231,76,60,0.15)", reconnaissance:"rgba(155,89,182,0.15)",
    loitering:"rgba(230,126,34,0.15)", unknown:"rgba(150,150,150,0.05)",
  };
  for(let i = 0; i < data.length - 1; i++) {
    const x1 = toX(data[i].t);
    const x2 = toX(data[i+1].t);
    const ic = intentColors[data[i].intent] || intentColors.unknown;
    ctx.fillStyle = ic;
    ctx.fillRect(x1, pad.t, x2 - x1, ch);
  }

  // ── Threat score line ──
  ctx.strokeStyle = "#3498db";
  ctx.lineWidth = 2;
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = toX(d.t), y = toY(d.score);
    if(i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // ── Score fill gradient ──
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
  grad.addColorStop(0, "rgba(231,76,60,0.25)");
  grad.addColorStop(0.5, "rgba(52,152,219,0.1)");
  grad.addColorStop(1, "rgba(39,174,96,0.05)");
  ctx.fillStyle = grad;
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = toX(d.t), y = toY(d.score);
    if(i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.lineTo(toX(data[data.length-1].t), pad.t + ch);
  ctx.lineTo(toX(data[0].t), pad.t + ch);
  ctx.closePath();
  ctx.fill();

  // ── Threat level color on score dots ──
  const levelColors = {HIGH:"#e74c3c", MEDIUM:"#f39c12", LOW:"#27ae60"};
  data.forEach(d => {
    const x = toX(d.t), y = toY(d.score);
    ctx.fillStyle = levelColors[d.level] || "#3498db";
    ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI*2); ctx.fill();
  });

  // ── Anomaly event markers (triangles) ──
  const anomColors = {CRITICAL:"#e74c3c", HIGH:"#e67e22", MEDIUM:"#f1c40f"};
  data.forEach(d => {
    if(!d.events || d.events.length === 0) return;
    const x = toX(d.t);
    d.events.forEach(ev => {
      const color = anomColors[ev.severity] || "#f1c40f";
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, pad.t + 2);
      ctx.lineTo(x - 4, pad.t + 10);
      ctx.lineTo(x + 4, pad.t + 10);
      ctx.closePath();
      ctx.fill();
      // Vertical line down
      ctx.strokeStyle = color;
      ctx.lineWidth = 0.5;
      ctx.globalAlpha = 0.4;
      ctx.beginPath(); ctx.moveTo(x, pad.t + 10); ctx.lineTo(x, pad.t + ch); ctx.stroke();
      ctx.globalAlpha = 1.0;
    });
  });

  // ── Time axis ──
  ctx.fillStyle = "rgba(255,255,255,0.4)";
  ctx.font = "8px sans-serif";
  ctx.textAlign = "center";
  const elapsed = tRange;
  const steps = Math.min(6, data.length);
  for(let i = 0; i <= steps; i++) {
    const t = tMin + (i / steps) * tRange;
    const sec = Math.round(t - tMin);
    ctx.fillText(`${sec}s`, toX(t), H - 4);
  }

  // ── Legend ──
  const legendEl = document.getElementById("tl-legend");
  if(legendEl) {
    const lastD = data[data.length - 1];
    const intentLabel = (lastD.intent || "unknown").toUpperCase();
    legendEl.innerHTML =
      `<span style="color:#3498db">\u2501 Score</span> &nbsp; ` +
      `<span style="color:${levelColors[lastD.level]||'#fff'}">\u25CF ${lastD.level}</span> &nbsp; ` +
      `<span style="color:${(intentColors[lastD.intent]||'').replace('0.15','0.9')}">${intentLabel}</span> &nbsp; ` +
      `<span style="color:#f1c40f">\u25B2 Anomaly</span> &nbsp; ` +
      `Latest: ${lastD.score}/100`;
  }
}

/* ── AI: After-Action Report (AAR) modal ──────────────────── */
let aarModalEl = null;

function mountAARButton() {
  const btn = el("div", {style:{
    position:"fixed", top:"12px", right:"320px", zIndex:"10002",
    background:"#c0392b", color:"#fff",
    padding:"6px 14px", borderRadius:"20px", cursor:"pointer",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    fontWeight:"bold", boxShadow:"0 2px 8px rgba(0,0,0,0.4)",
    border:"1px solid rgba(255,255,255,0.2)",
  }, onclick:()=>openAAR()}, ["AAR Raporu"]);
  document.body.appendChild(btn);
}

function mountAARModal() {
  aarModalEl = el("div", { id:"aar-modal", style:{
    display:"none", position:"fixed", inset:"0", zIndex:"10010",
    background:"rgba(0,0,0,0.88)", color:"white",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    overflowY:"auto",
  }});
  aarModalEl.innerHTML = `
    <div id="aar-content" style="max-width:780px;margin:30px auto;padding:20px 28px;
         background:rgba(15,15,30,0.95);border-radius:14px;border:1px solid rgba(100,150,255,0.2)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h2 style="margin:0;font-size:18px">After-Action Report</h2>
        <div>
          <button id="aar-download" style="background:#2980b9;color:#fff;border:none;border-radius:6px;
            padding:5px 12px;cursor:pointer;margin-right:6px;font-size:11px">JSON Indir</button>
          <button id="aar-print" style="background:#27ae60;color:#fff;border:none;border-radius:6px;
            padding:5px 12px;cursor:pointer;margin-right:8px;font-size:11px">Yazdir / PDF</button>
          <span id="aar-close" style="cursor:pointer;opacity:.6;font-size:20px;padding:0 8px">\u2715</span>
        </div>
      </div>
      <div id="aar-body" style="line-height:1.7">
        <p style="opacity:.5">Rapor yukleniyor...</p>
      </div>
    </div>`;
  document.body.appendChild(aarModalEl);
  document.getElementById("aar-close").addEventListener("click", ()=>{
    aarModalEl.style.display = "none";
  });
  document.getElementById("aar-download").addEventListener("click", ()=>{
    downloadAARJson();
  });
  document.getElementById("aar-print").addEventListener("click", ()=>{
    printAAR();
  });
}

let _lastAARData = null;

async function openAAR() {
  if(!aarModalEl) return;
  aarModalEl.style.display = "block";
  document.getElementById("aar-body").innerHTML = "<p style='opacity:.5'>Rapor yukleniyor...</p>";
  try {
    const resp = await fetch("/api/ai/aar").then(r=>r.json());
    _lastAARData = resp;
    renderAAR(resp);
  } catch(e) {
    document.getElementById("aar-body").innerHTML = "<p style='color:#e74c3c'>Rapor alinamadi: " + escHtml(e.message) + "</p>";
  }
}

function downloadAARJson() {
  if(!_lastAARData) return;
  const blob = new Blob([JSON.stringify(_lastAARData, null, 2)], {type:"application/json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `aar_${new Date().toISOString().slice(0,19).replace(/:/g,"-")}.json`;
  a.click(); URL.revokeObjectURL(url);
}

function printAAR() {
  const body = document.getElementById("aar-body");
  if (!body) return;
  const title = `NIZAM COP — After-Action Report — ${new Date().toISOString().slice(0,19).replace("T"," ")} UTC`;
  const win = window.open("", "_blank", "width=900,height=700");
  if (!win) { alert("Pop-up engellendi. Tarayici pop-up izinlerini kontrol edin."); return; }
  win.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>${title}</title>
  <style>
    body { font-family: Arial, sans-serif; font-size: 12px; background: #fff; color: #111; margin: 20px 40px; }
    h2 { font-size: 18px; margin-bottom: 4px; }
    h3 { font-size: 13px; margin: 14px 0 4px; color: #333; border-bottom: 1px solid #ddd; padding-bottom: 2px; }
    table { width:100%; border-collapse:collapse; font-size:11px; margin-top:4px; }
    th, td { padding: 3px 6px; text-align:left; border-bottom:1px solid #eee; }
    th { background:#f5f5f5; font-weight:600; }
    div[style*="grid"] > div { border: 1px solid #ddd !important; background: #f9f9f9 !important; }
    @media print { body { margin:10mm 15mm; } button { display:none; } }
  </style></head><body>
  <div style="border-bottom:2px solid #2980b9;padding-bottom:8px;margin-bottom:16px">
    <div style="font-size:15px;font-weight:bold;color:#2980b9">NIZAM COP — After-Action Report</div>
    <div style="font-size:11px;color:#555">${title}</div>
  </div>
  ${body.innerHTML}
  <script>window.onload=function(){window.print();}<\/script>
  </body></html>`);
  win.document.close();
}

function renderAAR(r) {
  const body = document.getElementById("aar-body");
  if(!body) return;
  const ex = r.executive_summary || {};
  const ta = r.threat_analysis || {};
  const aa = r.anomaly_analysis || {};
  const ew = r.ew_analysis || {};
  const dc = r.deconfliction_summary || {};
  const ca = r.coordinated_attack_analysis || {};
  const za = r.zone_breach_analysis || {};
  const ts = r.task_summary || {};
  const ra = r.risk_assessment || {};
  const tracks = r.track_summaries || [];
  const timeline = r.key_event_timeline || [];

  const riskColors = {CRITICAL:"#e74c3c",HIGH:"#e67e22",MEDIUM:"#f39c12",LOW:"#27ae60"};
  const riskColor = riskColors[ra.overall_risk] || "#95a5a6";

  let html = "";

  // ── Risk Assessment Banner ──
  html += `<div style="background:${riskColor}22;border:2px solid ${riskColor};border-radius:10px;padding:12px 16px;margin-bottom:16px">
    <div style="font-size:16px;font-weight:bold;color:${riskColor}">GENEL RISK: ${ra.overall_risk || "?"}</div>
    <ul style="margin:6px 0 0 16px;padding:0;opacity:.9">${(ra.reasons||[]).map(r=>`<li>${escHtml(r)}</li>`).join("")}</ul>
  </div>`;

  // ── Executive Summary ──
  html += `<div style="margin-bottom:14px">
    <h3 style="margin:0 0 6px;color:#3498db;font-size:14px">Yonetici Ozeti</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
      ${_aarStatCard("Sure", ex.duration_display || "?", "#3498db")}
      ${_aarStatCard("Toplam Hedef", ex.total_unique_tracks || 0, "#9b59b6")}
      ${_aarStatCard("Maks. Esanli", ex.max_concurrent_tracks || 0, "#8e44ad")}
      ${_aarStatCard("Zirve Tehdit", ex.peak_threat_score || 0, "#e74c3c")}
      ${_aarStatCard("Anomali", ex.total_anomalies || 0, "#e67e22")}
      ${_aarStatCard("EW Saldiri", ew.total || 0, "#a060e0")}
      ${_aarStatCard("Deconflict", dc.total_merges || 0, "#20c0d0")}
      ${_aarStatCard("Koord. Saldiri", ex.total_coord_attacks || 0, "#c0392b")}
      ${_aarStatCard("Bolge Ihlali", ex.total_zone_breaches || 0, "#f39c12")}
      ${_aarStatCard("Gorev", ex.total_tasks || 0, "#2980b9")}
      ${_aarStatCard("Bolge", ex.zones_defined || 0, "#1abc9c")}
    </div>
    ${ex.peak_threat_track ? `<div style="margin-top:6px;opacity:.7;font-size:11px">Zirve tehdit: <b>${escHtml(ex.peak_threat_track)}</b> (skor:${ex.peak_threat_score}, t+${ex.peak_threat_time_elapsed}s)</div>` : ""}
  </div>`;

  // ── Threat Analysis ──
  html += `<div style="margin-bottom:14px">
    <h3 style="margin:0 0 6px;color:#e74c3c;font-size:14px">Tehdit Analizi</h3>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px">
      <div><span style="color:#e74c3c">\u25A0</span> HIGH: <b>${ta.high_threat_count||0}</b></div>
      <div><span style="color:#f39c12">\u25A0</span> MEDIUM: <b>${ta.medium_threat_count||0}</b></div>
      <div><span style="color:#27ae60">\u25A0</span> LOW: <b>${ta.low_threat_count||0}</b></div>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px;opacity:.8">
      ${Object.entries(ta.intent_distribution||{}).map(([k,v])=>`<div>${k}: <b>${v}</b></div>`).join("")}
    </div>`;

  if((ta.top_threatening_tracks||[]).length > 0) {
    html += `<table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:4px">
      <tr style="opacity:.6;text-align:left"><th style="padding:2px 6px">Hedef</th><th>Zirve Skor</th><th>Seviye</th><th>Niyet</th></tr>`;
    (ta.top_threatening_tracks||[]).slice(0,8).forEach(t => {
      const c = riskColors[t.peak_level] || "#aaa";
      html += `<tr><td style="padding:2px 6px;font-weight:bold">${escHtml(t.track_id)}</td>
        <td>${t.peak_score}</td><td style="color:${c}">${t.peak_level}</td><td>${t.peak_intent}</td></tr>`;
    });
    html += `</table>`;
  }
  html += `</div>`;

  // ── Anomaly Analysis ──
  if(aa.total > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#e67e22;font-size:14px">Anomali Analizi (${aa.total})</h3>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px">
        ${Object.entries(aa.by_type||{}).map(([k,v])=>`<div style="background:rgba(255,255,255,0.06);padding:2px 8px;border-radius:4px">${k}: <b>${v}</b></div>`).join("")}
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;opacity:.7">
        ${Object.entries(aa.by_severity||{}).map(([k,v])=>{
          const sc = riskColors[k]||"#aaa";
          return `<div><span style="color:${sc}">\u25CF</span> ${k}: ${v}</div>`;
        }).join("")}
      </div>
    </div>`;
  }

  // ── EW Attack Analysis ──
  if ((ew.total || 0) > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#a060e0;font-size:14px">Elektronik Harp Saldirilari (${ew.total})</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        ${ew.gps_spoofing_count   ? `<div style="background:rgba(160,96,224,0.12);border:1px solid rgba(160,96,224,0.3);padding:3px 10px;border-radius:6px">GPS Spoof: <b>${ew.gps_spoofing_count}</b></div>` : ""}
        ${ew.radar_jamming_count  ? `<div style="background:rgba(240,64,64,0.12);border:1px solid rgba(240,64,64,0.3);padding:3px 10px;border-radius:6px">Radar Jam: <b>${ew.radar_jamming_count}</b></div>` : ""}
        ${ew.false_injection_count ? `<div style="background:rgba(240,128,48,0.12);border:1px solid rgba(240,128,48,0.3);padding:3px 10px;border-radius:6px">Sahte Enjeksiyon: <b>${ew.false_injection_count}</b></div>` : ""}
        ${ew.critical_count ? `<div style="background:rgba(240,64,64,0.2);border:1px solid #f04040;padding:3px 10px;border-radius:6px;color:#f04040;font-weight:bold">CRITICAL: ${ew.critical_count}</div>` : ""}
      </div>
      ${(ew.recent||[]).map(a => {
        const ec = a.severity === "CRITICAL" ? "#f04040" : "#f08030";
        return `<div style="border-left:3px solid ${ec};padding-left:6px;margin:2px 0;font-size:11px">
          <span style="color:${ec};font-weight:bold">${a.type}</span>
          ${a.track_id ? `<span style="opacity:.6"> ${escHtml(a.track_id)}</span>` : ""}
          <span style="opacity:.6;margin-left:4px">${escHtml(a.detail||"").slice(0,80)}</span>
        </div>`;
      }).join("")}
    </div>`;
  }

  // ── Deconfliction Summary ──
  if ((dc.total_merges || 0) > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#20c0d0;font-size:14px">Iz Deconfliction</h3>
      <div style="opacity:.9">Birlestirilmis iz sayisi: <b style="color:#20c0d0">${dc.total_merges}</b>
        <span style="opacity:.6;font-size:11px;margin-left:8px">(cok sensoru tek kanonik ize indirdi)</span>
      </div>
    </div>`;
  }

  // ── Coordinated Attack Analysis ──
  if(ca.total > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#ff0050;font-size:14px">Koordineli Saldiri Analizi (${ca.total})</h3>
      <div style="display:flex;gap:12px;margin-bottom:6px">
        <div style="background:#ff005033;padding:3px 10px;border-radius:6px">Kiskac: <b>${ca.pincer_count||0}</b></div>
        <div style="background:#ff660033;padding:3px 10px;border-radius:6px">Yakinsama: <b>${ca.convergence_count||0}</b></div>
      </div>`;
    (ca.events||[]).slice(0,5).forEach(e => {
      const ec = e.subtype.includes("PINCER") ? "#ff0050" : "#ff6600";
      html += `<div style="border-left:3px solid ${ec};padding-left:6px;margin:3px 0;font-size:11px">${escHtml(e.message||e.subtype)}</div>`;
    });
    html += `</div>`;
  }

  // ── Zone Breach Analysis ──
  if(za.total > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#f39c12;font-size:14px">Bolge Ihlali (${za.total})</h3>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        ${Object.entries(za.by_zone||{}).map(([k,v])=>`<div style="background:rgba(243,156,18,0.12);padding:2px 8px;border-radius:4px">${escHtml(k)}: <b>${v}</b></div>`).join("")}
      </div>
    </div>`;
  }

  // ── Task Summary ──
  if(ts.total_created > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#2980b9;font-size:14px">Gorev Ozeti (${ts.total_created})</h3>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        ${Object.entries(ts.by_action||{}).map(([k,v])=>`<div>${k}: <b>${v}</b></div>`).join("")}
      </div>
      <div style="margin-top:4px;opacity:.7">Bekleyen: ${ts.pending_count||0} | Onaylanan: ${ts.approved_count||0} | Reddedilen: ${ts.rejected_count||0}</div>
    </div>`;
  }

  // ── Track Summaries ──
  if(tracks.length > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#9b59b6;font-size:14px">Hedef Ozeti (${tracks.length})</h3>
      <table style="width:100%;border-collapse:collapse;font-size:11px">
        <tr style="opacity:.6;text-align:left">
          <th style="padding:2px 6px">ID</th><th>Zirve</th><th>Ort.</th><th>Son</th><th>Niyet</th><th>Deg.</th><th>Anom.</th>
        </tr>`;
    tracks.slice(0,12).forEach(t => {
      const c = riskColors[t.final_level] || "#aaa";
      html += `<tr>
        <td style="padding:2px 6px;font-weight:bold">${escHtml(t.track_id)}</td>
        <td>${t.peak_score}</td><td>${t.avg_score}</td>
        <td style="color:${c}">${t.final_score}</td>
        <td>${t.dominant_intent}</td><td>${t.intent_changes}</td><td>${t.anomaly_count}</td>
      </tr>`;
    });
    html += `</table></div>`;
  }

  // ── Key Event Timeline ──
  if(timeline.length > 0) {
    html += `<div style="margin-bottom:14px">
      <h3 style="margin:0 0 6px;color:#1abc9c;font-size:14px">Olay Zaman Cizelgesi (${timeline.length})</h3>
      <div style="max-height:200px;overflow-y:auto">`;
    timeline.forEach(ev => {
      const sc = riskColors[ev.severity] || "#95a5a6";
      html += `<div style="display:flex;gap:8px;margin:2px 0;font-size:11px">
        <span style="color:${sc};min-width:50px;opacity:.7">${ev.elapsed_display}</span>
        <span style="color:${sc};font-weight:bold;min-width:100px">${ev.type}</span>
        <span style="opacity:.85">${escHtml(ev.message)}</span>
      </div>`;
    });
    html += `</div></div>`;
  }

  // ── Footer ──
  html += `<div style="margin-top:12px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.1);opacity:.4;font-size:10px;text-align:center">
    NIZAM COP — After-Action Report — ${r.generated_at_iso || ""}
  </div>`;

  body.innerHTML = html;
}

function _aarStatCard(label, value, color) {
  return `<div style="background:${color}18;border:1px solid ${color}44;border-radius:8px;padding:6px 10px;text-align:center">
    <div style="font-size:18px;font-weight:bold;color:${color}">${value}</div>
    <div style="font-size:10px;opacity:.7">${label}</div>
  </div>`;
}

/* ── Multi-effector Assignment Panel ────────────────────── */
let _assignPanelEl = null;

function mountAssignmentPanel() {
  _assignPanelEl = el("div", {id:"assign-panel", class:"nz-card", style:{
    width:"100%", marginBottom:"4px", display:"none",
  }});
  _assignPanelEl.innerHTML = `
    <div class="nz-section" style="margin-bottom:6px">Effektör Atama <span id="assign-badge" style="font-size:9px;opacity:.6"></span></div>
    <div id="assign-body" style="font-size:10px"></div>`;
  const aiTab = RIGHT_TABS["ai"];
  if (aiTab) aiTab.appendChild(_assignPanelEl);
}

function renderAssignmentPanel(data) {
  if (!_assignPanelEl) return;
  const assignments = data.assignments || [];
  const unassigned  = data.unassigned_threats || [];
  const stats       = data.stats || {};
  if (assignments.length === 0 && unassigned.length === 0) {
    _assignPanelEl.style.display = "none";
    return;
  }
  _assignPanelEl.style.display = "";
  const badge = document.getElementById("assign-badge");
  if (badge) badge.textContent = `${stats.assigned||0}/${stats.threats||0} atandı`;

  const body = document.getElementById("assign-body");
  if (!body) return;
  let html = "";
  if (assignments.length > 0) {
    html += assignments.map(a => {
      const engC = a.engagement === "WEAPONS_FREE" ? "#e74c3c" : "#e67e22";
      return `<div style="display:flex;justify-content:space-between;align-items:center;
                padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
        <div>
          <span style="font-weight:bold;color:#e74c3c">${escHtml(a.threat_id)}</span>
          <span style="opacity:.5;margin:0 4px">→</span>
          <span style="color:#27ae60">${escHtml(a.effector_name||a.effector_id)}</span>
        </div>
        <div style="opacity:.7;font-size:9px;text-align:right">
          <span style="color:${engC}">${a.engagement}</span>
          <span style="margin-left:4px">${a.dist_km}km</span>
          <span style="margin-left:4px;opacity:.6">c=${a.cost}</span>
        </div>
      </div>`;
    }).join("");
  }
  if (unassigned.length > 0) {
    html += `<div style="margin-top:5px;opacity:.6;font-size:9px">Atanamadı: ${unassigned.map(escHtml).join(", ")}</div>`;
  }
  body.innerHTML = html;
}

/* ── Blue Force Protection Panel ────────────────────────── */
let _bftPanelEl = null;
const _bftLog = [];

function mountBFTPanel() {
  _bftPanelEl = el("div", {id:"bft-panel", class:"nz-card", style:{
    width:"100%", marginBottom:"4px", display:"none",
    borderLeft:"3px solid #3498db",
  }});
  _bftPanelEl.innerHTML = `
    <div class="nz-section" style="margin-bottom:6px;color:#3498db">🔵 Mavi Kuvvet Koruması</div>
    <div id="bft-body" style="font-size:10px"></div>`;
  const aiTab = RIGHT_TABS["ai"];
  if (aiTab) aiTab.appendChild(_bftPanelEl);
}

function renderBFTPanel(warnings) {
  if (!_bftPanelEl) return;
  if (!warnings || warnings.length === 0) {
    _bftPanelEl.style.display = "none";
    return;
  }
  _bftPanelEl.style.display = "";
  const body = document.getElementById("bft-body");
  if (!body) return;
  body.innerHTML = warnings.map(w => {
    const assets = (w.at_risk||[]).map(r =>
      `<span style="color:#f39c12">${escHtml(r.asset_name)}</span> <span style="opacity:.5">(${r.clearance_m}m)</span>`
    ).join(", ");
    return `<div style="background:rgba(52,152,219,0.08);border-left:2px solid #3498db;
              padding:4px 6px;margin-bottom:4px;border-radius:0 4px 4px 0">
      <div style="font-weight:bold;color:#3498db">${escHtml(w.track_id)}</div>
      <div style="opacity:.8;margin-top:2px">Koridor: ${assets||"?"}</div>
      <div style="opacity:.5;font-size:9px;margin-top:1px">WEAPONS_FREE → WEAPONS_HOLD</div>
    </div>`;
  }).join("");
}

function pushBFTWarning(payload) {
  if (!payload) return;
  _bftLog.unshift({...payload, _t: Date.now()});
  if (_bftLog.length > 10) _bftLog.length = 10;
  renderBFTPanel(_bftLog.filter(w => Date.now() - w._t < 120000));
  // Flash EW panel tab badge briefly
  setTabBadge("ai", (_bftLog.length));
}

/* ── Effector Status & Outcome Panel ────────────────────── */
let _effStatusPanelEl = null;
const _outcomeLog = [];   // rolling outcome log (max 20)

/* STATUS_COLORS, OUTCOME_COLORS → modules/constants.js */

function mountEffectorStatusPanel() {
  _effStatusPanelEl = el("div", {id:"eff-status-panel", class:"nz-card", style:{
    width:"100%", marginBottom:"4px", display:"none",
  }});
  _effStatusPanelEl.innerHTML = `
    <div class="nz-section" style="margin-bottom:6px">Effektör Durumu</div>
    <div id="eff-status-body" style="font-size:10px"></div>
    <div class="nz-section" style="margin:6px 0 4px">Son Angajman Sonuçları</div>
    <div id="eff-outcome-body" style="font-size:10px"></div>`;
  const aiTab = RIGHT_TABS["ai"];
  if (aiTab) aiTab.appendChild(_effStatusPanelEl);
}

function renderEffectorStatusPanel(statusMap, outcomes) {
  if (!_effStatusPanelEl) return;
  const hasStatus   = Object.keys(statusMap).length > 0;
  const hasOutcomes = outcomes && outcomes.length > 0;
  if (!hasStatus && !hasOutcomes && _outcomeLog.length === 0) {
    _effStatusPanelEl.style.display = "none";
    return;
  }
  _effStatusPanelEl.style.display = "";

  // Status table
  const sbody = document.getElementById("eff-status-body");
  if (sbody) {
    if (!hasStatus) {
      sbody.innerHTML = `<span style="opacity:.4">Aktif effektör yok</span>`;
    } else {
      sbody.innerHTML = Object.entries(statusMap).map(([eid, s]) => {
        const col = STATUS_COLORS[s.status] || "#aaa";
        return `<div style="display:flex;justify-content:space-between;align-items:center;
                             padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
          <span style="opacity:.8">${escHtml(eid)}</span>
          <span style="background:${col}22;color:${col};border:1px solid ${col}44;
                       padding:1px 5px;border-radius:3px;font-size:9px;font-weight:bold">
            ${escHtml(s.status)}
          </span>
        </div>`;
      }).join("");
    }
  }

  // Outcomes (merge server payload + local log, newest first)
  if (outcomes && outcomes.length > 0) {
    outcomes.forEach(o => {
      if (!_outcomeLog.some(x => x.task_id === o.task_id && x.outcome === o.outcome)) {
        _outcomeLog.unshift({...o, _t: Date.now()});
      }
    });
    if (_outcomeLog.length > 20) _outcomeLog.length = 20;
  }
  const obody = document.getElementById("eff-outcome-body");
  if (obody) {
    if (_outcomeLog.length === 0) {
      obody.innerHTML = `<span style="opacity:.4">Henüz sonuç yok</span>`;
    } else {
      obody.innerHTML = _outcomeLog.slice(0, 8).map(o => {
        const col = OUTCOME_COLORS[o.outcome] || "#aaa";
        const ago = Math.round((Date.now() - o._t) / 1000);
        return `<div style="display:flex;justify-content:space-between;align-items:center;
                             padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
          <div>
            <span style="color:${ACTION_COLORS[o.action]||'#aaa'}">${escHtml(o.action||"?")}</span>
            <span style="opacity:.5;margin:0 3px">→</span>
            <span style="font-family:var(--mono);font-size:9px">${escHtml(o.track_id||o.effector_id||"?")}</span>
          </div>
          <div style="text-align:right">
            <span style="color:${col};font-weight:bold;font-size:9px">${escHtml(o.outcome)}</span>
            <span style="opacity:.4;margin-left:4px;font-size:8px">${ago}s önce</span>
          </div>
        </div>`;
      }).join("");
    }
  }
}

function pushEffectorOutcome(payload) {
  if (!payload) return;
  _outcomeLog.unshift({...payload, _t: Date.now()});
  if (_outcomeLog.length > 20) _outcomeLog.length = 20;
  renderEffectorStatusPanel({}, []);   // re-render outcome section
}

function applyEffectorStatus(payload) {
  if (!payload?.effector_id) return;
  // Update local status map and re-render
  renderEffectorStatusPanel({ [payload.effector_id]: { status: payload.status } }, []);
}

/* ── Model Drift Panel ───────────────────────────────────── */
let _driftPanelEl = null;

function mountDriftPanel() {
  _driftPanelEl = el("div", {id:"drift-panel", class:"nz-card", style:{
    width:"100%", marginBottom:"4px", display:"none",
  }});
  _driftPanelEl.innerHTML = `
    <div class="nz-section" style="margin-bottom:6px">Model Drift <span id="drift-badge"></span></div>
    <div id="drift-body" style="font-size:10px"></div>`;
  const aiTab = RIGHT_TABS["ai"];
  if (aiTab) aiTab.appendChild(_driftPanelEl);
}

function renderDriftPanel(d) {
  if (!_driftPanelEl || !d || d.observations === undefined) return;
  if (d.observations === 0) { _driftPanelEl.style.display = "none"; return; }
  _driftPanelEl.style.display = "";

  const LEVEL_COLOR = { none:"#27ae60", minor:"#f39c12", major:"#e74c3c" };
  const level = d.drift_level || "none";
  const col   = LEVEL_COLOR[level] || "#aaa";

  const badge = document.getElementById("drift-badge");
  if (badge) badge.innerHTML =
    `<span style="background:${col}22;color:${col};border:1px solid ${col}44;
                  padding:1px 5px;border-radius:3px;font-size:9px;font-weight:bold">
      ${level.toUpperCase()}
    </span>`;

  const body = document.getElementById("drift-body");
  if (!body) return;

  // Grade distribution bar
  const dist = d.grade_dist || {};
  const base = d.baseline_dist || {};
  const bars = [
    {label:"HIGH",   pct:(dist.HIGH||0)*100,   basePct:(base.HIGH||0)*100,   col:"#e74c3c"},
    {label:"MEDIUM", pct:(dist.MEDIUM||0)*100, basePct:(base.MEDIUM||0)*100, col:"#f39c12"},
    {label:"LOW",    pct:(dist.LOW||0)*100,     basePct:(base.LOW||0)*100,   col:"#27ae60"},
  ];
  const distHtml = bars.map(b => `
    <div style="display:flex;align-items:center;gap:4px;margin-bottom:3px">
      <span style="width:42px;color:${b.col};font-size:9px">${b.label}</span>
      <div style="flex:1;background:rgba(255,255,255,0.07);border-radius:2px;height:8px;position:relative">
        <div style="width:${b.pct.toFixed(1)}%;background:${b.col};height:100%;border-radius:2px;opacity:.8"></div>
        ${b.basePct > 0 ? `<div style="position:absolute;top:0;left:${b.basePct.toFixed(1)}%;width:2px;height:100%;background:#fff;opacity:.4"></div>` : ""}
      </div>
      <span style="width:32px;text-align:right;opacity:.7;font-size:9px">${b.pct.toFixed(0)}%</span>
    </div>`).join("");

  const psiHtml = `
    <div style="display:flex;justify-content:space-between;padding:4px 0;
                border-top:1px solid rgba(255,255,255,0.08);margin-top:4px">
      <span style="opacity:.6">PSI</span>
      <span style="color:${col};font-weight:bold">${(d.psi||0).toFixed(4)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;padding:1px 0">
      <span style="opacity:.6">ML Mean</span>
      <span>${d.ml_mean != null ? d.ml_mean.toFixed(3) : "—"}</span>
    </div>
    <div style="display:flex;justify-content:space-between;padding:1px 0">
      <span style="opacity:.6">FP Rate</span>
      <span>${d.fp_rate != null ? (d.fp_rate*100).toFixed(1)+"%" : "—"}</span>
    </div>
    <div style="display:flex;justify-content:space-between;padding:1px 0">
      <span style="opacity:.6">Gözlem</span>
      <span>${d.observations||0} / ${d.window_size||2000}</span>
    </div>
    ${!d.baseline_locked ? `<div style="opacity:.5;font-size:9px;margin-top:3px">Baseline bekleniyor (${d.observations}/${100})</div>` : ""}`;

  body.innerHTML = distHtml + psiHtml;
}

/* ── Weather Overlay ─────────────────────────────────────── */
let _wxEnabled = false;
const _wxMarkers = [];
let   _wxInterval = null;

const WX_COLORS = {
  TSRA:"#9b59b6", RA:"#3498db", "-RA":"#85c1e9", "+RA":"#1a5276",
  SN:"#d5d8dc", FG:"#839192", MIFG:"#b2babb", HZ:"#f0b27a", "":"#27ae60",
};

function _wxIcon(obs) {
  const col  = WX_COLORS[obs.wx] || "#2ecc71";
  const wind = obs.wind_kt;
  const vis  = obs.visibility_m;
  const vis_txt = vis < 9999 ? `${(vis/1000).toFixed(1)}km` : "CAVOK";
  const wx_txt  = obs.wx || "CLR";
  // Wind barb: short line in wind direction
  const angle = obs.wind_dir;
  const sinA  = Math.sin(angle * Math.PI / 180);
  const cosA  = Math.cos(angle * Math.PI / 180);
  const x2    = 16 + sinA * 12 * Math.min(wind / 20, 1.5);
  const y2    = 16 - cosA * 12 * Math.min(wind / 20, 1.5);
  return L.divIcon({
    className: "",
    iconSize: [32, 32],
    iconAnchor: [16, 16],
    html: `<div style="position:relative;width:32px;height:32px">
      <svg width="32" height="32" style="position:absolute;top:0;left:0">
        <circle cx="16" cy="16" r="5" fill="${col}" opacity=".85" stroke="#fff" stroke-width="1"/>
        ${wind > 3 ? `<line x1="16" y1="16" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"
                           stroke="${col}" stroke-width="2" opacity=".9"/>` : ""}
      </svg>
      <div style="position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);
                  white-space:nowrap;font-size:8px;color:#ddd;
                  text-shadow:0 0 3px #000;pointer-events:none">
        ${obs.station}<br>${wx_txt} ${vis_txt}
      </div>
    </div>`,
  });
}

async function _loadWeatherObs() {
  try {
    const r = await fetch("/api/weather");
    if (!r.ok) return;
    const data = await r.json();
    // Remove old markers
    _wxMarkers.forEach(m => { try { m.remove(); } catch {} });
    _wxMarkers.length = 0;
    (data.observations || []).forEach(obs => {
      if (obs.lat == null || obs.lon == null) return;
      const m = L.marker([obs.lat, obs.lon], { icon: _wxIcon(obs), zIndexOffset: -500 })
        .bindTooltip(_wxTooltip(obs), { direction: "top", offset: [0, -10] })
        .addTo(UI.map);
      _wxMarkers.push(m);
    });
    // Show warnings in EW panel / topbar if any high severity
    const highs = (data.warnings || []).filter(w => w.severity === "HIGH");
    if (highs.length > 0) {
      highs.forEach(w => {
        pushEWAlert({ type: "WEATHER", track_id: w.station,
                      message: w.message, severity: w.severity });
      });
    }
  } catch {}
}

function _wxTooltip(obs) {
  const wind = `${obs.wind_dir}°/${obs.wind_kt}kt${obs.gust_kt ? ` G${obs.gust_kt}kt` : ""}`;
  const vis  = obs.visibility_m < 9999 ? `${obs.visibility_m}m` : "CAVOK";
  const ceil = obs.ceiling_ft ? `${obs.ceiling_ft}ft` : "—";
  return `<div style="font-size:10px;min-width:140px">
    <b>${escHtml(obs.station)}</b> — ${escHtml(obs.name)}<br>
    Sıcaklık: ${obs.temp_c}°C / Çiğ: ${obs.dew_c}°C<br>
    Rüzgar: ${wind}<br>
    Görüş: ${vis} | Tavan: ${ceil}<br>
    WX: <b>${escHtml(obs.wx || 'CLR')}</b>
    <div style="font-size:8px;opacity:.6;margin-top:2px">${escHtml(obs.metar)}</div>
  </div>`;
}

function toggleWeatherOverlay() {
  _wxEnabled = !_wxEnabled;
  const btn = document.getElementById("weather-btn");
  if (btn) {
    btn.style.background    = _wxEnabled ? "rgba(52,152,219,0.25)" : "rgba(100,100,100,0.2)";
    btn.style.borderColor   = _wxEnabled ? "rgba(52,152,219,0.5)"  : "rgba(150,150,150,0.4)";
    btn.style.color         = _wxEnabled ? "#3498db" : "#aaa";
  }
  if (_wxEnabled) {
    _loadWeatherObs();
    _wxInterval = setInterval(_loadWeatherObs, 300_000);  // refresh every 5 min
  } else {
    clearInterval(_wxInterval);
    _wxMarkers.forEach(m => { try { m.remove(); } catch {} });
    _wxMarkers.length = 0;
  }
}

/* ── Audit Log Modal ─────────────────────────────────────── */
let _auditModalEl = null;

function mountAuditModal() {
  _auditModalEl = el("div", { id:"audit-modal", style:{
    display:"none", position:"fixed", inset:"0", zIndex:"10012",
    background:"rgba(0,0,0,0.88)", color:"white",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    overflowY:"auto",
  }});
  _auditModalEl.innerHTML = `
    <div style="max-width:900px;margin:30px auto;padding:20px 28px;
         background:rgba(10,10,30,0.97);border-radius:14px;border:1px solid rgba(52,152,219,0.25)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h2 style="margin:0;font-size:17px;color:#3498db">Operator Audit Log</h2>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="audit-filter-user" placeholder="Kullanici filtrele..." style="background:#1a1a2e;color:#ccc;border:1px solid rgba(100,100,150,0.4);border-radius:6px;padding:4px 8px;font-size:11px;width:130px">
          <button onclick="_auditLoad()" style="background:#2980b9;color:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px">Yenile</button>
          <span id="audit-close" style="cursor:pointer;opacity:.6;font-size:20px;padding:0 8px" onclick="_closeAuditModal()">&#x2715;</span>
        </div>
      </div>
      <div id="audit-status" style="opacity:.5;font-size:11px;margin-bottom:8px"></div>
      <div id="audit-table-wrap" style="overflow-x:auto">
        <table id="audit-table" style="width:100%;border-collapse:collapse;font-size:11px">
          <thead>
            <tr style="opacity:.6;text-align:left;border-bottom:1px solid rgba(255,255,255,0.1)">
              <th style="padding:4px 8px;min-width:130px">Zaman</th>
              <th style="padding:4px 8px">Kullanici</th>
              <th style="padding:4px 8px">Rol</th>
              <th style="padding:4px 8px">Islem</th>
              <th style="padding:4px 8px">Kaynak</th>
              <th style="padding:4px 8px">ID</th>
              <th style="padding:4px 8px">Durum</th>
              <th style="padding:4px 8px">IP</th>
            </tr>
          </thead>
          <tbody id="audit-tbody"></tbody>
        </table>
      </div>
      <div id="audit-pagination" style="margin-top:10px;display:flex;gap:8px;align-items:center"></div>
    </div>`;
  document.body.appendChild(_auditModalEl);
}

let _auditOffset = 0;
const _AUDIT_LIMIT = 50;

function openAuditModal() {
  if (!_auditModalEl) return;
  _auditOffset = 0;
  _auditModalEl.style.display = "block";
  _auditLoad();
}
function _closeAuditModal() {
  if (_auditModalEl) _auditModalEl.style.display = "none";
}

async function _auditLoad() {
  const userFilter = (document.getElementById("audit-filter-user")||{}).value || "";
  const url = `/api/audit?limit=${_AUDIT_LIMIT}&offset=${_auditOffset}` +
    (userFilter ? `&username=${encodeURIComponent(userFilter)}` : "");
  const statusEl = document.getElementById("audit-status");
  const tbody = document.getElementById("audit-tbody");
  if (statusEl) statusEl.textContent = "Yukleniyor...";
  try {
    const resp = await fetch(url, {headers: authHeaders()});
    if (resp.status === 403) {
      if (statusEl) statusEl.textContent = "Erisim reddedildi — admin yetkisi gerekli";
      if (tbody) tbody.innerHTML = `<tr><td colspan="8" style="padding:16px;opacity:.5;text-align:center">Bu panel sadece admin kullanicilara aciktir.</td></tr>`;
      return;
    }
    const data = await resp.json();
    const records = data.records || [];
    const total = data.total || 0;
    if (statusEl) statusEl.textContent = `${total} kayit (${_auditOffset + 1}–${Math.min(_auditOffset + _AUDIT_LIMIT, total)})` + (data.note ? `  — ${data.note}` : "");

    const actionColors = {
      approve:"#27ae60", reject:"#e74c3c", create:"#3498db",
      delete:"#e67e22", login:"#9b59b6", logout:"#95a5a6",
      ack:"#f39c12", resolve:"#1abc9c",
    };
    if (tbody) {
      if (records.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="padding:16px;opacity:.5;text-align:center">Kayit bulunamadi.</td></tr>`;
      } else {
        tbody.innerHTML = records.map(r => {
          const actionKey = (r.action||"").toLowerCase().split("_")[0];
          const ac = actionColors[actionKey] || "#aaa";
          const ok = r.success ? `<span style="color:#27ae60">&#x2713;</span>` : `<span style="color:#e74c3c">&#x2717;</span>`;
          const ts = r.time ? r.time.replace("T"," ").slice(0,19) : "—";
          return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);transition:background .1s" onmouseover="this.style.background='rgba(52,152,219,0.07)'" onmouseout="this.style.background=''">
            <td style="padding:4px 8px;font-family:monospace;opacity:.7">${escHtml(ts)}</td>
            <td style="padding:4px 8px;font-weight:bold">${escHtml(r.username||"—")}</td>
            <td style="padding:4px 8px;opacity:.7">${escHtml(r.role||"—")}</td>
            <td style="padding:4px 8px;color:${ac};font-weight:600">${escHtml(r.action||"—")}</td>
            <td style="padding:4px 8px;opacity:.8">${escHtml(r.resource_type||"—")}</td>
            <td style="padding:4px 8px;font-family:monospace;opacity:.65">${escHtml((r.resource_id||"").slice(0,14))}</td>
            <td style="padding:4px 8px">${ok}</td>
            <td style="padding:4px 8px;opacity:.55;font-size:10px">${escHtml(r.ip||"—")}</td>
          </tr>`;
        }).join("");
      }
    }

    // pagination
    const pagEl = document.getElementById("audit-pagination");
    if (pagEl) {
      const hasPrev = _auditOffset > 0;
      const hasNext = _auditOffset + _AUDIT_LIMIT < total;
      pagEl.innerHTML =
        `<button onclick="_auditPage(-1)" style="background:#1a1a2e;color:#${hasPrev?'3498db':'555'};border:1px solid rgba(100,100,150,0.3);border-radius:5px;padding:3px 10px;cursor:${hasPrev?'pointer':'default'}" ${hasPrev?'':' disabled'}>&#x2190; Onceki</button>` +
        `<span style="opacity:.5">${Math.floor(_auditOffset/_AUDIT_LIMIT)+1} / ${Math.max(1,Math.ceil(total/_AUDIT_LIMIT))}</span>` +
        `<button onclick="_auditPage(1)" style="background:#1a1a2e;color:#${hasNext?'3498db':'555'};border:1px solid rgba(100,100,150,0.3);border-radius:5px;padding:3px 10px;cursor:${hasNext?'pointer':'default'}" ${hasNext?'':' disabled'}>Sonraki &#x2192;</button>`;
    }
  } catch(e) {
    if (statusEl) statusEl.textContent = "Hata: " + e.message;
  }
}

function _auditPage(dir) {
  _auditOffset = Math.max(0, _auditOffset + dir * _AUDIT_LIMIT);
  _auditLoad();
}

/* ── AI: periodic refresh ──────────────────────────────────── */
async function refreshAI() {
  // Polling fallback — used only if WebSocket is down. Normal AI updates
  // arrive via cop.ai_update events (see applyAIUpdate).
  if (UI.wsConnected) return;
  try {
    const [predResp, anomResp, recResp, breachResp, coneResp, coordResp, roeResp, mlResp] = await Promise.all([
      fetch("/api/ai/predictions").then(r=>r.json()).catch(()=>({})),
      fetch("/api/ai/anomalies").then(r=>r.json()).catch(()=>({anomalies:[]})),
      fetch("/api/ai/recommendations").then(r=>r.json()).catch(()=>({recommendations:[]})),
      fetch("/api/ai/pred_breaches").then(r=>r.json()).catch(()=>({breaches:[]})),
      fetch("/api/ai/uncertainty").then(r=>r.json()).catch(()=>({cones:{}})),
      fetch("/api/ai/coordinated").then(r=>r.json()).catch(()=>({attacks:[]})),
      fetch("/api/ai/roe").then(r=>r.json()).catch(()=>({advisories:[]})),
      fetch("/api/ai/ml").then(r=>r.json()).catch(()=>({predictions:{}})),
    ]);
    applyAIUpdate({
      predictions:       predResp.predictions || {},
      anomalies:         anomResp.anomalies || [],
      recommendations:   recResp.recommendations || [],
      pred_breaches:     breachResp.breaches || [],
      uncertainty_cones: coneResp.cones || {},
      coord_attacks:     coordResp.attacks || [],
      roe_advisories:    roeResp.advisories || [],
      ml_predictions:    mlResp.predictions || {},
      ml_available:      mlResp.model?.available ?? false,
    });
  } catch(e) { /* silent */ }
}

/* ── Replay System ──────────────────────────────────────── */

let replayBarEl = null;
let replayListEl = null;
let replayActive = false;
let replayTimer = null;
let replayInfo = { state:"IDLE", duration_s:0, current_elapsed_s:0, speed:1, filename:"", scenario:"" };

function mountReplayBar() {
  // Bottom replay control bar (hidden by default)
  replayBarEl = el("div", { id:"replay-bar", style:{
    position:"fixed", bottom:"0", left:"0", right:"0", zIndex:"10001",
    background:"linear-gradient(180deg, rgba(15,10,40,0.95), rgba(10,5,30,0.98))",
    color:"white", padding:"10px 20px", display:"none",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    borderTop:"2px solid #7c3aed",
    boxShadow:"0 -4px 20px rgba(124,58,237,0.3)",
  }});
  replayBarEl.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;max-width:1400px;margin:0 auto">
      <div style="display:flex;align-items:center;gap:6px;min-width:140px">
        <span style="color:#a78bfa;font-weight:bold;font-size:13px">\u25B6 REPLAY</span>
        <span id="replay-scenario" style="color:#c4b5fd;font-size:11px"></span>
      </div>
      <button id="replay-btn-play" onclick="replayTogglePlay()" style="background:#7c3aed;color:#fff;border:none;border-radius:6px;padding:5px 14px;cursor:pointer;font-weight:bold;font-size:12px">\u25B6 Play</button>
      <button onclick="replayStop()" style="background:#dc2626;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px">\u25A0 Stop</button>
      <div style="display:flex;align-items:center;gap:4px">
        <span style="opacity:0.7">Speed:</span>
        <select id="replay-speed" onchange="replaySetSpeed(this.value)" style="background:#1e1b4b;color:#fff;border:1px solid #4c1d95;border-radius:4px;padding:2px 4px;font-size:11px">
          <option value="0.5">0.5x</option>
          <option value="1" selected>1x</option>
          <option value="2">2x</option>
          <option value="5">5x</option>
          <option value="10">10x</option>
        </select>
      </div>
      <span id="replay-time" style="min-width:90px;text-align:center;font-family:monospace;color:#e9d5ff">00:00 / 00:00</span>
      <input id="replay-slider" type="range" min="0" max="1000" value="0" step="1"
        oninput="replaySeekFromSlider(this.value)"
        style="flex:1;accent-color:#7c3aed;cursor:pointer">
      <span id="replay-frame" style="opacity:0.5;min-width:50px;text-align:right;font-family:monospace"></span>
    </div>
  `;
  document.body.appendChild(replayBarEl);
}

function mountReplayList() {
  // Modal for selecting a recording
  replayListEl = el("div", { id:"replay-list-modal", style:{
    position:"fixed", inset:"0", zIndex:"10002", display:"none",
    background:"rgba(0,0,0,0.8)", alignItems:"center", justifyContent:"center",
  }});
  replayListEl.innerHTML = `
    <div style="background:#1e1b4b;border:2px solid #7c3aed;border-radius:16px;padding:24px;max-width:600px;width:90%;max-height:70vh;overflow-y:auto;color:white;font-family:ui-sans-serif,system-ui,Arial;margin:auto;margin-top:15vh">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0;color:#a78bfa;font-size:18px">\u25B6 Kayitli Senaryolar</h2>
        <button onclick="closeReplayList()" style="background:none;border:none;color:#fff;font-size:20px;cursor:pointer">\u2715</button>
      </div>
      <div id="replay-list-body" style="font-size:13px">
        <span style="opacity:0.5">Y\u00fckleniyor...</span>
      </div>
    </div>
  `;
  document.body.appendChild(replayListEl);
}

/* ── Scenario Editor ────────────────────────────────────────── */
let scenarioModalEl = null;
let _scenarioEntities = [];  // working copy of entity list

function mountScenarioEditor() {
  // Button in topbar area
  const btn = el("button", {
    id: "scenario-open-btn",
    onclick: () => openScenarioEditor(),
    style: {
      position:"fixed", top:"12px", left:"160px", zIndex:"9999",
      background:"linear-gradient(135deg,#0ea5e9,#0369a1)", color:"#fff",
      border:"none", borderRadius:"8px", padding:"8px 14px",
      cursor:"pointer", fontWeight:"bold", fontSize:"12px",
      fontFamily:"var(--font)", boxShadow:"0 2px 10px rgba(14,165,233,0.4)",
    }
  }, ["Senaryolar"]);
  document.body.appendChild(btn);

  // Modal
  scenarioModalEl = el("div", { id:"scenario-modal", style:{
    display:"none", position:"fixed", inset:"0", zIndex:"10020",
    background:"rgba(0,0,0,0.88)", overflowY:"auto",
  }});
  scenarioModalEl.innerHTML = `
    <div style="max-width:860px;margin:30px auto;padding:24px 28px;
         background:var(--bg-1);border-radius:14px;border:1px solid var(--border);color:var(--text-1);font-family:var(--font)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="margin:0;font-size:17px;color:var(--text-1)">Senaryo Editoru</h2>
        <span id="sc-close" style="cursor:pointer;font-size:20px;color:var(--text-3);padding:0 8px">&times;</span>
      </div>
      <div style="display:grid;grid-template-columns:240px 1fr;gap:16px">
        <!-- Left: scenario list -->
        <div>
          <div class="nz-section">MEVCUT SENARYOLAR</div>
          <div id="sc-list" style="display:flex;flex-direction:column;gap:4px;margin-top:6px"></div>
          <button class="nz-btn" style="width:100%;margin-top:10px;font-size:11px" onclick="scNewScenario()">+ Yeni Senaryo</button>
        </div>
        <!-- Right: editor form -->
        <div id="sc-editor">
          <div style="color:var(--text-3);font-size:12px;padding:20px 0">Sol taraftan bir senaryo secin veya yeni olusturun.</div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(scenarioModalEl);
  document.getElementById("sc-close").addEventListener("click", () => {
    scenarioModalEl.style.display = "none";
  });
}

async function openScenarioEditor() {
  if (!scenarioModalEl) return;
  scenarioModalEl.style.display = "block";
  await scLoadList();
}

async function scLoadList() {
  const listEl = document.getElementById("sc-list");
  if (!listEl) return;
  listEl.innerHTML = `<span style="color:var(--text-3);font-size:11px">Yukleniyor...</span>`;
  try {
    const resp = await fetch("/api/scenarios");
    const data = await resp.json();
    const scenarios = data.scenarios || [];
    if (scenarios.length === 0) {
      listEl.innerHTML = `<span style="color:var(--text-3);font-size:11px">Senaryo yok</span>`;
      return;
    }
    listEl.innerHTML = "";
    scenarios.forEach(sc => {
      const item = el("div", {
        style: {
          padding:"7px 10px", borderRadius:"6px", cursor:"pointer",
          background:"var(--bg-2)", border:"1px solid var(--border)",
          fontSize:"11px", lineHeight:"1.5",
        },
        onclick: () => scLoadEditor(sc.name),
      });
      item.innerHTML = `
        <div style="font-weight:600;color:var(--text-1)">${escHtml(sc.name)}</div>
        <div style="color:var(--text-3);font-size:10px">${escHtml(sc.description || "")} | ${sc.entity_count} hedef | ${sc.duration_s}s</div>`;
      listEl.appendChild(item);
    });
  } catch(e) {
    listEl.innerHTML = `<span style="color:var(--danger);font-size:11px">Hata: ${escHtml(e.message)}</span>`;
  }
}

async function scLoadEditor(name) {
  const editorEl = document.getElementById("sc-editor");
  if (!editorEl) return;
  try {
    const resp = await fetch(`/api/scenarios/${encodeURIComponent(name)}`);
    const sc = await resp.json();
    _scenarioEntities = (sc.entities || []).map(e => ({...e}));
    scRenderEditor(sc);
  } catch(e) {
    editorEl.innerHTML = `<span style="color:var(--danger)">Hata: ${escHtml(e.message)}</span>`;
  }
}

function scNewScenario() {
  _scenarioEntities = [];
  scRenderEditor({
    name: "", description: "", duration_s: 300, rate_hz: 1.0, entities: []
  });
}

function scRenderEditor(sc) {
  const editorEl = document.getElementById("sc-editor");
  if (!editorEl) return;

  editorEl.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
      <div>
        <label style="font-size:10px;color:var(--text-3)">Ad</label>
        <input id="sc-name" class="nz-input" style="width:100%;margin-top:2px" value="${escHtml(sc.name || "")}" placeholder="ornek_senaryo">
      </div>
      <div>
        <label style="font-size:10px;color:var(--text-3)">Aciklama</label>
        <input id="sc-desc" class="nz-input" style="width:100%;margin-top:2px" value="${escHtml(sc.description || "")}" placeholder="Kisa aciklama">
      </div>
      <div>
        <label style="font-size:10px;color:var(--text-3)">Sure (saniye)</label>
        <input id="sc-dur" class="nz-input" type="number" style="width:100%;margin-top:2px" value="${sc.duration_s || 300}">
      </div>
      <div>
        <label style="font-size:10px;color:var(--text-3)">Hiz (Hz)</label>
        <input id="sc-rate" class="nz-input" type="number" step="0.1" style="width:100%;margin-top:2px" value="${sc.rate_hz || 1.0}">
      </div>
    </div>

    <div class="nz-section" style="margin-bottom:6px">HEDEFLER</div>
    <div id="sc-entities"></div>
    <div style="display:flex;gap:6px;margin-top:6px">
      <button class="nz-btn" style="font-size:11px" onclick="scAddEntity()">+ Hedef Ekle</button>
      <button class="nz-btn c-ok" style="font-size:11px;margin-left:auto" onclick="scSave()">Kaydet</button>
      <button class="nz-btn c-danger" style="font-size:11px" onclick="scDelete()">Sil</button>
    </div>
    <div id="sc-run-hint" style="margin-top:10px;font-size:10px;color:var(--text-3);font-family:var(--mono);
      background:var(--bg-2);padding:8px 10px;border-radius:6px;display:none"></div>
    <div id="sc-msg" style="margin-top:6px;font-size:11px"></div>`;

  scRenderEntities();
}

function scRenderEntities() {
  const container = document.getElementById("sc-entities");
  if (!container) return;
  if (_scenarioEntities.length === 0) {
    container.innerHTML = `<div style="color:var(--text-3);font-size:11px;padding:4px 0">Hedef yok — eklemek icin + butonuna basin</div>`;
    return;
  }
  const LABELS = ["drone","aircraft","vehicle","boat","missile"];
  container.innerHTML = `
    <div style="display:grid;grid-template-columns:100px 80px 70px 60px 65px 65px 30px;gap:4px;margin-bottom:4px;font-size:9px;color:var(--text-3)">
      <span>ID</span><span>Tip</span><span>Mesafe(m)</span><span>Az(deg)</span><span>Hiz(m/s)</span><span>Yon(deg)</span><span></span>
    </div>` +
    _scenarioEntities.map((e, i) => `
    <div style="display:grid;grid-template-columns:100px 80px 70px 60px 65px 65px 30px;gap:4px;margin-bottom:3px;align-items:center">
      <input class="nz-input" style="font-size:10px;padding:3px 5px" value="${escHtml(e.entity_id||"")}" oninput="_scenarioEntities[${i}].entity_id=this.value">
      <select class="nz-select" style="font-size:10px;padding:3px 4px" onchange="_scenarioEntities[${i}].label=this.value">
        ${LABELS.map(l=>`<option value="${l}"${e.label===l?" selected":""}>${l}</option>`).join("")}
      </select>
      <input class="nz-input" type="number" style="font-size:10px;padding:3px 5px" value="${e.range_m||1000}" oninput="_scenarioEntities[${i}].range_m=+this.value">
      <input class="nz-input" type="number" style="font-size:10px;padding:3px 5px" value="${e.az_deg||0}" oninput="_scenarioEntities[${i}].az_deg=+this.value">
      <input class="nz-input" type="number" style="font-size:10px;padding:3px 5px" value="${e.speed_mps||20}" oninput="_scenarioEntities[${i}].speed_mps=+this.value">
      <input class="nz-input" type="number" style="font-size:10px;padding:3px 5px" value="${e.heading_deg||180}" oninput="_scenarioEntities[${i}].heading_deg=+this.value">
      <button class="nz-btn c-danger" style="font-size:10px;padding:2px 6px" onclick="scRemoveEntity(${i})">×</button>
    </div>`).join("");
}

function scAddEntity() {
  const i = _scenarioEntities.length;
  _scenarioEntities.push({
    entity_id: `E-NEW-${String(i+1).padStart(2,"0")}`,
    label: "drone", range_m: 1500, az_deg: 0, speed_mps: 20, heading_deg: 180,
  });
  scRenderEntities();
}

function scRemoveEntity(i) {
  _scenarioEntities.splice(i, 1);
  scRenderEntities();
}

async function scSave() {
  const name = (document.getElementById("sc-name")?.value || "").trim();
  const msg = document.getElementById("sc-msg");
  if (!name) { if(msg) { msg.textContent = "Ad gerekli!"; msg.style.color = "var(--danger)"; } return; }
  const body = {
    name,
    description: document.getElementById("sc-desc")?.value || "",
    duration_s:  +(document.getElementById("sc-dur")?.value  || 300),
    rate_hz:     +(document.getElementById("sc-rate")?.value || 1.0),
    entities:    _scenarioEntities,
  };
  try {
    const resp = await fetch("/api/scenarios", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.ok) {
      if(msg) { msg.textContent = "Kaydedildi!"; msg.style.color = "var(--ok)"; }
      const hint = document.getElementById("sc-run-hint");
      if (hint) {
        hint.style.display = "block";
        hint.textContent = `Calistirmak icin: python start.py --scenario scenarios/${name}.json`;
      }
      await scLoadList();
    } else {
      if(msg) { msg.textContent = "Hata: " + (data.error || "?"); msg.style.color = "var(--danger)"; }
    }
  } catch(e) {
    if(msg) { msg.textContent = "Hata: " + e.message; msg.style.color = "var(--danger)"; }
  }
}

async function scDelete() {
  const name = (document.getElementById("sc-name")?.value || "").trim();
  if (!name) return;
  if (!confirm(`"${name}" senaryosunu silmek istediginize emin misiniz?`)) return;
  try {
    const resp = await fetch(`/api/scenarios/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById("sc-editor").innerHTML = `<div style="color:var(--ok);font-size:12px">Silindi.</div>`;
      await scLoadList();
    }
  } catch(e) {
    alert("Silinemedi: " + e.message);
  }
}

/* ── Admin panel: user management ────────────────────────── */

function mountAdminPanel() { /* placeholder — panel created on open */ }

async function openAdminPanel() {
  const existing = document.getElementById("admin-modal");
  if (existing) { existing.remove(); return; }

  const modal = el("div", { id: "admin-modal" });
  const box   = el("div", { class: "am-box" });
  const title = el("div", { class: "am-title" }, ["Admin Panel"]);

  // ── Tab bar ──
  const tabBar = el("div", { style: { display:"flex", gap:"6px", marginBottom:"14px" }});
  const paneUsers     = el("div", { id:"am-pane-users" });
  const paneAudit     = el("div", { id:"am-pane-audit",     style:{ display:"none" }});
  const paneAnalytics = el("div", { id:"am-pane-analytics", style:{ display:"none" }});
  const paneWebhooks  = el("div", { id:"am-pane-webhooks",  style:{ display:"none" }});
  const panes = { users: paneUsers, audit: paneAudit, analytics: paneAnalytics, webhooks: paneWebhooks };
  const tabBtns = {};
  ["users","audit","analytics","webhooks"].forEach(id => {
    const btn = el("button", {
      class: id === "users" ? "nz-btn c-ok" : "nz-btn",
      style: { fontSize:"11px", padding:"4px 10px" },
      onclick: () => {
        Object.values(panes).forEach(p => p.style.display = "none");
        Object.values(tabBtns).forEach(b => b.className = "nz-btn");
        panes[id].style.display = "";
        tabBtns[id].className = "nz-btn c-ok";
        if (id === "audit")     amLoadAudit(paneAudit);
        if (id === "analytics") amLoadAnalytics(paneAnalytics);
        if (id === "webhooks")  amLoadWebhooks(paneWebhooks);
      },
    }, [id.charAt(0).toUpperCase() + id.slice(1)]);
    tabBtns[id] = btn;
    tabBar.appendChild(btn);
  });

  // ── Users pane ──
  const listEl     = el("div", { id: "am-list" });
  const addSection = el("div", { class: "nz-write-ctrl", style: { marginTop: "14px" } });
  const nuName = el("input", { type:"text", placeholder:"Username", class:"nz-input",
    style:{ marginBottom:"6px" } });
  const nuPass = el("input", { type:"password", placeholder:"Password", class:"nz-input",
    style:{ marginBottom:"6px" } });
  const nuRole = el("select", { class:"nz-select", style:{ marginBottom:"8px" } });
  ["OPERATOR","VIEWER","ADMIN"].forEach(r => nuRole.appendChild(el("option",{value:r},[r])));
  const nuBtn  = el("button", { class:"nz-btn c-ok", style:{ width:"100%" },
    onclick: async () => {
      const resp = await authFetch("/auth/register", {
        method:"POST",
        body: JSON.stringify({ username:nuName.value, password:nuPass.value, role:nuRole.value }),
      });
      if (resp.ok) { nuName.value=""; nuPass.value=""; await amRefresh(listEl); }
      else { const e=await resp.json(); alert(e.detail || "Error"); }
    }
  }, ["+ Add User"]);
  addSection.append(el("div",{class:"nz-section",style:{marginBottom:"8px"}},["Add User"]),
    nuName, nuPass, nuRole, nuBtn);
  paneUsers.append(listEl, addSection);

  const closeBtn = el("button", {
    class: "nz-btn c-danger", style: { marginTop:"12px", width:"100%" },
    onclick: () => modal.remove(),
  }, ["Close"]);

  box.append(title, tabBar, paneUsers, paneAudit, paneAnalytics, paneWebhooks, closeBtn);
  modal.appendChild(box);
  document.body.appendChild(modal);
  modal.addEventListener("click", e => { if (e.target === modal) modal.remove(); });
  await amRefresh(listEl);
}

async function amLoadAudit(pane) {
  pane.innerHTML = `<div style="color:var(--text-3);font-size:11px;margin-bottom:8px">Last 100 actions</div>`;
  try {
    const r = await authFetch("/api/audit?limit=100");
    if (!r.ok) { pane.innerHTML += "Cannot load audit log."; return; }
    const { records } = await r.json();
    if (!records.length) { pane.innerHTML += "No audit records yet."; return; }
    const tbl = el("div", { style:{ fontSize:"10px", fontFamily:"var(--mono)" }});
    records.forEach(rec => {
      const t = rec.time ? rec.time.slice(0, 19).replace("T"," ") : "?";
      const color = rec.action.startsWith("DELETE") ? "var(--danger)"
                  : rec.action.startsWith("CREATE") ? "var(--ok)"
                  : rec.action.startsWith("APPROVE") ? "var(--accent)" : "var(--text-2)";
      tbl.appendChild(el("div", { style:{
        display:"flex", gap:"8px", padding:"3px 0",
        borderBottom:"1px solid var(--border)", alignItems:"baseline",
      }}, [
        el("span",{style:{color:"var(--text-3)",minWidth:"110px"}},[t]),
        el("span",{style:{color:"var(--text-2)",minWidth:"80px"}},[rec.username]),
        el("span",{style:{color,minWidth:"120px",fontWeight:"600"}},[rec.action]),
        el("span",{style:{color:"var(--text-3)"}},[`${rec.resource_type}/${rec.resource_id||""}`]),
      ]));
    });
    pane.appendChild(tbl);
  } catch(e) { pane.innerHTML += "Error: " + e.message; }
}

async function amLoadAnalytics(pane) {
  pane.innerHTML = `<div style="color:var(--text-3);font-size:11px;margin-bottom:8px">Last 24 hours</div>`;
  try {
    const [tr, thr, al] = await Promise.all([
      authFetch("/api/analytics/tracks").then(r=>r.json()),
      authFetch("/api/analytics/threats").then(r=>r.json()),
      authFetch("/api/analytics/alerts").then(r=>r.json()),
    ]);
    pane.appendChild(amSparkCard("Track Ingest Rate", tr.data, d => d.count, "var(--accent)"));
    pane.appendChild(amSparkCard("Zone Breach Alerts", al.data, d => d.count, "var(--danger)"));
    // Threat dist summary
    const threatSumm = el("div", { style:{ marginTop:"10px" }});
    threatSumm.appendChild(el("div",{class:"nz-section",style:{marginBottom:"6px"}},["Threat Distribution (24h)"]));
    const byLevel = {};
    (thr.data||[]).forEach(d => { byLevel[d.threat_level] = (byLevel[d.threat_level]||0) + d.count; });
    const total = Object.values(byLevel).reduce((a,b)=>a+b, 0) || 1;
    ["HIGH","MEDIUM","LOW"].forEach(lvl => {
      const cnt = byLevel[lvl] || 0;
      const pct = Math.round(cnt/total*100);
      const color = lvl==="HIGH" ? "var(--danger)" : lvl==="MEDIUM" ? "var(--warn)" : "var(--ok)";
      const bar = el("div",{style:{marginBottom:"4px"}});
      bar.innerHTML = `<div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px">
        <span style="color:${color}">${lvl}</span><span style="color:var(--text-3)">${cnt}</span></div>
        <div style="height:5px;background:var(--border);border-radius:3px">
          <div style="height:100%;width:${pct}%;background:${color};border-radius:3px;transition:width .4s"></div></div>`;
      threatSumm.appendChild(bar);
    });
    pane.appendChild(threatSumm);
  } catch(e) { pane.innerHTML += "Error: " + e.message; }
}

function amSparkCard(label, data, valFn, color) {
  const card = el("div", { style:{ marginBottom:"12px" }});
  card.appendChild(el("div",{class:"nz-section",style:{marginBottom:"4px"}},[label]));
  if (!data || !data.length) {
    card.appendChild(el("div",{style:{color:"var(--text-3)",fontSize:"10px"}},["No data"]));
    return card;
  }
  const vals = data.map(valFn);
  const max = Math.max(...vals, 1);
  const W = 340, H = 40;
  const pts = vals.map((v,i) =>
    `${Math.round(i/(vals.length-1||1)*W)},${Math.round(H - (v/max)*H)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg","svg");
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  svg.setAttribute("width","100%"); svg.setAttribute("height","40");
  const pl = document.createElementNS("http://www.w3.org/2000/svg","polyline");
  pl.setAttribute("points", pts);
  pl.setAttribute("fill","none"); pl.setAttribute("stroke", color);
  pl.setAttribute("stroke-width","2"); pl.setAttribute("stroke-linejoin","round");
  svg.appendChild(pl);
  const total = vals.reduce((a,b)=>a+b,0);
  card.appendChild(svg);
  card.appendChild(el("div",{style:{fontSize:"10px",color:"var(--text-3)",textAlign:"right"}},
    [`total: ${total}`]));
  return card;
}

async function amLoadWebhooks(pane) {
  pane.innerHTML = "";
  pane.appendChild(el("div",{class:"nz-section",style:{marginBottom:"8px"}},["Outbound Webhooks"]));
  pane.appendChild(el("div",{style:{color:"var(--text-3)",fontSize:"10px",marginBottom:"10px"}},
    ["POST to these URLs on: HIGH threat, zone breach, EW alert"]));

  const listEl = el("div", { id:"wh-list" });
  pane.appendChild(listEl);

  // Add form
  const urlIn = el("input",{ type:"url", placeholder:"https://hooks.slack.com/...",
    class:"nz-input", style:{marginBottom:"6px"} });
  const addBtn = el("button",{ class:"nz-btn c-ok", style:{width:"100%"},
    onclick: async () => {
      const url = urlIn.value.trim();
      if (!url) return;
      const r = await authFetch("/api/webhooks",{ method:"POST", body:JSON.stringify({url}) });
      if (r.ok) { urlIn.value=""; await whRefresh(listEl); }
      else { const e=await r.json(); alert(e.error || "Error"); }
    }
  },["+ Add URL"]);
  pane.append(urlIn, addBtn);
  await whRefresh(listEl);
}

async function whRefresh(listEl) {
  listEl.innerHTML = "";
  try {
    const r = await authFetch("/api/webhooks");
    if (!r.ok) { listEl.textContent = "Cannot load."; return; }
    const { webhooks } = await r.json();
    if (!webhooks.length) {
      listEl.appendChild(el("div",{style:{color:"var(--text-3)",fontSize:"11px",marginBottom:"8px"}},
        ["No webhooks registered."]));
      return;
    }
    webhooks.forEach(url => {
      const delBtn = el("button",{ class:"nz-btn c-danger",
        style:{fontSize:"10px",padding:"2px 7px"},
        onclick: async () => {
          await authFetch("/api/webhooks",{ method:"DELETE", body:JSON.stringify({url}) });
          await whRefresh(listEl);
        }
      },["Del"]);
      const row = el("div",{style:{
        display:"flex",alignItems:"center",gap:"8px",padding:"4px 0",
        borderBottom:"1px solid var(--border)",marginBottom:"4px",
      }});
      row.append(
        el("span",{style:{flex:1,fontSize:"10px",color:"var(--text-2)",wordBreak:"break-all"}},[url]),
        delBtn
      );
      listEl.appendChild(row);
    });
  } catch(e) { listEl.textContent = "Error: " + e.message; }
}

async function amRefresh(listEl) {
  listEl.innerHTML = "";
  try {
    const r = await authFetch("/auth/users");
    if (!r.ok) { listEl.textContent = "Could not load users."; return; }
    const { users } = await r.json();
    if (!users.length) { listEl.textContent = "No users."; return; }
    users.forEach(u => {
      const roleSelect = el("select", { class:"nz-select" });
      ["ADMIN","OPERATOR","VIEWER"].forEach(ro => {
        const opt = el("option", { value:ro }, [ro]);
        if (ro === u.role) opt.selected = true;
        roleSelect.appendChild(opt);
      });
      roleSelect.addEventListener("change", async () => {
        await authFetch(`/auth/users/${encodeURIComponent(u.username)}/role`, {
          method:"PUT", body:JSON.stringify({ role:roleSelect.value }),
        });
      });

      const delBtn = el("button", { class:"nz-btn c-danger",
        style:{ fontSize:"10px", padding:"2px 7px" },
        onclick: async () => {
          if (!confirm(`Delete user "${u.username}"?`)) return;
          await authFetch(`/auth/users/${encodeURIComponent(u.username)}`, { method:"DELETE" });
          await amRefresh(listEl);
        }
      }, ["Del"]);

      const row = el("div", { class:"am-user-row" });
      row.append(
        el("span", { class:"am-uname" }, [u.username]),
        roleSelect,
        delBtn,
      );
      listEl.appendChild(row);
    });
  } catch(e) {
    listEl.textContent = "Error: " + e.message;
  }
}

/* ── Shift Handover Report ───────────────────────────────── */

let _handoverModalEl = null;

function _mountHandoverModal() {
  if (_handoverModalEl) return;
  _handoverModalEl = el("div", { id:"handover-modal", style:{
    display:"none", position:"fixed", top:"0", left:"0", right:"0", bottom:"0",
    zIndex:"10100", background:"rgba(0,0,0,0.7)",
    overflow:"auto", padding:"20px",
  }});
  document.body.appendChild(_handoverModalEl);
  _handoverModalEl.addEventListener("click", e => {
    if (e.target === _handoverModalEl) _handoverModalEl.style.display = "none";
  });
}

async function openHandoverModal() {
  _mountHandoverModal();
  _handoverModalEl.innerHTML = `
    <div style="max-width:800px;margin:0 auto;background:rgba(10,12,25,0.98);
                border-radius:12px;border:1px solid rgba(100,150,255,0.2);
                padding:0;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.8)">
      <div style="background:rgba(255,255,255,0.04);padding:14px 20px;
                  display:flex;justify-content:space-between;align-items:center;
                  border-bottom:1px solid rgba(255,255,255,0.08)">
        <span style="font-size:15px;font-weight:700;color:#e0e0e0">Shift Handover Report</span>
        <div style="display:flex;gap:8px">
          <button id="ho-print-btn" style="padding:5px 14px;font-size:11px;font-weight:600;
            background:rgba(39,174,96,0.2);border:1px solid rgba(39,174,96,0.5);
            border-radius:6px;color:#2ecc71;cursor:pointer">Print / Save PDF</button>
          <button id="ho-close-btn" style="padding:5px 14px;font-size:11px;
            background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);
            border-radius:6px;color:#aaa;cursor:pointer">Close</button>
        </div>
      </div>
      <div id="ho-body" style="padding:20px;font-family:ui-sans-serif,system-ui,Arial;
                               font-size:12px;color:#e0e0e0;line-height:1.6">
        <span style="opacity:.5">Loading...</span>
      </div>
    </div>
  `;
  _handoverModalEl.style.display = "block";
  document.getElementById("ho-close-btn").addEventListener("click", () => {
    _handoverModalEl.style.display = "none";
  });
  document.getElementById("ho-print-btn").addEventListener("click", _handoverPrint);

  try {
    const data = await authFetch("/api/handover").then(r => r.json());
    _renderHandoverBody(document.getElementById("ho-body"), data);
  } catch(e) {
    document.getElementById("ho-body").innerHTML =
      `<span style="color:#e74c3c">Failed to load handover data: ${escHtml(e.message)}</span>`;
  }
}

function _renderHandoverBody(bodyEl, d) {
  const s = d.summary ?? {};
  const ts = new Date(d.generated_at).toLocaleString();
  const tlColors = { HIGH:"#e74c3c", MEDIUM:"#f39c12", LOW:"#27ae60" };

  bodyEl.innerHTML = `
    <!-- Header -->
    <div style="border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:12px;margin-bottom:16px">
      <div style="font-size:18px;font-weight:700;color:#fff;margin-bottom:4px">NIZAM COP — Shift Handover</div>
      <div style="opacity:.6;font-size:11px">
        Generated: <b>${escHtml(ts)}</b> &nbsp;|&nbsp;
        Operator: <b>${escHtml(d.generated_by ?? "?")}</b> &nbsp;|&nbsp;
        Node: <b>${escHtml(d.node_id ?? "?")}</b>
      </div>
    </div>

    <!-- Summary KPIs -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px">
      ${_hoKpi("Total Tracks", s.total_tracks ?? 0, "#4fc3f7")}
      ${_hoKpi("HIGH Threats", s.high_threats ?? 0, "#e74c3c")}
      ${_hoKpi("MEDIUM Threats", s.medium_threats ?? 0, "#f39c12")}
      ${_hoKpi("Active Zones", s.total_zones ?? 0, "#66bb6a")}
      ${_hoKpi("Pending Tasks", s.pending_tasks ?? 0, "#ff9800")}
      ${_hoKpi("Annotated Tracks", s.annotated_tracks ?? 0, "#9c27b0")}
    </div>

    <!-- Tracks table -->
    <div style="margin-bottom:18px">
      <div style="font-weight:600;font-size:12px;color:#90caf9;margin-bottom:6px;
                  border-bottom:1px solid rgba(144,202,249,0.2);padding-bottom:4px">
        Active Tracks (${(d.tracks ?? []).length})
      </div>
      ${(d.tracks ?? []).length === 0
        ? '<span style="opacity:.4">No active tracks.</span>'
        : `<table style="width:100%;border-collapse:collapse;font-size:11px">
            <tr style="opacity:.5;text-align:left">
              <th style="padding:3px 8px">ID</th>
              <th style="padding:3px 8px">Threat</th>
              <th style="padding:3px 8px">Score</th>
              <th style="padding:3px 8px">Action</th>
              <th style="padding:3px 8px">Notes</th>
            </tr>
            ${(d.tracks ?? []).map(t => `
              <tr style="border-top:1px solid rgba(255,255,255,0.05)">
                <td style="padding:3px 8px;font-weight:600;font-family:monospace">${escHtml(t.id)}</td>
                <td style="padding:3px 8px;color:${tlColors[t.threat_level]??'#aaa'}">${escHtml(t.threat_level ?? "-")}</td>
                <td style="padding:3px 8px">${t.score != null ? Number(t.score).toFixed(2) : "-"}</td>
                <td style="padding:3px 8px;opacity:.8">${escHtml(t.action ?? "-")}</td>
                <td style="padding:3px 8px">${t.annotation_count > 0 ? `\u{1F4AC}${t.annotation_count}` : "-"}</td>
              </tr>`).join("")}
          </table>`}
    </div>

    <!-- Zones -->
    <div style="margin-bottom:18px">
      <div style="font-weight:600;font-size:12px;color:#90caf9;margin-bottom:6px;
                  border-bottom:1px solid rgba(144,202,249,0.2);padding-bottom:4px">
        Active Zones (${(d.zones ?? []).length})
      </div>
      ${(d.zones ?? []).length === 0
        ? '<span style="opacity:.4">No active zones.</span>'
        : (d.zones ?? []).map(z =>
            `<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;
              border-radius:12px;background:rgba(102,187,106,0.15);
              border:1px solid rgba(102,187,106,0.3);font-size:11px">
              ${escHtml(z.name ?? z.id)} <span style="opacity:.5">(${escHtml(z.type ?? "")})</span>
            </span>`).join("")}
    </div>

    <!-- Pending Tasks -->
    <div style="margin-bottom:18px">
      <div style="font-weight:600;font-size:12px;color:#90caf9;margin-bottom:6px;
                  border-bottom:1px solid rgba(144,202,249,0.2);padding-bottom:4px">
        Pending Tasks (${(d.pending_tasks ?? []).length})
      </div>
      ${(d.pending_tasks ?? []).length === 0
        ? '<span style="opacity:.4">No pending tasks.</span>'
        : `<table style="width:100%;border-collapse:collapse;font-size:11px">
            <tr style="opacity:.5;text-align:left">
              <th style="padding:3px 8px">ID</th>
              <th style="padding:3px 8px">Track</th>
              <th style="padding:3px 8px">Action</th>
              <th style="padding:3px 8px">Proposed By</th>
            </tr>
            ${(d.pending_tasks ?? []).map(t => `
              <tr style="border-top:1px solid rgba(255,255,255,0.05)">
                <td style="padding:3px 8px;font-family:monospace;font-size:10px;opacity:.7">${escHtml(t.id ?? "-")}</td>
                <td style="padding:3px 8px;font-weight:600">${escHtml(t.track_id ?? "-")}</td>
                <td style="padding:3px 8px">${escHtml(t.action ?? "-")}</td>
                <td style="padding:3px 8px;opacity:.7">${escHtml(t.proposed_by ?? "-")}</td>
              </tr>`).join("")}
          </table>`}
    </div>

    <!-- Recent Alerts -->
    <div>
      <div style="font-weight:600;font-size:12px;color:#90caf9;margin-bottom:6px;
                  border-bottom:1px solid rgba(144,202,249,0.2);padding-bottom:4px">
        Recent Alerts (last ${(d.recent_alerts ?? []).length})
      </div>
      ${(d.recent_alerts ?? []).length === 0
        ? '<span style="opacity:.4">No recent alerts.</span>'
        : (d.recent_alerts ?? []).map(a => {
            const msg  = a.message ?? a.alert_type ?? a.subtype ?? JSON.stringify(a).slice(0,80);
            const color = (a.threat_level === "HIGH" || a.severity === "HIGH") ? "#e74c3c"
                        : (a.threat_level === "MEDIUM") ? "#f39c12" : "#aaa";
            return `<div style="padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.04);
                                font-size:11px;color:${color}">
              ${escHtml(msg)}
            </div>`;
          }).join("")}
    </div>
  `;
}

function _hoKpi(label, value, color) {
  return `<div style="background:rgba(255,255,255,0.04);border-radius:8px;
                      padding:10px 14px;border:1px solid rgba(255,255,255,0.07);
                      text-align:center">
    <div style="font-size:22px;font-weight:700;color:${color}">${value}</div>
    <div style="font-size:10px;opacity:.6;margin-top:2px">${escHtml(label)}</div>
  </div>`;
}

function _handoverPrint() {
  const body = document.getElementById("ho-body");
  if (!body) return;
  const w = window.open("", "_blank", "width=900,height=700");
  w.document.write(`
    <!DOCTYPE html><html><head>
    <meta charset="UTF-8">
    <title>NIZAM COP — Shift Handover Report</title>
    <style>
      body { background:#0a0c19; color:#e0e0e0; font-family: Arial, sans-serif;
             font-size:12px; padding:20px; margin:0; }
      table { width:100%; border-collapse:collapse; }
      th, td { padding:4px 8px; text-align:left; }
      @media print { body { background:#fff; color:#000; } }
    </style>
    </head><body>${body.innerHTML}</body></html>`);
  w.document.close();
  w.focus();
  setTimeout(() => { w.print(); }, 400);
}

/* ── Mobile FAB bar (≤767px only, CSS hides it on desktop) ── */

function mountMobileFAB() {
  let leftOpen = false, rightOpen = false;

  function toggleLeft() {
    leftOpen = !leftOpen;
    rightOpen = false;
    document.body.classList.toggle("mob-left-open",  leftOpen);
    document.body.classList.toggle("mob-right-open", rightOpen);
    btnLeft.classList.toggle("active",  leftOpen);
    btnRight.classList.toggle("active", rightOpen);
  }

  function toggleRight() {
    rightOpen = !rightOpen;
    leftOpen  = false;
    document.body.classList.toggle("mob-right-open", rightOpen);
    document.body.classList.toggle("mob-left-open",  leftOpen);
    btnRight.classList.toggle("active", rightOpen);
    btnLeft.classList.toggle("active",  leftOpen);
  }

  const btnLeft = el("button", { class:"mob-fab", onclick: toggleLeft }, [
    el("span", { class:"mob-icon" }, ["☰"]),
    "Controls",
  ]);
  const btnRight = el("button", { class:"mob-fab", onclick: toggleRight }, [
    el("span", { class:"mob-icon" }, ["⊞"]),
    "Panels",
  ]);

  const fab = el("div", { id: "mobile-fab-bar" }, [btnLeft, btnRight]);
  document.body.appendChild(fab);

  // Close drawers on map click (touch)
  document.getElementById("map")?.addEventListener("click", () => {
    if (!leftOpen && !rightOpen) return;
    leftOpen = rightOpen = false;
    document.body.classList.remove("mob-left-open", "mob-right-open");
    btnLeft.classList.remove("active");
    btnRight.classList.remove("active");
  });
}

function mountReplayButton() {
  const btn = el("button", {
    id: "replay-open-btn",
    onclick: () => openReplayList(),
    style: {
      position:"fixed", top:"12px", left:"260px", zIndex:"9999",
      background:"linear-gradient(135deg,#7c3aed,#4c1d95)", color:"#fff",
      border:"none", borderRadius:"8px", padding:"8px 14px",
      cursor:"pointer", fontWeight:"bold", fontSize:"12px",
      fontFamily:"ui-sans-serif,system-ui,Arial",
      boxShadow:"0 2px 10px rgba(124,58,237,0.4)",
    }
  }, ["\u25B6 Replay"]);
  document.body.appendChild(btn);
}

async function openReplayList() {
  replayListEl.style.display = "flex";
  const body = document.getElementById("replay-list-body");
  body.innerHTML = '<span style="opacity:0.5">Y\u00fckleniyor...</span>';
  try {
    const resp = await fetch("/api/replay/recordings").then(r=>r.json());
    const recs = resp.recordings || [];
    if (recs.length === 0) {
      body.innerHTML = '<span style="opacity:0.6">Hen\u00fcz kay\u0131t yok. Bir senaryo \u00e7al\u0131\u015ft\u0131r\u0131n, otomatik kaydedilecek.</span>';
      return;
    }
    let html = '<table style="width:100%;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #4c1d95;color:#a78bfa"><th style="text-align:left;padding:6px">Senaryo</th><th>S\u00fcre</th><th>Frame</th><th>Boyut</th><th></th></tr>';
    recs.forEach(r => {
      const dur = r.duration_s ? fmtTime(r.duration_s) : "-";
      const frames = r.total_frames || "?";
      const size = r.size_kb ? `${r.size_kb} KB` : "-";
      const scenario = r.scenario || r.filename;
      const date = r.start_time_iso ? r.start_time_iso.replace("T"," ").slice(0,19) : "";
      html += `<tr style="border-bottom:1px solid rgba(124,58,237,0.2)">
        <td style="padding:6px"><b>${scenario}</b><br><span style="opacity:0.5;font-size:11px">${date}</span></td>
        <td style="text-align:center;padding:6px">${dur}</td>
        <td style="text-align:center;padding:6px">${frames}</td>
        <td style="text-align:center;padding:6px">${size}</td>
        <td style="padding:6px"><button onclick="loadReplay('${r.filename}')" style="background:#7c3aed;color:#fff;border:none;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:11px">\u25B6 Y\u00fckle</button></td>
      </tr>`;
    });
    html += '</table>';
    body.innerHTML = html;
  } catch(e) {
    body.innerHTML = '<span style="color:#f87171">Hata: ' + e.message + '</span>';
  }
}

function closeReplayList() {
  replayListEl.style.display = "none";
}

async function loadReplay(filename) {
  closeReplayList();
  try {
    const resp = await fetch("/api/replay/load", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({filename})
    }).then(r=>r.json());
    if (!resp.ok) { alert("Y\u00fckleme hatas\u0131: " + (resp.error||"")); return; }
    replayInfo = resp.info;
    enterReplayMode();
  } catch(e) { alert("Replay y\u00fckleme hatas\u0131: " + e.message); }
}

function enterReplayMode() {
  replayActive = true;
  // Pause live mode
  CopEngine.pause();
  // Show replay bar
  replayBarEl.style.display = "block";
  // Hide replay open button
  const btn = document.getElementById("replay-open-btn");
  if (btn) btn.style.display = "none";
  // Update UI
  document.getElementById("replay-scenario").textContent = replayInfo.scenario || replayInfo.filename;
  updateReplayUI();
  // Start polling frames
  startReplayPolling();
}

function exitReplayMode() {
  replayActive = false;
  stopReplayPolling();
  replayBarEl.style.display = "none";
  const btn = document.getElementById("replay-open-btn");
  if (btn) btn.style.display = "";
  // Resume live
  CopEngine.resume(fetchCompositeSnapshot);
}

async function replayTogglePlay() {
  if (replayInfo.state === "PLAYING") {
    await fetch("/api/replay/pause", {method:"POST"});
    replayInfo.state = "PAUSED";
  } else {
    const speed = document.getElementById("replay-speed").value;
    await fetch("/api/replay/play", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({speed: parseFloat(speed)})
    });
    replayInfo.state = "PLAYING";
  }
  updateReplayUI();
}

async function replayStop() {
  await fetch("/api/replay/stop", {method:"POST"});
  replayInfo = { state:"IDLE", duration_s:0, current_elapsed_s:0, speed:1, filename:"", scenario:"" };
  exitReplayMode();
}

async function replaySetSpeed(speed) {
  await fetch("/api/replay/speed", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({speed: parseFloat(speed)})
  });
}

async function replaySeekFromSlider(val) {
  const t = (parseFloat(val) / 1000) * replayInfo.duration_s;
  await fetch("/api/replay/seek", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({elapsed_s: t})
  });
  // Immediately fetch the frame at this position
  replayFetchFrame();
}

function startReplayPolling() {
  stopReplayPolling();
  replayTimer = setInterval(replayFetchFrame, 500);
}

function stopReplayPolling() {
  if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
}

async function replayFetchFrame() {
  if (!replayActive) return;
  try {
    const resp = await fetch("/api/replay/frame").then(r=>r.json());
    if (!resp.ok) return;
    replayInfo = resp.info;
    if (resp.frame) {
      applyReplayFrame(resp.frame);
    }
    updateReplayUI();
    // Auto-stop at end
    if (replayInfo.state === "PAUSED" && replayInfo.current_elapsed_s >= replayInfo.duration_s && replayInfo.duration_s > 0) {
      document.getElementById("replay-btn-play").textContent = "\u21BB Tekrar";
    }
  } catch(e) { /* silent */ }
}

function applyReplayFrame(state) {
  // Feed the replay frame through applySnapshot
  applySnapshot(state);
  // Also apply AI overlay data if present
  if (state.predictions) drawPredictions(state.predictions);
  if (state.trajectories) drawTrajectories(state.trajectories);
  if (state.uncertainty_cones) drawUncertaintyCones(state.uncertainty_cones);
  if (state.pred_breaches) renderBreachPanel(state.pred_breaches);
  if (state.coord_attacks) renderCoordPanel(state.coord_attacks);
  if (state.roe_advisories) renderROEPanel(state.roe_advisories);
  if (state.recommendations) renderTacticalPanel(state.recommendations);
  if (state.ml_predictions) { mlPredictions = state.ml_predictions; renderMLPanel(mlPredictions); }
}

function updateReplayUI() {
  const btn = document.getElementById("replay-btn-play");
  if (btn) btn.textContent = replayInfo.state === "PLAYING" ? "\u23F8 Duraklat" : "\u25B6 Oynat";
  const slider = document.getElementById("replay-slider");
  if (slider && replayInfo.duration_s > 0) {
    slider.value = Math.round((replayInfo.current_elapsed_s / replayInfo.duration_s) * 1000);
  }
  const timeEl = document.getElementById("replay-time");
  if (timeEl) {
    timeEl.textContent = fmtTime(replayInfo.current_elapsed_s) + " / " + fmtTime(replayInfo.duration_s);
  }
}

/* fmtTime → modules/utils.js */

/* ── Auth: JWT login + RBAC ──────────────────────────────── */

let AUTH_TOKEN = localStorage.getItem("nizam_jwt") || null;
let USER_ROLE  = "ADMIN";    // default full access; overwritten by fetchUserRole()
let USER_NAME  = "anonymous";

function authHeaders() {
  return AUTH_TOKEN
    ? { "Content-Type": "application/json", "Authorization": `Bearer ${AUTH_TOKEN}` }
    : { "Content-Type": "application/json" };
}

async function authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, authHeaders(), opts.headers || {});
  const r = await fetch(url, opts);
  if (r.status === 401) { AUTH_TOKEN = null; localStorage.removeItem("nizam_jwt"); showLoginModal(); }
  return r;
}

function showLoginModal(msg = "") {
  const existing = document.getElementById("login-modal");
  if (existing) { existing.querySelector(".login-msg").textContent = msg; return; }

  const overlay = el("div", { id: "login-modal", style: {
    position: "fixed", inset: "0", zIndex: "99999",
    background: "rgba(0,0,0,0.7)", display: "flex",
    alignItems: "center", justifyContent: "center",
  }});
  const box = el("div", { style: {
    background: "#1a1a2e", color: "white", padding: "28px 32px",
    borderRadius: "12px", minWidth: "280px", fontFamily: "ui-sans-serif,system-ui,Arial",
    border: "1px solid rgba(255,255,255,0.15)",
  }});
  const title = el("div", { style: { fontSize: "16px", fontWeight: "bold", marginBottom: "16px" }}, ["🔐 NIZAM Login"]);
  const msgEl = el("div", { class: "login-msg", style: { color: "#e74c3c", fontSize: "12px", minHeight: "16px", marginBottom: "8px" }}, [msg]);
  const userInput = el("input", { type: "text", placeholder: "Username",
    style: { width: "100%", padding: "7px 10px", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.2)",
      background: "rgba(255,255,255,0.08)", color: "white", boxSizing: "border-box", marginBottom: "8px" }});
  const passInput = el("input", { type: "password", placeholder: "Password",
    style: { width: "100%", padding: "7px 10px", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.2)",
      background: "rgba(255,255,255,0.08)", color: "white", boxSizing: "border-box", marginBottom: "12px" }});
  const loginBtn = el("button", {
    style: { width: "100%", padding: "8px", background: "#2980b9", color: "white",
      border: "none", borderRadius: "6px", cursor: "pointer", fontWeight: "bold" },
    onclick: async () => {
      const resp = await fetch("/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: userInput.value, password: passInput.value }),
      });
      if (resp.ok) {
        const data = await resp.json();
        AUTH_TOKEN = data.access_token;
        localStorage.setItem("nizam_jwt", AUTH_TOKEN);
        overlay.remove();
        await fetchUserRole();
      } else {
        msgEl.textContent = "Geçersiz kullanıcı adı veya şifre";
      }
    },
  }, ["Giriş Yap"]);

  passInput.addEventListener("keydown", e => { if (e.key === "Enter") loginBtn.click(); });
  box.append(title, msgEl, userInput, passInput, loginBtn);
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  setTimeout(() => userInput.focus(), 100);
}

async function checkAuth() {
  try {
    const r = await fetch("/auth/status");
    const d = await r.json();
    if (d.auth_enabled && !AUTH_TOKEN) showLoginModal();
    else await fetchUserRole();
  } catch { /* auth endpoint not available */ }
}

async function fetchUserRole() {
  try {
    const headers = AUTH_TOKEN ? { Authorization: `Bearer ${AUTH_TOKEN}` } : {};
    const r = await fetch("/auth/me", { headers });
    if (!r.ok) return;
    const d = await r.json();
    USER_ROLE = (d.role || "ADMIN").toUpperCase();
    USER_NAME = d.username || "anonymous";
    applyRoleUI();
  } catch { /* ignore */ }
}

function applyRoleUI() {
  // Apply role class to body for CSS-based write control hiding
  document.body.dataset.role = USER_ROLE.toLowerCase();

  // Update topbar user badge
  const badge = document.getElementById("tb-user-badge");
  if (badge) {
    badge.textContent = USER_NAME + " · " + USER_ROLE;
    badge.className   = "role-" + USER_ROLE.toLowerCase();
    badge.style.display = "";
  }

  // Show/hide admin panel button
  const adminBtn = document.getElementById("admin-panel-btn");
  if (adminBtn) adminBtn.style.display = USER_ROLE === "ADMIN" ? "" : "none";
}

/* ── Right-side tab container ────────────────────────────── */

const RIGHT_TABS = {};

function setTabBadge(tabId, count) {
  const btn = document.getElementById(`rtab-${tabId}`);
  if (!btn) return;
  let badge = btn.querySelector(".nz-tab-badge");
  if (count <= 0) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "nz-tab-badge";
    btn.appendChild(badge);
  }
  badge.textContent = count > 99 ? "99+" : String(count);
}

/* ── Multi-node Federation panel ───────────────────────── */
let _nodesPanelEl = null;
let _nodesRefreshTimer = null;

function mountNodesPanel() {
  _nodesPanelEl = el("div", { id:"nodes-panel", style:{
    fontFamily:"var(--mono)", fontSize:"11px", width:"100%",
  }});
  _nodesPanelEl.innerHTML = "<span style='color:var(--text-3)'>Loading…</span>";
  RIGHT_TABS.nodes.appendChild(_nodesPanelEl);
}

async function _refreshNodesPanel() {
  if (!_nodesPanelEl) return;
  try {
    const s = await fetch("/api/sync/status").then(r => r.json());
    const peers = s.peers ?? [];
    const stats = s.push_stats ?? {};
    let html = `<div class="nz-section">Federation Nodes</div>`;
    html += `<div style="color:var(--text-3);font-size:10px;margin-bottom:6px">`;
    html += `Node: <b style="color:#3498db">${s.node_id ?? "?"}</b> &nbsp;`;
    html += `Interval: ${s.sync_interval_s ?? 5}s &nbsp; Peers: ${peers.length}`;
    if (s.conflict_count > 0) {
      html += ` &nbsp;<span style="color:var(--warn);font-weight:bold">Conflicts: ${s.conflict_count}</span>`;
    }
    html += `</div>`;

    if (peers.length === 0) {
      html += `<div style="color:var(--text-3);font-size:10px;margin-bottom:8px">No peers registered</div>`;
    } else {
      peers.forEach(p => {
        const ps = stats[p] ?? {};
        const ok = ps.ok ?? 0;
        const fail = ps.fail ?? 0;
        const last = ps.last_push_ago_s != null ? `${ps.last_push_ago_s.toFixed(0)}s ago` : "never";
        const partitioned = p.partitioned === true;
        const statusColor = partitioned ? "var(--danger)" : (fail > 0 ? "var(--warn)" : "var(--ok)");
        html += `<div class="nz-card" style="margin-bottom:4px;border-left:3px solid ${statusColor}">
          <div style="display:flex;align-items:center;gap:4px">
            <span style="color:${statusColor};font-size:10px">●</span>
            <span style="color:var(--text-1);font-size:10px;flex:1;overflow:hidden;text-overflow:ellipsis">${escHtml(p)}</span>
            <button style="font-size:9px;padding:1px 5px;background:rgba(231,76,60,0.2);border:1px solid #e74c3c;
                           border-radius:4px;color:#e74c3c;cursor:pointer"
              onclick="_removePeer(${JSON.stringify(p)})">✕</button>
          </div>
          <div style="color:var(--text-3);font-size:9px;margin-top:2px">
            ok:${ok} fail:${fail} last:${last}${partitioned ? ` <span style="color:var(--danger);font-weight:bold">PARTITIONED ${p.partition_duration_s ?? "?"}s</span>` : ""}
          </div>
        </div>`;
      });
    }

    // Add peer form
    html += `<div style="margin-top:8px;display:flex;gap:4px">
      <input id="nodes-peer-url" placeholder="http://peer:8100" style="flex:1;font-size:10px;
             background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.15);
             border-radius:4px;padding:3px 6px;color:#fff;min-width:0"/>
      <button onclick="_addPeer()" style="font-size:10px;padding:3px 8px;background:rgba(52,152,219,0.2);
              border:1px solid #3498db;border-radius:4px;color:#3498db;cursor:pointer;white-space:nowrap">+ Add</button>
    </div>`;

    _nodesPanelEl.innerHTML = html;
  } catch (e) {
    _nodesPanelEl.innerHTML = `<span style="color:var(--danger)">Error: ${escHtml(e.message)}</span>`;
  }
}

async function _addPeer() {
  const inp = document.getElementById("nodes-peer-url");
  const url = (inp?.value ?? "").trim();
  if (!url) return;
  try {
    await fetch("/api/sync/peers", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ url }) });
    inp.value = "";
    _refreshNodesPanel();
  } catch (e) { alert("Failed to add peer: " + e.message); }
}

async function _removePeer(url) {
  try {
    await fetch("/api/sync/peers", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ url, action: "remove" }) });
    _refreshNodesPanel();
  } catch (e) { alert("Failed to remove peer: " + e.message); }
}

/* ── Kill Chain Panel ────────────────────────────────────── */
const _KC_STAGES = [
  { id:"DETECTED",   label:"Tespit",     color:"#3498db" },
  { id:"CLASSIFIED", label:"Sinif",      color:"#9b59b6" },
  { id:"ROE",        label:"ROE",        color:"#e67e22" },
  { id:"TASKED",     label:"Gorev",      color:"#e74c3c" },
  { id:"ENGAGED",    label:"Angaje",     color:"#c0392b" },
];
// Stage colours for track rows
const _KC_LEVEL_C = { HIGH:"#e74c3c", MEDIUM:"#f39c12", LOW:"#27ae60", UNKNOWN:"#aaa" };

function mountKillChainPanel() {
  const panel = RIGHT_TABS["kill"];
  if (!panel) return;
  panel.innerHTML = `
    <div class="nz-section">Kill Chain Durumu</div>
    <div id="kc-funnel" style="display:flex;gap:3px;margin-bottom:10px;align-items:flex-end"></div>
    <div id="kc-track-list" style="overflow-y:auto;max-height:350px"></div>`;
  refreshKillChain();
  setInterval(refreshKillChain, 5000);
}

async function refreshKillChain() {
  const funnelEl = document.getElementById("kc-funnel");
  const listEl   = document.getElementById("kc-track-list");
  if (!funnelEl) return;
  try {
    const data = await fetch("/api/ai/kill_chain", {headers:authHeaders()}).then(r=>r.json());
    const counts = data.stage_counts || {};
    const total  = data.total || 0;
    const pipeline = data.pipeline || [];

    // ── Funnel bars ──
    const maxCount = Math.max(1, ...Object.values(counts));
    funnelEl.innerHTML = _KC_STAGES.map(s => {
      const n   = counts[s.id] || 0;
      const pct = total > 0 ? Math.round(100 * n / total) : 0;
      const h   = Math.max(18, Math.round(60 * n / maxCount));
      return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:3px">
        <div style="font-size:9px;color:${s.color};font-weight:bold">${n}</div>
        <div style="width:100%;height:${h}px;background:${s.color}33;border:1px solid ${s.color}88;
             border-radius:4px 4px 0 0;transition:height .4s" title="${s.label}: ${n} (${pct}%)"></div>
        <div style="font-size:9px;opacity:.7;text-align:center;line-height:1.1">${s.label}</div>
        <div style="font-size:8px;opacity:.45">${pct}%</div>
      </div>`;
    }).join("");

    // ── Track rows (ROE+ only) ──
    const interesting = pipeline.filter(p => ["ROE","TASKED","ENGAGED"].includes(p.stage));
    if (!listEl) return;
    if (interesting.length === 0) {
      listEl.innerHTML = `<div style="opacity:.4;font-size:10px;padding:8px 0">ROE veya gorev atanan hedef yok.</div>`;
      return;
    }
    const stageOrder = {ENGAGED:0, TASKED:1, ROE:2, CLASSIFIED:3, DETECTED:4};
    interesting.sort((a,b) => (stageOrder[a.stage]||9) - (stageOrder[b.stage]||9));

    listEl.innerHTML = interesting.map(p => {
      const sc  = _KC_STAGES.find(s => s.id === p.stage);
      const lc  = _KC_LEVEL_C[p.threat_level] || "#aaa";
      return `<div style="border-left:3px solid ${sc?.color||'#888'};padding:4px 6px;margin-bottom:4px;
                          background:rgba(255,255,255,0.03);border-radius:0 4px 4px 0">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:bold;font-size:11px">${escHtml(p.track_id)}</span>
          <span style="font-size:9px;background:${sc?.color||'#888'}22;color:${sc?.color||'#888'};
                       border:1px solid ${sc?.color||'#888'}66;border-radius:10px;padding:1px 6px">${p.stage}</span>
        </div>
        <div style="font-size:10px;opacity:.7;margin-top:2px">
          <span style="color:${lc}">${p.threat_level}</span>
          <span style="margin-left:6px;opacity:.8">${escHtml(p.intent||'unknown')}</span>
          ${p.engagement ? `<span style="margin-left:6px;color:#e67e22;font-size:9px">${escHtml(p.engagement)}</span>` : ''}
        </div>
      </div>`;
    }).join("");
  } catch(e) {
    if (funnelEl) funnelEl.innerHTML = `<span style="opacity:.4;font-size:10px">Yüklenemedi</span>`;
  }
}

function mountRightTabContainer() {
  const TAB_DEFS = [
    { id: "threats", label: "Threats" },
    { id: "zones",   label: "Zones"   },
    { id: "assets",  label: "Assets"  },
    { id: "tasks",   label: "Tasks"   },
    { id: "alerts",  label: "Alerts"  },
    { id: "ew",      label: "EW"      },
    { id: "metrics", label: "Metrics" },
    { id: "ai",      label: "AI"      },
    { id: "kill",    label: "Kill"    },
    { id: "nodes",   label: "Nodes"   },
  ];
  const activeTabId = { v: "threats" };

  const container = el("div", { id: "right-sidebar", style: {
    position: "fixed", top: "52px", right: "12px", zIndex: "9998",
    width: "300px", maxHeight: "calc(100vh - 64px)",
    display: "flex", flexDirection: "column",
    pointerEvents: "none",
  }});

  const tabBar = el("div", { class: "nz-tab-bar" });
  const contentMap = {};

  function switchTab(tabId) {
    TAB_DEFS.forEach(t => {
      contentMap[t.id].style.display = t.id === tabId ? "flex" : "none";
      const btn = document.getElementById(`rtab-${t.id}`);
      if (btn) btn.className = t.id === tabId ? "nz-tab active" : "nz-tab";
    });
    activeTabId.v = tabId;
    // Clear EW badge when EW tab is opened
    if (tabId === "ew") { _ewBadgeCount = 0; setTabBadge("ew", 0); }
  }

  TAB_DEFS.forEach(tab => {
    const content = el("div", {
      id: `rtab-content-${tab.id}`,
      class: "nz-tab-pane",
      style: { display: tab.id === activeTabId.v ? "flex" : "none" },
    });
    contentMap[tab.id] = content;
    RIGHT_TABS[tab.id] = content;
    container.appendChild(content);

    const btn = el("button", {
      id: `rtab-${tab.id}`,
      class: tab.id === activeTabId.v ? "nz-tab active" : "nz-tab",
      onclick: () => switchTab(tab.id),
    }, [tab.label]);
    tabBar.appendChild(btn);
  });

  container.insertBefore(tabBar, container.firstChild);
  document.body.appendChild(container);
}

/* ── Boot ────────────────────────────────────────────────── */

function mountCoordBar() {
  const bar = document.createElement("div");
  bar.id = "coord-bar";
  bar.innerHTML = `<span id="cb-lat">–</span><span class="sep">°N</span>
    <span id="cb-lon">–</span><span class="sep">°E</span>
    <span class="sep">|</span>
    <span id="cb-zoom">z10</span>
    <span class="sep">|</span>
    <span id="cb-tile" style="cursor:pointer;color:var(--accent)" title="Toggle tile layer">OSM</span>`;
  document.body.appendChild(bar);

  // Tile layers
  const TILES = {
    OSM: L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom:19, attribution:"&copy; OSM" }),
    SAT: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom:19, attribution:"&copy; Esri" }),
  };
  let currentTile = "OSM";
  TILES.OSM.addTo(UI.map);

  document.getElementById("cb-tile").addEventListener("click", () => {
    TILES[currentTile].remove();
    currentTile = currentTile === "OSM" ? "SAT" : "OSM";
    TILES[currentTile].addTo(UI.map);
    document.getElementById("cb-tile").textContent = currentTile;
  });

  UI.map.on("mousemove", e => {
    const lat = document.getElementById("cb-lat");
    const lon = document.getElementById("cb-lon");
    if (lat) lat.textContent = e.latlng.lat.toFixed(4);
    if (lon) lon.textContent = e.latlng.lng.toFixed(4);
  });
  UI.map.on("zoomend", () => {
    const z = document.getElementById("cb-zoom");
    if (z) z.textContent = `z${UI.map.getZoom()}`;
  });
}

function boot(){
  mountTopBar();
  initMap();
  mountControls();
  mountRightTabContainer();
  mountThreatList();
  mountCoordBar();
  mountZonePanel();
  mountAgentPanel();
  mountAlertPanel();
  mountEWPanel();
  mountTaskPanel();
  mountAssetPanel();
  mountMissionPanel();
  // Phase 5: AI panels
  mountEscalationBanner();
  mountMLPanel();
  mountROEPanel();
  mountConfidencePanel();
  mountCoordPanel();
  mountBreachPanel();
  mountAnomalyPanel();
  mountMetricsPanel();
  mountTacticalPanel();
  mountTimelinePopup();
  mountLineageModal();
  mountOperatorPanel();
  mountAIAdvisorPanel();
  mountChatPanel();
  mountChatToggle();
  mountAARModal();
  mountAARButton();
  mountAuditModal();
  mountKillChainPanel();
  mountAssignmentPanel();
  mountBFTPanel();
  mountEffectorStatusPanel();
  mountDriftPanel();
  // Federation nodes panel
  mountNodesPanel();
  _refreshNodesPanel();
  setInterval(_refreshNodesPanel, 10000);
  // Scenario editor
  mountScenarioEditor();
  // Admin panel (content loaded on demand)
  mountAdminPanel();
  // Mobile FAB bar (hidden on desktop via CSS)
  mountMobileFAB();
  // Replay system
  mountReplayBar();
  mountReplayList();
  mountReplayButton();
  connectWS();
  refreshAgentHealth();
  setInterval(refreshAgentHealth, 5000);
  setInterval(updateTopBar, 1000);
  // AI data now streams via WebSocket (cop.ai_update).
  // Keep slow polling as a safety net for when the socket drops.
  setInterval(refreshAI, 15000);
  checkAuth();
}
document.addEventListener("DOMContentLoaded", () => {
  try { boot(); }
  catch(e) {
    document.body.style.cssText = "background:#111;color:#f55;padding:20px;font:14px monospace";
    document.body.innerHTML = "<h2>NIZAM Boot Error</h2><pre>" + e.stack + "</pre>";
  }
});
