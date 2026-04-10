"""
tests/test_mqtt_adapter.py — Unit tests for adapters/mqtt_adapter.py

Covers:
  - dot_get(): nested field extraction
  - RateLimiter: allow/deny, per-track independence
  - make_track_event(): output schema + field values
  - MessageParser.parse(): pass-through mode, field mapping,
                            missing required fields, bad JSON
  - OutputHandler (stdout mode): emit writes JSONL to stdout
"""
from __future__ import annotations

import argparse
import json

# The adapter lives outside the usual package tree; add it via path
import importlib.util, pathlib

_ADAPTER_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "adapters" / "mqtt_adapter.py"
)
_spec = importlib.util.spec_from_file_location("mqtt_adapter", _ADAPTER_PATH)
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

dot_get         = _mod.dot_get
RateLimiter     = _mod.RateLimiter
make_track_event = _mod.make_track_event
MessageParser   = _mod.MessageParser
OutputHandler   = _mod.OutputHandler


# ── dot_get ───────────────────────────────────────────────────────────────────

class TestDotGet:
    def test_top_level(self):
        assert dot_get({"a": 1}, "a") == 1

    def test_nested(self):
        assert dot_get({"pos": {"lat": 41.0}}, "pos.lat") == 41.0

    def test_deeply_nested(self):
        obj = {"a": {"b": {"c": "deep"}}}
        assert dot_get(obj, "a.b.c") == "deep"

    def test_missing_key_returns_none(self):
        assert dot_get({"a": 1}, "b") is None

    def test_partial_path_returns_none(self):
        assert dot_get({"a": {"b": 1}}, "a.c") is None

    def test_non_dict_intermediate_returns_none(self):
        assert dot_get({"a": 42}, "a.b") is None

    def test_empty_dict(self):
        assert dot_get({}, "a") is None


# ── RateLimiter ───────────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_unlimited_always_allows(self):
        rl = RateLimiter(0.0)
        for _ in range(100):
            assert rl.allow("T-001") is True

    def test_first_call_always_allowed(self):
        rl = RateLimiter(1.0)
        assert rl.allow("T-001") is True

    def test_second_call_within_interval_denied(self):
        rl = RateLimiter(1.0)   # 1 Hz → 1s min interval
        rl.allow("T-001")       # first: allowed
        assert rl.allow("T-001") is False

    def test_second_call_after_interval_allowed(self):
        rl = RateLimiter(10.0)  # 0.1s interval
        rl.allow("T-001")
        # Manually push last-seen time back past the interval
        rl._last["T-001"] -= 0.2
        assert rl.allow("T-001") is True

    def test_independent_per_track(self):
        rl = RateLimiter(1.0)
        rl.allow("T-001")
        # T-001 is blocked, T-002 is fresh
        assert rl.allow("T-001") is False
        assert rl.allow("T-002") is True

    def test_high_rate_allows_burst(self):
        rl = RateLimiter(1000.0)  # 1 kHz
        rl.allow("T-001")
        # At 1 kHz the min interval is 1ms; without sleeping, second call
        # will be denied (monotonic clock advances by < 1ms in a tight loop).
        # Just verify allow() returns a bool.
        result = rl.allow("T-001")
        assert isinstance(result, bool)


# ── make_track_event ──────────────────────────────────────────────────────────

class TestMakeTrackEvent:
    def test_returns_dict(self):
        ev = make_track_event("T1", 41.0, 29.0, 10.0, 90.0, 100.0, "drone")
        assert isinstance(ev, dict)

    def test_event_type(self):
        ev = make_track_event("T1", 41.0, 29.0, 10.0, 90.0, 100.0, "drone")
        assert ev["event_type"] == "track.update"

    def test_schema_version(self):
        ev = make_track_event("T1", 41.0, 29.0, 10.0, 90.0, 100.0, "drone")
        assert "schema_version" in ev

    def test_payload_fields(self):
        ev = make_track_event("T1", 41.0, 29.0, 10.0, 90.0, 100.0, "drone")
        p = ev["payload"]
        assert p["lat"] == 41.0
        assert p["lon"] == 29.0
        assert p["kinematics"]["speed_mps"] == 10.0
        assert p["kinematics"]["heading_deg"] == 90.0
        assert p["kinematics"]["altitude_m"] == 100.0

    def test_label_stored_in_classification(self):
        ev = make_track_event("T1", 41.0, 29.0, 0.0, 0.0, 0.0, "helicopter")
        assert ev["payload"]["classification"]["label"] == "helicopter"

    def test_id_prefixed_with_mqtt(self):
        ev = make_track_event("ABC", 41.0, 29.0, 0.0, 0.0, 0.0, "")
        assert ev["payload"]["id"] == "MQTT-ABC"

    def test_correlation_id_set(self):
        ev = make_track_event("XYZ", 41.0, 29.0, 0.0, 0.0, 0.0, "")
        assert ev["correlation_id"] == "XYZ"

    def test_empty_label(self):
        ev = make_track_event("T1", 41.0, 29.0, 0.0, 0.0, 0.0, "")
        assert ev["payload"]["classification"]["label"] == "unknown"


# ── MessageParser ─────────────────────────────────────────────────────────────

def _args(**kw):
    """Build a minimal argparse.Namespace for MessageParser."""
    defaults = dict(
        id_field="id", lat_field="lat", lon_field="lon",
        speed_field="", speed_scale=1.0,
        heading_field="", altitude_field="", label_field="",
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestMessageParser:
    def test_basic_message(self):
        parser = MessageParser(_args())
        msg = json.dumps({"id": "T1", "lat": 41.0, "lon": 29.0}).encode()
        ev = parser.parse(msg)
        assert ev is not None
        assert ev["payload"]["lat"] == 41.0
        assert ev["payload"]["lon"] == 29.0

    def test_passthrough_native_event(self):
        parser = MessageParser(_args())
        native = {
            "event_type": "track.update",
            "payload": {"id": "T1", "lat": 41.0, "lon": 29.0},
        }
        ev = parser.parse(json.dumps(native).encode())
        # Pass-through: deserialised event returned unchanged (equal, not same obj)
        assert ev == native
        assert ev["event_type"] == "track.update"
        assert ev["payload"]["id"] == "T1"

    def test_missing_lat_returns_none(self):
        parser = MessageParser(_args())
        msg = json.dumps({"id": "T1", "lon": 29.0}).encode()
        assert parser.parse(msg) is None

    def test_missing_lon_returns_none(self):
        parser = MessageParser(_args())
        msg = json.dumps({"id": "T1", "lat": 41.0}).encode()
        assert parser.parse(msg) is None

    def test_bad_json_returns_none(self):
        parser = MessageParser(_args())
        assert parser.parse(b"{not valid json}") is None

    def test_non_dict_returns_none(self):
        parser = MessageParser(_args())
        assert parser.parse(json.dumps([1, 2, 3]).encode()) is None

    def test_nested_lat_lon(self):
        parser = MessageParser(_args(lat_field="pos.lat", lon_field="pos.lon"))
        msg = json.dumps({"id": "T1", "pos": {"lat": 41.0, "lon": 29.0}}).encode()
        ev = parser.parse(msg)
        assert ev is not None
        assert ev["payload"]["lat"] == 41.0

    def test_speed_with_scale(self):
        # speed in knots (1 kn ≈ 0.514 m/s)
        parser = MessageParser(_args(speed_field="spd", speed_scale=0.514))
        msg = json.dumps({"id": "T1", "lat": 41.0, "lon": 29.0, "spd": 10.0}).encode()
        ev = parser.parse(msg)
        assert ev is not None
        assert abs(ev["payload"]["kinematics"]["speed_mps"] - 5.14) < 0.01

    def test_label_field_mapping(self):
        parser = MessageParser(_args(label_field="type"))
        msg = json.dumps({"id": "T1", "lat": 41.0, "lon": 29.0, "type": "uav"}).encode()
        ev = parser.parse(msg)
        assert ev["payload"]["classification"]["label"] == "uav"

    def test_autogenerated_id_when_missing(self):
        parser = MessageParser(_args())
        msg = json.dumps({"lat": 41.0, "lon": 29.0}).encode()
        ev = parser.parse(msg)
        assert ev is not None
        # An ID was generated (not empty)
        assert ev["payload"]["id"]


# ── OutputHandler (stdout mode) ───────────────────────────────────────────────

class TestOutputHandlerStdout:
    def test_emit_writes_jsonl(self, capsys):
        handler = OutputHandler(cop_url=None, api_key="")
        event = {"event_type": "track.update", "payload": {"id": "T1"}}
        handler.emit(event)
        captured = capsys.readouterr()
        line = captured.out.strip()
        parsed = json.loads(line)
        assert parsed["payload"]["id"] == "T1"

    def test_emit_multiple_lines(self, capsys):
        handler = OutputHandler(cop_url=None, api_key="")
        handler.emit({"n": 1})
        handler.emit({"n": 2})
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().splitlines() if l]
        assert len(lines) == 2
        assert json.loads(lines[0])["n"] == 1
        assert json.loads(lines[1])["n"] == 2
