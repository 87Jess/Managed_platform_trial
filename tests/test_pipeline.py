import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from managed_agents.console import AgentEngineCloud, ConsoleConfig
from managed_agents.pipeline import MANAGED_BY, UPDATE_MASK, PipelineError, deploy, main, plan

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "projects/123/locations/us-central1/reasoningEngines/42"


def args(folder="examples/source-agent", **overrides):
    values = {"folder": folder, "source_root": str(ROOT), "project": "example-project",
              "region": "us-central1", "runtime_account": "", "network_attachment": "", "display_name": "",
              "timeout": 60.0, "no_wait": False, "smoke_message": ""}
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeCloud:
    def __init__(self, agents=(), statuses=None, outputs=None):
        self.agents = list(agents)
        self.statuses = list(statuses or [{"done": True, "response": {"name": ENGINE}}])
        self.outputs = outputs or {}
        self.calls = []

    def list_agents(self):
        self.calls.append(("list",))
        return self.agents

    def submit(self, body):
        self.calls.append(("create", body))
        return {"name": f"{ENGINE}/operations/7"}

    def update(self, name, body, mask):
        self.calls.append(("update", name, body, mask))
        return {"name": f"{name}/operations/8"}

    def status(self, name):
        self.calls.append(("status", name))
        return self.statuses.pop(0)

    def invoke_agent(self, name, method, payload, stream):
        self.calls.append(("invoke", name, method, payload, stream))
        return self.outputs[method]


def test_plan_packages_the_agent_without_cloud_calls(capsys):
    summary = plan(args())
    assert summary["agent"] == "echo-agent" and summary["framework"] == "custom"
    assert summary["labels"]["managed-by"] == MANAGED_BY
    assert summary["runtime_account"] == "agent-research@example-project.iam.gserviceaccount.com"
    assert '"agent": "echo-agent"' in capsys.readouterr().out


def test_first_deploy_creates_the_agent_and_waits(capsys):
    cloud = FakeCloud(statuses=[{"done": False}, {"done": True, "response": {"name": ENGINE}}])
    sleeps = []
    assert deploy(args(), cloud=cloud, sleep=sleeps.append) == ENGINE
    kinds = [call[0] for call in cloud.calls]
    assert kinds == ["list", "create", "status", "status"]
    body = cloud.calls[1][1]
    assert body["labels"]["agentspec-name"] == "echo-agent" and body["labels"]["managed-by"] == MANAGED_BY
    assert body["spec"]["serviceAccount"] == "agent-research@example-project.iam.gserviceaccount.com"
    assert sleeps == [15.0]
    assert f"Deployed: {ENGINE}" in capsys.readouterr().out


def test_later_deploys_update_the_same_agent_in_place():
    previous = {"name": ENGINE, "labels": {"agentspec-name": "echo-agent", "managed-by": MANAGED_BY}}
    console_copy = {"name": ENGINE[:-2] + "99", "labels": {"agentspec-name": "echo-agent",
                                                           "managed-by": "agent-platform-console"}}
    cloud = FakeCloud(agents=[previous, console_copy])
    assert deploy(args(), cloud=cloud) == ENGINE
    update = next(call for call in cloud.calls if call[0] == "update")
    assert update[1] == ENGINE and update[3] == UPDATE_MASK
    assert not any(call[0] == "create" for call in cloud.calls)


def test_duplicate_pipeline_agents_stop_the_deploy():
    twin = {"labels": {"agentspec-name": "echo-agent", "managed-by": MANAGED_BY}}
    cloud = FakeCloud(agents=[{**twin, "name": ENGINE}, {**twin, "name": ENGINE[:-2] + "43"}])
    with pytest.raises(PipelineError, match="2 pipeline agents"):
        deploy(args(), cloud=cloud)
    assert [call[0] for call in cloud.calls] == ["list"]


def test_a_failed_operation_fails_the_pipeline():
    cloud = FakeCloud(statuses=[{"done": True, "error": {"code": 3, "message": "Build failed: bad requirements"}}])
    with pytest.raises(PipelineError, match="bad requirements"):
        deploy(args(), cloud=cloud)


def test_smoke_test_queries_a_custom_agent_with_message():
    cloud = FakeCloud(outputs={"query": {"output": {"reply": "hello"}}})
    deploy(args(smoke_message="hello"), cloud=cloud)
    assert ("invoke", ENGINE, "query", {"message": "hello"}, False) in cloud.calls


def test_smoke_test_uses_a_session_for_adk_agents_and_fails_on_agent_errors():
    events = {"events": [{"error_code": "500", "error_message": "model not found"}]}
    cloud = FakeCloud(outputs={"async_create_session": {"output": {"id": "s1"}}, "async_stream_query": events})
    with pytest.raises(PipelineError, match="model not found"):
        deploy(args("examples/adk-agent", smoke_message="What time is it?"), cloud=cloud)
    stream = next(call for call in cloud.calls if call[0] == "invoke" and call[2] == "async_stream_query")
    assert stream[3]["session_id"] == "s1" and stream[4] is True


def test_resource_name_is_exported_to_azure_devops(monkeypatch, capsys):
    monkeypatch.setenv("TF_BUILD", "True")
    deploy(args(), cloud=FakeCloud())
    assert f"##vso[task.setvariable variable=AGENT_RESOURCE;isOutput=true]{ENGINE}" in capsys.readouterr().out


def test_folders_without_a_valid_spec_are_refused(tmp_path, capsys):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent.py").write_text("root_agent = None\n")
    assert main(["plan", "agent", "--source-root", str(tmp_path)]) == 1
    assert "no agentspec.yaml" in capsys.readouterr().err


def test_private_agents_need_a_network_attachment(tmp_path):
    folder = tmp_path / "private-agent"
    folder.mkdir()
    (folder / "agent.py").write_text("class A:\n    def query(self, message: str) -> dict:\n        return {}\n\n\nroot_agent = A()\n")
    (folder / "requirements.txt").write_text("")
    (folder / "agentspec.yaml").write_text(
        "apiVersion: agentspec/v1\nname: private-agent\nversion: 0.1.0\n"
        "owner: {name: Platform, team: AI Platform, email: ai-platform@example.org}\n"
        "framework: custom\ndata_classification: internal\nruntime: {network: private}\n")
    assert main(["plan", "private-agent", "--source-root", str(tmp_path), "--network-attachment", "none"]) == 1
    assert main(["plan", "private-agent", "--source-root", str(tmp_path)]) == 0


def test_cloud_update_patches_with_the_update_mask(tmp_path):
    session = MagicMock()
    session.__enter__.return_value = session
    session.patch.return_value.json.return_value = {"name": f"{ENGINE}/operations/8"}
    cloud = AgentEngineCloud(ConsoleConfig(source_root=tmp_path, project="example-project"))
    cloud.session = lambda: session
    assert cloud.update(ENGINE, {"displayName": "x"}, ["display_name", "labels"])["name"].endswith("/8")
    url, = session.patch.call_args.args
    assert url == f"https://us-central1-aiplatform.googleapis.com/v1/{ENGINE}"
    assert session.patch.call_args.kwargs["params"] == {"updateMask": "display_name,labels"}
    with pytest.raises(Exception, match="Unexpected agent"):
        cloud.update("projects/x/locations/europe-west1/reasoningEngines/1", {}, [])
