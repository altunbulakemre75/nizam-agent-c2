from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, Field


class BoundingBox(TypedDict):
    x1: float
    y1: float
    x2: float
    y2: float


class Detection(TypedDict):
    bbox: BoundingBox
    conf: float
    class_id: int
    class_name: str


class CameraDetectionEvent(BaseModel):
    """Kamera sensöründen gelen tek frame tespiti.

    NATS subject: nizam.raw.camera.{sensor_id}
    """

    sensor_id: str
    timestamp_iso: str
    frame_id: int = Field(ge=0)
    detections: list[Detection]
    inference_ms: float = Field(ge=0.0)
    frame_width: int = Field(gt=0)
    frame_height: int = Field(gt=0)
