import base64
import io
import json
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
        self.sandbox_tests = []
        self.sandbox_status = "passed"

    def submit(self, body):
        self.submissions.append(body)
        return {"name": "projects/example-project/locations/us-central1/operations/123"}

    def status(self, name):
        return {"done": True, "response": {
            "name": "projects/example-project/locations/us-central1/reasoningEngines/456"}}

    def sandbox_test(self, bundle, module, object_name, framework):
        self.sandbox_tests.append((bundle.digest, module, object_name, framework))
        return {"status": self.sandbox_status, "message": "fake sandbox result"}


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
    client, cloud, _ = console
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
    _, cloud, source = console
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

SANDBOX_ENGINE = "projects/example-project/locations/us-central1/reasoningEngines/77"


def sandbox_console(tmp_path, monkeypatch, **overrides):
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text('<meta name="console-token" content="__CONSOLE_TOKEN__">')
    monkeypatch.setattr("managed_agents.console.WEB_ROOT", web)
    source = tmp_path / "agent-source"
    source.mkdir(exist_ok=True)
    (source / "agent.py").write_text("raise RuntimeError('never execute locally')")
    (source / "requirements.txt").write_text("")
    cloud = FakeCloud()
    settings = {"source_root": tmp_path, "project": "example-project", "allow_deploy": True,
                "security_review_approved": True, "sandbox_engine": SANDBOX_ENGINE,
                "allow_sandbox_test": True, **overrides}
    client = TestClient(create_console(ConsoleConfig(**settings), cloud=cloud),
                        base_url="http://127.0.0.1:8088")
    token = re.search('content="([^"]+)"', client.get("/").text).group(1)
    client.headers.update({"X-Console-Token": token})
    return client, cloud, source


def wait_for_sandbox(client, plan):
    import time
    for _ in range(100):
        current = client.get(f"/api/plans/{plan['id']}").json()
        if current["sandbox"]["status"] != "running":
            return current
        time.sleep(0.02)
    raise AssertionError("sandbox test did not finish")


def test_sandbox_test_runs_reviewed_snapshot_without_deploying(tmp_path, monkeypatch):
    client, cloud, source = sandbox_console(tmp_path, monkeypatch)
    plan = plan_for(client, source)
    assert plan["sandbox"]["status"] == "not_run"
    started = client.post(f"/api/plans/{plan['id']}/sandbox-test")
    assert started.status_code == 200
    finished = wait_for_sandbox(client, plan)
    assert finished["sandbox"]["status"] == "passed"
    assert finished["status"] == "planned"
    assert cloud.sandbox_tests == [(plan["digest"], "agent", "root_agent", "custom")]
    assert not cloud.submissions
    assert client.get("/api/config").json()["sandbox_enabled"] is True


@pytest.mark.parametrize("overrides", [{"allow_sandbox_test": False}, {"sandbox_engine": ""}])
def test_sandbox_test_locked_unless_enabled_and_configured(tmp_path, monkeypatch, overrides):
    client, cloud, source = sandbox_console(tmp_path, monkeypatch, **overrides)
    plan = plan_for(client, source)
    assert client.post(f"/api/plans/{plan['id']}/sandbox-test").status_code == 403
    assert not cloud.sandbox_tests


def test_required_sandbox_test_blocks_deploy_until_passed(tmp_path, monkeypatch):
    client, cloud, source = sandbox_console(tmp_path, monkeypatch, require_sandbox_test=True)
    plan = plan_for(client, source)
    assert approve(client, plan).status_code == 409
    cloud.sandbox_status = "failed"
    client.post(f"/api/plans/{plan['id']}/sandbox-test")
    wait_for_sandbox(client, plan)
    assert approve(client, plan).status_code == 409
    cloud.sandbox_status = "passed"
    client.post(f"/api/plans/{plan['id']}/sandbox-test")
    wait_for_sandbox(client, plan)
    assert approve(client, plan).json()["status"] == "deploying"


def test_sandbox_errors_do_not_leak_details(tmp_path, monkeypatch):
    client, cloud, source = sandbox_console(tmp_path, monkeypatch)
    def reject(*args):
        raise DefaultCredentialsError("private credential path")
    monkeypatch.setattr(cloud, "sandbox_test", reject)
    plan = plan_for(client, source)
    client.post(f"/api/plans/{plan['id']}/sandbox-test")
    result = wait_for_sandbox(client, plan)["sandbox"]
    assert result["status"] == "error"
    assert "private" not in str(result)


IAP_AUDIENCE = "/projects/123456/locations/us-central1/services/agent-platform-console"
HOSTED_URL = "https://agent-platform-console-abc-uc.a.run.app"


def hosted_console(tmp_path, monkeypatch, **overrides):
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text('<meta name="console-token" content="__CONSOLE_TOKEN__">')
    monkeypatch.setattr("managed_agents.console.WEB_ROOT", web)
    cloud = FakeCloud()

    def verifier(assertion, audience):
        assert audience == IAP_AUDIENCE
        if not assertion.startswith("valid:"):
            raise ValueError("bad signature")
        return {"email": assertion.removeprefix("valid:"), "iss": "https://cloud.google.com/iap"}

    settings = {"source_root": tmp_path, "project": "example-project", "allow_deploy": True,
                "security_review_approved": True, "hosted": True, "iap_audience": IAP_AUDIENCE,
                **overrides}
    client = TestClient(create_console(ConsoleConfig(**settings), cloud=cloud, iap_verifier=verifier),
                        base_url=HOSTED_URL)
    return client, cloud


def signed_in(client, email="operator@example.com"):
    client.headers.update({"X-Goog-IAP-JWT-Assertion": f"valid:{email}"})
    token = re.search('content="([^"]+)"', client.get("/").text).group(1)
    client.headers.update({"X-Console-Token": token})
    return client


def test_hosted_console_requires_valid_iap_assertion(tmp_path, monkeypatch):
    client, _ = hosted_console(tmp_path, monkeypatch)
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401
    client.headers.update({"X-Goog-IAP-JWT-Assertion": "forged"})
    assert client.get("/").status_code == 401
    config = signed_in(client).get("/api/config").json()
    assert config["hosted"] is True
    assert config["user"] == "operator@example.com"
    assert config["local_sources_enabled"] is False
    assert config["source_root"] == ""


def test_hosted_console_enforces_user_allowlist(tmp_path, monkeypatch):
    client, _ = hosted_console(tmp_path, monkeypatch,
                               allowed_users=frozenset({"operator@example.com"}))
    client.headers.update({"X-Goog-IAP-JWT-Assertion": "valid:someone@example.com"})
    assert client.get("/").status_code == 401
    assert signed_in(client).get("/api/config").status_code == 200


def test_hosted_console_rejects_local_folders_and_cross_origin(tmp_path, monkeypatch):
    client, cloud = hosted_console(tmp_path, monkeypatch)
    signed_in(client)
    response = client.post("/api/plans", json={"source_kind": "local", "location": str(tmp_path),
                                               "display_name": "Agent"})
    assert response.status_code == 400
    assert client.get("/api/config", headers={"Origin": "https://attacker.test"}).status_code == 403
    assert client.get("/api/config", headers={"Origin": HOSTED_URL}).status_code == 200
    assert not cloud.submissions


def test_hosted_console_records_who_reviewed_and_approved(tmp_path, monkeypatch):
    client, cloud = hosted_console(tmp_path, monkeypatch)
    signed_in(client, "reviewer@example.com")
    archive = make_zip({"agent.py": "root_agent = object()", "requirements.txt": ""})
    upload_id = client.post("/api/uploads", content=archive).json()["id"]
    plan = client.post("/api/plans", json={"source_kind": "zip", "location": upload_id,
                                           "display_name": "Hosted agent"}).json()
    assert plan["reviewed_by"] == "reviewer@example.com"
    signed_in(client, "approver@example.com")
    result = approve(client, plan).json()
    assert result["status"] == "deploying"
    assert result["approved_by"] == "approver@example.com"
    assert len(cloud.submissions) == 1


def test_hosted_console_needs_iap_audience(tmp_path):
    with pytest.raises(ValueError, match="CONSOLE_IAP_AUDIENCE"):
        create_console(ConsoleConfig(source_root=tmp_path, hosted=True), cloud=FakeCloud())


def test_iap_verifier_checks_issuer_and_audience(monkeypatch):
    from google.oauth2 import id_token

    from managed_agents.console import IAP_CERTS_URL, verify_iap_assertion

    calls = []
    def fake_verify(token, request, audience, certs_url):
        calls.append((audience, certs_url))
        return {"iss": "https://accounts.google.com", "email": "a@example.com"}
    monkeypatch.setattr(id_token, "verify_token", fake_verify)
    with pytest.raises(ValueError):
        verify_iap_assertion("token", IAP_AUDIENCE)
    assert calls == [(IAP_AUDIENCE, IAP_CERTS_URL)]
    monkeypatch.setattr(id_token, "verify_token", lambda *a, **k: {
        "iss": "https://cloud.google.com/iap", "email": "a@example.com"})
    assert verify_iap_assertion("token", IAP_AUDIENCE)["email"] == "a@example.com"


def test_poc_mode_omits_runtime_account_and_private_networking(tmp_path):
    from managed_agents.console import DeploymentInput, deployment_body
    from managed_agents.sources import bundle_source

    config = ConsoleConfig(source_root=tmp_path, project="example-project",
                           runtime_account="default", network_attachment="none")
    bundle = bundle_source({"agent.py": b"root_agent = object()", "requirements.txt": b""},
                           "agent", "requirements.txt")
    body = deployment_body(config, DeploymentInput(source_kind="zip", location="x",
                                                   display_name="Agent"), bundle)
    assert "serviceAccount" not in body["spec"]
    assert "pscInterfaceConfig" not in body["spec"]["deploymentSpec"]
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {"billingEnabled": True, "state": "ENABLED"}
    cloud = AgentEngineCloud(config)
    cloud.session = lambda: session
    statuses = {item["id"]: item["status"] for item in cloud.preflight()["checks"]}
    assert statuses["runtime_account"] == "skipped"
    assert statuses["psc_attachment"] == "skipped"
    assert session.get.call_count == 2


def google_http_error(code, status, message):
    response = Response()
    response.status_code = code
    response._content = json.dumps({"error": {"code": code, "status": status,
                                              "message": message}}).encode()
    return HTTPError("detail", response=response)


def test_status_check_failure_keeps_deploying_and_explains(console, monkeypatch):
    client, cloud, source = console
    plan = plan_for(client, source)
    approve(client, plan)
    def denied(name):
        raise google_http_error(403, "PERMISSION_DENIED", "Permission 'aiplatform.operations.get' denied")
    monkeypatch.setattr(cloud, "status", denied)
    response = client.get(f"/api/plans/{plan['id']}")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "deploying"
    assert "HTTP 403" in result["status_check"]
    assert "PERMISSION_DENIED" in result["status_check"]
    monkeypatch.setattr(cloud, "status", FakeCloud().status)
    result = client.get(f"/api/plans/{plan['id']}").json()
    assert result["status"] == "succeeded"
    assert result["status_check"] == ""
    assert result["completed_at"] >= result["submitted_at"] > 0


def test_operation_failure_reports_google_message(console, monkeypatch):
    client, cloud, source = console
    monkeypatch.setattr(cloud, "status", lambda name: {
        "done": True, "error": {"code": 3, "message": "Build failed: No module named 'google.adk'"}})
    plan = plan_for(client, source)
    approve(client, plan)
    result = client.get(f"/api/plans/{plan['id']}").json()
    assert result["status"] == "failed"
    assert "No module named 'google.adk'" in result["message"]


def test_rejected_submission_includes_google_reason(console, monkeypatch):
    client, cloud, source = console
    def reject(body):
        raise google_http_error(400, "INVALID_ARGUMENT", "sourceCodeSpec is invalid")
    monkeypatch.setattr(cloud, "submit", reject)
    result = approve(client, plan_for(client, source)).json()
    assert result["status"] == "failed"
    assert "INVALID_ARGUMENT: sourceCodeSpec is invalid" in result["message"]


def test_plan_warns_about_framework_and_missing_adk_dependency(console):
    client, _, _ = console
    archive = make_zip({"agent.py": "from google.adk.agents import LlmAgent\nroot_agent = None",
                        "requirements.txt": "requests\n"})
    for framework, expected in [("custom", "Choose the Google ADK framework"),
                                ("google-adk", "google-cloud-aiplatform")]:
        upload_id = client.post("/api/uploads", content=archive).json()["id"]
        plan = client.post("/api/plans", json={"source_kind": "zip", "location": upload_id,
                                               "display_name": "Weather", "framework": framework}).json()
        assert any(expected in warning for warning in plan["warnings"]), plan["warnings"]


ENGINE = {
    "name": "projects/123/locations/us-central1/reasoningEngines/42",
    "displayName": "weather-agent", "description": "Answers weather questions",
    "createTime": "2026-11-18T10:00:00Z", "updateTime": "2026-11-18T10:05:00Z",
    "labels": {"managed-by": "agent-platform-console", "source-sha": "abc"},
    "spec": {"agentFramework": "google-adk",
             "classMethods": [{"name": "async_create_session"}, {"name": "async_stream_query"}],
             "sourceCodeSpec": {"pythonSpec": {"entrypointModule": "_agent_platform_adk",
                                               "entrypointObject": "root_agent"}},
             "deploymentSpec": {"maxInstances": 2}},
}


def test_agent_garden_lists_project_agents(console, monkeypatch):
    client, cloud, _ = console
    older = {"name": "projects/123/locations/us-central1/reasoningEngines/7",
             "createTime": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(cloud, "list_agents", lambda: [older, ENGINE], raising=False)
    data = client.get("/api/agents").json()
    assert data["project"] == "example-project"
    first, second = data["agents"]
    assert first["id"] == "42"
    assert first["display_name"] == "weather-agent"
    assert first["framework"] == "google-adk"
    assert first["methods"] == ["async_create_session", "async_stream_query"]
    assert first["managed_by_console"] is True
    assert first["service_account"] == ""
    assert first["entrypoint"] == "_agent_platform_adk.root_agent"
    assert second["display_name"] == "7"
    assert second["deployment"] == "none"
    assert not cloud.submissions


def test_agent_garden_reports_google_errors(console, monkeypatch):
    client, cloud, _ = console
    def denied():
        raise google_http_error(403, "PERMISSION_DENIED", "aiplatform.reasoningEngines.list denied")
    monkeypatch.setattr(cloud, "list_agents", denied, raising=False)
    response = client.get("/api/agents")
    assert response.status_code == 502
    assert "PERMISSION_DENIED" in response.json()["detail"]
    del client.headers["X-Console-Token"]
    assert client.get("/api/agents").status_code == 403


def test_list_agents_pages_through_results(tmp_path):
    session = MagicMock()
    session.__enter__.return_value = session
    first, second = MagicMock(), MagicMock()
    first.json.return_value = {"reasoningEngines": [{"name": "a"}], "nextPageToken": "next"}
    second.json.return_value = {"reasoningEngines": [{"name": "b"}]}
    session.get.side_effect = [first, second]
    cloud = AgentEngineCloud(ConsoleConfig(source_root=tmp_path, project="example-project"))
    cloud.session = lambda: session
    assert [agent["name"] for agent in cloud.list_agents()] == ["a", "b"]
    url = session.get.call_args_list[0].args[0]
    assert url == ("https://us-central1-aiplatform.googleapis.com/v1/projects/example-project/"
                   "locations/us-central1/reasoningEngines")
    assert session.get.call_args_list[1].kwargs["params"]["pageToken"] == "next"


def test_failed_deployment_exposes_logs_link_logs_and_hints(console, monkeypatch):
    client, cloud, source = console
    logs_url = "https://console.cloud.google.com/logs/query;query=x"
    monkeypatch.setattr(cloud, "status", lambda name: {"done": True, "error": {
        "code": 3, "message": "The Reasoning Engine's container failed to start.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "REASONING_ENGINE_CONTAINER_FAILED_TO_START",
                     "metadata": {"logs_url": logs_url}}]}})
    calls = []
    def container_logs(engine_id, since):
        calls.append(engine_id)
        return [{"time": "2026-11-18T10:00:01Z", "severity": "ERROR", "stream": "stderr",
                 "text": "ModuleNotFoundError: No module named 'google.adk'"}]
    monkeypatch.setattr(cloud, "container_logs", container_logs, raising=False)
    monkeypatch.setattr(cloud, "submit", lambda body: {
        "name": "projects/123/locations/us-central1/reasoningEngines/456/operations/9"})
    plan = plan_for(client, source)
    assert client.get(f"/api/plans/{plan['id']}/logs").status_code == 409
    approve(client, plan)
    result = client.get(f"/api/plans/{plan['id']}").json()
    assert result["status"] == "failed"
    assert result["logs_url"] == logs_url
    logs = client.get(f"/api/plans/{plan['id']}/logs").json()
    assert calls == ["456"]
    assert logs["engine_id"] == "456"
    assert logs["lines"][0]["text"].startswith("ModuleNotFoundError")
    assert any("google.adk" in hint for hint in logs["hints"])
    assert "reasoning_engine_id" in logs["explorer_url"]
    assert logs["explorer_url"].endswith("?project=example-project")


def test_container_logs_queries_cloud_logging(tmp_path):
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value.json.return_value = {"entries": [
        {"timestamp": "t2", "severity": "ERROR", "textPayload": "boom",
         "logName": "projects/p/logs/aiplatform.googleapis.com%2Freasoning_engine_stderr"},
        {"timestamp": "t1", "jsonPayload": {"message": "starting"}},
    ]}
    cloud = AgentEngineCloud(ConsoleConfig(source_root=tmp_path, project="example-project"))
    cloud.session = lambda: session
    lines = cloud.container_logs("42", since=1_800_000_000)
    assert [line["text"] for line in lines] == ["starting", "boom"]
    assert lines[1]["stream"] == "stderr"
    body = session.post.call_args.kwargs["json"]
    assert body["resourceNames"] == ["projects/example-project"]
    assert 'resource.labels.reasoning_engine_id="42"' in body["filter"]
    assert 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' in body["filter"]
    with pytest.raises(Exception, match="Unexpected"):
        cloud.container_logs('42" OR true', since=0)
