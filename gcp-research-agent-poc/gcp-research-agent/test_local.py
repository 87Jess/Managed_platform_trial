"""Run a local smoke test. Requires ADC credentials and project configuration."""

from dotenv import load_dotenv
from agent import build_agent

load_dotenv()

agent = build_agent()

tests = [
    "What is the Model Context Protocol? Explain its core components.",
    "Compare Google Agent Runtime and AWS Bedrock AgentCore. Cite sources.",
]

for question in tests:
    print("\n" + "=" * 80)
    print("QUESTION:", question)
    print("=" * 80)
    result = agent.query(input=question)
    print(result)
