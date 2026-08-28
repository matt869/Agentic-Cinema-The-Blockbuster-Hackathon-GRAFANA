"""OTLP/HTTP exporter setup and Grafana Cloud auth.

Builds the metric and log providers that ship stage telemetry to Grafana
Cloud. Auth is HTTP Basic over OTLP/HTTP: the instance ID is the username and
the access token is the password, base64-encoded into an ``Authorization``
header on the exporter. All credentials come from the environment -- nothing
is hardcoded and there is no offline fallback. If the environment is
incomplete we raise rather than guess.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource

# The stage emits at 1 Hz, so push at 1 Hz -- a fault that ramps over 40s needs
# per-second resolution to be visible as a ramp rather than three data points.
EXPORT_INTERVAL_MS = 1_000

_REQUIRED_VARS = (
    "GRAFANA_OTLP_ENDPOINT",
    "GRAFANA_OTLP_INSTANCE_ID",
    "GRAFANA_OTLP_TOKEN",
)


class JsonFormatter(logging.Formatter):
    """Structured stdout logging, per project conventions."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


def configure_stdout_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger, writing to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def load_credentials() -> dict[str, str]:
    """Read OTLP credentials from the environment. Raises if any are missing."""
    load_dotenv()
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in."
        )
    return {
        "endpoint": os.environ["GRAFANA_OTLP_ENDPOINT"].rstrip("/"),
        "instance_id": os.environ["GRAFANA_OTLP_INSTANCE_ID"],
        "token": os.environ["GRAFANA_OTLP_TOKEN"],
    }


def auth_headers() -> dict[str, str]:
    """HTTP Basic header: instance ID as user, access token as password.

    Built programmatically, so a literal space after ``Basic`` is correct. The
    ``Basic%20`` workaround in Grafana's quickstart applies only to the
    ``OTEL_EXPORTER_OTLP_HEADERS`` environment variable, which the OTel SDK
    URL-decodes; we never route auth through that path.
    """
    creds = load_credentials()
    raw = f"{creds['instance_id']}:{creds['token']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def build_meter_provider(
    resource: Resource, views: tuple[View, ...] = ()
) -> MeterProvider:
    """MeterProvider exporting to Grafana Cloud over OTLP/HTTP.

    Export failures are logged by the SDK's periodic reader and the loop
    continues -- a dropped batch must never stop the simulator.
    """
    creds = load_credentials()
    exporter = OTLPMetricExporter(
        endpoint=f"{creds['endpoint']}/v1/metrics",
        headers=auth_headers(),
    )
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=EXPORT_INTERVAL_MS
    )
    return MeterProvider(resource=resource, metric_readers=[reader], views=list(views))


def build_logger_provider(resource: Resource) -> LoggerProvider:
    """LoggerProvider exporting to Grafana Cloud (Loki) over OTLP/HTTP."""
    creds = load_credentials()
    exporter = OTLPLogExporter(
        endpoint=f"{creds['endpoint']}/v1/logs",
        headers=auth_headers(),
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def stage_resource(service_name: str) -> Resource:
    """Resource identifying one stage service.

    ``service.name`` is the one OTel resource attribute Grafana Cloud reliably
    promotes to a Loki index label, so it carries the service identity.
    """
    return Resource.create({"service.name": service_name})
