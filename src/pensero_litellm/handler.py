"""LiteLLM callback that sends one OpenTelemetry log event for each request and each applied file edit."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from pensero_litellm import __version__
from pensero_litellm.edits import count_edits
from pensero_litellm.exporter import LogExporter, make_exporter

logger = logging.getLogger(__name__)

REQUEST_EVENT = "ai_gateway.request"
FILE_EDIT_EVENT = "ai_gateway.file_edit"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cache_tokens(usage: Any) -> tuple[int, int]:
    """Cache read and cache write input tokens of a LiteLLM usage object."""
    if not isinstance(usage, dict):
        return 0, 0
    details = usage.get("prompt_tokens_details") or {}
    read = _int(usage.get("cache_read_input_tokens")) or _int(details.get("cached_tokens"))
    write = _int(usage.get("cache_creation_input_tokens")) or _int(details.get("cache_creation_tokens"))
    return read, write


def request_attributes(payload: dict) -> dict[str, Any]:
    """Attributes that both event types share: who sent the request, from which client, to which model."""
    metadata = payload.get("metadata") or {}
    attributes = {
        "gen_ai.response.id": payload.get("id") or payload.get("litellm_call_id"),
        "gen_ai.operation.name": payload.get("call_type"),
        "gen_ai.provider.name": payload.get("custom_llm_provider"),
        "gen_ai.request.model": payload.get("model_group") or payload.get("model"),
        "gen_ai.response.model": payload.get("model"),
        "user.id": metadata.get("user_api_key_user_id"),
        "user.email": metadata.get("user_api_key_user_email"),
        "user_agent.original": payload.get("user_agent") or metadata.get("user_agent"),
    }
    return {key: str(value) for key, value in attributes.items() if value}


class OTelLogger(CustomLogger):
    """LiteLLM `CustomLogger`. The OpenTelemetry pipeline starts on the first request.

    An error here is logged and never fails or slows the request: the events go to a queue, and a
    background thread sends them in batches.
    """

    def __init__(self, exporter: LogExporter | None = None) -> None:
        super().__init__()
        self._exporter = exporter
        self._logger = None

    def _otel_logger(self):
        if self._logger is None:
            resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME") or "litellm"})
            provider = LoggerProvider(resource=resource)
            provider.add_log_record_processor(BatchLogRecordProcessor(self._exporter or make_exporter()))
            self._provider = provider
            self._logger = provider.get_logger("pensero_litellm", __version__)
        return self._logger

    def force_flush(self) -> None:
        if self._logger is not None:
            self._provider.force_flush()

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
        try:
            self.emit(kwargs.get("standard_logging_object"), end_time)
        except Exception:
            logger.exception("pensero-litellm: could not send the usage of a request")

    def emit(self, payload: dict | None, end_time: datetime | None = None) -> None:
        if not payload:
            return
        timestamp = int(end_time.timestamp() * 1e9) if isinstance(end_time, datetime) else None
        shared = request_attributes(payload)
        cache_read, cache_write = _cache_tokens((payload.get("metadata") or {}).get("usage_object"))
        request = {
            **shared,
            "event.name": REQUEST_EVENT,
            "gen_ai.usage.input_tokens": _int(payload.get("prompt_tokens")),
            "gen_ai.usage.output_tokens": _int(payload.get("completion_tokens")),
            "gen_ai.usage.cache_read.input_tokens": cache_read,
            "gen_ai.usage.cache_creation.input_tokens": cache_write,
            "gen_ai.usage.cost": float(payload.get("response_cost") or 0.0),
        }
        otel_logger = self._otel_logger()
        otel_logger.emit(timestamp=timestamp, event_name=REQUEST_EVENT, attributes=request)
        for edit in count_edits(payload.get("messages")):
            otel_logger.emit(
                timestamp=timestamp,
                event_name=FILE_EDIT_EVENT,
                attributes={
                    **shared,
                    "event.name": FILE_EDIT_EVENT,
                    "gen_ai.tool.call.id": edit.call_id,
                    "gen_ai.tool.name": edit.tool,
                    "lines.added": edit.added,
                    "lines.deleted": edit.deleted,
                },
            )
