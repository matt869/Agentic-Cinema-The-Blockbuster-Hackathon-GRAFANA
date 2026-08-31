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

log = logging.getLogger("simulator.otlp")

_REQUIRED_VARS = (
    "GRAFANA_OTLP_ENDPOINT",
    "GRAFANA_OTLP_INSTANCE_ID",
    "GRAFANA_OTLP_TOKEN",
)

#: Which stage produced a sample. A deployed Space and a laptop write the same
#: metric names, with the same entity labels, into the same Grafana stack --
#: node_07 from one is indistinguishable from node_07 from the other. Without
#: this label the two stages merge into a single fictional stage: the agent
#: reads interleaved samples from two independent random walks, and a
#: before/after comparison measures nothing.
#:
#: Defaults to "local" so a laptop needs no configuration; a deployment sets
#: DEPLOYMENT_ENV to something else ("space") to claim its own series.
DEPLOYMENT: str = os.environ.get("DEPLOYMENT_ENV", "").strip() or "local"


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


def clean_env(name: str) -> str:
    """One environment value, with the damage a paste-in-a-box UI does undone.

    Render, Spaces and every other dashboard that takes secrets in a text
    field routinely deliver the value with a trailing newline, a stray space,
    or wrapped in the quotes someone copied along with it. None of that is
    visible in the UI and all of it survives into os.environ.

    It matters because these strings are concatenated into a URL. Measured
    against the real Grafana gateway: the correct path answers 200 for
    metrics and 204 for logs, while a SINGLE trailing space answers 404 --
    and a 404 per batch is logged by the SDK and swallowed, so the app keeps
    serving happily while nothing is exported at all. Stripping here is the
    difference between a demo that works and one that looks like it does.
    """
    return os.environ.get(name, "").strip().strip("\"'").strip()


#: Signal paths the gateway serves under /otlp. Stripped before normalising,
#: because Grafana's own quickstart displays the full metrics URL and people
#: paste what they are shown.
_SIGNAL_PATHS = ("/v1/metrics", "/v1/logs", "/v1/traces")


def normalise_endpoint(raw: str) -> str:
    """The OTLP base URL, from any of the forms people actually paste.

    The gateway answers 404 -- never a useful error -- for every wrong shape,
    and the SDK swallows it per batch, so a mistake here is silent. Measured
    against the live gateway: the correct path returns 200 for metrics and
    204 for logs; a trailing space, a missing /otlp, a doubled /otlp and a
    trailing slash all return 404. Auth problems return 401, so a 404 always
    means the path, never the credentials.

    Accepts the base with or without /otlp, with or without a signal path
    already appended, and normalises all of them to the one working base.
    """
    endpoint = raw.rstrip("/")
    for signal in _SIGNAL_PATHS:
        if endpoint.endswith(signal):
            endpoint = endpoint[: -len(signal)].rstrip("/")
            break
    if not endpoint.endswith("/otlp"):
        endpoint += "/otlp"
    return endpoint


def load_credentials() -> dict[str, str]:
    """Read OTLP credentials from the environment. Raises if any are missing."""
    load_dotenv()
    # Whitespace-only counts as missing: an env var set to " " is a mistake,
    # not a credential, and failing here names it instead of 404ing forever.
    missing = [v for v in _REQUIRED_VARS if not clean_env(v)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in."
        )
    return {
        "endpoint": normalise_endpoint(clean_env("GRAFANA_OTLP_ENDPOINT")),
        "instance_id": clean_env("GRAFANA_OTLP_INSTANCE_ID"),
        "token": clean_env("GRAFANA_OTLP_TOKEN"),
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
    url = f"{creds['endpoint']}/v1/metrics"
    # Say where the batches are going, once, at startup. An export failure is
    # logged per batch by the SDK and never mentions the URL, so without this
    # a 404 caused by a mangled endpoint looks identical to one caused by bad
    # credentials -- and the first thing you need is the URL actually in use.
    log.info("exporting metrics to %s", url)
    exporter = OTLPMetricExporter(endpoint=url, headers=auth_headers())
    reader = PeriodicExportingMetricReader(
        exporter, export_interval_millis=EXPORT_INTERVAL_MS
    )
    return MeterProvider(resource=resource, metric_readers=[reader], views=list(views))


def build_logger_provider(resource: Resource) -> LoggerProvider:
    """LoggerProvider exporting to Grafana Cloud (Loki) over OTLP/HTTP."""
    creds = load_credentials()
    url = f"{creds['endpoint']}/v1/logs"
    # Debug, not info: one provider is built per stage service, so info here
    # would print the same line three times on every start.
    log.debug("exporting logs to %s", url)
    exporter = OTLPLogExporter(endpoint=url, headers=auth_headers())
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def stage_resource(service_name: str) -> Resource:
    """Resource identifying one stage service.

    ``service.name`` is the one OTel resource attribute Grafana Cloud reliably
    promotes to a Loki index label, so it carries the service identity.
    """
    return Resource.create({"service.name": service_name})
