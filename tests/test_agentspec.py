"""AgentSpec v1: schema, validation rules, CLI and console integration."""

import json
from pathlib import Path

import pytest
import yaml

from managed_agents import agentspec
from managed_agents.agentspec import AgentSpecError, check_files, load_spec
from tests.test_console import approve, console, plan_for  # noqa: F401 (fixture)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_AGENTS = ["data360agent", "examples/source-agent", "examples/adk-agent"]

MINIMAL = {
    "apiVersion": "agentspec/v1", "name": "demo-agent", "version": "1.2.3",
    "owner": {"name": "Platform Team", "team": "AI Platform", "email": "team@example.org"},
    "framework": "langgraph", "data_classification": "internal",
    "models": [{"id": "gemini-2.5-flash"}],
}


def spec_with(**changes):
    document = json.loads(json.dumps(MINIMAL))
    for dotted, value in changes.items():
        target, path = document, dotted.split("__")
        for key in path[:-1]:
            target = target.setdefault(key, {})
        if value is None:
            target.pop(path[-1], None)
        else:
            target[path[-1]] = value
    return yaml.safe_dump(document)


def errors_for(**changes):
    with pytest.raises(AgentSpecError) as caught:
        load_spec(spec_with(**changes))
    return " | ".join(caught.value.errors)


def test_checked_in_schema_matches_the_model():
    assert json.loads((ROOT / "schemas/agentspec-v1.json").read_text()) == agentspec.json_schema(), (
        "Regenerate with: python -m managed_agents.agentspec schema > schemas/agentspec-v1.json")


@pytest.mark.parametrize("folder", REFERENCE_AGENTS)
def test_reference_agents_have_valid_specs(folder):
    assert agentspec.validate_path(ROOT / folder) == []


def test_minimal_spec_gets_safe_defaults():
    spec = load_spec(spec_with())
    assert spec.entrypoint.module == "agent" and spec.entrypoint.object == "root_agent"
    assert spec.runtime.python == "3.12" and spec.runtime.network == "public"
    assert spec.runtime.resources.max_instances == 2
    assert spec.labels()["agentspec-version"] == "1_2_3"


@pytest.mark.parametrize(("changes", "message"), [
    ({"apiVersion": "agentspec/v9"}, "unsupported apiVersion 'agentspec/v9'"),
    ({"owner": None}, "owner: Field required"),
    ({"version": "1.0"}, "version: String should match pattern"),
    ({"name": "Bad Name"}, "name: String should match pattern"),
    ({"framework": "crewai"}, "framework: Input should be"),
    ({"colour": "blue"}, "colour: Extra inputs are not permitted"),
    ({"runtime__env": {"OPENAI_API_KEY": "x"}}, "looks like a secret"),
    ({"runtime__resources": {"min_instances": 3, "max_instances": 1}}, "min_instances must not exceed"),
    ({"data_classification": "confidential"}, "confidential agents must use runtime.network: private"),
    ({"models": [{"id": "gemini-a"}, {"id": "gemini-b"}]}, "exactly one primary model"),
    ({"tools__mcp_servers": [{"name": "data", "url": "http://insecure.example.org/mcp"}]},
     "tools.mcp_servers.0.url"),
    ({"evaluation": {"required_before_deploy": True}}, "needs at least one suite"),
])
def test_invalid_specs_are_rejected_with_the_field(changes, message):
    assert message in errors_for(**changes)


def test_all_problems_are_reported_together():
    problems = errors_for(owner=None, version="x")
    assert "owner" in problems and "version" in problems


def test_yaml_problems_are_reported():
    with pytest.raises(AgentSpecError, match="line 3: not valid YAML"):
        load_spec("apiVersion: agentspec/v1\nname: [unclosed\n")
    with pytest.raises(AgentSpecError, match="must be a mapping"):
        load_spec("- just a list\n")


def test_spec_is_checked_against_the_agent_files():
    spec = load_spec(spec_with(evaluation={"suites": [
        {"name": "golden", "kind": "golden", "dataset": "evals/golden.jsonl", "threshold": 0.8}]}))
    problems = check_files(spec, {"agent/agent.py": b"", "agent/other.txt": b""}, "agent")
    assert any("runtime.requirements" in problem for problem in problems)
    assert any("evals/golden.jsonl" in problem for problem in problems)
    assert not any("entrypoint" in problem for problem in problems)


def test_cli_validates_folders_and_prints_the_schema(tmp_path, capsys):
    assert agentspec.main(["validate", *[str(ROOT / folder) for folder in REFERENCE_AGENTS]]) == 0
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "agentspec.yaml").write_text(spec_with(owner=None))
    assert agentspec.main(["validate", str(broken)]) == 1
    assert "owner: Field required" in capsys.readouterr().err
    assert agentspec.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "AgentSpec v1"


# ---------------------------------------------------------------- console integration

def write_agent(source, spec_text):
    (source / "agent.py").write_text("class A:\n    def query(self, input: str): ...\nroot_agent = A()\n")
    (source / "requirements.txt").write_text("langgraph==1.2.22\n")
    (source / "agentspec.yaml").write_text(spec_text)


def test_console_builds_the_agent_from_its_spec(console):  # noqa: F811
    client, cloud, source = console
    write_agent(source, spec_with(runtime={"python": "3.11", "env": {"MODE": "demo"},
                                             "resources": {"cpu": "2", "memory": "2Gi", "max_instances": 4}}))
    plan = plan_for(client, source)
    assert plan["agentspec"]["name"] == "demo-agent" and plan["agentspec_file"] == "agentspec.yaml"
    assert plan["agentspec"]["owner"]["team"] == "AI Platform"
    assert approve(client, plan).json()["status"] == "deploying"
    body = cloud.submissions[0]
    spec = body["spec"]
    assert spec["agentFramework"] == "langgraph"
    assert spec["sourceCodeSpec"]["pythonSpec"]["version"] == "3.11"
    assert spec["deploymentSpec"]["resourceLimits"] == {"cpu": "2", "memory": "2Gi"}
    assert spec["deploymentSpec"]["maxInstances"] == 4
    assert {"name": "MODE", "value": "demo"} in spec["deploymentSpec"]["env"]
    assert spec["classMethods"][0]["parameters"]["required"] == ["input"]
    assert body["labels"]["agentspec-name"] == "demo-agent"
    assert body["labels"]["data-class"] == "internal"


def test_console_rejects_an_invalid_spec(console):  # noqa: F811
    client, cloud, source = console
    write_agent(source, spec_with(owner=None))
    response = client.post("/api/plans", json={"source_kind": "local", "location": str(source),
                                               "display_name": "Bad spec"})
    assert response.status_code == 400
    assert "agentspec.yaml is not valid AgentSpec: owner: Field required" in response.json()["detail"]
    assert not cloud.submissions


def test_adk_spec_is_packaged_as_adk(console):  # noqa: F811
    client, cloud, source = console
    write_agent(source, spec_with(framework="adk"))
    (source / "requirements.txt").write_text("google-cloud-aiplatform[agent_engines,adk]==1.144.0\n")
    plan = plan_for(client, source)
    assert plan["framework"] == "google-adk"
    approve(client, plan)
    assert cloud.submissions[0]["spec"]["agentFramework"] == "google-adk"


def test_private_network_spec_needs_an_attachment(tmp_path, monkeypatch):
    import re

    from fastapi.testclient import TestClient

    from managed_agents.console import ConsoleConfig, create_console
    from tests.test_console import FakeCloud

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<meta name="console-token" content="__CONSOLE_TOKEN__">')
    monkeypatch.setattr("managed_agents.console.WEB_ROOT", web)
    source = tmp_path / "agent"
    source.mkdir()
    write_agent(source, spec_with(runtime={"network": "private"}))
    config = ConsoleConfig(source_root=tmp_path, project="example-project", network_attachment="none")
    with TestClient(create_console(config, cloud=FakeCloud()), base_url="http://127.0.0.1:8088") as client:
        client.headers["X-Console-Token"] = re.search('content="([^"]+)"', client.get("/").text).group(1)
        response = client.post("/api/plans", json={"source_kind": "local", "location": str(source),
                                                   "display_name": "Private"})
    assert response.status_code == 400
    assert "runtime.network: private" in response.json()["detail"]


def test_rejected_framework_label_falls_back_to_custom(console, monkeypatch):  # noqa: F811
    from requests import HTTPError, Response

    client, cloud, source = console
    write_agent(source, spec_with())
    original = cloud.submit

    def picky(body):
        if body["spec"]["agentFramework"] == "langgraph":
            response = Response()
            response.status_code = 400
            response._content = b'{"error": {"message": "Unsupported agent_framework for source deployment"}}'
            raise HTTPError(response=response)
        return original(body)

    monkeypatch.setattr(cloud, "submit", picky)
    assert approve(client, plan_for(client, source)).json()["status"] == "deploying"
    submitted = cloud.submissions[0]
    assert submitted["spec"]["agentFramework"] == "custom"
    assert submitted["labels"]["agent-framework"] == "langgraph"  # still recorded as a label
