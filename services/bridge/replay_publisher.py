"""JSONL scenario recordings → NATS replay publisher.

`recordings/*.jsonl` içindeki kayıtlı senaryo snapshot'larını okuyup
NATS'e gerçek zamanlı (veya hızlandırılmış) yayınlar.

Kullanım:
    python -m services.bridge.replay_publisher recordings/my_recording.jsonl
    python -m services.bridge.replay_publisher --speed 5.0 --nats nats://localhost:6222 FILE
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from prometheus_client import Counter, start_http_server

log = logging.getLogger(__name__)
_replay_msgs = Counter("nizam_replay_messages_total", "Yeniden yayınlanan mesaj sayısı")


async def run(jsonl_path: Path, nats_url: str, speed: float = 1.0) -> None:
    import nats

    nc = await nats.connect(nats_url)
    log.info("Replay: %s  hız=%.2fx  NATS: %s", jsonl_path, speed, nats_url)

    prev_ts: float | None = None
    count = 0
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Snapshot içeriğinden track'leri sim.cop subject'ine yayınla
                for track in frame.get("tracks", []):
                    await nc.publish("nizam.raw.sim.cop", json.dumps(track).encode())
                    _replay_msgs.inc()
                    count += 1

                ts = float(frame.get("t", frame.get("timestamp", 0.0)))
                if prev_ts is not None and speed > 0:
                    delta = (ts - prev_ts) / speed
                    if 0 < delta < 10:   # mantıksız büyükse atla
                        await asyncio.sleep(delta)
                prev_ts = ts

        log.info("Replay tamamlandı — %d mesaj yayınlandı", count)
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM recording replay")
    parser.add_argument("file", type=Path, help="JSONL recording dosyası")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--metrics-port", type=int, default=8006)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(args.metrics_port)
    asyncio.run(run(args.file, args.nats, args.speed))


if __name__ == "__main__":
    main()
