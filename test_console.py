import base64
import io
import re
import tarfile
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import DefaultCredentialsError
from requests import Response
from requests.exceptions import HTTPError, Timeout

from managed_agents.console import AgentEngineCloud, ConsoleConfig, create_console
from tests.test_sources import make_zip, uv_source


class FakeCloud:
    def __init__(self):
        self.submissions = []

    def submit(self, body):
        self.submissions.append(body)
        return {"name": "projects/example-project/locations/us-central1/operations/123"}

    def status(self, name):
        return {"done": True, "response": {
            "name": "projects/example-project/locations/us-central1/reasoningEngines/456"}}


@pytest.fixture
def console(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<meta name="console-token" content="__CONSOLE_TOKEN__">')
    monkeypatch.setattr("managed_agents.console.WEB_ROOT", web)
    source = tmp_path / "agent-source"
    source.mkdir()
    (source / "agent.py").write_text("raise RuntimeError('never execute locally')")
    (source / "requirements.txt").write_text("")
    cloud = FakeCloud()
    config = ConsoleConfig(source_root=tmp_path, project="example-project",
                           allow_deploy=True, security_review_approved=True)
    with TestClient(create_console(config, cloud=cloud), base_url="http://127.0.0.1:8088") as client:
        token = re.search('content="([^"]+)"', client.get("/").text).group(1)
        client.headers.update({"X-Console-Token": token})
        yield client, cloud, source


def plan_for(client, source):
    response = client.post("/api/plans", json={"source_kind": "local", "location": str(source),
                                              "display_name": "Test agent"})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, plan, **overrides):
    body = {"digest": plan["digest"], "project": plan["project"],
            "approve_cost": True, "approve_code": True}
    body.update(overrides)
    return client.post(f"/api/plans/{plan['id']}/deploy", json=body)


def test_review_does_not_deploy_or_run_source(console):
    client, cloud, source = console
    plan = plan_for(client, source)
    assert plan["status"] == "planned"
    assert len(plan["files"]) == 2
    assert "bundle" not in plan
    assert not cloud.submissions


def test_deploy_uses_reviewed_snapshot_and_polls_real_operation(console):
    client, cloud, source = console
    plan = plan_for(client, source)
    (source / "agent.py").write_text("changed after review")
    assert approve(client, plan).json()["status"] == "deploying"
    body = cloud.submissions[0]
    spec = body["spec"]
    assert spec["serviceAccount"] == "agent-research@example-project.iam.gserviceaccount.com"
    assert spec["sourceCodeSpec"]["pythonSpec"]["entrypointModule"] == "agent"
    payload = base64.b64decode(spec["sourceCodeSpec"]["inlineSource"]["sourceArchive"])
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert b"never execute locally" in archive.extractfile("agent.py").read()
    assert client.get(f"/api/plans/{plan['id']}").json()["status"] == "succeeded"
    assert approve(client, plan).status_code == 409


@pytest.mark.parametrize("override", [{"digest": "wrong"}, {"project": "other-project"},
                                      {"approve_cost": False}, {"approve_code": False}])
def test_snapshot_and_explicit_approval_required(console, override):
    client, cloud, source = console
    assert approve(client, plan_for(client, source), **override).status_code == 400
    assert not cloud.submissions


def test_csrf_and_dns_rebinding_rejected(console):
    client, cloud, source = console
    assert client.get("/api/config", headers={"Origin": "https://attacker.test"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.test:8088"}).status_code == 403
    del client.headers["X-Console-Token"]
    assert client.post("/api/plans", json={}).status_code == 403
    assert not cloud.submissions


@pytest.mark.parametrize("prefix", ["", "./", "project\\"])
def test_zip_upload_and_plan(console, prefix):
    client, cloud, _ = console
    upload = client.post("/api/uploads", content=make_zip({prefix + "agent.py": "root_agent = object()",
                                                          prefix + "requirements.txt": ""}))
    assert upload.status_code == 200
    response = client.post("/api/plans", json={"source_kind": "zip",
        "location": upload.json()["id"], "display_name": "Uploaded agent"})
    assert response.status_code == 200
    assert response.json()["source"] == "ZIP upload"
    assert not cloud.submissions


def test_uv_zip_plan_and_cloud_request_use_generated_requirements(console, monkeypatch):
    client, cloud, _ = console
    monkeypatch.setattr("managed_agents.sources.export_uv_requirements",
                        lambda files: b"httpx==0.28.1\n")
    upload = client.post("/api/uploads", content=make_zip(uv_source()))
    response = client.post("/api/plans", json={"source_kind": "zip",
        "location": upload.json()["id"], "display_name": "uv agent"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["dependency_format"] == "uv"
    assert plan["requirements_file"] == ".agent-platform-requirements.txt"
    assert any(item["path"] == "uv.lock" for item in plan["files"])
    assert not cloud.submissions
    assert approve(client, plan).json()["status"] == "deploying"
    spec = cloud.submissions[0]["spec"]["sourceCodeSpec"]
    assert spec["pythonSpec"]["requirementsFile"] == plan["requirements_file"]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(spec["inlineSource"]["sourceArchive"])),
                      mode="r:gz") as archive:
        assert archive.extractfile(plan["requirements_file"]).read() == b"httpx==0.28.1\n"


def test_identical_zip_retries_reuse_upload_slot(console):
    client, cloud, _ = console
    archive = make_zip({"agent.py": "root_agent = object()", "requirements.txt": ""})
    ids = set()
    for _ in range(5):
        upload = client.post("/api/uploads", content=archive)
        assert upload.status_code == 200
        ids.add(upload.json()["id"])
    assert len(ids) == 1
    different = client.post("/api/uploads", content=make_zip({"other.py": "different"}))
    assert different.status_code == 200
    assert not cloud.submissions


def test_adk_plan_uses_managed_session_and_streaming_contract(console):
    client, cloud, source = console
    response = client.post("/api/plans", json={"source_kind": "local", "location": str(source),
        "display_name": "Weather agent", "framework": "google-adk"})
    assert response.status_code == 200
    plan = response.json()
    assert plan["framework"] == "google-adk"
    assert any(item["path"] == "_agent_platform_adk.py" for item in plan["files"])
    assert not cloud.submissions
    assert approve(client, plan).json()["status"] == "deploying"
    spec = cloud.submissions[0]["spec"]
    assert spec["agentFramework"] == "google-adk"
    assert spec["sourceCodeSpec"]["pythonSpec"]["entrypointModule"] == "_agent_platform_adk"
    assert [item["name"] for item in spec["classMethods"]] == ["async_create_session", "async_stream_query"]
    assert spec["classMethods"][1]["api_mode"] == "async_stream"
    assert {"name": "GOOGLE_GENAI_USE_VERTEXAI", "value": "true"} in spec["deploymentSpec"]["env"]


def test_preflight_separates_missing_resources_from_unverified_permissions(tmp_path, monkeypatch):
    cloud = AgentEngineCloud(ConsoleConfig(source_root=tmp_path, project="example-project"))
    session = MagicMock()
    session.__enter__.return_value = session
    results = [(200, {"billingEnabled": True}), (200, {"state": "ENABLED"}),
               (404, {}), (403, {})]
    session.get.side_effect = [MagicMock(status_code=status, json=lambda body=body: body)
                               for status, body in results]
    monkeypatch.setattr(cloud, "session", lambda: session)
    report = cloud.preflight()
    statuses = {item["id"]: item["status"] for item in report["checks"]}
    assert statuses == {"submission": "blocked", "billing": "passed", "vertex_api": "passed",
                        "runtime_account": "missing", "psc_attachment": "unknown"}
    assert report["quota_project"] == "example-project"
    session.post.assert_not_called()


def test_preflight_missing_credentials_is_actionable(tmp_path, monkeypatch):
    cloud = AgentEngineCloud(ConsoleConfig(source_root=tmp_path))
    def unavailable():
        raise DefaultCredentialsError("must not leak private paths")
    monkeypatch.setattr(cloud, "session", unavailable)
    report = cloud.preflight()
    assert report["checks"][-1]["id"] == "credentials"
    assert "private paths" not in str(report)


def test_preflight_endpoint_requires_console_session(console, monkeypatch):
    client, cloud, _ = console
    monkeypatch.setattr(cloud, "preflight", lambda: {"checks": []}, raising=False)
    assert client.get("/api/preflight").json() == {"checks": []}
    del client.headers["X-Console-Token"]
    assert client.get("/api/preflight").status_code == 403


def test_read_only_cloud_check_command_does_not_start_or_deploy(monkeypatch, capsys):
    from managed_agents import console as console_module

    monkeypatch.setattr("sys.argv", ["console", "--check-cloud"])
    monkeypatch.setattr(AgentEngineCloud, "preflight", lambda self: {"checks": [], "project": self.config.project})
    server = MagicMock()
    monkeypatch.setattr(console_module.uvicorn, "run", server)
    console_module.main()
    assert '"checks": []' in capsys.readouterr().out
    server.assert_not_called()


def test_failed_review_can_retry_same_upload(console):
    client, cloud, _ = console
    archive = make_zip({"agent.py": "root_agent = object()", "requirements.txt": ""})
    upload_id = client.post("/api/uploads", content=archive).json()["id"]
    body = {"source_kind": "zip", "location": upload_id, "display_name": "Retry agent",
            "module": "missing"}
    for _ in range(3):
        assert client.post("/api/plans", json=body).status_code == 400
    body["module"] = "agent"
    assert client.post("/api/plans", json=body).status_code == 200
    assert not cloud.submissions


def test_clear_staged_uploads_recovers_capacity_and_preserves_plans(console):
    client, cloud, source = console
    plan = plan_for(client, source)
    for content in ["one", "two"]:
        assert client.post("/api/uploads", content=make_zip({"agent.py": content})).status_code == 200
    archive = make_zip({"agent.py": "three"})
    assert client.post("/api/uploads", content=archive).status_code == 429
    response = client.delete("/api/uploads")
    assert response.status_code == 200
    assert response.json() == {"discarded": 2}
    assert client.get(f"/api/plans/{plan['id']}").status_code == 200
    assert client.post("/api/uploads", content=archive).status_code == 200
    assert not cloud.submissions


def test_clear_uploads_requires_console_credentials(console):
    client, _, _ = console
    del client.headers["X-Console-Token"]
    assert client.delete("/api/uploads").status_code == 403


def test_private_or_arbitrary_repository_url_rejected(console):
    client, cloud, _ = console
    for location in ["http://127.0.0.1/secret", "https://token@github.com/org/repo"]:
        response = client.post("/api/plans", json={"source_kind": "github", "location": location,
                                                  "revision": "a" * 40, "display_name": "Agent"})
        assert response.status_code == 400
    assert not cloud.submissions


def test_cloud_deployment_locked_by_default(console):
    client, cloud, source = console
    config = ConsoleConfig(source_root=source.parent, project="example-project")
    with TestClient(create_console(config, cloud=cloud), base_url="http://127.0.0.1:8088") as locked:
        token = re.search('content="([^"]+)"', locked.get("/").text).group(1)
        locked.headers.update({"X-Console-Token": token})
        plan = plan_for(locked, source)
        assert approve(locked, plan).status_code == 403
    assert not cloud.submissions


def test_real_frontend_and_assets_are_served(tmp_path, monkeypatch):
    from managed_agents import console as console_module

    web = console_module.Path(__file__).resolve().parents[1] / "managed_agents" / "web"
    monkeypatch.setattr(console_module, "WEB_ROOT", web)
    with TestClient(create_console(ConsoleConfig(source_root=tmp_path)),
                    base_url="http://127.0.0.1:8088") as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "__CONSOLE_TOKEN__" not in page.text
        assert "Deployment review" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        for asset in ["app.js", "styles.css", "lucide.js", "cloud.svg", "plex.woff2"]:
            assert client.get(f"/static/{asset}").status_code == 200


@pytest.mark.parametrize("error,status", [
    (DefaultCredentialsError("private credential detail"), "failed"),
    (Timeout("private request detail"), "unknown"),
])
def test_submission_errors_are_safe_and_not_resubmitted(console, monkeypatch, error, status):
    client, cloud, source = console
    def reject(body):
        raise error
    monkeypatch.setattr(cloud, "submit", reject)
    plan = plan_for(client, source)
    result = approve(client, plan).json()
    assert result["status"] == status
    assert "private" not in result["message"]
    assert approve(client, plan).status_code == 409


def test_permission_rejection_reports_actionable_status(console, monkeypatch):
    client, cloud, source = console
    def reject(body):
        response = Response()
        response.status_code = 403
        raise HTTPError("credential details must not leak", response=response)
    monkeypatch.setattr(cloud, "submit", reject)
    result = approve(client, plan_for(client, source)).json()
    assert result["status"] == "failed"
    assert "403" in result["message"]
    assert "credential details" not in result["message"]


def test_google_operation_failure_is_not_reported_as_success(console, monkeypatch):
    client, cloud, source = console
    monkeypatch.setattr(cloud, "status", lambda name: {"done": True, "error": {"code": 3}})
    plan = plan_for(client, source)
    approve(client, plan)
    assert client.get(f"/api/plans/{plan['id']}").json()["status"] == "failed"