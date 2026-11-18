import json
from typing import Any

import httpx

if __package__:
    from .chat import GatewayModel, converse
else:
    from chat import GatewayModel, converse


MCP_ENDPOINT = "https://maimcpext.worldbank.org/ext/data360/mcp"
MCP_PROTOCOL_VERSION = "2025-03-26"


class MCPError(RuntimeError):
    pass


class Data360MCPClient:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> "Data360MCPClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._http.close()

    def list_tools(self) -> dict[str, Any]:
        headers = self._initialize()
        return self._request("tools/list", {}, headers)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = self._initialize()
        return self._request("tools/call", {"name": name, "arguments": arguments}, headers)

    def _initialize(self) -> dict[str, str]:
        request_id = 1
        response, response_headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "data360-agent", "version": "0.1.0"},
                },
            },
            {},
            request_id,
        )
        result = self._result(response, request_id)
        protocol_version = result.get("protocolVersion")
        if protocol_version != MCP_PROTOCOL_VERSION:
            raise MCPError(f"Unsupported MCP protocol version: {protocol_version!r}")

        headers = {"MCP-Protocol-Version": protocol_version}
        session_id = response_headers.get("MCP-Session-Id")
        if session_id:
            headers["MCP-Session-Id"] = session_id

        notification, _ = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers,
            None,
        )
        if notification is not None:
            raise MCPError("MCP server returned a response to an initialized notification")
        return headers

    def _request(
        self, method: str, params: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        request_id = 2
        response, _ = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            headers,
            request_id,
        )
        return self._result(response, request_id)

    def _post(
        self, payload: dict[str, Any], headers: dict[str, str], request_id: int | None
    ) -> tuple[dict[str, Any] | None, httpx.Headers]:
        response = self._http.post(
            MCP_ENDPOINT,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
            json=payload,
        )
        response.raise_for_status()
        if response.status_code == 202 or not response.content:
            return None, response.headers

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type == "application/json":
            try:
                body = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise MCPError("MCP server returned invalid JSON") from error
            if not isinstance(body, dict):
                raise MCPError("MCP server returned a non-object JSON response")
            return self._matching_response(body, request_id), response.headers

        if content_type == "text/event-stream":
            for event_data in self._event_data(response.text):
                try:
                    body = json.loads(event_data)
                except json.JSONDecodeError as error:
                    raise MCPError("MCP server returned invalid JSON in its event stream") from error
                if isinstance(body, dict) and body.get("id") == request_id:
                    return body, response.headers
            if request_id is None:
                return None, response.headers
            raise MCPError("MCP event stream did not contain a response for the request")

        raise MCPError(f"Unsupported MCP response content type: {content_type or 'missing'}")

    @staticmethod
    def _event_data(event_stream: str) -> list[str]:
        events: list[str] = []
        data_lines: list[str] = []
        for line in event_stream.splitlines():
            if not line:
                if data_lines:
                    events.append("\n".join(data_lines))
                    data_lines.clear()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            events.append("\n".join(data_lines))
        return events

    @staticmethod
    def _matching_response(
        response: dict[str, Any], request_id: int | None
    ) -> dict[str, Any] | None:
        if request_id is None:
            raise MCPError("MCP server returned a response to a notification")
        if response.get("id") != request_id:
            raise MCPError("MCP server response ID did not match the request")
        return response

    @staticmethod
    def _result(response: dict[str, Any] | None, request_id: int) -> dict[str, Any]:
        if response is None:
            raise MCPError("MCP server returned no response")
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message = error.get("message", "unspecified error")
                raise MCPError(f"MCP JSON-RPC error {code}: {message}")
            raise MCPError("MCP server returned an invalid JSON-RPC error")
        if response.get("id") != request_id:
            raise MCPError("MCP server response ID did not match the request")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP server response did not contain an object result")
        return result


class Data360Agent:
    def query(
        self, message: str, user_id: str = "", authorization: str = "", app_token: str = ""
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        try:
            command = json.loads(message)
        except json.JSONDecodeError as error:
            if message.lstrip().startswith(("{", "[")):
                raise ValueError("Malformed JSON command") from error
            model = GatewayModel.from_env()
            if not authorization or not app_token:
                raise ValueError(
                    "Conversational mode requires per-request authorization and app_token "
                    "from the platform gateway, not credentials in the message."
                )
            with Data360MCPClient() as client:
                return converse(message, client, model, authorization, app_token)
        if not isinstance(command, dict):
            raise ValueError("The request must be a JSON object")

        action = command.get("action")
        if action == "list_tools":
            with Data360MCPClient() as client:
                return {"action": action, "result": client.list_tools()}
        if action == "call_tool":
            name = command.get("name")
            arguments = command.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise ValueError("call_tool requires a non-empty string 'name'")
            if not isinstance(arguments, dict):
                raise ValueError("'arguments' must be a JSON object")
            with Data360MCPClient() as client:
                return {
                    "action": action,
                    "name": name,
                    "result": client.call_tool(name, arguments),
                }
        raise ValueError("Unsupported action; use 'list_tools' or 'call_tool'")


root_agent = Data360Agent()
