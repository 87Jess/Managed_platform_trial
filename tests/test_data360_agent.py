"""Offline tests for data360agent (LangGraph + Gemini + Data360 MCP).

Skipped unless the agent's own dependencies are installed:
    pip install -r data360agent/requirements.txt
"""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_google_genai")

from langchain_core.messages import AIMessage

AGENT_PATH = Path(__file__).resolve().parents[1] / "data360agent" / "agent.py"
spec = importlib.util.spec_from_file_location("data360_agent", AGENT_PATH)
data360 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data360)

TOOLS = [
    {"name": "data360_search", "description": "Search indicators",
     "inputSchema": {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#",
                     "additionalProperties": False,
                     "properties": {"query": {"type": "string"},
                                    "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                                    "countries": {"type": "array", "items": {"type": "string"}}},
                     "required": ["query", "missing"]}},
    {"name": "data360/get data", "description": "Fetch observations",
     "inputSchema": {"type": "object", "properties": {"indicator": {"type": ["string", "null"]}}}},
]


class FakeClient:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def list_tools(self):
        return {"tools": TOOLS}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "data360_search":
            return {"content": [{"type": "text", "text": "WB_WDI_NY_GDP_MKTP_CD: GDP (current US$)"}]}
        return {"content": [{"type": "text", "text": "India 2024: 3.9 trillion US$"}]}


class ScriptedModel:
    """Stands in for ChatGoogleGenerativeAI: replays AI messages, records what it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []
        self.tools = None

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)


def agent_with(model, client=None):
    client = client or FakeClient()
    return data360.Data360Agent(model_factory=lambda name: model, client_factory=lambda: client), client


def test_plain_question_runs_tools_then_answers():
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "data360_search", "args": {"query": "GDP"}, "id": "1"}]),
        AIMessage(content="", tool_calls=[{"name": "data360_get_data",
                                           "args": {"indicator": "WB_WDI_NY_GDP_MKTP_CD"}, "id": "2"}]),
        AIMessage(content="India's GDP in 2024 was about 3.9 trillion US$ (World Bank WDI)."),
    ])
    agent, client = agent_with(model)
    result = agent.query(message="What is the GDP of India?")
    assert result["reply"].startswith("India's GDP in 2024")
    assert result["tools_used"] == ["data360_search", "data360/get data"]
    assert client.calls[1] == ("data360/get data", {"indicator": "WB_WDI_NY_GDP_MKTP_CD"})
    final_prompt = model.seen[-1]
    assert final_prompt[0].type == "system"
    assert "3.9 trillion" in final_prompt[-1].content
    assert {tool["function"]["name"] for tool in model.tools} == {"data360_search", "data360_get_data"}


def test_json_commands_still_work_for_debugging():
    agent, _ = agent_with(ScriptedModel([]))
    listed = agent.query(message='{"action": "list_tools"}')
    assert listed["result"]["tools"][0]["name"] == "data360_search"
    called = agent.query(message=json.dumps({"action": "call_tool", "name": "data360_search",
                                             "arguments": {"query": "GDP"}}))
    assert called["result"]["content"][0]["text"].startswith("WB_WDI")
    with pytest.raises(ValueError, match="Malformed"):
        agent.query(message="{not json")


def test_falls_back_when_the_model_is_not_found(monkeypatch):
    monkeypatch.setenv("DATA360_MODEL", "gemini-retired")
    monkeypatch.setenv("DATA360_FALLBACK_MODEL", "gemini-next")

    class Missing:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("404 NOT_FOUND: Publisher model gemini-retired was not found")

    good = ScriptedModel([AIMessage(content="Hello! Ask me about development data.")])
    agent = data360.Data360Agent(model_factory=lambda name: Missing() if name == "gemini-retired" else good,
                                 client_factory=FakeClient)
    result = agent.query(message="hi")
    assert result["model"] == "gemini-next"
    assert result["reply"].startswith("Hello!")


def test_tool_budget_ends_with_a_friendly_reply():
    loop = [AIMessage(content="", tool_calls=[{"name": "data360_search", "args": {"query": "x"}, "id": str(i)}])
            for i in range(40)]
    agent, _ = agent_with(ScriptedModel(loop))
    assert "tool-call budget" in agent.query(message="everything about everything")["reply"]


def test_tool_errors_are_returned_to_the_model():
    class Failing(FakeClient):
        def call_tool(self, name, arguments):
            raise data360.MCPError("MCP JSON-RPC error -32602: bad arguments")

    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "data360_search", "args": {"query": "GDP"}, "id": "1"}]),
        AIMessage(content="Sorry, the data service rejected that request."),
    ])
    agent, _ = agent_with(model, Failing())
    assert agent.query(message="GDP of India?")["tools_used"] == []
    assert "TOOL ERROR" in model.seen[-1][-1].content


def test_schema_cleaning_matches_gemini_rules():
    definitions, names = data360.gemini_tools(TOOLS)
    search = definitions[0]["function"]["parameters"]
    assert "$schema" not in search and "additionalProperties" not in search
    assert search["properties"]["limit"] == {"type": "integer"}
    assert search["properties"]["countries"] == {"type": "array", "items": {"type": "string"}}
    assert search["required"] == ["query"]
    assert names == {"data360_search": "data360_search", "data360_get_data": "data360/get data"}
    assert definitions[1]["function"]["parameters"]["properties"]["indicator"] == {"type": "string"}


def test_real_gemini_client_accepts_the_tool_definitions():
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = ChatGoogleGenerativeAI(model="gemini-2.5-flash", vertexai=True, project="example-project",
                                   location="global", google_api_key="unused-in-test")
    definitions, _ = data360.gemini_tools(TOOLS)
    bound = model.bind_tools(definitions)
    declarations = bound.kwargs["tools"]
    assert declarations


def test_accepts_other_input_field_names():
    model = ScriptedModel([AIMessage(content="Hi!"), AIMessage(content="Hello again.")])
    agent, _ = agent_with(model)
    assert agent.query(input="hello")["reply"] == "Hi!"
    assert agent.query(prompt="hello")["reply"] == "Hello again."
    with pytest.raises(ValueError, match="non-empty"):
        agent.query()


def test_answers_even_when_the_data_server_is_unreachable():
    import httpx

    class Down(FakeClient):
        def list_tools(self):
            raise httpx.ConnectError("connection refused")

    model = ScriptedModel([AIMessage(content="I can't reach the Data360 service right now.")])
    agent, _ = agent_with(model, Down())
    result = agent.query(message="GDP of India?")
    assert result["reply"].startswith("I can't reach")
    assert "ConnectError" in result["tools_unavailable"]
    assert model.tools is None  # no tools bound
    assert "unavailable right now" in model.seen[0][0].content


def test_history_is_passed_to_the_model():
    model = ScriptedModel([AIMessage(content="In 2023 it was 3.6 trillion US$.")])
    agent, _ = agent_with(model)
    agent.query(message="and in 2023?", history=[{"role": "user", "content": "GDP of India?"},
                                                 {"role": "assistant", "content": "3.9 trillion US$ in 2024."}])
    sent = model.seen[0]
    assert [message.type for message in sent] == ["system", "human", "ai", "human"]
