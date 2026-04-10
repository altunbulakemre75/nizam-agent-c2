"""
tests/test_cot_adapter.py  —  Unit tests for adapters/cot_adapter.py
"""
from __future__ import annotations

import json
import pytest
from adapters.cot_adapter import (
    parse_cot_xml,
    make_track_event,
    build_cot_xml,
    cot_type_to_fields,
    _read_file_source,
    OutputHandler,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

FRIENDLY_AIR = """<event version="2.0" uid="BLUE-HAWK-1" type="a-f-A"
    time="2024-01-01T10:00:00Z" start="2024-01-01T10:00:00Z"
    stale="2024-01-01T10:05:00Z" how="m-g">
  <point lat="41.015" lon="28.979" hae="3000" ce="10" le="15"/>
  <detail>
    <contact callsign="BLUE-HAWK"/>
    <track speed="250" course="90"/>
    <uid Droid="BLUE-HAWK"/>
    <remarks>Patrol Alpha</remarks>
  </detail>
</event>"""

HOSTILE_GROUND = """<event version="2.0" uid="RED-TK-42" type="a-h-G"
    time="2024-01-01T10:00:00Z" start="2024-01-01T10:00:00Z"
    stale="2024-01-01T10:05:00Z" how="h-e">
  <point lat="40.800" lon="29.100" hae="50" ce="200" le="50"/>
  <detail>
    <contact callsign="HOSTILE-42"/>
    <track speed="15" course="270"/>
  </detail>
</event>"""

UNKNOWN_SURFACE = """<event version="2.0" uid="UNKN-SEA-7" type="a-u-S"
    time="2024-01-01T10:00:00Z" start="2024-01-01T10:00:00Z"
    stale="2024-01-01T10:05:00Z" how="m-g">
  <point lat="40.600" lon="29.300" hae="0" ce="50" le="0"/>
  <detail>
    <track speed="8" course="180"/>
  </detail>
</event>"""

JOKER = """<event version="2.0" uid="JKR-001" type="a-j-A"
    time="2024-01-01T10:00:00Z" start="2024-01-01T10:00:00Z"
    stale="2024-01-01T10:05:00Z" how="m-g">
  <point lat="41.100" lon="28.800" hae="5000" ce="30" le="10"/>
  <detail><contact callsign="JOKER-1"/><track speed="400" course="45"/></detail>
</event>"""

MINIMAL_NO_DETAIL = """<event version="2.0" uid="MIN-001" type="a-u-A"
    time="2024-01-01T10:00:00Z" start="2024-01-01T10:00:00Z"
    stale="2024-01-01T10:05:00Z" how="m-g">
  <point lat="41.0" lon="29.0" hae="0" ce="9999999" le="9999999"/>
</event>"""

MALFORMED_XML = "<event><point lat='bad'/></event>"
NOT_AN_EVENT   = "<foo><bar/></foo>"
NO_POINT       = """<event version="2.0" uid="X"><detail/></event>"""


# ── cot_type_to_fields ────────────────────────────────────────────────────────

class TestCotTypeToFields:
    def test_friendly_air(self):
        threat, cls, intent = cot_type_to_fields("a-f-A")
        assert threat == "LOW"
        assert cls    == "aircraft"
        assert intent == "friendly"

    def test_hostile_ground(self):
        threat, cls, intent = cot_type_to_fields("a-h-G")
        assert threat == "HIGH"
        assert cls    == "ground_vehicle"
        assert intent == "attack"

    def test_unknown_surface(self):
        threat, cls, intent = cot_type_to_fields("a-u-S")
        assert threat == "MEDIUM"
        assert cls    == "surface_vessel"
        assert intent == "unknown"

    def test_joker(self):
        threat, cls, intent = cot_type_to_fields("a-j-A")
        assert threat == "HIGH"
        assert intent == "attack"

    def test_neutral_ground(self):
        threat, cls, intent = cot_type_to_fields("a-n-G")
        assert threat == "LOW"
        assert intent == "transit"

    def test_assumed_friend(self):
        threat, cls, intent = cot_type_to_fields("a-a-A")
        assert threat == "LOW"
        assert intent == "friendly"

    def test_unknown_dim_defaults(self):
        threat, cls, intent = cot_type_to_fields("a-u-X")
        assert cls == "unknown"


# ── parse_cot_xml ─────────────────────────────────────────────────────────────

class TestParseCotXml:
    def test_friendly_air_parsed(self):
        p = parse_cot_xml(FRIENDLY_AIR)
        assert p is not None
        assert p["uid"]            == "BLUE-HAWK-1"
        assert p["lat"]            == pytest.approx(41.015)
        assert p["lon"]            == pytest.approx(28.979)
        assert p["altitude_m"]     == pytest.approx(3000.0)
        assert p["speed_mps"]      == pytest.approx(250.0)
        assert p["heading_deg"]    == pytest.approx(90.0)
        assert p["callsign"]       == "BLUE-HAWK"
        assert p["threat_level"]   == "LOW"
        assert p["classification"] == "aircraft"
        assert p["intent"]         == "friendly"
        assert p["note"]           == "Patrol Alpha"

    def test_hostile_ground_parsed(self):
        p = parse_cot_xml(HOSTILE_GROUND)
        assert p is not None
        assert p["threat_level"]   == "HIGH"
        assert p["classification"] == "ground_vehicle"
        assert p["intent"]         == "attack"
        assert p["confidence"]     < 0.8  # CE=200m → lower confidence

    def test_unknown_surface_parsed(self):
        p = parse_cot_xml(UNKNOWN_SURFACE)
        assert p is not None
        assert p["threat_level"]   == "MEDIUM"
        assert p["classification"] == "surface_vessel"
        assert p["callsign"]       == ""   # no contact element

    def test_joker_high_threat(self):
        p = parse_cot_xml(JOKER)
        assert p is not None
        assert p["threat_level"] == "HIGH"

    def test_minimal_no_detail(self):
        p = parse_cot_xml(MINIMAL_NO_DETAIL)
        assert p is not None
        assert p["lat"] == pytest.approx(41.0)
        assert p["speed_mps"]   == 0.0
        assert p["heading_deg"] == 0.0
        assert p["callsign"]    == ""

    def test_malformed_xml_returns_none(self):
        assert parse_cot_xml(MALFORMED_XML) is None

    def test_not_event_tag_returns_none(self):
        assert parse_cot_xml(NOT_AN_EVENT) is None

    def test_no_point_returns_none(self):
        assert parse_cot_xml(NO_POINT) is None

    def test_empty_string_returns_none(self):
        assert parse_cot_xml("") is None

    def test_high_confidence_small_ce(self):
        p = parse_cot_xml(FRIENDLY_AIR)  # CE=10
        assert p["confidence"] == pytest.approx(0.95)

    def test_low_confidence_large_ce(self):
        p = parse_cot_xml(MINIMAL_NO_DETAIL)  # CE=9999999
        assert p["confidence"] < 0.7


# ── make_track_event ──────────────────────────────────────────────────────────

class TestMakeTrackEvent:
    def test_event_structure(self):
        p = parse_cot_xml(FRIENDLY_AIR)
        ev = make_track_event(p, "udp")
        assert ev["event_type"] == "track.update"
        assert ev["source"]["agent_id"] == "cot-adapter"
        assert ev["source"]["instance_id"] == "udp"

    def test_payload_fields(self):
        p  = parse_cot_xml(FRIENDLY_AIR)
        pl = make_track_event(p)["payload"]
        assert pl["id"].startswith("COT-")
        assert pl["lat"]         == pytest.approx(41.015, abs=1e-4)
        assert pl["lon"]         == pytest.approx(28.979, abs=1e-4)
        assert pl["threat_level"] == "LOW"
        assert pl["intent"]      == "friendly"
        assert pl["status"]      == "CONFIRMED"
        assert "cot" in pl["supporting_sensors"]

    def test_hostile_threat_score_high(self):
        p  = parse_cot_xml(HOSTILE_GROUND)
        pl = make_track_event(p)["payload"]
        assert pl["threat_score"] >= 0.8

    def test_callsign_in_classification(self):
        p  = parse_cot_xml(FRIENDLY_AIR)
        pl = make_track_event(p)["payload"]
        assert pl["classification"]["callsign"] == "BLUE-HAWK"

    def test_cot_type_preserved(self):
        p  = parse_cot_xml(HOSTILE_GROUND)
        pl = make_track_event(p)["payload"]
        assert pl["classification"]["cot_type"] == "a-h-G"

    def test_note_preserved(self):
        p  = parse_cot_xml(FRIENDLY_AIR)
        pl = make_track_event(p)["payload"]
        assert pl["note"] == "Patrol Alpha"

    def test_json_serialisable(self):
        p  = parse_cot_xml(FRIENDLY_AIR)
        ev = make_track_event(p)
        json.dumps(ev)   # must not raise


# ── build_cot_xml (round-trip) ────────────────────────────────────────────────

class TestBuildCotXml:
    def test_roundtrip(self):
        parsed  = parse_cot_xml(FRIENDLY_AIR)
        xml_out = build_cot_xml(parsed)
        reparsed = parse_cot_xml(xml_out)
        assert reparsed is not None
        assert reparsed["lat"]          == pytest.approx(parsed["lat"],  abs=1e-4)
        assert reparsed["lon"]          == pytest.approx(parsed["lon"],  abs=1e-4)
        assert reparsed["speed_mps"]    == pytest.approx(parsed["speed_mps"], abs=0.1)
        assert reparsed["heading_deg"]  == pytest.approx(parsed["heading_deg"], abs=0.1)

    def test_hostile_roundtrip(self):
        parsed  = parse_cot_xml(HOSTILE_GROUND)
        xml_out = build_cot_xml(parsed)
        assert "a-h-G" in xml_out
        reparsed = parse_cot_xml(xml_out)
        assert reparsed["threat_level"] == "HIGH"

    def test_xml_escaping(self):
        p = parse_cot_xml(FRIENDLY_AIR)
        p["callsign"] = 'Attack <"exploit">'
        xml = build_cot_xml(p)
        # Must not produce broken XML
        import xml.etree.ElementTree as ET
        ET.fromstring(xml)  # raises if malformed

    def test_valid_xml_structure(self):
        import xml.etree.ElementTree as ET
        p   = parse_cot_xml(UNKNOWN_SURFACE)
        xml = build_cot_xml(p)
        root = ET.fromstring(xml)
        assert root.tag == "event"
        assert root.find("point") is not None


# ── _read_file_source ─────────────────────────────────────────────────────────

class TestReadFileSource:
    def test_single_xml(self, tmp_path):
        f = tmp_path / "evt.xml"
        f.write_text(FRIENDLY_AIR)
        events = _read_file_source(str(f))
        assert len(events) == 1
        assert "<event" in events[0]

    def test_multiple_xml(self, tmp_path):
        f = tmp_path / "multi.xml"
        f.write_text(FRIENDLY_AIR + HOSTILE_GROUND)
        events = _read_file_source(str(f))
        assert len(events) == 2

    def test_jsonl_format(self, tmp_path):
        f = tmp_path / "events.jsonl"
        f.write_text(
            json.dumps({"xml": FRIENDLY_AIR}) + "\n" +
            json.dumps({"xml": HOSTILE_GROUND}) + "\n"
        )
        events = _read_file_source(str(f))
        assert len(events) == 2

    def test_missing_file_returns_empty(self):
        events = _read_file_source("/nonexistent/path/file.xml")
        assert events == []


# ── OutputHandler (stdout) ────────────────────────────────────────────────────

class TestOutputHandler:
    def test_stdout_emit(self, capsys):
        p  = parse_cot_xml(FRIENDLY_AIR)
        ev = make_track_event(p)
        h  = OutputHandler()
        h.emit(ev)
        captured = capsys.readouterr()
        obj = json.loads(captured.out.strip())
        assert obj["event_type"] == "track.update"

    def test_stdout_multiple(self, capsys):
        h = OutputHandler()
        for xml_str in [FRIENDLY_AIR, HOSTILE_GROUND, UNKNOWN_SURFACE]:
            p  = parse_cot_xml(xml_str)
            ev = make_track_event(p)
            h.emit(ev)
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().splitlines() if l]
        assert len(lines) == 3
