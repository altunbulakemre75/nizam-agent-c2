"""
train_trajectory.py — Generate synthetic trajectories and train the LSTM.

Usage:
    python train_trajectory.py [--epochs 30] [--samples 8000] [--out ai/trajectory_model.pt]

Trajectory patterns generated:
  - Straight (constant velocity)
  - Constant-rate turn (left/right)
  - Accelerating / decelerating
  - S-curve (two successive turns)
  - Evasive jink (sudden heading change)

The model learns to predict the next PRED_STEPS positions given
a SEQ_LEN-step history, working entirely in local metres so the
result is translation-invariant across geographic areas.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import numpy as np
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    print("ERROR: PyTorch not installed.")
    print("  pip install torch numpy")
    sys.exit(1)

# ── Hyper-parameters (must match ai/trajectory.py) ───────────────────────────
SEQ_LEN    = 20
PRED_STEPS = 12
HIDDEN     = 128
LAYERS     = 2
INPUT_DIM  = 5
POS_SCALE  = 5_000.0
SPD_SCALE  = 150.0
DT         = 1.0    # seconds per step


# ── Trajectory generator ──────────────────────────────────────────────────────

def _gen_one(n_steps: int = SEQ_LEN + PRED_STEPS) -> list[tuple]:
    """
    Generate one synthetic trajectory.
    Returns list of (x_m, y_m, speed_ms, heading_deg) — local metres.
    """
    pattern = random.choice([
        "straight", "straight", "straight",   # most common
        "turn_left", "turn_right",
        "accel", "decel",
        "s_curve", "jink",
    ])

    x, y     = 0.0, 0.0
    speed    = random.uniform(15.0, 120.0)          # m/s
    heading  = random.uniform(0.0, 360.0)           # degrees
    turn_r   = random.uniform(1.5, 6.0)             # deg/step
    jink_at  = random.randint(3, SEQ_LEN - 3)
    jink_hdg = heading + random.choice([-1, 1]) * random.uniform(30, 90)
    s_half   = n_steps // 2

    pts = []
    for i in range(n_steps):
        # Apply pattern
        if pattern == "turn_left":
            heading = (heading - turn_r) % 360
        elif pattern == "turn_right":
            heading = (heading + turn_r) % 360
        elif pattern == "accel":
            speed = min(speed + random.uniform(0.5, 2.0), SPD_SCALE * 0.9)
        elif pattern == "decel":
            speed = max(speed - random.uniform(0.5, 2.0), 5.0)
        elif pattern == "s_curve":
            if i < s_half:
                heading = (heading + turn_r) % 360
            else:
                heading = (heading - turn_r) % 360
        elif pattern == "jink":
            if i == jink_at:
                heading = jink_hdg % 360

        # Observation noise
        spd_obs = max(speed + random.gauss(0, speed * 0.03), 1.0)
        hdg_obs = (heading + random.gauss(0, 2.5)) % 360

        pts.append((x, y, spd_obs, hdg_obs))

        # Propagate true state
        hr     = math.radians(heading)
        x     += speed * DT * math.sin(hr)
        y     += speed * DT * math.cos(hr)

    return pts


def _build_dataset(n_samples: int):
    """
    Returns:
        X: (n_samples, SEQ_LEN, INPUT_DIM)  float32 tensor
        Y: (n_samples, PRED_STEPS, 2)        float32 tensor
    """
    X_list, Y_list = [], []
    total = SEQ_LEN + PRED_STEPS

    for _ in range(n_samples):
        pts = _gen_one(total)

        # Feature sequence for input window
        seq = []
        for x, y, spd, hdg in pts[:SEQ_LEN]:
            hr = math.radians(hdg)
            seq.append([
                x / POS_SCALE,
                y / POS_SCALE,
                spd / SPD_SCALE,
                math.sin(hr),
                math.cos(hr),
            ])

        # Target: (dx, dy) offsets from last input point
        ox, oy = pts[SEQ_LEN - 1][0], pts[SEQ_LEN - 1][1]
        tgt = []
        for x, y, _, _ in pts[SEQ_LEN:]:
            tgt.append([(x - ox) / POS_SCALE, (y - oy) / POS_SCALE])

        X_list.append(seq)
        Y_list.append(tgt)

    X = torch.tensor(X_list, dtype=torch.float32)
    Y = torch.tensor(Y_list, dtype=torch.float32)
    return X, Y


# ── Model (must match ai/trajectory.py) ──────────────────────────────────────

class _TrajLSTM(nn.Module):
    def __init__(self):
        super().__init__()
        self.lstm = nn.LSTM(INPUT_DIM, HIDDEN, LAYERS, batch_first=True, dropout=0.1)
        self.head = nn.Sequential(
            nn.Linear(HIDDEN, 64),
            nn.ReLU(),
            nn.Linear(64, PRED_STEPS * 2),
        )

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :]).view(-1, PRED_STEPS, 2)


# ── Training ──────────────────────────────────────────────────────────────────

def train(n_samples: int, epochs: int, out_path: str) -> None:
    print(f"[train] Generating {n_samples} synthetic trajectories …")
    t0 = time.time()
    X, Y = _build_dataset(n_samples)
    print(f"[train] Dataset ready in {time.time()-t0:.1f}s  "
          f"X={tuple(X.shape)}  Y={tuple(Y.shape)}")

    # 90/10 train/val split
    split = int(0.9 * len(X))
    ds_tr = TensorDataset(X[:split], Y[:split])
    ds_va = TensorDataset(X[split:], Y[split:])
    dl_tr = DataLoader(ds_tr, batch_size=256, shuffle=True)
    dl_va = DataLoader(ds_va, batch_size=512)

    model     = _TrajLSTM()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.MSELoss()

    best_val = float("inf")
    best_state = None

    print(f"[train] Training {epochs} epochs …")
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
        tr_loss /= len(ds_tr)

        model.eval()
        va_loss = 0.0
        with torch.no_grad():
            for xb, yb in dl_va:
                va_loss += criterion(model(xb), yb).item() * len(xb)
        va_loss /= len(ds_va)

        scheduler.step()

        if va_loss < best_val:
            best_val   = va_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if ep % 5 == 0 or ep == 1:
            # Convert MSE (normalised) → approximate metres RMSE
            rmse_m = math.sqrt(va_loss) * POS_SCALE
            print(f"  epoch {ep:3d}/{epochs}  train={tr_loss:.5f}  "
                  f"val={va_loss:.5f}  (~{rmse_m:.0f}m RMSE)")

    # Save best checkpoint
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    torch.save(best_state, out_path)
    rmse_m = math.sqrt(best_val) * POS_SCALE
    print(f"\n[train] Best val RMSE ≈ {rmse_m:.1f} m  →  saved to {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Train NIZAM LSTM trajectory predictor")
    ap.add_argument("--epochs",  type=int, default=40,
                    help="Training epochs (default 40)")
    ap.add_argument("--samples", type=int, default=10_000,
                    help="Synthetic trajectory count (default 10 000)")
    ap.add_argument("--out",     default="ai/trajectory_model.pt",
                    help="Output model path")
    args = ap.parse_args()

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)

    train(args.samples, args.epochs, args.out)
