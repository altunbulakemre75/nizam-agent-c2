import { Ion, Viewer, Cartesian3, Math as CesiumMath, Color } from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import { TrackRenderer } from "./track_renderer";
import { DeckOverlay } from "./deck_overlay";
import { ViewshedPanel } from "./viewshed_panel";
import { OperatorPanel } from "./operator_panel";

// Cesium offline mode — Ion token boş bırakıldı, OSM base layer kullanılır.
Ion.defaultAccessToken = "";

const viewer = new Viewer("cesium", {
  animation: false,
  timeline: false,
  baseLayerPicker: false,
  geocoder: false,
  homeButton: false,
  sceneModePicker: false,
  navigationHelpButton: false,
  fullscreenButton: false,
  selectionIndicator: false,
  infoBox: false,
});
viewer.scene.globe.baseColor = Color.fromCssColorString("#0a1929");

// Ankara'ya odaklan
viewer.camera.flyTo({
  destination: Cartesian3.fromDegrees(32.8597, 39.9334, 30000),
  orientation: {
    heading: CesiumMath.toRadians(0),
    pitch: CesiumMath.toRadians(-45),
    roll: 0,
  },
  duration: 0,
});

// Render katmanları
const renderer = new TrackRenderer(viewer);
const deckOverlay = new DeckOverlay();
const viewshed = new ViewshedPanel(viewer);
const operatorPanel = new OperatorPanel(viewer, (trackId) => {
  // ENGAGE onay → gateway'e POST
  fetch("/api/approve_engage", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_id: trackId }),
  }).catch((e) => console.warn("approve failed:", e));
});

// Kamera hareketinde deck.gl sync et
viewer.scene.postRender.addEventListener(() => {
  const camera = viewer.camera;
  const pos = camera.positionCartographic;
  deckOverlay.syncView(
    CesiumMath.toDegrees(pos.longitude),
    CesiumMath.toDegrees(pos.latitude),
    Math.max(1, 18 - Math.log2(Math.max(pos.height, 1))),
    CesiumMath.toDegrees(camera.pitch) + 90,
    CesiumMath.toDegrees(camera.heading),
  );
});

// WebSocket — tracks
const statusEl = document.getElementById("status")!;
function setStatus(online: boolean, label: string): void {
  statusEl.classList.toggle("offline", !online);
  statusEl.textContent = label;
}

function connect(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/tracks`);

  ws.onopen = () => setStatus(true, "WS ●──●");
  ws.onclose = () => {
    setStatus(false, "WS ●──○ (yeniden...)");
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "track") {
        renderer.upsert(msg.track);
        deckOverlay.update(msg.track);
        operatorPanel.updateTrack(msg.track);
      } else if (msg.type === "snapshot") {
        renderer.replaceAll(msg.tracks);
        (msg.tracks || []).forEach((t: any) => {
          deckOverlay.update(t);
          operatorPanel.updateTrack(t);
        });
      } else if (msg.type === "remove") {
        renderer.remove(msg.track_id);
        deckOverlay.remove(msg.track_id);
        operatorPanel.removeTrack(msg.track_id);
      } else if (msg.type === "decision") {
        operatorPanel.updateDecision(msg.decision);
      }
    } catch {
      /* yoksay */
    }
  };
}

connect();

// Demo: Ankara merkezde örnek bir sensör kapsama alanı (viewshed panel UI'ından
// etkileşimli olarak sensor eklenebilir)
viewshed.addSensor({
  id: "cam-01",
  latitude: 39.9334,
  longitude: 32.8597,
  altitude_m: 900,
  heading_deg: 0,
  fov_h_deg: 60,
  range_m: 5000,
});
