import json

import pytest
from fastapi.testclient import TestClient

from managed_agents.api import create_app
from managed_agents.auth import AuthenticationFailed, Settings
from managed_agents.policy import Principal


class FakeVerifier:
    def verify(self, authorization, app_token):
        if authorization != "Bearer valid-user" or app_token != "valid-app":
            raise AuthenticationFailed()
        return Principal(subject="user-one", roles={"research.read", "docs.read"})


class FakeBackend:
    def __init__(self):
        self.calls = []

    def invoke(self, agent, subject, message, authorization, app_token):
        self.calls.append(subject)
        return {"text": "Agent response"}

    def generate(self, model, message):
        self.calls.append(model.id)
        return {"text": "Model response"}

    def read_object(self, connector, name):
        self.calls.append(name)
        return b"approved document"


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "enabled": True,
        "agents": [{"id": "research", "enabled": True, "allowed_roles": ["research.read"],
                    "connectors": ["docs"], "model_aliases": ["fast"]}],
        "connectors": [{"id": "docs", "enabled": True, "kind": "storage", "bucket": "docs",
                        "prefix": "approved/", "allowed_roles": ["docs.read"]}],
        "models": [{"id": "fast", "enabled": True, "model": "gemini-2.5-flash",
                    "data_classes": ["internal"]}],
    }), encoding="utf-8")
    settings = Settings(project="example-project", tenant_id="00000000-0000-0000-0000-000000000001",
                        audience="test-api", app_token_sha256="a" * 64, registry_path=str(path))
    backend = FakeBackend()
    with TestClient(create_app(settings, verifier=FakeVerifier(), backend=backend)) as test_client:
        test_client.headers.update({"Authorization": "Bearer valid-user", "X-App-Token": "valid-app"})
        yield test_client, backend, path


def test_invoke_and_subject_is_pseudonymized(client):
    http, backend, _ = client
    response = http.post("/v1/agents/research/invoke", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json()["text"] == "Agent response"
    assert len(backend.calls[0]) == 64
    assert backend.calls[0] != "user-one"


@pytest.mark.parametrize("header", ["Authorization", "X-App-Token"])
def test_both_credentials_required(client, header):
    http, backend, _ = client
    del http.headers[header]
    assert http.post("/v1/agents/research/invoke", json={"message": "Hi"}).status_code == 401
    assert not backend.calls


def test_unknown_agent_denied(client):
    http, backend, _ = client
    assert http.post("/v1/agents/unknown/invoke", json={"message": "Hi"}).status_code == 403
    assert not backend.calls


def test_registry_failure_denies_instead_of_using_stale_grants(client):
    http, backend, path = client
    path.write_text("invalid json", encoding="utf-8")
    assert http.post("/v1/agents/research/invoke", json={"message": "Hi"}).status_code == 503
    assert not backend.calls


def test_kill_switch_applies_on_next_request(client):
    http, backend, path = client
    manifest = json.loads(path.read_text())
    manifest["enabled"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert http.post("/v1/agents/research/invoke", json={"message": "Hi"}).status_code == 403
    assert not backend.calls


def test_model_alias_grant(client):
    http, backend, _ = client
    assert http.post("/v1/agents/research/models/fast/generate",
                     json={"message": "Hi"}).status_code == 200
    assert backend.calls == ["fast"]
    assert http.post("/v1/agents/research/models/unknown/generate",
                     json={"message": "Hi"}).status_code == 403


@pytest.mark.parametrize("name", ["private/file.txt", "approved/../secret", "approved/./secret",
                                  "approved/dir\\secret"])
def test_connector_prefix_and_traversal_denied(client, name):
    http, backend, _ = client
    response = http.get("/v1/agents/research/connectors/docs/object", params={"name": name})
    assert response.status_code == 403
    assert not backend.calls


def test_connector_is_read_only(client):
    http, backend, _ = client
    route = "/v1/agents/research/connectors/docs/object"
    assert http.get(route, params={"name": "approved/file.txt"}).content == b"approved document"
    assert http.post(route, json={"name": "approved/file.txt"}).status_code == 405


def test_connector_requires_user_grant(client):
    http, backend, path = client
    manifest = json.loads(path.read_text())
    manifest["connectors"][0]["allowed_roles"] = ["restricted"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert http.get("/v1/agents/research/connectors/docs/object",
                    params={"name": "approved/file.txt"}).status_code == 403
    assert not backend.calls


def test_untrusted_user_id_and_oversized_input_rejected(client):
    http, backend, _ = client
    for body in [{"message": "Hi", "user_id": "other"}, {"message": "x" * 16001}]:
        assert http.post("/v1/agents/research/invoke", json=body).status_code == 422
    assert not backend.calls


def test_health_does_not_report_disabled_platform_as_ready(client):
    http, _, path = client
    assert http.get("/healthz").json() == {"status": "ok"}
    manifest = json.loads(path.read_text())
    manifest["enabled"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert http.get("/readyz").status_code == 503