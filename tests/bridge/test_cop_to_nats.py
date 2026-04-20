"""COP → NATS bridge tests."""
from __future__ import annotations

from services.bridge.cop_to_nats import track_to_camera_event


def test_track_mapping_preserves_coords():
    track = {"id": "t1", "lat": 39.9, "lon": 32.8, "alt": 150.0,
             "confidence": 0.85, "intent": "attack", "tick": 5}
    event = track_to_camera_event(track)
    assert event.sensor_id == "cop-sim"
    assert event.frame_id == 5
    assert len(event.detections) == 1
    det = event.detections[0]
    assert det["class_name"] == "attack"
    assert det["conf"] == 0.85


def test_track_defaults_when_fields_missing():
    track = {"id": "x"}
    event = track_to_camera_event(track)
    assert event.detections[0]["class_name"] == "drone"
    assert event.frame_id == 0
