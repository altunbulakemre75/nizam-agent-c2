import type { Viewer, Entity } from "cesium";
import {
  Cartesian3, Color, HeightReference, PolylineGlowMaterialProperty,
  SampledPositionProperty, JulianDate, VerticalOrigin, CallbackProperty,
} from "cesium";

export interface Track {
  track_id: string;
  state: "tentative" | "confirmed" | "lost" | "deleted";
  x: number;
  y: number;
  z: number;
  vx: number;
  vy: number;
  vz: number;
  confidence: number;
  hits?: number;
  sources?: string[];
  uas_id?: string | null;
  class_name?: string | null;
  latitude?: number;
  longitude?: number;
  altitude?: number;
}

// Threat level renk eşlemesi (güven + state → renk)
function trackColor(track: Track): Color {
  if (track.state === "lost") return Color.GRAY.withAlpha(0.6);
  if (track.state === "tentative") return Color.YELLOW;
  if (track.confidence >= 0.85) return Color.RED;
  if (track.confidence >= 0.6) return Color.ORANGE;
  return Color.LIME;
}

interface TrackArtifact {
  entity: Entity;
  trailPositions: Cartesian3[];
  lastUpdate: number;
}

const TRAIL_MAX_POINTS = 30;   // ~son 30 tick
const TRAIL_LIFETIME_MS = 30_000;

export class TrackRenderer {
  private readonly artifacts = new Map<string, TrackArtifact>();

  constructor(private readonly viewer: Viewer) {}

  upsert(track: Track): void {
    const lat = track.latitude ?? 0;
    const lon = track.longitude ?? 0;
    const alt = track.altitude ?? Math.max(track.z, 0);
    const position = Cartesian3.fromDegrees(lon, lat, alt);
    const color = trackColor(track);
    const label = this._buildLabel(track);

    let artifact = this.artifacts.get(track.track_id);
    if (!artifact) {
      artifact = this._create(track, position, color, label);
    } else {
      this._update(artifact, track, position, color, label);
    }
  }

  remove(trackId: string): void {
    const artifact = this.artifacts.get(trackId);
    if (!artifact) return;
    this.viewer.entities.remove(artifact.entity);
    this.artifacts.delete(trackId);
  }

  replaceAll(tracks: Track[]): void {
    const incoming = new Set(tracks.map((t) => t.track_id));
    for (const id of Array.from(this.artifacts.keys())) {
      if (!incoming.has(id)) this.remove(id);
    }
    for (const t of tracks) this.upsert(t);
  }

  private _buildLabel(track: Track): string {
    const name = track.uas_id ?? track.class_name ?? track.track_id.slice(0, 8);
    const conf = (track.confidence * 100).toFixed(0);
    return `${name} ${conf}%`;
  }

  private _create(
    track: Track, position: Cartesian3, color: Color, label: string
  ): TrackArtifact {
    const trail: Cartesian3[] = [position];
    const entity = this.viewer.entities.add({
      id: track.track_id,
      position,
      point: {
        pixelSize: 14,
        color,
        outlineColor: Color.BLACK,
        outlineWidth: 2,
        heightReference: HeightReference.NONE,
      },
      polyline: {
        positions: new CallbackProperty(() => trail.slice(), false) as unknown as any,
        width: 2,
        material: new PolylineGlowMaterialProperty({
          glowPower: 0.25,
          color,
        }),
      },
      label: {
        text: label,
        font: "12px ui-monospace, monospace",
        fillColor: Color.WHITE,
        outlineColor: Color.BLACK,
        outlineWidth: 2,
        style: 2,
        verticalOrigin: VerticalOrigin.BOTTOM,
        pixelOffset: new Cartesian3(0, -16, 0) as unknown as any,
      },
    });
    const artifact: TrackArtifact = {
      entity,
      trailPositions: trail,
      lastUpdate: Date.now(),
    };
    this.artifacts.set(track.track_id, artifact);
    return artifact;
  }

  private _update(
    artifact: TrackArtifact, track: Track,
    position: Cartesian3, color: Color, label: string,
  ): void {
    artifact.entity.position = position as unknown as Entity["position"];
    if (artifact.entity.point) {
      artifact.entity.point.color = color as unknown as Entity["point"]["color"];
    }
    if (artifact.entity.label) {
      artifact.entity.label.text = label as unknown as Entity["label"]["text"];
    }
    artifact.trailPositions.push(position);
    if (artifact.trailPositions.length > TRAIL_MAX_POINTS) {
      artifact.trailPositions.shift();
    }
    artifact.lastUpdate = Date.now();
  }
}
