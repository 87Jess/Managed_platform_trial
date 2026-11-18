import re
from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession

from managed_agents.auth import Settings
from managed_agents.policy import Agent, Connector, ModelAlias


class BackendUnavailable(Exception):
    pass


class GoogleBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    def session(self) -> AuthorizedSession:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(credentials)

    def invoke(self, agent: Agent, subject: str, message: str,
               authorization: str, app_token: str) -> dict:
        pattern = (
            r"projects/[a-zA-Z0-9-]+/locations/" + re.escape(self.settings.location)
            + r"/reasoningEngines/[0-9]+"
        )
        if not re.fullmatch(pattern, agent.engine_resource):
            raise BackendUnavailable("Agent runtime has not been configured")
        url = (f"https://{self.settings.location}-aiplatform.googleapis.com/v1/"
               f"{agent.engine_resource}:query")
        with self.session() as client:
            response = client.post(url, json={
                "class_method": "query",
                "input": {"user_id": subject, "message": message,
                          "authorization": authorization, "app_token": app_token},
            }, timeout=120)
            response.raise_for_status()
            return response.json().get("output", {})

    def generate(self, model: ModelAlias, message: str) -> dict:
        url = (
            f"https://{self.settings.location}-aiplatform.googleapis.com/v1/projects/"
            f"{self.settings.project}/locations/{self.settings.location}/publishers/google/"
            f"models/{model.model}:generateContent"
        )
        with self.session() as client:
            response = client.post(url, json={
                "contents": [{"role": "user", "parts": [{"text": message}]}],
                "generationConfig": {"maxOutputTokens": model.max_output_tokens},
            }, timeout=90)
            response.raise_for_status()
            result = response.json()
            texts = [part["text"] for candidate in result.get("candidates", [])
                     for part in candidate.get("content", {}).get("parts", []) if "text" in part]
            return {"text": "\n".join(texts), "usage": result.get("usageMetadata", {})}

    def read_object(self, connector: Connector, object_name: str) -> bytes:
        url = (f"https://storage.googleapis.com/storage/v1/b/{quote(connector.bucket, safe='')}/o/"
               f"{quote(object_name, safe='')}?alt=media")
        with self.session() as client, client.get(url, timeout=30, stream=True) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise BackendUnavailable("Object exceeds the 1 MiB read limit")
            return bytes(data)