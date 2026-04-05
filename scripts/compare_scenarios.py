"""
scripts/compare_scenarios.py — Multi-scenario battle comparison runner.

Runs a set of scenarios sequentially against a running COP server,
captures the After-Action Report (AAR) from each, and prints a
side-by-side comparison table plus a JSON dump for further analysis.

Prereq: COP server must already be running (e.g. on :8100).

Usage:
  python scripts/compare_scenarios.py \
      --cop_url http://127.0.0.1:8100 \
      --duration 60 \
      --scenarios scenarios/single_drone.json scenarios/swarm_attack.json ...

The runner for each scenario:
  1. POST /api/reset                    → clear COP + AAR + recorder
  2. Spawn run_pipeline.py subprocess   → feeds sensor events into COP
  3. Wait for the pipeline to exit      → scenario duration elapses
  4. GET /api/ai/aar                    → fetch generated AAR
  5. Save JSON + extract key metrics

Output:
  - reports/comparison_<timestamp>.json   full AAR per scenario
  - stdout table with peak threat, coord attacks, breaches, risk level, etc.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_post(url: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ── Scenario runner ─────────────────────────────────────────────────────────

def run_scenario(
    cop_url: str,
    scenario_path: Path,
    duration_s: float,
    rate_hz: float,
    verbose: bool = False,
) -> dict:
    """Run one scenario end-to-end and return its AAR."""
    name = scenario_path.stem
    print(f"\n[runner] >> {name}  ({duration_s}s @ {rate_hz}Hz)", flush=True)

    # 1) Reset COP state so the AAR only covers this scenario.
    try:
        _http_post(f"{cop_url}/api/reset")
    except Exception as e:
        print(f"[runner] reset failed: {e}", file=sys.stderr)
        raise

    # Give the server a moment to fully clear / restart AAR session.
    time.sleep(0.5)

    # 2) Launch pipeline.
    cmd = [
        sys.executable, str(ROOT / "run_pipeline.py"),
        "--cop_url",    cop_url,
        "--duration_s", str(duration_s),
        "--rate_hz",    str(rate_hz),
        "--scenario",   str(scenario_path),
    ]
    t0 = time.time()
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if not verbose else None,
        stderr=subprocess.DEVNULL if not verbose else None,
    )
    elapsed = time.time() - t0
    print(f"[runner] OK pipeline exited rc={proc.returncode} in {elapsed:.1f}s", flush=True)

    # Small drain so last AI tick is processed.
    time.sleep(1.5)

    # 3) Fetch AAR (this does not end the session, just snapshots it).
    aar = _http_get(f"{cop_url}/api/ai/aar", timeout=10.0)
    aar["_scenario"] = name
    aar["_runner_duration_s"] = round(elapsed, 1)
    return aar


# ── Metric extraction ───────────────────────────────────────────────────────

def summarize(aar: dict) -> dict:
    """Pull the key numbers out of a raw AAR for the comparison table."""
    exec_ = aar.get("executive_summary", {})
    threat = aar.get("threat_analysis", {})
    coord = aar.get("coordinated_attack_analysis", {})
    breach = aar.get("zone_breach_analysis", {})
    anom = aar.get("anomaly_analysis", {})
    tasks = aar.get("task_summary", {})
    risk = aar.get("risk_assessment", {})

    return {
        "scenario":        aar.get("_scenario", "?"),
        "unique_tracks":   exec_.get("total_unique_tracks", 0),
        "max_concurrent":  exec_.get("max_concurrent_tracks", 0),
        "peak_score":      exec_.get("peak_threat_score", 0),
        "peak_track":      exec_.get("peak_threat_track", ""),
        "threat_events":   exec_.get("total_threat_events", 0),
        "high_threats":    threat.get("high_threat_count", 0),
        "medium_threats":  threat.get("medium_threat_count", 0),
        "low_threats":     threat.get("low_threat_count", 0),
        "anomalies":       anom.get("total", 0),
        "coord_attacks":   coord.get("total", 0),
        "pincer":          coord.get("pincer_count", 0),
        "convergence":     coord.get("convergence_count", 0),
        "zone_breaches":   breach.get("total", 0),
        "tasks_created":   tasks.get("total_created", 0),
        "risk_level":      risk.get("overall_risk", "?"),
        "duration_s":      exec_.get("duration_s", 0),
    }


# ── Pretty print ────────────────────────────────────────────────────────────

HEADERS = [
    ("scenario",       "SCENARIO",   18),
    ("unique_tracks",  "TRKs",        5),
    ("max_concurrent", "MAX",         4),
    ("peak_score",     "PEAK",        5),
    ("high_threats",   "HIGH",        5),
    ("medium_threats", "MED",         5),
    ("low_threats",    "LOW",         5),
    ("anomalies",      "ANOM",        5),
    ("coord_attacks",  "CORD",        5),
    ("pincer",         "PINC",        5),
    ("zone_breaches",  "BRCH",        5),
    ("tasks_created",  "TASK",        5),
    ("risk_level",     "RISK",       10),
]


def print_table(rows: list[dict]) -> None:
    line = " | ".join(f"{label:<{w}}" for _k, label, w in HEADERS)
    sep = "-+-".join("-" * w for _k, _label, w in HEADERS)
    print("\n" + line)
    print(sep)
    for r in rows:
        cells = []
        for key, _label, w in HEADERS:
            val = r.get(key, "")
            cells.append(f"{str(val):<{w}}")
        print(" | ".join(cells))
    print()


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cop_url",  default="http://127.0.0.1:8100")
    ap.add_argument("--duration", type=float, default=60.0,
                    help="Override scenario duration (seconds)")
    ap.add_argument("--rate_hz",  type=float, default=1.0)
    ap.add_argument("--verbose",  action="store_true")
    ap.add_argument(
        "--scenarios", nargs="+",
        default=[
            "scenarios/single_drone.json",
            "scenarios/swarm_attack.json",
            "scenarios/coordinated_attack.json",
            "scenarios/multi_axis_attack.json",
            "scenarios/decoy_attack.json",
        ],
    )
    args = ap.parse_args()

    # Sanity check: server up?
    try:
        status = _http_get(f"{args.cop_url}/api/ai/status")
        print(f"[runner] COP up -- ML available: {status.get('ml_model', {}).get('available')}")
    except Exception as e:
        print(f"[runner] cannot reach COP at {args.cop_url}: {e}", file=sys.stderr)
        sys.exit(2)

    all_aars: list[dict] = []
    rows: list[dict] = []
    for sc in args.scenarios:
        path = (ROOT / sc).resolve() if not Path(sc).is_absolute() else Path(sc)
        if not path.exists():
            print(f"[runner] missing scenario: {path}", file=sys.stderr)
            continue
        aar = run_scenario(
            args.cop_url, path,
            duration_s=args.duration, rate_hz=args.rate_hz,
            verbose=args.verbose,
        )
        all_aars.append(aar)
        rows.append(summarize(aar))

    # Table
    print_table(rows)

    # Save full report
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = reports_dir / f"comparison_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": ts, "summary_rows": rows, "full_aars": all_aars},
            f, indent=2, default=str,
        )
    print(f"[runner] full report -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
