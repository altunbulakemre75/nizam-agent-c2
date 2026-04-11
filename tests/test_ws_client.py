"""
tests/test_ws_client.py — Node-based smoke tests for modules/ws-client.js.

Tests the back-off state machine, connection callbacks, message routing, and
disconnect guard entirely inside a Node subprocess — no browser needed.

Strategy: mock global.WebSocket so the test controls when it throws, when it
fires onopen/onclose, and what messages arrive.  global.setTimeout is replaced
with a synchronous "tick" queue so we can drive time forward without real waits.
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
    reason="Node.js not available — ws-client JS tests are skipped",
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
# Shared preamble: set up window, load ws-client, install test harness
# ---------------------------------------------------------------------------
_PREAMBLE = r"""
const fs = require('fs');

// Minimal window/document shim
global.window = { NIZAM: undefined };
global.document = {
  createElement: () => ({
    textContent: '', innerHTML: '', style: {},
    setAttribute(){}, addEventListener(){}, appendChild(){},
  }),
  querySelector: () => null,
};

// Synchronous tick queue replaces real setTimeout
const _ticks = [];
global.setTimeout = (fn, _delay) => { _ticks.push(fn); };
function runTicks(n) {
  for (let i = 0; i < (n || 1); i++) {
    const fn = _ticks.shift();
    if (fn) fn();
  }
}

// Load ws-client
eval(fs.readFileSync('cop/static/modules/ws-client.js', 'utf-8'));
const wsc = window.NIZAM.wsClient;

// MockWS factory — stores the last created instance globally
let _lastWs = null;
class MockWS {
  constructor(url) {
    this.url = url;
    this.onopen = null; this.onclose = null;
    this.onerror = null; this.onmessage = null;
    _lastWs = this;
  }
  close() { if (this.onclose) this.onclose(); }
}
global.WebSocket = MockWS;
"""


class TestBackoff:
    def test_initial_delay_is_base(self):
        out = _run_node(_PREAMBLE + r"""
            console.log(JSON.stringify({ delay: wsc.delay }));
        """)
        assert out["delay"] == 500

    def test_delay_doubles_on_failure(self):
        """Each failed open() call doubles the delay."""
        out = _run_node(_PREAMBLE + r"""
            // Make WebSocket constructor throw to trigger _scheduleReconnect
            global.WebSocket = function() { throw new Error("refused"); };
            const statuses = [];
            wsc.connect("ws://x", () => {}, s => statuses.push(s));
            // delay after 1st failure: 1000
            const d1 = wsc.delay;
            runTicks(1);   // fires the scheduled _open → fails again
            const d2 = wsc.delay;
            runTicks(1);
            const d3 = wsc.delay;
            console.log(JSON.stringify({ d1, d2, d3 }));
        """)
        assert out == {"d1": 1000, "d2": 2000, "d3": 4000}

    def test_delay_caps_at_max(self):
        """Delay never exceeds 30 000 ms regardless of how many failures occur."""
        out = _run_node(_PREAMBLE + r"""
            global.WebSocket = function() { throw new Error("refused"); };
            wsc.connect("ws://x", () => {}, () => {});
            // Run many ticks to saturate back-off
            for (let i = 0; i < 10; i++) runTicks(1);
            console.log(JSON.stringify({ delay: wsc.delay }));
        """)
        assert out["delay"] == 30_000

    def test_delay_resets_after_successful_open(self):
        """Successful onopen resets delay back to 500."""
        out = _run_node(_PREAMBLE + r"""
            // First: fail twice to inflate the delay
            global.WebSocket = function() { throw new Error("refused"); };
            wsc.connect("ws://x", () => {}, () => {});
            runTicks(1);
            const inflated = wsc.delay;   // 2000

            // Now install a MockWS that fires onopen immediately
            global.WebSocket = MockWS;
            runTicks(1);              // fires _open → creates MockWS
            _lastWs.onopen();         // simulate handshake success
            console.log(JSON.stringify({ inflated, reset: wsc.delay }));
        """)
        assert out["inflated"] == 2000
        assert out["reset"] == 500


class TestConnectedFlag:
    def test_false_before_connect(self):
        out = _run_node(_PREAMBLE + r"""
            console.log(JSON.stringify({ connected: wsc.connected }));
        """)
        assert out["connected"] is False

    def test_true_after_onopen(self):
        out = _run_node(_PREAMBLE + r"""
            wsc.connect("ws://x", () => {}, () => {});
            _lastWs.onopen();
            console.log(JSON.stringify({ connected: wsc.connected }));
        """)
        assert out["connected"] is True

    def test_false_after_onclose(self):
        out = _run_node(_PREAMBLE + r"""
            wsc.connect("ws://x", () => {}, () => {});
            _lastWs.onopen();
            _lastWs.onclose();
            console.log(JSON.stringify({ connected: wsc.connected }));
        """)
        assert out["connected"] is False

    def test_conn_change_callback(self):
        out = _run_node(_PREAMBLE + r"""
            const changes = [];
            wsc.connect("ws://x", () => {}, () => {}, ok => changes.push(ok));
            _lastWs.onopen();
            _lastWs.onclose();
            console.log(JSON.stringify({ changes }));
        """)
        assert out["changes"] == [True, False]


class TestMessageRouting:
    def test_valid_json_forwarded(self):
        out = _run_node(_PREAMBLE + r"""
            const received = [];
            wsc.connect("ws://x", ev => received.push(ev), () => {});
            _lastWs.onopen();
            _lastWs.onmessage({ data: '{"event_type":"cop.ping"}' });
            _lastWs.onmessage({ data: '{"event_type":"cop.track","payload":{"id":1}}' });
            console.log(JSON.stringify({ count: received.length, first: received[0] }));
        """)
        assert out["count"] == 2
        assert out["first"] == {"event_type": "cop.ping"}

    def test_invalid_json_silently_dropped(self):
        out = _run_node(_PREAMBLE + r"""
            let calls = 0;
            wsc.connect("ws://x", () => calls++, () => {});
            _lastWs.onopen();
            _lastWs.onmessage({ data: '{not valid json' });
            _lastWs.onmessage({ data: 'plain text' });
            console.log(JSON.stringify({ calls }));
        """)
        assert out["calls"] == 0


class TestDisconnect:
    def test_disconnect_sets_stopped(self):
        out = _run_node(_PREAMBLE + r"""
            wsc.connect("ws://x", () => {}, () => {});
            _lastWs.onopen();
            wsc.disconnect();
            console.log(JSON.stringify({
              connected: wsc.connected,
              stopped: wsc._stopped,
            }));
        """)
        assert out["connected"] is False
        assert out["stopped"] is True

    def test_disconnect_prevents_reconnect(self):
        """After disconnect(), onclose must not schedule another _open."""
        out = _run_node(_PREAMBLE + r"""
            let openCount = 0;
            const origOpen = wsc._open.bind(wsc);
            wsc._open = function() { openCount++; origOpen(); };

            wsc.connect("ws://x", () => {}, () => {});
            const countAfterConnect = openCount;   // 1

            wsc.disconnect();
            // Even if a stale tick fires, _open should bail on _stopped
            runTicks(5);
            console.log(JSON.stringify({ countAfterConnect, countAfterDisconnect: openCount }));
        """)
        assert out["countAfterConnect"] == 1
        assert out["countAfterDisconnect"] == 1   # no extra opens

    def test_status_callbacks_during_lifecycle(self):
        out = _run_node(_PREAMBLE + r"""
            const statuses = [];
            wsc.connect("ws://x", () => {}, s => statuses.push(s));
            _lastWs.onopen();
            _lastWs.onclose();        // schedules reconnect
            wsc.disconnect();
            console.log(JSON.stringify({ statuses }));
        """)
        # connecting… → connected → closed — retry … → (disconnect prevents further)
        assert "connecting" in out["statuses"][0]
        assert out["statuses"][1] == "WS: connected"
        assert "retry" in out["statuses"][2]
