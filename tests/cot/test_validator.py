"""CoT validator tests."""
from __future__ import annotations

import pytest
from xml.etree import ElementTree as ET

from services.cot.cot_builder import build_cot_event
from services.cot.validator import CoTValidationError, validate_structure


def _valid_event():
    return build_cot_event(
        uid="NIZAM.t1", cot_type="a-h-A-M-F-U",
        latitude=39.9, longitude=32.8, altitude_hae_m=100.0,
    )


def test_valid_event_passes():
    validate_structure(_valid_event())


def test_missing_version_fails():
    ev = ET.Element("event", {"uid": "x", "type": "a", "time": "t", "start": "t", "stale": "t"})
    ET.SubElement(ev, "point", {"lat": "0", "lon": "0", "hae": "0"})
    with pytest.raises(CoTValidationError, match="zorunlu attrib"):
        validate_structure(ev)


def test_missing_point_fails():
    ev = ET.Element("event", {
        "version": "2.0", "uid": "x", "type": "a",
        "time": "t", "start": "t", "stale": "t",
    })
    with pytest.raises(CoTValidationError, match="<point> eksik"):
        validate_structure(ev)


def test_non_numeric_lat_fails():
    ev = _valid_event()
    ev.find("point").set("lat", "not-a-number")
    with pytest.raises(CoTValidationError, match="sayı olmalı"):
        validate_structure(ev)


def test_wrong_root_tag_fails():
    ev = ET.Element("not-event")
    with pytest.raises(CoTValidationError, match="Root 'event'"):
        validate_structure(ev)


def test_wrong_version_fails():
    ev = _valid_event()
    ev.set("version", "1.0")
    with pytest.raises(CoTValidationError, match="version 2.0"):
        validate_structure(ev)
