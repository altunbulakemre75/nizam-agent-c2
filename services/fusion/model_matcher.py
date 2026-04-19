"""Drone modeli tanımlama — in-memory vector lookup.

Gerçek FAISS opsiyonel: yüksek hacimde (10k+ drone model) FAISS hızlı.
Düşük hacimde basit numpy nearest neighbor yeterli ve FAISS kurulum
gerektirmez. Bu yüzden iki path:
  1. FAISS varsa → IndexFlatL2
  2. Yoksa → numpy argmin

Veri: services/fusion/drone_catalog.json — {model_name, manufacturer, embedding[16]}
Üretimde: YOLO feature extractor çıktısı veya ODID manufacturer+UA_type
string'inin TF-IDF hash'i embedding olarak kullanılabilir.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

CATALOG_PATH = Path(__file__).parent / "drone_catalog.json"


class DroneCatalog:
    """Drone modeli lookup — hem FAISS hem numpy fallback."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        path = catalog_path or CATALOG_PATH
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = []
        self._entries = data
        if not data:
            self._vectors = np.zeros((0, 16), dtype=np.float32)
        else:
            self._vectors = np.array([e["embedding"] for e in data], dtype=np.float32)
        self._index = self._build_index()

    def _build_index(self):
        if len(self._entries) == 0:
            return None
        try:
            import faiss  # noqa: PLC0415

            d = self._vectors.shape[1]
            idx = faiss.IndexFlatL2(d)
            idx.add(self._vectors)
            return idx
        except ImportError:
            return None  # numpy fallback

    def query(self, embedding: list[float] | np.ndarray, threshold: float = 0.5) -> dict | None:
        """En yakın drone modelini ara.

        Args:
            embedding: sorgu vektörü (16-dim)
            threshold: maksimum L2 mesafe (0..inf, küçük iyi)

        Returns:
            {model_name, manufacturer, distance} veya None
        """
        if len(self._entries) == 0:
            return None
        vec = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
        if vec.shape[1] != self._vectors.shape[1]:
            raise ValueError(
                f"Embedding boyutu uyuşmuyor: {vec.shape[1]} vs {self._vectors.shape[1]}"
            )

        if self._index is not None:
            distances, indices = self._index.search(vec, 1)
            best_idx = int(indices[0, 0])
            dist = float(distances[0, 0])
        else:
            dists_sq = np.sum((self._vectors - vec) ** 2, axis=1)
            best_idx = int(np.argmin(dists_sq))
            dist = float(np.sqrt(dists_sq[best_idx]))

        if dist > threshold:
            return None

        entry = self._entries[best_idx]
        return {
            "model_name": entry["model_name"],
            "manufacturer": entry["manufacturer"],
            "distance": dist,
        }
