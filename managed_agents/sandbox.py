"""Smoke-test reviewed agent source in a Google-managed Agent Engine code-execution sandbox.

The snapshot is never imported on the console host. It is sent as a tar.gz to a
code-execution sandbox (gVisor-isolated, no network, no project credentials) where a
small harness extracts it, imports the entrypoint and, for custom agents, calls
``query(message=...)`` once. The sandbox is created with a TTL and deleted afterwards.
"""

import base64
import contextlib
import json
import re
import time

SANDBOX_API = "v1beta1"
RESULT_MARKER = "__AGENT_PLATFORM_SANDBOX_RESULT__"
ARCHIVE_NAME = "agent_source.tar.gz"
TEST_MESSAGE = "Agent platform sandbox smoke test"

HARNESS = r'''
import importlib, json, os, signal, sys, tarfile, traceback
MODULE, OBJECT, FRAMEWORK, MESSAGE, ARCHIVE, MARKER = __PARAMS__
result = {"status": "failed", "stage": "extract"}
try:
    with tarfile.open(ARCHIVE, "r:gz") as archive:
        try:
            archive.extractall("agent_src", filter="data")
        except TypeError:
            archive.extractall("agent_src")
    sys.path.insert(0, os.path.abspath("agent_src"))
    result["stage"] = "import"
    module = importlib.import_module(MODULE)
    agent = getattr(module, OBJECT)
    result["object_type"] = type(agent).__module__ + "." + type(agent).__qualname__
    if FRAMEWORK == "custom":
        result["stage"] = "query"
        query = getattr(agent, "query", None)
        if not callable(query):
            raise TypeError(OBJECT + " has no callable query() method")
        if hasattr(signal, "SIGALRM"):
            signal.alarm(60)
        output = query(message=MESSAGE)
        result["query_output"] = repr(output)[:2000]
    result["status"] = "passed"
except BaseException as error:
    result["error"] = type(error).__name__ + ": " + str(error)[:1000]
    result["traceback"] = traceback.format_exc()[-4000:]
    if isinstance(error, ModuleNotFoundError):
        result["missing_module"] = error.name
print(MARKER + json.dumps(result))
'''


class SandboxError(Exception):
    pass


def harness_code(module: str, object_name: str, framework: str) -> str:
    params = json.dumps([module, object_name, framework, TEST_MESSAGE, ARCHIVE_NAME,
                         RESULT_MARKER])
    return HARNESS.replace("__PARAMS__", params, 1)


def execute_request(archive: bytes, module: str, object_name: str, framework: str) -> dict:
    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    code = json.dumps({"code": harness_code(module, object_name, framework)}).encode("utf-8")
    return {"inputs": [
        {"mimeType": "application/json", "data": b64(code)},
        {"mimeType": "application/gzip", "data": b64(archive),
         "metadata": {"attributes": {"file_name": b64(ARCHIVE_NAME.encode("utf-8"))}}},
    ]}


def parse_execute_response(response: dict) -> dict:
    stdout = stderr = ""
    for chunk in response.get("outputs", []):
        if chunk.get("mimeType") != "application/json" or "data" not in chunk:
            continue
        try:
            payload = json.loads(base64.b64decode(chunk["data"]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        stdout += payload.get("msg_out", "")
        stderr += payload.get("msg_err", "")
    details = {}
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                details = json.loads(line[len(RESULT_MARKER):])
            except ValueError:
                details = {}
            break
    user_stdout = "\n".join(line for line in stdout.splitlines()
                            if not line.startswith(RESULT_MARKER))
    if not details:
        status, message = "failed", "The sandbox did not report a result; see stderr"
    elif details.get("status") == "passed":
        status = "passed"
        message = ("Imported the entrypoint and query() returned" if "query_output" in details
                   else "Imported the entrypoint")
    else:
        status = "failed"
        message = f"Failed at {details.get('stage', 'unknown')} stage: {details.get('error', '')}"
        if details.get("missing_module"):
            message += (". Code-execution sandboxes have no network and only preinstalled"
                        " packages, so this dependency cannot be installed there")
    return {"status": status, "message": message, "stdout": user_stdout[-4000:],
            "stderr": stderr[-4000:], "details": details}


class SandboxRunner:
    """Creates a code-execution sandbox under an Agent Engine instance, runs the harness, deletes it."""

    def __init__(self, session_factory, region: str, engine: str, *, poll_seconds: float = 3,
                 create_timeout: float = 240, sleep=time.sleep):
        pattern = r"projects/[a-z0-9-]+/locations/" + re.escape(region) + r"/reasoningEngines/[0-9]+"
        if not re.fullmatch(pattern, engine):
            raise SandboxError("Sandbox host engine is not configured")
        self.session_factory = session_factory
        self.base = f"https://{region}-aiplatform.googleapis.com/{SANDBOX_API}/"
        self.engine = engine
        self.poll_seconds = poll_seconds
        self.create_timeout = create_timeout
        self.sleep = sleep
        self.sandbox_pattern = re.escape(engine) + r"/sandboxEnvironments/[0-9a-zA-Z_-]+"

    def run(self, archive: bytes, digest: str, module: str, object_name: str,
            framework: str) -> dict:
        with self.session_factory() as client:
            sandbox = self._create(client, digest)
            try:
                response = client.post(f"{self.base}{sandbox}:execute",
                                       json=execute_request(archive, module, object_name,
                                                            framework), timeout=300)
                response.raise_for_status()
                result = parse_execute_response(response.json())
            finally:
                # Best effort: the sandbox's TTL removes it if this delete fails.
                with contextlib.suppress(Exception):
                    client.delete(f"{self.base}{sandbox}", timeout=30)
        result["sandbox"] = sandbox
        return result

    def _create(self, client, digest: str) -> str:
        response = client.post(f"{self.base}{self.engine}/sandboxEnvironments", json={
            "displayName": f"console-test-{digest[:12]}",
            "spec": {"codeExecutionEnvironment": {"codeLanguage": "LANGUAGE_PYTHON"}},
            "ttl": "1800s",
        }, timeout=60)
        response.raise_for_status()
        operation = response.json()
        deadline = time.monotonic() + self.create_timeout
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise SandboxError("Sandbox did not become ready in time")
            self.sleep(self.poll_seconds)
            name = operation.get("name", "")
            if not re.fullmatch(re.escape(self.engine) + r"/.*operations/[0-9a-zA-Z_-]+", name):
                raise SandboxError("Cloud returned an unexpected operation name")
            poll = client.get(f"{self.base}{name}", timeout=30)
            poll.raise_for_status()
            operation = poll.json()
        if operation.get("error"):
            raise SandboxError("Agent Engine could not create the sandbox")
        sandbox = operation.get("response", {}).get("name", "")
        if not re.fullmatch(self.sandbox_pattern, sandbox):
            raise SandboxError("Cloud returned an unexpected sandbox name")
        return sandbox
