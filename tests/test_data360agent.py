import json
import unittest
from unittest.mock import Mock, patch

import httpx

from data360agent.agent import Data360Agent, Data360MCPClient, MCP_ENDPOINT
from data360agent.chat import GatewayModel, converse


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

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            with Data360MCPClient(http_client) as client:
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

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            with Data360MCPClient(http_client) as client:
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

        with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
            with Data360MCPClient(http_client) as client:
                result = client.list_tools()

        self.assertEqual(result["tools"], [])

    def test_agent_requires_gateway_configuration_for_chat(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DATA360_GATEWAY_URL"):
                Data360Agent().query("Find GDP data")

    def test_agent_preserves_json_commands_without_model_configuration(self) -> None:
        with patch("data360agent.agent.Data360MCPClient") as client_class:
            client_class.return_value.__enter__.return_value.list_tools.return_value = {"tools": []}
            result = Data360Agent().query('{"action":"list_tools"}')
        self.assertEqual(result, {"action": "list_tools", "result": {"tools": []}})

    def test_agent_routes_greeting_to_conversation(self) -> None:
        with (
            patch("data360agent.agent.GatewayModel.from_env") as model_factory,
            patch("data360agent.agent.Data360MCPClient") as client_class,
        ):
            model_factory.return_value.generate.return_value = {
                "action": "answer", "text": "I can help you explore World Bank Data360."
            }
            client_class.return_value.__enter__.return_value.list_tools.return_value = {"tools": []}
            result = Data360Agent().query(
                "Hi! What can you do?", user_id="user", authorization="Bearer test",
                app_token="test",
            )
        self.assertIn("World Bank", result["reply"])
        self.assertEqual(result["tools_used"], [])

    def test_agent_rejects_malformed_json_and_missing_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "Malformed JSON"):
            Data360Agent().query('{"action":')
        with patch("data360agent.agent.GatewayModel.from_env"):
            with self.assertRaisesRegex(ValueError, "per-request authorization"):
                Data360Agent().query("Hello")


class Data360ConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.list_tools.return_value = {"tools": [{
            "name": "search",
            "description": "Search indicators",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }]}
        self.client.call_tool.return_value = {"content": [{"text": "GDP indicator found"}]}
        self.model = Mock()

    def test_chat_describes_calls_and_summarizes_tool(self) -> None:
        self.model.generate.side_effect = [
            {"action": "describe_tool", "name": "search"},
            {"action": "call_tool", "name": "search", "arguments": {"query": "GDP"}},
            {"action": "answer", "text": "I found a GDP indicator."},
        ]
        result = converse("Find GDP", self.client, self.model, "Bearer user", "app-secret")
        self.assertEqual(result, {
            "reply": "I found a GDP indicator.", "tools_used": ["search"],
        })
        self.client.call_tool.assert_called_once_with("search", {"query": "GDP"})
        final_prompt = self.model.generate.call_args.args[0]
        self.assertIn("GDP indicator found", final_prompt)
        self.assertNotIn("app-secret", final_prompt)
        self.assertNotIn("Bearer user", final_prompt)

    def test_unknown_tool_is_never_called(self) -> None:
        self.model.generate.return_value = {
            "action": "call_tool", "name": "unknown", "arguments": {},
        }
        with self.assertRaisesRegex(RuntimeError, "unknown Data360 tool"):
            converse("Find GDP", self.client, self.model, "Bearer user", "app")
        self.client.call_tool.assert_not_called()

    def test_tool_requires_schema_before_execution(self) -> None:
        self.model.generate.return_value = {
            "action": "call_tool", "name": "search", "arguments": {},
        }
        with self.assertRaisesRegex(RuntimeError, "without reading its schema"):
            converse("Find GDP", self.client, self.model, "Bearer user", "app")
        self.client.call_tool.assert_not_called()

    def test_model_loop_is_bounded(self) -> None:
        self.model.generate.return_value = {"action": "describe_tool", "name": "search"}
        with self.assertRaisesRegex(RuntimeError, "step limit"):
            converse("Find GDP", self.client, self.model, "Bearer user", "app")
        self.assertEqual(self.model.generate.call_count, 8)

    def test_tool_error_is_visible_to_model(self) -> None:
        self.client.call_tool.return_value = {
            "isError": True, "content": [{"text": "Invalid indicator"}],
        }
        self.model.generate.side_effect = [
            {"action": "describe_tool", "name": "search"},
            {"action": "call_tool", "name": "search", "arguments": {}},
            {"action": "answer", "text": "The data lookup failed."},
        ]
        result = converse("Find GDP", self.client, self.model, "Bearer user", "app")
        self.assertEqual(result["reply"], "The data lookup failed.")
        self.assertIn('"isError": true', self.model.generate.call_args.args[0])


class GatewayModelTests(unittest.TestCase):
    def test_generate_uses_only_fixed_gateway_and_per_request_credentials(self) -> None:
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={
                "text": '{"action":"answer","text":"Hello from Data360"}',
            })

        client = httpx.Client
        with patch(
            "data360agent.chat.httpx.Client",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs),
        ):
            command = GatewayModel("https://gateway.test", "data360", "fast").generate(
                "Hello", "Bearer test", "test-token"
            )
        self.assertEqual(command["text"], "Hello from Data360")
        self.assertEqual(
            str(calls[0].url), "https://gateway.test/v1/agents/data360/models/fast/generate"
        )
        self.assertEqual(calls[0].headers["authorization"], "Bearer test")
        self.assertEqual(calls[0].headers["x-app-token"], "test-token")
        self.assertEqual(json.loads(calls[0].content), {"message": "Hello"})

    def test_gateway_denial_propagates(self) -> None:
        client = httpx.Client
        with patch(
            "data360agent.chat.httpx.Client",
            side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(lambda request: httpx.Response(403)), **kwargs
            ),
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                GatewayModel("https://gateway.test", "data360", "fast").generate(
                    "Hello", "Bearer test", "token"
                )

    def test_gateway_rejects_oversized_prompt_before_network_call(self) -> None:
        model = GatewayModel("https://gateway.test", "data360", "fast")
        with self.assertRaisesRegex(ValueError, "16000-character"):
            model.generate("x" * 16001, "Bearer test", "token")

    def test_gateway_rejects_invalid_json_model_output(self) -> None:
        client = httpx.Client
        with patch(
            "data360agent.chat.httpx.Client",
            side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"text": "not JSON"})
                ), **kwargs,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "valid JSON action"):
                GatewayModel("https://gateway.test", "data360", "fast").generate(
                    "Hello", "Bearer test", "token"
                )


if __name__ == "__main__":
    unittest.main()
