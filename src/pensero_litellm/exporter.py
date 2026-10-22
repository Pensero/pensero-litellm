"""Pick the OTLP/HTTP log exporter from the standard OpenTelemetry environment variables.

`http/protobuf` (the default) uses the exporter of the OpenTelemetry SDK. The SDK has no JSON exporter,
so `http/json` uses `JsonLogExporter`, which sends the same request encoded as OTLP JSON.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import requests
from google.protobuf.json_format import MessageToJson
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs.export import LogExportResult

try:
    from opentelemetry.sdk._logs.export import LogRecordExporter as LogExporter
except ImportError:
    from opentelemetry.sdk._logs.export import LogExporter
from opentelemetry.util.re import parse_env_headers

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://localhost:4318"
_TIMEOUT_SECONDS = 10.0


def _env(name: str) -> str:
    """The logs-specific variable `OTEL_EXPORTER_OTLP_LOGS_<name>`, else `OTEL_EXPORTER_OTLP_<name>`."""
    return os.environ.get(f"OTEL_EXPORTER_OTLP_LOGS_{name}") or os.environ.get(f"OTEL_EXPORTER_OTLP_{name}") or ""


def logs_endpoint() -> str:
    """The logs URL: the logs endpoint as is, else the base endpoint plus `/v1/logs`."""
    if endpoint := os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"):
        return endpoint
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or _DEFAULT_ENDPOINT
    return base.rstrip("/") + "/v1/logs"


class JsonLogExporter(LogExporter):
    """OTLP/HTTP JSON log exporter."""

    def __init__(self, endpoint: str | None = None, headers: dict[str, str] | None = None) -> None:
        self._endpoint = endpoint or logs_endpoint()
        self._headers = {
            **(headers if headers is not None else parse_env_headers(_env("HEADERS"), liberal=True)),
            "Content-Type": "application/json",
        }
        self._session = requests.Session()

    def export(self, batch: Sequence) -> LogExportResult:
        body = MessageToJson(encode_logs(batch), use_integers_for_enums=True, indent=None)
        try:
            response = self._session.post(self._endpoint, data=body, headers=self._headers, timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            logger.warning("pensero-litellm: export failed: %s", error)
            return LogExportResult.FAILURE
        if not response.ok:
            logger.warning("pensero-litellm: export failed with HTTP %s", response.status_code)
            return LogExportResult.FAILURE
        return LogExportResult.SUCCESS

    def shutdown(self) -> None:
        self._session.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def make_exporter() -> LogExporter:
    if _env("PROTOCOL") == "http/json":
        return JsonLogExporter()
    return OTLPLogExporter()
