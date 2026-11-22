"""AgentSpec: the versioned, declarative description of a managed agent.

An agent's source folder carries an ``agentspec.yaml`` next to its code. It records who owns
the agent, which framework and entrypoint it uses, the models, tools (MCP servers and platform
connectors) and runtime resources it needs, its data classification and how it is evaluated.
The console reads it at review time, and CI can validate it with::

    python -m managed_agents.agentspec validate path/to/agent-folder [more paths ...]
    python -m managed_agents.agentspec schema > schemas/agentspec-v1.json

Versioning rules are in docs/agentspec.md.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

API_VERSION = "agentspec/v1"
# Accepted apiVersions. When v2 ships, v1 stays here for one deprecation period (see docs/agentspec.md).
SUPPORTED_API_VERSIONS = (API_VERSION,)
SPEC_FILENAMES = ("agentspec.yaml", "agentspec.yml", "agentspec.json")
MAX_SPEC_BYTES = 64 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "agentspec-v1.json"

# Agent Engine framework label for each AgentSpec framework, and how the console packages it.
ENGINE_FRAMEWORKS = {"adk": "google-adk", "langgraph": "langgraph", "langchain": "langchain", "custom": "custom"}
PACKAGING = {"adk": "google-adk", "langgraph": "custom", "langchain": "custom", "custom": "custom"}

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,61}[a-z0-9]$",
                            description="Lowercase letters, digits and hyphens, 3-63 characters")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z_]\w*$", max_length=100)]
SECRET_HINT = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE", re.IGNORECASE)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Owner(Strict):
    name: str = Field(min_length=1, max_length=100, description="Accountable person")
    team: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)


class Entrypoint(Strict):
    module: str = Field(default="agent", pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$", max_length=150)
    object: Identifier = "root_agent"


class ModelRef(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.@/_-]{1,99}$", description="e.g. gemini-2.5-flash")
    role: Literal["primary", "fallback"] = "primary"
    location: str = Field(default="global", pattern=r"^(global|[a-z]+-[a-z]+[0-9]+)$")


class MCPServer(Strict):
    name: Name
    url: str = Field(pattern=r"^https://[^\s/?#]+(/[^\s?#]*)?$", max_length=500,
                     description="HTTPS endpoint of the MCP server (streamable HTTP)")
    tools: list[str] = Field(default_factory=list, description="Allowed tools; empty means all tools")


class Tools(Strict):
    mcp_servers: list[MCPServer] = Field(default_factory=list)
    connectors: list[Name] = Field(default_factory=list, description="Platform registry connector IDs")


class Resources(Strict):
    cpu: Literal["1", "2", "4", "8"] = "1"
    memory: str = Field(default="1Gi", pattern=r"^[1-9][0-9]*(Mi|Gi)$")
    min_instances: int = Field(default=0, ge=0, le=10)
    max_instances: int = Field(default=2, ge=1, le=100)

    @model_validator(mode="after")
    def instance_range(self):
        if self.min_instances > self.max_instances:
            raise ValueError("min_instances must not exceed max_instances")
        return self


class Runtime(Strict):
    python: Literal["3.10", "3.11", "3.12", "3.13"] = "3.12"
    requirements: str = Field(default="requirements.txt", pattern=r"^[\w./-]{1,150}$")
    resources: Resources = Field(default_factory=Resources)
    network: Literal["public", "private"] = Field(
        default="public", description="private requires the platform's Private Service Connect attachment")
    env: dict[str, str] = Field(default_factory=dict, description="Non-secret environment variables only")
    telemetry: StrictBool = Field(default=False, description="Enable managed Agent Runtime telemetry")

    @model_validator(mode="after")
    def no_secrets(self):
        for name, value in self.env.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name):
                raise ValueError(f"env name {name!r} must be UPPER_SNAKE_CASE")
            if name.startswith(("GOOGLE_CLOUD_AGENT_ENGINE", "K_")) or SECRET_HINT.search(name):
                raise ValueError(f"env {name!r} is reserved or looks like a secret; use Secret Manager")
            if len(value) > 1000:
                raise ValueError(f"env {name!r} value is longer than 1000 characters")
        return self


class EvalSuite(Strict):
    name: Name
    kind: Literal["golden", "safety", "prompt_injection", "custom"]
    dataset: str = Field(pattern=r"^[\w./-]{1,200}$", description="Path in the agent folder or gs:// URI")
    threshold: float = Field(ge=0, le=1, description="Minimum pass rate, 0-1")


class Evaluation(Strict):
    suites: list[EvalSuite] = Field(default_factory=list)
    required_before_deploy: bool = False


class AgentSpec(Strict):
    api_version: Literal["agentspec/v1"] = Field(alias="apiVersion")
    name: Name
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$",
                         description="Semantic version of the agent")
    description: str = Field(default="", max_length=500)
    owner: Owner
    framework: Literal["adk", "langgraph", "langchain", "custom"]
    entrypoint: Entrypoint = Field(default_factory=Entrypoint)
    data_classification: Literal["public", "internal", "confidential"]
    models: list[ModelRef] = Field(default_factory=list, description="Models the agent calls; empty if none")
    tools: Tools = Field(default_factory=Tools)
    runtime: Runtime = Field(default_factory=Runtime)
    evaluation: Evaluation = Field(default_factory=Evaluation)

    @model_validator(mode="after")
    def policy(self):
        if self.models and sum(model.role == "primary" for model in self.models) != 1:
            raise ValueError("models must have exactly one primary model")
        names = [server.name for server in self.tools.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        if self.data_classification == "confidential":
            if self.runtime.network != "private":
                raise ValueError("confidential agents must use runtime.network: private")
            if not any(suite.kind == "safety" for suite in self.evaluation.suites):
                raise ValueError("confidential agents must declare a safety evaluation suite")
        if self.evaluation.required_before_deploy and not self.evaluation.suites:
            raise ValueError("evaluation.required_before_deploy needs at least one suite")
        return self

    def labels(self) -> dict[str, str]:
        """Google Cloud resource labels (lowercase, max 63 characters)."""
        def clean(value: str) -> str:
            return re.sub(r"[^a-z0-9_-]", "_", value.lower())[:63]
        return {"agentspec-name": clean(self.name), "agentspec-version": clean(self.version),
                "agent-team": clean(self.owner.team), "data-class": self.data_classification,
                "agent-framework": self.framework}

    def summary(self) -> dict:
        return {"api_version": self.api_version, "name": self.name, "version": self.version,
                "description": self.description, "owner": self.owner.model_dump(),
                "framework": self.framework, "data_classification": self.data_classification,
                "models": [f"{model.id} ({model.role})" for model in self.models],
                "mcp_servers": [server.url for server in self.tools.mcp_servers],
                "connectors": list(self.tools.connectors), "network": self.runtime.network,
                "python": self.runtime.python, "resources": self.runtime.resources.model_dump(),
                "telemetry": self.runtime.telemetry,
                "evaluation": [f"{suite.name} ({suite.kind}, ≥{suite.threshold:.0%})"
                               for suite in self.evaluation.suites]}


class AgentSpecError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _format(error: ValidationError) -> list[str]:
    messages = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"]) or "(root)"
        message = item["msg"].removeprefix("Value error, ")
        if item["type"] == "literal_error" and item["loc"] == ("apiVersion",):
            message = (f"unsupported apiVersion {item['input']!r}; supported: "
                       + ", ".join(SUPPORTED_API_VERSIONS))
        messages.append(f"{where}: {message}")
    return messages


def load_spec(raw: bytes | str) -> AgentSpec:
    """Parse and validate an AgentSpec (YAML or JSON). Raises AgentSpecError with every problem."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_SPEC_BYTES:
            raise AgentSpecError([f"(file): larger than {MAX_SPEC_BYTES // 1024} KiB"])
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AgentSpecError(["(file): not UTF-8 text"]) from None
    try:
        document = yaml.safe_load(raw)  # JSON is valid YAML
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark else "(file)"
        raise AgentSpecError([f"{where}: not valid YAML ({getattr(error, 'problem', error)})"]) from None
    if not isinstance(document, dict):
        raise AgentSpecError(["(root): the spec must be a mapping of fields"])
    try:
        return AgentSpec.model_validate(document)
    except ValidationError as error:
        raise AgentSpecError(_format(error)) from None


def find_spec(files: dict[str, bytes], subdirectory: str = "") -> tuple[str, bytes] | None:
    prefix = subdirectory.strip("/") + "/" if subdirectory.strip("/") else ""
    for name in SPEC_FILENAMES:
        if prefix + name in files:
            return name, files[prefix + name]
    return None


def check_files(spec: AgentSpec, files: dict[str, bytes], subdirectory: str = "") -> list[str]:
    """Cross-check the spec against the agent's files (paths relative to the agent folder)."""
    prefix = subdirectory.strip("/") + "/" if subdirectory.strip("/") else ""
    present = {name[len(prefix):] for name in files if name.startswith(prefix)}
    module = spec.entrypoint.module.replace(".", "/")
    problems = []
    if module + ".py" not in present and module + "/__init__.py" not in present:
        problems.append(f"entrypoint.module: {spec.entrypoint.module!r} was not found in the agent folder")
    if spec.runtime.requirements not in present:
        problems.append(f"runtime.requirements: {spec.runtime.requirements!r} was not found in the agent folder")
    for suite in spec.evaluation.suites:
        if not suite.dataset.startswith("gs://") and suite.dataset not in present:
            problems.append(f"evaluation.suites.{suite.name}.dataset: {suite.dataset!r} was not found")
    return problems


def json_schema() -> dict:
    schema = AgentSpec.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://agent-platform.internal/schemas/agentspec-v1.json"
    schema["title"] = "AgentSpec v1"
    return schema


def _folder_files(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): b"" for path in folder.rglob("*")
            if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts}


def validate_path(path: Path) -> list[str]:
    """Validate a spec file, or the spec inside an agent folder (also checking its files)."""
    if path.is_dir():
        found = next((path / name for name in SPEC_FILENAMES if (path / name).is_file()), None)
        if found is None:
            return [f"{path}: no agentspec.yaml found"]
        folder, spec_path = path, found
    else:
        folder, spec_path = path.parent, path
    try:
        spec = load_spec(spec_path.read_bytes())
    except AgentSpecError as error:
        return [f"{spec_path}: {problem}" for problem in error.errors]
    return [f"{spec_path}: {problem}" for problem in check_files(spec, _folder_files(folder))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m managed_agents.agentspec", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="Validate agentspec files or agent folders")
    check.add_argument("paths", nargs="+", type=Path)
    commands.add_parser("schema", help="Print the AgentSpec JSON Schema")
    args = parser.parse_args(argv)
    if args.command == "schema":
        print(json.dumps(json_schema(), indent=2, ensure_ascii=False))
        return 0
    failed = 0
    for path in args.paths:
        problems = validate_path(path)
        if problems:
            failed += 1
            print("\n".join(problems), file=sys.stderr)
        else:
            print(f"{path}: valid {API_VERSION}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
