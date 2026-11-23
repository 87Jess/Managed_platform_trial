"""Query an already deployed Agent Runtime agent."""

import os
from pathlib import Path
from dotenv import load_dotenv
import agentplatform

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION", "us-central1")
RESOURCE_NAME = os.getenv("AGENT_RESOURCE_NAME")

if not RESOURCE_NAME and Path(".agent_resource").exists():
    RESOURCE_NAME = Path(".agent_resource").read_text(encoding="utf-8").strip()

if not PROJECT_ID:
    raise RuntimeError("Set PROJECT_ID.")
if not RESOURCE_NAME:
    raise RuntimeError(
        "Set AGENT_RESOURCE_NAME or run deploy.py to create .agent_resource."
    )

client = agentplatform.Client(project=PROJECT_ID, location=LOCATION)

# Retrieve the deployed agent by its full resource name.
remote_agent = client.runtimes.get(name=RESOURCE_NAME)

question = (
    "Research the latest official Google Cloud documentation for Agent Runtime. "
    "Summarize its deployment workflow and cite source URLs."
)
print(remote_agent.query(input=question))
