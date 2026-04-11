"""
tests/test_static_modules.py — Node-based smoke test for the new frontend modules.

The frontend is being incrementally split out of cop/static/app.js into
cop/static/modules/*.js classic scripts attached to a window.NIZAM
namespace. These tests load each module inside a tiny Node shim that
mocks `window`/`document`, executes the module, and asserts its public
API is present and behaves correctly.

Why Node-based: a real browser harness (Playwright/Selenium) is overkill
for pure-logic modules that have no DOM dependencies beyond a single
createElement stub. Skips cleanly if Node is not installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "cop" / "static" / "modules"

node_available = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(
    not node_available,
    reason="Node.js not available — static JS modules are skipped",
)


def _run_node(script: str) -> dict:
    """Execute a JS script in Node and return the parsed JSON from stdout.

    Forces UTF-8 decoding so em-dashes and other non-ASCII glyphs from
    the constants survive the trip from Node → Windows cp1252 → Python.
    """
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


_LOAD_MODULES = r"""
const fs = require('fs');
global.window = { NIZAM: undefined };
global.document = {
  createElement: () => ({
    textContent: '',
    innerHTML: '',
    style: {},
    setAttribute(){},
    addEventListener(){},
    appendChild(){},
  }),
  querySelector: () => null,
};
for (const f of [
  'cop/static/modules/utils.js',
  'cop/static/modules/constants.js',
  'cop/static/modules/store.js',
]) {
  eval(fs.readFileSync(f, 'utf-8'));
}
"""


class TestModuleLoad:
    def test_nizam_namespace_populated(self):
        out = _run_node(_LOAD_MODULES + r"""
            console.log(JSON.stringify({
              keys: Object.keys(window.NIZAM).sort()
            }));
        """)
        assert out["keys"] == ["constants", "store", "utils"]


class TestUtils:
    def test_formatters(self):
        out = _run_node(_LOAD_MODULES + r"""
            const u = window.NIZAM.utils;
            console.log(JSON.stringify({
              fmtMs_null:   u.fmtMs(null),
              fmtMs_num:    u.fmtMs(123.4),
              fmtMs_big:    u.fmtMs(4567),
              fmtNum_null:  u.fmtNum(null),
              fmtTime_0:    u.fmtTime(0),
              fmtTime_125:  u.fmtTime(125),
              fmtTime_neg:  u.fmtTime(-10),
              escHtml:      u.escHtml('<a&b>"\''),
              escText_none: u.escText(null),
              parse_ok:     u.safeJsonParse('{"a":1}'),
              parse_bad:    u.safeJsonParse('{not json'),
            }));
        """)
        DASH = "\u2014"
        assert out["fmtMs_null"]   == DASH
        assert out["fmtMs_num"]    == "123 ms"
        assert out["fmtMs_big"]    == "4567 ms"
        assert out["fmtNum_null"]  == DASH
        assert out["fmtTime_0"]    == "00:00"
        assert out["fmtTime_125"]  == "02:05"
        assert out["fmtTime_neg"]  == "00:00"
        assert "&lt;" in out["escHtml"] and "&amp;" in out["escHtml"]
        assert out["escText_none"] == ""
        assert out["parse_ok"]  == {"a": 1}
        assert out["parse_bad"] is None


class TestConstants:
    def test_key_set_matches_app_js_expectations(self):
        out = _run_node(_LOAD_MODULES + r"""
            const k = window.NIZAM.constants;
            console.log(JSON.stringify({
              has_keys: [
                'THREAT_COLORS','INTENT_META','ROE_COLORS','ROE_LABELS',
                'CONF_GRADE_COLORS','ACTION_COLORS','ACTION_BG',
                'STATUS_COLORS','OUTCOME_COLORS','ANOMALY_COLORS',
                'OP_COLORS','altitudeColor',
              ].every(key => key in k),
              threat_high: k.THREAT_COLORS.HIGH,
              action_engage: k.ACTION_COLORS.ENGAGE,
              roe_label_free: k.ROE_LABELS.WEAPONS_FREE,
              anomaly_critical: k.ANOMALY_COLORS.CRITICAL,
            }));
        """)
        assert out["has_keys"] is True
        assert out["threat_high"]      == "#e74c3c"
        assert out["action_engage"]    == "var(--danger)"
        assert out["roe_label_free"]   == "SERBEST"
        assert out["anomaly_critical"] == "#e74c3c"

    def test_altitude_color_bands(self):
        out = _run_node(_LOAD_MODULES + r"""
            const c = window.NIZAM.constants.altitudeColor;
            console.log(JSON.stringify({
              low:     c(100),
              medium:  c(500),
              high:    c(2000),
              v_high:  c(4000),
              extreme: c(8000),
              zero:    c(0),
              neg:     c(-50),
              null_v:  c(null),
            }));
        """)
        assert out["low"]     == "#27ae60"
        assert out["medium"]  == "#f1c40f"
        assert out["high"]    == "#e67e22"
        assert out["v_high"]  == "#e74c3c"
        assert out["extreme"] == "#9b59b6"
        assert out["zero"]    == "#27ae60"
        assert out["neg"]     == "#27ae60"
        assert out["null_v"]  == "#27ae60"


class TestStore:
    def test_set_get(self):
        out = _run_node(_LOAD_MODULES + r"""
            const s = window.NIZAM.store;
            s.set('a', 1);
            s.set('b', 'hello');
            console.log(JSON.stringify({
              a: s.get('a'),
              b: s.get('b'),
              miss: s.get('missing') === undefined,
            }));
        """)
        assert out == {"a": 1, "b": "hello", "miss": True}

    def test_subscribe_fires_once_per_set(self):
        out = _run_node(_LOAD_MODULES + r"""
            const s = window.NIZAM.store;
            s.clear();
            let calls = 0;
            let lastVal = null;
            s.subscribe('k', (v) => { calls++; lastVal = v; });
            s.set('k', 10);
            s.set('k', 20);
            s.set('other', 99);
            console.log(JSON.stringify({ calls, lastVal }));
        """)
        assert out == {"calls": 2, "lastVal": 20}

    def test_unsubscribe(self):
        out = _run_node(_LOAD_MODULES + r"""
            const s = window.NIZAM.store;
            s.clear();
            let calls = 0;
            const unsub = s.subscribe('k', () => { calls++; });
            s.set('k', 1);
            unsub();
            s.set('k', 2);
            s.set('k', 3);
            console.log(JSON.stringify({ calls }));
        """)
        assert out == {"calls": 1}

    def test_update_reducer_fires_subscriber(self):
        out = _run_node(_LOAD_MODULES + r"""
            const s = window.NIZAM.store;
            s.clear();
            const seen = [];
            s.subscribe('list', (v) => { seen.push(v.slice()); });
            s.set('list', []);
            s.update('list', (arr) => { arr.push('a'); return arr; });
            s.update('list', (arr) => { arr.push('b'); return arr; });
            console.log(JSON.stringify({ seen }));
        """)
        assert out["seen"] == [[], ["a"], ["a", "b"]]

    def test_throwing_subscriber_does_not_block_others(self):
        out = _run_node(_LOAD_MODULES + r"""
            const s = window.NIZAM.store;
            s.clear();
            let goodCalls = 0;
            s.subscribe('k', () => { throw new Error('boom'); });
            s.subscribe('k', () => { goodCalls++; });
            // Silence the console.error from the store's catch handler
            const origErr = console.error;
            console.error = () => {};
            s.set('k', 1);
            console.error = origErr;
            console.log(JSON.stringify({ goodCalls }));
        """)
        assert out == {"goodCalls": 1}
