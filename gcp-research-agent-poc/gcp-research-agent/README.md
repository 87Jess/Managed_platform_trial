# LangChain Research Agent on Gemini Enterprise Agent Runtime

A single-agent research POC using LangChain, Gemini, Google Search grounding,
and managed Agent Runtime deployment.

## Architecture

User question -> Agent Runtime -> LangChainAgent -> Gemini model + Google
Search grounding -> evidence synthesis -> research response with sources.

## Prerequisites

- Google Cloud project with billing enabled.
- `gcloud` CLI installed.
- Python 3.10+ (Python 3.11 recommended).
- IAM permissions to enable APIs and deploy Agent Runtime resources.
- A region where the selected Gemini model and Agent Runtime are available.

## 1. Configure Google Cloud

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID

gcloud services enable \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  --project=YOUR_PROJECT_ID
```

Enabling APIs requires `serviceusage.services.enable`, commonly granted by
`roles/serviceusage.serviceUsageAdmin`.

The deploying identity also needs appropriate Agent Platform permissions.
For a POC, ask your cloud administrator for the least-privilege roles needed
to create and manage Agent Runtime resources and invoke the selected Gemini
model. Avoid granting broad Owner/Editor roles just for convenience.

## 2. Create a virtual environment

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
```

Install dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Configure environment

```bash
cp .env.example .env
```

On Windows, copy `.env.example` to `.env` manually if `cp` is unavailable.
Set `PROJECT_ID` to your actual project ID. Check that `LOCATION` supports
Agent Runtime and the selected Gemini model.

Default model is `gemini-3.5-flash`. Change `MODEL_NAME` to another model
available to your project and region if needed.

## 4. Test locally

```bash
python test_local.py
```

The first run may take time while the model initializes. The agent uses
Application Default Credentials; it does not require a Gemini API key.

## 5. Deploy to Agent Runtime

```bash
python deploy.py
```

The script creates a managed Agent Runtime resource and writes its resource
name to `.agent_resource`. Deployment can take several minutes.

## 6. Test the deployed agent

```bash
python test_remote.py
```

Alternatively, set `AGENT_RESOURCE_NAME` in `.env` to the full resource name.

## 7. Example research prompts

- Research MCP architecture, security risks, and enterprise adoption.
- Compare Google Agent Runtime, AWS Bedrock AgentCore, and Azure AI Foundry.
- Summarize current official guidance for deploying LangChain agents on GCP.
- Compare two technical approaches and identify trade-offs, risks, and gaps.

## 8. Troubleshooting

### Permission denied / 403
Check the active ADC identity (`gcloud auth application-default print-access-token`)
and ask your administrator to grant the required Agent Runtime, Vertex AI,
and API enablement permissions.

### API not enabled
Enable `aiplatform.googleapis.com` and `storage.googleapis.com` in the target
project. API enablement requires the appropriate Service Usage permission.

### Model not found / region error
Verify that `MODEL_NAME` is available in `LOCATION` for your project. Try a
supported model and region listed in current Google Cloud model documentation.

### Grounding tool errors
Confirm that Google Search grounding is supported for the selected model,
region, and project. The code uses Google's documented
`Tool.from_google_search_retrieval(grounding.GoogleSearchRetrieval())`
integration.

### Dependency conflicts
Use a clean virtual environment and reinstall `requirements.txt`. For
reproducible production builds, pin and test exact dependency versions after
the POC is validated.

## 9. Security and production considerations

- Never commit `.env`, service-account keys, or access tokens.
- Prefer workload identity / Agent Runtime identity over long-lived keys.
- Apply least-privilege IAM and organization policies.
- Treat retrieved web content as untrusted input.
- Log request metadata and operational errors, but avoid logging secrets or
  unnecessary personal data.
- Add request quotas, timeout handling, and cost controls before production.
- Evaluate citation accuracy, factual correctness, source relevance,
  completeness, hallucination rate, and task completion.
- Validate source links and claims before using reports for consequential
  decisions.

## 10. Cleanup

After recording the resource name, delete the Agent Runtime resource through
the Google Cloud console or the supported SDK/API delete operation for your
installed SDK version. Confirm the resource name and project before deletion.
