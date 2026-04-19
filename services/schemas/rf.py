"""RF sensör event şemaları — OpenDroneID (ASTM F3411) ve WiFi OUI."""
from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, Field


class ODIDMessageType(IntEnum):
    BASIC_ID = 0x0
    LOCATION = 0x1
    AUTH = 0x2
    SELF_ID = 0x3
    SYSTEM = 0x4
    OPERATOR_ID = 0x5


class ODIDIDType(IntEnum):
    NONE = 0
    SERIAL_NUMBER = 1
    CAA_REGISTRATION = 2
    UTM_ASSIGNED = 3
    SPECIFIC_SESSION = 4


class ODIDUAType(IntEnum):
    NONE = 0
    AEROPLANE = 1
    HELICOPTER_MULTIROTOR = 2
    GYROPLANE = 3
    VTOL = 4
    ORNITHOPTER = 5
    GLIDER = 6
    KITE = 7
    FREE_BALLOON = 8
    CAPTIVE_BALLOON = 9
    AIRSHIP = 10
    FREE_FALL_PARACHUTE = 11
    ROCKET = 12
    TETHERED_POWERED = 13
    GROUND_OBSTACLE = 14
    OTHER = 15


class ODIDBasicID(BaseModel):
    """ASTM F3411-22 Basic ID message (0x0)."""
    id_type: ODIDIDType
    ua_type: ODIDUAType
    uas_id: str = Field(max_length=20)


class ODIDLocation(BaseModel):
    """ASTM F3411-22 Location message (0x1)."""
    latitude: float  # degrees
    longitude: float  # degrees
    altitude_baro_m: float | None = None   # meters MSL (baro), -1000 = invalid
    altitude_geo_m: float | None = None    # meters WGS84 HAE, -1000 = invalid
    height_agl_m: float | None = None      # meters above takeoff, -1000 = invalid
    speed_horizontal_mps: float | None = None
    speed_vertical_mps: float | None = None
    heading_deg: float | None = None       # 0..360
    timestamp_sec_after_hour: float | None = None


class ODIDEvent(BaseModel):
    """Tek bir ODID paketi — bir veya daha fazla mesaj içerir.

    NATS subject: nizam.raw.rf.odid.{sensor_id}
    """
    sensor_id: str
    timestamp_iso: str
    source: str = Field(description="bluetooth-legacy | bluetooth-le | wifi-nan | wifi-beacon")
    rssi_dbm: float | None = None
    basic_id: ODIDBasicID | None = None
    location: ODIDLocation | None = None


class WiFiOUIEvent(BaseModel):
    """WiFi OUI tabanlı drone üreticisi tespiti.

    NATS subject: nizam.raw.rf.wifi.{sensor_id}
    """
    sensor_id: str
    timestamp_iso: str
    mac: str
    oui: str
    vendor: str
    ssid: str | None = None
    rssi_dbm: float | None = None
    channel: int | None = None
