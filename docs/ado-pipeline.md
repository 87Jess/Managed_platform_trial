# Azure DevOps Pipeline

[`azure-pipelines.yml`](../azure-pipelines.yml) runs CI on every pull request and push, and deploys
agents to Vertex AI Agent Engine (Agent Runtime) from `main` after an approval. It signs in to
Google Cloud with workload identity federation: Azure DevOps issues a short-lived ID token, Google
exchanges it for a short-lived token of a deployer service account. No keys are created or stored.

| Stage | Runs on | Does |
| --- | --- | --- |
| CI | Every PR and push | `pytest`, `ruff`, AgentSpec validation, offline packaging of each agent (`managed_agents.pipeline plan`) |
| Deploy | `main` only, after the `gcp-agent-runtime` approval | Signs in to Google, then `managed_agents.pipeline deploy` for each agent folder |

`managed_agents.pipeline deploy` packages and describes the agent exactly as the deployment console
does (same AgentSpec validation, bundle and Agent Engine request). The first run creates the agent;
later runs update the same agent in place, found by its `agentspec-name` label and
`managed-by=ado-pipeline`. It waits for the operation, fails the pipeline on a build error, and sends
one smoke-test message (`smokeMessage` parameter; empty skips it). The resource name is exported as
the output variable `AGENT_RESOURCE`.

Every agent folder must contain a valid `agentspec.yaml` ([agentspec.md](agentspec.md)): the spec
decides framework, entrypoint, requirements, resources and environment variables.

## One-time setup: Google Cloud

Prerequisites from the provisioning checklist: Vertex AI API, the `agent-research` runtime account
and, for private networking, the `managed-agent-runtime` attachment. Replace `PROJECT_NUMBER`.

1. Enable the token exchange APIs:

   ```bash
   gcloud services enable sts.googleapis.com iamcredentials.googleapis.com --project=itsai-mai-gcp-lab-proj
   ```

2. Create the deployer service account and its roles. It can deploy agents and attach the runtime
   account, nothing else:

   ```bash
   gcloud iam service-accounts create ado-agent-deployer --display-name="Azure DevOps agent deployer" --project=itsai-mai-gcp-lab-proj
   gcloud projects add-iam-policy-binding itsai-mai-gcp-lab-proj --member="serviceAccount:ado-agent-deployer@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com" --role="roles/aiplatform.user"
   gcloud iam service-accounts add-iam-policy-binding agent-research@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com --member="serviceAccount:ado-agent-deployer@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com" --role="roles/iam.serviceAccountUser" --project=itsai-mai-gcp-lab-proj
   ```

3. Create the Azure DevOps service connection first (next section) and note its **Issuer**, its
   **Subject identifier**, and the **object ID** of its Entra application.

4. Create the workload identity pool and the Azure DevOps provider. The allowed audience is the
   application ID of the Azure token exchange endpoint. The subject is shortened to the part after
   `/sc/`, because the full subject exceeds Google's 127-character limit for `google.subject`. The
   condition admits only tokens issued to this service connection's application:

   ```bash
   gcloud iam workload-identity-pools create ado-pool --location=global --display-name="Azure DevOps" --project=itsai-mai-gcp-lab-proj
   gcloud iam workload-identity-pools providers create-oidc ado-provider --location=global --workload-identity-pool=ado-pool --project=itsai-mai-gcp-lab-proj \
     --issuer-uri="ISSUER_FROM_THE_SERVICE_CONNECTION" \
     --allowed-audiences="fb60f99c-7a34-4190-8149-302f77469936" \
     --attribute-mapping="google.subject=assertion.sub.split('/sc/')[1]" \
     --attribute-condition="assertion.oid=='ENTRA_APPLICATION_OBJECT_ID'"
   ```

5. Let that one service connection impersonate the deployer. `SUBJECT` is the part of the
   service connection's Subject identifier after `/sc/`:

   ```bash
   gcloud iam service-accounts add-iam-policy-binding ado-agent-deployer@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com --project=itsai-mai-gcp-lab-proj \
     --role="roles/iam.workloadIdentityUser" \
     --member="principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/ado-pool/subject/SUBJECT"
   ```

## One-time setup: Azure DevOps

1. **Service connection.** Project Settings > Service connections > New > Azure Resource Manager:
   identity type *App registration (automatic)*, credential *Workload identity federation*, scope
   *Subscription*, name `google-cloud`. Azure DevOps grants its app a role on that subscription;
   remove or reduce it under *Manage service connection roles* (the pipeline needs no Azure access).
2. **Variable group** `gcp-agent-runtime` (Pipelines > Library), plain (non-secret) variables:

   | Variable | Value |
   | --- | --- |
   | `GCP_PROJECT_ID` | `itsai-mai-gcp-lab-proj` |
   | `GCP_PROJECT_NUMBER` | The project number |
   | `GCP_REGION` | `us-central1` |
   | `WIF_POOL_ID` | `ado-pool` |
   | `WIF_PROVIDER_ID` | `ado-provider` |
   | `GCP_DEPLOYER_SA` | `ado-agent-deployer@itsai-mai-gcp-lab-proj.iam.gserviceaccount.com` |
   | `AGENT_RUNTIME_ACCOUNT` | Optional. Empty = `agent-research@…`; `default` = Agent Engine default identity (demo) |
   | `AGENT_NETWORK_ATTACHMENT` | Optional. Empty = `managed-agent-runtime`; `none` = no private networking (demo) |

3. **Environment** `gcp-agent-runtime` (Pipelines > Environments) with an *Approvals* check, so every
   deploy waits for a named reviewer.
4. **Pipeline.** Pipelines > New pipeline > select this repository > *Existing Azure Pipelines YAML
   file* > `/azure-pipelines.yml`. On the first run, authorize the variable group, the environment
   and the service connection.

## Running it

- Change the agent list in the `agents` parameter (or set it when running the pipeline manually).
- `uv run python -m managed_agents.pipeline plan <folder>` shows locally what would be deployed,
  without Google Cloud access.
- A failing deploy prints Google's error; a failing smoke test also prints a Logs Explorer link
  filtered to that agent.
