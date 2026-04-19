import type { Viewer, Entity } from "cesium";
import { Cartesian3, Color, HeightReference, VerticalOrigin } from "cesium";

export interface Track {
  track_id: string;
  state: "tentative" | "confirmed" | "lost" | "deleted";
  x: number;          // not used for rendering (ENU); we use lat/lon if added
  y: number;
  z: number;
  vx: number;
  vy: number;
  vz: number;
  confidence: number;
  uas_id?: string | null;
  class_name?: string | null;
  // For now the gateway must attach lat/lon/alt alongside the ENU state
  latitude?: number;
  longitude?: number;
  altitude?: number;
}

const STATE_COLORS: Record<Track["state"], Color> = {
  tentative: Color.YELLOW,
  confirmed: Color.RED,
  lost: Color.GRAY,
  deleted: Color.BLACK,
};

export class TrackRenderer {
  private readonly entities = new Map<string, Entity>();

  constructor(private readonly viewer: Viewer) {}

  upsert(track: Track): void {
    const lat = track.latitude ?? 0;
    const lon = track.longitude ?? 0;
    const alt = track.altitude ?? Math.max(track.z, 0);
    const position = Cartesian3.fromDegrees(lon, lat, alt);
    const color = STATE_COLORS[track.state] ?? Color.WHITE;
    const label = track.uas_id ?? track.class_name ?? track.track_id.slice(0, 8);

    let entity = this.entities.get(track.track_id);
    if (!entity) {
      entity = this.viewer.entities.add({
        id: track.track_id,
        position,
        point: {
          pixelSize: 12,
          color,
          outlineColor: Color.BLACK,
          outlineWidth: 1,
          heightReference: HeightReference.NONE,
        },
        label: {
          text: label,
          font: "12px ui-monospace, monospace",
          fillColor: Color.WHITE,
          outlineColor: Color.BLACK,
          outlineWidth: 2,
          style: 2,  // FILL_AND_OUTLINE
          verticalOrigin: VerticalOrigin.BOTTOM,
          pixelOffset: new Cartesian3(0, -16, 0) as unknown as Cartesian3,
        },
      });
      this.entities.set(track.track_id, entity);
    } else {
      entity.position = position as unknown as Entity["position"];
      if (entity.point) {
        entity.point.color = color as unknown as Entity["point"]["color"];
      }
      if (entity.label) {
        entity.label.text = label as unknown as Entity["label"]["text"];
      }
    }
  }

  remove(trackId: string): void {
    const entity = this.entities.get(trackId);
    if (entity) {
      this.viewer.entities.remove(entity);
      this.entities.delete(trackId);
    }
  }

  replaceAll(tracks: Track[]): void {
    const incoming = new Set(tracks.map((t) => t.track_id));
    for (const id of Array.from(this.entities.keys())) {
      if (!incoming.has(id)) this.remove(id);
    }
    for (const t of tracks) this.upsert(t);
  }
}
