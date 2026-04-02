/* ==========================================================
   NIZAM COP — cop/static/app.js (drop-in, from scratch)
   - Leaflet map + markers
   - WebSocket (/ws) event stream: snapshot + track + threat
   - Pause/Resume buffering (UI-side)
   - Reset (POST /api/reset)
   ========================================================== */

/* ---------------------------
   Small utilities
--------------------------- */
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
function safeJsonParse(s) {
  try { return JSON.parse(s); } catch { return null; }
}

/* ---------------------------
   Global UI State
--------------------------- */
const UI = {
  mode: "LIVE", // LIVE | PAUSED | RESUMING
  ws: null,
  wsConnected: false,
  buffer: [],
  bufferMax: 1000,
  // COP state (UI copy)
  tracks: new Map(),   // id -> track payload
  threats: new Map(),  // id -> threat payload (optional)
  // Leaflet stuff
  map: null,
  trackMarkers: new Map(), // id -> L.Marker
  lastCenter: null,
};

/* ---------------------------
   Create / ensure map container
--------------------------- */
function ensureMapContainer() {
  let mapDiv = $("#map");
  if (!mapDiv) {
    mapDiv = el("div", {
      id: "map",
      style: {
        position: "fixed",
        inset: "0",
        width: "100vw",
        height: "100vh",
      }
    });
    document.body.appendChild(mapDiv);
  } else {
    // Ensure it has a visible height if template forgot it
    const h = getComputedStyle(mapDiv).height;
    if (!h || h === "0px") {
      mapDiv.style.height = "100vh";
      mapDiv.style.width = "100%";
    }
  }
  return mapDiv;
}

/* ---------------------------
   Initialize Leaflet map
--------------------------- */
function initMap() {
  ensureMapContainer();

  // Leaflet global must exist (loaded by index.html)
  if (typeof window.L === "undefined") {
    alert("Leaflet (L) bulunamadı. index.html içine Leaflet CSS/JS ekli olmalı.");
    throw new Error("Leaflet not loaded");
  }

  // Default center: Istanbul-ish; you can change
  const startLatLng = [41.015, 28.979];

  UI.map = L.map("map", { zoomControl: true }).setView(startLatLng, 10);

  // OSM tile
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  }).addTo(UI.map);
}

/* ---------------------------
   Simple control panel overlay
--------------------------- */
let statusEl = null;
let bufEl = null;
let modeEl = null;

function setStatus(text) {
  if (statusEl) statusEl.textContent = text;
}

function setMode(mode) {
  UI.mode = mode;
  if (modeEl) modeEl.textContent = `Mode: ${mode}`;
}

function setBufferSize(n) {
  if (bufEl) bufEl.textContent = `Buffer: ${n}`;
}

function mountControls() {
  const panel = el("div", {
    style: {
      position: "fixed",
      top: "12px",
      left: "12px",
      zIndex: "9999",
      background: "rgba(0,0,0,0.65)",
      color: "white",
      padding: "10px 12px",
      borderRadius: "10px",
      fontFamily: "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial",
      fontSize: "12px",
      lineHeight: "1.4",
      minWidth: "220px"
    }
  });

  const btnPause = el("button", {
    style: { marginRight: "6px", cursor: "pointer" },
    onclick: () => CopEngine.pause()
  }, ["Pause"]);

  const btnResume = el("button", {
    style: { marginRight: "6px", cursor: "pointer" },
    onclick: () => CopEngine.resume(fetchCompositeSnapshot)
  }, ["Resume"]);

  const btnClear = el("button", {
    style: { cursor: "pointer" },
    onclick: async () => {
      CopEngine.clearBuffer();
      await hardReset();
    }
  }, ["Clear/Reset"]);

  modeEl = el("div", { style: { marginTop: "8px", opacity: "0.95" } }, [`Mode: ${UI.mode}`]);
  statusEl = el("div", { style: { marginTop: "4px", opacity: "0.85" } }, ["WS: connecting..."]);
  bufEl = el("div", { style: { marginTop: "4px", opacity: "0.85" } }, ["Buffer: 0"]);

  const hint = el("div", { style: { marginTop: "8px", opacity: "0.7" } }, [
    "Kısayol: P=Pause, R=Resume"
  ]);

  panel.appendChild(el("div", {}, [btnPause, btnResume, btnClear]));
  panel.appendChild(modeEl);
  panel.appendChild(statusEl);
  panel.appendChild(bufEl);
  panel.appendChild(hint);

  document.body.appendChild(panel);

  window.addEventListener("keydown", (e) => {
    if (e.key === "p" || e.key === "P") CopEngine.pause();
    if (e.key === "r" || e.key === "R") CopEngine.resume(fetchCompositeSnapshot);
  });
}

/* ---------------------------
   Normalize payload shapes (robust)
--------------------------- */
function normalizeTracksPayload(x) {
  // Accept:
  // 1) {tracks: {...}}  (dict)
  // 2) {tracks: [...]}  (list)
  // 3) {...}            (dict of id->track)
  // 4) [...]            (list of track)
  if (!x) return [];
  if (Array.isArray(x)) return x;
  if (typeof x === "object" && Array.isArray(x.tracks)) return x.tracks;
  if (typeof x === "object" && x.tracks && typeof x.tracks === "object" && !Array.isArray(x.tracks)) {
    return Object.values(x.tracks);
  }
  if (typeof x === "object" && x.tracks === undefined) {
    // maybe dict of id->track
    return Object.values(x);
  }
  return [];
}

function getTrackLatLon(track) {
  const lat = track.lat ?? track.latitude ?? track.y;
  const lon = track.lon ?? track.lng ?? track.longitude ?? track.x;
  if (typeof lat !== "number" || typeof lon !== "number") return null;
  return [lat, lon];
}

/* ---------------------------
   Render/update functions
--------------------------- */
function applySnapshot(payload) {
  // snapshot payload may include tracks/threats, or be full snapshot object
  const tracksArr = normalizeTracksPayload(payload?.tracks ?? payload);
  // Clear UI state and markers
  UI.tracks.clear();
  for (const [id, marker] of UI.trackMarkers.entries()) {
    try { marker.remove(); } catch {}
  }
  UI.trackMarkers.clear();

  // Re-add
  tracksArr.forEach(t => {
    const id = String(t.id ?? t.track_id ?? t.uid ?? "");
    if (!id) return;
    UI.tracks.set(id, t);
    upsertTrack(t);
  });
}

/* ---------------------------
   Threat-level icon factory
--------------------------- */
const THREAT_COLORS = { HIGH: "#e74c3c", MEDIUM: "#f39c12", LOW: "#27ae60" };

function makeThreatIcon(level) {
  const color = THREAT_COLORS[level] ?? "#2980b9";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="38" viewBox="0 0 28 38">
    <path d="M14 0 C6.27 0 0 6.27 0 14 C0 24.5 14 38 14 38 C14 38 28 24.5 28 14 C28 6.27 21.73 0 14 0 Z"
          fill="${color}" stroke="#fff" stroke-width="2"/>
    <circle cx="14" cy="14" r="6" fill="#fff" opacity="0.9"/>
  </svg>`;
  return L.divIcon({
    html: svg,
    className: "",
    iconSize: [28, 38],
    iconAnchor: [14, 38],
    tooltipAnchor: [0, -38],
  });
}

function buildTooltip(track, threat) {
  const id    = track.id ?? track.global_track_id ?? "?";
  const kin   = track.kinematics ?? {};
  const cls   = track.classification ?? {};
  const sens  = (track.supporting_sensors ?? []).join(", ") || "—";
  const level = threat?.threat_level ?? "—";
  const score = threat?.score ?? "—";
  const tti   = threat?.tti_s != null ? `${threat.tti_s}s` : "—";
  const vr    = kin.radial_velocity_mps != null ? `${kin.radial_velocity_mps.toFixed(1)} m/s` : "—";
  const range = kin.range_m != null ? `${Math.round(kin.range_m)} m` : "—";
  const action = threat?.recommended_action ?? "—";

  return `<div style="font:12px/1.5 monospace;min-width:160px">
    <b style="font-size:13px">${id}</b><br>
    <span style="color:${THREAT_COLORS[level] ?? '#aaa'}">&#9632; ${level}</span>
    &nbsp;score: <b>${score}</b><br>
    TTI: <b>${tti}</b> &nbsp; Vr: <b>${vr}</b><br>
    Range: <b>${range}</b><br>
    Label: ${cls.label ?? "?"} (${cls.conf != null ? (cls.conf * 100).toFixed(0) + "%" : "?"})<br>
    Sensors: ${sens}<br>
    Action: <b>${action}</b>
  </div>`;
}

function upsertTrack(track) {
  const id = String(track.id ?? track.track_id ?? track.uid ?? "");
  if (!id) return;

  UI.tracks.set(id, track);

  const latlon = getTrackLatLon(track);
  if (!latlon || !UI.map) return;

  const threat = UI.threats.get(id);
  const level  = threat?.threat_level ?? "LOW";
  const icon   = makeThreatIcon(level);

  const existing = UI.trackMarkers.get(id);
  if (!existing) {
    const m = L.marker(latlon, { icon }).addTo(UI.map);
    m.bindTooltip(buildTooltip(track, threat), { permanent: false, direction: "top", opacity: 0.95 });
    UI.trackMarkers.set(id, m);
  } else {
    existing.setLatLng(latlon);
    existing.setIcon(icon);
    existing.setTooltipContent(buildTooltip(track, threat));
  }
}

function upsertThreat(threat) {
  const id = String(threat.id ?? threat.global_track_id ?? threat.threat_id ?? "");
  if (!id) return;
  UI.threats.set(id, threat);

  // Re-render marker if track already on map
  const marker = UI.trackMarkers.get(id);
  const track  = UI.tracks.get(id);
  if (marker && track) {
    const level = threat.threat_level ?? "LOW";
    marker.setIcon(makeThreatIcon(level));
    marker.setTooltipContent(buildTooltip(track, threat));
  }
}

/* ---------------------------
   Pause/Resume Engine (UI-side)
--------------------------- */
const CopEngine = (() => {
  let mode = "LIVE"; // LIVE | PAUSED | RESUMING
  let buffer = [];

  const hooks = {
    applySnapshot,
    upsertTrack,
    upsertThreat,
    onPausedUI: () => {},
    onResumingUI: () => {},
    onLiveUI: () => {},
  };

  function setModeInternal(m) {
    mode = m;
    setMode(m);
  }

  function route(ev) {
    if (!ev || !ev.event_type) return;
    if (ev.event_type === "cop.snapshot") return hooks.applySnapshot(ev.payload);
    if (ev.event_type === "cop.track") return hooks.upsertTrack(ev.payload);
    if (ev.event_type === "cop.threat") return hooks.upsertThreat(ev.payload);
  }

  function onEvent(ev) {
    if (mode === "LIVE") {
      route(ev);
      return;
    }
    buffer.push(ev);
    if (buffer.length > UI.bufferMax) buffer.shift();
    setBufferSize(buffer.length);
  }

  function pause() {
    if (mode !== "LIVE") return;
    setModeInternal("PAUSED");
    hooks.onPausedUI({ bufferSize: buffer.length });
    setStatus("WS: connected (paused buffering)");
  }

  async function resume(fetchSnapshotFn) {
    if (mode !== "PAUSED") return;
    setModeInternal("RESUMING");
    hooks.onResumingUI({ bufferSize: buffer.length });
    setStatus("WS: connected (resuming...)");

    // Deterministic: fetch snapshot from REST
    try {
      if (typeof fetchSnapshotFn === "function") {
        const snap = await fetchSnapshotFn();
        if (snap) hooks.applySnapshot(snap);
      }
    } catch (e) {
      console.warn("Resume snapshot fetch failed; continuing with buffer only.", e);
    }

    // Drain buffer (fast, sync)
    buffer.forEach(route);
    buffer = [];
    setBufferSize(0);

    setModeInternal("LIVE");
    hooks.onLiveUI();
    setStatus("WS: connected (live)");
  }

  function clearBuffer() {
    buffer = [];
    setBufferSize(0);
  }

  return { onEvent, pause, resume, clearBuffer };
})();

// expose for onclick usage (if needed)
window.CopEngine = CopEngine;

/* ---------------------------
   REST helpers
--------------------------- */
async function fetchCompositeSnapshot() {
  // Minimum deterministic snapshot: tracks (+ optional threats)
  const [tracks, threats] = await Promise.all([
    fetch("/api/tracks").then(r => r.json()),
    fetch("/api/threats").then(r => r.json()).catch(() => null),
  ]);

  // applySnapshot expects something with tracks array/dict. We pass {tracks: ...}
  // Keep raw shapes; normalize will handle.
  return { tracks: tracks?.tracks ?? tracks, threats: threats?.threats ?? threats };
}

async function hardReset() {
  await fetch("/api/reset", { method: "POST" });
  // After reset, either WS sends snapshot or we pull one:
  try {
    const snap = await fetchCompositeSnapshot();
    applySnapshot(snap);
  } catch {}
}

/* ---------------------------
   WebSocket connection
--------------------------- */
function wsUrl() {
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

function connectWS() {
  const url = wsUrl();
  setStatus(`WS: connecting ${url}`);

  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    UI.wsConnected = false;
    setStatus(`WS: failed (${String(e)})`);
    setTimeout(connectWS, 1200);
    return;
  }

  UI.ws = ws;

  ws.onopen = () => {
    UI.wsConnected = true;
    setStatus("WS: connected (live)");
  };

  ws.onclose = () => {
    UI.wsConnected = false;
    setStatus("WS: closed (reconnecting...)");
    setTimeout(connectWS, 1200);
  };

  ws.onerror = () => {
    // onclose will handle reconnect
  };

  ws.onmessage = (msg) => {
    const ev = safeJsonParse(msg.data);
    if (!ev) return;
    CopEngine.onEvent(ev);
  };
}

/* ---------------------------
   Agent health panel
--------------------------- */
const ORCH_URL = "";
let agentPanelEl = null;

function mountAgentPanel() {
  const panel = el("div", {
    id: "agent-panel",
    style: {
      position: "fixed",
      bottom: "12px",
      left: "12px",
      zIndex: "9999",
      background: "rgba(0,0,0,0.65)",
      color: "white",
      padding: "8px 12px",
      borderRadius: "10px",
      fontFamily: "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial",
      fontSize: "11px",
      lineHeight: "1.5",
      minWidth: "180px",
    }
  });
  panel.innerHTML = "<b>Agents</b><br><span style='opacity:.6'>connecting...</span>";
  document.body.appendChild(panel);
  agentPanelEl = panel;
}

async function refreshAgentHealth() {
  if (!agentPanelEl) return;
  try {
    const r = await fetch(`${ORCH_URL}/api/orchestrator/health`);
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    const agents = data.agents ?? [];
    let html = `<b>Agents</b> <span style='opacity:.6'>${data.alive}/${data.total} alive</span><br>`;
    agents.forEach(a => {
      const color = a.status === "ALIVE" ? "#27ae60" : "#e74c3c";
      const dot   = `<span style='color:${color}'>&#9679;</span>`;
      const met   = a.metrics?.events_sent != null ? ` (${a.metrics.events_sent} ev)` : "";
      html += `${dot} ${a.name}${met}<br>`;
    });
    agentPanelEl.innerHTML = html;
  } catch {
    agentPanelEl.innerHTML = "<b>Agents</b><br><span style='opacity:.5'>orchestrator offline</span>";
  }
}

/* ---------------------------
   Boot
--------------------------- */
function boot() {
  initMap();
  mountControls();
  mountAgentPanel();
  connectWS();
  // Poll orchestrator health every 5 seconds
  refreshAgentHealth();
  setInterval(refreshAgentHealth, 5000);
}

document.addEventListener("DOMContentLoaded", boot);
