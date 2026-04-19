"""Unit tests for yolo_service — Arrange / Act / Assert."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.detectors.camera.yolo_service import NATSSubject, build_detection_event
from services.schemas.detection import CameraDetectionEvent


# ── helpers ──────────────────────────────────────────────────────

def _mock_result(boxes: list[tuple[float, float, float, float, float, int]]):
    """Ultralytics Results mock with given (x1,y1,x2,y2,conf,cls) boxes."""
    result = MagicMock()
    result.orig_shape = (480, 640)

    mock_boxes = MagicMock()
    mock_boxes.__len__ = lambda _: len(boxes)

    xyxy_rows, conf_rows, cls_rows = [], [], []
    for x1, y1, x2, y2, conf, cls in boxes:
        xyxy_rows.append([x1, y1, x2, y2])
        conf_rows.append(conf)
        cls_rows.append(cls)

    mock_boxes.xyxy.tolist.return_value = xyxy_rows
    mock_boxes.conf.tolist.return_value = conf_rows
    mock_boxes.cls.tolist.return_value = cls_rows
    result.boxes = mock_boxes
    result.names = {0: "drone", 1: "bird"}
    return result


# ── schema tests ──────────────────────────────────────────────────

def test_build_detection_event_empty_frame():
    # Arrange
    result = _mock_result([])

    # Act
    event = build_detection_event(
        result=result,
        sensor_id="cam-01",
        frame_id=0,
        inference_ms=12.3,
    )

    # Assert
    assert isinstance(event, CameraDetectionEvent)
    assert event.sensor_id == "cam-01"
    assert event.detections == []
    assert event.inference_ms == 12.3
    assert event.frame_width == 640
    assert event.frame_height == 480


def test_build_detection_event_with_detections():
    # Arrange
    result = _mock_result([
        (10.0, 20.0, 100.0, 200.0, 0.92, 0),
        (300.0, 150.0, 450.0, 300.0, 0.75, 1),
    ])

    # Act
    event = build_detection_event(
        result=result,
        sensor_id="cam-02",
        frame_id=42,
        inference_ms=8.5,
    )

    # Assert
    assert len(event.detections) == 2
    assert event.detections[0]["class_name"] == "drone"
    assert event.detections[0]["conf"] == pytest.approx(0.92)
    assert event.detections[1]["class_name"] == "bird"
    assert event.frame_id == 42


def test_nats_subject_format():
    # Assert
    assert NATSSubject.camera("cam-01") == "nizam.raw.camera.cam-01"
    assert NATSSubject.camera("edge-node-3") == "nizam.raw.camera.edge-node-3"


# ── integration-style async test ─────────────────────────────────

@pytest.mark.asyncio
async def test_publish_calls_nats_with_correct_subject():
    from services.detectors.camera.yolo_service import publish_event

    # Arrange
    nc = AsyncMock()
    event = build_detection_event(
        result=_mock_result([(0, 0, 50, 50, 0.9, 0)]),
        sensor_id="cam-test",
        frame_id=1,
        inference_ms=5.0,
    )

    # Act
    await publish_event(nc, event)

    # Assert
    nc.publish.assert_awaited_once()
    subject = nc.publish.call_args[0][0]
    assert subject == "nizam.raw.camera.cam-test"
