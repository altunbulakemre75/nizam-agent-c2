"""
tests/conftest.py — Shared fixtures for NIZAM COP test suite
"""
import math
import pytest


# ── Sample track data ─────────────────────────────────────────────────────

@pytest.fixture
def istanbul_origin():
    """Reference point: Istanbul center."""
    return {"lat": 41.015, "lon": 28.979}


@pytest.fixture
def sample_track(istanbul_origin):
    """A single hostile drone track."""
    return {
        "id": "T-001",
        "track_id": "T-001",
        "lat": 41.020,
        "lon": 28.985,
        "alt": 150.0,
        "altitude": 150.0,
        "speed": 35.0,
        "heading": 180.0,
        "classification": {"label": "drone", "confidence": 0.85},
        "intent": "attack",
        "intent_conf": 0.9,
        "supporting_sensors": ["radar-01", "eo-01"],
        "kinematics": {
            "speed_mps": 35.0,
            "heading_deg": 180.0,
            "radial_velocity_mps": -20.0,
            "range_m": 1200.0,
        },
        "threat_level": "HIGH",
    }


@pytest.fixture
def sample_tracks():
    """Multiple tracks for multi-track tests."""
    return {
        "T-001": {
            "id": "T-001", "lat": 41.020, "lon": 28.985,
            "speed": 35.0, "heading": 180.0,
            "classification": {"label": "drone"},
            "intent": "attack", "threat_level": "HIGH",
            "kinematics": {"speed_mps": 35.0, "heading_deg": 180.0},
        },
        "T-002": {
            "id": "T-002", "lat": 41.018, "lon": 28.990,
            "speed": 25.0, "heading": 220.0,
            "classification": {"label": "drone"},
            "intent": "reconnaissance", "threat_level": "MEDIUM",
            "kinematics": {"speed_mps": 25.0, "heading_deg": 220.0},
        },
        "T-003": {
            "id": "T-003", "lat": 41.025, "lon": 28.975,
            "speed": 10.0, "heading": 90.0,
            "classification": {"label": "helicopter"},
            "intent": "loitering", "threat_level": "LOW",
            "kinematics": {"speed_mps": 10.0, "heading_deg": 90.0},
        },
    }


# ── Sample threat data ────────────────────────────────────────────────────

@pytest.fixture
def sample_threats():
    """Threat assessments keyed by track_id."""
    return {
        "T-001": {
            "track_id": "T-001",
            "threat_level": "HIGH",
            "score": 85,
            "intent": "attack",
        },
        "T-002": {
            "track_id": "T-002",
            "threat_level": "MEDIUM",
            "score": 55,
            "intent": "reconnaissance",
        },
        "T-003": {
            "track_id": "T-003",
            "threat_level": "LOW",
            "score": 15,
            "intent": "loitering",
        },
    }


# ── Sample zones ──────────────────────────────────────────────────────────

@pytest.fixture
def sample_zones():
    """Zone polygons: restricted and kill zone."""
    return {
        "zone-restricted": {
            "id": "zone-restricted",
            "name": "Alpha Restricted",
            "type": "restricted",
            "coordinates": [
                [41.010, 28.970],
                [41.010, 28.990],
                [41.020, 28.990],
                [41.020, 28.970],
            ],
        },
        "zone-kill": {
            "id": "zone-kill",
            "name": "Bravo Kill Zone",
            "type": "kill",
            "coordinates": [
                [41.025, 28.980],
                [41.025, 28.990],
                [41.030, 28.990],
                [41.030, 28.980],
            ],
        },
        "zone-friendly": {
            "id": "zone-friendly",
            "name": "HQ Safe Zone",
            "type": "friendly",
            "coordinates": [
                [41.000, 28.960],
                [41.000, 28.970],
                [41.005, 28.970],
                [41.005, 28.960],
            ],
        },
    }


# ── Sample assets ─────────────────────────────────────────────────────────

@pytest.fixture
def sample_assets():
    """Friendly and other assets."""
    return {
        "asset-hq": {
            "id": "asset-hq",
            "name": "Komuta Merkezi",
            "type": "friendly",
            "status": "active",
            "lat": 41.015,
            "lon": 28.979,
        },
        "asset-sam": {
            "id": "asset-sam",
            "name": "SAM Battery",
            "type": "friendly",
            "status": "active",
            "lat": 41.012,
            "lon": 28.982,
        },
    }


# ── Helper to offset lat/lon by meters ────────────────────────────────────

@pytest.fixture
def offset_m():
    """Return a function that offsets (lat,lon) by (north_m, east_m)."""
    DEG_TO_M = 111_320.0
    def _offset(lat, lon, north_m, east_m):
        new_lat = lat + north_m / DEG_TO_M
        new_lon = lon + east_m / (DEG_TO_M * math.cos(math.radians(lat)))
        return new_lat, new_lon
    return _offset
