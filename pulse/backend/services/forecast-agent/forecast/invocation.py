"""Invocation gating for the PULSE Forecasting Agent (Req 1.4, 1.5).

This module is the ``propertyId`` gate the thin agent entrypoint calls before
any operational read. A per-property forecasting run is bound to exactly one
property (the data-isolation boundary, Req 6.1), so the entrypoint must resolve
and validate that single ``propertyId`` from the session input before the Signal
Reader touches the Gateway. ``validate_invocation`` performs that check and
either returns the validated id or raises :class:`InvalidInvocationError` — and
it does so BEFORE any read, so an invalid invocation never triggers a Gateway
tool call or a ``pulse-alerts`` write and leaves any previously generated
forecast unchanged (Req 1.5, design: Property 2).

The module is deliberately dependency-light: it imports only the standard
library so the gate can be unit/property tested on any host without an
AgentCore Runtime session (PYQUALITY-05). It uses a module-level ``logging``
logger (PYQUALITY-03) and defines a custom domain exception rather than a
generic ``ValueError`` (PYQUALITY-02). All names are snake_case (NAMING-03).

Design reference: design.md -> Components -> ``invocation.validate_invocation``;
Error Handling -> custom exceptions.

Requirements: 1.4, 1.5.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

# Module-level named logger (PYQUALITY-03). A library-style named logger lets the
# host application (the agent entrypoint / AgentCore Runtime) own handler and
# level configuration while this module simply emits structured records.
logger = logging.getLogger(__name__)

# The session-input key carrying the single property to bind this run to. The
# scope always comes from the invocation context, never from the model.
PROPERTY_ID_KEY = "propertyId"

# Rejection reasons recorded on InvalidInvocationError and logged on failure.
# These exact tokens are the ones the design and tasks reference (Req 1.5).
REASON_MISSING_PROPERTY_ID = "missing-property-id"
REASON_UNKNOWN_PROPERTY_ID = "unknown-property-id"


class InvalidInvocationError(Exception):
    """Raised when an invocation's ``propertyId`` is missing, empty, or unknown.

    A domain-specific exception (PYQUALITY-02) that carries the offending
    received value and a machine-readable ``reason`` so the agent entrypoint can
    log the invalid invocation and terminate the run before any read (Req 1.5).
    Raised strictly before any Gateway tool call or ``pulse-alerts`` write, so a
    previously generated forecast is left unchanged (design: Property 2).

    Attributes:
        received_value: The raw ``propertyId`` value read from the session input
            (may be ``None``, an empty string, or an unknown id). Never a secret.
        reason: The rejection reason, one of :data:`REASON_MISSING_PROPERTY_ID`
            (absent/empty) or :data:`REASON_UNKNOWN_PROPERTY_ID` (not enabled).
    """

    def __init__(self, received_value: Any, reason: str) -> None:
        """Initialize the error with the received value and rejection reason.

        Args:
            received_value: The raw ``propertyId`` value that failed validation.
            reason: The rejection reason token (missing vs. unknown property id).
        """
        # Keep the structured fields on the instance so the entrypoint can log
        # them, and build a human-readable message for the base Exception.
        self.received_value = received_value
        self.reason = reason
        super().__init__(
            f"Invalid invocation ({reason}): received propertyId={received_value!r}"
        )


def validate_invocation(
    session_input: Mapping[str, Any], enabled_ids: frozenset[str]
) -> str:
    """Return the validated ``propertyId``, or raise ``InvalidInvocationError``.

    The invocation gate for one per-property forecasting run (Req 1.4, 1.5). It
    parses ``propertyId`` from the session input and validates it against the set
    of enabled properties, raising BEFORE any operational read so no Gateway tool
    is called and no ``pulse-alerts`` write occurs for an invalid invocation
    (design: Property 2). On success it returns the single ``propertyId`` the run
    binds to (the data-isolation boundary, Req 6.1).

    Validation, in order:

    1. **Missing/empty (Req 1.5):** if ``propertyId`` is absent, ``None``, not a
       string, or blank after stripping whitespace, raise with reason
       :data:`REASON_MISSING_PROPERTY_ID`.
    2. **Unknown (Req 1.5):** if the (stripped) id is not in ``enabled_ids``,
       raise with reason :data:`REASON_UNKNOWN_PROPERTY_ID`.

    On either failure the received value and reason are logged as structured
    context (PYQUALITY-03) before the exception propagates; no secrets are
    logged (a ``propertyId`` is a non-sensitive resource identifier).

    Args:
        session_input: The raw AgentCore session input mapping. The single bound
            property is read from the ``propertyId`` key
            (:data:`PROPERTY_ID_KEY`).
        enabled_ids: The set of property ids this agent is enabled to run for.
            Only an id present here is accepted.

    Returns:
        The validated, whitespace-stripped ``propertyId`` string.

    Raises:
        InvalidInvocationError: If ``propertyId`` is missing/empty
            (:data:`REASON_MISSING_PROPERTY_ID`) or not in ``enabled_ids``
            (:data:`REASON_UNKNOWN_PROPERTY_ID`). Raised before any read.
    """
    raw_property_id = session_input.get(PROPERTY_ID_KEY)

    # Step 1 - missing/empty: absent, wrong type, or blank after stripping.
    # Guard the type before .strip() so a non-string value is rejected, not raised
    # as an AttributeError.
    property_id = raw_property_id.strip() if isinstance(raw_property_id, str) else ""
    if not property_id:
        logger.warning(
            "Rejecting invocation with missing or empty propertyId",
            extra={
                "reason": REASON_MISSING_PROPERTY_ID,
                "received_property_id": raw_property_id,
            },
        )
        raise InvalidInvocationError(raw_property_id, REASON_MISSING_PROPERTY_ID)

    # Step 2 - unknown: present but not an enabled property id.
    if property_id not in enabled_ids:
        logger.warning(
            "Rejecting invocation with unknown propertyId",
            extra={
                "reason": REASON_UNKNOWN_PROPERTY_ID,
                "received_property_id": property_id,
            },
        )
        raise InvalidInvocationError(property_id, REASON_UNKNOWN_PROPERTY_ID)

    # Enabled id: this run binds to exactly this single property (Req 6.1).
    return property_id


__all__ = [
    "InvalidInvocationError",
    "validate_invocation",
    "PROPERTY_ID_KEY",
    "REASON_MISSING_PROPERTY_ID",
    "REASON_UNKNOWN_PROPERTY_ID",
]
