"""
start.py  —  NIZAM all-in-one launcher

Starts:
  1) COP server  (FastAPI + WebSocket)  on --cop_port  (default 8100)
  2) Agent pipeline  (world -> radar -> rf -> fuser -> cop_publisher)
  3) MQTT adapter  (optional, when --mqtt_broker is supplied)

Usage:
  python start.py
  python start.py --cop_port 8100 --duration_s 600 --rate_hz 2
  python start.py --open_browser

  # With external MQTT sensor feed:
  python start.py --mqtt_broker 192.168.1.10 --mqtt_topic sensors/tracks
  python start.py --mqtt_broker mqtt.example.com --mqtt_port 8883 \
                  --mqtt_topic sensors/# --mqtt_api_key YOUR_KEY

Ctrl+C stops everything cleanly.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent

# Load .env before anything else so LLM_PROVIDER, OLLAMA_URL etc. are visible
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Poll /api/metrics until the COP server responds or timeout expires."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{url}/api/metrics", timeout=2)
            return True
        except Exception:
            time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# COP server thread (uvicorn in-process)
# ---------------------------------------------------------------------------

def run_cop_server(host: str, port: int) -> None:
    import asyncio
    import uvicorn
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "cop.server:app",
        host=host,
        port=port,
        log_level="warning",
        reload=False,
    )


def run_orchestrator(host: str, port: int) -> None:
    import asyncio
    import uvicorn
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "orchestrator.app:app",
        host=host,
        port=port,
        log_level="warning",
        reload=False,
    )


# ---------------------------------------------------------------------------
# Pipeline subprocess (delegates to run_pipeline.py)
# ---------------------------------------------------------------------------

def run_cot_adapter(args: argparse.Namespace, cop_url: str) -> subprocess.Popen:
    """Launch adapters/cot_adapter.py — posts directly to COP via HTTP."""
    cmd = [
        sys.executable, str(ROOT / "adapters" / "cot_adapter.py"),
        "--source",  args.cot_source,
        "--cop_url", cop_url,
    ]
    if args.cot_source == "udp":
        cmd += ["--mcast_group", args.cot_mcast_group,
                "--udp_port",    str(args.cot_udp_port)]
    elif args.cot_source == "tcp":
        cmd += ["--tcp_host", args.cot_tcp_host,
                "--tcp_port", str(args.cot_tcp_port)]
    if args.cot_output_host:
        cmd += ["--cot_output_host",  args.cot_output_host,
                "--cot_output_port",  str(args.cot_output_port)]
        if args.cot_output_mcast:
            cmd.append("--cot_output_mcast")
    if args.mqtt_api_key:
        cmd += ["--api_key", args.mqtt_api_key]
    return subprocess.Popen(cmd, stderr=sys.stderr)


def run_ais_adapter(args: argparse.Namespace, cop_url: str) -> subprocess.Popen:
    """Launch adapters/ais_adapter.py — posts directly to COP via HTTP."""
    cmd = [
        sys.executable, str(ROOT / "adapters" / "ais_adapter.py"),
        "--source",  args.ais_source,
        "--lat_min", str(args.ais_lat_min),
        "--lat_max", str(args.ais_lat_max),
        "--lon_min", str(args.ais_lon_min),
        "--lon_max", str(args.ais_lon_max),
        "--cop_url", cop_url,
    ]
    if args.ais_source == "aisstream":
        if not args.ais_api_key:
            print("[start] WARNING: --ais_api_key missing for aisstream source", file=sys.stderr)
        cmd += ["--ais_api_key", args.ais_api_key]
    elif args.ais_source == "tcp":
        cmd += ["--host", args.ais_host, "--port", str(args.ais_port)]
    if args.mqtt_api_key:
        cmd += ["--api_key", args.mqtt_api_key]
    return subprocess.Popen(cmd, stderr=sys.stderr)


def run_adsb_adapter(args: argparse.Namespace, cop_url: str) -> subprocess.Popen:
    """Launch adapters/adsb_adapter.py — posts directly to COP via HTTP."""
    cmd = [
        sys.executable, str(ROOT / "adapters" / "adsb_adapter.py"),
        "--source",   args.adsb_source,
        "--lat",      str(args.adsb_lat),
        "--lon",      str(args.adsb_lon),
        "--radius_km", str(args.adsb_radius_km),
        "--interval", str(args.adsb_interval),
        "--cop_url",  cop_url,
    ]
    if args.mqtt_api_key:          # reuse the same ingest key if auth is on
        cmd += ["--api_key", args.mqtt_api_key]
    return subprocess.Popen(cmd, stderr=sys.stderr)


def run_mqtt_adapter(args: argparse.Namespace, cop_url: str) -> subprocess.Popen:
    """Launch adapters/mqtt_adapter.py — posts directly to COP via HTTP."""
    cmd = [
        sys.executable, str(ROOT / "adapters" / "mqtt_adapter.py"),
        "--broker", args.mqtt_broker,
        "--port",   str(args.mqtt_port),
        "--cop_url", cop_url,
    ]
    for topic in (args.mqtt_topic or ["nizam/tracks"]):
        cmd += ["--topic", topic]
    if args.mqtt_api_key:
        cmd += ["--api_key", args.mqtt_api_key]
    return subprocess.Popen(cmd, stderr=sys.stderr)


def run_pipeline(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [
        sys.executable, str(ROOT / "run_pipeline.py"),
        "--cop_url",          f"http://127.0.0.1:{args.cop_port}",
        "--orchestrator_url", f"http://127.0.0.1:{args.orch_port}",
        "--origin_lat",       str(args.origin_lat),
        "--origin_lon",       str(args.origin_lon),
        "--duration_s",       str(args.duration_s),
        "--rate_hz",          str(args.rate_hz),
    ]
    if args.scenario:
        cmd += ["--scenario", args.scenario]
    if args.log_out:
        cmd += ["--log_out", args.log_out]
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
    ap.add_argument("--orch_port",   type=int,   default=8200)
    ap.add_argument("--origin_lat",  type=float, default=41.015)
    ap.add_argument("--origin_lon",  type=float, default=28.979)
    ap.add_argument("--duration_s",  type=float, default=300.0)
    ap.add_argument("--rate_hz",     type=float, default=1.0)
    ap.add_argument("--scenario",    default=None, help="Path to scenario JSON (e.g. scenarios/swarm_attack.json)")
    ap.add_argument("--log_out",     default=None, help="Save events to JSONL for replay (e.g. logs/run.jsonl)")
    ap.add_argument("--open_browser", action="store_true", help="Open COP UI in default browser")
    ap.add_argument("--verbose",     action="store_true")
    # MQTT adapter (optional external sensor feed)
    ap.add_argument("--mqtt_broker",  default=None,
                    help="MQTT broker hostname — enables MQTT adapter (e.g. 192.168.1.10)")
    ap.add_argument("--mqtt_port",    type=int, default=1883,
                    help="MQTT broker port (default 1883; use 8883 for TLS)")
    ap.add_argument("--mqtt_topic",   action="append", default=None,
                    help="MQTT topic to subscribe (repeatable; default: nizam/tracks)")
    ap.add_argument("--mqtt_api_key", default="",
                    help="X-API-Key value for COP /api/ingest (when AUTH is enabled)")
    # CoT/ATAK adapter (optional TAK device feed)
    ap.add_argument("--cot_source",
                    choices=["udp", "tcp"],
                    default=None,
                    help="Enable CoT/ATAK adapter (udp=multicast SA, tcp=TAK Server)")
    ap.add_argument("--cot_mcast_group", default="239.2.3.1",
                    help="UDP multicast group for CoT SA (default: 239.2.3.1)")
    ap.add_argument("--cot_udp_port",    type=int, default=4242,
                    help="UDP port for CoT SA (default: 4242)")
    ap.add_argument("--cot_tcp_host",    default="localhost",
                    help="TAK Server host (--cot_source tcp)")
    ap.add_argument("--cot_tcp_port",    type=int, default=8087,
                    help="TAK Server TCP port (default: 8087)")
    ap.add_argument("--cot_output_host", default="",
                    help="Echo NIZAM tracks back as CoT SA to this host/group")
    ap.add_argument("--cot_output_port", type=int, default=6969,
                    help="CoT SA output port (default: 6969)")
    ap.add_argument("--cot_output_mcast", action="store_true",
                    help="Use multicast for CoT SA output")
    # AIS maritime adapter (optional real vessel feed)
    ap.add_argument("--ais_source",
                    choices=["aisstream", "tcp"],
                    default=None,
                    help="Enable AIS adapter (aisstream=aisstream.io WebSocket, tcp=NMEA TCP)")
    ap.add_argument("--ais_api_key", default="",
                    help="aisstream.io API key (free at aisstream.io)")
    ap.add_argument("--ais_host",    default="127.0.0.1", help="AIS TCP host")
    ap.add_argument("--ais_port",    type=int, default=10110, help="AIS TCP port")
    ap.add_argument("--ais_lat_min", type=float, default=36.0)
    ap.add_argument("--ais_lat_max", type=float, default=42.5)
    ap.add_argument("--ais_lon_min", type=float, default=26.0)
    ap.add_argument("--ais_lon_max", type=float, default=45.0)
    # ADS-B adapter (optional real aircraft feed)
    ap.add_argument("--adsb_source",
                    choices=["opensky", "adsbfi", "airplaneslive", "dump1090"],
                    default=None,
                    help="Enable ADS-B adapter with this source (e.g. adsbfi)")
    ap.add_argument("--adsb_lat",      type=float, default=41.015,
                    help="ADS-B bounding-box centre latitude")
    ap.add_argument("--adsb_lon",      type=float, default=28.979,
                    help="ADS-B bounding-box centre longitude")
    ap.add_argument("--adsb_radius_km", type=float, default=200.0,
                    help="ADS-B bounding-box radius in km")
    ap.add_argument("--adsb_interval", type=float, default=10.0,
                    help="ADS-B poll interval in seconds (default 10)")
    args = ap.parse_args()

    ui_url   = f"http://127.0.0.1:{args.cop_port}"
    orch_url = f"http://127.0.0.1:{args.orch_port}"

    # -- 0) Pass scenario name to COP server via env -------------------------
    if args.scenario:
        scenario_label = Path(args.scenario).stem
        os.environ["NIZAM_SCENARIO"] = scenario_label

    # -- 1a) Orchestrator in a daemon thread --------------------------------
    threading.Thread(
        target=run_orchestrator,
        args=(args.cop_host, args.orch_port),
        daemon=True,
        name="orchestrator",
    ).start()
    print(f"[start] Orchestrator starting on {orch_url} ...", file=sys.stderr)

    # -- 1b) COP server in a daemon thread ----------------------------------
    threading.Thread(
        target=run_cop_server,
        args=(args.cop_host, args.cop_port),
        daemon=True,
        name="cop-server",
    ).start()
    print(f"[start] COP server starting on {ui_url} ...", file=sys.stderr)

    # Wait until COP server is actually accepting requests before starting pipeline
    print(f"[start] Waiting for COP server on {ui_url} ...", file=sys.stderr)
    if _wait_for_server(ui_url, timeout=30.0):
        print(f"[start] COP server ready. Orchestrator: {orch_url}", file=sys.stderr)
    else:
        print(f"[start] WARNING: COP server did not respond within 30 s — pipeline may fail", file=sys.stderr)

    # -- 2) Open browser (optional) -----------------------------------------
    if args.open_browser:
        webbrowser.open(ui_url)
        print(f"[start] Browser opened: {ui_url}", file=sys.stderr)
    else:
        print(f"[start] Open browser at: {ui_url}", file=sys.stderr)

    # -- 3) Agent pipeline ---------------------------------------------------
    print("[start] Starting agent pipeline...", file=sys.stderr)
    pipeline = run_pipeline(args)

    # -- 4) CoT/ATAK adapter (optional) -------------------------------------
    cot_proc = None
    if args.cot_source:
        print(f"[start] Starting CoT/ATAK adapter  source={args.cot_source}", file=sys.stderr)
        cot_proc = run_cot_adapter(args, ui_url)

    # -- 5) AIS adapter (optional) ------------------------------------------
    ais_proc = None
    if args.ais_source:
        print(f"[start] Starting AIS adapter  source={args.ais_source}", file=sys.stderr)
        ais_proc = run_ais_adapter(args, ui_url)

    # -- 5) MQTT adapter (optional) -----------------------------------------
    mqtt_proc = None
    if args.mqtt_broker:
        topics = args.mqtt_topic or ["nizam/tracks"]
        print(
            f"[start] Starting MQTT adapter → {args.mqtt_broker}:{args.mqtt_port}"
            f"  topics={topics}",
            file=sys.stderr,
        )
        mqtt_proc = run_mqtt_adapter(args, ui_url)

    # -- 5) ADS-B adapter (optional) ----------------------------------------
    adsb_proc = None
    if args.adsb_source:
        print(
            f"[start] Starting ADS-B adapter  source={args.adsb_source}"
            f"  radius={args.adsb_radius_km}km  interval={args.adsb_interval}s",
            file=sys.stderr,
        )
        adsb_proc = run_adsb_adapter(args, ui_url)

    # -- 6) Wait / handle Ctrl+C --------------------------------------------
    try:
        pipeline.wait()
    except KeyboardInterrupt:
        print("\n[start] Interrupted — shutting down...", file=sys.stderr)
        pipeline.terminate()
        try:
            pipeline.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pipeline.kill()
    finally:
        for proc in (cot_proc, ais_proc, mqtt_proc, adsb_proc):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    print("[start] Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
