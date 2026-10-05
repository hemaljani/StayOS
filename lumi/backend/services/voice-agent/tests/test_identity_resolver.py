"""Unit tests for the identity resolution module (identity_resolver.py).

Tests resolve_identity behavior for valid Access Tokens, empty tokens, invalid
tokens (NotAuthorizedException), user-not-found scenarios, and missing custom
claims (custom:propertyId, custom:gmAlias). Uses unittest.mock to mock the
boto3 cognito-idp client and its typed exceptions.

Validates: Requirements 2.6, 2.7
"""

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# Add the voice-agent service directory to the path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Test fixtures: Mock Cognito client and exception classes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_cognito_client() -> MagicMock:
    """Mock the module-level _cognito_client in identity_resolver.

    Provides a mock cognito-idp client with typed exceptions
    (NotAuthorizedException, UserNotFoundException) that behave like
    real botocore exceptions for isinstance checks in the production code.

    Yields:
        MagicMock representing the cognito-idp client.
    """
    with patch("identity_resolver._cognito_client") as mock_client:
        # Create exception classes that behave like boto3 typed exceptions.
        # These are classes (not instances) attached to client.exceptions.
        mock_client.exceptions.NotAuthorizedException = type(
            "NotAuthorizedException", (Exception,), {}
        )
        mock_client.exceptions.UserNotFoundException = type(
            "UserNotFoundException", (Exception,), {}
        )
        yield mock_client


def _build_user_attributes(
    property_id: str = "PROP-BEACH-42",
    gm_alias: str = "gm-carlos",
    include_property_id: bool = True,
    include_gm_alias: bool = True,
) -> List[Dict[str, str]]:
    """Build a UserAttributes list matching the Cognito GetUser response format.

    Args:
        property_id: Value for the custom:propertyId attribute.
        gm_alias: Value for the custom:gmAlias attribute.
        include_property_id: Whether to include custom:propertyId in the list.
        include_gm_alias: Whether to include custom:gmAlias in the list.

    Returns:
        List of attribute dicts in the Cognito GetUser response format.
    """
    attributes: List[Dict[str, str]] = [
        {"Name": "sub", "Value": "user-uuid-12345"},
        {"Name": "email", "Value": "carlos@beach-resort.com"},
    ]

    if include_property_id:
        attributes.append({"Name": "custom:propertyId", "Value": property_id})

    if include_gm_alias:
        attributes.append({"Name": "custom:gmAlias", "Value": gm_alias})

    return attributes


# ---------------------------------------------------------------------------
# Happy path: Valid Access Token returns property_id and gm_alias
# ---------------------------------------------------------------------------


class TestResolveIdentityHappyPath:
    """Tests for successful identity resolution from Cognito GetUser."""

    @pytest.mark.asyncio
    async def test_resolve_identity_extracts_claims(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """Valid Access Token with both custom claims returns property_id and gm_alias.

        Validates: Requirement 2.6 — extract custom:propertyId and custom:gmAlias
        from the verified identity via Cognito GetUser API.
        """
        from identity_resolver import resolve_identity

        # Configure GetUser to return valid user attributes
        mock_cognito_client.get_user.return_value = {
            "Username": "user-uuid-12345",
            "UserAttributes": _build_user_attributes(
                property_id="PROP-BEACH-42",
                gm_alias="gm-carlos",
            ),
        }

        result = await resolve_identity("valid-access-token-abc123")

        assert result["property_id"] == "PROP-BEACH-42"
        assert result["gm_alias"] == "gm-carlos"

    @pytest.mark.asyncio
    async def test_resolve_identity_calls_get_user_with_token(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """Verify GetUser is called with the provided Access Token.

        Ensures the token passed to resolve_identity is forwarded to Cognito
        as the AccessToken parameter (not modified or truncated).
        """
        from identity_resolver import resolve_identity

        mock_cognito_client.get_user.return_value = {
            "Username": "user-uuid-12345",
            "UserAttributes": _build_user_attributes(),
        }

        await resolve_identity("my-specific-access-token")

        mock_cognito_client.get_user.assert_called_once_with(
            AccessToken="my-specific-access-token"
        )


# ---------------------------------------------------------------------------
# Empty token: Raises IdentityError before calling Cognito
# ---------------------------------------------------------------------------


class TestResolveIdentityEmptyToken:
    """Tests for empty or missing Access Token handling."""

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_empty_string(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """Empty string token raises IdentityError with reason 'token_empty'.

        Validates: Requirement 2.7 — IF identity verification fails, close
        the WebSocket with an error. Empty token is caught before the API call.
        """
        from identity_resolver import IdentityError, resolve_identity

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("")

        assert exc_info.value.reason == "token_empty"
        # GetUser should NOT be called for empty tokens
        mock_cognito_client.get_user.assert_not_called()


# ---------------------------------------------------------------------------
# Invalid token: GetUser raises NotAuthorizedException
# ---------------------------------------------------------------------------


class TestResolveIdentityInvalidToken:
    """Tests for invalid, expired, or revoked Access Tokens."""

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_invalid_token(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """GetUser raising NotAuthorizedException results in IdentityError with reason 'token_invalid'.

        Covers: expired tokens, revoked tokens (user signed out), and malformed tokens.
        Validates: Requirement 2.7 — IF identity verification fails, close
        the WebSocket with an error message.
        """
        from identity_resolver import IdentityError, resolve_identity

        # Simulate Cognito rejecting the token (expired, revoked, or invalid)
        mock_cognito_client.get_user.side_effect = (
            mock_cognito_client.exceptions.NotAuthorizedException(
                "Access Token has expired"
            )
        )

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("expired-or-invalid-token")

        assert exc_info.value.reason == "token_invalid"


# ---------------------------------------------------------------------------
# User not found: GetUser raises UserNotFoundException
# ---------------------------------------------------------------------------


class TestResolveIdentityUserNotFound:
    """Tests for tokens associated with deleted users."""

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_user_not_found(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """GetUser raising UserNotFoundException results in IdentityError with reason 'user_not_found'.

        Covers the scenario where the user associated with the Access Token
        has been deleted from the Cognito User Pool.
        Validates: Requirement 2.7
        """
        from identity_resolver import IdentityError, resolve_identity

        # Simulate user deleted from User Pool after token was issued
        mock_cognito_client.get_user.side_effect = (
            mock_cognito_client.exceptions.UserNotFoundException(
                "User does not exist"
            )
        )

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("token-for-deleted-user")

        assert exc_info.value.reason == "user_not_found"


# ---------------------------------------------------------------------------
# Missing claims: custom:propertyId or custom:gmAlias not in UserAttributes
# ---------------------------------------------------------------------------


class TestResolveIdentityMissingClaims:
    """Tests for missing or empty required custom attributes."""

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_missing_property_id(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """GetUser response without custom:propertyId raises IdentityError with reason 'missing_claim'.

        A user who authenticated but lacks the custom:propertyId attribute
        cannot establish a property-scoped session.
        Validates: Requirement 2.6, 2.7
        """
        from identity_resolver import IdentityError, resolve_identity

        # UserAttributes list has gmAlias but no propertyId
        mock_cognito_client.get_user.return_value = {
            "Username": "user-uuid-12345",
            "UserAttributes": _build_user_attributes(
                include_property_id=False,
                include_gm_alias=True,
            ),
        }

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("valid-token-missing-property")

        assert exc_info.value.reason == "missing_claim"
        assert "custom:propertyId" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_missing_gm_alias(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """GetUser response without custom:gmAlias raises IdentityError with reason 'missing_claim'.

        A user with propertyId but no gmAlias cannot load language preferences
        or be identified in logs.
        Validates: Requirement 2.6, 2.7
        """
        from identity_resolver import IdentityError, resolve_identity

        # UserAttributes list has propertyId but no gmAlias
        mock_cognito_client.get_user.return_value = {
            "Username": "user-uuid-12345",
            "UserAttributes": _build_user_attributes(
                include_property_id=True,
                include_gm_alias=False,
            ),
        }

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("valid-token-missing-alias")

        assert exc_info.value.reason == "missing_claim"
        assert "custom:gmAlias" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_resolve_identity_rejects_empty_property_id(
        self, mock_cognito_client: MagicMock
    ) -> None:
        """GetUser response with empty custom:propertyId raises IdentityError.

        An empty string attribute value is treated as missing since it cannot
        be used to scope DynamoDB queries.
        Validates: Requirement 2.6, 2.7
        """
        from identity_resolver import IdentityError, resolve_identity

        # PropertyId exists in attributes but has an empty value
        mock_cognito_client.get_user.return_value = {
            "Username": "user-uuid-12345",
            "UserAttributes": _build_user_attributes(
                property_id="",
                gm_alias="gm-carlos",
            ),
        }

        with pytest.raises(IdentityError) as exc_info:
            await resolve_identity("valid-token-empty-property")

        assert exc_info.value.reason == "missing_claim"
        assert "custom:propertyId" in exc_info.value.message
