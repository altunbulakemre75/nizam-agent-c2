/**
 * deck.gl overlay — track history trail ve threat heatmap.
 *
 * Cesium viewer üzerine HTML canvas overlay olarak konumlanır.
 * Her track için son 30 position point → ArcLayer + ScatterplotLayer.
 */
import { Deck } from "@deck.gl/core";
import { ScatterplotLayer, PathLayer } from "@deck.gl/layers";
import type { Track } from "./track_renderer";

export interface TrackHistory {
  trackId: string;
  path: Array<[number, number, number]>;   // [lon, lat, alt]
  confidence: number;
}

export class DeckOverlay {
  private readonly deck: Deck;
  private readonly histories = new Map<string, TrackHistory>();
  private static readonly MAX_PATH_POINTS = 40;

  constructor(canvasId: string = "deck-overlay") {
    let canvas = document.getElementById(canvasId) as HTMLCanvasElement | null;
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.id = canvasId;
      canvas.style.position = "absolute";
      canvas.style.top = "0";
      canvas.style.left = "0";
      canvas.style.width = "100%";
      canvas.style.height = "100%";
      canvas.style.pointerEvents = "none";
      canvas.style.zIndex = "5";
      document.body.appendChild(canvas);
    }
    this.deck = new Deck({
      canvas,
      initialViewState: {
        longitude: 32.8597,
        latitude: 39.9334,
        zoom: 11,
        pitch: 45,
        bearing: 0,
      },
      controller: false,  // Cesium kontrol ediyor
      layers: [],
    });
  }

  update(track: Track): void {
    const lon = track.longitude ?? 0;
    const lat = track.latitude ?? 0;
    const alt = track.altitude ?? Math.max(track.z, 0);
    const existing = this.histories.get(track.track_id);
    if (existing) {
      existing.path.push([lon, lat, alt]);
      if (existing.path.length > DeckOverlay.MAX_PATH_POINTS) {
        existing.path.shift();
      }
      existing.confidence = track.confidence;
    } else {
      this.histories.set(track.track_id, {
        trackId: track.track_id,
        path: [[lon, lat, alt]],
        confidence: track.confidence,
      });
    }
    this._redraw();
  }

  remove(trackId: string): void {
    this.histories.delete(trackId);
    this._redraw();
  }

  private _redraw(): void {
    const paths = Array.from(this.histories.values());
    this.deck.setProps({
      layers: [
        new PathLayer({
          id: "track-trails",
          data: paths,
          getPath: (d: TrackHistory) => d.path,
          getColor: (d: TrackHistory) => {
            const alpha = 180;
            if (d.confidence >= 0.85) return [255, 50, 50, alpha];
            if (d.confidence >= 0.6) return [255, 165, 0, alpha];
            return [50, 255, 100, alpha];
          },
          getWidth: 3,
          widthUnits: "pixels",
        }),
        new ScatterplotLayer({
          id: "track-heads",
          data: paths,
          getPosition: (d: TrackHistory) => d.path[d.path.length - 1],
          getFillColor: (d: TrackHistory) => {
            if (d.confidence >= 0.85) return [255, 0, 0, 230];
            if (d.confidence >= 0.6) return [255, 165, 0, 230];
            return [50, 255, 100, 230];
          },
          getRadius: 5,
          radiusUnits: "pixels",
        }),
      ],
    });
  }

  syncView(longitude: number, latitude: number, zoom: number, pitch: number, bearing: number): void {
    this.deck.setProps({
      initialViewState: { longitude, latitude, zoom, pitch, bearing },
    });
  }
}
