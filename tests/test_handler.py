import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import litellm
import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from pensero_litellm import OTelLogger
from pensero_litellm.exporter import JsonLogExporter, make_exporters

EDIT_REQUEST = [
    {"role": "user", "content": "edit"},
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Edit",
                "input": {"file_path": "secret/a.py", "old_string": "a\nb", "new_string": "a\nc\nd"},
            }
        ],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
]


@pytest.fixture
def exported():
    exporter = InMemoryLogRecordExporter()
    handler = OTelLogger(exporter)
    litellm.callbacks = [handler]
    yield lambda: (handler.force_flush(), [r.log_record for r in exporter.get_finished_logs()])[1]
    litellm.callbacks = []


async def test_claude_code_request_through_litellm(exported):
    await litellm.anthropic.messages.acreate(
        model="anthropic/claude-sonnet-4-5", messages=EDIT_REQUEST, max_tokens=10, mock_response="ok"
    )
    await litellm.acompletion(model="openai/gpt-5", messages=[{"role": "user", "content": "hi"}], mock_response="ok")
    import asyncio

    await asyncio.sleep(1)
    records = exported()
    events = [r.attributes["event.name"] for r in records]
    assert events == ["ai_gateway.request", "ai_gateway.file_edit", "ai_gateway.request"]
    request, edit, chat = records
    assert request.event_name == "ai_gateway.request"
    assert request.attributes["gen_ai.request.model"] == "anthropic/claude-sonnet-4-5"
    assert request.attributes["gen_ai.usage.output_tokens"] > 0
    assert edit.attributes["gen_ai.tool.call.id"] == "toolu_1"
    assert (edit.attributes["lines.added"], edit.attributes["lines.deleted"]) == (2, 1)
    assert edit.attributes["gen_ai.response.id"] == request.attributes["gen_ai.response.id"]
    assert chat.attributes["gen_ai.usage.cost"] > 0
    sent = json.dumps([dict(r.attributes) for r in records])
    assert "secret/a.py" not in sent and "new_string" not in sent


def test_emit_maps_litellm_payload():
    exporter = InMemoryLogRecordExporter()
    handler = OTelLogger(exporter)
    handler.emit(
        {
            "id": "msg_1",
            "call_type": "anthropic_messages",
            "custom_llm_provider": "anthropic",
            "model": "claude-sonnet-4-5-20250929",
            "model_group": "claude-sonnet-4-5",
            "prompt_tokens": 1500,
            "completion_tokens": 200,
            "response_cost": 0.0123,
            "user_agent": "claude-cli/2.0.14 (external, cli)",
            "metadata": {
                "user_api_key_user_id": "u1",
                "user_api_key_user_email": "ana@acme.com",
                "usage_object": {"cache_read_input_tokens": 1000, "cache_creation_input_tokens": 300},
            },
            "messages": [],
        }
    )
    handler.force_flush()
    (record,) = [r.log_record for r in exporter.get_finished_logs()]
    assert dict(record.attributes) == {
        "gen_ai.response.id": "msg_1",
        "gen_ai.operation.name": "anthropic_messages",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-sonnet-4-5",
        "gen_ai.response.model": "claude-sonnet-4-5-20250929",
        "user.id": "u1",
        "user.email": "ana@acme.com",
        "user_agent.original": "claude-cli/2.0.14 (external, cli)",
        "event.name": "ai_gateway.request",
        "gen_ai.usage.input_tokens": 1500,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "gen_ai.usage.cache_creation.input_tokens": 300,
        "gen_ai.usage.cost": 0.0123,
    }


def test_emit_never_raises_into_litellm(caplog):
    class Broken(OTelLogger):
        def emit(self, payload, end_time=None):
            raise RuntimeError("boom")

    import asyncio

    asyncio.run(Broken().async_log_success_event({"standard_logging_object": {"id": "x"}}, None, None, None))
    assert "could not send" in caplog.text


@pytest.mark.parametrize(
    ("value", "kinds"),
    [
        (None, ["OTLPLogExporter"]),
        ("console", ["Console"]),
        ("console, otlp", ["Console", "OTLPLogExporter"]),
        ("none", []),
    ],
)
def test_otel_logs_exporter_selects_the_exporters(monkeypatch, value, kinds):
    if value is None:
        monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    else:
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", value)

    names = [type(e).__name__ for e in make_exporters()]

    assert [next(k for k in kinds if k in n) for n in names] == kinds


def test_console_prints_the_events_and_sends_nothing(monkeypatch, capfd):
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "console")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")

    handler = OTelLogger()
    handler.emit({"id": "r1", "model": "gpt-5", "prompt_tokens": 10, "completion_tokens": 5, "messages": EDIT_REQUEST})
    handler.force_flush()

    out = capfd.readouterr().out
    assert "ai_gateway.request" in out and "ai_gateway.file_edit" in out
    assert "secret/a.py" not in out


def test_json_exporter_posts_otlp_json(monkeypatch):
    received = {}

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["auth"] = self.headers["Authorization"]
            received["type"] = self.headers["Content-Type"]
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}/otel/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20abc")
    handler = OTelLogger(JsonLogExporter())
    handler.emit({"id": "r1", "model": "gpt-5", "prompt_tokens": 10, "completion_tokens": 5, "response_cost": 0.5})
    handler.force_flush()
    server.server_close()

    assert received["path"] == "/otel/v1/logs"
    assert received["auth"] == "Bearer abc"
    assert received["type"] == "application/json"
    (resource_logs,) = received["body"]["resourceLogs"]
    resource = {a["key"]: a["value"] for a in resource_logs["resource"]["attributes"]}
    assert resource["service.name"] == {"stringValue": "litellm"}
    (record,) = resource_logs["scopeLogs"][0]["logRecords"]
    attributes = {a["key"]: a["value"] for a in record["attributes"]}
    assert attributes["event.name"] == {"stringValue": "ai_gateway.request"}
    assert attributes["gen_ai.usage.input_tokens"] == {"intValue": "10"}
    assert attributes["gen_ai.usage.cost"] == {"doubleValue": 0.5}
