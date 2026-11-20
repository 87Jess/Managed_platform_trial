# Data360 agent (LangGraph + Gemini)

Answers questions about World Bank development data in plain language, e.g. "What is the GDP
of India?". Gemini on Vertex AI uses tools based on the World Bank's
[Data360 OpenAPI spec](https://raw.githubusercontent.com/worldbank/open-api-specs/refs/heads/main/Data360%20Open_API.json):
indicator search, observations, indicator metadata, disaggregations, and dataset indicator lists.
REST is the default; set `DATA360_BACKEND=mcp` for the original MCP tool catalog.

```text
question ─► Gemini (tools bound) ─► tool calls? ──yes──► Data360 tools ─────┐
                 ▲                        │no                                │
                 └──────── results ───────┼◄────────────────────────────────┘
                                          ▼
                                       answer
```

## Deploy with the console

1. **Ship** → Local folder `data360agent` (or zip this folder).
2. Framework **Custom Python**, entrypoint `agent` / `root_agent`, dependencies `requirements.txt`.
3. Review, approve, deploy. Then **Chat** from the Agent garden and ask a question.

The agent uses its own Agent Engine identity for Vertex AI, so there is no API key or gateway
token. The identity needs permission to call Gemini (`roles/aiplatform.user`; the Agent Engine
default service agent normally has it) and the runtime needs outbound HTTPS to
`data360api.worldbank.org` for REST or `maimcpext.worldbank.org` for MCP
(public networking, or a PSC route that allows it). Switching backends does not bypass egress
restrictions, and there is no automatic failover between hosts.

## Deployment telemetry

The [AgentSpec](./agentspec.yaml) enables Google-managed runtime telemetry. Chat initializes
the Vertex AI SDK's `LangchainAgent` with `enable_tracing=True` and a custom runnable that
keeps the existing LangGraph workflow and `query(message=...)` interface. JSON debug commands
still work without initializing the Vertex AI SDK. The wrapper uses the managed OpenTelemetry
provider for Data360 spans rather than enabling automatic LangChain payload instrumentation.
Spans cover queries, model calls, and tool operations without recording prompts,
responses, tool arguments, or exception messages. The agent uses the runtime's existing
OpenTelemetry provider; it does not configure a local exporter. Review and deploy a fresh
snapshot from an updated console, then invoke the agent and check Cloud Trace in the
deployment project.
If traces are missing, verify that the Telemetry and Cloud Logging APIs are enabled and
the runtime identity has the required exporter permissions. See Google's
[Agent Runtime tracing guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/tracing).

## Configuration (environment variables, all optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA360_BACKEND` | `rest` | Use the OpenAPI REST tools, or set `mcp` to use the MCP server |
| `DATA360_MODEL` | `gemini-2.5-flash` | Gemini model on Vertex AI |
| `DATA360_FALLBACK_MODEL` | `gemini-3-flash-preview` | Used if the first model is not found (e.g. retired) |
| `DATA360_LOCATION` | `global` | Vertex AI location for the model |
| `GOOGLE_CLOUD_PROJECT` | project of the credentials | Project billed for Gemini calls |
| `DATA360_PROXY` | none | HTTP proxy for the Data360 server, e.g. `http://10.10.0.5:3128`, when the runtime has no direct internet route |

## Inputs

* `query(message="What is the GDP of India?")` → `{"reply": ..., "tools_used": [...], "model": ...}`
* `input=`, `prompt=` or `question=` work too, for callers that use those names.
* Optional `history=[{"role": "user"|"assistant", "content": "..."}]` for multi-turn context.
* JSON commands for debugging: `{"action": "list_tools"}` or
  `{"action": "call_tool", "name": "TOOL", "arguments": {...}}` sent as the message.

With REST selected, `list_tools` returns local definitions and does not check connectivity.
Use a real tool call to verify egress; for example, send this as the `message`:

```json
{"action":"call_tool","name":"data360_get_data","arguments":{"DATABASE_ID":"WB_WDI","INDICATOR":"WB_WDI_NY_GDP_MKTP_CD","REF_AREA":"IND","TIME_PERIOD":"2024"}}
```

REST results include the endpoint and API data in `result.structuredContent`. If a tool call
fails during chat, Gemini receives the tool error instead of observations. If MCP discovery
fails, the reply also includes `tools_unavailable`. The console sends earlier turns as `history`
so follow-up questions ("and in 2023?") work.

Tool results are treated as data, capped at 12,000 characters each, and the loop stops after
8 tool rounds with a request to narrow the question.

## Local interactive chat

From the repository root in PowerShell, run:

```powershell
$env:GOOGLE_CLOUD_PROJECT = "itsai-mai-gcp-lab-proj"
uv run --with-requirements .\data360agent\requirements.txt python .\tests\test_chat.py
```

The launcher resolves the repository import path automatically; no `PYTHONPATH` setting
is needed. Enter `quit` or `exit` to stop. Chat requires Application Default Credentials
with Vertex AI model access, and model calls may incur charges.

Set `GOOGLE_CLOUD_PROJECT` to the project ID where you are authorized to call Vertex AI
(the lab project above is an example). Data360 passes the resolved project directly to the
Vertex AI SDK so the chat wrapper does not need a Cloud Resource Manager lookup to resolve it.
This does not change your credentials or their quota project.

## Tests

```bash
pip install -r data360agent/requirements.txt pytest
python -m pytest tests/test_data360agent.py tests/test_data360_rest.py tests/test_data360_agent.py
```

They run offline with a scripted model and a fake MCP server.
The MCP and schema-conversion tests run without the optional LangGraph/Gemini packages;
the conversation test module is skipped when those packages are missing.
