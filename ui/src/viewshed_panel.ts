/**
 * SensorCoveragePanel — FOV sektör polygon renderer (saf Cesium).
 *
 * ŞU ANKİ DURUM (dürüst):
 *   Düz-Earth sektör polygon. DEM kullanmıyor. "Drone bu yönde, bu kadar
 *   uzakta gözükebilir" diyen konik bir alan. Gerçek line-of-sight analizi
 *   (arazi engelleri, binalar) YAPMAZ.
 *
 * GELECEK (planlanmış, yapılmamış):
 *   maptalks.analysis.ViewshedAnalysis (ui/package.json optionalDependencies)
 *   + DEM raster (örn. OpenTopography SRTM). Bu modülün export adı
 *   değişmeden implementasyon DEM-aware'e terfi edebilir.
 */
import type { Viewer } from "cesium";
import {
  Cartesian3, Color, PolygonHierarchy, Entity, HeightReference, ColorMaterialProperty,
  Math as CesiumMath,
} from "cesium";

export interface SensorCoverage {
  id: string;
  latitude: number;
  longitude: number;
  altitude_m: number;
  heading_deg: number;    // true north clockwise
  fov_h_deg: number;
  range_m: number;
}

const EARTH_R = 6378137;

function offsetLatLon(
  lat: number, lon: number, bearingDeg: number, rangeM: number,
): [number, number] {
  const br = (bearingDeg * Math.PI) / 180;
  const dLat = (rangeM * Math.cos(br)) / EARTH_R;
  const dLon = (rangeM * Math.sin(br)) / (EARTH_R * Math.cos((lat * Math.PI) / 180));
  return [lat + dLat * 180 / Math.PI, lon + dLon * 180 / Math.PI];
}

export class ViewshedPanel {
  private readonly entities = new Map<string, Entity>();
  private readonly sensors = new Map<string, SensorCoverage>();

  constructor(private readonly viewer: Viewer) {}

  addSensor(s: SensorCoverage): void {
    this.sensors.set(s.id, s);
    this._render(s);
  }

  removeSensor(id: string): void {
    const e = this.entities.get(id);
    if (e) this.viewer.entities.remove(e);
    this.entities.delete(id);
    this.sensors.delete(id);
  }

  toggle(): void {
    for (const e of this.entities.values()) {
      if (typeof e.show === "boolean") e.show = !e.show;
    }
  }

  private _render(s: SensorCoverage): void {
    const existing = this.entities.get(s.id);
    if (existing) this.viewer.entities.remove(existing);

    // 30 noktalı fan — daha yumuşak görünüm
    const segments = 30;
    const halfFov = s.fov_h_deg / 2;
    const positions: number[] = [];

    // Tepe: sensör konumu
    positions.push(s.longitude, s.latitude, s.altitude_m);

    for (let i = 0; i <= segments; i++) {
      const bearing = s.heading_deg - halfFov + (s.fov_h_deg * i) / segments;
      const [lat, lon] = offsetLatLon(s.latitude, s.longitude, bearing, s.range_m);
      positions.push(lon, lat, s.altitude_m);
    }

    const entity = this.viewer.entities.add({
      id: `viewshed-${s.id}`,
      polygon: {
        hierarchy: new PolygonHierarchy(Cartesian3.fromDegreesArrayHeights(positions)),
        material: new ColorMaterialProperty(Color.LIME.withAlpha(0.25)),
        outline: true,
        outlineColor: Color.LIME.withAlpha(0.8),
        heightReference: HeightReference.CLAMP_TO_GROUND,
      },
      label: {
        text: s.id,
        font: "11px ui-monospace, monospace",
        fillColor: Color.LIME,
        outlineColor: Color.BLACK,
        outlineWidth: 2,
        style: 2,
        position: Cartesian3.fromDegrees(s.longitude, s.latitude, s.altitude_m + 20),
      },
    });
    this.entities.set(s.id, entity);
    // Silence unused var
    void CesiumMath;
  }
}
