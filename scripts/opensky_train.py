#!/usr/bin/env python3
"""
scripts/opensky_train.py — Gerçek ADS-B verisiyle LSTM yeniden eğitimi

OpenSky Network'ten canlı uçuş trajektoryaları çeker (ücretsiz, hesap gerekmez),
sensör gürültüsü ekler, sentetik veriyle karıştırır ve LSTM'yi yeniden eğitir.

Kullanım:
    python scripts/opensky_train.py                     # 20dk topla + sıfırdan eğit
    python scripts/opensky_train.py --minutes 10        # kısa toplama
    python scripts/opensky_train.py --finetune          # mevcut modeli ince ayarla
    python scripts/opensky_train.py --cache data/opensky.json  # önbellek kullan
    python scripts/opensky_train.py --collect_only      # sadece veri topla
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
# Windows terminali UTF-8 desteklemeyebilir
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ── Bağımlılık kontrolü ──────────────────────────────────────────────────────
try:
    import numpy as np
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    print("HATA: PyTorch/NumPy kurulu değil.")
    print("  pip install torch numpy")
    sys.exit(1)

# ── Sabitler (train_trajectory.py ile eşleşmeli) ─────────────────────────────
SEQ_LEN    = 20
PRED_STEPS = 12
HIDDEN     = 128
LAYERS     = 2
INPUT_DIM  = 5
POS_SCALE  = 5_000.0   # metre
SPD_SCALE  = 150.0     # m/s
WINDOW_LEN = SEQ_LEN + PRED_STEPS  # 32 nokta per örnek

# OpenSky — Türkiye + çevresi
BBOX = {"lamin": 35.5, "lamax": 43.0, "lomin": 25.5, "lomax": 45.0}
OPENSKY_URL = "https://opensky-network.org/api/states/all"

POLL_INTERVAL_S = 15    # Anonim erişim limiti: ~400 istek/gün
TARGET_DT_S     = 5.0   # Track'leri bu adım büyüklüğüne yeniden örnekle
MIN_TRACK_PTS   = WINDOW_LEN + 4
MAX_SPD_MS      = 350.0  # filtre: ses hızından hızlı
MIN_SPD_MS      = 8.0    # filtre: yerdeki/durağan
AUGMENT_N       = 5      # her gerçek pencere için kaç gürültülü kopya
SYNTH_RATIO     = 0.4    # %40 sentetik, %60 gerçek

_R_EARTH = 6_371_000.0


# ── Koordinat yardımcıları ────────────────────────────────────────────────────

def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> Tuple[float, float]:
    cos0 = math.cos(math.radians(lat0))
    x = math.radians(lon - lon0) * _R_EARTH * cos0
    y = math.radians(lat - lat0) * _R_EARTH
    return x, y


def _heading_from_pts(lat1, lon1, lat2, lon2) -> float:
    """İki nokta arasındaki başlık açısı (derece, 0=Kuzey)."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(dlon, dlat)) % 360


# ── OpenSky veri çekme ────────────────────────────────────────────────────────

def _fetch_states() -> Optional[List]:
    """Tek OpenSky API isteği. State vektörlerini döndürür veya None."""
    params = "&".join(f"{k}={v}" for k, v in BBOX.items())
    url = f"{OPENSKY_URL}?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NIZAM-COP/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("states") or []
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  [opensky] Rate limit (429) — {POLL_INTERVAL_S*2}s bekleniyor")
            time.sleep(POLL_INTERVAL_S * 2)
        return None
    except Exception as e:
        print(f"  [opensky] İstek hatası: {e}")
        return None


def collect_live(minutes: int) -> Dict[str, List[dict]]:
    """
    N dakika boyunca OpenSky'ı yokla, uçak başına track geçmişi döndür.

    Dönüş: {icao24: [{lat, lon, speed, heading, t}, ...]}
    """
    tracks: Dict[str, List[dict]] = defaultdict(list)
    deadline = time.time() + minutes * 60
    poll = 0

    print(f"[opensky] {minutes} dakika canlı veri toplanıyor "
          f"(Türkiye bölgesi, ~{60//POLL_INTERVAL_S * minutes} istek)…")

    while time.time() < deadline:
        states = _fetch_states()
        if states is None:
            time.sleep(POLL_INTERVAL_S)
            continue

        now = time.time()
        active = 0
        for s in states:
            if len(s) < 17:
                continue
            icao24     = s[0]
            lon        = s[5]
            lat        = s[6]
            on_ground  = s[8]
            velocity   = s[9]   # m/s
            true_track = s[10]  # derece, 0=Kuzey

            if on_ground or lat is None or lon is None:
                continue
            if velocity is None or not (MIN_SPD_MS <= velocity <= MAX_SPD_MS):
                continue

            heading = true_track if true_track is not None else (
                _heading_from_pts(
                    tracks[icao24][-1]["lat"], tracks[icao24][-1]["lon"],
                    lat, lon
                ) if tracks[icao24] else 0.0
            )

            # Aynı uçaktan çok sık nokta ekleme (GPS güncellemesi yoksa)
            if tracks[icao24]:
                last = tracks[icao24][-1]
                if now - last["t"] < POLL_INTERVAL_S * 0.7:
                    continue

            tracks[icao24].append({
                "lat": lat, "lon": lon,
                "speed": velocity,
                "heading": heading % 360,
                "t": now,
            })
            active += 1

        poll += 1
        elapsed = minutes * 60 - (deadline - time.time())
        n_tracks = sum(1 for pts in tracks.values() if len(pts) >= 3)
        print(f"  [{elapsed:.0f}s] {active} aktif ucak, "
              f"{n_tracks} track (>=3 nokta), toplam nokta={sum(len(v) for v in tracks.values())}",
              end="\r", flush=True)

        remaining = deadline - time.time()
        sleep = min(POLL_INTERVAL_S, remaining)
        if sleep > 0:
            time.sleep(sleep)

    print()
    return dict(tracks)


# ── Track işleme ──────────────────────────────────────────────────────────────

def _interpolate(pts: List[dict], target_dt: float = TARGET_DT_S) -> List[dict]:
    """Uniform target_dt saniyelik aralıklara doğrusal interpolasyon."""
    if len(pts) < 2:
        return pts
    result = [pts[0]]
    for i in range(len(pts) - 1):
        p0, p1 = pts[i], pts[i + 1]
        gap = p1["t"] - p0["t"]
        if gap <= 0 or gap > 120:  # büyük boşluğu atla
            result.append(p1)
            continue
        steps = max(1, round(gap / target_dt))
        for j in range(1, steps):
            frac = j / steps
            result.append({
                "lat":     p0["lat"]     + (p1["lat"]     - p0["lat"])     * frac,
                "lon":     p0["lon"]     + (p1["lon"]     - p0["lon"])     * frac,
                "speed":   p0["speed"]   + (p1["speed"]   - p0["speed"])   * frac,
                "heading": p0["heading"],  # başlık adım adım değişir, kesik tut
                "t":       p0["t"]       + gap * frac,
            })
        result.append(p1)
    return result


def _normalize_speed(pts: List[dict]) -> List[dict]:
    """
    Uçak hızlarını (150-300 m/s) sentetik UAV aralığına (15-120 m/s) ölçekle.
    Trajektori şekli korunur, sadece hız ve mesafe küçülür.
    """
    speeds = [p["speed"] for p in pts]
    med = float(np.median(speeds))
    if med < 15:
        return pts  # zaten UAV hızında
    scale = 60.0 / med  # hedef: 60 m/s ortalamasına normalize et
    if not pts:
        return pts
    # Referans noktadan itibaren pozisyonları da ölçekle
    lat0, lon0 = pts[0]["lat"], pts[0]["lon"]
    prev_x, prev_y = 0.0, 0.0
    result_pts = []
    for p in pts:
        x, y = _to_xy(p["lat"], p["lon"], lat0, lon0)
        # Orijinal'den sapma ölçeklenir
        sx = x * scale
        sy = y * scale
        # lat/lon'a geri dönüştür
        new_lat = lat0 + math.degrees(sy / _R_EARTH)
        cos0 = math.cos(math.radians(lat0))
        new_lon = lon0 + math.degrees(sx / (_R_EARTH * cos0))
        result_pts.append({
            "lat": new_lat, "lon": new_lon,
            "speed": p["speed"] * scale,
            "heading": p["heading"],
            "t": p["t"],
        })
    return result_pts


# ── Gürültü augmentasyonu ──────────────────────────────────────────────────────

def _augment_once(pts: List[dict]) -> List[dict]:
    """Gerçekçi sensör gürültüsü ekle — tek kopya."""
    noisy = []
    prev_dropout = False
    for p in pts:
        # GPS ölçüm gürültüsü (~5m RMS)
        lat = p["lat"] + random.gauss(0, 0.000045)
        lon = p["lon"] + random.gauss(0, 0.000045)

        # Hız gürültüsü (%3 RMS)
        spd = max(1.0, p["speed"] + random.gauss(0, p["speed"] * 0.03))

        # Başlık gürültüsü (±2°)
        hdg = (p["heading"] + random.gauss(0, 2.0)) % 360

        # %5 dropout: son ölçümü tekrar et
        if not prev_dropout and random.random() < 0.05:
            if noisy:
                noisy.append(dict(noisy[-1], t=p["t"]))
                prev_dropout = True
                continue
        prev_dropout = False

        # %0.5 GPS spoofing: ani konum zıplaması
        if random.random() < 0.005:
            lat += random.uniform(-0.003, 0.003)
            lon += random.uniform(-0.003, 0.003)

        noisy.append({"lat": lat, "lon": lon, "speed": spd, "heading": hdg, "t": p["t"]})
    return noisy


# ── Pencere çıkarma ───────────────────────────────────────────────────────────

def extract_windows(tracks: Dict[str, List[dict]]) -> List[List[dict]]:
    """
    Her track'ten SEQ_LEN+PRED_STEPS noktalık kayan pencereler çıkar.
    Gürültülü kopyalar dahil.
    """
    windows: List[List[dict]] = []

    for icao24, pts in tracks.items():
        pts = _interpolate(pts)
        pts = _normalize_speed(pts)

        if len(pts) < MIN_TRACK_PTS:
            continue

        # Kayan pencere (stride=PRED_STEPS — örtüşmeyi azalt)
        for start in range(0, len(pts) - WINDOW_LEN + 1, PRED_STEPS):
            win = pts[start: start + WINDOW_LEN]
            if len(win) < WINDOW_LEN:
                break
            windows.append(win)
            # Gürültülü kopyalar
            for _ in range(AUGMENT_N):
                windows.append(_augment_once(win))

    return windows


# ── Eğitim verisi oluşturma ───────────────────────────────────────────────────

def _window_to_tensors(win: List[dict]) -> Optional[Tuple[List, List]]:
    """Pencereyi (X_seq, Y_target) formatına dönüştür."""
    lat0 = win[0]["lat"]
    lon0 = win[0]["lon"]

    # Giriş dizisi (SEQ_LEN nokta)
    seq = []
    for p in win[:SEQ_LEN]:
        x, y = _to_xy(p["lat"], p["lon"], lat0, lon0)
        hdg_r = math.radians(p.get("heading", 0.0))
        seq.append([
            x / POS_SCALE,
            y / POS_SCALE,
            p.get("speed", 0.0) / SPD_SCALE,
            math.sin(hdg_r),
            math.cos(hdg_r),
        ])

    # Son giriş noktasına göre hedefler
    ox, oy = _to_xy(win[SEQ_LEN - 1]["lat"], win[SEQ_LEN - 1]["lon"], lat0, lon0)
    tgt = []
    for p in win[SEQ_LEN:]:
        x, y = _to_xy(p["lat"], p["lon"], lat0, lon0)
        tgt.append([(x - ox) / POS_SCALE, (y - oy) / POS_SCALE])

    if len(tgt) < PRED_STEPS:
        return None
    return seq, tgt[:PRED_STEPS]


def _build_synthetic(n: int) -> Tuple[List, List]:
    """Sentetik trajektoryalar üret (train_trajectory.py ile aynı mantık)."""
    import math as _math
    DT = 1.0
    X_list, Y_list = [], []
    total = SEQ_LEN + PRED_STEPS
    patterns = [
        "straight", "straight", "straight",
        "turn_left", "turn_right",
        "accel", "decel", "s_curve", "jink",
    ]
    for _ in range(n):
        pattern = random.choice(patterns)
        x, y    = 0.0, 0.0
        speed   = random.uniform(15.0, 120.0)
        heading = random.uniform(0.0, 360.0)
        turn_r  = random.uniform(1.5, 6.0)
        jink_at = random.randint(3, SEQ_LEN - 3)
        jink_h  = (heading + random.choice([-1, 1]) * random.uniform(30, 90)) % 360
        s_half  = total // 2
        pts = []
        for i in range(total):
            if pattern == "turn_left":
                heading = (heading - turn_r) % 360
            elif pattern == "turn_right":
                heading = (heading + turn_r) % 360
            elif pattern == "accel":
                speed = min(speed + random.uniform(0.5, 2.0), SPD_SCALE * 0.9)
            elif pattern == "decel":
                speed = max(speed - random.uniform(0.5, 2.0), 5.0)
            elif pattern == "s_curve":
                heading = (heading + (turn_r if i < s_half else -turn_r)) % 360
            elif pattern == "jink":
                if i == jink_at:
                    heading = jink_h
            spd_obs = max(speed + random.gauss(0, speed * 0.03), 1.0)
            hdg_obs = (heading + random.gauss(0, 2.5)) % 360
            pts.append((x, y, spd_obs, hdg_obs))
            hr = _math.radians(heading)
            x += speed * DT * _math.sin(hr)
            y += speed * DT * _math.cos(hr)

        seq = []
        for px, py, spd, hdg in pts[:SEQ_LEN]:
            hr = _math.radians(hdg)
            seq.append([px / POS_SCALE, py / POS_SCALE, spd / SPD_SCALE,
                        _math.sin(hr), _math.cos(hr)])
        ox, oy = pts[SEQ_LEN - 1][0], pts[SEQ_LEN - 1][1]
        tgt = [[(p[0] - ox) / POS_SCALE, (p[1] - oy) / POS_SCALE]
               for p in pts[SEQ_LEN:]]
        X_list.append(seq)
        Y_list.append(tgt)
    return X_list, Y_list


def build_dataset(real_windows: List[List[dict]], n_synthetic: int):
    """Gerçek + sentetik veriyi birleştir, tensor döndür."""
    X_list, Y_list = [], []

    # Gerçek veri
    skipped = 0
    for win in real_windows:
        result = _window_to_tensors(win)
        if result is None:
            skipped += 1
            continue
        X_list.append(result[0])
        Y_list.append(result[1])

    n_real = len(X_list)
    if skipped:
        print(f"  [data] {skipped} pencere atlandı (kısa/hatalı)")

    # Sentetik veri
    sx, sy = _build_synthetic(n_synthetic)
    X_list.extend(sx)
    Y_list.extend(sy)

    print(f"  [data] Gerçek: {n_real}  Sentetik: {n_synthetic}  Toplam: {len(X_list)}")

    X = torch.tensor(X_list, dtype=torch.float32)
    Y = torch.tensor(Y_list, dtype=torch.float32)
    return X, Y


# ── Model (train_trajectory.py ile aynı) ─────────────────────────────────────

class _TrajLSTM(nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = nn.LSTM(INPUT_DIM, HIDDEN, LAYERS, batch_first=True, dropout=0.1)
        self.head = nn.Sequential(
            nn.Linear(HIDDEN, 64), nn.ReLU(),
            nn.Linear(64, PRED_STEPS * 2),
        )

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :]).view(-1, PRED_STEPS, 2)


# ── Eğitim ────────────────────────────────────────────────────────────────────

def train(X, Y, epochs: int, out_path: str,
          finetune_from: Optional[str] = None, lr: float = 1e-3) -> None:
    split = int(0.9 * len(X))
    dl_tr = DataLoader(TensorDataset(X[:split], Y[:split]), batch_size=256, shuffle=True)
    dl_va = DataLoader(TensorDataset(X[split:], Y[split:]), batch_size=512)

    model = _TrajLSTM()

    if finetune_from and os.path.exists(finetune_from):
        state = torch.load(finetune_from, map_location="cpu", weights_only=True)
        model.load_state_dict(state)
        print(f"[train] İnce ayar: {finetune_from} ağırlıkları yüklendi (lr={lr:.0e})")
    else:
        print(f"[train] Sıfırdan eğitim (lr={lr:.0e})")

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.MSELoss()

    best_val, best_state = float("inf"), None

    for ep in range(1, epochs + 1):
        model.train()
        tr_loss = 0.0
        for xb, yb in dl_tr:
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            tr_loss += loss.item() * len(xb)
        tr_loss /= len(dl_tr.dataset)

        model.eval()
        va_loss = 0.0
        with torch.no_grad():
            for xb, yb in dl_va:
                va_loss += criterion(model(xb), yb).item() * len(xb)
        va_loss /= len(dl_va.dataset)
        scheduler.step()

        if va_loss < best_val:
            best_val = va_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if ep % 5 == 0 or ep == 1 or ep == epochs:
            rmse_m = math.sqrt(va_loss) * POS_SCALE
            print(f"  epoch {ep:3d}/{epochs}  train={tr_loss:.5f}  "
                  f"val={va_loss:.5f}  (~{rmse_m:.0f}m RMSE)")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    torch.save(best_state, out_path)
    rmse_m = math.sqrt(best_val) * POS_SCALE
    print(f"\n[train] En iyi val RMSE ≈ {rmse_m:.1f}m  →  {out_path}")


# ── Ana akış ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="OpenSky ADS-B verisiyle LSTM eğitimi")
    ap.add_argument("--minutes",      type=int,   default=20,
                    help="Canlı toplama süresi (dk, varsayılan 20)")
    ap.add_argument("--epochs",       type=int,   default=40,
                    help="Eğitim epoch sayısı (varsayılan 40)")
    ap.add_argument("--cache",        default="",
                    help="Önbellek JSON dosyası (toplama yerine kullan)")
    ap.add_argument("--save_cache",   default="data/opensky.json",
                    help="Toplanan veriyi bu dosyaya kaydet")
    ap.add_argument("--collect_only", action="store_true",
                    help="Sadece veri topla, eğitme")
    ap.add_argument("--finetune",     action="store_true",
                    help="Sıfırdan değil, mevcut modeli ince ayarla")
    ap.add_argument("--out",          default="ai/trajectory_model.pt",
                    help="Çıkış model yolu")
    ap.add_argument("--seed",         type=int,   default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    print("=" * 60)
    print("  NIZAM LSTM — OpenSky Gerçek Veri Eğitimi")
    print("=" * 60)

    # ── 1. Veri toplama ──
    if args.cache and os.path.exists(args.cache):
        print(f"[data] Önbellek yükleniyor: {args.cache}")
        with open(args.cache, encoding="utf-8") as f:
            raw_tracks = json.load(f)
    else:
        raw_tracks = collect_live(args.minutes)
        # Kaydet
        os.makedirs(os.path.dirname(args.save_cache) or ".", exist_ok=True)
        with open(args.save_cache, "w", encoding="utf-8") as f:
            json.dump(raw_tracks, f)
        print(f"[data] Ham veri kaydedildi: {args.save_cache}")

    n_aircraft = len(raw_tracks)
    n_points   = sum(len(v) for v in raw_tracks.values())
    print(f"[data] {n_aircraft} uçak, {n_points} toplam nokta")

    if args.collect_only:
        print("[data] --collect_only: eğitim atlandı.")
        return

    # ── 2. Pencere çıkarma ──
    windows = extract_windows(raw_tracks)
    n_real_windows = len(windows)
    print(f"[data] {n_real_windows} gerçek eğitim penceresi "
          f"({AUGMENT_N}x augment dahil)")

    if n_real_windows < 50:
        print(f"[uyarı] Çok az gerçek pencere ({n_real_windows}). "
              f"Daha uzun toplama süresi deneyin (--minutes 40).")

    # ── 3. Dataset ──
    n_synth = max(200, int(n_real_windows * SYNTH_RATIO / (1 - SYNTH_RATIO)))
    X, Y = build_dataset(windows, n_synth)
    print(f"[data] Dataset: {len(X)} örnek  X={tuple(X.shape)}  Y={tuple(Y.shape)}")

    # ── 4. Eğitim ──
    finetune_path = args.out if args.finetune else None
    lr = 3e-4 if args.finetune else 1e-3
    print(f"\n[train] Başlıyor — {args.epochs} epoch…")
    train(X, Y, args.epochs, args.out, finetune_from=finetune_path, lr=lr)

    print("\n[bitti] Model güncellendi:", args.out)
    print("  Sunucuyu yeniden başlat: python start.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
