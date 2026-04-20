"""JSON structured logging — Loki/Promtail için uygun format.

Kullanım:
    from shared.logging_setup import setup_logging
    setup_logging("fusion_service")
    logging.getLogger(__name__).info("tick done", extra={"dt_ms": 2.3})

Loki'ye push: Promtail docker konteynerı stdout'u tail eder ve
json_mode ile field'ları çıkarır (infra/promtail/config.yml).
Servisler sadece stdout'a yazar, Loki'ye direkt TCP yok.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Her log satırı tek JSON line — Loki/Promtail pipeline_stages için."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)

        # extra={"key": value} desteği
        reserved = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        for k, v in record.__dict__.items():
            if k not in reserved and not k.startswith("_"):
                base[k] = v
        return json.dumps(base, ensure_ascii=False, default=str)


def setup_logging(service_name: str, level: str | None = None) -> None:
    """Root logger'ı JSON formatter + stdout'a yönlendir."""
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service_name))
    root = logging.getLogger()
    root.setLevel(lvl)
    root.handlers = [handler]
