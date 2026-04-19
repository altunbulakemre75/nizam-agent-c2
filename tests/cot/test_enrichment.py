"""CoT enrichment tests."""
from __future__ import annotations

from services.cot.cot_builder import COT_TYPE_HOSTILE_UAV, build_cot_event
from services.cot.enrichment import enrich_event, enrich_with_icon, enrich_with_model


def _event():
    return build_cot_event(
        uid="t1", cot_type=COT_TYPE_HOSTILE_UAV,
        latitude=39.9, longitude=32.8, altitude_hae_m=100.0,
    )


def test_enrich_with_model_updates_callsign():
    ev = _event()
    enrich_with_model(ev, "Mavic 3", "DJI")
    contact = ev.find("detail/contact")
    assert contact is not None
    assert contact.attrib["callsign"] == "DJI Mavic 3"


def test_enrich_adds_usericon_for_hostile():
    ev = _event()
    enrich_with_icon(ev)
    icon = ev.find("detail/usericon")
    assert icon is not None
    assert "b-m-p-s-m" in icon.attrib["iconsetpath"]  # hostile


def test_enrich_event_pipeline_with_model():
    ev = _event()
    enriched = enrich_event(ev, model_match={"model_name": "Phantom 4", "manufacturer": "DJI"})
    callsign = enriched.find("detail/contact")
    icon = enriched.find("detail/usericon")
    assert callsign is not None and callsign.attrib["callsign"] == "DJI Phantom 4"
    assert icon is not None


def test_enrich_without_model_still_adds_icon():
    ev = _event()
    enrich_event(ev, model_match=None)
    assert ev.find("detail/usericon") is not None
