"""
start.py  —  NIZAM all-in-one launcher

Starts:
  1) COP server  (FastAPI + WebSocket)  on --cop_port  (default 8100)
  2) Agent pipeline  (world -> radar -> rf -> fuser -> cop_publisher)

Usage:
  python start.py
  python start.py --cop_port 8100 --duration_s 600 --rate_hz 2
  python start.py --open_browser

Ctrl+C stops everything cleanly.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# COP server thread (uvicorn in-process)
# ---------------------------------------------------------------------------

def run_cop_server(host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(
        "cop.server:app",
        host=host,
        port=port,
        log_level="warning",   # keep stderr clean
        reload=False,
    )


# ---------------------------------------------------------------------------
# Pipeline subprocess (delegates to run_pipeline.py)
# ---------------------------------------------------------------------------

def run_pipeline(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable, str(ROOT / "run_pipeline.py"),
        "--cop_url",    f"http://127.0.0.1:{args.cop_port}",
        "--origin_lat", str(args.origin_lat),
        "--origin_lon", str(args.origin_lon),
        "--duration_s", str(args.duration_s),
        "--rate_hz",    str(args.rate_hz),
    ]
    if args.verbose:
        cmd.append("--verbose")
    return subprocess.Popen(cmd, stderr=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="NIZAM all-in-one launcher")
    ap.add_argument("--cop_host",    default="0.0.0.0")
    ap.add_argument("--cop_port",    type=int,   default=8100)
    ap.add_argument("--origin_lat",  type=float, default=41.015)
    ap.add_argument("--origin_lon",  type=float, default=28.979)
    ap.add_argument("--duration_s",  type=float, default=300.0)
    ap.add_argument("--rate_hz",     type=float, default=1.0)
    ap.add_argument("--open_browser", action="store_true", help="Open COP UI in default browser")
    ap.add_argument("--verbose",     action="store_true")
    args = ap.parse_args()

    ui_url = f"http://127.0.0.1:{args.cop_port}"

    # -- 1) COP server in a daemon thread -----------------------------------
    server_thread = threading.Thread(
        target=run_cop_server,
        args=(args.cop_host, args.cop_port),
        daemon=True,
        name="cop-server",
    )
    server_thread.start()
    print(f"[start] COP server starting on {ui_url} ...", file=sys.stderr)

    # Give uvicorn a moment to bind the port before pipeline starts posting
    time.sleep(1.5)
    print(f"[start] COP server ready.", file=sys.stderr)

    # -- 2) Open browser (optional) -----------------------------------------
    if args.open_browser:
        webbrowser.open(ui_url)
        print(f"[start] Browser opened: {ui_url}", file=sys.stderr)
    else:
        print(f"[start] Open browser at: {ui_url}", file=sys.stderr)

    # -- 3) Agent pipeline ---------------------------------------------------
    print("[start] Starting agent pipeline...", file=sys.stderr)
    pipeline = run_pipeline(args)

    # -- 4) Wait / handle Ctrl+C --------------------------------------------
    try:
        pipeline.wait()
    except KeyboardInterrupt:
        print("\n[start] Interrupted — shutting down pipeline...", file=sys.stderr)
        pipeline.terminate()
        try:
            pipeline.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pipeline.kill()

    print("[start] Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
