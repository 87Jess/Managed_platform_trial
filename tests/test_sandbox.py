import base64
import json
import subprocess
import sys

import pytest

from managed_agents.sandbox import (
    ARCHIVE_NAME,
    RESULT_MARKER,
    SandboxError,
    SandboxRunner,
    execute_request,
    parse_execute_response,
)
from managed_agents.sources import bundle_source

ENGINE = "projects/example-project/locations/us-central1/reasoningEngines/123"
SANDBOX = ENGINE + "/sandboxEnvironments/456"
BASE = "https://us-central1-aiplatform.googleapis.com/v1beta1/"


def output(stdout, stderr=""):
    payload = json.dumps({"msg_out": stdout, "msg_err": stderr}).encode()
    return {"outputs": [{"mimeType": "application/json", "data": base64.b64encode(payload).decode()}]}


class Reply:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        assert self.status_code == 200

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, execute_payload):
        self.calls = []
        self.execute_payload = execute_payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json, timeout):
        self.calls.append(("post", url, json))
        if url.endswith("/sandboxEnvironments"):
            return Reply({"name": ENGINE + "/operations/9", "done": False})
        return Reply(self.execute_payload)

    def get(self, url, timeout):
        self.calls.append(("get", url, None))
        return Reply({"name": ENGINE + "/operations/9", "done": True,
                      "response": {"name": SANDBOX}})

    def delete(self, url, timeout):
        self.calls.append(("delete", url, None))
        return Reply({})


def run_harness(tmp_path, files, module="agent", object_name="root_agent", framework="custom"):
    bundle = bundle_source(files, module, "requirements.txt")
    request = execute_request(bundle.archive, module, object_name, framework)
    code = json.loads(base64.b64decode(request["inputs"][0]["data"]))["code"]
    (tmp_path / ARCHIVE_NAME).write_bytes(base64.b64decode(request["inputs"][1]["data"]))
    completed = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                               capture_output=True, text=True, timeout=60, check=False)
    return parse_execute_response(output(completed.stdout, completed.stderr))


def test_runner_creates_executes_and_deletes_sandbox():
    session = FakeSession(output(RESULT_MARKER + json.dumps(
        {"status": "passed", "stage": "query", "query_output": "{'reply': 'x'}"})))
    runner = SandboxRunner(lambda: session, "us-central1", ENGINE, sleep=lambda _: None)
    result = runner.run(b"archive", "a" * 64, "agent", "root_agent", "custom")
    assert result["status"] == "passed"
    assert result["sandbox"] == SANDBOX
    create = session.calls[0]
    assert create[1] == BASE + ENGINE + "/sandboxEnvironments"
    assert create[2]["spec"] == {"codeExecutionEnvironment": {"codeLanguage": "LANGUAGE_PYTHON"}}
    assert create[2]["ttl"] == "1800s"
    assert ("post", BASE + SANDBOX + ":execute") == session.calls[2][:2]
    assert session.calls[-1] == ("delete", BASE + SANDBOX, None)


def test_runner_deletes_sandbox_even_when_execution_fails():
    session = FakeSession(output(""))
    session.post_original = session.post

    def failing_post(url, json, timeout):
        if url.endswith(":execute"):
            raise TimeoutError("lost")
        return session.post_original(url, json, timeout)

    session.post = failing_post
    runner = SandboxRunner(lambda: session, "us-central1", ENGINE, sleep=lambda _: None)
    with pytest.raises(TimeoutError):
        runner.run(b"archive", "a" * 64, "agent", "root_agent", "custom")
    assert session.calls[-1][0] == "delete"


@pytest.mark.parametrize("engine", ["", "projects/p/locations/europe-west1/reasoningEngines/1",
                                    "projects/p/locations/us-central1/reasoningEngines/1/x"])
def test_runner_rejects_unconfigured_or_foreign_engine(engine):
    with pytest.raises(SandboxError):
        SandboxRunner(lambda: None, "us-central1", engine)


def test_harness_imports_and_queries_custom_agent(tmp_path):
    result = run_harness(tmp_path, {
        "agent.py": b"class A:\n    def query(self, message):\n        return {'reply': message}\n"
                    b"root_agent = A()\n",
        "requirements.txt": b""})
    assert result["status"] == "passed", result
    assert "smoke test" in result["details"]["query_output"]


def test_harness_reports_missing_dependency(tmp_path):
    result = run_harness(tmp_path, {"agent.py": b"import not_a_real_package\nroot_agent = 1\n",
                                    "requirements.txt": b"not-a-real-package\n"})
    assert result["status"] == "failed"
    assert result["details"]["missing_module"] == "not_a_real_package"
    assert "no network" in result["message"]


def test_harness_requires_query_for_custom_agents(tmp_path):
    result = run_harness(tmp_path, {"agent.py": b"root_agent = object()\n",
                                    "requirements.txt": b""})
    assert result["status"] == "failed"
    assert "query()" in result["message"]


def test_missing_result_is_a_failure():
    assert parse_execute_response(output("printed only"))["status"] == "failed"
