"""
scripts/data_pipeline.py — Automated real-data collection and retraining pipeline

Provides two modes:

  run        — Execute one pipeline cycle right now:
                • Collect COLLECT_MINUTES of live OpenSky ADS-B data
                • Append to the rolling data archive  (data/opensky_archive/)
                • Train or fine-tune the LSTM model
                • Log the result to  data/pipeline_log.jsonl

  schedule   — Register Windows Task Scheduler jobs:
                • Daily collection at DAILY_HOUR:00  (default 02:00)
                • Weekly fine-tune  every Sunday at WEEKLY_HOUR:00 (default 03:00)
                The tasks call  python data_pipeline.py run  automatically.

  status     — Print the last N log entries and scheduled task info.

  unschedule — Remove the Task Scheduler jobs.

Usage:
  python scripts/data_pipeline.py run
  python scripts/data_pipeline.py run --finetune          # force fine-tune
  python scripts/data_pipeline.py run --minutes 30
  python scripts/data_pipeline.py schedule
  python scripts/data_pipeline.py schedule --daily_hour 1 --weekly_hour 2
  python scripts/data_pipeline.py status
  python scripts/data_pipeline.py unschedule
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT         = Path(__file__).resolve().parent.parent
DATA_DIR     = ROOT / "data"
ARCHIVE_DIR  = DATA_DIR / "opensky_archive"
LOG_FILE     = DATA_DIR / "pipeline_log.jsonl"
MODEL_PATH   = ROOT / "ai" / "trajectory_model.pt"

TASK_DAILY   = "NIZAM_daily_collect"
TASK_WEEKLY  = "NIZAM_weekly_finetune"

DEFAULT_COLLECT_MINUTES = 20
DEFAULT_DAILY_HOUR      = 2
DEFAULT_WEEKLY_HOUR     = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(entry: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _last_log_entries(n: int = 10) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    results = []
    for line in lines[-n:]:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return results


# ── Collection ────────────────────────────────────────────────────────────────

def run_collect(minutes: int) -> dict:
    """
    Run opensky_train.py in collect-only mode.
    Returns {"ok": bool, "output_path": str, "duration_s": float, "detail": str}.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path  = ARCHIVE_DIR / f"opensky_{ts}.json"

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "opensky_train.py"),
        "--minutes",      str(minutes),
        "--collect_only",
        "--cache",        str(out_path),
    ]

    t0 = time.time()
    print(f"[pipeline] Collecting {minutes} min of OpenSky data → {out_path.name}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=minutes * 75,   # 25% headroom
        )
        duration = time.time() - t0
        ok = result.returncode == 0
        detail = (result.stdout + result.stderr).strip()[-500:]
        if ok:
            # Also update the "latest" symlink-style copy
            import shutil
            shutil.copy2(out_path, DATA_DIR / "opensky.json")
        return {"ok": ok, "output_path": str(out_path), "duration_s": round(duration, 1), "detail": detail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output_path": str(out_path),
                "duration_s": time.time() - t0, "detail": "collection timed out"}
    except Exception as e:
        return {"ok": False, "output_path": str(out_path),
                "duration_s": time.time() - t0, "detail": str(e)}


# ── Training ──────────────────────────────────────────────────────────────────

def run_train(finetune: bool, cache_path: Path | None = None) -> dict:
    """
    Run opensky_train.py in training mode using the latest cached data.
    Returns {"ok": bool, "val_rmse_m": float | None, "duration_s": float, "detail": str}.
    """
    latest_cache = cache_path or (DATA_DIR / "opensky.json")
    if not latest_cache.exists():
        return {"ok": False, "val_rmse_m": None, "duration_s": 0,
                "detail": f"no cache at {latest_cache}"}

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "opensky_train.py"),
        "--cache", str(latest_cache),
    ]
    if finetune:
        cmd.append("--finetune")

    mode = "fine-tune" if finetune else "full retrain"
    t0 = time.time()
    print(f"[pipeline] Running LSTM {mode} from {latest_cache.name} ...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=3600,
        )
        duration = time.time() - t0
        ok = result.returncode == 0
        detail = (result.stdout + result.stderr).strip()[-800:]

        # Parse val RMSE from output: "En iyi val RMSE ≈ 123.4m"
        val_rmse = None
        for line in (result.stdout + result.stderr).splitlines():
            if "val RMSE" in line or "RMSE" in line:
                import re
                m = re.search(r"(\d+\.?\d*)\s*m", line)
                if m:
                    val_rmse = float(m.group(1))

        return {"ok": ok, "val_rmse_m": val_rmse, "duration_s": round(duration, 1), "detail": detail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "val_rmse_m": None, "duration_s": time.time() - t0,
                "detail": "training timed out after 1h"}
    except Exception as e:
        return {"ok": False, "val_rmse_m": None, "duration_s": time.time() - t0, "detail": str(e)}


# ── Pipeline cycle ────────────────────────────────────────────────────────────

def run_pipeline(minutes: int, finetune: bool) -> int:
    start = _utc_now_iso()
    print(f"\n{'='*60}")
    print(f"  NIZAM Data Pipeline  —  {start}")
    print(f"{'='*60}")

    # 1. Collect
    collect_result = run_collect(minutes)
    cache_path = Path(collect_result["output_path"]) if collect_result["ok"] else None

    # 2. Train (even if collect failed, use existing cache as fallback)
    if not collect_result["ok"]:
        print(f"[pipeline] Collection failed: {collect_result['detail'][:200]}")
        print("[pipeline] Falling back to existing cache for training ...")
        cache_path = None

    train_result = run_train(finetune, cache_path=cache_path)

    # 3. Determine weekly fine-tune based on day-of-week (Sun=6 or 0 depending on platform)
    # (When scheduled via --schedule, mode is determined at call time)

    # 4. Log result
    entry = {
        "ts":          start,
        "collect":     collect_result,
        "train":       train_result,
        "finetune":    finetune,
        "model_path":  str(MODEL_PATH),
        "ok":          collect_result["ok"] and train_result["ok"],
    }
    _log(entry)

    # 5. Print summary
    print()
    if entry["ok"]:
        rmse = train_result.get("val_rmse_m")
        rmse_str = f"  val RMSE = {rmse:.1f} m" if rmse else ""
        print(f"  [OK] Pipeline complete.{rmse_str}")
    else:
        print(f"  [FAIL] collect={collect_result['ok']}  train={train_result['ok']}")
        if not collect_result["ok"]:
            print(f"         collect detail: {collect_result['detail'][:200]}")
        if not train_result["ok"]:
            print(f"         train detail:   {train_result['detail'][:200]}")
    print(f"  Logged to: {LOG_FILE}")
    print(f"{'='*60}\n")

    return 0 if entry["ok"] else 1


# ── Windows Task Scheduler ────────────────────────────────────────────────────

def _schtasks_create(task_name: str, schedule: str, cmd_args: str) -> bool:
    """Register a Windows Task Scheduler task."""
    python_exe = sys.executable
    script     = str(ROOT / "scripts" / "data_pipeline.py")
    full_cmd   = f'"{python_exe}" "{script}" {cmd_args}'

    schtasks_cmd = [
        "schtasks", "/create", "/f",
        "/tn", task_name,
        "/tr", full_cmd,
        "/sc", "WEEKLY" if "WEEKLY" in task_name else "DAILY",
    ] + schedule.split()

    try:
        result = subprocess.run(schtasks_cmd, capture_output=True,
                                encoding="utf-8", errors="replace")
        if result.returncode == 0:
            print(f"  [OK] Task '{task_name}' registered.")
            return True
        else:
            print(f"  [FAIL] {task_name}: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def _schtasks_delete(task_name: str) -> bool:
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/f", "/tn", task_name],
            capture_output=True, encoding="utf-8", errors="replace",
        )
        ok = result.returncode == 0
        msg = "removed" if ok else result.stderr.strip()
        print(f"  [{('OK' if ok else 'FAIL')}] {task_name}: {msg}")
        return ok
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def _schtasks_query(task_name: str) -> str:
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "LIST"],
            capture_output=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "Next Run" in line or "Status" in line:
                    return line.strip()
        return "not found"
    except Exception:
        return "query failed"


def schedule_tasks(daily_hour: int, weekly_hour: int) -> None:
    if platform.system() != "Windows":
        print("[schedule] Task Scheduler is Windows-only.")
        print("           For Linux/macOS, add to crontab:")
        print(f"           0 {daily_hour} * * * cd {ROOT} && python scripts/data_pipeline.py run")
        print(f"           0 {weekly_hour} * * 0 cd {ROOT} && python scripts/data_pipeline.py run --finetune")
        return

    print(f"\n[schedule] Registering Task Scheduler jobs ...")
    print(f"  Daily collection : every day at {daily_hour:02d}:00")
    print(f"  Weekly fine-tune : every Sunday at {weekly_hour:02d}:00")

    # Daily: collect + full retrain at daily_hour:00
    _schtasks_create(
        TASK_DAILY,
        f"/st {daily_hour:02d}:00 /d MON,TUE,WED,THU,FRI,SAT,SUN",
        f"run --minutes {DEFAULT_COLLECT_MINUTES}",
    )

    # Weekly: fine-tune on Sunday
    _schtasks_create(
        TASK_WEEKLY,
        f"/st {weekly_hour:02d}:00 /d SUN",
        "run --finetune --minutes 30",
    )


def unschedule_tasks() -> None:
    print("\n[unschedule] Removing Task Scheduler jobs ...")
    _schtasks_delete(TASK_DAILY)
    _schtasks_delete(TASK_WEEKLY)


def show_status(n: int = 10) -> None:
    print(f"\n{'='*60}")
    print("  NIZAM Data Pipeline — Status")
    print(f"{'='*60}")

    print(f"\n  Model:  {MODEL_PATH}")
    if MODEL_PATH.exists():
        mtime = datetime.fromtimestamp(MODEL_PATH.stat().st_mtime, tz=timezone.utc)
        print(f"  Updated: {mtime.isoformat()}")
    else:
        print("  Model file not found!")

    print(f"\n  Archive: {ARCHIVE_DIR}")
    if ARCHIVE_DIR.exists():
        files = sorted(ARCHIVE_DIR.glob("opensky_*.json"))
        print(f"  Files: {len(files)}  ({files[0].name if files else '—'} → {files[-1].name if files else '—'})")
    else:
        print("  (empty)")

    print(f"\n  Last {n} pipeline runs:")
    entries = _last_log_entries(n)
    if not entries:
        print("  (no runs recorded yet)")
    for e in entries:
        rmse = e.get("train", {}).get("val_rmse_m")
        rmse_str = f"  RMSE={rmse:.1f}m" if rmse else ""
        status = "OK" if e.get("ok") else "FAIL"
        mode   = "finetune" if e.get("finetune") else "full"
        print(f"  [{status}] {e['ts']}  mode={mode}{rmse_str}")

    if platform.system() == "Windows":
        print(f"\n  Scheduled tasks:")
        print(f"  {TASK_DAILY:<40} {_schtasks_query(TASK_DAILY)}")
        print(f"  {TASK_WEEKLY:<40} {_schtasks_query(TASK_WEEKLY)}")

    print(f"{'='*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="NIZAM automated data pipeline")
    sub = ap.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Execute one collection+train cycle")
    p_run.add_argument("--minutes",  type=int,  default=DEFAULT_COLLECT_MINUTES,
                       help=f"Collection duration in minutes (default {DEFAULT_COLLECT_MINUTES})")
    p_run.add_argument("--finetune", action="store_true",
                       help="Fine-tune existing model instead of full retrain")

    # schedule
    p_sched = sub.add_parser("schedule", help="Register Task Scheduler jobs (Windows)")
    p_sched.add_argument("--daily_hour",  type=int, default=DEFAULT_DAILY_HOUR)
    p_sched.add_argument("--weekly_hour", type=int, default=DEFAULT_WEEKLY_HOUR)

    # unschedule
    sub.add_parser("unschedule", help="Remove scheduled tasks")

    # status
    p_stat = sub.add_parser("status", help="Show pipeline status and last runs")
    p_stat.add_argument("--n", type=int, default=10)

    args = ap.parse_args()

    if args.command == "run":
        return run_pipeline(args.minutes, args.finetune)
    elif args.command == "schedule":
        schedule_tasks(args.daily_hour, args.weekly_hour)
        return 0
    elif args.command == "unschedule":
        unschedule_tasks()
        return 0
    elif args.command == "status":
        show_status(args.n)
        return 0
    else:
        ap.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
