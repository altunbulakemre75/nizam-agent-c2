"""
scripts/smoke_test.py — End-to-end smoke test for the NIZAM COP pipeline.

What it does:
  1. Starts the COP server (uvicorn) as a subprocess.
  2. Waits for /api/metrics to become reachable.
  3. POSTs /api/reset to clear state.
  4. Runs run_pipeline.py against scenarios/single_drone.json for ~15s.
  5. Reads /api/metrics and asserts:
       - ingest_total > 0
       - tactical.ran > 0
       - tactical.failed == 0
       - no tactical.overlap_skipped under a trivial 1-drone load
  6. Tears the server down.

Exit 0 on success, non-zero on any failure (so CI can gate on it).

This covers the hot path the unit tests CAN'T touch: uvicorn lifecycle,
real /ingest POSTs from cop_publisher, the async refactor both ends (server
tactical offload + publisher thread pool), and the new metrics endpoint.

Usage:
  python scripts/smoke_test.py [--port 8100] [--duration 15]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _http_get(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_post(url: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _wait_for_server(metrics_url: str, total_timeout: float = 30.0) -> bool:
    """Poll /api/metrics until it answers or we give up."""
    deadline = time.time() + total_timeout
    last_err: str = ""
    while time.time() < deadline:
        try:
            _http_get(metrics_url, timeout=2.0)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionResetError) as e:
            last_err = str(e)
            time.sleep(0.5)
    print(f"[smoke] server did not come up within {total_timeout}s: {last_err}",
          file=sys.stderr)
    return False


def _fail(msg: str) -> "NoReturn":
    print(f"[smoke] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--duration", type=float, default=15.0,
                    help="Pipeline duration in seconds")
    ap.add_argument("--scenario", default="scenarios/single_drone.json")
    args = ap.parse_args()

    base_url    = f"http://127.0.0.1:{args.port}"
    metrics_url = f"{base_url}/api/metrics"
    reset_url   = f"{base_url}/api/reset"

    # 1) Boot the COP server.
    print(f"[smoke] launching COP server on :{args.port} ...", flush=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cop.server:app",
         "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    try:
        # 2) Wait until /api/metrics responds.
        if not _wait_for_server(metrics_url):
            _fail("server never answered /api/metrics")

        print("[smoke] server is up, resetting state", flush=True)
        try:
            _http_post(reset_url)
        except Exception as e:
            _fail(f"reset failed: {e}")

        # Brief grace so reset fully settles.
        time.sleep(0.5)

        # 3) Run the pipeline against a simple scenario.
        scenario_path = ROOT / args.scenario
        if not scenario_path.exists():
            _fail(f"scenario not found: {scenario_path}")

        print(f"[smoke] running pipeline: {args.scenario} for {args.duration}s",
              flush=True)
        t0 = time.time()
        pipe_rc = subprocess.run(
            [sys.executable, str(ROOT / "run_pipeline.py"),
             "--cop_url",    base_url,
             "--duration_s", str(args.duration),
             "--rate_hz",    "1.0",
             "--scenario",   str(scenario_path)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        ).returncode
        elapsed = time.time() - t0
        print(f"[smoke] pipeline exited rc={pipe_rc} in {elapsed:.1f}s",
              flush=True)
        if pipe_rc != 0:
            _fail(f"pipeline exited with rc={pipe_rc}")

        # 4) Give the tactical engine a moment to drain.
        time.sleep(2.0)

        # 5) Assert on metrics.
        try:
            metrics = _http_get(metrics_url, timeout=5.0)
        except Exception as e:
            _fail(f"could not read metrics after pipeline: {e}")

        print("[smoke] metrics after run:", flush=True)
        print(json.dumps(metrics, indent=2), flush=True)

        ingest_total = metrics["ingest"]["total"]
        if ingest_total <= 0:
            _fail(f"ingest.total is {ingest_total}, expected > 0")

        tactical = metrics["tactical"]
        if tactical["ran"] <= 0:
            _fail(f"tactical.ran is {tactical['ran']}, expected > 0")
        if tactical["failed"] != 0:
            _fail(f"tactical.failed is {tactical['failed']}, expected 0")
        if tactical["overlap_skipped"] != 0:
            # A single-drone 15s run should never trigger overlap — if it
            # does, either our tactical engine got much slower or the
            # bg-lock logic regressed.
            _fail(f"tactical.overlap_skipped is {tactical['overlap_skipped']}, "
                  f"expected 0 under single_drone load")

        # 6) State check.
        state = metrics["state"]
        if state["tracks"] <= 0:
            _fail(f"state.tracks is {state['tracks']}, expected > 0")

        print("[smoke] PASS — all assertions held", flush=True)
        sys.exit(0)

    finally:
        print("[smoke] shutting down server", flush=True)
        try:
            server_proc.terminate()
            server_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=5.0)


if __name__ == "__main__":
    main()
