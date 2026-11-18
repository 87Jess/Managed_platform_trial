import json

import httpx
import pytest

from managed_agents.deploy_agent import main as deploy_main
from managed_agents.runtime import GovernedAgent, PlatformClient


@pytest.mark.parametrize("url", ["http://gateway.test", "https://user:pass@gateway.test",
                                 "https://gateway.test/path", "https://gateway.test?redirect=evil"])
def test_sdk_rejects_untrusted_gateway_origins(url):
    with pytest.raises(ValueError):
        PlatformClient(url, "research", "Bearer user", "app")


def test_agent_calls_only_configured_gateway(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"text": "governed answer"})

    original_client = httpx.Client
    monkeypatch.setattr("managed_agents.runtime.httpx.Client", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(respond), **kwargs))
    agent = GovernedAgent("https://gateway.test", "research", "fast")
    assert agent.query("user-one", "Hello", "Bearer delegated-user", "app") == {
        "text": "governed answer"}
    assert str(calls[0].url) == "https://gateway.test/v1/agents/research/models/fast/generate"
    assert calls[0].headers["authorization"] == "Bearer delegated-user"
    assert calls[0].headers["x-app-token"] == "app"


def test_gateway_denial_propagates(monkeypatch):
    original_client = httpx.Client
    monkeypatch.setattr("managed_agents.runtime.httpx.Client", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(
                            lambda request: httpx.Response(403)), **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        GovernedAgent("https://gateway.test", "research", "fast").query(
            "user", "Hello", "Bearer revoked", "app")


def test_deployment_defaults_to_no_cloud_mutation(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "deploy_agent", "--gateway-url", "https://gateway.test", "--staging-bucket", "gs://staging",
        "--network-attachment", "projects/example/regions/us-central1/networkAttachments/agents",
    ])
    deploy_main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["apply"] is False
    assert plan["direct_model_or_data_permissions"] is False
    assert plan["project"] == "itsai-mai-gcp-lab-proj"


def test_agent_apply_requires_explicit_approval(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "deploy_agent", "--gateway-url", "https://gateway.test", "--staging-bucket", "gs://staging",
        "--network-attachment", "attachment", "--apply",
    ])
    with pytest.raises(SystemExit) as error:
        deploy_main()
    assert error.value.code == 2