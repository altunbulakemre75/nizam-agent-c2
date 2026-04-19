"""CoT servisini çalıştır: `python -m services.cot`."""
from __future__ import annotations

import argparse
import asyncio
import logging

from services.cot.workers import run_pytak_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="NIZAM CoT → TAK pipeline")
    parser.add_argument("--nats", default="nats://localhost:6222")
    parser.add_argument("--tak-host", default="localhost")
    parser.add_argument("--tak-port", type=int, default=8087)
    parser.add_argument("--ref-lat", type=float, default=39.9334)
    parser.add_argument("--ref-lon", type=float, default=32.8597)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_pytak_pipeline(
        args.nats, args.tak_host, args.tak_port, args.ref_lat, args.ref_lon,
    ))


if __name__ == "__main__":
    main()
