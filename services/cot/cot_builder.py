"""MIL-STD-2525 / CoT (Cursor on Target) XML builder.

Saf fonksiyonlar — hiçbir yan etki yok. Test edilebilir.

Reference şablon: vendor/10-tak/adsbxcot/adsbxcot/functions.py
ATAK CoT XSD: vendor/10-tak/AndroidTacticalAssaultKit-CIV/takcot/xsd/
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

# CoT affiliation+dimension+entity prefixes
COT_TYPE_HOSTILE_UAV = "a-h-A-M-F-U"      # hostile, air, military, fixed-wing, UAV
COT_TYPE_UNKNOWN_UAV = "a-u-A-M-F-U"      # unknown, same
COT_TYPE_NEUTRAL_UAV = "a-n-A-M-F-U"      # neutral (test / friendly drone)

DEFAULT_STALE_SEC = 30
DEFAULT_HOST = "nizam.cop"


def _iso_now(clock_now: datetime | None = None) -> str:
    now = clock_now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_offset(clock_now: datetime, seconds: float) -> str:
    return (clock_now + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def build_cot_event(
    uid: str,
    cot_type: str,
    latitude: float,
    longitude: float,
    altitude_hae_m: float = 0.0,
    course_deg: float | None = None,
    speed_mps: float | None = None,
    callsign: str | None = None,
    remarks: str | None = None,
    stale_sec: int = DEFAULT_STALE_SEC,
    host: str = DEFAULT_HOST,
    clock_now: datetime | None = None,
    hae_error_m: float = 9999999.0,
    ce_error_m: float = 9999999.0,
    le_error_m: float = 9999999.0,
) -> ET.Element:
    """Tek bir CoT event XML elementi oluştur.

    CoT spec (MITRE):
      <event version="2.0" uid=... type=... time=... start=... stale=... how=...>
        <point lat=... lon=... hae=... ce=... le=... />
        <detail> <contact callsign=... /> <track course=... speed=... /> <remarks>...</remarks> </detail>
      </event>
    """
    now = clock_now or datetime.now(timezone.utc)
    event = ET.Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "time": _iso_now(now),
            "start": _iso_now(now),
            "stale": _iso_offset(now, stale_sec),
            "how": "m-g",  # machine, GPS-derived
        },
    )
    ET.SubElement(
        event,
        "point",
        {
            "lat": f"{latitude:.7f}",
            "lon": f"{longitude:.7f}",
            "hae": f"{altitude_hae_m:.1f}",
            "ce": f"{ce_error_m:.1f}",
            "le": f"{le_error_m:.1f}",
        },
    )
    detail = ET.SubElement(event, "detail")
    if callsign:
        ET.SubElement(detail, "contact", {"callsign": callsign})
    if course_deg is not None or speed_mps is not None:
        track_attrs: dict[str, str] = {}
        if course_deg is not None:
            track_attrs["course"] = f"{course_deg:.2f}"
        if speed_mps is not None:
            track_attrs["speed"] = f"{speed_mps:.2f}"
        ET.SubElement(detail, "track", track_attrs)
    if remarks:
        remarks_el = ET.SubElement(detail, "remarks", {"source": host})
        remarks_el.text = remarks
    _ = hae_error_m  # reserved for future use
    return event


def serialize(event: ET.Element) -> bytes:
    """CoT event'i wire-format (XML) olarak serialize et."""
    return ET.tostring(event, encoding="utf-8", xml_declaration=False)
