# Agent Deployment Console

## Start Locally

```powershell
uv sync --locked
uv run python -m managed_agents.console
```

Open `http://127.0.0.1:8088`. This is a local, single-operator tool, not an internet-facing
multiuser administration service. It runs separately from the Entra-protected policy API.
It binds only to loopback, checks Host/Origin, uses a random per-process browser token,
and rejects cross-origin requests. Do not reverse-proxy it or expose it on a shared host.
Other processes or users who control this workstation remain inside its trust boundary.

Cloud deployment is locked by default. Reviewing source and downloading a plan requires
no Google credentials and creates no cloud resources.

## Sources

- **Local folder:** a folder accessible to the console server inside `CONSOLE_SOURCE_ROOT`.
  The root defaults to the current working directory. Browser users cannot reference folders
  on another machine by typing that machine's path.
- **GitHub:** a public `https://github.com/OWNER/REPOSITORY` URL and full 40-character commit SHA.
  The console downloads a bounded archive from GitHub. Private repositories and credentials
  embedded in URLs are not supported. No repository code is executed locally.
- **ZIP:** upload or drop an archive of at most 8 MiB. A single enclosing archive folder is
  removed automatically. Relative `./` prefixes and Windows path separators are normalized.
  Symlinks, absolute/traversal paths, encrypted ZIPs and duplicate normalized paths are rejected.

Source snapshots are limited to 1,000 files / 8 MiB uncompressed and packaged. Common credential
files, `.env*`, private keys, `.git`, virtual environments and dependency caches are excluded.
These exclusions are not a comprehensive secret scanner. Inspect the source file list and code
before approval. Source archives and plans are held in server memory, never imported locally.
For uv projects only, project and lock metadata are copied into an isolated temporary directory
for dependency export, then removed. Uploaded Python code is not copied there or executed.

The console supports **custom Python agents** with a synchronous `query(message: str)` method
and **Google ADK agents** selected explicitly under **Agent framework**. It does not convert
arbitrary web apps to either contract. Dependencies can use a requirements file (which may be
empty) or a standalone uv project containing both `pyproject.toml` and `uv.lock`.
Dependencies and source are built/executed in Google's runtime after approval, with the privileges
of the configured runtime service account. Only deploy trusted, reviewed code and dependencies.

### Dependency Selection

Leave **Dependency file** as `auto` to prefer `requirements.txt` when present, otherwise detect
`pyproject.toml` plus `uv.lock`. You can explicitly select `uv.lock` / `pyproject.toml`, or enter
a custom requirements-file path. Both uv files must be inside the selected source directory;
files outside the uploaded archive or selected subdirectory are not discovered automatically.

For a uv project, run `uv lock` before uploading. The server requires `uv` on its PATH and runs
`uv export --locked --offline --no-dev --no-default-groups --no-emit-project` with configuration
discovery, build hooks and source overrides disabled and an isolated cache. It does not install dependencies, download Python,
or execute the agent. Runtime dependencies, environment markers and hashes are exported into
`.agent-platform-requirements.txt`, included in the immutable reviewed snapshot, and sent as
the Agent Engine requirements file. The original project and lockfile remain in the snapshot.
The review panel identifies uv exports. A stale/incompatible lockfile is rejected, not regenerated.

This version supports static standard `[project]` metadata and public PyPI packages. Dynamic
metadata, uv workspaces, local/path packages, Git dependencies and private indexes are not
supported. Extras and dependency groups are not selected automatically. The application source
is loaded via its entrypoint rather than installed as a package; use a compatible source layout.

### Google ADK Agents

Select **Google ADK** for `google.adk.agents.Agent` / `LlmAgent` entrypoints. The console adds
a reviewed `_agent_platform_adk.py` wrapper to the source snapshot. It imports the selected
object and wraps it in `vertexai.agent_engines.AdkApp` only inside the cloud runtime, not
during upload/review. An existing `AdkApp` object is retained. The deployment declares
`google-adk`, `async_create_session` and `async_stream_query`, rather than the custom query
contract. It also selects Vertex AI as the Google Gen AI backend.

Include compatible, pinned `google-cloud-aiplatform[agent_engines,adk]` / ADK dependencies
in the requirements or uv metadata. The console does not add or resolve new dependencies
behind the reviewed lockfile. Local `.adk` session databases are excluded from uploads.

ADK agents configured with a direct Gemini model identifier require model and managed-session
permissions for their runtime identity. ADK support alone does not implement the diagram's
Kong-only model integration. Do not approve a standalone direct-model lab deployment as if it
were the full gateway-controlled architecture.

### Check Deployment Blockers

Open **Environment > Check GCP access**, or run this without starting another server:

```powershell
uv run python -m managed_agents.console --check-cloud
```

Checks only read billing, Vertex API status, runtime account and PSC attachment metadata.
They never enable APIs, grant permissions or create resources. A missing resource is reported
separately from a permission/network failure. The report also identifies the local submission
lock. Model access, `actAs`, quotas, organization policies and the security review still require
separate verification. Google requests use the configured target project as ADC quota context
in memory; saved ADC credentials and their existing quota-project setting are not modified.

The minimal example is [examples/source-agent/agent.py](../examples/source-agent/agent.py).
Select the repository's `examples/source-agent` folder, keep module `agent` / object `root_agent`,
and enter an agent name to review it. This example echoes the provided message; it makes no LLM calls.

## Enable Real Deployment

Complete the infrastructure and security gates in [deployment-plan.md](deployment-plan.md),
including the runtime account, PSC attachment, billing, quotas and perimeter review. The console
does not create those prerequisites, grant IAM, enable APIs or configure an organization perimeter.
It uses the configured account and PSC attachment on every Agent Engine create request.
The current upload flow does not add private gateway DNS peering or environment/secret variables;
agents that require those need an extended, reviewed deployment configuration first.

Authenticate Application Default Credentials directly in your terminal, not through the frontend:

```powershell
gcloud auth application-default login
```

The operator needs Agent Engine create/get and operation-read permissions, and permission to
act as the selected service account. The Agent Platform service identities also need the
documented deployment/build and PSC permissions. No service-account key upload is supported.

After the provisioning plan and security review are actually approved, restart the console with:

```powershell
$env:GOOGLE_CLOUD_PROJECT = "itsai-mai-gcp-lab-proj"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"
$env:CONSOLE_ALLOW_DEPLOY = "1"
$env:CONSOLE_SECURITY_REVIEW_APPROVED = "1"
uv run python -m managed_agents.console
```

Optional operator configuration:

| Variable | Default |
| --- | --- |
| `CONSOLE_PORT` | `8088` |
| `CONSOLE_SOURCE_ROOT` | Current directory |
| `CONSOLE_RUNTIME_ACCOUNT` | `agent-research@PROJECT.iam.gserviceaccount.com` |
| `CONSOLE_NETWORK_ATTACHMENT` | `projects/PROJECT/regions/REGION/networkAttachments/managed-agent-runtime` |

The frontend deliberately cannot choose arbitrary projects or more-privileged service accounts.
Change those allowlisted values in operator configuration before starting the process.
Use a unique least-privilege runtime account per deployed agent in production; the default
research account is intended for one lab agent, not a shared production identity.

Review the snapshot, check the code/cost approval box, and type the exact project ID. The
Deploy button submits **that immutable snapshot**, not the latest folder contents. Agent Engine
creates a new resource on each approved plan; this is not an update/rollback or delete interface.
Submission immediately incurs possible build/runtime/model charges and is not automatically undone.

The browser polls the Google long-running operation through the console API. It reports success
only when Google reports completion without an error. If submission times out, status is
**Needs verification**; check Google Cloud before creating another deployment to avoid duplicates.
Credential failures are reported without exposing credential material. No secrets go into URLs.

Plans and status are not durable: restart loses local history, not GCP resources. Drafts/uploads
expire after 30 minutes; up to eight plans and two pending uploads are allowed per process.
Identical ZIP uploads reuse a staging slot, and review retries reuse the staged upload.
Use **Clear staged uploads** in the ZIP section to discard abandoned uploads without restarting.
This clears staging across console tabs but does not delete reviewed plans or GCP resources.
Record operation/resource IDs or download the plan before restarting. Delete unused resources
through Google Cloud change control to stop ongoing costs. Resource registration into the platform
registry, Kong routes and consumer access remain separate reviewed steps after deployment.

## Verification

```powershell
uv run python -m pytest tests/test_sources.py tests/test_console.py -q
```

These tests fake the cloud API and assert no code execution or cloud changes during review,
snapshot consistency, CSRF/Host rejection, ZIP handling and approval gates. A live Agent Engine
build remains unverified until the prerequisites are configured and a deployment is approved.

Local visual assets: Lucide 0.468.0 (ISC), IBM Plex Sans (SIL Open Font License), and the
Google Cloud logo from VectorLogoZone. The accompanying Lucide and font license files are in
the frontend static directory. The Google Cloud logo remains subject to its owner's brand rules.