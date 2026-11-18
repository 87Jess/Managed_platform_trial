from unittest.mock import MagicMock

import pytest
from requests.exceptions import HTTPError

from managed_agents.auth import Settings
from managed_agents.backend import BackendUnavailable, GoogleBackend
from managed_agents.policy import Agent, Connector


@pytest.fixture
def backend():
    return GoogleBackend(Settings(
        project="example-project", tenant_id="00000000-0000-0000-0000-000000000001",
        audience="api", app_token_sha256="a" * 64))


def test_unconfigured_engine_cannot_call_google(backend, monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(backend, "session", session)
    with pytest.raises(BackendUnavailable):
        backend.invoke(Agent(id="research"), "user", "hello", "Bearer user", "app")
    session.assert_not_called()


def test_agent_rest_contract(backend, monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.json.return_value = {"output": {"text": "answer"}}
    monkeypatch.setattr(backend, "session", lambda: session)
    agent = Agent(id="research", engine_resource=(
        "projects/example-project/locations/us-central1/reasoningEngines/123"))
    assert backend.invoke(agent, "user", "hello", "Bearer user", "app") == {"text": "answer"}
    args, kwargs = session.post.call_args
    assert args[0].endswith("reasoningEngines/123:query")
    assert kwargs["json"]["class_method"] == "query"
    assert kwargs["json"]["input"]["authorization"] == "Bearer user"
    assert kwargs["timeout"] == 120


def test_storage_response_is_bounded(backend, monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    response = session.get.return_value.__enter__.return_value
    response.iter_content.return_value = [b"x" * (1024 * 1024), b"extra"]
    monkeypatch.setattr(backend, "session", lambda: session)
    with pytest.raises(BackendUnavailable):
        backend.read_object(Connector(id="docs", kind="storage", bucket="docs", prefix="ok/"),
                            "ok/file")


def test_upstream_errors_are_not_accepted_as_success(backend, monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.raise_for_status.side_effect = HTTPError("upstream failure")
    monkeypatch.setattr(backend, "session", lambda: session)
    agent = Agent(id="research", engine_resource=(
        "projects/example-project/locations/us-central1/reasoningEngines/123"))
    with pytest.raises(HTTPError):
        backend.invoke(agent, "user", "hello", "Bearer user", "app")