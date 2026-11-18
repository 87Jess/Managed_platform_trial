import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import google.auth
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from pydantic import Field
from requests.exceptions import HTTPError, RequestException

from managed_agents.policy import StrictModel
from managed_agents.sources import (
    MAX_SOURCE_BYTES,
    Bundle,
    SourceError,
    bundle_source,
    local_files,
    zip_files,
)

WEB_ROOT = Path(__file__).resolve().parent / "web"


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

    @property
    def account(self):
        return self.runtime_account or f"agent-research@{self.project}.iam.gserviceaccount.com"

    @property
    def attachment(self):
        return self.network_attachment or (
            f"projects/{self.project}/regions/{self.region}/networkAttachments/managed-agent-runtime")

    @classmethod
    def from_env(cls):
        return cls(
            source_root=Path(os.environ.get("CONSOLE_SOURCE_ROOT", str(Path.cwd()))).resolve(),
            project=os.environ.get("GOOGLE_CLOUD_PROJECT", "itsai-mai-gcp-lab-proj"),
            region=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            port=int(os.environ.get("CONSOLE_PORT", "8088")),
            allow_deploy=os.environ.get("CONSOLE_ALLOW_DEPLOY") == "1",
            security_review_approved=os.environ.get("CONSOLE_SECURITY_REVIEW_APPROVED") == "1",
            runtime_account=os.environ.get("CONSOLE_RUNTIME_ACCOUNT", ""),
            network_attachment=os.environ.get("CONSOLE_NETWORK_ATTACHMENT", ""),
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


class Approval(StrictModel):
    digest: str
    project: str
    approve_cost: bool
    approve_code: bool


class AgentEngineCloud:
    def __init__(self, config: ConsoleConfig):
        self.config = config

    def session(self):
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if hasattr(credentials, "with_quota_project"):
            credentials = credentials.with_quota_project(self.config.project)
        return AuthorizedSession(credentials)

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
            ("runtime_account", f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{self.config.account}", "disabled", False),
            ("psc_attachment", f"https://compute.googleapis.com/compute/v1/{self.config.attachment}", "name", None),
        ]
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


def github_files(location: str, revision: str):
    match = re.fullmatch(r"https://github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?/?",
                         location)
    if not match or not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise SourceError("Use a public github.com repository URL and a full 40-character commit SHA")
    owner, repository = match.groups()
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{revision}"
    payload = io.BytesIO()
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        with client.stream("GET", url) as response:
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
            "serviceAccount": config.account,
            "agentFramework": bundle.framework,
            "sourceCodeSpec": {
                "inlineSource": {"sourceArchive": base64.b64encode(bundle.archive).decode("ascii")},
                "pythonSpec": {"version": "3.12", "entrypointModule": bundle.entrypoint_module or data.module,
                               "entrypointObject": bundle.entrypoint_object,
                               "requirementsFile": bundle.requirements},
            },
            "classMethods": [{"name": "query", "api_mode": "", "parameters": {
                "type": "object", "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }}],
            "deploymentSpec": {
                "minInstances": 0, "maxInstances": 2,
                "resourceLimits": {"cpu": "1", "memory": "1Gi"},
                "pscInterfaceConfig": {"networkAttachment": config.attachment},
            },
        },
    }
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


def create_console(config: ConsoleConfig | None = None, *, cloud=None) -> FastAPI:
    config = config or ConsoleConfig.from_env()
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

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if f"http://{request.headers.get('host', '')}" not in origins:
            return JSONResponse({"detail": "Loopback host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
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
    def configuration():
        return {"project": config.project, "region": config.region,
                "source_root": str(config.source_root), "runtime_account": config.account,
                "network_attachment": config.attachment,
                "deployment_enabled": config.allow_deploy and config.security_review_approved}

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
    def prepare(data: DeploymentInput):
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
                "message": "Snapshot ready for approval", "bundle": bundle, "input": data}
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
    def deploy(plan_id: str, approval: Approval):
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
            plan["status"] = "submitting"
        try:
            operation = cloud.submit(deployment_body(config, plan["input"], plan["bundle"]))
            with lock:
                plan["operation"] = operation["name"]
                plan["status"] = "deploying"
                plan["message"] = "Google Cloud accepted the deployment"
        except GoogleAuthError:
            with lock:
                plan["status"] = "failed"
                plan["message"] = "Google Application Default Credentials are unavailable or invalid"
        except HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            with lock:
                plan["status"] = "failed" if 400 <= code < 500 and code != 408 else "unknown"
                plan["message"] = (
                    f"Google Cloud rejected the request ({code}). Check API enablement, billing, IAM and runtime settings."
                    if plan["status"] == "failed" else
                    "Submission could not be confirmed. Check Google Cloud before retrying.")
        except (RequestException, KeyError, ValueError):
            with lock:
                plan["status"] = "unknown"
                plan["message"] = "Submission could not be confirmed. Check Google Cloud before retrying."
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
            try:
                result = cloud.status(operation)
            except (RequestException, GoogleAuthError, SourceError):
                raise HTTPException(502, "Cloud status is temporarily unavailable; the operation may still be running") from None
            if result.get("done"):
                with lock:
                    if result.get("error"):
                        plan["status"] = "failed"
                        plan["message"] = "Agent Engine reported a deployment failure. Review the operation in Google Cloud."
                    else:
                        plan["status"] = "succeeded"
                        plan["resource"] = result.get("response", {}).get("name", "")
                        plan["message"] = "Agent Engine deployment completed"
        with lock:
            return public_plan(plan)

    app.mount("/static", StaticFiles(directory=str(WEB_ROOT), check_dir=False), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="Local Agent Engine deployment console")
    parser.add_argument("--check-cloud", action="store_true", help="Run read-only GCP prerequisite checks without starting the server")
    args = parser.parse_args()
    config = ConsoleConfig.from_env()
    if args.check_cloud:
        print(json.dumps(AgentEngineCloud(config).preflight(), indent=2), flush=True)
        return
    uvicorn.run(create_console(config), host="127.0.0.1", port=config.port, access_log=False)


if __name__ == "__main__":
    main()