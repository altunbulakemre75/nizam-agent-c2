"""ASTM F3411-22 Remote ID parser (pure Python, no ctypes).

Reference: vendor/12-sdr-rf/opendroneid-core-c/libopendroneid/opendroneid.h
"""
from __future__ import annotations

import struct

from services.schemas.rf import (
    ODIDBasicID,
    ODIDIDType,
    ODIDLocation,
    ODIDMessageType,
    ODIDUAType,
)

ODID_MESSAGE_SIZE = 25
ODID_ID_SIZE = 20
INVALID_ALT = -1000.0


def parse_message_header(byte0: int) -> tuple[ODIDMessageType, int]:
    """Üst nibble mesaj tipi, alt nibble protokol sürümü."""
    msg_type = ODIDMessageType(byte0 >> 4)
    protocol_version = byte0 & 0x0F
    return msg_type, protocol_version


def parse_basic_id(payload: bytes) -> ODIDBasicID:
    """Basic ID mesaj payload'ı (24 bayt, header hariç)."""
    if len(payload) < 2 + ODID_ID_SIZE:
        raise ValueError(f"Basic ID payload çok kısa: {len(payload)}")
    id_type = ODIDIDType(payload[0] >> 4)
    ua_type = ODIDUAType(payload[0] & 0x0F)
    uas_id_bytes = payload[1 : 1 + ODID_ID_SIZE]
    uas_id = uas_id_bytes.rstrip(b"\x00").decode("ascii", errors="replace")
    return ODIDBasicID(id_type=id_type, ua_type=ua_type, uas_id=uas_id)


def parse_location(payload: bytes) -> ODIDLocation:
    """Location mesaj payload'ı (24 bayt, header hariç).

    Layout (offset bytes):
      0  : Status(4) | HeightType(1) | EW(1) | SpeedMult(2)
      1  : Track direction (0..180 or 180..360 with EW flag)
      2  : Horizontal speed (scaled)
      3  : Vertical speed (int8)
      4-7: Latitude (int32 LE, 1e-7 deg)
      8-11: Longitude (int32 LE, 1e-7 deg)
      12-13: Baro altitude (uint16 LE, 0.5m steps, +1000m offset)
      14-15: Geo altitude (uint16 LE, same)
      16-17: Height AGL (uint16 LE, same)
      18 : Horiz accuracy | Vert accuracy
      19 : Baro accuracy | Speed accuracy
      20-21: Timestamp (uint16 LE, 0.1s after hour)
      22 : Timestamp accuracy (nibble)
      23 : Reserved
    """
    if len(payload) < 24:
        raise ValueError(f"Location payload çok kısa: {len(payload)}")

    flags = payload[0]
    ew_direction = (flags >> 1) & 0x01
    speed_mult = flags & 0x01

    track = payload[1]
    heading = float(track + 180) if ew_direction else float(track)
    if heading >= 361:
        heading_out: float | None = None
    else:
        heading_out = heading

    h_speed_raw = payload[2]
    h_speed = (h_speed_raw * 0.75) if speed_mult else (h_speed_raw * 0.25)
    h_speed_out: float | None = None if h_speed_raw == 255 else h_speed

    v_speed_raw = struct.unpack_from("<b", payload, 3)[0]
    v_speed_out: float | None = None if v_speed_raw == 63 else float(v_speed_raw) * 0.5

    lat_raw = struct.unpack_from("<i", payload, 4)[0]
    lon_raw = struct.unpack_from("<i", payload, 8)[0]
    latitude = lat_raw * 1e-7
    longitude = lon_raw * 1e-7

    alt_baro_raw = struct.unpack_from("<H", payload, 12)[0]
    alt_geo_raw = struct.unpack_from("<H", payload, 14)[0]
    height_agl_raw = struct.unpack_from("<H", payload, 16)[0]

    def _decode_alt(raw: int) -> float | None:
        if raw == 0:
            return INVALID_ALT
        val = raw * 0.5 - 1000.0
        return None if val <= INVALID_ALT else val

    ts_raw = struct.unpack_from("<H", payload, 20)[0]
    timestamp_out: float | None = None if ts_raw == 0xFFFF else ts_raw * 0.1

    return ODIDLocation(
        latitude=latitude,
        longitude=longitude,
        altitude_baro_m=_decode_alt(alt_baro_raw),
        altitude_geo_m=_decode_alt(alt_geo_raw),
        height_agl_m=_decode_alt(height_agl_raw),
        speed_horizontal_mps=h_speed_out,
        speed_vertical_mps=v_speed_out,
        heading_deg=heading_out,
        timestamp_sec_after_hour=timestamp_out,
    )


def parse_message(message: bytes) -> tuple[ODIDMessageType, ODIDBasicID | ODIDLocation | None]:
    """Tek bir 25-bayt ODID mesajını ayrıştır."""
    if len(message) < ODID_MESSAGE_SIZE:
        raise ValueError(f"Mesaj çok kısa: {len(message)} < {ODID_MESSAGE_SIZE}")

    msg_type, _protocol = parse_message_header(message[0])
    payload = message[1:]

    if msg_type == ODIDMessageType.BASIC_ID:
        return msg_type, parse_basic_id(payload)
    if msg_type == ODIDMessageType.LOCATION:
        return msg_type, parse_location(payload)
    return msg_type, None


def build_basic_id_message(
    id_type: ODIDIDType, ua_type: ODIDUAType, uas_id: str
) -> bytes:
    """Test fixture üretici — gerçek parser'ı doğrulamak için."""
    header = (ODIDMessageType.BASIC_ID << 4) | 0x2  # protocol v2
    flags = (int(id_type) << 4) | int(ua_type)
    id_bytes = uas_id.encode("ascii")[:ODID_ID_SIZE].ljust(ODID_ID_SIZE, b"\x00")
    payload = bytes([flags]) + id_bytes
    reserved = b"\x00" * (24 - len(payload))
    return bytes([header]) + payload + reserved


def build_location_message(
    latitude: float,
    longitude: float,
    altitude_geo_m: float = 100.0,
    heading_deg: float = 0.0,
    h_speed_mps: float = 0.0,
) -> bytes:
    """Test fixture üretici — Location message."""
    header = (ODIDMessageType.LOCATION << 4) | 0x2
    flags = 0x00  # status=0, EW=0, speedMult=0
    if heading_deg >= 180:
        flags |= 0x02
        track = int(heading_deg - 180) & 0xFF
    else:
        track = int(heading_deg) & 0xFF

    h_speed_raw = min(int(h_speed_mps * 4), 254)  # 0.25 m/s steps
    v_speed_raw = 0
    lat_raw = int(latitude * 1e7)
    lon_raw = int(longitude * 1e7)
    alt_geo_raw = int((altitude_geo_m + 1000.0) * 2)

    payload = (
        bytes([flags, track, h_speed_raw])
        + struct.pack("<b", v_speed_raw)
        + struct.pack("<i", lat_raw)
        + struct.pack("<i", lon_raw)
        + struct.pack("<H", 0)           # baro
        + struct.pack("<H", alt_geo_raw)
        + struct.pack("<H", 0)           # height AGL
        + bytes([0, 0])                   # accuracies
        + struct.pack("<H", 0)           # timestamp
        + bytes([0, 0])                   # ts accuracy + reserved
    )
    return bytes([header]) + payload
