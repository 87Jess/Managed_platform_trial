import json

import httpx
import pytest

from data360agent.agent import (
    Data360Agent,
    Data360MCPClient,
    Data360RESTClient,
    MCPError,
    data360_client,
)


@pytest.mark.parametrize(("name", "arguments", "method", "path", "expected"), [
    ("search_indicators", {"search": "GDP", "top": 3}, "POST", "/data360/searchv2",
     {"search": "GDP", "top": 3, "filter": "type eq 'indicator'"}),
    ("get_data", {"DATABASE_ID": "WB_WDI", "REF_AREA": "IND", "skip": 1000},
     "GET", "/data360/data", {"DATABASE_ID": "WB_WDI", "REF_AREA": "IND", "skip": "1000"}),
    ("get_metadata", {"indicatorId": "WB_WDI_SP_POP_TOTL"}, "POST", "/data360/metadata",
     {"query": "&$filter=series_description/idno eq 'WB_WDI_SP_POP_TOTL'"}),
    ("get_disaggregation", {"datasetId": "WB_WDI"}, "GET", "/data360/disaggregation",
     {"datasetId": "WB_WDI"}),
    ("list_indicators", {"datasetId": "WB_WDI"}, "GET", "/data360/indicators",
     {"datasetId": "WB_WDI"}),
])
def test_rest_request(name, arguments, method, path, expected):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.host == "data360api.worldbank.org"
        assert request.url.path == path
        assert request.method == method
        actual = dict(request.url.params) if method == "GET" else json.loads(request.content)
        assert all(actual[key] == value for key, value in expected.items())
        return httpx.Response(200, json={"value": [{"OBS_VALUE": "123"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        agent = Data360Agent(client_factory=lambda: Data360RESTClient(http))
        result = agent.query(json.dumps({
            "action": "call_tool", "name": f"data360_{name}", "arguments": arguments,
        }))
    assert len(seen) == 1
    assert result["result"]["structuredContent"] == {
        "source": "https://data360api.worldbank.org" + path,
        "data": {"value": [{"OBS_VALUE": "123"}]},
    }


@pytest.mark.parametrize(("name", "arguments"), [
    ("data360_get_data", {}),
    ("data360_get_data", {"DATABASE_ID": "WB_WDI", "url": "https://example.org"}),
    ("data360_search_indicators", {"search": "GDP", "top": True}),
    ("data360_get_metadata", {"indicatorId": "ID' or '1' eq '1"}),
    ("https://example.org", {}),
])
def test_rest_rejects_invalid_tool_arguments_before_network(name, arguments):
    transport = httpx.MockTransport(lambda request: pytest.fail(str(request)))
    with httpx.Client(transport=transport) as http, pytest.raises(MCPError):
        Data360RESTClient(http).call_tool(name, arguments)


def test_rest_is_default_and_mcp_remains_selectable(monkeypatch):
    monkeypatch.delenv("DATA360_BACKEND", raising=False)
    with data360_client() as client:
        assert isinstance(client, Data360RESTClient)
    response = Data360Agent().query('{"action":"list_tools"}')
    assert len(response["result"]["tools"]) == 5

    monkeypatch.setenv("DATA360_BACKEND", "mcp")
    with data360_client() as client:
        assert isinstance(client, Data360MCPClient)

    monkeypatch.setenv("DATA360_BACKEND", "invalid")
    with pytest.raises(ValueError, match="DATA360_BACKEND"):
        data360_client()
