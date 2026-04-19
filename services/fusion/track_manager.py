"""Track yaşam döngüsü yöneticisi.

Lifecycle:
    tentative ──(N_CONFIRM hit)──► confirmed
        │                              │
        └─(M_DELETE miss)─► deleted    └─(M_LOST miss)─► lost
                                                          │
                                                          └─(K_DELETE miss)─► deleted

Her tick:
    1. predict() — tüm filtreleri ilerlet
    2. associate() — ölçümlerle eşleştir
    3. update() — eşleşenleri güncelle, hit/miss sayaçlarını ayarla
    4. spawn() — eşleşmeyen ölçümlerden tentative track yarat
    5. reap() — silinecek track'leri kaldır
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np
from filterpy.kalman import KalmanFilter
from shared.clock import get_clock

from services.fusion import kf_engine
from services.fusion.association import associate
from services.schemas.track import Measurement, SensorType, Track, TrackState

# Varsayılan lifecycle parametreleri
N_CONFIRM = 3       # 3 ardışık hit → confirmed
M_LOST = 3          # 3 ardışık miss → lost
K_DELETE = 10       # lost'tan sonra 10 miss → deleted


@dataclass
class _TrackRec:
    """İç track kaydı — dışa Track pydantic modeli olarak expose edilir."""
    track_id: str
    kf: KalmanFilter
    state: TrackState
    hits: int = 0
    misses: int = 0
    sources: set[SensorType] = field(default_factory=set)
    uas_id: str | None = None
    class_name: str | None = None
    last_update_iso: str = ""

    def to_pydantic(self) -> Track:
        x = self.kf.x
        P = self.kf.P
        return Track(
            track_id=self.track_id,
            state=self.state,
            x=float(x[0]), y=float(x[1]), z=float(x[2]),
            vx=float(x[3]), vy=float(x[4]), vz=float(x[5]),
            sigma_x=float(np.sqrt(max(P[0, 0], 0.0))),
            sigma_y=float(np.sqrt(max(P[1, 1], 0.0))),
            sigma_z=float(np.sqrt(max(P[2, 2], 0.0))),
            last_update_iso=self.last_update_iso,
            hits=self.hits,
            misses=self.misses,
            sources=sorted(self.sources, key=lambda s: s.value),
            uas_id=self.uas_id,
            class_name=self.class_name,
            confidence=min(1.0, self.hits / float(N_CONFIRM * 2)),
        )


class TrackManager:
    """Track yaşam döngüsü yöneticisi."""

    def __init__(
        self,
        n_confirm: int = N_CONFIRM,
        m_lost: int = M_LOST,
        k_delete: int = K_DELETE,
    ) -> None:
        self._tracks: dict[str, _TrackRec] = {}
        self.n_confirm = n_confirm
        self.m_lost = m_lost
        self.k_delete = k_delete

    # ── Public API ───────────────────────────────────────────────

    def step(self, measurements: list[Measurement], dt: float) -> list[Track]:
        """Tek tick — tahmin, ilişkilendir, güncelle, spawn, reap.

        Returns: mevcut (TENTATIVE/CONFIRMED/LOST) track listesi
        """
        ordered_ids = list(self._tracks.keys())
        kfs = [self._tracks[tid].kf for tid in ordered_ids]

        for kf in kfs:
            kf_engine.predict(kf, dt)

        z_vectors = [np.array([m.x, m.y, m.z]) for m in measurements]
        matches, unmatched_t, unmatched_m = associate(kfs, z_vectors)

        matched_track_idx: set[int] = set()
        for t_idx, m_idx in matches:
            tid = ordered_ids[t_idx]
            rec = self._tracks[tid]
            meas = measurements[m_idx]
            kf_engine.update(rec.kf, z_vectors[m_idx], measurement_sigma=max(meas.sigma_x, 1.0))
            rec.hits += 1
            rec.misses = 0
            rec.sources.add(meas.sensor_type)
            if meas.uas_id:
                rec.uas_id = meas.uas_id
            if meas.class_name:
                rec.class_name = meas.class_name
            rec.last_update_iso = meas.timestamp_iso
            if rec.state == TrackState.TENTATIVE and rec.hits >= self.n_confirm:
                rec.state = TrackState.CONFIRMED
            elif rec.state == TrackState.LOST:
                rec.state = TrackState.CONFIRMED
            matched_track_idx.add(t_idx)

        for t_idx in unmatched_t:
            tid = ordered_ids[t_idx]
            rec = self._tracks[tid]
            rec.misses += 1
            if rec.state == TrackState.CONFIRMED and rec.misses >= self.m_lost:
                rec.state = TrackState.LOST
            elif rec.state == TrackState.LOST and rec.misses >= (self.m_lost + self.k_delete):
                rec.state = TrackState.DELETED
            elif rec.state == TrackState.TENTATIVE and rec.misses >= self.m_lost:
                rec.state = TrackState.DELETED

        for m_idx in unmatched_m:
            self._spawn(measurements[m_idx])

        self._reap()

        return [rec.to_pydantic() for rec in self._tracks.values()]

    def active_tracks(self) -> list[Track]:
        return [rec.to_pydantic() for rec in self._tracks.values()]

    # ── Internals ────────────────────────────────────────────────

    def _spawn(self, meas: Measurement) -> None:
        track_id = f"t-{uuid.uuid4().hex[:10]}"
        kf = kf_engine.make_cv_filter(meas.x, meas.y, meas.z, sigma_pos=max(meas.sigma_x, 1.0))
        rec = _TrackRec(
            track_id=track_id,
            kf=kf,
            state=TrackState.TENTATIVE,
            hits=1,
            misses=0,
            sources={meas.sensor_type},
            uas_id=meas.uas_id,
            class_name=meas.class_name,
            last_update_iso=meas.timestamp_iso or get_clock().utcnow_iso(),
        )
        self._tracks[track_id] = rec

    def _reap(self) -> None:
        dead = [tid for tid, rec in self._tracks.items() if rec.state == TrackState.DELETED]
        for tid in dead:
            del self._tracks[tid]
