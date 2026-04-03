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
function setStatus(t)     { if(statusEl) statusEl.textContent=t; }
function setMode(m)       { UI.mode=m; if(modeEl) modeEl.textContent=`Mode: ${m}`; }
function setBufferSize(n) { if(bufEl) bufEl.textContent=`Buffer: ${n}`; }

/* ── Control panel ─────────────────────────────────────── */
function mountControls() {
  const panel = el("div", { style: {
    position:"fixed", top:"12px", left:"12px", zIndex:"9999",
    background:"rgba(0,0,0,0.65)", color:"white",
    padding:"10px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    lineHeight:"1.4", minWidth:"220px"
  }});
  modeEl   = el("div", {style:{marginTop:"8px"}}, [`Mode: ${UI.mode}`]);
  statusEl = el("div", {style:{marginTop:"4px",opacity:"0.85"}}, ["WS: connecting..."]);
  bufEl    = el("div", {style:{marginTop:"4px",opacity:"0.85"}}, ["Buffer: 0"]);
  panel.appendChild(el("div",{}, [
    el("button",{style:{marginRight:"6px",cursor:"pointer"},onclick:()=>CopEngine.pause()},["Pause"]),
    el("button",{style:{marginRight:"6px",cursor:"pointer"},onclick:()=>CopEngine.resume(fetchCompositeSnapshot)},["Resume"]),
    el("button",{style:{cursor:"pointer"},onclick:async()=>{CopEngine.clearBuffer();await hardReset();}},["Clear/Reset"]),
  ]));
  panel.appendChild(modeEl);
  panel.appendChild(statusEl);
  panel.appendChild(bufEl);
  panel.appendChild(el("div",{style:{marginTop:"8px",opacity:"0.7"}},["P=Pause  R=Resume"]));
  document.body.appendChild(panel);
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
  const mlP   = threat?.ml_probability != null ? `${(threat.ml_probability*100).toFixed(0)}%` : "-";
  return `<div style="font:12px/1.5 monospace;min-width:175px">
    <b style="font-size:13px">${id}</b><br>
    <span style="color:${THREAT_COLORS[level]??'#aaa'}"> ${level}</span>
    \u00a0score:<b>${score}</b> p=<b>${mlP}</b><br>
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
    UI.trackMarkers.set(id,m);
  } else { ex.setLatLng(ll); ex.setIcon(icon); ex.setTooltipContent(tip); }
  upsertTrail(track, threat);
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
}

/* ── Zone breach alert panel ─────────────────────────────── */
const MAX_ALERTS = 20;
let alertPanelEl = null;
const alertsLog  = [];

function mountAlertPanel() {
  alertPanelEl = el("div", { id:"alert-panel", style: {
    position:"fixed", bottom:"12px", right:"12px", zIndex:"9999",
    background:"rgba(0,0,0,0.72)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"11px",
    lineHeight:"1.5", minWidth:"200px", maxWidth:"280px",
    maxHeight:"160px", overflowY:"auto",
  }});
  alertPanelEl.innerHTML = "<b>Zone Alerts</b><br><span style='opacity:.5'>No breaches yet</span>";
  document.body.appendChild(alertPanelEl);
}

function pushAlert(p) {
  const t = p.server_time ? new Date(p.server_time).toLocaleTimeString() : new Date().toLocaleTimeString();
  alertsLog.unshift({t, track_id:p.track_id, zone_name:p.zone_name, zone_type:p.zone_type});
  if(alertsLog.length>MAX_ALERTS) alertsLog.pop();
  const zc={kill:"#e74c3c",restricted:"#f39c12",friendly:"#27ae60"};
  let html="<b>Zone Alerts</b><br>";
  alertsLog.forEach(a => {
    const ac = zc[a.zone_type] || '#e74c3c';
    html += '<span style="color:' + ac + '">\u25A0</span> '
          + '<b>' + a.track_id + '</b> - ' + a.zone_name
          + ' <span style="opacity:.6">' + a.t + '</span><br>';
  });
  alertPanelEl.innerHTML=html;
  const color=zc[p.zone_type]??"#e74c3c";
  alertPanelEl.style.outline=`2px solid ${color}`;
  setTimeout(()=>{alertPanelEl.style.outline="none";},800);
  const marker=UI.trackMarkers.get(p.track_id);
  if(marker){
    const e=marker.getElement();
    if(e){e.style.filter="drop-shadow(0 0 8px red)";setTimeout(()=>{e.style.filter="";},1000);}
  }
}

/* ── Phase 3: Task / action-queue panel ─────────────────── */
let taskPanelEl = null;
const pendingTasks = new Map();  // task_id -> task

function mountTaskPanel() {
  taskPanelEl = el("div", { id:"task-panel", style: {
    position:"fixed", top:"12px", left:"260px", zIndex:"9999",
    background:"rgba(0,0,0,0.75)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"11px",
    lineHeight:"1.6", minWidth:"240px", maxWidth:"320px",
    maxHeight:"320px", overflowY:"auto",
  }});
  taskPanelEl.innerHTML = "<b>Task Queue</b><br><span style='opacity:.5'>No tasks yet</span>";
  document.body.appendChild(taskPanelEl);
}

const ACTION_COLORS = { ENGAGE:"#e74c3c", OBSERVE:"#f39c12", EVADE:"#3498db" };

function _renderTaskPanel() {
  if(!taskPanelEl) return;
  const tasks=[...pendingTasks.values()];
  if(tasks.length===0){
    taskPanelEl.innerHTML="<b>Task Queue</b><br><span style='opacity:.5'>No pending tasks</span>";
    return;
  }
  let html="<b>Task Queue</b> <span style='opacity:.6'>"+tasks.length+" pending</span><br><br>";
  tasks.slice(0,8).forEach(t => {
    const c=ACTION_COLORS[t.action]??"#aaa";
    const tti=t.tti_s!=null?` TTI:${t.tti_s}s`:"";
    const intent=INTENT_META[t.intent]?.label??t.intent??"?";
    html+=`<div style="border-left:3px solid ${c};padding-left:6px;margin-bottom:6px">
      <span style="color:${c};font-weight:bold">${t.action}</span>
      &nbsp;<b>${t.track_id}</b><br>
      <span style="opacity:.7">${t.threat_level} | ${intent}${tti} | score:${t.score}</span><br>
      <button data-id="${t.id}" data-act="approve"
        style="background:#27ae60;color:#fff;border:none;border-radius:3px;padding:1px 6px;cursor:pointer;margin-right:4px;font-size:10px">
        Approve
      </button>
      <button data-id="${t.id}" data-act="reject"
        style="background:#e74c3c;color:#fff;border:none;border-radius:3px;padding:1px 6px;cursor:pointer;font-size:10px">
        Reject
      </button>
    </div>`;
  });
  taskPanelEl.innerHTML=html;

  // Attach button handlers
  taskPanelEl.querySelectorAll("button[data-id]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const tid=btn.dataset.id, act=btn.dataset.act;
      await fetch(`/api/tasks/${tid}/${act}`,{method:"POST",
        headers:{"Content-Type":"application/json"},body:JSON.stringify({operator:"operator"})});
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
  assetPanelEl = el("div", { id:"asset-panel", style:{
    position:"fixed", top:"200px", right:"12px", zIndex:"9999",
    background:"rgba(0,0,0,0.68)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    lineHeight:"1.5", minWidth:"170px",
  }});

  const mkBtn=(label,type,color)=>el("button",{
    style:{display:"block",width:"100%",marginBottom:"4px",cursor:"pointer",
           background:color,color:"#fff",border:"none",borderRadius:"4px",padding:"3px 6px"},
    onclick:()=>startAssetPlace(type)
  },[label]);

  assetPanelEl.appendChild(el("b",{},["Assets"]));
  assetPanelEl.appendChild(el("br"));
  assetPanelEl.appendChild(mkBtn("+ Place Friendly","friendly","#2980b9"));
  assetPanelEl.appendChild(mkBtn("+ Place Hostile","hostile","#e74c3c"));
  assetPanelEl.appendChild(mkBtn("+ Place Unknown","unknown","#7f8c8d"));
  const hint=el("div",{style:{marginTop:"4px",opacity:"0.65",fontSize:"10px"}},["Click map to place"]);
  assetPanelEl.appendChild(hint);
  document.body.appendChild(assetPanelEl);

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
  missionPanelEl = el("div",{id:"mission-panel",style:{
    position:"fixed", top:"360px", right:"12px", zIndex:"9999",
    background:"rgba(0,0,0,0.68)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    lineHeight:"1.5", minWidth:"170px",
  }});

  const hint=el("div",{style:{marginTop:"4px",opacity:"0.65",fontSize:"10px"}},["Click map to add waypoints"]);
  const btnStart=el("button",{style:{marginRight:"4px",cursor:"pointer"},onclick:startMission},["Plan Mission"]);
  const btnDone =el("button",{style:{marginRight:"4px",cursor:"pointer",display:"none"},onclick:stopMission},["Done"]);
  const btnClear=el("button",{style:{cursor:"pointer"},onclick:async()=>{
    await fetch("/api/waypoints",{method:"DELETE"}); clearWaypoints(); missionOrder=0;
  }},["Clear"]);

  missionPanelEl.appendChild(el("b",{},["Mission"]));
  missionPanelEl.appendChild(el("br"));
  missionPanelEl.appendChild(el("div",{},[btnStart,btnDone,btnClear]));
  missionPanelEl.appendChild(hint);
  document.body.appendChild(missionPanelEl);

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
      case "cop.track":           return upsertTrack(ev.payload);
      case "cop.threat":          return upsertThreat(ev.payload);
      case "cop.zone":            return upsertZone(ev.payload);
      case "cop.zone_removed":    return removeZone(ev.payload?.id);
      case "cop.alert":           return pushAlert(ev.payload);
      case "cop.asset":           return upsertAsset(ev.payload);
      case "cop.asset_removed":   return removeAsset(ev.payload?.id);
      case "cop.task":            return pushTask(ev.payload);
      case "cop.task_update":     return updateTask(ev.payload);
      case "cop.waypoint":        return upsertWaypoint(ev.payload);
      case "cop.waypoint_removed":return removeWaypoint(ev.payload?.id);
      case "cop.waypoints_cleared":return clearWaypoints();
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
  const url=`${proto}://${location.host}/ws`;
  setStatus(`WS: connecting ${url}`);
  let ws;
  try{ws=new WebSocket(url);}
  catch(e){setStatus(`WS: failed (${e})`);setTimeout(connectWS,1200);return;}
  UI.ws=ws;
  ws.onopen =()=>{UI.wsConnected=true; setStatus("WS: connected (live)");};
  ws.onclose=()=>{UI.wsConnected=false;setStatus("WS: closed (reconnecting...)");setTimeout(connectWS,1200);};
  ws.onerror=()=>{};
  ws.onmessage=msg=>{const ev=safeJsonParse(msg.data);if(ev)CopEngine.onEvent(ev);};
}

/* ── Zone draw panel ─────────────────────────────────────── */
let zoneDrawPoints=[],zoneDrawMarkers=[],zoneDrawing=false;
function mountZonePanel(){
  const panel=el("div",{style:{
    position:"fixed",top:"12px",right:"12px",zIndex:"9999",
    background:"rgba(0,0,0,0.65)",color:"white",
    padding:"8px 12px",borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial",fontSize:"12px",
    lineHeight:"1.5",minWidth:"180px",
  }});
  const typeSelect=el("select",{style:{marginBottom:"4px",width:"100%",borderRadius:"4px",padding:"2px"}});
  ["restricted","kill","friendly"].forEach(t=>typeSelect.appendChild(el("option",{value:t},[t])));
  const nameInput=el("input",{type:"text",placeholder:"Zone name",
    style:{width:"100%",marginBottom:"4px",borderRadius:"4px",padding:"2px",boxSizing:"border-box"}});
  const btnDraw  =el("button",{style:{marginRight:"4px",cursor:"pointer"}},["Draw Zone"]);
  const btnSave  =el("button",{style:{marginRight:"4px",cursor:"pointer",display:"none"}},["Save"]);
  const btnCancel=el("button",{style:{cursor:"pointer",display:"none"}},["Cancel"]);
  const hint     =el("div",{style:{marginTop:"4px",opacity:"0.7",fontSize:"11px"}},[""]);
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
  panel.appendChild(el("b",{},["Zones"]));panel.appendChild(el("br"));
  panel.appendChild(nameInput);panel.appendChild(typeSelect);
  panel.appendChild(el("div",{},[btnDraw,btnSave,btnCancel]));panel.appendChild(hint);
  document.body.appendChild(panel);
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
    position:"fixed", bottom:"390px", right:"12px", zIndex:"9999",
    background:"rgba(40,0,0,0.82)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", minWidth:"230px", maxWidth:"300px",
    maxHeight:"180px", overflowY:"auto",
    border:"1px solid rgba(231,76,60,0.4)",
  }});
  breachPanelEl.innerHTML = "<b>\u26A0 Predictive Breach</b><br><span style='opacity:.5'>No predicted breaches</span>";
  document.body.appendChild(breachPanelEl);
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

/* ── AI: Coordinated Attack panel + convergence lines ──── */
let coordPanelEl = null;
const convergenceMarkers = new Map(); // key -> L.circleMarker
const convergenceLines   = new Map(); // key -> [L.polyline, ...]

function mountCoordPanel() {
  coordPanelEl = el("div", { id:"coord-panel", style:{
    position:"fixed", bottom:"580px", right:"12px", zIndex:"9999",
    background:"rgba(80,0,40,0.85)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", minWidth:"240px", maxWidth:"320px",
    maxHeight:"180px", overflowY:"auto",
    border:"1px solid rgba(255,0,80,0.4)",
  }});
  coordPanelEl.innerHTML = "<b>\u2694 Coordinated Attack</b><br><span style='opacity:.5'>No coordinated threats</span>";
  document.body.appendChild(coordPanelEl);
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
  anomalyPanelEl = el("div", { id:"anomaly-panel", style:{
    position:"fixed", bottom:"180px", right:"12px", zIndex:"9999",
    background:"rgba(0,0,0,0.75)", color:"white",
    padding:"8px 12px", borderRadius:"10px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"10px",
    lineHeight:"1.4", minWidth:"220px", maxWidth:"290px",
    maxHeight:"200px", overflowY:"auto",
  }});
  anomalyPanelEl.innerHTML = "<b>AI Anomalies</b><br><span style='opacity:.5'>No anomalies</span>";
  document.body.appendChild(anomalyPanelEl);
}

function renderAnomalyPanel() {
  if(!anomalyPanelEl) return;
  if(anomalyLog.length === 0) {
    anomalyPanelEl.innerHTML = "<b>AI Anomalies</b><br><span style='opacity:.5'>No anomalies</span>";
    return;
  }
  let html = `<b>AI Anomalies</b> <span style="opacity:.6">${anomalyLog.length}</span><br>`;
  anomalyLog.slice(0, 15).forEach(a => {
    const c = ANOMALY_COLORS[a.severity] || "#aaa";
    const tid = a.track_id || (a.track_ids||[]).join(",") || "?";
    html += `<div style="border-left:2px solid ${c};padding-left:4px;margin:2px 0">
      <span style="color:${c};font-weight:bold">${a.type}</span>
      <span style="opacity:.7"> ${tid}</span><br>
      <span style="opacity:.6;font-size:9px">${a.detail||a.message||""}</span>
    </div>`;
  });
  anomalyPanelEl.innerHTML = html;
}

function pushAnomalies(anomalies) {
  if(!anomalies || !anomalies.length) return;
  for(const a of anomalies) {
    anomalyLog.unshift(a);
  }
  while(anomalyLog.length > MAX_ANOMALY_LOG) anomalyLog.pop();
  renderAnomalyPanel();
  // Flash
  if(anomalyPanelEl) {
    const sev = anomalies[0]?.severity;
    const c = ANOMALY_COLORS[sev] || "#e74c3c";
    anomalyPanelEl.style.outline = `2px solid ${c}`;
    setTimeout(() => { anomalyPanelEl.style.outline = "none"; }, 800);
  }
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
let chatPanelEl = null;
const chatHistory = [];

function mountChatPanel() {
  chatPanelEl = el("div", { id:"chat-panel", style:{
    position:"fixed", top:"12px", left:"50%", transform:"translateX(-50%)",
    zIndex:"9998", background:"rgba(0,0,0,0.85)", color:"white",
    padding:"10px 14px", borderRadius:"12px",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    lineHeight:"1.5", width:"420px", maxHeight:"400px",
    display:"none", flexDirection:"column",
  }});

  const header = el("div", {style:{display:"flex",justifyContent:"space-between",marginBottom:"6px"}}, [
    el("b", {}, ["AI Advisor"]),
    el("button", {style:{background:"none",border:"none",color:"#aaa",cursor:"pointer",fontSize:"14px"},
      onclick:()=>{ chatPanelEl.style.display="none"; }}, ["\u2715"]),
  ]);

  const msgArea = el("div", {id:"chat-messages", style:{
    flex:"1", overflowY:"auto", maxHeight:"280px", marginBottom:"8px",
    padding:"4px", fontSize:"11px", lineHeight:"1.5",
  }});
  msgArea.innerHTML = "<span style='opacity:.5'>AI danismana soru sorun...</span>";

  const inputRow = el("div", {style:{display:"flex",gap:"6px"}});
  const input = el("input", {type:"text", placeholder:"Soru sorun veya komut verin...",
    style:{flex:"1",padding:"5px 8px",borderRadius:"6px",border:"1px solid #555",
           background:"#222",color:"#fff",fontSize:"12px"}});
  const sendBtn = el("button", {style:{background:"#2980b9",color:"#fff",border:"none",
    borderRadius:"6px",padding:"5px 12px",cursor:"pointer",fontSize:"11px"},
    onclick:()=>sendChat(input, msgArea)}, ["Gonder"]);

  input.addEventListener("keydown", e => {
    if(e.key === "Enter") sendChat(input, msgArea);
  });

  // Briefing button
  const briefBtn = el("button", {style:{background:"#8e44ad",color:"#fff",border:"none",
    borderRadius:"6px",padding:"5px 10px",cursor:"pointer",fontSize:"10px",marginRight:"4px"},
    onclick:()=>getBriefing(msgArea)}, ["Brifing"]);

  inputRow.appendChild(input);
  inputRow.appendChild(briefBtn);
  inputRow.appendChild(sendBtn);
  chatPanelEl.appendChild(header);
  chatPanelEl.appendChild(msgArea);
  chatPanelEl.appendChild(inputRow);
  document.body.appendChild(chatPanelEl);
}

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

async function getBriefing(msgArea) {
  chatHistory.push({role:"user", text:"[Durum Brifing Istegi]"});
  renderChatMessages(msgArea);
  try {
    const resp = await fetch("/api/ai/briefing");
    const data = await resp.json();
    const badge = data.llm_used ? " [LLM]" : " [LOCAL]";
    chatHistory.push({role:"ai", text: (data.briefing || "Brifing alinamadi.") + badge});
  } catch(e) {
    chatHistory.push({role:"ai", text:"Hata: " + e.message});
  }
  renderChatMessages(msgArea);
}

function renderChatMessages(area) {
  let html = "";
  chatHistory.slice(-20).forEach(m => {
    if(m.role === "user") {
      html += `<div style="text-align:right;margin:3px 0">
        <span style="background:#2c3e50;padding:3px 8px;border-radius:8px;display:inline-block;max-width:85%">${escHtml(m.text)}</span>
      </div>`;
    } else {
      html += `<div style="text-align:left;margin:3px 0">
        <span style="background:#1a252f;padding:3px 8px;border-radius:8px;display:inline-block;max-width:85%;white-space:pre-wrap">${escHtml(m.text)}</span>
      </div>`;
    }
  });
  area.innerHTML = html;
  area.scrollTop = area.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

/* ── AI: toggle chat button (floating) ─────────────────── */
function mountChatToggle() {
  const btn = el("div", {style:{
    position:"fixed", top:"60px", left:"50%", transform:"translateX(-50%)",
    zIndex:"9999", background:"#8e44ad", color:"#fff",
    padding:"5px 14px", borderRadius:"20px", cursor:"pointer",
    fontFamily:"ui-sans-serif,system-ui,Arial", fontSize:"12px",
    boxShadow:"0 2px 8px rgba(0,0,0,0.4)",
  }, onclick:()=>{
    if(!chatPanelEl) return;
    chatPanelEl.style.display = chatPanelEl.style.display === "none" ? "flex" : "none";
  }}, ["AI Advisor"]);
  document.body.appendChild(btn);
}

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

/* ── AI: periodic refresh ──────────────────────────────────── */
async function refreshAI() {
  try {
    const [predResp, anomResp, recResp, breachResp, coneResp, coordResp] = await Promise.all([
      fetch("/api/ai/predictions").then(r=>r.json()).catch(()=>({})),
      fetch("/api/ai/anomalies").then(r=>r.json()).catch(()=>({anomalies:[]})),
      fetch("/api/ai/recommendations").then(r=>r.json()).catch(()=>({recommendations:[]})),
      fetch("/api/ai/pred_breaches").then(r=>r.json()).catch(()=>({breaches:[]})),
      fetch("/api/ai/uncertainty").then(r=>r.json()).catch(()=>({cones:{}})),
      fetch("/api/ai/coordinated").then(r=>r.json()).catch(()=>({attacks:[]})),
    ]);
    // Draw predictions
    drawPredictions(predResp.predictions || {});
    // Draw uncertainty cones
    drawUncertaintyCones(coneResp.cones || {});
    // Predictive breach warnings
    renderBreachPanel(breachResp.breaches || []);
    // Coordinated attack warnings
    renderCoordPanel(coordResp.attacks || []);
    // Update anomaly panel (only new ones)
    const newAnomalies = (anomResp.anomalies || []).filter(a => {
      return !anomalyLog.some(e => e.time === a.time && e.type === a.type &&
        (e.track_id||"") === (a.track_id||""));
    });
    if(newAnomalies.length > 0) pushAnomalies(newAnomalies);
    // Tactical recommendations
    renderTacticalPanel(recResp.recommendations || []);
    // Auto-refresh open timeline chart
    if(timelineCurrentTrack) fetchAndDrawTimeline(timelineCurrentTrack);
  } catch(e) { /* silent */ }
}

/* ── Boot ────────────────────────────────────────────────── */

function boot(){
  initMap();
  mountControls();
  mountZonePanel();
  mountAgentPanel();
  mountAlertPanel();
  mountTaskPanel();
  mountAssetPanel();
  mountMissionPanel();
  // Phase 5: AI panels
  mountCoordPanel();
  mountBreachPanel();
  mountAnomalyPanel();
  mountTacticalPanel();
  mountTimelinePopup();
  mountChatPanel();
  mountChatToggle();
  connectWS();
  refreshAgentHealth();
  setInterval(refreshAgentHealth, 5000);
  // AI refresh every 3s
  setInterval(refreshAI, 3000);
}
document.addEventListener("DOMContentLoaded", () => {
  try { boot(); }
  catch(e) {
    document.body.style.cssText = "background:#111;color:#f55;padding:20px;font:14px monospace";
    document.body.innerHTML = "<h2>NIZAM Boot Error</h2><pre>" + e.stack + "</pre>";
  }
});
