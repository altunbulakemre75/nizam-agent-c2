"""
scripts/test_federation.py — MANET Federation Integration Test
==============================================================

Starts two COP nodes on localhost:8101 and localhost:8102, wires them
as bidirectional peers, and runs a full federation test suite:

  Step 1 — Normal sync: inject track on node-01, verify it propagates to node-02
  Step 2 — Zone sync: create a zone on node-01, verify it mirrors to node-02
  Step 3 — Partition simulation: disconnect peers, make divergent edits on both
            sides, reconnect, verify split-brain conflict is logged
  Step 4 — Reconnect healing: after partition, new tracks sync normally again

Network impairment: on Windows tc-netem is unavailable, so we simulate
packet loss by temporarily removing the peer registration on both nodes
and re-adding it after making divergent changes.  This exercises the
vector-clock concurrent-edit path and the partition-heal log.

Usage:
  python scripts/test_federation.py            # full run
  python scripts/test_federation.py --verbose  # print extra detail
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Ports for the two ephemeral nodes ─────────────────────────────────────────
PORT_01 = 8101
PORT_02 = 8102
URL_01  = f"http://127.0.0.1:{PORT_01}"
URL_02  = f"http://127.0.0.1:{PORT_02}"

SYNC_INTERVAL_S = 3   # faster sync for tests (set via env on both nodes)
PASS = "\u2713"
FAIL = "\u2717"


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}
    except Exception as e:
        return {"_error": str(e)}


def _post(url: str, body: dict, timeout: float = 5.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}
    except Exception as e:
        return {"_error": str(e)}


def _delete(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}
    except Exception as e:
        return {"_error": str(e)}


def _wait_ready(base_url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/metrics", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ── Node lifecycle ─────────────────────────────────────────────────────────────

def _start_node(port: int, node_id: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["COP_NODE_ID"]     = node_id
    env["SYNC_INTERVAL_S"] = str(SYNC_INTERVAL_S)
    env["AUTH_ENABLED"]    = "false"
    env["DB_ENABLED"]      = "false"
    # Disable tactical AI to keep startup fast
    env["AI_TACTICAL_INTERVAL_S"] = "9999"
    cmd = [sys.executable, str(ROOT / "cop" / "server.py")]
    # Use uvicorn directly as a module
    cmd = [
        sys.executable, "-m", "uvicorn",
        "cop.server:app",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
    ]
    return subprocess.Popen(
        cmd,
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── Test helpers ───────────────────────────────────────────────────────────────

_results: list[dict] = []

def _check(name: str, condition: bool, detail: str = "", verbose: bool = False) -> bool:
    sym = PASS if condition else FAIL
    print(f"  {sym} {name}", end="")
    if detail and (verbose or not condition):
        print(f"  [{detail}]", end="")
    print()
    _results.append({"name": name, "ok": condition, "detail": detail})
    return condition


def _ingest_track(base_url: str, track_id: str, lat: float, lon: float) -> dict:
    """Inject a minimal track record via /ingest."""
    return _post(f"{base_url}/ingest", {
        "event_type": "cop.track",
        "payload": {
            "id":          track_id,
            "lat":         lat,
            "lon":         lon,
            "alt_m":       1000.0,
            "speed_ms":    50.0,
            "heading_deg": 90.0,
            "track_type":  "uav",
            "source":      "test",
            "server_time": datetime.now(timezone.utc).isoformat(),
        },
    })


def _create_zone(base_url: str, zone_id: str, name: str, lat_offset: float = 0.0) -> dict:
    """Create a simple rectangular zone."""
    clat = 41.0 + lat_offset
    return _post(f"{base_url}/api/zones", {
        "id":   zone_id,
        "name": name,
        "type": "keep_out",
        "coordinates": [
            [clat - 0.01, 28.9],
            [clat + 0.01, 28.9],
            [clat + 0.01, 29.1],
            [clat - 0.01, 29.1],
        ],
    })


def _peer_add(base_url: str, peer_url: str) -> dict:
    return _post(f"{base_url}/api/sync/peers", {"url": peer_url, "action": "add"})


def _peer_remove(base_url: str, peer_url: str) -> dict:
    return _post(f"{base_url}/api/sync/peers", {"url": peer_url, "action": "remove"})


def _wait_for_track(base_url: str, track_id: str,
                    timeout: float = 15.0) -> bool:
    """Poll /api/tracks until track_id appears or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _get(f"{base_url}/api/tracks")
        if isinstance(resp.get("tracks"), list):
            if any(t.get("id") == track_id for t in resp["tracks"]):
                return True
        time.sleep(0.5)
    return False


def _wait_for_zone(base_url: str, zone_id: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _get(f"{base_url}/api/zones")
        if isinstance(resp.get("zones"), list):
            if any(z.get("id") == zone_id for z in resp["zones"]):
                return True
        time.sleep(0.5)
    return False


def _wait_for_conflict(base_url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _get(f"{base_url}/api/sync/conflicts")
        if isinstance(resp.get("count"), int) and resp["count"] > 0:
            return True
        time.sleep(0.5)
    return False


# ── Main test runner ──────────────────────────────────────────────────────────

def run_tests(verbose: bool) -> int:
    print()
    print("=" * 62)
    print("  NIZAM — MANET Federation Integration Test")
    print("=" * 62)

    # ── Startup ───────────────────────────────────────────────────
    print("\n[+] Starting node-01 on :%d ..." % PORT_01)
    p01 = _start_node(PORT_01, "node-01")
    print("[+] Starting node-02 on :%d ..." % PORT_02)
    p02 = _start_node(PORT_02, "node-02")

    try:
        print("[+] Waiting for both nodes to be ready ...")
        ok1 = _wait_ready(URL_01, timeout=45)
        ok2 = _wait_ready(URL_02, timeout=45)

        if not ok1 or not ok2:
            print(f"  {FAIL} Nodes failed to start within 45s — aborting")
            return 1

        print(f"  {PASS} node-01 ready ({URL_01})")
        print(f"  {PASS} node-02 ready ({URL_02})")

        # ── Wire bidirectional peering ─────────────────────────────
        print("\n[Step 0] Wiring bidirectional peers ...")
        r1 = _peer_add(URL_01, URL_02)
        r2 = _peer_add(URL_02, URL_01)
        _check("node-01 registered node-02 as peer",
               r1.get("ok") is True, str(r1), verbose)
        _check("node-02 registered node-01 as peer",
               r2.get("ok") is True, str(r2), verbose)

        # Give sync loop one cycle to settle
        time.sleep(SYNC_INTERVAL_S + 1)

        # ─────────────────────────────────────────────────────────────
        # STEP 1 — Track propagation (node-01 → node-02)
        # ─────────────────────────────────────────────────────────────
        print("\n[Step 1] Track propagation: node-01 → node-02")
        tid = "FED-TRACK-001"
        r = _ingest_track(URL_01, tid, lat=41.01, lon=28.98)
        _check("track injected on node-01",
               r.get("ok") is True or "_error" not in r, str(r), verbose)

        appeared = _wait_for_track(URL_02, tid, timeout=SYNC_INTERVAL_S * 4)
        _check("track propagated to node-02 within timeout", appeared,
               f"track_id={tid}", verbose)

        # ─────────────────────────────────────────────────────────────
        # STEP 2 — Zone propagation (node-02 → node-01)
        # ─────────────────────────────────────────────────────────────
        print("\n[Step 2] Zone propagation: node-02 → node-01")
        zid = "FED-ZONE-A"
        r = _create_zone(URL_02, zid, "Echo Zone")
        _check("zone created on node-02",
               r.get("ok") is True or "id" in r, str(r), verbose)

        appeared = _wait_for_zone(URL_01, zid, timeout=SYNC_INTERVAL_S * 4)
        _check("zone propagated to node-01 within timeout", appeared,
               f"zone_id={zid}", verbose)

        # ─────────────────────────────────────────────────────────────
        # STEP 3 — Partition simulation + split-brain conflict
        # ─────────────────────────────────────────────────────────────
        print("\n[Step 3] Partition simulation + split-brain conflict")
        print("  [sim] Disconnecting peers (network partition) ...")

        # Simulate packet loss: remove peer registration on both sides
        _peer_remove(URL_01, URL_02)
        _peer_remove(URL_02, URL_01)
        time.sleep(0.5)

        # Both nodes create the SAME zone ID with different names → conflict
        conflict_zone_id = "FED-ZONE-CONFLICT"
        print(f"  [sim] node-01 creates '{conflict_zone_id}' during partition ...")
        r_n1 = _create_zone(URL_01, conflict_zone_id, "Alpha-Node1")
        _check("node-01 created conflicting zone during partition",
               r_n1.get("ok") is True or "id" in r_n1, str(r_n1), verbose)

        # Small delay so server_time on node-02 is strictly newer
        time.sleep(1.2)

        print(f"  [sim] node-02 creates '{conflict_zone_id}' during partition ...")
        r_n2 = _create_zone(URL_02, conflict_zone_id, "Alpha-Node2", lat_offset=0.1)
        _check("node-02 created conflicting zone during partition",
               r_n2.get("ok") is True or "id" in r_n2, str(r_n2), verbose)

        # Reconnect peers — simulate network healing
        print("  [sim] Reconnecting peers (network healed) ...")
        _peer_add(URL_01, URL_02)
        _peer_add(URL_02, URL_01)

        # Wait for sync cycles to run and conflict to be logged
        conflict_logged = _wait_for_conflict(URL_01, timeout=SYNC_INTERVAL_S * 6)
        _check("split-brain conflict logged on node-01",
               conflict_logged, "check /api/sync/conflicts", verbose)

        if verbose and conflict_logged:
            c = _get(f"{URL_01}/api/sync/conflicts")
            print(f"  [detail] conflicts: {json.dumps(c.get('conflicts', []), indent=4)}")

        # ─────────────────────────────────────────────────────────────
        # STEP 4 — Post-partition sync health check
        # ─────────────────────────────────────────────────────────────
        print("\n[Step 4] Post-partition sync health check")
        tid2 = "FED-TRACK-POST-HEAL"
        r = _ingest_track(URL_01, tid2, lat=41.05, lon=29.0)
        _check("new track injected on node-01 after healing",
               r.get("ok") is True or "_error" not in r, str(r), verbose)

        appeared2 = _wait_for_track(URL_02, tid2, timeout=SYNC_INTERVAL_S * 4)
        _check("post-heal track propagated to node-02", appeared2,
               f"track_id={tid2}", verbose)

        # ─────────────────────────────────────────────────────────────
        # STEP 5 — Sync status introspection
        # ─────────────────────────────────────────────────────────────
        print("\n[Step 5] Sync status introspection")
        st1 = _get(f"{URL_01}/api/sync/status")
        st2 = _get(f"{URL_02}/api/sync/status")
        _check("node-01 reports peer_count >= 1",
               st1.get("peer_count", 0) >= 1, str(st1), verbose)
        _check("node-02 reports peer_count >= 1",
               st2.get("peer_count", 0) >= 1, str(st2), verbose)
        _check("node-01 node_id correct",
               st1.get("node_id") == "node-01", str(st1), verbose)
        _check("node-02 node_id correct",
               st2.get("node_id") == "node-02", str(st2), verbose)

        # ── Summary ────────────────────────────────────────────────────
        print()
        print("=" * 62)
        passed = sum(1 for r in _results if r["ok"])
        total  = len(_results)
        failed = total - passed

        if failed == 0:
            print(f"  {PASS} All {total} checks passed")
        else:
            print(f"  {FAIL} {failed}/{total} checks FAILED")
            for r in _results:
                if not r["ok"]:
                    print(f"      - {r['name']}  [{r['detail']}]")
        print("=" * 62)
        print()
        return 0 if failed == 0 else 1

    finally:
        print("[+] Stopping test nodes ...")
        p01.terminate()
        p02.terminate()
        try:
            p01.wait(timeout=5)
            p02.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p01.kill()
            p02.kill()
        print("[+] Done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MANET Federation Integration Test")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print extra detail on each check")
    args = ap.parse_args()

    # On Windows, set UTF-8 output so Unicode tick/cross render correctly
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sys.exit(run_tests(args.verbose))
