# Google Managed Agent Platform

## Deployment Frontend

Run `uv run python -m managed_agents.console` and open `http://127.0.0.1:8088`.
Select a local folder, public GitHub repository at a pinned commit, or ZIP upload;
review the immutable snapshot; then explicitly approve deployment to Agent Engine.
Cloud submission is locked until the operator configures ADC and enables the approval gates.
See [docs/console.md](docs/console.md) for the custom-agent contract, sample, and deployment setup.

An initial, security-focused implementation of the supplied Google Cloud architecture.
Targets **itsai-mai-gcp-lab-proj / us-central1**, **Vertex AI Agent Engine**, and **Kong OSS**.
This is a tested foundation, not a complete production platform. Nothing has been deployed.

## Implemented

- FastAPI policy broker behind Kong OSS, with no unauthenticated data or model routes.
- Entra v2 delegated access tokens: RS256 signatures, tenant, issuer, audience, expiry,
	API scope, and application roles. An independent application token is also required.
- Validated registry, agent/connector/model grants, data classification and kill switches.
- Agent Engine custom runtime that makes model calls only through the gateway.
	The runtime service account receives no direct model or data permissions.
- Read-only Storage connector with both user-role and agent grants, prefix checks,
	scoped IAM and a 1 MiB response limit.
- Terraform foundation, disabled-by-default Cloud Run workload, container builds and CI.
- Dry-run-first Agent Engine deployment command, with explicit apply gates.

See [docs/deployment-plan.md](docs/deployment-plan.md) for missing architecture components,
cost drivers and the approval checklist. **Do not deploy workloads until those gates are met.**

## Repository Layout

| Path | Contents |
| --- | --- |
| `managed_agents/` | Policy broker API, Entra auth, registry policy, Agent Engine runtime, source bundler, deployment console |
| `managed_agents/web/` | Console frontend (served at `/static`) |
| `examples/source-agent/` | Minimal custom agent to deploy through the console |
| `registry/agents.json` | Agent/connector/model registry (disabled by default) |
| `gateway/` | Kong OSS DB-less config and image |
| `infra/` | Terraform foundation: VPC, PSC attachment, buckets, Artifact Registry, secrets, service accounts, optional Cloud Run gateway |
| `tests/` | Unit tests (no cloud credentials needed) |
| `docs/` | Console guide and deployment plans |

## Deploying an Agent to GCP

1. Provision the foundation: `terraform -chdir=infra plan`, review, then apply after approval.
2. Copy the Terraform outputs into your environment: `network_attachment` →
   `CONSOLE_NETWORK_ATTACHMENT`, an `agent_service_accounts` entry → `CONSOLE_RUNTIME_ACCOUNT`.
3. `gcloud auth application-default login`, then set `CONSOLE_ALLOW_DEPLOY=1` and
   `CONSOLE_SECURITY_REVIEW_APPROVED=1` once the security gates are met.
4. `uv run python -m managed_agents.console`, pick a folder / GitHub commit / ZIP,
   review the snapshot, optionally **Test in GCP sandbox**, approve, and the console
   creates the Agent Engine resource and tracks the operation until it completes.

To host the console in GCP instead of running it locally, set `deploy_console = true` in
Terraform: it runs on Cloud Run behind Identity-Aware Proxy and deploys with its own service
account. See [docs/console.md](docs/console.md#hosted-console-cloud-run--iap).

Sandbox testing runs the snapshot in an isolated Agent Engine code-execution sandbox
before deployment; see [docs/console.md](docs/console.md#test-in-a-gcp-sandbox).

## Local Checks

Python 3.12+ and `uv` are required:

```powershell
uv sync --locked
uv run python -m pytest -q
uv run python -m ruff check .
```

Windows application control can block generated launchers. Use
`.\.venv\Scripts\python.exe -m pytest -q` rather than the `pytest.exe` launcher.

To run the API, set `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `ENTRA_TENANT_ID`,
`ENTRA_AUDIENCE`, `APP_TOKEN_SHA256`, `REGISTRY_PATH`, `ENTRA_JWKS_PATH`, `HOST` and `PORT`
(the console also reads `CONSOLE_PORT`, `CONSOLE_SOURCE_ROOT`, `CONSOLE_ALLOW_DEPLOY`,
`CONSOLE_SECURITY_REVIEW_APPROVED`, `CONSOLE_RUNTIME_ACCOUNT` and `CONSOLE_NETWORK_ATTACHMENT`).
No development authentication bypass is provided.
Obtain your tenant's signing-key snapshot from
`https://login.microsoftonline.com/TENANT_ID/discovery/v2.0/keys`, verify it through
your identity-administration process, and place it at the configured `ENTRA_JWKS_PATH`.
Never send tokens, secrets, or service-account keys through chat.

```powershell
uv run --env-file .env python main.py
```

The API listens on `http://127.0.0.1:8080`. `/healthz` is a liveness check only.
The checked-in registry disables the platform, so `/readyz` returns 503 and protected
operations are denied until explicitly configured. Unit tests use signed local test keys
and fake backends, not Google Cloud credentials.

## API

Protected routes require `Authorization: Bearer ENTRA_ACCESS_TOKEN` and `X-App-Token`.
The access token must contain delegated scope `Platform.Invoke` and the registry's
assigned application roles. ID tokens and application-only tokens are rejected.

| Route | Purpose |
| --- | --- |
| `GET /v1/agents` | List the caller's enabled agents |
| `POST /v1/agents/{id}/invoke` | Invoke the configured Agent Engine |
| `POST /v1/agents/{id}/models/{alias}/generate` | Governed model generation |
| `GET /v1/agents/{id}/connectors/{connector}/object?name=approved/file.txt` | Read an approved document |

POST bodies contain only `{"message": "Your question"}`. Caller identity is derived
from the verified token, never supplied in the request body. The initial agent is
stateless and performs one governed model call; it does not yet expose managed sessions.

## Deployment Preparation

```powershell
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
terraform -chdir=infra plan
```

`plan` needs authenticated Google Cloud access and does not create resources. Review its
actual diff before approval. Do not run `apply` until the plan is approved. Use an encrypted,
access-controlled remote state backend before any real application; no backend is configured.

The agent deployment command is also a dry run unless `--apply` is explicitly supplied:

```powershell
uv run python -m managed_agents.deploy_agent --gateway-url https://agents.internal.example.com --staging-bucket gs://itsai-mai-gcp-lab-proj-platform-staging --network-attachment projects/itsai-mai-gcp-lab-proj/regions/us-central1/networkAttachments/managed-agent-runtime
```

The example gateway hostname is a placeholder, not a deployed endpoint. Only after approval,
install the deployment SDK with `uv sync --extra agent-engine --locked`. Actual agent creation
additionally requires `--apply --approved-project itsai-mai-gcp-lab-proj --security-review-approved`.

`GOOGLE_GENAI_USE_ENTERPRISE=1` is preserved in the environment example you provided;
this implementation uses authenticated Vertex REST calls and does not interpret that flag
or assume that it enables an enterprise entitlement.
