import re
from urllib.parse import urlparse

import httpx


class PlatformClient:
    def __init__(self, gateway_url: str, agent_id: str, authorization: str, app_token: str):
        parsed = urlparse(gateway_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("Gateway must be a fixed HTTPS origin")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", agent_id):
            raise ValueError("Invalid agent ID")
        self.origin = gateway_url.rstrip("/")
        self.agent_id = agent_id
        self.headers = {"Authorization": authorization, "X-App-Token": app_token}

    def generate(self, alias: str, message: str) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", alias):
            raise ValueError("Invalid model alias")
        with httpx.Client(timeout=100, follow_redirects=False, trust_env=False) as client:
            response = client.post(
                f"{self.origin}/v1/agents/{self.agent_id}/models/{alias}/generate",
                headers=self.headers, json={"message": message},
            )
            response.raise_for_status()
            return response.json()

    def read_document(self, connector_id: str, object_name: str) -> bytes:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", connector_id):
            raise ValueError("Invalid connector ID")
        with httpx.Client(timeout=40, follow_redirects=False, trust_env=False) as client:
            response = client.get(
                f"{self.origin}/v1/agents/{self.agent_id}/connectors/{connector_id}/object",
                headers=self.headers, params={"name": object_name},
            )
            response.raise_for_status()
            return response.content


class GovernedAgent:
    def __init__(self, gateway_url: str, agent_id: str, model_alias: str):
        self.gateway_url = gateway_url
        self.agent_id = agent_id
        self.model_alias = model_alias

    def query(self, user_id: str, message: str, authorization: str, app_token: str) -> dict:
        client = PlatformClient(self.gateway_url, self.agent_id, authorization, app_token)
        return client.generate(self.model_alias, message)