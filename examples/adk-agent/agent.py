"""Minimal Google ADK agent: Gemini with one local tool. Deploy with the console (AgentSpec framework adk)."""

import datetime

from google.adk.agents import Agent


def current_utc_time() -> dict:
    """Return the current UTC date and time."""
    return {"utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}


root_agent = Agent(
    name="clock_agent",
    model="gemini-2.5-flash",
    description="Answers questions about the current date and time.",
    instruction="You answer questions about the current date and time. Use the current_utc_time tool.",
    tools=[current_utc_time],
)
