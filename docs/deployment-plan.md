# Deployment Plan: Approval Required

Target: `itsai-mai-gcp-lab-proj`, `us-central1`.
Decisions: Agent Engine first, Kong OSS, Entra ID, new dedicated VPC.
No provisioning is authorized by this document. No cloud resources have been created.

## Request Path

```text
Consumer: app token + Entra delegated access token
  -> HTTPS load balancer (not implemented)
  -> Kong OSS (Cloud Run ingress container)
  -> policy broker (loopback-only backend port in the same instance)
  -> Agent Engine custom runtime (dedicated account, no data/model grants)
  -> private HTTPS Kong endpoint via PSC (load balancer/DNS setup outstanding)
  -> policy broker revalidates the original caller and current registry
  -> Vertex AI model / scoped read-only Storage
```

Cloud Run exposes only Kong's ingress port. The policy container binds on port 8081
inside the shared instance; this port is not a second Cloud Run public listener.
The runtime does not retain delegated tokens in application state. Tokens necessarily
transit the Agent Engine query request and gateway callback; security review must verify
that request-body logging, tracing, error reporting and infrastructure logs never capture
those fields. Do not enable body capture on these paths.

## Stage 1: Foundation Plan

Terraform declares these resources, with workloads off by default:

- Required project APIs, a custom-mode VPC, /24 service subnet and /28 PSC subnet.
- Agent Engine PSC network attachment with manual producer acceptance.
- Private `googleapis.com` DNS pointing to restricted Google API VIPs.
- No default internet route or NAT; route and egress allowance only for restricted
  Google APIs and private HTTPS in the service subnet, then explicit IPv4 egress deny.
- Private, versioned registry, staging and document buckets, with public access blocked.
- Artifact Registry with immutable tags, a Secret Manager secret container, build account,
  broker account, and a dedicated account for each agent in the manifest.
- Broker query/model IAM and read-only document IAM restricted to `approved/` objects.
  Agent accounts deliberately have no project-level data/model grants.
- Agent Platform service identity with attachment-management and DNS-peering permissions.

The dedicated VPC is an agreed deviation from the Shared VPC in the diagram.
VPC firewall rules are not proof that the Google-managed runtime has no alternate
egress. VPC Service Controls and runtime network verification remain mandatory.
The restricted Google API VIP does not itself establish a service perimeter.

Before a real plan: configure an encrypted remote Terraform backend, verify project
ownership/billing and organization policies, choose nonoverlapping CIDRs, and inspect
the existing project IAM. Preexisting broad IAM grants can bypass this design.
The Terraform does not remove existing IAM bindings or import existing resources.

## Stage 2: Blocked Workload Deployment

Required before setting `deploy_workloads=true`:

1. Provide Entra tenant UUID and API audience. Register delegated scope `Platform.Invoke`,
   assign `research.read` and `documents.read` application roles, and configure federation
   separately for Cloud Identity / administrator access if required. Direct token validation
   in this broker is not an organization-wide Entra-to-Google federation deployment.
2. Verify and upload `entra-jwks.json` into the registry bucket. The service reloads the
   snapshot per request and performs no internet key fetch. Establish scheduled refresh,
   integrity review, rotation overlap and alerts for unknown signing keys. Stale keys can
   retain trust after emergency rotation; refresh/revocation is an operational gate.
3. Generate a high-entropy application token locally. Put only its lowercase SHA-256 digest
   into a Secret Manager version. Distribute the actual token out of band. Do not put either
   value in Terraform variables/state or the repository. Configure rotation procedures.
4. Supply organization/access-policy context and implement a VPC Service Controls perimeter,
   restricted services, ingress/access levels and an audited break-glass path. Test in dry-run
   mode before enforcement. Organization-level restrictions cannot be inferred from a project ID.
5. Implement private HTTPS load balancing for the Agent Engine callback, certificates trusted
   by the runtime, private DNS/PSC DNS peering and associated proxy-subnet/firewall rules.
   Choose either private-only consumer access or a separately protected external HTTPS entrypoint
   with Cloud Armor. Neither load balancer is created by the current Terraform.
6. Approve default Cloud Build networking or provide a private worker pool and dependency mirror.
   Default workers fetch dependencies from the internet outside the dedicated VPC; this does not
   meet the diagram's strict perimeter-wide no-internet build posture.
7. Build the two images, scan them, record SBOM/provenance, sign approved digests and enforce
   attestations using Binary Authorization or an equivalent reviewed release gate. The supplied
   Cloud Build file tests/builds/pushes only; immutable tags are not signing or admission control.
8. Review maximum instances, regional model availability/entitlements, quotas, expected token
   consumption and budget alerts. Choose a model approved for each data classification.
9. Reserve callback capacity: an invocation waits for Agent Engine to call the same gateway.
   Load-test this cycle and enforce separate quotas/concurrency pools before production, so
   waiting invocations cannot starve the model/connector callback handlers.
10. Review the actual Terraform plan and approve provisioning. The code's approval flags are
    accidental-deployment guards, not replacements for organization policy or change control.

After those gates, enable the optional Cloud Run service, deploy the Agent Engine with its
dedicated service account and PSC configuration, then publish its resource name into the
registry. Enable only reviewed agent/model/connector entries and finally the global switch.
Update registry objects atomically with generation-match preconditions and maintain a signed,
reviewed release history. Terraform only bootstraps the registry; later content is operator-managed.

The GCS mount requests zero metadata-cache TTL. Verify actual cross-instance propagation
before relying on a kill-switch response time. Kill switches deny subsequent authorized
operations; they do not cancel already accepted model calls or active runtime executions.

## Diagram Coverage

| Diagram Capability | Current Status |
| --- | --- |
| Agent + connector registry | Validated JSON with grants; no registry UI, signing service or generated A2A cards |
| Cloud Build | Tests and container build configuration; no PR trigger or private pool |
| Evaluation harness | Deterministic security regression tests; no golden LLM/safety/injection evaluation set |
| Cloud Deploy | Not implemented; staging/canary/prod promotion is a release gate |
| Cloud Run agents | Not selected; Cloud Run hosts Kong and the policy broker |
| Agent Engine | Custom stateless gateway-only agent and deployment command; live deployment unverified |
| Sessions / Memory Bank | Not implemented; requires per-class retention, consent and deletion policy |
| Platform SDK | Model/Storage gateway client with fixed HTTPS origin and no redirects |
| Firestore / Memorystore state | Not provisioned; current agent is stateless |
| Kong ingress | OSS DB-less routing, request-size cap, local IP rate limiting; API checks tokens/grants |
| Kong MCP + A2A | Not implemented; REST endpoints are not MCP or A2A implementations |
| Policy + kill switch | Registry global/agent/connector/model toggles and role/classification checks |
| AI gateway | Broker model aliases and output-token caps; no enterprise Kong AI plugin or cumulative token budgets |
| BigQuery connector | Not implemented; per-user row/column authorization must be designed first |
| Storage connector | Implemented read-only GET, scoped IAM, user role, agent grant and approved prefix |
| Search connector | Not implemented; ACL-trimmed retrieval requires a real index and document ACL schema |
| Internal API facade | Not implemented; targets and delegated-claims contract needed |
| Observability | Minimal content-free JSON audit records; no Cloud Trace pipeline, BigQuery sink or dashboards |
| Operator access | No data access granted; PAM/JIT, federation and break-glass policies not configured |
| VPC-SC / perimeter | Required but not configured; no claim of production isolation |

`max_steps` is manifest metadata for future multi-step agents. The current agent always
makes exactly one model call; this is not a generic ADK step-limit enforcement mechanism.
Local rate limits reset per Kong instance and are not user quotas or a global token budget.
Broker model permissions are project-scoped; the registry is the application-level model
allowlist. Separate projects/service identities are needed for stronger blast-radius isolation.

## Cost Drivers

No dollar estimate is asserted without current pricing, traffic, model and retention choices.

| Resource | Billing Driver |
| --- | --- |
| Agent Engine | Runtime CPU/memory, active requests and instance settings; max 2 in the script |
| Vertex AI | Model-specific input/output tokens, grounding/retrieval if added |
| Cloud Run | Kong + policy CPU/memory, request duration and count; max 3 instances in template |
| HTTPS load balancing / Armor | Forwarding/proxy hours, rules and processed traffic |
| Storage / Artifact Registry | Versioned artifacts/documents, operations, image retention and egress |
| Cloud Build | Build minutes, machine size and private-pool capacity if selected |
| DNS / Secret Manager / Logging | Zones, queries, secret operations and log volume/retention |
| Future state/search services | Memorystore capacity, Firestore operations and search index/query costs |

Set budget alerts before any apply. Budgets are alerts, not hard spending caps.
Versioned buckets and container images currently have no automatic retention cleanup;
approve lifecycle policies before uploading sensitive or high-volume data.

## Verification Before Release

- Run Python tests/lint, Terraform fmt/validate, container builds and `kong config parse`.
- Perform authenticated Entra positive/negative smoke tests, including wrong tenant/audience,
  revoked roles, expired tokens, stale keys and application-token rotation.
- Prove direct data/model access fails using the actual agent service account.
- Prove unknown agents, revoked connector grants and cross-prefix reads fail before upstream calls.
- Prove no external internet egress from the managed runtime and Cloud Run workloads; validate
  private callback routing and deny direct alternate runtime entrypoints through Google IAM.
- Measure kill-switch propagation, registry/key outage behavior and concurrency starvation.
- Validate model safety/golden cases, injection resistance and content-free log/tracing capture.

Actual Terraform plan, Docker/Kong execution, Entra integration, private connectivity and
live Agent Engine behavior must be validated in the target environment before release.

## Local Verification Record

- Python 3.12: 49 tests passed, including real RSA JWT validation, API deny paths,
   gateway-only runtime behavior, backend contracts and deployment approval guards.
- One upstream Starlette/httpx deprecation warning remains; tests still pass.
- Terraform formatting completed; provider initialization downloaded signed providers.
- Terraform provider-schema validation and Ruff lint are blocked by this Windows machine's
   application-control policy. They are configured as CI checks, not reported as passing.
- Docker is not installed here, so neither container builds nor Kong execution were verified.
- No cloud plan/apply, builds, API enablement, resource creation or agent deployment was run.