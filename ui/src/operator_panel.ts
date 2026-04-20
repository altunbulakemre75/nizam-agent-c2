/**
 * Operatör paneli — sağ yan bar: track listesi + detay + ENGAGE onay butonu.
 *
 * Harita üzerine değil, sağ tarafta sabit yan panel. Her track için:
 *   - ID + class + confidence + state badge
 *   - Pozisyon + hız
 *   - Kaynak sensörler (camera, rf_odid, ...)
 *   - "Detail" → full track JSON + decision + guardrails
 *   - "Zoom" → kamera track'e fly-to
 *
 * ENGAGE onay butonu sadece backend tarafından `requires_operator_approval=true`
 * gelen karara aktif olur. Onaylanınca WebSocket'e approve mesajı gider.
 */
import type { Viewer } from "cesium";
import { Cartesian3 } from "cesium";
import type { Track } from "./track_renderer";

export interface OperatorDecision {
  track_id: string;
  action: "log" | "alert" | "engage" | "handoff";
  threat_level: "low" | "medium" | "high" | "critical";
  requires_operator_approval: boolean;
  reasoning: string;
  guardrail_reasoning?: string;
  guardrails_triggered?: string[];
}

export class OperatorPanel {
  private readonly container: HTMLDivElement;
  private readonly listEl: HTMLDivElement;
  private readonly tracks = new Map<string, Track>();
  private readonly decisions = new Map<string, OperatorDecision>();

  constructor(
    private readonly viewer: Viewer,
    private readonly approveCallback: (trackId: string) => void = () => {},
  ) {
    this.container = this._buildContainer();
    this.listEl = this.container.querySelector("#op-track-list") as HTMLDivElement;
    document.body.appendChild(this.container);
  }

  updateTrack(track: Track): void {
    this.tracks.set(track.track_id, track);
    this._render();
  }

  removeTrack(trackId: string): void {
    this.tracks.delete(trackId);
    this.decisions.delete(trackId);
    this._render();
  }

  updateDecision(decision: OperatorDecision): void {
    this.decisions.set(decision.track_id, decision);
    this._render();
  }

  private _buildContainer(): HTMLDivElement {
    const div = document.createElement("div");
    div.id = "operator-panel";
    div.style.cssText = `
      position: fixed; right: 0; top: 0; bottom: 0;
      width: 340px; background: rgba(10,25,41,0.93); color: #e0e6ed;
      font: 12px ui-monospace, monospace; overflow-y: auto;
      border-left: 1px solid #2a4055; padding: 10px; z-index: 100;
    `;
    div.innerHTML = `
      <h3 style="margin:0 0 10px;font-size:14px;color:#4fc3f7;">TRACKS</h3>
      <div id="op-track-list"></div>
    `;
    return div;
  }

  private _stateColor(state: string, confidence: number): string {
    if (state === "lost") return "#888";
    if (confidence >= 0.85) return "#ef5350";
    if (confidence >= 0.6) return "#ffa726";
    if (state === "tentative") return "#ffeb3b";
    return "#66bb6a";
  }

  private _actionColor(action: string): string {
    switch (action) {
      case "engage": return "#e53935";
      case "handoff": return "#fb8c00";
      case "alert": return "#fdd835";
      default: return "#66bb6a";
    }
  }

  private _render(): void {
    const items = Array.from(this.tracks.values()).sort(
      (a, b) => b.confidence - a.confidence,
    );
    this.listEl.innerHTML = items.map((t) => this._renderItem(t)).join("");

    // Event handler'ları bağla
    this.listEl.querySelectorAll<HTMLButtonElement>("[data-action='zoom']").forEach((btn) => {
      btn.onclick = () => {
        const tid = btn.dataset.trackId!;
        const track = this.tracks.get(tid);
        if (!track || !track.latitude || !track.longitude) return;
        this.viewer.camera.flyTo({
          destination: Cartesian3.fromDegrees(track.longitude, track.latitude, 2000),
          duration: 1.0,
        });
      };
    });
    this.listEl.querySelectorAll<HTMLButtonElement>("[data-action='approve']").forEach((btn) => {
      btn.onclick = () => {
        const tid = btn.dataset.trackId!;
        if (confirm(`ENGAGE track ${tid}? Bu geri alınamaz.`)) {
          this.approveCallback(tid);
          btn.disabled = true;
          btn.textContent = "✓ ONAYLANDI";
        }
      };
    });
  }

  private _renderItem(t: Track): string {
    const color = this._stateColor(t.state, t.confidence);
    const label = t.uas_id || t.class_name || t.track_id.slice(0, 8);
    const conf = (t.confidence * 100).toFixed(0);
    const sources = (t.sources || []).join(", ") || "—";
    const decision = this.decisions.get(t.track_id);

    let decisionBlock = "";
    if (decision) {
      const acolor = this._actionColor(decision.action);
      const approveBtn = decision.requires_operator_approval
        ? `<button data-action="approve" data-track-id="${t.track_id}"
            style="background:${acolor};color:#000;border:none;padding:4px 8px;
                   border-radius:3px;cursor:pointer;font-size:11px;margin-top:4px;">
            ${decision.action.toUpperCase()} ONAYLA
          </button>`
        : `<span style="color:${acolor};font-weight:bold;">${decision.action.toUpperCase()}</span>`;

      const guards = (decision.guardrails_triggered || []).length > 0
        ? `<div style="color:#ff9800;margin-top:2px;">⚠ ${decision.guardrails_triggered!.join(", ")}</div>`
        : "";

      decisionBlock = `
        <div style="margin-top:6px;padding:4px 6px;background:rgba(255,255,255,0.04);
                    border-left:3px solid ${acolor};">
          ${approveBtn}
          <div style="color:#999;margin-top:3px;">${decision.reasoning}</div>
          ${guards}
        </div>
      `;
    }

    return `
      <div style="margin-bottom:8px;padding:8px;border-radius:4px;
                  background:rgba(255,255,255,0.02);border:1px solid #2a4055;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong style="color:${color};">${label}</strong>
          <span style="color:${color};font-size:11px;">${conf}%</span>
        </div>
        <div style="color:#999;margin-top:2px;">
          state=<span style="color:${color};">${t.state}</span>
          hits=${t.hits || 0}
        </div>
        <div style="color:#777;font-size:11px;margin-top:2px;">
          ${t.latitude?.toFixed(5)}, ${t.longitude?.toFixed(5)} @ ${Math.round(t.altitude || 0)}m
        </div>
        <div style="color:#777;font-size:11px;">sources: ${sources}</div>
        <button data-action="zoom" data-track-id="${t.track_id}"
                style="margin-top:4px;background:#1e3a5f;color:#fff;border:none;
                       padding:3px 8px;border-radius:3px;cursor:pointer;font-size:11px;">
          Zoom
        </button>
        ${decisionBlock}
      </div>
    `;
  }
}
