"""CoT event XSD doğrulayıcı — ATAK-CIV şemasıyla uyumlu.

Şema kaynağı: vendor/10-tak/AndroidTacticalAssaultKit-CIV/takcot/xsd/
Henüz lokale kopyalanmadıysa basic yapısal doğrulama yapar:
  - <event> root
  - version="2.0"
  - zorunlu attrib'ler: uid, type, time, start, stale
  - <point> mevcut, lat/lon/hae numerik
  - <detail> opsiyonel
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

# Zorunlu event attributeleri
REQUIRED_EVENT_ATTRS = {"version", "uid", "type", "time", "start", "stale"}
REQUIRED_POINT_ATTRS = {"lat", "lon", "hae"}


class CoTValidationError(Exception):
    """CoT mesajı şemaya uymuyor."""


def validate_structure(event: ET.Element) -> None:
    """Temel yapısal doğrulama — XSD'siz.

    Raises CoTValidationError her eksik/yanlış alanda.
    """
    if event.tag != "event":
        raise CoTValidationError(f"Root 'event' olmalı, '{event.tag}' bulundu")

    missing = REQUIRED_EVENT_ATTRS - set(event.attrib.keys())
    if missing:
        raise CoTValidationError(f"event zorunlu attrib eksik: {missing}")

    if event.attrib["version"] != "2.0":
        raise CoTValidationError(f"CoT version 2.0 olmalı, {event.attrib['version']} bulundu")

    point = event.find("point")
    if point is None:
        raise CoTValidationError("event içinde <point> eksik")

    missing_point = REQUIRED_POINT_ATTRS - set(point.attrib.keys())
    if missing_point:
        raise CoTValidationError(f"point zorunlu attrib eksik: {missing_point}")

    for attr in ("lat", "lon", "hae"):
        try:
            float(point.attrib[attr])
        except ValueError as exc:
            raise CoTValidationError(f"point.{attr} sayı olmalı: {point.attrib[attr]}") from exc


def validate_xsd(event: ET.Element, xsd_path: Path) -> None:
    """lxml varsa tam XSD doğrulaması yap, yoksa yapısal fallback."""
    try:
        from lxml import etree  # noqa: PLC0415
    except ImportError:
        validate_structure(event)
        return

    schema_doc = etree.parse(str(xsd_path))
    schema = etree.XMLSchema(schema_doc)
    xml_bytes = ET.tostring(event)
    parsed = etree.fromstring(xml_bytes)
    if not schema.validate(parsed):
        errors = "; ".join(str(e) for e in schema.error_log)
        raise CoTValidationError(f"XSD validation failed: {errors}")


def find_atak_xsd() -> Path | None:
    """vendor/10-tak/AndroidTacticalAssaultKit-CIV/takcot/xsd/ altındaki ana şemayı bul."""
    candidates = [
        Path("vendor/10-tak/AndroidTacticalAssaultKit-CIV/takcot/xsd/EventCot.xsd"),
        Path("vendor/10-tak/AndroidTacticalAssaultKit-CIV-main/takcot/xsd/EventCot.xsd"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None
