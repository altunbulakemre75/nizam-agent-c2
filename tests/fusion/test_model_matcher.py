"""Drone model matcher (FAISS or numpy fallback) tests."""
from __future__ import annotations

from services.fusion.model_matcher import DroneCatalog


def test_catalog_loads_default():
    catalog = DroneCatalog()
    assert len(catalog._entries) >= 3


def test_exact_match_returns_closest():
    catalog = DroneCatalog()
    # DJI Mavic 3 embedding'ini kopyala
    exact = [0.9, 0.1, 0.2, 0.0, 0.8, 0.3, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    result = catalog.query(exact, threshold=0.5)
    assert result is not None
    assert result["model_name"] == "DJI Mavic 3"
    assert result["distance"] < 0.01


def test_far_embedding_rejected_above_threshold():
    catalog = DroneCatalog()
    far = [10.0] * 16
    result = catalog.query(far, threshold=1.0)
    assert result is None


def test_wrong_dim_raises():
    catalog = DroneCatalog()
    import pytest
    with pytest.raises(ValueError, match="Embedding boyutu"):
        catalog.query([1.0, 2.0, 3.0])  # 3-dim yerine 16
