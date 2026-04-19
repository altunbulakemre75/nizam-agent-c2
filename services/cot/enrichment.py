"""CoT enrichment — callsign + icon zenginleştirme.

"Unknown UAV-042" → "DJI Mavic 3 Enterprise" gibi dönüşümler.
FAISS drone model lookup ve threat level → icon eşlemesi.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from services.cot.cot_builder import (
    COT_TYPE_HOSTILE_UAV,
    COT_TYPE_NEUTRAL_UAV,
    COT_TYPE_UNKNOWN_UAV,
)

# CoT type → ATAK default icon
ICON_MAPPING = {
    COT_TYPE_HOSTILE_UAV: "COT_MAPPING_SPOTMAP/b-m-p-s-m",    # red
    COT_TYPE_UNKNOWN_UAV: "COT_MAPPING_SPOTMAP/b-m-p-s-u",    # yellow
    COT_TYPE_NEUTRAL_UAV: "COT_MAPPING_SPOTMAP/b-m-p-s-n",    # green
}


def enrich_with_model(event: ET.Element, model_name: str, manufacturer: str) -> None:
    """Callsign'ı drone model adı ile güncelle."""
    contact = event.find("detail/contact")
    if contact is None:
        detail = event.find("detail")
        if detail is None:
            detail = ET.SubElement(event, "detail")
        contact = ET.SubElement(detail, "contact")
    contact.set("callsign", f"{manufacturer} {model_name}")


def enrich_with_icon(event: ET.Element) -> None:
    """CoT type'a göre ATAK icon'u ekle."""
    cot_type = event.attrib.get("type", "")
    icon_path = ICON_MAPPING.get(cot_type)
    if icon_path is None:
        return
    detail = event.find("detail")
    if detail is None:
        detail = ET.SubElement(event, "detail")
    existing = detail.find("usericon")
    if existing is None:
        ET.SubElement(detail, "usericon", {"iconsetpath": icon_path})
    else:
        existing.set("iconsetpath", icon_path)


def enrich_event(
    event: ET.Element, model_match: dict | None = None
) -> ET.Element:
    """Pipeline: model adı + icon ekle."""
    enrich_with_icon(event)
    if model_match:
        enrich_with_model(event, model_match["model_name"], model_match["manufacturer"])
    return event
