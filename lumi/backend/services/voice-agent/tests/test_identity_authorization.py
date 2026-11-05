"""Exercise real RSA/JWT verification for both separately packaged agents."""

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(params=["voice-agent", "chat-agent"])
def resolver(request, monkeypatch):
    service = Path(__file__).resolve().parents[2] / request.param
    spec = importlib.util.spec_from_file_location(
        "authorization_" + request.param.replace("-", "_"),
        service / "identity_resolver.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "test-client")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(module, "_jwks_client", lambda issuer: jwks)
    return module, key, jwks


def token(key, **changes):
    claims = {
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
        "client_id": "test-client", "token_use": "access", "sub": "user-1",
        "iat": int(time.time()) - 1, "exp": int(time.time()) + 300,
    }
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def test_same_subject_refreshed_token_is_allowed(resolver):
    module, key, _ = resolver
    first = token(key)
    refreshed = token(key, exp=int(time.time()) + 600)
    assert module._validate_session_tokens(refreshed, "Bearer " + first)["sub"] == "user-1"


@pytest.mark.parametrize("changes", [
    {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OtherPool"},
    {"client_id": "another-client"}, {"token_use": "id"},
    {"exp": int(time.time()) - 600}, {"iat": int(time.time()) + 600},
    {"sub": ""}, {"sub": 123},
])
def test_reject_untrusted_claims(resolver, changes):
    module, key, _ = resolver
    with pytest.raises(module.IdentityError):
        module._verified_claims(token(key, **changes))


@pytest.mark.parametrize("claim", ["exp", "iat", "sub", "iss", "token_use", "client_id"])
def test_reject_missing_claim(resolver, claim):
    module, key, _ = resolver
    decoded = jwt.decode(token(key), options={"verify_signature": False})
    del decoded[claim]
    encoded = jwt.encode(decoded, key, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(module.IdentityError):
        module._verified_claims(encoded)


def test_reject_forged_signature(resolver):
    module, _, _ = resolver
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(module.IdentityError):
        module._verified_claims(token(other))


def test_reject_other_user_on_authenticated_socket(resolver):
    module, key, _ = resolver
    with pytest.raises(module.IdentityError, match="connected user"):
        module._validate_session_tokens(token(key, sub="user-2"), "Bearer " + token(key))


@pytest.mark.parametrize("authorization", ["", "Basic abc", None])
def test_reject_unbound_transport(resolver, authorization):
    module, key, _ = resolver
    with pytest.raises(module.IdentityError):
        module._validate_session_tokens(token(key), authorization)


def test_reject_unsigned_or_symmetric_token(resolver):
    module, _, _ = resolver
    encoded = jwt.encode({"sub": "user-1"}, "test-symmetric-key", algorithm="HS256")
    with pytest.raises(module.IdentityError):
        module._verified_claims(encoded)


def test_jwks_failure_is_closed(resolver):
    module, key, jwks = resolver
    jwks.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("Unavailable")
    with pytest.raises(module.IdentityError):
        module._verified_claims(token(key))


@pytest.mark.asyncio
async def test_get_user_cannot_replace_verified_subject(resolver, monkeypatch):
    module, key, _ = resolver
    cognito = MagicMock()
    cognito.get_user.return_value = {"UserAttributes": [{"Name": "sub", "Value": "user-2"}]}
    monkeypatch.setattr(module, "_cognito_client", cognito)
    with pytest.raises(module.IdentityError, match="subject"):
        await module.resolve_identity(token(key), "Bearer " + token(key))


@pytest.mark.asyncio
async def test_substitution_rejected_before_get_user(resolver, monkeypatch):
    module, key, _ = resolver
    cognito = MagicMock()
    monkeypatch.setattr(module, "_cognito_client", cognito)
    with pytest.raises(module.IdentityError):
        await module.resolve_identity(token(key, sub="user-2"), "Bearer " + token(key))
    cognito.get_user.assert_not_called()
