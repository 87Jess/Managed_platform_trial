"""Data360 MCP client tests (no model needed). Conversation tests: test_data360_agent.py."""

import json
import unittest
from unittest.mock import patch

import httpx

from data360agent.agent import MCP_ENDPOINT, Data360Agent, Data360MCPClient


class Data360MCPClientTests(unittest.TestCase):
    def test_list_tools_initializes_session_and_uses_mcp_headers(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json", "MCP-Session-Id": "session-1"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "data360_search"}]},
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client, Data360MCPClient(http_client) as client:
            result = client.list_tools()

        self.assertEqual(result["tools"][0]["name"], "data360_search")
        self.assertEqual([json.loads(request.content)["method"] for request in requests], [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ])
        self.assertTrue(all(str(request.url) == MCP_ENDPOINT for request in requests))
        self.assertEqual(requests[1].headers["MCP-Session-Id"], "session-1")
        self.assertEqual(requests[2].headers["MCP-Protocol-Version"], "2025-03-26")

    def test_call_tool_sends_explicit_name_and_arguments(self) -> None:
        calls: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"text": "ok"}]}},
            )

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client, Data360MCPClient(http_client) as client:
            result = client.call_tool("data360_search", {"query": "GDP"})

        self.assertEqual(result["content"][0]["text"], "ok")
        self.assertEqual(calls[-1]["method"], "tools/call")
        self.assertEqual(calls[-1]["params"], {
            "name": "data360_search",
            "arguments": {"query": "GDP"},
        })

    def test_list_tools_reads_streamable_http_event_response(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(
                    'event: message\n'
                    'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
                ),
            )

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client, Data360MCPClient(http_client) as client:
            result = client.list_tools()

        self.assertEqual(result["tools"], [])

    def test_agent_preserves_json_commands_without_model_configuration(self) -> None:
        with patch("data360agent.agent.Data360MCPClient") as client_class:
            client_class.return_value.__enter__.return_value.list_tools.return_value = {"tools": []}
            result = Data360Agent(client_factory=client_class).query('{"action":"list_tools"}')
        self.assertEqual(result, {"action": "list_tools", "result": {"tools": []}})

    def test_agent_rejects_malformed_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "Malformed JSON"):
            Data360Agent().query('{"action":')
