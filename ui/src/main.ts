import { Ion, Viewer, Cartesian3, Math as CesiumMath, Color } from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import { TrackRenderer } from "./track_renderer";

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

const renderer = new TrackRenderer(viewer);

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
      } else if (msg.type === "snapshot") {
        renderer.replaceAll(msg.tracks);
      } else if (msg.type === "remove") {
        renderer.remove(msg.track_id);
      }
    } catch {
      /* yoksay */
    }
  };
}

connect();
