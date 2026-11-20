import hashlib
import hmac
import os
from pathlib import Path
from uuid import UUID

import jwt
from pydantic import Field

from managed_agents.policy import Principal, StrictModel


class Settings(StrictModel):
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    location: str = Field(default="us-central1", pattern=r"^[a-z]+-[a-z]+[0-9]+$")
    tenant_id: UUID
    audience: str = Field(min_length=1)
    app_token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_path: str = "registry/agents.json"
    jwks_path: str = "registry/entra-jwks.json"
    required_scope: str = "Platform.Invoke"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            tenant_id=os.environ["ENTRA_TENANT_ID"],
            audience=os.environ["ENTRA_AUDIENCE"],
            app_token_sha256=os.environ["APP_TOKEN_SHA256"],
            registry_path=os.environ.get("REGISTRY_PATH", "registry/agents.json"),
            jwks_path=os.environ.get("ENTRA_JWKS_PATH", "registry/entra-jwks.json"),
        )


class AuthenticationFailed(Exception):
    pass


class EntraVerifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.issuer = f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"

    def verify(self, authorization: str, app_token: str) -> Principal:
        digest = hashlib.sha256(app_token.encode()).hexdigest()
        if not app_token or not hmac.compare_digest(digest, self.settings.app_token_sha256):
            raise AuthenticationFailed("Invalid application token")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or len(token) > 16384:
            raise AuthenticationFailed("A user access token is required")
        try:
            header = jwt.get_unverified_header(token)
            keys = jwt.PyJWKSet.from_json(Path(self.settings.jwks_path).read_text(encoding="utf-8"))
            key = keys[header.get("kid", "")]
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.settings.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "nbf", "sub", "tid", "scp"]},
            )
            if claims["tid"] != str(self.settings.tenant_id):
                raise AuthenticationFailed("Wrong tenant")
            if self.settings.required_scope not in str(claims["scp"]).split():
                raise AuthenticationFailed("Delegated API scope is required")
            roles = claims.get("roles", [])
            if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
                raise AuthenticationFailed("Invalid role claims")
            return Principal(subject=claims["sub"], roles=frozenset(roles))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError) as error:
            raise AuthenticationFailed("Invalid user access token") from error