# Weather Agent: GCP Deployment Plan

Status: standalone lab deployment approved using existing prerequisites only; stopped on failed preflight.
Date: 2026-11-18.
Target: `itsai-mai-gcp-lab-proj`, `us-central1`.
Current authorized scope: standalone ADK lab deployment using existing resources; stop on missing prerequisites.

## Verified Findings

- The supplied source folder is `C:/Users/wb639247/GCP_Managed_Agents/agents/weather_agent`.
- Its `agent.py` exports `root_agent`, an instance of `google.adk.agents.LlmAgent`.
- The configured model is `gemini-2.5-flash`. No tools or weather API integration are defined.
- This is an ADK agent, not a synchronous `query(message)` custom agent. The console now has
   an explicit Google ADK selector, cloud-only `AdkApp` wrapping and session/streaming schemas.
   Existing snapshots must be reviewed again with Google ADK selected to use this path.
- The repository-level `pyproject.toml` and `uv.lock` are outside the weather-agent folder.
  The inspected weather-agent ZIP did not include dependency metadata.
- Repository metadata declares `google-adk>=2.6.2` and `google-cloud-aiplatform>=1.163.0`,
  along with dependencies for other applications and development tools. These are constraints,
  not the exact resolved versions; deployment must use a reviewed lock/export.
- Application Default Credentials refreshed successfully.
- The saved ADC quota project is `itsai-claude-vertexai-prd-proj`, not the intended lab project.
  No saved credentials or quota-project settings were changed.

Authenticated read-only preflight completed through the console on 2026-11-18:

| Check | Observed Result |
| --- | --- |
| Vertex AI API | Enabled; service-usage lookup succeeded |
| Runtime account `agent-research@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com` | Lookup returned HTTP 404 Not Found |
| Billing status | Lookup returned HTTP 403 Forbidden; unverified, not assumed disabled |
| PSC attachment `managed-agent-runtime` in `us-central1` | Lookup returned HTTP 403 Forbidden; existence/access unverified |

No missing-resource creation was authorized. Stop deployment until an approved existing
runtime identity and required access are supplied, or a separately reviewed provisioning
change is approved. Model/session permissions, actAs, quotas and organization policies are
still unverified. These checks do not replace the required security review.

## Deployment Choice Required

### A. Standalone ADK Lab Deployment

Deploy the agent through the supported Agent Engine ADK runtime, with a dedicated identity
and direct, explicitly permitted Vertex AI model calls.

This is the smallest functional deployment for the supplied code. However, it is an explicit
deviation from the original architecture's Kong-only model path and must be approved as a
limited lab deployment. It does not claim VPC-SC isolation, gateway enforcement or the other
production platform controls. No public anonymous invocation permission is proposed.

### B. Diagram-Aligned Platform Deployment

Keep the requirement that agents cannot call models or data directly. Add an ADK-compatible
model adapter through the approved Kong/policy path, authenticated caller-context propagation,
ADK session/query support, private gateway connectivity, registry grants and required release
controls before deploying the weather agent.

This is more work than a standalone deployment and requires the gateway/network/security
prerequisites in [deployment-plan.md](deployment-plan.md). The current weather agent would
need a reviewed model integration change rather than a direct Gemini model identifier alone.

The user approved option A using existing prerequisites only, explicitly requiring a stop
if prerequisites are missing. Option B and creation of IAM/network prerequisites remain
unapproved. No deployment was submitted after the preflight failure.

## Read-Only Preflight

Complete these checks before proposing an apply or creating billable resources:

1. Verify the target project, billing status, organization policies and region availability.
2. Verify enabled Vertex AI, build, storage and relevant infrastructure APIs; distinguish
   permission-denied responses from genuinely absent resources.
3. Inventory suitable existing runtime identities, staging/artifact storage and network resources.
4. Verify the operator can create/read Agent Engine resources, read operations, and act as the
   chosen runtime identity. Validate the ADK runtime's session permissions against current docs.
5. Verify Gemini model availability, applicable terms, regional quotas and organization allowlists.
6. Explicitly select the lab quota project for deployment credentials; do not charge or use the
   unrelated saved quota project accidentally. Changing saved ADC settings requires approval.
7. Inventory any existing Agent Engine resource with the intended display name before creation
   to prevent duplicate deployments and charges.

## Conditional Resource Changes

Reuse existing resources when appropriate. The following resources are candidates, not a
confirmed list of missing resources and not a Terraform diff:

| Resource | Condition / Purpose |
| --- | --- |
| Required project APIs | Enable only if disabled and permitted by organization policy |
| Dedicated runtime identity, proposed `agent-weather` | Create only if no approved existing identity is selected |
| Least-privilege model/session grants | Determine from the chosen architecture; no direct model permission for B |
| Source staging bucket | Only if the selected supported ADK deployment method requires one |
| Agent Engine managed runtime | New billable agent deployment after source and resource approval |
| PSC attachment, VPC, subnet and DNS | Only if required by the approved network posture; verify existing resources first |
| Gateway infrastructure and model adapter | Required for B; not implicitly part of standalone option A |
| Logs, retention and budget alerts | Review capture settings, retention, cost ownership and alert recipients |

Do not apply the whole platform Terraform merely to deploy one test agent. It contains broader
platform resources and needs its own review, state backend and resource reconciliation.

## Packaging and Application Work

1. Build a clean, reviewable package containing the agent and its required package structure.
   Exclude `.env`, `.adk` session databases, virtual environments, caches and credentials.
2. Include dependency metadata at the selected source root. Either use a reviewed runtime
   requirements export, or a standalone project with both `pyproject.toml` and `uv.lock`.
   Do not assume the uploader can access parent files outside a ZIP or selected source root.
3. Lock compatible ADK/Vertex SDK versions. Avoid deploying unrelated notebook, graph and
   web API dependencies from the repository unless this agent actually requires them.
4. Use the console's Google ADK deployment path with its cloud-only entrypoint wrapper and
   managed session/streaming operation schemas. Do not reuse a custom-agent snapshot.
5. ADK payload and source-isolation regression tests are included. Live caller-owned session
   creation/querying still requires target-environment verification after deployment approval.
   Keep source-safety checks, immutable snapshots and explicit cost approval intact.
6. Perform local packaging/import checks only after dependencies and executable source have
   been reviewed. Do not import uploaded code automatically during ordinary source review.

## Cost and Approval Gates

No fixed dollar amount is asserted without verified current pricing and expected use.
Primary drivers are Agent Engine CPU/memory, Gemini input/output tokens, build minutes,
artifact/staging storage and log ingestion. Gateway/load-balancer/network services add costs
if option B requires them. Budget alerts are not hard spending caps.

Proposed initial runtime limits are zero minimum instances and at most two maximum instances,
subject to current runtime support and quota verification. These are not a guarantee of zero
idle or total cost. Agree on a test budget, retention and cleanup owner before deployment.

Approval sequence:

1. Choose standalone lab option A or diagram-aligned option B.
2. Complete preflight and produce an exact create/reuse/change list, including IAM scope.
3. Approve dependency/source snapshot, network posture, security deviations if any, and budget.
4. Approve resource changes and billable deployment explicitly.
5. Provision only approved missing prerequisites, deploy the reviewed agent, and record its
   operation and resource IDs. Do not expose it anonymously.
6. Smoke-test a managed ADK session/query, verify the Gemini response, inspect logs for secrets,
   and confirm invocation permissions. Describe this as an assistant test, not a live weather
   lookup, because the current source has no weather tool.

No agent deployment, API enablement, IAM mutation, resource creation or saved credential
change has been performed while preparing this plan. Cloud submission remains locked because
the required existing-resource prerequisites have not passed preflight.