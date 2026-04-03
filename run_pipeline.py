"""
run_pipeline.py  —  NIZAM agent pipeline launcher

Starts the full sensing pipeline and connects it to the COP server.

Pipeline topology:
                         ┌─→ rf_sim_agent ─┐
  world → radar_sim ──tee                  merge ─→ fuser → cop_publisher
                         └────────────────-┘

COP server must be running separately:
  uvicorn cop.server:app --host 0.0.0.0 --port 8100 --reload

Usage:
  python run_pipeline.py [options]

Options:
  --cop_url       COP ingest URL      (default: http://127.0.0.1:8100)
  --origin_lat    Sensor origin lat   (default: 41.015  = Istanbul)
  --origin_lon    Sensor origin lon   (default: 28.979)
  --duration_s    Simulation duration (default: 300s)
  --rate_hz       World update rate   (default: 1.0 Hz)
  --verbose       Print all events to stderr
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# Fan-out / merge helpers
# ---------------------------------------------------------------------------

def _forward(src, *dsts, verbose: bool = False, tag: str = "") -> None:
    """Read lines from src and write to all dsts (thread-safe per-write)."""
    try:
        for line in src:
            if verbose:
                print(f"[{tag}] {line.decode().rstrip()}", file=sys.stderr)
            for dst in dsts:
                try:
                    dst.write(line)
                    dst.flush()
                except BrokenPipeError:
                    pass
    except Exception as e:
        print(f"[pipeline] forward thread error ({tag}): {e}", file=sys.stderr)


def _merge_into(src, dst, verbose: bool = False, tag: str = "") -> None:
    """Read lines from src and write into dst (for merging side-streams)."""
    _forward(src, dst, verbose=verbose, tag=tag)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="NIZAM pipeline launcher")
    ap.add_argument("--cop_url",          default="http://127.0.0.1:8100")
    ap.add_argument("--orchestrator_url", default="http://127.0.0.1:8200")
    ap.add_argument("--origin_lat",  type=float, default=41.015)
    ap.add_argument("--origin_lon",  type=float, default=28.979)
    ap.add_argument("--duration_s",  type=float, default=300.0)
    ap.add_argument("--rate_hz",     type=float, default=1.0)
    ap.add_argument("--scenario",    default=None, help="Path to scenario JSON file")
    ap.add_argument("--log_out",     default=None, help="Path to save pipeline events as JSONL for replay")
    ap.add_argument("--verbose",     action="store_true")
    args = ap.parse_args()

    py = sys.executable  # use same Python interpreter

    print("[pipeline] Starting NIZAM pipeline...", file=sys.stderr)
    print(f"[pipeline] COP URL  : {args.cop_url}", file=sys.stderr)
    print(f"[pipeline] Origin   : lat={args.origin_lat}, lon={args.origin_lon}", file=sys.stderr)
    print(f"[pipeline] Duration : {args.duration_s}s @ {args.rate_hz} Hz", file=sys.stderr)

    PIPE = subprocess.PIPE

    # ------------------------------------------------------------------
    # 1) World agent  →  stdout (world.state JSONL)
    # ------------------------------------------------------------------
    world_cmd = [
        py, str(ROOT / "agents" / "world" / "world_agent.py"),
        "--stdout",
        "--duration_s", str(args.duration_s),
        "--rate_hz",    str(args.rate_hz),
        "--origin_lat", str(args.origin_lat),
        "--origin_lon", str(args.origin_lon),
    ]
    if args.scenario:
        world_cmd += ["--scenario", args.scenario]
        print(f"[pipeline] Scenario : {args.scenario}", file=sys.stderr)

    world = subprocess.Popen(world_cmd, stdout=PIPE, stderr=sys.stderr)

    # ------------------------------------------------------------------
    # 2) Radar sim agent  world.state → sensor.detection.radar
    # ------------------------------------------------------------------
    radar = subprocess.Popen(
        [py, str(ROOT / "agents" / "radar_sim" / "radar_sim_agent.py")],
        stdin=world.stdout,
        stdout=PIPE,
        stderr=sys.stderr,
    )
    world.stdout.close()  # let radar own the pipe

    # ------------------------------------------------------------------
    # 3) EO sim agent  sensor.detection.radar → + sensor.detection.eo
    #    (pass-through + adds EO events)
    # ------------------------------------------------------------------
    eo = subprocess.Popen(
        [py, str(ROOT / "agents" / "eo_sim" / "eo_sim_agent.py")],
        stdin=radar.stdout,
        stdout=PIPE,
        stderr=sys.stderr,
    )
    radar.stdout.close()  # let eo own radar's pipe

    # ------------------------------------------------------------------
    # 4) RF sim agent  sensor.detection.radar → sensor.detection.rf
    #    (reads from eo output which passes radar events through)
    # ------------------------------------------------------------------
    rf = subprocess.Popen(
        [py, str(ROOT / "agents" / "rf_sim" / "rf_sim_agent.py")],
        stdin=PIPE,
        stdout=PIPE,
        stderr=sys.stderr,
    )

    # ------------------------------------------------------------------
    # 5) Fuser agent  (radar + rf + eo) → track.update + threat.assessment
    # ------------------------------------------------------------------
    fuser = subprocess.Popen(
        [py, str(ROOT / "agents" / "fuser" / "fuser_agent.py")],
        stdin=PIPE,
        stdout=PIPE,
        stderr=sys.stderr,
    )

    # ------------------------------------------------------------------
    # 6) COP publisher  track.update / threat.assessment → POST /ingest
    # ------------------------------------------------------------------
    cop_cmd = [
        py, str(ROOT / "agents" / "cop_publisher.py"),
        "--cop_url",          args.cop_url,
        "--orchestrator_url", args.orchestrator_url,
        "--origin_lat",       str(args.origin_lat),
        "--origin_lon",       str(args.origin_lon),
    ]
    if args.log_out:
        cop_cmd += ["--log_out", args.log_out]
        print(f"[pipeline] Log out  : {args.log_out}", file=sys.stderr)

    cop_pub = subprocess.Popen(cop_cmd,
        stdin=fuser.stdout,
        stderr=sys.stderr,
    )
    fuser.stdout.close()  # let cop_pub own the pipe

    # ------------------------------------------------------------------
    # Thread wiring:
    #   T1: eo.stdout → rf.stdin  AND  fuser.stdin  (fan-out: radar+EO events)
    #   T2: rf.stdout → fuser.stdin                 (merge RF into fuser)
    #
    #   eo.stdout carries both radar pass-through AND sensor.detection.eo events.
    #   RF agent ignores EO events (only processes radar detections).
    #   Fuser handles radar, rf, AND eo event types.
    # ------------------------------------------------------------------
    fuser_lock = threading.Lock()

    def locked_write(stream, line: bytes) -> None:
        with fuser_lock:
            try:
                stream.write(line)
                stream.flush()
            except BrokenPipeError:
                pass

    def fanout_eo() -> None:
        """Read EO output (radar pass-through + EO events); fan to rf.stdin and fuser.stdin."""
        try:
            for line in eo.stdout:
                if args.verbose:
                    print(f"[eo] {line.decode().rstrip()}", file=sys.stderr)
                # RF only cares about radar events (it ignores others silently)
                try:
                    rf.stdin.write(line)
                    rf.stdin.flush()
                except BrokenPipeError:
                    pass
                # Fuser gets everything (radar + EO events)
                locked_write(fuser.stdin, line)
        except Exception as e:
            print(f"[pipeline] fanout_eo error: {e}", file=sys.stderr)
        finally:
            try:
                rf.stdin.close()
            except Exception:
                pass

    def merge_rf() -> None:
        """Read rf output; forward to fuser.stdin, then close fuser.stdin."""
        try:
            for line in rf.stdout:
                if args.verbose:
                    print(f"[rf] {line.decode().rstrip()}", file=sys.stderr)
                locked_write(fuser.stdin, line)
        except Exception as e:
            print(f"[pipeline] merge_rf error: {e}", file=sys.stderr)
        finally:
            try:
                fuser.stdin.close()
            except Exception:
                pass

    t_fanout = threading.Thread(target=fanout_eo,  daemon=True, name="fanout-eo")
    t_merge  = threading.Thread(target=merge_rf,   daemon=True, name="merge-rf")

    t_fanout.start()
    t_merge.start()

    # ------------------------------------------------------------------
    # Wait for all processes
    # ------------------------------------------------------------------
    procs = [
        ("world",   world),
        ("radar",   radar),
        ("eo",      eo),
        ("rf",      rf),
        ("fuser",   fuser),
        ("cop_pub", cop_pub),
    ]

    try:
        for name, proc in procs:
            rc = proc.wait()
            print(f"[pipeline] {name} exited (rc={rc})", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[pipeline] Interrupted — terminating all agents...", file=sys.stderr)
        for name, proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass

    t_fanout.join(timeout=2)
    t_merge.join(timeout=2)

    print("[pipeline] Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
