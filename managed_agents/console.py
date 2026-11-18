import argparse
import base64
import dataclasses
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import google.auth
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.auth.transport.requests import AuthorizedSession
from pydantic import Field
from requests.exceptions import HTTPError, RequestException

from managed_agents.policy import StrictModel
from managed_agents.sandbox import SandboxError, SandboxRunner
from managed_agents.sources import (
    MAX_SOURCE_BYTES,
    Bundle,
    SourceError,
    bundle_source,
    local_files,
    zip_files,
)

WEB_ROOT = Path(__file__).resolve().parent / "web"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"


def verify_iap_assertion(assertion: str, audience: str) -> dict:
    """Verify the signed header IAP adds to every request it lets through."""
    from google.oauth2 import id_token

    claims = id_token.verify_token(assertion, google_requests.Request(), audience=audience,
                                   certs_url=IAP_CERTS_URL)
    if claims.get("iss") != IAP_ISSUER or not claims.get("email"):
        raise ValueError("Not an IAP assertion")
    return claims


@dataclass(frozen=True)
class ConsoleConfig:
    source_root: Path
    project: str = "itsai-mai-gcp-lab-proj"
    region: str = "us-central1"
    port: int = 8088
    allow_deploy: bool = False
    security_review_approved: bool = False
    runtime_account: str = ""
    network_attachment: str = ""
    sandbox_engine: str = ""
    allow_sandbox_test: bool = False
    require_sandbox_test: bool = False
    hosted: bool = False
    iap_audience: str = ""
    allowed_users: frozenset = frozenset()

    @property
    def sandbox_enabled(self):
        return self.allow_sandbox_test and bool(self.sandbox_engine)

    @property
    def account(self):
        """Runtime service account; "" means Agent Engine's default identity (CONSOLE_RUNTIME_ACCOUNT=default)."""
        if self.runtime_account == "default":
            return ""
        return self.runtime_account or f"agent-research@{self.project}.iam.gserviceaccount.com"

    @property
    def attachment(self):
        """PSC network attachment; "" means no private networking (CONSOLE_NETWORK_ATTACHMENT=none)."""
        if self.network_attachment == "none":
            return ""
        return self.network_attachment or (
            f"projects/{self.project}/regions/{self.region}/networkAttachments/managed-agent-runtime")

    @classmethod
    def from_env(cls):
        hosted = os.environ.get("CONSOLE_HOSTED") == "1"
        port = os.environ.get("PORT") if hosted else None  # Cloud Run sets PORT
        return cls(
            source_root=Path(os.environ.get("CONSOLE_SOURCE_ROOT", str(Path.cwd()))).resolve(),
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "itsai-mai-gcp-lab-proj"),
            region=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            allow_deploy=os.environ.get("CONSOLE_ALLOW_DEPLOY") == "1",
            security_review_approved=os.environ.get("CONSOLE_SECURITY_REVIEW_APPROVED") == "1",
            runtime_account=os.environ.get("CONSOLE_RUNTIME_ACCOUNT", ""),
            network_attachment=os.environ.get("CONSOLE_NETWORK_ATTACHMENT", ""),
            sandbox_engine=os.environ.get("CONSOLE_SANDBOX_ENGINE", ""),
            allow_sandbox_test=os.environ.get("CONSOLE_ALLOW_SANDBOX_TEST") == "1",
            require_sandbox_test=os.environ.get("CONSOLE_REQUIRE_SANDBOX_TEST") == "1",
            hosted=hosted,
            port=int(port or os.environ.get("CONSOLE_PORT", "8088")),
            iap_audience=os.environ.get("CONSOLE_IAP_AUDIENCE", ""),
            allowed_users=frozenset(user.strip().lower() for user in
                                    os.environ.get("CONSOLE_ALLOWED_USERS", "").split(",")
                                    if user.strip()),
        )


class DeploymentInput(StrictModel):
    source_kind: Literal["local", "github", "zip"]
    location: str = Field(min_length=1, max_length=1024)
    revision: str = Field(default="", max_length=40)
    subdirectory: str = Field(default="", max_length=200)
    display_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9 _.-]*$")
    module: str = Field(default="agent", pattern=r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*$", max_length=150)
    object_name: str = Field(default="root_agent", pattern=r"^[a-zA-Z_]\w*$", max_length=100)
    requirements: str = Field(default="auto", min_length=1, max_length=150)
    framework: Literal["custom", "google-adk"] = "custom"


class AgentTest(StrictModel):
    name: str = Field(max_length=200)
    method: str = Field(pattern=r"^[a-zA-Z_]\w{0,79}$")
    mode: Literal["query", "stream"] = "query"
    input: dict = Field(default_factory=dict)


class Approval(StrictModel):
    digest: str
    project: str
    approve_cost: bool
    approve_code: bool


AGENT_LIST_FIELDS = (
    "nextPageToken,reasoningEngines(name,displayName,description,createTime,updateTime,labels,"
    "spec(agentFramework,classMethods,serviceAccount,deploymentSpec(maxInstances,pscInterfaceConfig),"
    "sourceCodeSpec(pythonSpec),packageSpec(pythonVersion),containerSpec(imageUri)))"
)


class _ReusedSession:
    """Context manager that hands out a pooled session without closing it on exit."""

    def __init__(self, cloud, session):
        self.cloud, self.session = cloud, session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None and issubclass(exc_type, GoogleAuthError):
            self.cloud.reset_credentials()  # e.g. expired login: re-read ADC next time
        return False


class AgentEngineCloud:
    def __init__(self, config: ConsoleConfig):
        self.config = config
        self._credentials = None
        self._sessions = threading.local()
        self._credentials_lock = threading.Lock()

    def reset_credentials(self):
        with self._credentials_lock:
            self._credentials = None
        self._sessions = threading.local()

    def session(self):
        """Authorized session reusing one credential (and its access token) and one connection
        pool per thread. Loading ADC and minting a token on every call made each request slow."""
        with self._credentials_lock:
            if self._credentials is None:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
                if hasattr(credentials, "with_quota_project"):
                    credentials = credentials.with_quota_project(self.config.project)
                self._credentials = credentials
            credentials = self._credentials
        session = getattr(self._sessions, "session", None)
        if session is None or session.credentials is not credentials:
            session = AuthorizedSession(credentials)
            self._sessions.session = session
        return _ReusedSession(self, session)

    def preflight(self):
        project = self.config.project
        checks = [{"id": "submission", "status": "passed" if (
            self.config.allow_deploy and self.config.security_review_approved) else "blocked",
            "message": "Cloud submission enabled" if (
                self.config.allow_deploy and self.config.security_review_approved) else
            "Console is locked: operator deployment and security approval flags are not both enabled"}]
        endpoints = [
            ("billing", f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo", "billingEnabled", True),
            ("vertex_api", f"https://serviceusage.googleapis.com/v1/projects/{project}/services/aiplatform.googleapis.com", "state", "ENABLED"),
        ]
        if self.config.account:
            endpoints.append(("runtime_account", f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{self.config.account}", "disabled", False))
        else:
            checks.append({"id": "runtime_account", "status": "skipped",
                           "message": "Using the Agent Engine default service agent (CONSOLE_RUNTIME_ACCOUNT=default)"})
        if self.config.attachment:
            endpoints.append(("psc_attachment", f"https://compute.googleapis.com/compute/v1/{self.config.attachment}", "name", None))
        else:
            checks.append({"id": "psc_attachment", "status": "skipped",
                           "message": "No private networking (CONSOLE_NETWORK_ATTACHMENT=none)"})
        if self.config.sandbox_engine:
            engine_url = (f"https://{self.config.region}-aiplatform.googleapis.com/"
                          f"v1/{self.config.sandbox_engine}")
            endpoints.append(("sandbox_engine", engine_url, "name", None))
        try:
            with self.session() as client:
                for name, url, field, expected in endpoints:
                    try:
                        response = client.get(url, timeout=15)
                        if response.status_code == 200:
                            payload = response.json()
                            present = field in payload or (name == "runtime_account" and "email" in payload)
                            value = payload.get(field, False if name == "runtime_account" else None)
                            passed = present and (bool(value) if expected is None else value == expected)
                            checks.append({"id": name, "status": "passed" if passed else "blocked",
                                           "message": "Verified" if passed else "Required resource is disabled or not ready"})
                        else:
                            checks.append({"id": name, "status": "missing" if response.status_code == 404 else "unknown",
                                "message": "Resource was not found" if response.status_code == 404 else
                                f"Could not verify (HTTP {response.status_code}); check API access and IAM"})
                    except (RequestException, ValueError):
                        checks.append({"id": name, "status": "unknown", "message": "Read-only cloud check failed; retry or check API access"})
        except GoogleAuthError:
            checks.append({"id": "credentials", "status": "blocked",
                           "message": "Application Default Credentials are missing, expired or cannot be refreshed"})
        return {"project": project, "region": self.config.region, "quota_project": project,
                "checks": checks,
                "note": "Read-only checks only; model permissions, actAs, quotas, organization policies and security review require separate verification"}

    def submit(self, body: dict):
        with self.session() as client:
            response = client.post(
                f"https://{self.config.region}-aiplatform.googleapis.com/v1/projects/"
                f"{self.config.project}/locations/{self.config.region}/reasoningEngines",
                json=body, timeout=60,
            )
            response.raise_for_status()
            return response.json()

    def sandbox_test(self, bundle: Bundle, module: str, object_name: str, framework: str):
        runner = SandboxRunner(self.session, self.config.region, self.config.sandbox_engine)
        return runner.run(bundle.archive, bundle.digest, module, object_name, framework)

    def create_sandbox_engine(self):
        """Create an empty Agent Engine instance that only hosts test sandboxes."""
        with self.session() as client:
            response = client.post(
                f"https://{self.config.region}-aiplatform.googleapis.com/v1/projects/"
                f"{self.config.project}/locations/{self.config.region}/reasoningEngines",
                json={"displayName": "agent-platform-sandbox-host",
                      "description": "Hosts code-execution test sandboxes for the deployment console"},
                timeout=60)
            response.raise_for_status()
            operation = response.json()["name"]
        match = re.match(r"(projects/[^/]+/locations/[^/]+/reasoningEngines/[0-9]+)/operations/", operation)
        if not match:
            raise SourceError("Cloud returned an unexpected operation name")
        return match.group(1)

    def container_logs(self, engine_id: str, since: float, limit: int = 80):
        """Recent stdout/stderr lines of an Agent Engine container from Cloud Logging, oldest first."""
        if not re.fullmatch(r"[0-9]+", engine_id):
            raise SourceError("Unexpected Agent Engine ID")
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(0, since - 300)))
        with self.session() as client:
            response = client.post("https://logging.googleapis.com/v2/entries:list", json={
                "resourceNames": [f"projects/{self.config.project}"],
                "filter": (f'resource.type="aiplatform.googleapis.com/ReasoningEngine" '
                           f'AND resource.labels.reasoning_engine_id="{engine_id}" '
                           f'AND timestamp>="{start}"'),
                "orderBy": "timestamp desc", "pageSize": limit,
            }, timeout=30)
            response.raise_for_status()
            entries = response.json().get("entries", [])
        lines = []
        for entry in reversed(entries):
            payload = entry.get("textPayload")
            if payload is None:
                body = entry.get("jsonPayload", {}) or {}
                payload = body.get("message") or json.dumps(body)[:2000]
            lines.append({"time": entry.get("timestamp", ""), "severity": entry.get("severity", "DEFAULT"),
                          "stream": "stderr" if str(entry.get("logName", "")).endswith("stderr") else "stdout",
                          "text": str(payload)[:4000]})
        return lines

    def invoke_agent(self, name: str, method: str, payload: dict, stream: bool):
        """Call a deployed agent's class method, as the Agent Platform SDK does."""
        pattern = r"projects/[a-z0-9-]+/locations/" + re.escape(self.config.region) + r"/reasoningEngines/[0-9]+"
        if not re.fullmatch(pattern, name):
            raise SourceError("Unexpected agent resource name")
        base = f"https://{self.config.region}-aiplatform.googleapis.com/v1/{name}"
        body = {"classMethod": method, "input": payload}
        with self.session() as client:
            if not stream:
                response = client.post(f"{base}:query", json=body, timeout=180)
                response.raise_for_status()
                return {"output": response.json().get("output")}
            events, size = [], 0
            with client.post(f"{base}:streamQuery?alt=sse", json=body, timeout=180,
                             stream=True) as response:
                response.raise_for_status()
                for raw in response.iter_lines(decode_unicode=True):
                    line = (raw or "").strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    size += len(line)
                    if size > 400_000 or len(events) >= 300:
                        break
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        events.append(line)
            return {"events": events}

    def list_agents(self, max_pages: int = 5):
        """Agent Engine resources in the project and region (the console's Agent Platform runtimes)."""
        url = (f"https://{self.config.region}-aiplatform.googleapis.com/v1/projects/"
               f"{self.config.project}/locations/{self.config.region}/reasoningEngines")
        agents, token, fields = [], "", AGENT_LIST_FIELDS
        with self.session() as client:
            for _ in range(max_pages):
                params = {"pageSize": 100, **({"pageToken": token} if token else {})}
                if fields:
                    # Partial response: skip each agent's stored source archive, which can be
                    # megabytes per agent and made the list slow.
                    params["fields"] = fields
                response = client.get(url, params=params, timeout=30)
                if fields and response.status_code == 400:
                    fields = ""  # API refused the field mask; fall back to full resources
                    params.pop("fields")
                    response = client.get(url, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                agents.extend(payload.get("reasoningEngines", []))
                token = payload.get("nextPageToken", "")
                if not token:
                    break
        return agents

    def status(self, name: str):
        pattern = (r"projects/[a-zA-Z0-9-]+/locations/" + re.escape(self.config.region)
                   + r"/(?:reasoningEngines/[a-zA-Z0-9_-]+/)?operations/[a-zA-Z0-9_-]+")
        if not re.fullmatch(pattern, name):
            raise SourceError("Cloud returned an unexpected operation name")
        with self.session() as client:
            response = client.get(
                f"https://{self.config.region}-aiplatform.googleapis.com/v1/{name}", timeout=20)
            response.raise_for_status()
            return response.json()


def google_error(error: HTTPError, limit: int = 400) -> str:
    """Short, operator-facing summary of a Google API error: HTTP code, status and message."""
    response = error.response
    if response is None:
        return "no response"
    detail = ""
    try:
        body = response.json().get("error", {})
        detail = ": ".join(part for part in (body.get("status", ""), body.get("message", "")) if part)
    except (ValueError, AttributeError):
        pass
    return f"HTTP {response.status_code}" + (f": {detail[:limit]}" if detail else "")


def find_logs_url(value) -> str:
    """Google attaches the failed container's logs link to the operation error's details."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"logs_url", "logsUrl"} and isinstance(item, str) and item.startswith("https://"):
                return item
            found = find_logs_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_logs_url(item)
            if found:
                return found
    return ""


LOG_HINTS = [
    (r"No module named 'vertexai",
     ("The Google ADK wrapper needs google-cloud-aiplatform[agent_engines,adk] "
      "in the agent's dependencies.")),
    (r"No module named '(?!vertexai)([\w.]+)'",
     ("Python could not import '{0}'. Add the package that provides it to requirements.txt "
      "(or pyproject.toml/uv.lock) and redeploy.")),
    (r"attempted relative import|ImportError: cannot import name",
     ("An import inside the agent failed. If agent.py uses relative imports (from . import ...), "
      "set the entrypoint module to the package path, for example weather_agent.agent, "
      "or use absolute imports.")),
    (r"has no attribute '(\w+)'",
     ("The entrypoint object is missing an attribute '{0}'. Check the entrypoint object name "
      "and the selected agent framework.")),
    (r"GOOGLE_API_KEY|API key",
     ("The agent expects an API key. On Agent Engine, use Vertex AI credentials "
      "(GOOGLE_GENAI_USE_VERTEXAI=true) instead of an API key.")),
    (r"PermissionDenied|403 ",
     "The agent's runtime identity lacks a permission it needs at startup."),
]


def log_hints(lines: list[dict]) -> list[str]:
    text = "\n".join(line["text"] for line in lines)
    hints = []
    for pattern, message in LOG_HINTS:
        match = re.search(pattern, text)
        if match:
            hint = message.format(*match.groups())
            if hint not in hints:
                hints.append(hint)
    return hints[:3]


def summarize_events(events: list) -> dict:
    """Pull the reply text and tool activity out of ADK-style stream events."""
    reply, tools, errors = [], [], []
    for event in events:
        if not isinstance(event, dict):
            reply.append(str(event))
            continue
        # ADK reports failures (model errors, timeouts) as events with an error message, not text.
        problem = event.get("error_message") or event.get("errorMessage")
        if problem or event.get("error_code") or event.get("errorCode"):
            code = event.get("error_code") or event.get("errorCode") or ""
            errors.append(f"{code}: {problem}" if code and problem else str(problem or code))
        content = event.get("content") or {}
        for part in content.get("parts", []) if isinstance(content, dict) else []:
            if not isinstance(part, dict):
                continue
            if part.get("text") and not part.get("thought"):
                reply.append(part["text"])
            call = part.get("function_call") or part.get("functionCall")
            if call:
                tools.append({"kind": "call", "name": call.get("name", ""),
                              "data": json.dumps(call.get("args", {}))[:500]})
            result = part.get("function_response") or part.get("functionResponse")
            if result:
                tools.append({"kind": "result", "name": result.get("name", ""),
                              "data": json.dumps(result.get("response", {}))[:500]})
    return {"reply": "".join(reply).strip(), "tools": tools, "errors": errors}


def operation_error(error: dict) -> str:
    message = str(error.get("message", "")).strip()
    return message[:600] if message else f"error code {error.get('code', 'unknown')}"


def source_warnings(files: dict[str, bytes], framework: str) -> list[str]:
    """Hints about common mistakes; never blocks a deployment."""
    def text(name):
        return files[name].decode("utf-8", "ignore")

    python = " ".join(text(name) for name in files if name.endswith(".py"))
    dependencies = " ".join(text(name).lower() for name in files
                            if name.rsplit("/", 1)[-1] in {"requirements.txt", "pyproject.toml", "uv.lock"})
    warnings = []
    if framework == "custom" and ("google.adk" in python or "from google import adk" in python):
        warnings.append("This source imports Google ADK. Choose the Google ADK framework, otherwise "
                        "Agent Engine will call a query() method an ADK agent does not have.")
    if framework == "google-adk" and "google-cloud-aiplatform" not in dependencies:
        warnings.append("Google ADK deployments need google-cloud-aiplatform[agent_engines,adk] in the "
                        "dependencies; it was not found, so the deployment will likely fail to start.")
    return warnings


def agent_summary(engine: dict) -> dict:
    """The fields of a reasoningEngine resource that the Agent garden page shows."""
    spec = engine.get("spec", {}) or {}
    source = spec.get("sourceCodeSpec", {}) or {}
    labels = engine.get("labels", {}) or {}
    deployment = spec.get("deploymentSpec", {}) or {}
    name = str(engine.get("name", ""))
    return {
        "name": name,
        "id": name.rsplit("/", 1)[-1],
        "display_name": engine.get("displayName", "") or name.rsplit("/", 1)[-1],
        "description": engine.get("description", ""),
        "create_time": engine.get("createTime", ""),
        "update_time": engine.get("updateTime", ""),
        "framework": spec.get("agentFramework", ""),
        "methods": [method.get("name", "") for method in spec.get("classMethods", []) or []
                    if isinstance(method, dict)],
        # The JSON schema each class method accepts, so the console can build the right input.
        "method_schemas": {method.get("name", ""): method.get("parameters") or {}
                           for method in spec.get("classMethods", []) or []
                           if isinstance(method, dict) and method.get("name")},
        "service_account": spec.get("serviceAccount", ""),
        "deployment": ("source code" if source else "package" if spec.get("packageSpec")
                       else "container" if spec.get("containerSpec") else "none"),
        "entrypoint": ".".join(part for part in (
            (source.get("pythonSpec", {}) or {}).get("entrypointModule", ""),
            (source.get("pythonSpec", {}) or {}).get("entrypointObject", "")) if part),
        "max_instances": deployment.get("maxInstances"),
        "private_network": bool(deployment.get("pscInterfaceConfig")),
        "managed_by_console": labels.get("managed-by") == "agent-platform-console",
        "source_sha": labels.get("source-sha", ""),
    }


def github_files(location: str, revision: str):
    match = re.fullmatch(r"https://github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?/?",
                         location)
    if not match or not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise SourceError("Use a public github.com repository URL and a full 40-character commit SHA")
    owner, repository = match.groups()
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{revision}"
    payload = io.BytesIO()
    with (httpx.Client(timeout=30, follow_redirects=False) as client,
          client.stream("GET", url) as response):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if payload.tell() + len(chunk) > MAX_SOURCE_BYTES:
                raise SourceError("GitHub archive exceeds the 8 MiB limit")
            payload.write(chunk)
    return zip_files(payload.getvalue())


def deployment_body(config: ConsoleConfig, data: DeploymentInput, bundle: Bundle):
    body = {
        "displayName": data.display_name,
        "labels": {"managed-by": "agent-platform-console", "source-sha": bundle.digest[:63]},
        "spec": {
            "agentFramework": bundle.framework,
            "sourceCodeSpec": {
                "inlineSource": {"sourceArchive": base64.b64encode(bundle.archive).decode("ascii")},
                "pythonSpec": {"version": "3.12", "entrypointModule": bundle.entrypoint_module or data.module,
                               "entrypointObject": bundle.entrypoint_object,
                               "requirementsFile": bundle.requirements},
            },
            # Read from the agent's own source (see sources.infer_class_methods).
            "classMethods": [dict(method) for method in bundle.class_methods] or [
                {"name": "query", "api_mode": "", "parameters": {
                    "type": "object", "properties": {"message": {"type": "string"}},
                    "required": ["message"]}}],
            "deploymentSpec": {
                "minInstances": 0, "maxInstances": 2,
                "resourceLimits": {"cpu": "1", "memory": "1Gi"},
            },
        },
    }
    if config.account:
        body["spec"]["serviceAccount"] = config.account
    if config.attachment:
        body["spec"]["deploymentSpec"]["pscInterfaceConfig"] = {"networkAttachment": config.attachment}
    if bundle.framework == "google-adk":
        body["spec"]["classMethods"] = [
            {"name": "async_create_session", "api_mode": "async", "parameters": {
                "type": "object", "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            }},
            {"name": "async_stream_query", "api_mode": "async_stream", "parameters": {
                "type": "object", "properties": {"user_id": {"type": "string"},
                    "message": {"type": "string"}, "session_id": {"type": "string"}},
                "required": ["user_id", "message"],
            }},
        ]
        body["spec"]["deploymentSpec"]["env"] = [
            {"name": "GOOGLE_GENAI_USE_VERTEXAI", "value": "true"}]
    return body


def create_console(config: ConsoleConfig | None = None, *, cloud=None,
                   iap_verifier=verify_iap_assertion) -> FastAPI:
    config = config or ConsoleConfig.from_env()
    if config.hosted and not re.fullmatch(
            r"/projects/[0-9]+/locations/[a-z0-9-]+/services/[a-z0-9-]+", config.iap_audience):
        raise ValueError("Hosted console requires CONSOLE_IAP_AUDIENCE "
                         "(/projects/NUMBER/locations/REGION/services/NAME)")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", config.project):
        raise ValueError("Invalid configured Google Cloud project")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", config.region):
        raise ValueError("Invalid configured Google Cloud region")
    cloud = cloud or AgentEngineCloud(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(32)
    lock = threading.RLock()
    plans = {}
    uploads = {}
    origins = {f"http://127.0.0.1:{config.port}", f"http://localhost:{config.port}"}

    def cleanup():
        cutoff = time.time() - 1800
        for key in list(uploads):
            if uploads[key][0] < cutoff:
                del uploads[key]
        for key in list(plans):
            if plans[key]["created_at"] < cutoff and plans[key]["status"] == "planned":
                del plans[key]

    def public_plan(plan):
        return {key: value for key, value in plan.items() if key not in {"bundle", "input"}}

    def run_sandbox_test(plan):
        data = plan["input"]
        try:
            result = cloud.sandbox_test(plan["bundle"], data.module, data.object_name, data.framework)
        except GoogleAuthError:
            result = {"status": "error",
                      "message": "Google Application Default Credentials are unavailable or invalid"}
        except HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            result = {"status": "error", "message": f"Google Cloud rejected the sandbox request ({code}). "
                      "Check the sandbox host engine, API enablement and roles/aiplatform.user."}
        except SandboxError as error:
            result = {"status": "error", "message": str(error)}
        except (RequestException, KeyError, ValueError):
            result = {"status": "error", "message": "The sandbox test could not be completed; retry"}
        result["finished_at"] = time.time()
        with lock:
            plan["sandbox"] = {**plan["sandbox"], **result}

    def hosted_user(request: Request) -> str | None:
        """Email of the IAP-authenticated caller, or None if the request must be refused."""
        assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
        if not assertion:
            return None
        try:
            email = str(iap_verifier(assertion, config.iap_audience)["email"]).lower()
        except (GoogleAuthError, ValueError, KeyError):
            return None
        if config.allowed_users and email not in config.allowed_users:
            return None
        return email

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if config.hosted:
            # Behind Cloud Run + IAP: every request must carry a valid IAP assertion.
            user = hosted_user(request)
            if user is None:
                return JSONResponse({"detail": "Sign-in through Identity-Aware Proxy required"},
                                    status_code=401)
            request.state.user = user
            if origin and origin != f"https://{request.headers.get('host', '')}":
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        else:
            if f"http://{request.headers.get('host', '')}" not in origins:
                return JSONResponse({"detail": "Loopback host required"}, status_code=403)
            if origin and origin not in origins:
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
            request.state.user = ""
        if request.url.path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get("x-console-token", ""), token):
                return JSONResponse({"detail": "Console session required"}, status_code=403)
            if request.method == "POST" and request.url.path != "/api/uploads":
                payload = bytearray()
                async for chunk in request.stream():
                    payload.extend(chunk)
                    if len(payload) > 16384:
                        return JSONResponse({"detail": "Request too large"}, status_code=413)
                request._body = bytes(payload)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if config.hosted:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(SourceError)
    async def source_error(request: Request, error: SourceError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(OSError)
    async def filesystem_error(request: Request, error: OSError):
        return JSONResponse({"detail": "Source folder is unavailable or cannot be read"}, status_code=400)

    @app.get("/")
    def index():
        return HTMLResponse((WEB_ROOT / "index.html").read_text(encoding="utf-8").replace(
            "__CONSOLE_TOKEN__", token))

    @app.get("/api/config")
    def configuration(request: Request):
        return {"project": config.project, "region": config.region,
                "hosted": config.hosted, "user": request.state.user,
                "local_sources_enabled": not config.hosted,
                "source_root": "" if config.hosted else str(config.source_root),
                "runtime_account": config.account,
                "network_attachment": config.attachment,
                "deployment_enabled": config.allow_deploy and config.security_review_approved,
                "sandbox_enabled": config.sandbox_enabled, "sandbox_engine": config.sandbox_engine,
                "sandbox_required": config.require_sandbox_test}

    @app.get("/api/plans/{plan_id}/logs")
    def plan_logs(plan_id: str):
        with lock:
            plan = plans.get(plan_id)
            if plan is None:
                raise HTTPException(404, "Plan expired or was not found")
            operation, since = plan["operation"], plan["submitted_at"] or plan["created_at"]
            resource = plan["resource"]
        match = re.search(r"/reasoningEngines/([0-9]+)", operation) or re.search(
            r"/reasoningEngines/([0-9]+)$", resource)
        if not match:
            raise HTTPException(409, "This snapshot has not created an Agent Engine resource yet")
        engine_id = match.group(1)
        query = (f'resource.type="aiplatform.googleapis.com/ReasoningEngine"\n'
                 f'resource.labels.reasoning_engine_id="{engine_id}"')
        explorer = (f"https://console.cloud.google.com/logs/query;query={quote(query, safe='')}"
                    f"?project={config.project}")
        try:
            lines = cloud.container_logs(engine_id, since)
        except GoogleAuthError:
            raise HTTPException(502, "Google credentials are unavailable or expired") from None
        except HTTPError as error:
            raise HTTPException(502, f"Could not read container logs ({google_error(error)}). "
                                     "Reading logs needs roles/logging.viewer.") from None
        except (RequestException, ValueError, SourceError):
            raise HTTPException(502, "Could not reach Cloud Logging; retry") from None
        return {"engine_id": engine_id, "lines": lines, "hints": log_hints(lines),
                "explorer_url": explorer}

    @app.post("/api/agents/test")
    def test_agent(data: AgentTest):
        try:
            result = cloud.invoke_agent(data.name, data.method, data.input, data.mode == "stream")
        except SourceError as error:
            raise HTTPException(400, str(error)) from None
        except GoogleAuthError:
            raise HTTPException(502, "Google credentials are unavailable or expired") from None
        except HTTPError as error:
            # Agents explain bad input in their error text, so keep enough of it to be useful.
            raise HTTPException(502, f"The agent call failed ({google_error(error, limit=2000)})") from None
        except (RequestException, ValueError):
            raise HTTPException(502, "Could not reach the agent; it may still be starting. Retry.") from None
        if data.mode == "stream":
            summary = summarize_events(result["events"])
            return {**summary, "events": result["events"][-50:], "event_count": len(result["events"])}
        output = result["output"]
        reply = output if isinstance(output, str) else ""
        if isinstance(output, dict):
            for key in ("reply", "output", "text", "answer", "response", "content"):
                if isinstance(output.get(key), str):
                    reply = output[key]
                    break
        return {"reply": reply, "output": output, "tools": []}

    agents_cache = {"at": 0.0, "data": None}

    @app.get("/api/agents")
    def list_agents(refresh: bool = False):
        with lock:
            cached = agents_cache["data"]
            age = time.time() - agents_cache["at"]
        if cached is not None and not refresh and age < 60:
            return {**cached, "cached": True, "age_seconds": round(age)}
        started = time.perf_counter()
        try:
            engines = cloud.list_agents()
        except GoogleAuthError:
            raise HTTPException(502, "Google credentials are unavailable or expired; run "
                                     "gcloud auth application-default login") from None
        except HTTPError as error:
            raise HTTPException(502, f"Could not list agents ({google_error(error)})") from None
        except (RequestException, ValueError):
            raise HTTPException(502, "Could not reach Google Cloud to list agents; retry") from None
        agents = sorted((agent_summary(engine) for engine in engines if isinstance(engine, dict)),
                        key=lambda agent: agent["create_time"], reverse=True)
        data = {"project": config.project, "region": config.region, "agents": agents,
                "fetch_ms": round((time.perf_counter() - started) * 1000)}
        with lock:
            agents_cache.update(at=time.time(), data=data)
        return {**data, "cached": False, "age_seconds": 0}

    @app.get("/api/preflight")
    def preflight():
        return cloud.preflight()

    @app.post("/api/uploads")
    async def upload(request: Request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_SOURCE_BYTES:
                raise HTTPException(413, "ZIP exceeds the 8 MiB upload limit")
        files = zip_files(bytes(content))
        digest = hashlib.sha256(content).hexdigest()
        with lock:
            cleanup()
            for upload_id, record in uploads.items():
                if record[2] == digest:
                    uploads[upload_id] = (time.time(), record[1], digest)
                    return {"id": upload_id, "file_count": len(record[1])}
            if len(uploads) >= 2:
                raise HTTPException(429, "Two ZIPs are staged. Clear staged uploads or review an existing ZIP.")
            upload_id = secrets.token_urlsafe(18)
            uploads[upload_id] = (time.time(), files, digest)
        return {"id": upload_id, "file_count": len(files)}

    @app.delete("/api/uploads")
    def clear_uploads():
        with lock:
            count = len(uploads)
            uploads.clear()
        return {"discarded": count}

    @app.post("/api/plans")
    def prepare(data: DeploymentInput, request: Request):
        if config.hosted and data.source_kind == "local":
            raise HTTPException(400, "Local folders are not available in the hosted console; "
                                     "use a GitHub commit or a ZIP upload")
        with lock:
            cleanup()
            if len(plans) >= 8:
                raise HTTPException(429, "Console plan limit reached; restart after recording operation IDs")
        if data.source_kind == "local":
            files = local_files(data.location, config.source_root)
        elif data.source_kind == "github":
            try:
                files = github_files(data.location, data.revision)
            except httpx.HTTPError:
                raise HTTPException(400, "Public GitHub archive could not be downloaded") from None
        else:
            with lock:
                record = uploads.get(data.location)
                if record is None:
                    raise HTTPException(400, "Upload expired; choose the ZIP again")
                files = record[1]
        bundle = bundle_source(files, data.module, data.requirements, data.subdirectory,
                       data.framework, data.object_name)
        plan = {"id": secrets.token_urlsafe(18), "display_name": data.display_name,
                "source_kind": data.source_kind, "source": data.location if data.source_kind != "zip" else "ZIP upload",
                "digest": bundle.digest, "files": list(bundle.files), "total_bytes": bundle.total_bytes,
                "project": config.project, "region": config.region, "runtime_account": config.account,
                "entrypoint": f"{data.module}.{data.object_name}",
                "framework": bundle.framework,
                "dependency_format": bundle.dependency_format, "requirements_file": bundle.requirements,
                "created_at": time.time(), "status": "planned", "operation": "", "resource": "",
                "message": "Snapshot ready for approval", "sandbox": {"status": "not_run"},
                "reviewed_by": request.state.user, "approved_by": "",
                "warnings": source_warnings(files, data.framework) + ([] if bundle.methods_inferred or bundle.framework != "custom" else [
                    (f"Couldn't read the query() signature of {data.module}.{data.object_name} from its source, "
                     "so it is registered as query(message). Callers must send a 'message'.")]),
                "class_methods": [dict(method) for method in bundle.class_methods],
                "status_check": "",
                "submitted_at": 0, "completed_at": 0, "logs_url": "",
                "bundle": bundle, "input": data}
        with lock:
            if len(plans) >= 8:
                raise HTTPException(429, "Console plan limit reached")
            plans[plan["id"]] = plan
            if data.source_kind == "zip":
                uploads.pop(data.location, None)
        return public_plan(plan)

    @app.get("/api/plans")
    def list_plans():
        with lock:
            cleanup()
            return {"plans": [public_plan(plan) for plan in reversed(list(plans.values()))]}

    @app.post("/api/plans/{plan_id}/deploy")
    def deploy(plan_id: str, approval: Approval, request: Request):
        if not config.allow_deploy or not config.security_review_approved:
            raise HTTPException(403, "Cloud deployment is locked by the operator configuration")
        with lock:
            cleanup()
            plan = plans.get(plan_id)
            if plan is None:
                raise HTTPException(404, "Plan expired or was not found")
            if (not approval.approve_cost or not approval.approve_code
                    or approval.project != config.project
                    or not hmac.compare_digest(approval.digest, plan["digest"])):
                raise HTTPException(400, "Approve this exact snapshot, project and billable deployment")
            if plan["status"] != "planned":
                raise HTTPException(409, "This snapshot has already been submitted")
            if config.require_sandbox_test and plan["sandbox"]["status"] != "passed":
                raise HTTPException(409, "This snapshot must pass a GCP sandbox test before deployment")
            plan["status"] = "submitting"
            plan["approved_by"] = request.state.user
        try:
            body = deployment_body(config, plan["input"], plan["bundle"])
            try:
                operation = cloud.submit(body)
            except HTTPError as error:
                # A 400 about the method schema read from source: deploy with query(message) instead.
                code = error.response.status_code if error.response is not None else 0
                if code != 400 or not plan["bundle"].methods_inferred or not re.search(
                        r"class_?methods|parameters|schema", google_error(error), re.IGNORECASE):
                    raise
                plan["bundle"] = dataclasses.replace(plan["bundle"], class_methods=(), methods_inferred=False)
                operation = cloud.submit(deployment_body(config, plan["input"], plan["bundle"]))
            with lock:
                plan["operation"] = operation["name"]
                plan["status"] = "deploying"
                plan["submitted_at"] = time.time()
                plan["message"] = "Google Cloud accepted the deployment and is building the agent"
        except GoogleAuthError:
            with lock:
                plan["status"] = "failed"
                plan["message"] = "Google Application Default Credentials are unavailable or invalid"
        except HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            with lock:
                plan["status"] = "failed" if 400 <= code < 500 and code != 408 else "unknown"
                plan["message"] = (
                    f"Google Cloud rejected the request ({google_error(error)})"
                    if plan["status"] == "failed" else
                    f"Submission could not be confirmed ({google_error(error)}). "
                    "Check Google Cloud before retrying.")
        except (RequestException, KeyError, ValueError):
            with lock:
                plan["status"] = "unknown"
                plan["message"] = "Submission could not be confirmed. Check Google Cloud before retrying."
        with lock:
            return public_plan(plan)

    @app.post("/api/plans/{plan_id}/sandbox-test")
    def sandbox_test(plan_id: str):
        if not config.sandbox_enabled:
            raise HTTPException(403, "GCP sandbox testing is not enabled by the operator configuration")
        with lock:
            cleanup()
            plan = plans.get(plan_id)
            if plan is None:
                raise HTTPException(404, "Plan expired or was not found")
            if plan["sandbox"]["status"] == "running":
                raise HTTPException(409, "A sandbox test is already running for this snapshot")
            plan["sandbox"] = {"status": "running", "started_at": time.time(),
                               "message": "Creating a code-execution sandbox in Google Cloud"}
        threading.Thread(target=run_sandbox_test, args=(plan,), daemon=True).start()
        with lock:
            return public_plan(plan)

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: str):
        with lock:
            cleanup()
            plan = plans.get(plan_id)
            if plan is None:
                raise HTTPException(404, "Plan expired or was not found")
            operation = plan["operation"] if plan["status"] == "deploying" else ""
        if operation:
            # A failed status check never changes the deployment state: the operation keeps
            # running in Google Cloud, so report why the check failed and poll again later.
            result, problem = None, ""
            try:
                result = cloud.status(operation)
            except HTTPError as error:
                problem = f"Could not read deployment progress ({google_error(error)})"
            except GoogleAuthError:
                problem = "Could not read deployment progress: Google credentials expired or are unavailable"
            except SourceError as error:
                problem = f"Could not read deployment progress: {error} ({operation})"
            except (RequestException, ValueError):
                problem = "Could not reach Google Cloud to read deployment progress; retrying"
            with lock:
                plan["status_check"] = problem
                if result and result.get("done"):
                    plan["completed_at"] = time.time()
                    if result.get("error"):
                        plan["status"] = "failed"
                        plan["message"] = ("Agent Engine reported a deployment failure: "
                                           + operation_error(result["error"]))
                        plan["logs_url"] = find_logs_url(result["error"])
                    else:
                        plan["status"] = "succeeded"
                        agents_cache["at"] = 0.0
                        plan["resource"] = result.get("response", {}).get("name", "") or (
                            operation.split("/operations/")[0] if "/reasoningEngines/" in operation else "")
                        plan["message"] = "Agent Engine deployment completed"
        with lock:
            return public_plan(plan)

    app.mount("/static", StaticFiles(directory=str(WEB_ROOT), check_dir=False), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="Local Agent Engine deployment console")
    parser.add_argument("--check-cloud", action="store_true", help="Run read-only GCP prerequisite checks without starting the server")
    parser.add_argument("--create-sandbox-engine", action="store_true",
                        help="Create an empty Agent Engine instance to host test sandboxes, then exit")
    args = parser.parse_args()
    config = ConsoleConfig.from_env()
    if args.check_cloud:
        print(json.dumps(AgentEngineCloud(config).preflight(), indent=2), flush=True)
        print("Checks only; the console was not started. Run without --check-cloud to open "
              f"the frontend at http://127.0.0.1:{config.port}", file=sys.stderr, flush=True)
        return
    if args.create_sandbox_engine:
        if not config.allow_sandbox_test:
            parser.error("Set CONSOLE_ALLOW_SANDBOX_TEST=1 to create the sandbox host engine")
        engine = AgentEngineCloud(config).create_sandbox_engine()
        print(f"Set CONSOLE_SANDBOX_ENGINE={engine}", flush=True)
        return
    app = create_console(config)
    if config.hosted:
        # Cloud Run terminates TLS and IAP authenticates every request before it arrives.
        print(f"Agent Platform console (hosted) listening on port {config.port}", flush=True)
        uvicorn.run(app, host="0.0.0.0", port=config.port, access_log=False,
                    proxy_headers=False)
        return
    print(f"Agent Platform console: open http://127.0.0.1:{config.port} in your browser "
          "(Ctrl+C to stop)", flush=True)
    print(f"  Cloud submission: {'ENABLED' if config.allow_deploy and config.security_review_approved else 'LOCKED'}"
          f" (CONSOLE_ALLOW_DEPLOY={int(config.allow_deploy)},"
          f" CONSOLE_SECURITY_REVIEW_APPROVED={int(config.security_review_approved)})", flush=True)
    print(f"  Runtime account: {config.account or 'Agent Engine default service agent'}", flush=True)
    print(f"  Network attachment: {config.attachment or 'none'}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=config.port, access_log=False)


if __name__ == "__main__":
    main()