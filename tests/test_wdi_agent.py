"""Offline tests for wdi_agent (LangGraph + Gemini + BigQuery WDI). Uses a fake BigQuery client.

Skipped unless the agent's own dependencies are installed:
    uv run --with-requirements wdi_agent/requirements.txt --with pytest python -m pytest tests/test_wdi_agent.py
"""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_google_genai")
pytest.importorskip("google.cloud.bigquery")

from google.api_core import exceptions
from langchain_core.messages import AIMessage

AGENT_PATH = Path(__file__).resolve().parents[1] / "wdi_agent" / "agent.py"
spec = importlib.util.spec_from_file_location("wdi_agent_module", AGENT_PATH)
wdi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wdi)


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self, timeout=None):
        return self.rows


class FakeBigQuery:
    """Records each query and returns canned rows chosen by the table it reads."""

    def __init__(self, error=None):
        self.queries = []
        self.error = error

    def query(self, sql, job_config=None, location=None):
        params = {p.name: getattr(p, "value", None) or getattr(p, "values", None)
                  for p in job_config.query_parameters}
        self.queries.append({"sql": sql, "params": params, "location": location,
                             "max_bytes": job_config.maximum_bytes_billed})
        if self.error:
            raise self.error
        if "country_summary" in sql:
            return FakeJob([{"country_code": "IND", "short_name": "India", "table_name": "India",
                             "region": "South Asia", "income_group": "Lower middle income"}])
        if "indicators_data" in sql:
            return FakeJob([{"country_code": "IND", "country_name": "India", "year": 2021,
                             "value": 3150306834279.0, "indicator_name": "GDP (current US$)",
                             "unit_of_measure": "US$"}])
        return FakeJob([{"indicator_code": "NY.GDP.MKTP.CD", "indicator_name": "GDP (current US$)",
                         "unit_of_measure": "US$", "topic": "Economic Policy & Debt: National accounts"}])


class ScriptedModel:
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


def agent_with(model, bigquery=None):
    bigquery = bigquery or FakeBigQuery()
    agent = wdi.WDIAgent(model_factory=lambda name: model,
                         data_factory=lambda: wdi.WDIData(client=bigquery))
    return agent, bigquery


def call(name, args, id_="1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_}])


def test_answers_gdp_question_from_bigquery():
    model = ScriptedModel([
        call("wdi_find_country", {"name": "India"}),
        call("wdi_search_indicators", {"keywords": "GDP current US$"}, "2"),
        call("wdi_get_data", {"indicator_code": "NY.GDP.MKTP.CD", "country_codes": ["IND"],
                              "latest_only": True}, "3"),
        call("wdi_calculate", {"operation": "format", "a": 3150306834279.0, "unit": "US$"}, "4"),
        AIMessage(content="India's GDP was 3.15 trillion US$ in 2021 (World Bank WDI)."),
    ])
    agent, bigquery = agent_with(model)
    result = agent.query(message="What is the GDP of India?")
    assert result["reply"].startswith("India's GDP was 3.15 trillion US$")
    assert result["tools_used"] == ["wdi_find_country", "wdi_search_indicators", "wdi_get_data", "wdi_calculate"]
    data_query = bigquery.queries[2]
    assert data_query["params"]["indicator"] == "NY.GDP.MKTP.CD"
    assert data_query["params"]["countries"] == ["IND"]
    assert "QUALIFY ROW_NUMBER()" in data_query["sql"]
    assert data_query["location"] == "US" and data_query["max_bytes"] == 5 * 1024**3
    assert "@indicator" in data_query["sql"] and "NY.GDP" not in data_query["sql"]  # parameterised
    assert '"formatted": "3.15 trillion US$"' in model.seen[-1][-1].content
    assert {tool["function"]["name"] for tool in model.tools} == wdi.TOOL_NAMES


def test_year_range_query_has_no_latest_filter():
    agent, bigquery = agent_with(ScriptedModel([]))
    result = agent.call_tool("wdi_get_data", {"indicator_code": "SP.POP.TOTL", "country_codes": ["ken", "UGA"],
                                              "start_year": 2015, "end_year": 2020.0})
    query = bigquery.queries[0]
    assert "QUALIFY" not in query["sql"]
    assert query["params"]["countries"] == ["KEN", "UGA"]
    assert (query["params"]["start"], query["params"]["end"]) == (2015, 2020)
    assert result["source"].startswith("World Bank")


@pytest.mark.parametrize(("name", "args", "message"), [
    ("wdi_get_data", {"indicator_code": "NY.GDP'; DROP TABLE x;--", "country_codes": ["IND"]}, "indicator_code"),
    ("wdi_get_data", {"indicator_code": "NY.GDP.MKTP.CD", "country_codes": "IND"}, "country_codes"),
    ("wdi_get_data", {"indicator_code": "NY.GDP.MKTP.CD", "country_codes": ["IND"] * 16}, "country_codes"),
    ("wdi_get_data", {"indicator_code": "NY.GDP.MKTP.CD", "country_codes": ["IND"],
                      "start_year": 2020, "end_year": 2010}, "start_year"),
    ("wdi_get_data", {"indicator_code": "X", "country_codes": ["IND"], "sql": "SELECT 1"}, "unknown"),
    ("wdi_search_indicators", {"keywords": "!!!"}, "keywords"),
    ("wdi_search_indicators", {"keywords": "gdp", "limit": 500}, "limit"),
    ("wdi_find_country", {"name": "x"}, "name"),
    ("wdi_drop_tables", {}, "Unknown tool"),
])
def test_bad_tool_input_is_rejected_before_bigquery(name, args, message):
    agent, bigquery = agent_with(ScriptedModel([]))
    with pytest.raises(wdi.ToolError, match=message):
        agent.call_tool(name, args)
    assert bigquery.queries == []


def test_permission_errors_explain_what_to_grant():
    agent, _ = agent_with(ScriptedModel([]), FakeBigQuery(exceptions.Forbidden("Access Denied: bigquery.jobs.create")))
    with pytest.raises(wdi.ToolError, match=r"roles/bigquery.jobUser.*VPC Service Controls"):
        agent.call_tool("wdi_find_country", {"name": "India"})


def test_tool_errors_go_back_to_the_model():
    model = ScriptedModel([
        call("wdi_get_data", {"indicator_code": "bad code!", "country_codes": ["IND"]}),
        AIMessage(content="Sorry, I couldn't find that indicator."),
    ])
    agent, _ = agent_with(model)
    assert agent.query(message="GDP of India?")["tools_used"] == []
    assert "TOOL ERROR: indicator_code" in model.seen[-1][-1].content


@pytest.mark.parametrize(("args", "formatted"), [
    ({"operation": "format", "a": 3389689197373.2, "unit": "US$"}, "3.39 trillion US$"),
    ({"operation": "percent_change", "a": 3.15e12, "b": 3.39e12}, "+7.62%"),
    ({"operation": "cagr", "a": 2.0e12, "b": 3.956e12, "start_year": 2015, "end_year": 2025},
     "7.06% per year (2015-2025)"),
    ({"operation": "difference", "a": 1.2e9, "b": 1.45e9, "unit": "people"}, "250.00 million people"),
    ({"operation": "share", "a": 3.39e12, "b": 101e12}, "3.36%"),
])
def test_calculator(args, formatted):
    assert wdi.calculate(**args)["formatted"] == formatted


@pytest.mark.parametrize("args", [
    {"operation": "ratio", "a": 1, "b": 0},
    {"operation": "cagr", "a": 1, "b": 2, "start_year": 2020, "end_year": 2020},
    {"operation": "difference", "a": True, "b": 1},
    {"operation": "sqrt", "a": 4, "b": 1},
])
def test_calculator_rejects_bad_input(args):
    with pytest.raises(wdi.ToolError):
        wdi.calculate(**args)


def test_json_commands_and_input_aliases():
    agent, _ = agent_with(ScriptedModel([AIMessage(content="Hi! Ask me about development data.")]))
    names = [tool["name"] for tool in agent.query(message='{"action": "list_tools"}')["result"]["tools"]]
    assert names == ["wdi_find_country", "wdi_search_indicators", "wdi_get_data", "wdi_calculate"]
    called = agent.query(message=json.dumps({"action": "call_tool", "name": "wdi_find_country",
                                             "arguments": {"name": "India"}}))
    assert called["result"]["countries"][0]["country_code"] == "IND"
    assert agent.query(input="hello")["reply"].startswith("Hi!")
    with pytest.raises(ValueError, match="Malformed"):
        agent.query(message="{not json")


def test_falls_back_when_the_model_is_not_found(monkeypatch):
    monkeypatch.setenv("WDI_MODEL", "gemini-retired")
    monkeypatch.setenv("WDI_FALLBACK_MODEL", "gemini-next")

    class Missing:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("404 NOT_FOUND: Publisher model gemini-retired was not found")

    good = ScriptedModel([AIMessage(content="Hello!")])
    agent = wdi.WDIAgent(model_factory=lambda name: Missing() if name == "gemini-retired" else good,
                         data_factory=lambda: wdi.WDIData(client=FakeBigQuery()))
    assert agent.query(message="hi")["model"] == "gemini-next"


def test_dataset_setting_is_validated(monkeypatch):
    monkeypatch.setenv("WDI_DATASET", "evil`; DROP")
    with pytest.raises(ValueError, match="WDI_DATASET"):
        wdi.WDIData(client=FakeBigQuery())
