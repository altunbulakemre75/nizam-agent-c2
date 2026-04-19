"""Track ve Measurement şemaları — füzyon katmanı için."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TrackState(str, Enum):
    TENTATIVE = "tentative"   # yeni tespit, henüz doğrulanmamış
    CONFIRMED = "confirmed"   # N ardışık tespitle doğrulandı
    LOST = "lost"             # M ardışık kayıp (ama silinmedi)
    DELETED = "deleted"       # sistem dışı


class SensorType(str, Enum):
    CAMERA = "camera"
    RF_ODID = "rf_odid"
    RF_WIFI = "rf_wifi"
    RADAR = "radar"
    AIS = "ais"


class Measurement(BaseModel):
    """Tek bir sensör ölçümü — füzyona girdi olur."""
    sensor_id: str
    sensor_type: SensorType
    timestamp_iso: str
    # 3D konum (ENU metre) veya lat/lon/alt — sensore göre değişir
    x: float
    y: float
    z: float = 0.0
    # Ölçüm gürültüsü (metre cinsinden 1-sigma)
    sigma_x: float = 5.0
    sigma_y: float = 5.0
    sigma_z: float = 10.0
    # Opsiyonel meta
    class_name: str | None = None
    class_conf: float | None = None
    uas_id: str | None = None  # ODID varsa
    rssi_dbm: float | None = None


class Track(BaseModel):
    """Füzyon motorunun ürettiği birleşik track.

    NATS subject: nizam.tracks.active
    """
    track_id: str
    state: TrackState
    # Kalman state: [x, y, z, vx, vy, vz] (ENU metre, m/s)
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    # Kovaryans köşegeni (konum 1-sigma, metre)
    sigma_x: float
    sigma_y: float
    sigma_z: float
    # Meta
    last_update_iso: str
    hits: int = Field(ge=0, description="Toplam tespit sayısı")
    misses: int = Field(ge=0, description="Ardışık kaçırılan tick")
    sources: list[SensorType] = Field(default_factory=list)
    uas_id: str | None = None
    class_name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
