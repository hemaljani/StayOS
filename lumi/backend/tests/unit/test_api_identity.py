"""Check client binding before routing and prevent authenticated data caching."""

import pytest

from exceptions import InternalError, UnauthorizedError
from lambda_function import _build_response, _validate_api_identity


@pytest.fixture(autouse=True)
def app_client(monkeypatch):
    monkeypatch.setenv("COGNITO_CLIENT_ID", "expected-client")


def test_accept_expected_id_token():
    _validate_api_identity({"requestContext": {"authorizer": {"claims": {
        "aud": "expected-client", "token_use": "id",
    }}}})


@pytest.mark.parametrize("claims", [
    {}, {"aud": "another-client", "token_use": "id"},
    {"client_id": "expected-client", "token_use": "access"},
])
def test_reject_other_or_missing_client(claims):
    with pytest.raises(UnauthorizedError):
        _validate_api_identity({"requestContext": {"authorizer": {"claims": claims}}})


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("COGNITO_CLIENT_ID")
    with pytest.raises(InternalError):
        _validate_api_identity({})


def test_responses_cannot_be_cached():
    response = _build_response(200, {"audioBrief": {}})
    assert response["headers"]["Cache-Control"] == "private, no-store"
