"""Bind chat/voice identity to the configured Cognito WebSocket principal.

Verify both the first-message access token and forwarded OAuth transport token
against the configured pool/client. GetUser then checks revocation and supplies
current property attributes; it cannot substitute for issuer/client validation.
Public JWKS are cached and refreshed on rotation. Tokens are never logged.
"""

import asyncio
import os
from functools import lru_cache
from typing import Any, Dict, List

import boto3
import jwt
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


@lru_cache(maxsize=2)
def _jwks_client(issuer: str) -> jwt.PyJWKClient:
    """Use a fixed trusted endpoint, never a URL taken from an unverified JWT."""
    return jwt.PyJWKClient(
        issuer + "/.well-known/jwks.json",
        cache_keys=True,
        max_cached_keys=4,
        lifespan=300,
        timeout=5,
    )


def _verified_claims(token: str) -> Dict[str, Any]:
    """Require Cognito's RS256 signature, issuer, client, use and expiry."""
    pool = os.environ.get("COGNITO_USER_POOL_ID", "")
    client = os.environ.get("COGNITO_CLIENT_ID", "")
    if not pool.startswith(AWS_REGION + "_") or not client:
        raise IdentityError("Identity validation is not configured", "configuration_error")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise IdentityError("Invalid access token", "token_invalid")
    issuer = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{pool}"
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise IdentityError("Invalid token signing algorithm or key", "token_invalid")
        if not header["kid"] or len(header["kid"]) > 256:
            raise IdentityError("Invalid token signing key", "token_invalid")
        key = _jwks_client(issuer).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={
                # Cognito access tokens identify the app using client_id.
                "verify_aud": False,
                "require": ["exp", "iat", "sub", "iss", "token_use", "client_id"],
            },
        )
    except jwt.PyJWTError as exc:
        raise IdentityError("Access token verification failed", "token_invalid") from exc
    if (
        claims.get("token_use") != "access"
        or claims.get("client_id") != client
        or not isinstance(claims.get("sub"), str)
        or not claims["sub"]
    ):
        raise IdentityError("Access token claims are not authorized", "token_invalid")
    return claims


def _validate_session_tokens(
    access_token: str, transport_authorization: str
) -> Dict[str, Any]:
    """Reject substituting another user's identity into an authenticated socket."""
    if not isinstance(transport_authorization, str):
        raise IdentityError("Authenticated transport identity is required", "transport_missing")
    scheme, _, transport_token = transport_authorization.partition(" ")
    if scheme.lower() != "bearer" or not transport_token:
        raise IdentityError("Authenticated transport identity is required", "transport_missing")
    transport_claims = _verified_claims(transport_token)
    claims = _verified_claims(access_token)
    if transport_claims["sub"] != claims["sub"]:
        raise IdentityError("Identity does not match the connected user", "identity_mismatch")
    return claims


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


async def resolve_identity(
    access_token: str, transport_authorization: str = ""
) -> Dict[str, str]:
    """Validate a Cognito Access Token and extract property-scoped claims.

    Validate JWTs before GetUser, then bind its returned subject to the verified
    subject. Blocking crypto/JWKS and SDK work runs off the WebSocket event loop.

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

    claims = await asyncio.to_thread(
        _validate_session_tokens, access_token, transport_authorization
    )
    try:
        # Call Cognito GetUser to validate the token and retrieve user attributes.
        # This single API call handles token signature verification, expiration
        # checking, and revocation status - no local crypto needed.
        response = await asyncio.to_thread(
            _cognito_client.get_user, AccessToken=access_token
        )
    except _cognito_client.exceptions.NotAuthorizedException as exc:
        # Token is invalid, expired, or has been revoked (user signed out).
        logger.warning(
            "Access token authorization failed",
            extra={"error_type": "NotAuthorizedException"},
        )
        raise IdentityError(
            "Access token is invalid or expired",
            reason="token_invalid",
        ) from exc
    except _cognito_client.exceptions.UserNotFoundException as exc:
        # User associated with the token no longer exists in the User Pool.
        logger.warning(
            "User not found for the provided access token",
            extra={"error_type": "UserNotFoundException"},
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
            extra={"error_code": error_code},
        )
        raise IdentityError(
            f"Identity resolution failed due to service error: {error_code}",
            reason="service_error",
        ) from exc

    # Extract user attributes from the GetUser response.
    # UserAttributes is a list of {"Name": "...", "Value": "..."} dicts.
    user_attributes: List[Dict[str, str]] = response.get("UserAttributes", [])
    if _extract_attribute(user_attributes, "sub") != claims["sub"]:
        raise IdentityError("User response does not match token subject", "identity_mismatch")

    # Extract custom:propertyId - required for property-scoped DynamoDB access.
    property_id = _extract_attribute(user_attributes, "custom:propertyId")

    # Extract custom:gmAlias - required for settings lookup and logging context.
    gm_alias = _extract_attribute(user_attributes, "custom:gmAlias")

    logger.info(
        "Identity resolved successfully",
        extra={"property_id": property_id, "gm_alias": gm_alias},
    )

    return {
        "property_id": property_id,
        "gm_alias": gm_alias,
    }
