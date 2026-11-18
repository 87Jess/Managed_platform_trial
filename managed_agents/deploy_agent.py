import argparse
import json
from importlib.metadata import version
from urllib.parse import urlparse

from managed_agents.runtime import GovernedAgent, PlatformClient


def main():
    parser = argparse.ArgumentParser(description="Plan an Agent Engine deployment; dry run by default")
    parser.add_argument("--project", default="itsai-mai-gcp-lab-proj")
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--agent-id", default="research")
    parser.add_argument("--model-alias", default="fast")
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--staging-bucket", required=True)
    parser.add_argument("--network-attachment", required=True)
    parser.add_argument("--network", default="managed-agents")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-project")
    parser.add_argument("--security-review-approved", action="store_true")
    args = parser.parse_args()
    PlatformClient(args.gateway_url, args.agent_id, "", "")
    service_account = f"agent-{args.agent_id}@{args.project}.iam.gserviceaccount.com"
    if len(f"agent-{args.agent_id}") > 30:
        parser.error("Agent ID is too long for its dedicated service account")
    plan = {
        "project": args.project, "location": args.location, "agent_id": args.agent_id,
        "gateway_url": args.gateway_url, "service_account": service_account,
        "network_attachment": args.network_attachment, "min_instances": 0, "max_instances": 2,
        "direct_model_or_data_permissions": False,
        "apply": args.apply,
    }
    print(json.dumps(plan, indent=2))
    if not args.apply:
        return
    if args.approved_project != args.project or not args.security_review_approved:
        parser.error("Apply requires --approved-project PROJECT and --security-review-approved")

    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=args.project, location=args.location, staging_bucket=args.staging_bucket)
    agent = agent_engines.create(
        agent_engine=GovernedAgent(args.gateway_url, args.agent_id, args.model_alias),
        display_name=f"platform-{args.agent_id}",
        requirements=[f"google-cloud-aiplatform[agent_engines]=={version('google-cloud-aiplatform')}",
                      f"httpx=={version('httpx')}", f"cloudpickle=={version('cloudpickle')}"],
        extra_packages=["managed_agents"],
        service_account=service_account,
        psc_interface_config={
            "network_attachment": args.network_attachment,
            "dns_peering_configs": [{"domain": urlparse(args.gateway_url).hostname + ".",
                                     "target_project": args.project,
                                     "target_network": args.network}],
        },
        min_instances=0, max_instances=2,
        resource_limits={"cpu": "1", "memory": "1Gi"},
    )
    print(agent.resource_name)


if __name__ == "__main__":
    main()