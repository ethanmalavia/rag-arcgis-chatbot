"""Lightweight tracing: structured logs + optional OpenTelemetry."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

from config import OTEL_ENABLED

logger = logging.getLogger("engestero.pipeline")

_tracer = None
if OTEL_ENABLED:
    try:
        from opentelemetry import trace

        _tracer = trace.get_tracer("engestero.rag")
    except ImportError:
        logger.warning("OTEL_ENABLED but opentelemetry not installed")


@contextmanager
def trace_span(name: str, attrs: dict[str, Any] | None = None) -> Iterator[None]:
    attrs = attrs or {}
    start = time.perf_counter()
    if _tracer is not None:
        with _tracer.start_as_current_span(name) as span:
            for k, v in attrs.items():
                span.set_attribute(k, str(v))
            yield
    else:
        yield
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("span=%s ms=%.1f attrs=%s", name, elapsed_ms, attrs)
