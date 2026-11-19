# Data360 agent (LangGraph + Gemini)

Answers questions about World Bank development data in plain language, e.g. "What is the GDP
of India?". A LangGraph loop lets Gemini on Vertex AI pick tools from the
[Data360 MCP server](https://maimcpext.worldbank.org/ext/data360/mcp), calls them, and writes
a short answer with units, year and source.

```text
question ─► Gemini (tools bound) ─► tool calls? ──yes──► Data360 MCP tools ─┐
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
`maimcpext.worldbank.org` (public networking, or a PSC route that allows it).

## Configuration (environment variables, all optional)

| Variable | Default | Purpose |
| --- | --- | --- |
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

If the Data360 server can't be reached (for example the runtime has no outbound internet),
the agent still replies, says it can't fetch data, and adds `tools_unavailable` with the reason;
the console shows that reason above the reply. The console's Chat also sends earlier turns as
`history`, so follow-up questions ("and in 2023?") work.

Tool results are treated as data, capped at 12,000 characters each, and the loop stops after
8 tool rounds with a request to narrow the question.

## Tests

```bash
pip install -r data360agent/requirements.txt pytest
python -m pytest tests/test_data360_agent.py
```

They run offline with a scripted model and a fake MCP server.
