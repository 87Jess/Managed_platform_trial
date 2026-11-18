# Data360 MCP Agent

This is a custom Python agent source folder for the managed-agent console. It connects to
the World Bank Data360 MCP endpoint over HTTPS and supports natural-language questions
using an LLM accessed exclusively through the existing governed platform model gateway.
It also preserves the explicit JSON tool-command interface.

## Conversational mode

Configure these non-secret environment variables on the Agent Engine runtime before deployment:

| Variable | Value |
| --- | --- |
| `DATA360_GATEWAY_URL` | Approved HTTPS gateway origin, without a path |
| `DATA360_AGENT_ID` | Enabled agent ID from the gateway registry (not the display name) |
| `DATA360_MODEL_ALIAS` | Enabled model alias granted to that agent |

The checked-in platform registry is disabled by default and has no Data360 agent entry.
An operator must configure/approve those grants separately. This project does not enable them,
grant IAM permissions, deploy resources, or bypass authentication automatically.

Call the agent through the platform's authenticated `/v1/agents/{id}/invoke` endpoint.
It forwards `user_id`, `authorization` and `app_token` per invocation, as required by
`query(message, user_id="", authorization="", app_token="")`. Never embed credentials in
source, runtime environment variables, model prompts, MCP arguments, or chat messages.
A direct console/Agent Engine call that supplies only `message` cannot use the governed model.
Missing settings or credentials fail with an explicit configuration/authentication error.

Messages such as `Hi! What can you do?` and `Find GDP indicators for India` enter the
conversational workflow. The model sees the tool catalog, requests individual input schemas,
selects tools, and summarizes their results. Each query is stateless and limited to eight
model steps. The gateway's 16000-character input limit is enforced; if a result exceeds the
budget, request fewer observations rather than silently truncating data. MCP errors are
shown to the model as errors, and transport/authentication/model-format failures propagate.

The small gateway adapter in `chat.py` follows the platform's `PlatformClient` HTTPS,
credential-forwarding, timeout and redirect conventions. It is included locally because the
console uploads this source folder alone, without the parent `managed_agents` package.
The existing gateway accepts text prompts, so model actions use a strict JSON protocol rather
than native model function-calling APIs.

## Explicit tool mode

JSON commands work without model configuration or model credentials. First list tools, then
call a specific tool by name with its documented arguments:

```json
{"action": "list_tools"}
```

```json
{"action": "call_tool", "name": "TOOL_NAME", "arguments": {}}
```

Select this folder in the console, choose the **custom Python agent** framework, and use
module `agent` and object `root_agent`. The runtime needs outbound HTTPS access to
`maimcpext.worldbank.org` and to the approved model gateway. Do not send secrets or sensitive
personal data to the MCP server.

After changing source, create and review a new snapshot and redeploy; the existing deployment
continues running the old immutable source. Include `chat.py` in the reviewed files.
No new Python dependencies are required.
