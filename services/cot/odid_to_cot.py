"""ODID (Remote ID) event → CoT dönüşümü.

Remote ID zaten drone'un kendi GPS'ini yayınladığı için lat/lon doğrudan
kullanılır. Füzyon gecikmesine gerek yok — direct-to-TAK kısa yol.
"""
from __future__ import annotations

from datetime import datetime
from xml.etree import ElementTree as ET

from services.cot.cot_builder import COT_TYPE_UNKNOWN_UAV, build_cot_event


def odid_event_to_cot(event: dict, clock_now: datetime | None = None) -> ET.Element | None:
    """ODIDEvent.model_dump() → CoT event. Location yoksa None döner."""
    loc = event.get("location")
    if loc is None:
        return None

    basic = event.get("basic_id") or {}
    uas_id = basic.get("uas_id") or "unknown"
    uid = f"NIZAM.ODID.{uas_id}"

    ua_type_name = basic.get("ua_type") or "UAV"
    remarks_parts = [f"sensor={event['sensor_id']}", f"src={event['source']}", f"ua_type={ua_type_name}"]
    if event.get("rssi_dbm") is not None:
        remarks_parts.append(f"rssi={event['rssi_dbm']:.0f}dBm")

    alt = loc.get("altitude_geo_m") or loc.get("altitude_baro_m") or 0.0
    return build_cot_event(
        uid=uid,
        cot_type=COT_TYPE_UNKNOWN_UAV,
        latitude=float(loc["latitude"]),
        longitude=float(loc["longitude"]),
        altitude_hae_m=float(alt),
        course_deg=loc.get("heading_deg"),
        speed_mps=loc.get("speed_horizontal_mps"),
        callsign=f"ODID-{uas_id[:10]}",
        remarks=" ".join(remarks_parts),
        stale_sec=30,
        clock_now=clock_now,
    )
