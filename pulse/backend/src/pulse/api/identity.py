"""Caller identity extraction and property scoping for the PULSE REST API.

Every PULSE API route is authenticated by the API Gateway Cognito authorizer,
which places the verified JWT claims on the invocation event. This module turns
those claims into a typed :class:`CallerIdentity` (the caller's ``gmAlias`` and
the set of properties they are associated with) so that every downstream query
and mutation can be scoped **server-side** to the caller's entitlement
(Requirement 16.6, Property 25). Property scoping is never left to the client.

The extraction is pure (no I/O) and tolerant of the two authorizer payload
shapes PULSE may run behind:

    * **HTTP API JWT authorizer (payload format v2):** claims live at
      ``event.requestContext.authorizer.jwt.claims``.
    * **REST API Cognito authorizer:** claims live at
      ``event.requestContext.authorizer.claims``.

The associated-property set is read from a Cognito custom claim
(``custom:properties`` or ``properties``), whose value may be a JSON array
string (``["A","B"]``) or a comma/space separated list (``"A, B"``). When only
the singular ``custom:propertyId`` (or ``propertyId``) claim is present — as on
LUMI's GM users — it contributes exactly one property id. The caller identity
(``gmAlias``) is read from ``cognito:username`` and falls back through
``username`` / ``email`` / ``sub``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Claim keys that may carry the caller's associated properties (checked in
# order). Cognito namespaces custom attributes as ``custom:<name>``.
#
# StayOS/LUMI provisions its GM users with the SINGULAR ``custom:propertyId``
# (one property per GM). PULSE also accepts a plural ``custom:properties`` (a
# JSON array or delimited list) for multi-property operators. Both are honored:
# the plural claim is a set; the singular claim contributes one property id.
# (See BUG-013: reading only ``custom:properties`` denied every real GM whose
# token carries only ``custom:propertyId``.)
_PROPERTY_CLAIM_KEYS = ("custom:properties", "properties")

# Singular single-property claim (one property id, not a list). LUMI users have
# this today (``custom:propertyId``); ``propertyId`` is a plain fallback.
_SINGULAR_PROPERTY_CLAIM_KEYS = ("custom:propertyId", "propertyId")

# Claim keys that may carry the caller identity, in preference order.
_IDENTITY_CLAIM_KEYS = ("cognito:username", "username", "email", "sub")


@dataclass(frozen=True)
class CallerIdentity:
    """The authenticated caller derived from the request's JWT claims.

    Attributes:
        gm_alias: The caller's identity (their ``gmAlias``); empty string when
            no identity claim is present.
        properties: The immutable set of property ids the caller is associated
            with. Used to scope every alert/rule query server-side.
    """

    gm_alias: str
    properties: frozenset[str]

    def is_associated_with(self, property_id: str) -> bool:
        """Return whether the caller is associated with a property.

        Args:
            property_id: The property to check.

        Returns:
            ``True`` if ``property_id`` is in the caller's associated set.
        """
        return property_id in self.properties


def _extract_claims(event: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the JWT claims mapping from the invocation event.

    Supports both the HTTP API v2 (``authorizer.jwt.claims``) and REST API
    (``authorizer.claims``) shapes, and falls back to the authorizer object
    itself when claims are inlined.

    Args:
        event: The API Gateway invocation event.

    Returns:
        The claims mapping, or an empty dict when no authorizer context is
        present.
    """
    authorizer = (
        event.get("requestContext", {}).get("authorizer", {})
        if isinstance(event.get("requestContext"), Mapping)
        else {}
    )
    if not isinstance(authorizer, Mapping):
        return {}
    jwt_context = authorizer.get("jwt")
    if isinstance(jwt_context, Mapping) and isinstance(
        jwt_context.get("claims"), Mapping
    ):
        return dict(jwt_context["claims"])
    if isinstance(authorizer.get("claims"), Mapping):
        return dict(authorizer["claims"])
    return {}


def parse_properties_claim(raw: Any) -> frozenset[str]:
    """Parse a raw associated-properties claim value into a set of ids.

    Accepts a JSON array string (``'["A","B"]'``), a comma/space separated
    string (``'A, B'``), or an already-parsed list. Blank entries are dropped.

    Args:
        raw: The raw claim value (string, list, or ``None``).

    Returns:
        The immutable set of property ids (empty when nothing parseable).
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set)):
        return frozenset(str(item).strip() for item in raw if str(item).strip())
    text = str(raw).strip()
    if not text:
        return frozenset()
    # A JSON array claim value is the common Cognito encoding for a list.
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return frozenset(
                str(item).strip() for item in parsed if str(item).strip()
            )
    # Otherwise treat it as a comma- or space-separated list.
    separators = "," if "," in text else None
    parts = text.split(separators)
    return frozenset(part.strip() for part in parts if part.strip())


def identity_from_claims(claims: Mapping[str, Any]) -> CallerIdentity:
    """Build a :class:`CallerIdentity` from a claims mapping (pure).

    Args:
        claims: The verified JWT claims.

    Returns:
        The caller identity with its associated-property set.
    """
    gm_alias = ""
    for key in _IDENTITY_CLAIM_KEYS:
        value = claims.get(key)
        if value:
            gm_alias = str(value)
            break

    properties: frozenset[str] = frozenset()
    for key in _PROPERTY_CLAIM_KEYS:
        if key in claims:
            properties = parse_properties_claim(claims[key])
            if properties:
                break

    # Fall back to the singular single-property claim (LUMI users carry
    # ``custom:propertyId``). A non-empty singular value contributes exactly
    # one property id. Union with any plural set already found so a token with
    # both is handled correctly.
    if not properties:
        for key in _SINGULAR_PROPERTY_CLAIM_KEYS:
            value = claims.get(key)
            if value and str(value).strip():
                properties = frozenset({str(value).strip()})
                break

    return CallerIdentity(gm_alias=gm_alias, properties=properties)


def extract_identity(event: Mapping[str, Any]) -> CallerIdentity:
    """Extract the caller identity from an API Gateway invocation event.

    Args:
        event: The API Gateway invocation event (HTTP API v2 or REST shape).

    Returns:
        The :class:`CallerIdentity` derived from the request's JWT claims.
    """
    return identity_from_claims(_extract_claims(event))


__all__ = [
    "CallerIdentity",
    "parse_properties_claim",
    "identity_from_claims",
    "extract_identity",
]
