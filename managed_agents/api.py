import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from google.auth.exceptions import GoogleAuthError
from pydantic import Field, ValidationError
from requests.exceptions import RequestException

from managed_agents.auth import AuthenticationFailed, EntraVerifier, Settings
from managed_agents.backend import BackendUnavailable, GoogleBackend
from managed_agents.policy import (
    AccessDenied,
    Principal,
    Registry,
    StrictModel,
    authorize_agent,
    authorize_connector,
)

logger = logging.getLogger("managed_agents.audit")


class Invocation(StrictModel):
    message: str = Field(min_length=1, max_length=16000)


def create_app(settings: Settings | None = None, *, verifier=None, backend=None) -> FastAPI:
    settings = settings or Settings.from_env()
    verifier = verifier or EntraVerifier(settings)
    backend = backend or GoogleBackend(settings)
    app = FastAPI(title="Managed Agent Platform", docs_url=None, redoc_url=None, openapi_url=None)

    def registry() -> Registry:
        try:
            return Registry.load(Path(settings.registry_path))
        except (OSError, ValidationError):
            raise HTTPException(503, "Registry unavailable") from None

    def principal(authorization: Annotated[str, Header()] = "",
                  x_app_token: Annotated[str, Header()] = "") -> Principal:
        try:
            return verifier.verify(authorization, x_app_token)
        except AuthenticationFailed:
            raise HTTPException(401, "Invalid application or user credentials",
                                headers={"WWW-Authenticate": "Bearer"}) from None

    @app.exception_handler(AccessDenied)
    async def denied(request: Request, error: AccessDenied):
        logger.info(json.dumps({"event": "access_denied", "method": request.method}))
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    @app.exception_handler(BackendUnavailable)
    async def unavailable(request: Request, error: BackendUnavailable):
        return JSONResponse(status_code=503, content={"detail": "Backend unavailable"})

    @app.exception_handler(RequestException)
    @app.exception_handler(GoogleAuthError)
    async def upstream_failure(request: Request, error: Exception):
        logger.warning(json.dumps({"event": "upstream_failure", "type": type(error).__name__}))
        return JSONResponse(status_code=502, content={"detail": "Upstream request failed"})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready(manifest: Annotated[Registry, Depends(registry)]):
        if not manifest.enabled:
            raise HTTPException(503, "Platform disabled")
        return {"status": "ready"}

    @app.get("/v1/agents")
    def list_agents(user: Annotated[Principal, Depends(principal)],
                    manifest: Annotated[Registry, Depends(registry)]):
        return {"agents": [{"id": agent.id, "data_class": agent.data_class}
                           for agent in manifest.agents if manifest.enabled and agent.enabled
                           and user.roles.intersection(agent.allowed_roles)]}

    @app.post("/v1/agents/{agent_id}/invoke")
    def invoke(agent_id: str, body: Invocation, request: Request,
               user: Annotated[Principal, Depends(principal)],
               manifest: Annotated[Registry, Depends(registry)]):
        agent = authorize_agent(manifest, user, agent_id)
        subject = hashlib.sha256(user.subject.encode()).hexdigest()
        logger.info(json.dumps({"event": "agent_invoke", "agent": agent.id, "subject": subject}))
        return backend.invoke(agent, subject, body.message,
                              request.headers["authorization"], request.headers["x-app-token"])

    @app.post("/v1/agents/{agent_id}/models/{alias}/generate")
    def generate(agent_id: str, alias: str, body: Invocation,
                 user: Annotated[Principal, Depends(principal)],
                 manifest: Annotated[Registry, Depends(registry)]):
        agent = authorize_agent(manifest, user, agent_id)
        model = next((item for item in manifest.models if item.id == alias), None)
        if (alias not in agent.model_aliases or model is None or not model.enabled
                or agent.data_class not in model.data_classes):
            raise AccessDenied("Model access denied")
        return backend.generate(model, body.message)

    @app.get("/v1/agents/{agent_id}/connectors/{connector_id}/object")
    def read_object(agent_id: str, connector_id: str, name: str,
                    user: Annotated[Principal, Depends(principal)],
                    manifest: Annotated[Registry, Depends(registry)]):
        agent = authorize_agent(manifest, user, agent_id)
        connector = authorize_connector(manifest, agent, connector_id)
        if not user.roles.intersection(connector.allowed_roles):
            raise AccessDenied("Connector user grant required")
        if (not name.startswith(connector.prefix) or "\\" in name
                or any(segment in {".", ".."} for segment in name.split("/"))):
            raise AccessDenied("Object outside approved prefix")
        return Response(backend.read_object(connector, name), media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app