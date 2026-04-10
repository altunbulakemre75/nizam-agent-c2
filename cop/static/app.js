/* ==========================================================
   NIZAM COP — cop/static/app.js  (Phase 5)
   Phase 1: tracks, threats, zones, zone-breach alerts
   Phase 2: trail polylines, intent badges, EO sensor, ML scoring
   Phase 3: asset management, autonomous task queue, mission waypoints
   Phase 5: AI decision support (predictions, anomalies, tactical, LLM chat)
   ========================================================== */

/* ── Utilities ──────────────────────────────────────────── */
function $(sel) { return document.querySelector(sel); }
function el(tag, attrs = {}, children = []) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "style") Object.assign(n.style, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) n.setAttribute(k, String(v));
  });
  children.forEach(c => n.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
  return n;
}
function safeJsonParse(s) { try { return JSON.parse(s); } catch { return null; } }

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

/* ── Threat / intent colours ────────────────────────────── */
const THREAT_COLORS = { HIGH:"#e74c3c", MEDIUM:"#f39c12", LOW:"#27ae60" };
const INTENT_META = {
  attack:        {color:"#e74c3c", icon:"!", label:"ATTACK"},
  reconnaissance:{color:"#9b59b6", icon:"@", label:"RECON"},
  loitering:     {color:"#e67e22", icon:"O", label:"LOITER"},
  unknown:       {color:"#95a5a6", icon:"?", label:"UNKNOWN"},
};

function makeThreatIcon(level, intent) {
  const c = THREAT_COLORS[level] ?? "#2980b9";
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
  return `<div style="font:12px/1.5 monospace;min-width:175px">
    <b style="font-size:13px">${id}</b><br>
    <span style="color:${THREAT_COLORS[level]??'#aaa'}"> ${level}</span>
    \u00a0score:<b>${score}</b>
    \u00a0<span style="color:${mlColor}">ML:${mlLevel}(${mlProb})</span><br>
    TTI:<b>${tti}</b> Vr:<b>${vr}</b> Range:<b>${range}</b><br>
    Intent:<span style="color:${im.color}">${im.icon} ${im.label}</span>(${(iconf*100).toFixed(0)}%)<br>
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
  const icon  =makeThreatIcon(level, intent);
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

function upsertThreat(threat) {
  const id=String(threat.id??threat.global_track_id??threat.threat_id??""); if(!id) return;
  UI.threats.set(id,threat);
  const marker=UI.trackMarkers.get(id), track=UI.tracks.get(id);
  if(marker&&track){
    const level=threat.threat_level??"LOW";
    marker.setIcon(makeThreatIcon(level,track.intent??threat.intent??"unknown"));
    marker.setTooltipContent(buildTooltip(track,threat));
    const pl=UI.trackPolylines.get(id);
    if(pl) pl.setStyle({color:THREAT_COLORS[level]??"#2980b9"});
  }
  _scheduleRenderThreatList();
  updateThreatIntelCard();
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
  renderROEPanel(payload.roe_advisories || []);
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
  // Auto-refresh open timeline chart
  if (timelineCurrentTrack) fetchAndDrawTimeline(timelineCurrentTrack);
}

/* ── Zone breach alert panel ─────────────────────────────── */
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
  } else if (p.severity === "HIGH") {
    _ewToast(`EW: ${p.type}`, "var(--warn)");
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

const ACTION_COLORS = { ENGAGE:"var(--danger)", OBSERVE:"var(--warn)", EVADE:"var(--accent)" };
const ACTION_BG     = { ENGAGE:"rgba(240,64,64,0.1)", OBSERVE:"rgba(240,128,48,0.1)", EVADE:"rgba(79,127,255,0.1)" };

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
  if(task.status!=="PENDING") pendingTasks.delete(task.id);
  else pendingTasks.set(task.id, task);
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

  assetPanelEl.appendChild(header);
  assetPanelEl.appendChild(mkBtn("+ Friendly","friendly","c-ok"));
  assetPanelEl.appendChild(mkBtn("+ Hostile","hostile","c-danger"));
  assetPanelEl.appendChild(el("button",{class:"nz-btn",style:{width:"100%",marginBottom:"4px"},onclick:()=>startAssetPlace("unknown")},["+ Unknown"]));
  assetPanelEl.appendChild(hint);
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
      case "cop.track_merged":    return pushTrackMerged(ev.payload);
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
function connectWS(){
  const proto=location.protocol==="https:"?"wss":"ws";
  const url=`${proto}://${location.host}/ws?operator_id=${encodeURIComponent(MY_OPERATOR_ID)}`;
  setStatus(`WS: connecting ${url}`);
  let ws;
  try{ws=new WebSocket(url);}
  catch(e){setStatus(`WS: failed (${e})`);setTimeout(connectWS,1200);return;}
  UI.ws=ws;
  ws.onopen =()=>{UI.wsConnected=true; setStatus(`WS: connected (live) · ${MY_OPERATOR_ID}`);};
  ws.onclose=()=>{UI.wsConnected=false;setStatus("WS: closed (reconnecting...)");setTimeout(connectWS,1200);};
  ws.onerror=()=>{};
  ws.onmessage=msg=>{const ev=safeJsonParse(msg.data);if(ev)CopEngine.onEvent(ev);};
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
  panel.appendChild(el("div",{style:{display:"flex",gap:"4px"}},[btnDraw,btnSave,btnCancel]));
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
  breachPanelEl = el("div", { id:"breach-panel", style:{
    background:"rgba(40,0,0,0.82)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", maxHeight:"180px", overflowY:"auto",
    border:"1px solid rgba(231,76,60,0.4)",
  }});
  breachPanelEl.innerHTML = "<b>\u26A0 Predictive Breach</b><br><span style='opacity:.5'>No predicted breaches</span>";
  RIGHT_TABS.threats.appendChild(breachPanelEl);
}

function renderBreachPanel(breaches) {
  if(!breachPanelEl) return;
  if(!breaches || breaches.length === 0) {
    breachPanelEl.innerHTML = "<b>\u26A0 Predictive Breach</b><br><span style='opacity:.5'>No predicted breaches</span>";
    breachPanelEl.style.borderColor = "rgba(231,76,60,0.4)";
    return;
  }
  let html = `<b>\u26A0 Predictive Breach</b> <span style="opacity:.6">${breaches.length} warning(s)</span><br>`;
  breaches.slice(0, 6).forEach(b => {
    const sevColor = b.severity === "CRITICAL" ? "#e74c3c" : "#f39c12";
    const confBadge = b.confidence === "HIGH"
      ? '<span style="background:#e74c3c;padding:0 4px;border-radius:3px;font-size:8px">CERTAIN</span>'
      : '<span style="background:#f39c12;padding:0 4px;border-radius:3px;font-size:8px">PROBABLE</span>';
    html += `<div style="border-left:3px solid ${sevColor};padding-left:5px;margin:3px 0">
      <span style="color:${sevColor};font-weight:bold">${b.track_id}</span>
      \u2192 ${b.zone_name} ${confBadge}<br>
      <span style="font-size:9px;opacity:.8">\u23F1 ${b.time_to_breach_s}s | ${b.current_distance_m}m | ${b.zone_type}</span>
    </div>`;
  });
  breachPanelEl.innerHTML = html;
  // Flash on new critical breaches
  breachPanelEl.style.borderColor = "#e74c3c";
  setTimeout(() => { breachPanelEl.style.borderColor = "rgba(231,76,60,0.4)"; }, 1200);
}

/* ── AI: ROE Advisory panel ────────────────────────────── */
/* ── ML Threat Panel ────────────────────────────────────── */
let mlPanelEl = null;
let mlModelAvailable = false;

function mountMLPanel() {
  mlPanelEl = el("div", { id:"ml-panel", style:{
    background:"rgba(15,10,50,0.88)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"11px",
    lineHeight:"1.5", maxHeight:"220px", overflowY:"auto",
    border:"1px solid rgba(99,102,241,0.4)",
  }});
  mlPanelEl.innerHTML = '<b style="color:#818cf8">ML Model</b><br><span style="opacity:.5">Bekleniyor...</span>';
  RIGHT_TABS.threats.appendChild(mlPanelEl);
}

function renderMLPanel(preds) {
  if (!mlPanelEl) return;
  const entries = Object.entries(preds);
  if (entries.length === 0) {
    mlPanelEl.innerHTML = '<b style="color:#818cf8">ML Model</b><br><span style="opacity:.5">' +
      (mlModelAvailable ? 'Veri bekleniyor...' : 'Model yok — egitim gerekli') + '</span>';
    return;
  }
  // Sort by probability desc, show top 8
  const sorted = entries
    .map(([tid,p]) => ({tid, ...p}))
    .sort((a,b) => (b.ml_probability||0) - (a.ml_probability||0))
    .slice(0, 8);

  let html = `<b style="color:#818cf8">\u2699 ML Threat</b> <span style="opacity:.5">(${entries.length} track)</span><br>`;
  sorted.forEach(p => {
    const c = THREAT_COLORS[p.ml_level] || "#6366f1";
    const pct = p.ml_probability != null ? (p.ml_probability * 100).toFixed(0) : "?";
    // Mini probability bar
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
  mlPanelEl.innerHTML = html;
}

let roePanelEl = null;

const ROE_COLORS = {
  WEAPONS_FREE:"#e74c3c", WEAPONS_TIGHT:"#e67e22", WEAPONS_HOLD:"#f39c12",
  WARN:"#9b59b6", TRACK_ONLY:"#3498db", HOLD_FIRE:"#27ae60",
};
const ROE_LABELS = {
  WEAPONS_FREE:"SERBEST", WEAPONS_TIGHT:"KOSITLI", WEAPONS_HOLD:"SAVUNMA",
  WARN:"UYAR", TRACK_ONLY:"IZLE", HOLD_FIRE:"ATES ETME",
};

function mountROEPanel() {
  roePanelEl = el("div", { id:"roe-panel", style:{
    background:"rgba(20,10,40,0.88)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", maxHeight:"200px", overflowY:"auto",
    border:"1px solid rgba(155,89,182,0.4)",
  }});
  roePanelEl.innerHTML = "<b>\u2694 ROE Advisory</b><br><span style='opacity:.5'>No engagements</span>";
  RIGHT_TABS.threats.appendChild(roePanelEl);
}

function renderROEPanel(advisories) {
  if(!roePanelEl) return;
  if(!advisories || advisories.length === 0) {
    roePanelEl.innerHTML = "<b>\u2694 ROE Advisory</b><br><span style='opacity:.5'>No engagements</span>";
    roePanelEl.style.borderColor = "rgba(155,89,182,0.4)";
    return;
  }

  let html = `<b>\u2694 ROE Advisory</b> <span style="opacity:.6">${advisories.length} active</span><br>`;
  advisories.slice(0, 8).forEach(a => {
    const color = ROE_COLORS[a.engagement] || "#aaa";
    const label = ROE_LABELS[a.engagement] || a.engagement;
    const urgColor = {CRITICAL:"#e74c3c",HIGH:"#e67e22",MEDIUM:"#f39c12",LOW:"#95a5a6"}[a.urgency] || "#aaa";

    const badges = [];
    if(a.is_coordinated) badges.push('<span style="background:#ff0050;padding:0 3px;border-radius:3px;font-size:7px">KOORD</span>');
    if(a.in_kill_zone) badges.push('<span style="background:#e74c3c;padding:0 3px;border-radius:3px;font-size:7px">KILL</span>');

    html += `<div style="border-left:3px solid ${color};padding-left:5px;margin:3px 0">
      <span style="background:${color};padding:1px 5px;border-radius:3px;font-size:9px;font-weight:bold;color:#fff">${label}</span>
      <span style="font-weight:bold;margin-left:3px">${a.track_id}</span>
      ${badges.join(" ")}
      <span style="float:right;color:${urgColor};font-size:9px;font-weight:bold">${a.urgency}</span><br>
      <span style="font-size:9px;opacity:.75">${(a.reasons||[]).join("; ")}</span>
    </div>`;
  });

  roePanelEl.innerHTML = html;
  // Flash on CRITICAL
  const hasCritical = advisories.some(a => a.urgency === "CRITICAL");
  if(hasCritical) {
    roePanelEl.style.borderColor = "#e74c3c";
    setTimeout(() => { roePanelEl.style.borderColor = "rgba(155,89,182,0.4)"; }, 1500);
  }
}

/* ── AI: Coordinated Attack panel + convergence lines ──── */
let coordPanelEl = null;
const convergenceMarkers = new Map(); // key -> L.circleMarker
const convergenceLines   = new Map(); // key -> [L.polyline, ...]

function mountCoordPanel() {
  coordPanelEl = el("div", { id:"coord-panel", style:{
    background:"rgba(80,0,40,0.85)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", maxHeight:"180px", overflowY:"auto",
    border:"1px solid rgba(255,0,80,0.4)",
  }});
  coordPanelEl.innerHTML = "<b>\u2694 Coordinated Attack</b><br><span style='opacity:.5'>No coordinated threats</span>";
  RIGHT_TABS.threats.appendChild(coordPanelEl);
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

const ANOMALY_COLORS = {
  CRITICAL: "#e74c3c",
  HIGH:     "#e67e22",
  MEDIUM:   "#f1c40f",
  LOW:      "#95a5a6",
};

function mountAnomalyPanel() {
  const wrap = el("div", { style: { width:"100%" } });

  // Stats header
  const statsRow = el("div", { id:"anomaly-stats", style:{
    display:"flex", gap:"4px", flexWrap:"wrap", marginBottom:"6px",
  }});
  statsRow.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali bekleniyor...</span>`;

  // Event list
  anomalyPanelEl = el("div", { id:"anomaly-panel", style:{
    width:"100%", display:"flex", flexDirection:"column", gap:"3px",
  }});
  anomalyPanelEl.innerHTML = `<span style="color:var(--text-3);font-size:10px">Anomali yok</span>`;

  wrap.appendChild(statsRow);
  wrap.appendChild(anomalyPanelEl);
  RIGHT_TABS.threats.appendChild(wrap);
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

function _fmtMs(v)  { return (v == null ? "—" : (+v).toFixed(0) + " ms"); }
function _fmtNum(v) { return (v == null ? "—" : (+v).toLocaleString()); }

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

function _barColor(val, warn, crit) {
  if (val >= crit) return "var(--danger)";
  if (val >= warn) return "var(--warn)";
  return "var(--ok)";
}

function renderMetricsPanel(m) {
  if (!metricsPanelEl) return;
  const ing = m.ingest    || {};
  const tac = m.tactical  || {};
  const ws  = m.websocket || {};
  const st  = m.state     || {};
  const upMin = ((m.uptime_s || 0) / 60).toFixed(1);
  const rps   = (+ing.per_sec || 0).toFixed(1);
  const p50c  = _barColor(+tac.p50_ms||0, 500, 2000);
  const p95c  = _barColor(+tac.p95_ms||0, 500, 2000);

  const row = (label, val, cls="") =>
    `<div class="nz-row"><span class="label">${label}</span><span class="val ${cls}">${val}</span></div>`;

  const bar = (label, val, max, color) => {
    const pct = Math.min(100, Math.round(((val||0)/max)*100));
    return `<div class="nz-bar-wrap">
      <span class="nz-bar-label">${label}</span>
      <div class="nz-bar-track"><div class="nz-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="nz-bar-val">${_fmtMs(val)}</span>
    </div>`;
  };

  let typesHtml = Object.entries(ing.by_type||{})
    .map(([k,v]) => row(k, _fmtNum(v))).join("") || row("–","–");

  metricsPanelEl.innerHTML = `
    <div class="nz-section">Server Metrics <span style="float:right;font-weight:400">${upMin}m up</span></div>

    <div class="nz-card" style="margin-bottom:5px">
      <div class="nz-section" style="margin-bottom:4px">Ingest</div>
      ${row("total", _fmtNum(ing.total), "c-accent")}
      ${row("per second", rps, "c-ok")}
      ${row("bad", _fmtNum(ing.bad_request))}
      <div style="margin-top:4px;border-top:1px solid var(--border);padding-top:4px">${typesHtml}</div>
    </div>

    <div class="nz-card" style="margin-bottom:5px">
      <div class="nz-section" style="margin-bottom:4px">Tactical Engine</div>
      ${bar("p50", tac.p50_ms, 3000, p50c)}
      ${bar("p95", tac.p95_ms, 3000, p95c)}
      ${row("ran / sched", `${_fmtNum(tac.ran)} / ${_fmtNum(tac.scheduled)}`)}
      ${row("last / max", `${_fmtMs(tac.last_ms)} / ${_fmtMs(tac.max_ms)}`)}
    </div>

    <div class="nz-card" style="margin-bottom:5px">
      <div class="nz-section" style="margin-bottom:4px">WebSocket</div>
      ${row("clients", _fmtNum(ws.clients), "c-accent")}
      ${row("broadcasts", _fmtNum(ws.broadcasts))}
      ${row("sent / fail", `${_fmtNum(ws.messages_sent)} / ${_fmtNum(ws.send_failures)}`)}
    </div>

    <div class="nz-card">
      <div class="nz-section" style="margin-bottom:4px">State</div>
      ${row("tracks", _fmtNum(st.tracks), "c-accent")}
      ${row("threats", _fmtNum(st.threats), "c-danger")}
      ${row("assets / zones / tasks", `${_fmtNum(st.assets)} / ${_fmtNum(st.zones)} / ${_fmtNum(st.tasks)}`)}
    </div>
  `;
}

/* ── AI: Tactical recommendations panel ─────────────────── */
let tacPanelEl = null;

const TAC_ICONS = {
  INTERCEPT: "\u2694",     // crossed swords
  ZONE_WARNING: "\u26A0",  // warning
  ESCALATE: "\u2B06",      // up arrow
  WITHDRAW: "\u21A9",      // return arrow
  MONITOR: "\u{1F441}",    // eye
  REPOSITION: "\u27A1",    // right arrow
};
const TAC_COLORS = {
  INTERCEPT: "#e74c3c",
  ZONE_WARNING: "#f39c12",
  ESCALATE: "#e74c3c",
  WITHDRAW: "#3498db",
  MONITOR: "#9b59b6",
  REPOSITION: "#1abc9c",
};

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
  if(!tacPanelEl) return;
  if(!recs || recs.length === 0) {
    tacPanelEl.innerHTML = "<b>AI Tactical</b><br><span style='opacity:.5'>No recommendations</span>";
    return;
  }
  let html = `<b>AI Tactical</b> <span style="opacity:.6">${recs.length} active</span><br>`;
  recs.slice(0, 8).forEach(r => {
    const icon = TAC_ICONS[r.type] || "\u2022";
    const color = TAC_COLORS[r.type] || "#aaa";
    html += `<div style="border-left:3px solid ${color};padding-left:5px;margin:3px 0">
      <span style="font-size:12px">${icon}</span>
      <span style="color:${color};font-weight:bold"> ${r.type}</span>
      <span style="opacity:.6"> P${r.priority}</span><br>
      <span style="font-size:9px">${r.message || ""}</span>
    </div>`;
  });
  tacPanelEl.innerHTML = html;
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

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

/* ── AI: toggle chat button (floating) ─────────────────── */
function mountChatToggle() { /* replaced by AI sidebar tab */ }

/* ── AI: Threat Timeline chart (canvas popup) ──────────── */
let timelinePopupEl = null;
let timelineCurrentTrack = null;

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
  `;
  document.body.appendChild(timelinePopupEl);
  document.getElementById("tl-close").addEventListener("click", () => {
    timelinePopupEl.style.display = "none";
    timelineCurrentTrack = null;
  });
}

function openTimeline(trackId) {
  if(!timelinePopupEl) return;
  timelineCurrentTrack = trackId;
  document.getElementById("tl-title").textContent = `Timeline: ${trackId}`;
  timelinePopupEl.style.display = "block";
  fetchAndDrawTimeline(trackId);
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

function _esc(s) {
  const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML;
}

/* ── Multi-operator ─────────────────────────────────────── */

let operatorPanelEl = null;

// Assign stable colors to operator IDs
const _OP_COLORS = ["#4fc3f7","#a5d6a7","#ffcc80","#ef9a9a","#ce93d8","#80cbc4","#fff176"];
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
            padding:5px 12px;cursor:pointer;margin-right:8px;font-size:11px">JSON Indir</button>
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

function fmtTime(s) {
  if (!s || s < 0) return "00:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return String(m).padStart(2,"0") + ":" + String(sec).padStart(2,"0");
}

/* ── Auth: JWT login ─────────────────────────────────────── */

let AUTH_TOKEN = localStorage.getItem("nizam_jwt") || null;

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
  } catch { /* auth endpoint not available */ }
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
  mountMLPanel();
  mountROEPanel();
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
