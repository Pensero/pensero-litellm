# pensero-litellm

A [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) callback that sends the usage of each request
as OpenTelemetry log events: the user, the client (Claude Code, Codex, ...), the model, the tokens, the cost,
and the lines of each file edit that the coding agent applied. It sends standard OTLP/HTTP, so any OpenTelemetry
backend can receive it: an OpenTelemetry Collector, Grafana, Datadog, or [Pensero](https://pensero.ai).

No source code, prompt or file path leaves the proxy. The edit events hold only line counts.

## Install

### One command

On the machine that runs the LiteLLM proxy:

```sh
curl -fsSL https://raw.githubusercontent.com/Pensero/pensero-litellm/main/install.sh | sh
```

The script asks for the OTel endpoint (Pensero is the default) and the token or headers. Then it finds the proxy:

| The proxy runs in | The script |
| --- | --- |
| Docker Compose | Builds a local image from the proxy image plus this package, so the LiteLLM version stays the same. Sets the image and the OTel variables of the service (the values go in `.env`), adds the callback to `config.yaml` and restarts the service. |
| Docker, started with `docker run` | Builds the same image and adds the callback to `config.yaml`. It cannot restart the container with the same options, so it shows the image and the `-e` options to use. |
| A Python environment (pip) | Installs the package in the Python of the `litellm` command, adds the callback to `config.yaml`, and shows the variables to set before you restart LiteLLM. |
| Nothing | Creates a new proxy in `./litellm-proxy` (Docker Compose, Anthropic or OpenAI models) and starts it. |

It shows the changes and asks before it writes files or restarts a service. It saves a `.bak` copy of each file
that it edits. Options such as `--endpoint`, `--token`, `--headers` and `--yes` let it run without questions
(`curl ... | sh -s -- --help`). Kubernetes and Helm: use the manual steps below.

### Manual

Add the package to your LiteLLM proxy image:

```sh
pip install pensero-litellm
```

Add the callback to the `config.yaml` of the proxy:

```yaml
litellm_settings:
  callbacks: pensero_litellm.handler
```

Set the standard OpenTelemetry variables:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.example.com:4318
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<token>
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # or http/json
```

To send to Pensero, use the endpoint and the token on the LiteLLM setup page in Pensero, with
`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`.

The callback sends the events in batches from a background thread. If the backend is down, the proxy logs a
warning and the requests do not fail or wait.

| Variable | Default |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` (`/v1/logs` is added) or `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` (used as is) | `http://localhost:4318` |
| `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_LOGS_HEADERS` | none |
| `OTEL_EXPORTER_OTLP_PROTOCOL` or `OTEL_EXPORTER_OTLP_LOGS_PROTOCOL` | `http/protobuf` |
| `OTEL_SERVICE_NAME` | `litellm` |
| `OTEL_BLRP_SCHEDULE_DELAY` (batch interval, ms) | `1000` |

## Events

Each event is an OTLP log record. Its name is in `event_name` and in the `event.name` attribute.

`ai_gateway.request`: one for each successful request.

| Attribute | Example |
| --- | --- |
| `gen_ai.response.id` | `msg_01XFDUDYJgAACzvnptvVoYEL` |
| `gen_ai.operation.name` | `anthropic_messages`, `aresponses`, `acompletion` |
| `gen_ai.provider.name` | `anthropic` |
| `gen_ai.request.model` | `claude-sonnet-4-5` (the model name that the client asked for) |
| `gen_ai.response.model` | `claude-sonnet-4-5-20250929` |
| `user.id`, `user.email` | the LiteLLM user of the virtual key |
| `user_agent.original` | `claude-cli/2.0.14 (external, cli)` |
| `gen_ai.usage.input_tokens` | `1500` (includes the cache tokens) |
| `gen_ai.usage.output_tokens` | `200` |
| `gen_ai.usage.cache_read.input_tokens` | `1000` |
| `gen_ai.usage.cache_creation.input_tokens` | `300` |
| `gen_ai.usage.cost` | `0.0123` (USD, the cost that LiteLLM calculated) |

`ai_gateway.file_edit`: one for each file edit that the agent applied. It has the attributes of the request that
sent the result of the edit (without the usage), plus:

| Attribute | Example |
| --- | --- |
| `gen_ai.tool.call.id` | `toolu_01A09q90qw90lq917835lq9` |
| `gen_ai.tool.name` | `Edit`, `MultiEdit`, `Write`, `apply_patch` |
| `lines.added` | `5` |
| `lines.deleted` | `3` |

The ids are stable, so a backend can drop an event that it gets two times.

## How the lines are counted

The agent sends the result of each tool call back to the model in its next request. The callback reads only the
tool results at the end of the request, so each edit is counted one time. It skips failed edits.

- Claude Code (`/v1/messages`): `Edit` and `MultiEdit` count the lines that differ between `old_string` and
  `new_string`. `Write` counts every line of the file as added. A result with `is_error` is skipped.
- Codex (`/v1/responses` or `/v1/chat/completions`): `apply_patch`, also when it runs from a shell call,
  counts the `+` and `-` lines of the patch, if the result says `Success. Updated the following files`.

Limits:

- `Edit` with `replace_all` counts one replacement, because the request does not say how many matches the file had.
- `Write` over a file that exists counts every line as added and no line as deleted.
- A deleted file counts no lines, because the patch does not list them.
- A client that sends only `previous_response_id` and not the history does not send the edit call, so its edits are not counted.
- Other clients (Cline, Roo Code, ...) send their usage, but not their edits.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## License

MIT
