"""Deploy the LangChain research agent to Gemini Enterprise Agent Runtime."""

import os
from dotenv import load_dotenv
import vertexai

from agent import build_agent

load_dotenv()

PROJECT_ID = os.getenv("PROJECT_ID")
LOCATION = os.getenv("LOCATION", "us-central1")
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "LangChain Research Agent")

if not PROJECT_ID:
    raise RuntimeError("Set PROJECT_ID in .env or the environment.")

# The deployment client is used for managed Agent Runtime operations.
client = vertexai.Client(project=PROJECT_ID, location=LOCATION)
agent = build_agent()

remote_agent = client.agent_engines.create(
    agent,
    config={
        "display_name": DISPLAY_NAME,
        "requirements": [
            "google-cloud-aiplatform[agent_engines,langchain]>=1.112.0,<2.0.0",
        ],
    },
)

resource_name = getattr(remote_agent, "resource_name", None)
if not resource_name:
    resource_name = getattr(remote_agent, "name", None)

if resource_name:
    with open(".agent_resource", "w", encoding="utf-8") as f:
        f.write(resource_name)

print("Deployment request completed.")
print(f"Agent resource: {resource_name or remote_agent}")
print("Keep the resource name for remote testing and cleanup.")
