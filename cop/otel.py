"""
cop/otel.py — OpenTelemetry Distributed Tracing

Optional instrumentation layer. If opentelemetry-sdk and
opentelemetry-exporter-otlp-proto-grpc are installed, traces are exported
to an OTLP-compatible backend (e.g. Jaeger). If packages are absent, all
functions here are harmless no-ops — the server starts normally without
any tracing.

Environment variables:
  OTEL_ENABLED          : "true" to activate (default: false)
  OTEL_SERVICE_NAME     : service name in traces (default: "nizam-cop")
  OTEL_EXPORTER_OTLP_ENDPOINT : OTLP endpoint (default: "http://jaeger:4317")

Usage in server.py:
    from cop.otel import init_tracing, span

    # Call once at startup (inside lifespan)
    init_tracing(app)

    # Wrap any code block with a span
    with span("my.operation", attributes={"track.id": tid}) as s:
        do_work()
        s.set_attribute("result", "ok")
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

_ENABLED = os.environ.get("OTEL_ENABLED", "false").lower() == "true"
_SERVICE  = os.environ.get("OTEL_SERVICE_NAME", "nizam-cop")
_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")

_tracer = None


def init_tracing(app=None) -> bool:
    """
    Initialise OpenTelemetry SDK and instrument FastAPI.

    Returns True if tracing was successfully enabled, False otherwise.
    """
    global _tracer
    if not _ENABLED:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": _SERVICE})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=_ENDPOINT, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(_SERVICE)

        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(app)
            except ImportError:
                pass   # opentelemetry-instrumentation-fastapi not installed

        return True

    except ImportError:
        return False   # OTel packages not installed — silent no-op


@contextmanager
def span(
    name: str,
    attributes: Optional[Dict[str, Any]] = None,
) -> Generator:
    """
    Context manager that creates a trace span if OTel is enabled.
    If OTel is disabled or unavailable, yields a dummy no-op object.

    Usage:
        with span("roe.evaluate", {"tracks": 12}) as s:
            result = do_work()
            s.set_attribute("advisories", len(result))
    """
    if _tracer is None:
        yield _NoOpSpan()
        return

    try:
        from opentelemetry import trace as _otrace
        with _tracer.start_as_current_span(name) as s:
            if attributes:
                for k, v in attributes.items():
                    s.set_attribute(k, v)
            yield s
    except Exception:
        yield _NoOpSpan()


class _NoOpSpan:
    """Dummy span returned when OTel is not available."""
    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass
    def set_status(self, *a, **kw) -> None:
        pass
    def record_exception(self, *a, **kw) -> None:
        pass


def is_enabled() -> bool:
    return _tracer is not None
