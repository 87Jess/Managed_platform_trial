import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from managed_agents.auth import AuthenticationFailed, EntraVerifier, Settings


@pytest.fixture
def credentials(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_key["kid"] = "current-key"
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [public_key]}), encoding="utf-8")
    settings = Settings(project="example-project", tenant_id="00000000-0000-0000-0000-000000000001",
                        audience="api-audience", app_token_sha256=hashlib.sha256(b"app").hexdigest(),
                        jwks_path=str(path))
    now = int(time.time())
    claims = {"sub": "user-one", "tid": str(settings.tenant_id), "aud": settings.audience,
              "iss": f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0",
              "exp": now + 300, "nbf": now - 1, "iat": now - 1,
              "scp": "Platform.Invoke", "roles": ["research.read"]}
    return EntraVerifier(settings), private_key, claims, path


def signed(private_key, claims, kid="current-key"):
    return "Bearer " + jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def test_real_signature_verification(credentials):
    verifier, private_key, claims, _ = credentials
    principal = verifier.verify(signed(private_key, claims), "app")
    assert principal.subject == "user-one"
    assert principal.roles == {"research.read"}


@pytest.mark.parametrize("field,value", [("aud", "another-api"), ("iss", "https://attacker.test"),
                                         ("tid", "another-tenant"), ("exp", 1),
                                         ("scp", "openid"), ("roles", "research.read")])
def test_untrusted_or_expired_claims_rejected(credentials, field, value):
    verifier, private_key, claims, _ = credentials
    claims[field] = value
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(private_key, claims), "app")


def test_id_token_is_not_an_api_access_token(credentials):
    verifier, private_key, claims, _ = credentials
    del claims["scp"]
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(private_key, claims), "app")


def test_forged_signature_rejected(credentials):
    verifier, _, claims, _ = credentials
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(attacker_key, claims), "app")


def test_unknown_key_and_wrong_app_rejected(credentials):
    verifier, private_key, claims, _ = credentials
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(private_key, claims, kid="unknown"), "app")
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(private_key, claims), "wrong-app")


def test_missing_or_corrupted_key_snapshot_fails_closed(credentials):
    verifier, private_key, claims, path = credentials
    for data in ["not-json", '{"keys": []}']:
        path.write_text(data, encoding="utf-8")
        with pytest.raises(AuthenticationFailed):
            verifier.verify(signed(private_key, claims), "app")
    path.unlink()
    with pytest.raises(AuthenticationFailed):
        verifier.verify(signed(private_key, claims), "app")