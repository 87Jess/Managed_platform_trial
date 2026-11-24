"""Deploy an AgentSpec agent folder to Agent Engine from CI (for example an Azure DevOps pipeline).

It packages and describes the agent exactly as the deployment console does (same AgentSpec
validation, bundle and Agent Engine request), then creates the agent, or updates the agent this
pipeline deployed before under the same AgentSpec name, waits for the operation and optionally
sends one smoke-test message.

    python -m managed_agents.pipeline plan   wdi_agent            # offline: validate + package
    python -m managed_agents.pipeline deploy wdi_agent --smoke-message "hello"

Google credentials come from Application Default Credentials (in CI, a workload identity
federation credential file named by GOOGLE_APPLICATION_CREDENTIALS). No keys are read here.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from requests.exceptions import HTTPError, RequestException

from managed_agents.agentspec import (
    PACKAGING,
    AgentSpec,
    AgentSpecError,
    check_files,
    find_spec,
    load_spec,
)
from managed_agents.console import (
    AgentEngineCloud,
    ConsoleConfig,
    DeploymentInput,
    deployment_body,
    google_error,
    logs_explorer_url,
    operation_error,
    summarize_events,
)
from managed_agents.sources import Bundle, SourceError, bundle_source, local_files

MANAGED_BY = "ado-pipeline"

# Fields an update replaces; the same paths the Vertex AI SDK sends for an in-place update.
UPDATE_MASK = [
    "display_name", "description", "labels",
    "spec.agent_framework", "spec.class_methods", "spec.service_account",
    "spec.source_code_spec.inline_source.source_archive",
    "spec.source_code_spec.python_spec.version",
    "spec.source_code_spec.python_spec.entrypoint_module",
    "spec.source_code_spec.python_spec.entrypoint_object",
    "spec.source_code_spec.python_spec.requirements_file",
    "spec.deployment_spec.env", "spec.deployment_spec.psc_interface_config", "spec.deployment_spec.min_instances",
    "spec.deployment_spec.max_instances", "spec.deployment_spec.resource_limits",
]


class PipelineError(Exception):
    """A deployment problem reported to the pipeline log; exits non-zero."""


def config_from_env(args) -> ConsoleConfig:
    return ConsoleConfig(
        source_root=Path(args.source_root).resolve(),
        project=args.project or os.environ.get("GOOGLE_CLOUD_PROJECT", "itsai-mai-gcp-lab-proj"),
        region=args.region or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        runtime_account=args.runtime_account or os.environ.get("AGENT_RUNTIME_ACCOUNT", ""),
        network_attachment=args.network_attachment or os.environ.get("AGENT_NETWORK_ATTACHMENT", ""),
    )


def prepare(folder: str, config: ConsoleConfig, display_name: str = "") -> tuple[AgentSpec, Bundle, dict]:
    """Validate the folder's AgentSpec and build the Agent Engine request body (no cloud calls)."""
    try:
        files = local_files(folder, config.source_root)
    except SourceError as error:
        raise PipelineError(f"{folder}: {error}") from None
    found = find_spec(files)
    if not found:
        raise PipelineError(f"{folder}: no agentspec.yaml; pipeline deployments require an AgentSpec")
    spec_name, raw = found
    try:
        spec = load_spec(raw)
        problems = check_files(spec, files)
    except AgentSpecError as error:
        problems = error.errors
    if problems:
        raise PipelineError(f"{folder}/{spec_name} is not valid: " + "; ".join(problems))
    if spec.runtime.network == "private" and not config.attachment:
        raise PipelineError(f"{spec.name} needs runtime.network: private, but no network attachment is set")
    framework = PACKAGING[spec.framework]
    try:
        bundle = bundle_source(files, spec.entrypoint.module, spec.runtime.requirements, "",
                               framework, spec.entrypoint.object)
    except SourceError as error:
        raise PipelineError(f"{folder}: {error}") from None
    data = DeploymentInput(source_kind="local", location=folder, display_name=display_name or spec.name,
                           module=spec.entrypoint.module, object_name=spec.entrypoint.object,
                           requirements=spec.runtime.requirements, framework=framework)
    body = deployment_body(config, data, bundle, spec)
    body["labels"]["managed-by"] = MANAGED_BY
    return spec, bundle, body


def find_existing(cloud: AgentEngineCloud, spec: AgentSpec) -> str:
    """Resource name of the agent this pipeline deployed before under the same AgentSpec name."""
    wanted = spec.labels()["agentspec-name"]
    matches = [engine["name"] for engine in cloud.list_agents()
               if (engine.get("labels") or {}).get("agentspec-name") == wanted
               and (engine.get("labels") or {}).get("managed-by") == MANAGED_BY]
    if len(matches) > 1:
        raise PipelineError(f"{len(matches)} pipeline agents are labelled agentspec-name={wanted}; "
                            f"delete the extras first: {', '.join(matches)}")
    return matches[0] if matches else ""


def wait_for(cloud: AgentEngineCloud, operation: str, timeout: float, poll: float = 15.0,
             sleep=time.sleep, clock=time.monotonic) -> dict:
    deadline = clock() + timeout
    while True:
        status = cloud.status(operation)
        if status.get("done"):
            if status.get("error"):
                raise PipelineError(f"Deployment failed: {operation_error(status['error'])}")
            return status
        if clock() >= deadline:
            raise PipelineError(f"Still deploying after {int(timeout)} s; operation {operation}")
        sleep(poll)


def smoke_test(cloud: AgentEngineCloud, name: str, bundle: Bundle, message: str) -> str:
    """Send one message the way the console's Test chat does; returns the reply text."""
    if bundle.framework == "google-adk":
        user = f"pipeline-{uuid.uuid4().hex[:8]}"
        session = cloud.invoke_agent(name, "async_create_session", {"user_id": user}, False)["output"]
        session_id = session.get("id") if isinstance(session, dict) else None
        if not session_id:
            raise PipelineError("Smoke test: the agent did not return a session id")
        events = cloud.invoke_agent(name, "async_stream_query",
                                    {"user_id": user, "session_id": session_id, "message": message}, True)["events"]
        summary = summarize_events(events)
        if summary["errors"]:
            raise PipelineError("Smoke test: the agent returned an error: " + "; ".join(summary["errors"])[:1000])
        return summary["reply"]
    method = next((m for m in bundle.class_methods if m.get("name") == "query"), None)
    properties = ((method or {}).get("parameters") or {}).get("properties") or {"message": {}}
    key = "message" if "message" in properties else "input" if "input" in properties else next(iter(properties))
    output = cloud.invoke_agent(name, "query", {key: message}, False)["output"]
    if isinstance(output, dict):
        for field in ("reply", "output", "text", "answer", "response", "content"):
            if isinstance(output.get(field), str):
                return output[field]
    return output if isinstance(output, str) else json.dumps(output)[:2000]


def set_pipeline_variable(name: str, value: str):
    """Expose a value to later Azure DevOps steps and stages (ignored outside Azure Pipelines)."""
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.setvariable variable={name};isOutput=true]{value}")


def deploy(args, cloud: AgentEngineCloud | None = None, sleep=time.sleep) -> str:
    config = config_from_env(args)
    spec, bundle, body = prepare(args.folder, config, args.display_name)
    cloud = cloud or AgentEngineCloud(config)
    existing = find_existing(cloud, spec)
    if existing:
        print(f"Updating {spec.name} {spec.version} in place: {existing}")
        operation = cloud.update(existing, body, UPDATE_MASK)
    else:
        print(f"Creating {spec.name} {spec.version} in {config.project}/{config.region}")
        operation = cloud.submit(body)
    print(f"Operation: {operation['name']}")
    if args.no_wait:
        return existing or operation["name"].split("/operations/")[0]
    status = wait_for(cloud, operation["name"], args.timeout, sleep=sleep)
    name = existing or (status.get("response") or {}).get("name") or operation["name"].split("/operations/")[0]
    print(f"Deployed: {name}")
    set_pipeline_variable("AGENT_RESOURCE", name)
    if args.smoke_message:
        try:
            reply = smoke_test(cloud, name, bundle, args.smoke_message)
        except HTTPError as error:
            raise PipelineError(f"Smoke test failed ({google_error(error, limit=600)}). Logs: "
                                + logs_explorer_url(config.project, name.rsplit("/", 1)[-1])) from None
        if not reply.strip():
            raise PipelineError("Smoke test: the agent answered with an empty reply")
        print(f"Smoke test reply: {reply[:500]}")
    return name


def plan(args) -> dict:
    config = config_from_env(args)
    spec, bundle, body = prepare(args.folder, config, args.display_name)
    summary = {"agent": spec.name, "version": spec.version, "framework": body["spec"]["agentFramework"],
               "entrypoint": f"{bundle.entrypoint_module}.{bundle.entrypoint_object}",
               "requirements": bundle.requirements, "files": len(bundle.files), "bytes": bundle.total_bytes,
               "digest": bundle.digest, "runtime_account": config.account or "Agent Engine default",
               "network_attachment": config.attachment or "none", "labels": body["labels"]}
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m managed_agents.pipeline", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "deploy"):
        p = sub.add_parser(command)
        p.add_argument("folder", help="Agent folder containing agentspec.yaml, relative to --source-root")
        p.add_argument("--source-root", default=".", help="Repository root (default: current directory)")
        p.add_argument("--project", default="")
        p.add_argument("--region", default="")
        p.add_argument("--runtime-account", default="",
                       help="Runtime service account; 'default' = Agent Engine default identity")
        p.add_argument("--network-attachment", default="", help="PSC network attachment; 'none' = public")
        p.add_argument("--display-name", default="", help="Defaults to the AgentSpec name")
        if command == "deploy":
            p.add_argument("--timeout", type=float, default=1800, help="Seconds to wait for the deployment")
            p.add_argument("--no-wait", action="store_true")
            p.add_argument("--smoke-message", default="", help="Send this message after deploying")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan(args)
        else:
            deploy(args)
    except PipelineError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except GoogleAuthError:
        print("ERROR: Google credentials are missing or could not be exchanged "
              "(check the workload identity federation setup)", file=sys.stderr)
        return 1
    except HTTPError as error:
        print(f"ERROR: Google Cloud refused the request ({google_error(error, limit=1000)})", file=sys.stderr)
        return 1
    except RequestException as error:
        print(f"ERROR: Could not reach Google Cloud ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
