"""Opt-in OpenTelemetry helpers for the CWAtlas dashboard.

Telemetry is deliberately narrow and sanitized: endpoint names, status codes,
panel error counts, and request/summary durations only. It does not export
request bodies, query strings, command lines, catalog rows, SDR payloads, or
journal text.
"""
from __future__ import annotations

import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any


@dataclass
class _NoopTelemetry:
    enabled: bool = False

    def span(self, _name: str, **_attrs: Any):
        return nullcontext()

    def record_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_summary(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _OtelTelemetry:
    enabled = True

    def __init__(self) -> None:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318").rstrip("/")
        interval_ms = int(os.environ.get("CWATLAS_DASH_OTEL_EXPORT_INTERVAL_MS", "15000"))
        resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "cwatlas-dash"),
            "service.namespace": "cwatlas",
            "service.instance.id": os.uname().nodename,
        })

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
            export_interval_millis=interval_ms,
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
        self._meter = metrics.get_meter("cwatlas_dash")
        self._requests = self._meter.create_counter(
            "cwatlas_dash.requests",
            description="Sanitized Flask request count by endpoint and status class.",
        )
        self._request_duration = self._meter.create_histogram(
            "cwatlas_dash.request.duration",
            unit="s",
            description="Sanitized Flask request duration by endpoint and status class.",
        )
        self._summary_duration = self._meter.create_histogram(
            "cwatlas_dash.summary.duration",
            unit="s",
            description="Time spent building /api/summary payload.",
        )
        self._summary_panel_errors = self._meter.create_counter(
            "cwatlas_dash.summary.panel_errors",
            description="Count of degraded /api/summary panels by panel name.",
        )

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
        trace.set_tracer_provider(tracer_provider)
        self._tracer = trace.get_tracer("cwatlas_dash")

    def span(self, name: str, **attrs: Any):
        return self._tracer.start_as_current_span(name, attributes={k: v for k, v in attrs.items() if v is not None})

    def record_request(self, endpoint: str, status_code: int, duration_s: float) -> None:
        attrs = {
            "http.route": endpoint or "unknown",
            "http.status_class": f"{status_code // 100}xx",
        }
        self._requests.add(1, attrs)
        self._request_duration.record(duration_s, attrs)

    def record_summary(self, payload: dict[str, Any], duration_s: float) -> None:
        self._summary_duration.record(duration_s, {})
        for panel in ("service", "sdr", "adc", "totals", "hourly", "inflight", "solar", "journal"):
            value = payload.get(panel)
            if isinstance(value, dict) and "error" in value:
                self._summary_panel_errors.add(1, {"panel": panel})
        windows = payload.get("windows")
        if isinstance(windows, dict):
            for window, value in windows.items():
                if isinstance(value, dict) and "error" in value:
                    self._summary_panel_errors.add(1, {"panel": "windows", "window": str(window)})


_TELEMETRY: _NoopTelemetry | _OtelTelemetry | None = None


def get_telemetry() -> _NoopTelemetry | _OtelTelemetry:
    global _TELEMETRY
    if _TELEMETRY is not None:
        return _TELEMETRY
    if os.environ.get("CWATLAS_DASH_OTEL_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
        _TELEMETRY = _NoopTelemetry()
        return _TELEMETRY
    try:
        _TELEMETRY = _OtelTelemetry()
    except Exception as exc:  # noqa: BLE001 - telemetry must not break the dashboard
        print(f"CWAtlas dashboard OpenTelemetry disabled after init error: {type(exc).__name__}: {exc}", flush=True)
        _TELEMETRY = _NoopTelemetry()
    return _TELEMETRY


def request_start_time() -> float:
    return time.perf_counter()
