/*
  cop/static/app.js (Leaflet PRO)
  NIZAM COP Phase 4 – C (Professionalize)

  Works with index.html that contains:
    - <div id="map"></div>
    - #wsStatus, #clearBtn, #threats, #tracks, #agents, #tail

  Features:
    - Robust WS connect (/ws) with fallback+timeout+retry
    - Live/Pause + timeline scrubber (controls injected into UI)
    - Trails (polylines)
    - Layer toggles (Tracks / Threats / Trails)
    - Threat filters (type, min score, only active, text)
    - Server reset (/api/reset) + local state clear
*/

(() => {
  "use strict";

  // -----------------------------
  // Helpers
  // -----------------------------
  const $ = (sel) => document.querySelector(sel);

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const nowMs = () => Date.now();

  function safeJsonParse(s) {
    try { return JSON.parse(s); } catch { return null; }
  }

  function fmtTime(ms) {
    if (!Number.isFinite(ms)) return "--:--:--.---";
    const d = new Date(ms);
    const pad = (n, w=2) => String(n).padStart(w, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  function debounce(fn, wait = 150) {
    let t = null;
    return (...args) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  }

  // -----------------------------
  // DOM bindings (from your index.html)
  // -----------------------------
  const wsStatusEl = $("#wsStatus");
  const clearBtn   = $("#clearBtn");

  const mapEl      = $("#map");
  const threatsEl  = $("#threats");
  const tracksEl   = $("#tracks");
  const agentsEl   = $("#agents");
  const tailEl     = $("#tail");

  // Top card container to inject controls (first .card)
  const topCard = document.querySelector(".side .card");

  // -----------------------------
  // URLs
  // -----------------------------
  const DEFAULT_WS_PATH = "/ws";
  const WS_CANDIDATES = [
    window.COP_WS_URL,
    (() => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      return `${proto}://${location.host}${DEFAULT_WS_PATH}`;
    })(),
    "ws://127.0.0.1:5000/ws",
    "ws://localhost:5000/ws",
  ].filter(Boolean);

  let wsCandidateIdx = 0;

  const API_RESET = "/api/reset";

  // -----------------------------
  // State
  // -----------------------------
  const state = {
    ws: null,
    wsConnected: false,

    live: true,
    viewTimeMs: nowMs(),

    // history for timeline rendering
    // id -> [{ts, ...data}] sorted append
    trackHist: new Map(),
    threatHist: new Map(),

    // agents
    agents: new Map(), // name/id -> obj

    // timeline bounds
    minTs: null,
    maxTs: null,

    // UI options
    showTracks: true,
    showThreats: true,
    showTrails: true,

    trailSeconds: 20,

    filter: {
      type: "ALL",
      minScore: 0,
      onlyActive: false,
      text: "",
    },

    // event tail (last N)
    tail: [],
    tailMax: 120,
  };

  function setWsStatus(text) {
    if (wsStatusEl) wsStatusEl.textContent = text;
  }

  function pushTail(line) {
    state.tail.push(line);
    if (state.tail.length > state.tailMax) state.tail.splice(0, state.tail.length - state.tailMax);
    if (tailEl) tailEl.textContent = state.tail.join("\n");
  }

  function updateBounds(ts) {
    if (!Number.isFinite(ts)) return;
    if (state.minTs === null || ts < state.minTs) state.minTs = ts;
    if (state.maxTs === null || ts > state.maxTs) state.maxTs = ts;
  }

  function getTs(obj) {
    return Number(obj?.ts ?? obj?.timestamp ?? obj?.t ?? nowMs());
  }

  // -----------------------------
  // Leaflet map setup
  // -----------------------------
  let map = null;
  let baseLayer = null;

  const layers = {
    tracks: null,
    threats: null,
    trails: null,
  };

  const markers = {
    tracks: new Map(),   // id -> L.Marker
    threats: new Map(),  // id -> L.Marker
  };

  const polylines = {
    tracks: new Map(),   // id -> L.Polyline
    threats: new Map(),  // id -> L.Polyline
  };

  function ensureLeaflet() {
    return typeof window.L !== "undefined";
  }

  function initMap() {
    if (!mapEl) throw new Error("Map div (#map) not found.");
    if (!ensureLeaflet()) throw new Error("Leaflet (L) not loaded. Ensure leaflet.js included.");

    map = window.L.map(mapEl, { zoomControl: true }).setView([39.0, 35.0], 6);

    baseLayer = window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap",
    }).addTo(map);

    layers.tracks  = window.L.layerGroup().addTo(map);
    layers.threats = window.L.layerGroup().addTo(map);
    layers.trails  = window.L.layerGroup().addTo(map);
  }

  function setLayerVisibility(layerGroup, visible) {
    if (!map || !layerGroup) return;
    const has = map.hasLayer(layerGroup);
    if (visible && !has) layerGroup.addTo(map);
    if (!visible && has) map.removeLayer(layerGroup);
  }

  // -----------------------------
  // UI: inject Phase-C controls (no HTML edits needed)
  // -----------------------------
  const ui = {
    liveBtn: null,
    pauseBtn: null,
    slider: null,
    timeLabel: null,

    trailsToggle: null,
    trailLen: null,

    layerTracks: null,
    layerThreats: null,
    layerTrails: null,

    filterType: null,
    filterMinScore: null,
    filterOnlyActive: null,
    filterText: null,
  };

  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v;
      else if (k === "style") e.setAttribute("style", v);
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.substring(2), v);
      else e.setAttribute(k, String(v));
    }
    for (const c of children) {
      if (typeof c === "string") e.appendChild(document.createTextNode(c));
      else if (c) e.appendChild(c);
    }
    return e;
  }

  function injectControls() {
    if (!topCard) return;

    // Insert a compact control block below WS status row
    const controls = el("div", { style: "margin-top:10px; display:grid; gap:10px;" }, []);

    // Live/Pause row
    ui.liveBtn = el("button", { class: "btn", type: "button" }, ["Live"]);
    ui.pauseBtn = el("button", { class: "btn", type: "button" }, ["Pause"]);
    ui.timeLabel = el("div", { class: "small", style: "text-align:right;" }, [`${fmtTime(state.viewTimeMs)}`]);

    const row1 = el("div", { class: "row", style: "justify-content: space-between;" }, [
      el("div", { class: "row" }, [ui.liveBtn, ui.pauseBtn]),
      ui.timeLabel,
    ]);

    // Timeline slider
    ui.slider = el("input", { type: "range", min: "0", max: "0", value: "0", style: "width:100%;" }, []);

    const row2 = el("div", {}, [ui.slider]);

    // Trails + length
    ui.trailsToggle = el("input", { type: "checkbox", checked: "checked" }, []);
    ui.trailLen = el("input", { type: "number", min: "1", max: "600", value: String(state.trailSeconds), style: "width:90px;" }, []);

    const trailsRow = el("div", { class: "row" }, [
      el("label", { class: "small" }, ["Trails ", ui.trailsToggle]),
      el("span", { class: "small" }, ["Len(s):"]),
      ui.trailLen,
    ]);

    // Layer toggles
    ui.layerTracks  = el("input", { type: "checkbox", checked: "checked" }, []);
    ui.layerThreats = el("input", { type: "checkbox", checked: "checked" }, []);
    ui.layerTrails  = el("input", { type: "checkbox", checked: "checked" }, []);

    const layerRow = el("div", { class: "row" }, [
      el("label", { class: "small" }, ["Tracks ", ui.layerTracks]),
      el("label", { class: "small" }, ["Threats ", ui.layerThreats]),
      el("label", { class: "small" }, ["Trails ", ui.layerTrails]),
    ]);

    // Threat filters
    ui.filterType = el("select", { class: "btn", style: "padding:4px 8px;" }, [
      el("option", { value: "ALL" }, ["Type: ALL"]),
      el("option", { value: "RADAR" }, ["RADAR"]),
      el("option", { value: "RF" }, ["RF"]),
      el("option", { value: "FUSED" }, ["FUSED"]),
      el("option", { value: "UNKNOWN" }, ["UNKNOWN"]),
    ]);

    ui.filterMinScore = el("input", { type: "number", value: "0", min: "0", step: "1", style: "width:90px;" }, []);
    ui.filterOnlyActive = el("input", { type: "checkbox" }, []);
    ui.filterText = el("input", { type: "text", placeholder: "search…", style: "flex:1; min-width:120px;" }, []);

    const filterRow1 = el("div", { class: "row" }, [
      ui.filterType,
      el("span", { class: "small" }, ["MinScore"]),
      ui.filterMinScore,
      el("label", { class: "small" }, ["OnlyActive ", ui.filterOnlyActive]),
    ]);

    const filterRow2 = el("div", { class: "row" }, [
      el("span", { class: "small" }, ["Filter"]),
      ui.filterText,
    ]);

    controls.appendChild(row1);
    controls.appendChild(row2);
    controls.appendChild(trailsRow);
    controls.appendChild(layerRow);
    controls.appendChild(filterRow1);
    controls.appendChild(filterRow2);

    // Place controls at end of top card
    topCard.appendChild(controls);

    // Wire handlers
    ui.liveBtn.addEventListener("click", () => goLive());
    ui.pauseBtn.addEventListener("click", () => pause());

    ui.slider.addEventListener("input", () => {
      const v = Number(ui.slider.value);
      if (!Number.isFinite(v)) return;
      if (state.live) pause();
      setViewTime(v);
      renderAt(state.viewTimeMs);
    });

    ui.trailsToggle.addEventListener("change", () => {
      state.showTrails = !!ui.trailsToggle.checked;
      setLayerVisibility(layers.trails, state.showTrails && !!ui.layerTrails.checked);
      renderAt(state.viewTimeMs);
    });

    ui.trailLen.addEventListener("input", debounce(() => {
      const v = Number(ui.trailLen.value);
      if (Number.isFinite(v) && v > 0) state.trailSeconds = clamp(v, 1, 600);
      renderAt(state.viewTimeMs);
    }, 120));

    ui.layerTracks.addEventListener("change", () => {
      state.showTracks = !!ui.layerTracks.checked;
      setLayerVisibility(layers.tracks, state.showTracks);
      renderAt(state.viewTimeMs);
    });

    ui.layerThreats.addEventListener("change", () => {
      state.showThreats = !!ui.layerThreats.checked;
      setLayerVisibility(layers.threats, state.showThreats);
      renderAt(state.viewTimeMs);
    });

    ui.layerTrails.addEventListener("change", () => {
      setLayerVisibility(layers.trails, state.showTrails && !!ui.layerTrails.checked);
      renderAt(state.viewTimeMs);
    });

    ui.filterType.addEventListener("change", () => {
      state.filter.type = ui.filterType.value || "ALL";
      renderAt(state.viewTimeMs);
    });

    ui.filterMinScore.addEventListener("input", () => {
      state.filter.minScore = Number(ui.filterMinScore.value || 0);
      renderAt(state.viewTimeMs);
    });

    ui.filterOnlyActive.addEventListener("change", () => {
      state.filter.onlyActive = !!ui.filterOnlyActive.checked;
      renderAt(state.viewTimeMs);
    });

    ui.filterText.addEventListener("input", debounce(() => {
      state.filter.text = String(ui.filterText.value || "").trim();
      renderAt(state.viewTimeMs);
    }, 120));

    syncControlStates();
  }

  function syncControlStates() {
    if (ui.liveBtn) ui.liveBtn.disabled = state.live;
    if (ui.pauseBtn) ui.pauseBtn.disabled = !state.live;

    if (ui.timeLabel) ui.timeLabel.textContent = fmtTime(state.viewTimeMs);

    const min = state.minTs ?? nowMs();
    const max = state.maxTs ?? nowMs();

    if (ui.slider) {
      ui.slider.min = String(min);
      ui.slider.max = String(max);
      ui.slider.value = String(state.live ? max : state.viewTimeMs);
    }
  }

  function setViewTime(ms) {
    state.viewTimeMs = ms;
    syncControlStates();
    setWsStatus(`WS: ${state.wsConnected ? "connected" : "disconnected"} | ${state.live ? "LIVE" : "PAUSED"} @ ${fmtTime(ms)}`);
  }

  function goLive() {
    state.live = true;
    const max = state.maxTs ?? nowMs();
    setViewTime(max);
  }

  function pause() {
    state.live = false;
    setViewTime(state.viewTimeMs ?? (state.maxTs ?? nowMs()));
  }

  // -----------------------------
  // Filtering
  // -----------------------------
  function threatPassesFilter(th) {
    const f = state.filter;
    const type = String(th.type ?? "UNKNOWN");
    const score = Number(th.score ?? 0);
    const active = Boolean(th.active);

    if (f.type && f.type !== "ALL" && type !== f.type) return false;
    if (Number.isFinite(f.minScore) && score < Number(f.minScore)) return false;
    if (f.onlyActive && !active) return false;

    if (f.text) {
      const t = f.text.toLowerCase();
      const hay = `${th.id} ${type} ${th.level ?? ""} ${score} ${active} ${JSON.stringify(th.reason ?? th.meta ?? {})}`.toLowerCase();
      if (!hay.includes(t)) return false;
    }
    return true;
  }

  // -----------------------------
  // History management
  // -----------------------------
  function appendHist(map, id, entry) {
    const arr = map.get(id) || [];
    arr.push(entry);
    // prune to avoid runaway (keep last 5000)
    if (arr.length > 5000) arr.splice(0, arr.length - 5000);
    map.set(id, arr);
  }

  function latestAt(histArr, ts) {
    // histArr is append-only, mostly sorted; find last <= ts by reverse scan (fast enough)
    for (let i = histArr.length - 1; i >= 0; i--) {
      if (histArr[i].ts <= ts) return histArr[i];
    }
    return null;
  }

  // -----------------------------
  // Rendering: side panels + map layers
  // -----------------------------
  function clearLayerGroups() {
    layers.tracks?.clearLayers();
    layers.threats?.clearLayers();
    layers.trails?.clearLayers();
    markers.tracks.clear();
    markers.threats.clear();
    polylines.tracks.clear();
    polylines.threats.clear();
  }

  function renderSidePanels(trackList, threatList) {
    if (tracksEl) {
      tracksEl.innerHTML = "";
      for (const t of trackList) {
        const div = document.createElement("div");
        div.className = "small";
        const vr = (t.vr != null) ? ` vr=${t.vr}` : "";
        const rng = (t.range != null) ? ` r=${t.range}` : "";
        const az  = (t.az != null) ? ` az=${t.az}` : "";
        div.textContent = `${t.id}  (${t.state ?? ""})${rng}${az}${vr}`;
        tracksEl.appendChild(div);
      }
      if (trackList.length === 0) tracksEl.innerHTML = "<div class='small'>—</div>";
    }

    if (threatsEl) {
      threatsEl.innerHTML = "";
      for (const h of threatList) {
        const div = document.createElement("div");
        div.className = `card ${h.level === "HIGH" ? "th-high" : h.level === "MED" ? "th-med" : "th-low"}`;
        div.style.padding = "8px";
        div.style.marginBottom = "8px";

        const title = document.createElement("div");
        title.className = "row";
        title.style.justifyContent = "space-between";
        title.innerHTML = `<b>${h.id}</b><span class="badge">${h.type ?? "UNKNOWN"} | ${h.level ?? "N/A"} | ${h.score ?? 0}</span>`;

        const reason = document.createElement("div");
        reason.className = "small";
        reason.style.marginTop = "6px";
        reason.textContent = h.reason?.rule ?? h.reason?.reason ?? (typeof h.reason === "string" ? h.reason : "");

        div.appendChild(title);
        if (reason.textContent) div.appendChild(reason);

        threatsEl.appendChild(div);
      }
      if (threatList.length === 0) threatsEl.innerHTML = "<div class='small'>—</div>";
    }

    if (agentsEl) {
      agentsEl.innerHTML = "";
      const arr = Array.from(state.agents.values());
      for (const a of arr) {
        const div = document.createElement("div");
        div.className = "small";
        div.textContent = `${a.name ?? a.id ?? "agent"}: ${a.status ?? a.state ?? "OK"}`;
        agentsEl.appendChild(div);
      }
      if (arr.length === 0) agentsEl.innerHTML = "<div class='small'>—</div>";
    }
  }

  function markerFor(type, item) {
    // Keep simple markers; you can swap to DivIcon for tactical style later.
    const latlng = [Number(item.lat ?? item.y ?? 0), Number(item.lon ?? item.x ?? 0)];
    return window.L.marker(latlng);
  }

  function polylineFor(points) {
    return window.L.polyline(points, { weight: 3, opacity: 0.35 });
  }

  function buildTrailPoints(histArr, viewTs) {
    const cutoff = viewTs - state.trailSeconds * 1000;
    const pts = [];
    for (let i = histArr.length - 1; i >= 0; i--) {
      const p = histArr[i];
      if (p.ts > viewTs) continue;
      if (p.ts < cutoff) break;
      const lat = Number(p.lat ?? p.y);
      const lon = Number(p.lon ?? p.x);
      if (Number.isFinite(lat) && Number.isFinite(lon)) pts.push([lat, lon]);
    }
    pts.reverse();
    return pts;
  }

  function renderAt(viewTs) {
    if (!map) return;

    // Build current visible state at viewTs
    const tracksNow = [];
    for (const [id, hist] of state.trackHist.entries()) {
      const cur = latestAt(hist, viewTs);
      if (!cur) continue;
      tracksNow.push(cur);
    }

    const threatsNow = [];
    for (const [id, hist] of state.threatHist.entries()) {
      const cur = latestAt(hist, viewTs);
      if (!cur) continue;
      if (!threatPassesFilter(cur)) continue;
      threatsNow.push(cur);
    }

    // Render panels
    renderSidePanels(
      tracksNow.sort((a,b)=>String(a.id).localeCompare(String(b.id))),
      threatsNow.sort((a,b)=>(Number(b.score??0)-Number(a.score??0)))
    );

    // Render map layers
    clearLayerGroups();

    setLayerVisibility(layers.tracks, state.showTracks);
    setLayerVisibility(layers.threats, state.showThreats);
    setLayerVisibility(layers.trails, state.showTrails && (!!ui.layerTrails ? ui.layerTrails.checked : true));

    // Tracks markers + trails
    if (state.showTracks) {
      for (const t of tracksNow) {
        const id = String(t.id);
        const lat = Number(t.lat ?? t.y);
        const lon = Number(t.lon ?? t.x);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

        const m = markerFor("track", { ...t, lat, lon });
        m.bindPopup(`Track ${id}`);
        m.addTo(layers.tracks);

        markers.tracks.set(id, m);

        if (state.showTrails && (ui.layerTrails ? ui.layerTrails.checked : true)) {
          const hist = state.trackHist.get(id) || [];
          const pts = buildTrailPoints(hist, viewTs);
          if (pts.length >= 2) {
            const pl = polylineFor(pts);
            pl.addTo(layers.trails);
            polylines.tracks.set(id, pl);
          }
        }
      }
    }

    // Threat markers + trails
    if (state.showThreats) {
      for (const h of threatsNow) {
        const id = String(h.id);
        const lat = Number(h.lat ?? h.y);
        const lon = Number(h.lon ?? h.x);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

        const m = markerFor("threat", { ...h, lat, lon });
        m.bindPopup(`Threat ${id} (${h.type ?? "UNKNOWN"}) score=${h.score ?? 0}`);
        m.addTo(layers.threats);

        markers.threats.set(id, m);

        if (state.showTrails && (ui.layerTrails ? ui.layerTrails.checked : true)) {
          const hist = state.threatHist.get(id) || [];
          const pts = buildTrailPoints(hist, viewTs);
          if (pts.length >= 2) {
            const pl = polylineFor(pts);
            pl.addTo(layers.trails);
            polylines.threats.set(id, pl);
          }
        }
      }
    }
  }

  // -----------------------------
  // Ingest messages
  // -----------------------------
  function normalizeThreat(obj) {
    const id = String(obj.id ?? obj.threat_id ?? obj.name ?? "threat");
    const ts = getTs(obj);
    const type = String(obj.type ?? obj.sensor ?? "UNKNOWN");
    const level = String(obj.level ?? obj.severity ?? "N/A");
    const score = Number(obj.score ?? obj.threat_score ?? 0);
    const active = Boolean(obj.active ?? obj.is_active ?? false);

    // prefer lat/lon; fallback x/y if already lat/lon
    const lat = Number(obj.lat ?? obj.y);
    const lon = Number(obj.lon ?? obj.x);

    return { ...obj, id, ts, type, level, score, active, lat, lon };
  }

  function normalizeTrack(obj) {
    const id = String(obj.id ?? obj.track_id ?? obj.name ?? "track");
    const ts = getTs(obj);
    const stateStr = obj.state ?? obj.status ?? "";
    const lat = Number(obj.lat ?? obj.y);
    const lon = Number(obj.lon ?? obj.x);

    return { ...obj, id, ts, state: stateStr, lat, lon };
  }

  function ingest(msg) {
      // ---- BACKEND ADAPTER (cop.* events) ----
  if (msg.event_type) {
    // cop.snapshot → snapshot
    if (msg.event_type === "cop.snapshot") {
      msg = {
        type: "snapshot",
        data: {
          tracks: msg.tracks || msg.data?.tracks || [],
          threats: msg.threats || msg.data?.threats || [],
          agents: msg.agents || msg.data?.agents || []
        },
        ts: Date.parse(msg.timestamp)
      };
    }

    // cop.track
    else if (msg.event_type === "cop.track") {
      msg = {
        type: "track",
        track: msg.track || msg.data,
        ts: Date.parse(msg.timestamp)
      };
    }

    // cop.threat
    else if (msg.event_type === "cop.threat") {
      msg = {
        type: "threat",
        threat: msg.threat || msg.data,
        ts: Date.parse(msg.timestamp)
      };
    }
  }
  // ---- END ADAPTER ----

    if (!msg || typeof msg !== "object") return;

    const kind = String(msg.type ?? msg.kind ?? "");
    const payload = msg.data ?? msg.payload ?? msg;

    const ts = getTs(msg);

    if (kind === "track" || msg.track) {
      const t0 = msg.track ?? payload;
      const t = normalizeTrack(t0);
      updateBounds(t.ts);
      appendHist(state.trackHist, t.id, t);
      pushTail(`[${fmtTime(t.ts)}] TRACK ${t.id}`);
    } else if (kind === "threat" || msg.threat) {
      const h0 = msg.threat ?? payload;
      const h = normalizeThreat(h0);
      updateBounds(h.ts);
      appendHist(state.threatHist, h.id, h);
      pushTail(`[${fmtTime(h.ts)}] THREAT ${h.id} ${h.type} score=${h.score}`);
    } else if (kind === "snapshot") {
      const snap = payload;
      const tss = getTs(snap);
      updateBounds(tss);

      if (Array.isArray(snap?.tracks)) {
        for (const tt of snap.tracks) {
          const t = normalizeTrack({ ...tt, ts: getTs(tt) || tss });
          updateBounds(t.ts);
          appendHist(state.trackHist, t.id, t);
        }
      }
      if (Array.isArray(snap?.threats)) {
        for (const hh of snap.threats) {
          const h = normalizeThreat({ ...hh, ts: getTs(hh) || tss });
          updateBounds(h.ts);
          appendHist(state.threatHist, h.id, h);
        }
      }
      if (Array.isArray(snap?.agents)) {
        for (const a of snap.agents) {
          const id = String(a.id ?? a.name ?? "agent");
          state.agents.set(id, { ...a, id });
        }
      }
      pushTail(`[${fmtTime(tss)}] SNAPSHOT tracks=${snap?.tracks?.length ?? 0} threats=${snap?.threats?.length ?? 0}`);
    } else if (kind === "agent" || msg.agent) {
      const a0 = msg.agent ?? payload;
      const id = String(a0.id ?? a0.name ?? "agent");
      state.agents.set(id, { ...a0, id });
      pushTail(`[${fmtTime(getTs(a0))}] AGENT ${id} ${a0.status ?? a0.state ?? ""}`);
    } else if (kind === "reset") {
      clearLocalState("WS_RESET");
      pushTail(`[${fmtTime(ts)}] RESET (WS)`);
    } else {
      pushTail(`[${fmtTime(ts)}] MSG ${kind || "unknown"}`);
    }

    // keep slider updated
    if (state.live) {
      const max = state.maxTs ?? nowMs();
      setViewTime(max);
      renderAt(state.viewTimeMs);
    } else {
      syncControlStates();
      renderAt(state.viewTimeMs);
    }
  }

  // -----------------------------
  // WS connect (robust)
  // -----------------------------
  function connectWS() {
    const url = WS_CANDIDATES[wsCandidateIdx % WS_CANDIDATES.length];
    wsCandidateIdx++;

    setWsStatus(`WS: connecting -> ${url}`);
    pushTail(`[${fmtTime(nowMs())}] WS connecting -> ${url}`);

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      state.wsConnected = false;
      setWsStatus(`WS: failed (${String(e)})`);
      setTimeout(connectWS, 1200);
      return;
    }

    state.ws = ws;

    const openTimeout = setTimeout(() => {
      if (!state.wsConnected) {
        try { ws.close(); } catch {}
        setWsStatus("WS: connect timeout -> retrying...");
      }
    }, 2500);

    ws.onopen = () => {
      clearTimeout(openTimeout);
      state.wsConnected = true;
      setWsStatus(`WS: connected | ${state.live ? "LIVE" : "PAUSED"} @ ${fmtTime(state.viewTimeMs)}`);
      pushTail(`[${fmtTime(nowMs())}] WS connected`);
    };

    ws.onmessage = (ev) => {
      const msg = safeJsonParse(ev.data);
      if (!msg) return;
      ingest(msg);
    };

    ws.onerror = () => {
      setWsStatus("WS: error");
    };

    ws.onclose = (ev) => {
      clearTimeout(openTimeout);
      state.wsConnected = false;
      setWsStatus(`WS: disconnected (code=${ev.code}) retry in 1.2s`);
      pushTail(`[${fmtTime(nowMs())}] WS closed code=${ev.code}`);
      setTimeout(connectWS, 1200);
    };
  }

  // -----------------------------
  // Reset
  // -----------------------------
  function clearLocalState(reason = "LOCAL_CLEAR") {
    state.trackHist.clear();
    state.threatHist.clear();
    state.agents.clear();
    state.tail.length = 0;

    // reset bounds
    const t = nowMs();
    state.minTs = t;
    state.maxTs = t;
    state.viewTimeMs = t;

    // clear UI
    if (tailEl) tailEl.textContent = "";
    if (tracksEl) tracksEl.innerHTML = "<div class='small'>—</div>";
    if (threatsEl) threatsEl.innerHTML = "<div class='small'>—</div>";
    if (agentsEl) agentsEl.innerHTML = "<div class='small'>—</div>";

    clearLayerGroups();

    pushTail(`[${fmtTime(t)}] State cleared: ${reason}`);
    syncControlStates();
  }

  async function serverReset() {
    const resp = await fetch(API_RESET, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ reason: "ui_reset", ts: nowMs() }),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Reset failed (${resp.status}): ${text}`);
    }
    return resp.json().catch(() => ({}));
  }

  // Clear button -> backend reset + local clear + live
  function wireClearBtn() {
    if (!clearBtn) return;
    clearBtn.addEventListener("click", async () => {
      const prev = clearBtn.textContent;
      clearBtn.disabled = true;
      clearBtn.textContent = "Resetting...";
      try {
        await serverReset();
        clearLocalState("SERVER_RESET_OK");
        state.live = true;
        goLive();
      } catch (e) {
        // still clear locally to keep demos deterministic
        clearLocalState("SERVER_RESET_FAILED_LOCAL_CLEARED");
        setWsStatus(`Reset error: ${String(e?.message || e)}`);
      } finally {
        clearBtn.textContent = prev || "Clear";
        clearBtn.disabled = false;
      }
    });
  }

  // -----------------------------
  // Boot
  // -----------------------------
  function boot() {
    try {
      initMap();
    } catch (e) {
      setWsStatus(`Map init error: ${String(e?.message || e)}`);
      pushTail(`[${fmtTime(nowMs())}] Map init error: ${String(e?.message || e)}`);
      return;
    }

    // init bounds/time
    const t = nowMs();
    state.minTs = t;
    state.maxTs = t;
    state.viewTimeMs = t;

    injectControls();
    wireClearBtn();

    // initial render
    syncControlStates();
    renderAt(state.viewTimeMs);

    // connect ws
    connectWS();

    // live tick updates slider label even if no messages
    setInterval(() => {
      if (!state.live) return;
      const max = state.maxTs ?? nowMs();
      state.viewTimeMs = max;
      syncControlStates();
    }, 200);
  }

  window.addEventListener("load", boot);
})();
