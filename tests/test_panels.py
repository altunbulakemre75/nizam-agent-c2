"""
tests/test_panels.py — Node-based tests for modules/panels.js.

Each builder is tested as a pure function: call with sample data, assert
that the returned HTML string contains the expected fragments. No DOM,
no Leaflet, no app.js state needed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

node_available = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(
    not node_available,
    reason="Node.js not available — panels JS tests are skipped",
)


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Node script failed (rc={result.returncode}):\n"
            f"STDERR: {result.stderr}\nSTDOUT: {result.stdout}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Shared loader: window shim + load prerequisite modules + panels.js
# ---------------------------------------------------------------------------
_LOAD = r"""
const fs = require('fs');
global.window = { NIZAM: undefined };
global.document = {
  createElement: () => ({
    textContent: '', innerHTML: '', style: {},
    setAttribute(){}, addEventListener(){}, appendChild(){},
  }),
  querySelector: () => null,
};
for (const f of [
  'cop/static/modules/utils.js',
  'cop/static/modules/constants.js',
  'cop/static/modules/panels.js',
]) {
  eval(fs.readFileSync(f, 'utf-8'));
}
const P = window.NIZAM.panels;
"""


class TestBuildBreachHtml:
    def test_empty_returns_no_warnings_message(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildBreachHtml([]);
            console.log(JSON.stringify({ hasBreach: h.includes('No predicted breaches') }));
        """)
        assert out["hasBreach"] is True

    def test_null_treated_as_empty(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildBreachHtml(null);
            console.log(JSON.stringify({ ok: h.includes('No predicted') }));
        """)
        assert out["ok"] is True

    def test_breach_row_contains_track_and_zone(self):
        out = _run_node(_LOAD + r"""
            const breach = {
              track_id: 'TRK-42', zone_name: 'ALPHA', severity: 'CRITICAL',
              confidence: 'HIGH', time_to_breach_s: 30, current_distance_m: 200,
              zone_type: 'restricted',
            };
            const h = P.buildBreachHtml([breach]);
            console.log(JSON.stringify({
              hasTrack: h.includes('TRK-42'),
              hasZone:  h.includes('ALPHA'),
              hasCertain: h.includes('CERTAIN'),
            }));
        """)
        assert out["hasTrack"]   is True
        assert out["hasZone"]    is True
        assert out["hasCertain"] is True

    def test_caps_at_six_rows(self):
        out = _run_node(_LOAD + r"""
            const breaches = Array.from({length: 10}, (_, i) => ({
              track_id: 'T' + i, zone_name: 'Z', severity: 'HIGH',
              confidence: 'LOW', time_to_breach_s: 5, current_distance_m: 100, zone_type: 'kill',
            }));
            const h = P.buildBreachHtml(breaches);
            // Count how many track IDs appear (T0..T5 should, T6..T9 should not)
            let count = 0;
            for (let i = 0; i < 10; i++) { if (h.includes('T' + i)) count++; }
            console.log(JSON.stringify({ count }));
        """)
        assert out["count"] == 6


class TestBuildMLHtml:
    def test_empty_no_model(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildMLHtml({}, false);
            console.log(JSON.stringify({ hasNoModel: h.includes('egitim') }));
        """)
        assert out["hasNoModel"] is True

    def test_empty_model_available(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildMLHtml({}, true);
            console.log(JSON.stringify({ waitMsg: h.includes('bekleniyor') }));
        """)
        assert out["waitMsg"] is True

    def test_track_appears_with_level_badge(self):
        out = _run_node(_LOAD + r"""
            const preds = { 'abc-123': { ml_level: 'HIGH', ml_probability: 0.91 } };
            const h = P.buildMLHtml(preds, true);
            console.log(JSON.stringify({
              hasTrack: h.includes('abc-123'),
              hasLevel: h.includes('HIGH'),
              hasPct:   h.includes('91'),
            }));
        """)
        assert out["hasTrack"] is True
        assert out["hasLevel"] is True
        assert out["hasPct"]   is True

    def test_sorted_by_probability_desc(self):
        out = _run_node(_LOAD + r"""
            const preds = {
              'low-p':  { ml_level: 'LOW',  ml_probability: 0.1 },
              'high-p': { ml_level: 'HIGH', ml_probability: 0.95 },
            };
            const h = P.buildMLHtml(preds, true);
            // high-p should appear before low-p in the HTML
            console.log(JSON.stringify({ order: h.indexOf('high-p') < h.indexOf('low-p') }));
        """)
        assert out["order"] is True


class TestBuildROEHtml:
    def test_empty_returns_no_engagements(self):
        out = _run_node(_LOAD + r"""
            const r = P.buildROEHtml([]);
            console.log(JSON.stringify({ noEng: r.html.includes('No engagements'), crit: r.hasCritical }));
        """)
        assert out["noEng"] is True
        assert out["crit"]  is False

    def test_advisory_row_uses_turkish_label(self):
        out = _run_node(_LOAD + r"""
            const adv = [{
              engagement: 'WEAPONS_FREE', track_id: 'TRK-7',
              urgency: 'HIGH', is_coordinated: false, in_kill_zone: false,
              confidence: 80, reasons: ['close range'],
            }];
            const r = P.buildROEHtml(adv);
            console.log(JSON.stringify({
              hasTurkish: r.html.includes('SERBEST'),
              hasTrack:   r.html.includes('TRK-7'),
              notCritical: !r.hasCritical,
            }));
        """)
        assert out["hasTurkish"]  is True
        assert out["hasTrack"]    is True
        assert out["notCritical"] is True

    def test_critical_urgency_sets_flag(self):
        out = _run_node(_LOAD + r"""
            const adv = [{
              engagement: 'WEAPONS_HOLD', track_id: 'T1',
              urgency: 'CRITICAL', is_coordinated: false, in_kill_zone: true,
              confidence: null, reasons: [],
            }];
            const r = P.buildROEHtml(adv);
            console.log(JSON.stringify({ crit: r.hasCritical, hasKill: r.html.includes('KILL') }));
        """)
        assert out["crit"]    is True
        assert out["hasKill"] is True


class TestBuildConfidenceHtml:
    def test_empty_scores(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildConfidenceHtml({});
            console.log(JSON.stringify({ empty: h.includes('Tehdit yok') }));
        """)
        assert out["empty"] is True

    def test_grade_badge_present(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildConfidenceHtml({ 'XY-99': { confidence: 87, grade: 'HIGH' } });
            console.log(JSON.stringify({ hasHigh: h.includes('HIGH'), hasTrack: h.includes('XY-99') }));
        """)
        assert out["hasHigh"]  is True
        assert out["hasTrack"] is True

    def test_sorted_by_confidence_desc(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildConfidenceHtml({
              'TRACK-BETA':  { confidence: 20, grade: 'LOW' },
              'TRACK-ALPHA': { confidence: 90, grade: 'HIGH' },
            });
            console.log(JSON.stringify({
              order: h.indexOf('TRACK-ALPHA') < h.indexOf('TRACK-BETA'),
            }));
        """)
        assert out["order"] is True


class TestBuildTacticalHtml:
    def test_empty(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildTacticalHtml([]);
            console.log(JSON.stringify({ empty: h.includes('No recommendations') }));
        """)
        assert out["empty"] is True

    def test_recommendation_row(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildTacticalHtml([{ type:'INTERCEPT', priority:1, message:'Engage T1' }]);
            console.log(JSON.stringify({
              hasType: h.includes('INTERCEPT'),
              hasPrio: h.includes('P1'),
              hasMsg:  h.includes('Engage T1'),
            }));
        """)
        assert out["hasType"] is True
        assert out["hasPrio"] is True
        assert out["hasMsg"]  is True

    def test_unknown_type_uses_bullet(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildTacticalHtml([{ type:'UNKNOWN_XYZ', priority:2, message:'' }]);
            console.log(JSON.stringify({ hasBullet: h.includes('\u2022') }));
        """)
        assert out["hasBullet"] is True

    def test_caps_at_eight_rows(self):
        out = _run_node(_LOAD + r"""
            const recs = Array.from({length: 12}, (_, i) => ({
              type: 'MONITOR', priority: i, message: 'msg-' + i,
            }));
            const h = P.buildTacticalHtml(recs);
            let count = 0;
            for (let i = 0; i < 12; i++) { if (h.includes('msg-' + i)) count++; }
            console.log(JSON.stringify({ count }));
        """)
        assert out["count"] == 8


class TestBuildMetricsHtml:
    def test_empty_payload_renders_without_crash(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildMetricsHtml({});
            console.log(JSON.stringify({ hasSection: h.includes('Server Metrics') }));
        """)
        assert out["hasSection"] is True

    def test_uptime_appears(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildMetricsHtml({ uptime_s: 120 });
            console.log(JSON.stringify({ hasUp: h.includes('2.0m up') }));
        """)
        assert out["hasUp"] is True

    def test_track_and_threat_counts(self):
        out = _run_node(_LOAD + r"""
            const h = P.buildMetricsHtml({
              state: { tracks: 42, threats: 7, assets: 3, zones: 2, tasks: 1 },
            });
            console.log(JSON.stringify({ has42: h.includes('42'), has7: h.includes('7') }));
        """)
        assert out["has42"] is True
        assert out["has7"]  is True

    def test_bar_color_helper(self):
        out = _run_node(_LOAD + r"""
            const b = P._barColor;
            console.log(JSON.stringify({
              ok:   b(100, 500, 2000),
              warn: b(600, 500, 2000),
              crit: b(2500, 500, 2000),
            }));
        """)
        assert out["ok"]   == "var(--ok)"
        assert out["warn"] == "var(--warn)"
        assert out["crit"] == "var(--danger)"


class TestTacConstantsExposed:
    def test_tac_icons_and_colors_exported(self):
        out = _run_node(_LOAD + r"""
            const icons  = Object.keys(P._TAC_ICONS).sort();
            const colors = Object.keys(P._TAC_COLORS).sort();
            console.log(JSON.stringify({ icons, colors, same: JSON.stringify(icons) === JSON.stringify(colors) }));
        """)
        assert out["same"] is True
        assert "INTERCEPT" in out["icons"]
        assert "MONITOR"   in out["icons"]
