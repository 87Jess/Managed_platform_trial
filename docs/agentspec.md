# AgentSpec

An AgentSpec is a versioned, declarative description of a managed agent. It lives in the
agent's folder as `agentspec.yaml`, next to the code, and records:

| Section | What it captures |
| --- | --- |
| `name`, `version`, `description` | Identity. `version` is the agent's own semantic version. |
| `owner` | Accountable person, team and contact email. |
| `framework`, `entrypoint` | `adk`, `langgraph`, `langchain` or `custom`, and the module/object to load. |
| `data_classification` | `public`, `internal` or `confidential`. |
| `models` | Models the agent calls: one `primary`, optional `fallback`s, and their location. |
| `tools` | MCP servers (HTTPS URL, optional tool allowlist) and platform registry connectors. |
| `runtime` | Python version, requirements file, CPU/memory, min/max instances, network, non-secret env vars. |
| `evaluation` | Evaluation suites (golden, safety, prompt injection, custom), datasets and pass thresholds. |

The JSON Schema is [`schemas/agentspec-v1.json`](../schemas/agentspec-v1.json), generated from
[`managed_agents/agentspec.py`](../managed_agents/agentspec.py). Reference specs:

* [`wdi_agent/agentspec.yaml`](../wdi_agent/agentspec.yaml): LangGraph, Gemini, BigQuery and evaluation suites
* [`examples/adk-agent/agentspec.yaml`](../examples/adk-agent/agentspec.yaml): Google ADK
* [`examples/source-agent/agentspec.yaml`](../examples/source-agent/agentspec.yaml): minimal custom Python agent

## Minimal example

```yaml
apiVersion: agentspec/v1
name: my-agent
version: 0.1.0
owner: {name: Platform Team, team: AI Platform, email: ai-platform@example.org}
framework: langgraph
data_classification: internal
models:
  - id: gemini-2.5-flash
```

Everything else has defaults: entrypoint `agent.root_agent`, Python 3.12, `requirements.txt`,
1 CPU / 1Gi, 0–2 instances, public network, no tools and no evaluation suites.

## Validation

```bash
uv run python -m managed_agents.agentspec validate path/to/agent-folder
```

The validator reports every problem at once, with the field path, and exits non-zero on failure.
CI (`cloudbuild.yaml`) validates the reference agents on every build. Besides the schema, it
enforces these rules:

* unknown fields are rejected, so typos don't pass silently
* exactly one `primary` model when models are listed
* `min_instances` ≤ `max_instances`
* MCP servers must use HTTPS, and their names must be unique
* environment variables must be UPPER_SNAKE_CASE, non-reserved and must not look like secrets
  (names containing KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL or PRIVATE); secrets belong in Secret Manager
* `confidential` agents need `runtime.network: private` and a `safety` evaluation suite
* `evaluation.required_before_deploy` needs at least one suite
* the entrypoint module, requirements file and local evaluation datasets must exist in the folder

## How the console uses it

When the selected folder, ZIP or GitHub path contains `agentspec.yaml`, the console validates it
during review and refuses a snapshot whose spec is invalid. A valid spec decides the framework,
entrypoint, requirements file, Python version, CPU/memory, instance range and environment
variables, overriding the form. The review panel shows the owner, models, tools, runtime and
evaluation, and the deployed Agent Engine resource is labelled with `agentspec-name`,
`agentspec-version`, `agent-team`, `data-class` and `agent-framework`, so it can be found in
Google Cloud. `runtime.network: private` is refused unless the console has a network attachment
configured. LangGraph and LangChain agents are packaged as Custom Python, with their framework recorded
on the Agent Engine resource. Folders without a spec still deploy using the form, as before.

Set `runtime.telemetry: true` to enable Google-managed Agent Runtime telemetry. It defaults
to `false`; the console only sets the reserved Google telemetry flag for opted-in specs.
This setting does not opt into GenAI prompt or response capture.

Evaluation suites are declared and validated here. Running them in CI is a separate story
(evaluation harness).

## Versioning and backward compatibility

There are two separate versions:

1. **Schema version (`apiVersion`).** The current version is `agentspec/v1`.
   * **Compatible changes** stay in v1: new *optional* fields with defaults, new enum values (for
     example a new framework), and relaxed limits. Existing v1 specs keep validating unchanged.
   * **Breaking changes** need `agentspec/v2`: removing or renaming a field, making a field
     required, changing a field's type or meaning, or tightening a rule that rejects
     previously valid specs.
   * When v2 ships, the validator and console accept **both v1 and v2 for at least two PI
     increments** (`SUPPORTED_API_VERSIONS` in `agentspec.py`). v1 specs then get a deprecation
     warning, and a migration note in this document lists the field changes. After the
     deprecation window v1 is removed in a release announced in advance.
   * Each schema version has its own file (`schemas/agentspec-v1.json`, `-v2.json`, ...) and is
     never edited in place in an incompatible way. A test fails if the checked-in schema drifts
     from the code.
2. **Agent version (`version`).** It's the agent's own semantic version, chosen by its owner:
   MAJOR for a changed input/output contract, MINOR for new capabilities, PATCH for fixes.
   It's recorded on the deployed resource as the `agentspec-version` label.

To change the schema: edit `managed_agents/agentspec.py`, regenerate the JSON Schema with
`uv run python -m managed_agents.agentspec schema > schemas/agentspec-v1.json`, update this
document and the reference specs, and add tests.
