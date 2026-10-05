"""Identity resolution module for the StayOS Chat Agent on AgentCore.

Validates Cognito Access Tokens by calling the Cognito GetUser API and extracts
custom:propertyId and custom:gmAlias for property-scoped data access. This is
an adapted copy of the voice agent's identity_resolver.py - the identity
resolution logic is not voice-specific, so it is reused as-is with only the
logger service name changed.

Role in project: Called by server.py after the first WebSocket message is
received. The Access Token (sent by the browser as the first message) is
validated via Cognito GetUser, which simultaneously verifies token validity
and retrieves the user's custom attributes. The extracted propertyId is
injected into every Gateway tool call made during the chat session.

Why GetUser instead of local JWT validation:
    - AgentCore already authenticates the caller via SigV4 at the platform level
    - GetUser respects token revocation (sign-out) immediately
    - No JWKS caching, key rotation, or crypto dependencies needed
    - Single boto3 call vs multi-step JWKS fetch + RS256 verification

Environment variables:
    AWS_DEFAULT_REGION: AWS region for the Cognito User Pool (default: us-east-1)
"""

import os
from typing import Any, Dict, List

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from aws_lambda_powertools import Logger

# Module-level logger for structured logging via Powertools.
# Service name matches the agent identity for log correlation in CloudWatch.
logger: Logger = Logger(service="stayos-chat-agent")

# Read region from environment (injected by AgentCore runtime configuration).
AWS_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Retry configuration using standard mode for automatic exponential backoff.
# Standard mode handles throttling and transient errors with up to 3 attempts.
_RETRY_CONFIG: Config = Config(
    region_name=AWS_REGION,
    retries={"mode": "standard"},
)

# Module-level Cognito Identity Provider client for connection reuse across
# WebSocket sessions. Initialized once at import time per PYQUALITY-06.
_cognito_client: Any = boto3.client(
    "cognito-idp",
    config=_RETRY_CONFIG,
)


class IdentityError(Exception):
    """Raised when identity resolution fails.

    Covers scenarios where the Access Token is invalid, expired, revoked,
    or is missing required custom claims (custom:propertyId, custom:gmAlias).

    Attributes:
        message: Human-readable description of the identity failure.
        reason: Machine-readable error category for structured logging.
    """

    def __init__(self, message: str, reason: str = "unknown") -> None:
        """Initialize IdentityError.

        Args:
            message: Human-readable description of the failure.
            reason: Machine-readable category (e.g., "token_invalid",
                "token_expired", "missing_claim").
        """
        super().__init__(message)
        self.message = message
        self.reason = reason


def _extract_attribute(
    user_attributes: List[Dict[str, str]], attribute_name: str
) -> str:
    """Extract a specific attribute value from the Cognito UserAttributes list.

    Cognito GetUser returns attributes as a list of {"Name": ..., "Value": ...}
    dicts. This helper searches for the specified attribute name and returns
    its value.

    Args:
        user_attributes: List of attribute dicts from the GetUser response.
            Each dict has "Name" (str) and "Value" (str) keys.
        attribute_name: The full attribute name to search for
            (e.g., "custom:propertyId").

    Returns:
        The attribute value as a string.

    Raises:
        IdentityError: If the attribute is not found or has an empty value.
    """
    for attribute in user_attributes:
        if attribute.get("Name") == attribute_name:
            value = attribute.get("Value", "")
            if value:
                return value
            # Attribute exists but has an empty value
            logger.warning(
                "Identity attribute has empty value",
                extra={"attribute_name": attribute_name},
            )
            raise IdentityError(
                f"Required attribute '{attribute_name}' is empty",
                reason="missing_claim",
            )

    # Attribute not present in the response at all
    logger.warning(
        "Required identity attribute not found in user attributes",
        extra={"attribute_name": attribute_name},
    )
    raise IdentityError(
        f"Required attribute '{attribute_name}' is missing",
        reason="missing_claim",
    )


async def resolve_identity(access_token: str) -> Dict[str, str]:
    """Validate a Cognito Access Token and extract property-scoped claims.

    Calls the Cognito GetUser API with the Access Token. If the call succeeds,
    the token is valid and the user's custom attributes are returned. If it
    fails (expired, revoked, malformed), an IdentityError is raised.

    This function is async to match the server's async WebSocket handler pattern,
    though the underlying boto3 call is synchronous. The async wrapper enables
    future migration to aioboto3 if needed without changing the caller interface.

    Args:
        access_token: The Cognito Access Token received as the first WebSocket
            message from the browser. Must not be empty.

    Returns:
        Dictionary containing the validated identity claims:
            - property_id: The GM's property ID (from custom:propertyId)
            - gm_alias: The GM's alias (from custom:gmAlias)

    Raises:
        IdentityError: If the token is empty, invalid, expired, revoked,
            or if required custom claims are missing.
    """
    if not access_token:
        logger.warning("Empty access token received for identity resolution")
        raise IdentityError(
            "Access token is required for identity resolution",
            reason="token_empty",
        )

    try:
        # Call Cognito GetUser to validate the token and retrieve user attributes.
        # This single API call handles token signature verification, expiration
        # checking, and revocation status - no local crypto needed.
        response = _cognito_client.get_user(AccessToken=access_token)
    except _cognito_client.exceptions.NotAuthorizedException as exc:
        # Token is invalid, expired, or has been revoked (user signed out).
        logger.warning(
            "Access token authorization failed",
            extra={"error_type": "NotAuthorizedException", "error": str(exc)},
        )
        raise IdentityError(
            "Access token is invalid or expired",
            reason="token_invalid",
        ) from exc
    except _cognito_client.exceptions.UserNotFoundException as exc:
        # User associated with the token no longer exists in the User Pool.
        logger.warning(
            "User not found for the provided access token",
            extra={"error_type": "UserNotFoundException", "error": str(exc)},
        )
        raise IdentityError(
            "User associated with the token was not found",
            reason="user_not_found",
        ) from exc
    except ClientError as exc:
        # Top-level catch for unexpected Cognito errors (throttling, service errors).
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "Unexpected Cognito API error during identity resolution",
            extra={"error_code": error_code, "error": str(exc)},
        )
        raise IdentityError(
            f"Identity resolution failed due to service error: {error_code}",
            reason="service_error",
        ) from exc

    # Extract user attributes from the GetUser response.
    # UserAttributes is a list of {"Name": "...", "Value": "..."} dicts.
    user_attributes: List[Dict[str, str]] = response.get("UserAttributes", [])

    # Extract custom:propertyId - required for property-scoped tool invocations.
    property_id = _extract_attribute(user_attributes, "custom:propertyId")

    # Extract custom:gmAlias - required for logging context and personalization.
    gm_alias = _extract_attribute(user_attributes, "custom:gmAlias")

    logger.info(
        "Identity resolved successfully",
        extra={"property_id": property_id, "gm_alias": gm_alias},
    )

    return {
        "property_id": property_id,
        "gm_alias": gm_alias,
    }
