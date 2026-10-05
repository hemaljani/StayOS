"""PULSE-owned quiesce / un-quiesce seam for the rule engine.

The StayOS Unified Data Orchestrator rewrites the LUMI operational tables in
bulk every day (window re-anchor). Those bulk upserts flow through DynamoDB
Streams into the PULSE rule engine and would otherwise fire an alert storm.
This module is the bounded, reversible suppression PULSE owns and the
orchestrator calls (design "Component 3: PULSE quiesce seam").

Mechanism (chosen: preferred ESM toggle, design Component 3):
    We toggle the rule-engine Lambda's DynamoDB Streams *event-source-mapping*
    (ESM) enabled/disabled around the rewrite window. Disabling the ESM pauses
    the rule-engine Lambda's *consumption* of the stream, so the bulk upserts
    are never evaluated and produce zero alerts (Requirements 5.1, 5.3). It does
    NOT disable the DynamoDB stream itself, so no DynamoDB safety protection is
    turned off (Requirement 8.3): the stream keeps flowing, PULSE just stops
    reading it. Re-enabling the ESM resumes normal evaluation and subsequent
    genuine changes fire again (Requirement 5.4). We chose this over the
    per-property suppression flag fallback because it is estate-wide, requires
    no evaluator changes, and cannot leak partial alerts mid-rewrite.

Boundary ownership:
    PULSE owns the mechanism here; the orchestrator's Quiesce / UnQuiesce step
    Lambdas only call :func:`quiesce_rule_engine` / :func:`unquiesce_rule_engine`
    through this documented seam. The ESM identifiers come from an environment
    variable (PYQUALITY-06 / NAMING); nothing is hardcoded.

Reversibility guarantee (Requirement 5.5):
    Un-quiesce is the orchestrator ``Catch`` target and must never leave PULSE
    permanently suppressed. :func:`unquiesce_rule_engine` retries the enable
    with bounded attempts and, on continued failure, emits a CRITICAL log
    (``logger.critical``) identifying every ESM still disabled so an operator
    can re-enable it manually before the next demo.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from pulse.common.aws import get_client
from pulse.common.errors import PulseError
from pulse.common.logging import get_logger

logger = get_logger("pulse-rule-engine-quiesce")

# Environment variable holding the rule-engine DynamoDB Streams event-source
# mapping UUIDs, comma-separated (one per consumed LUMI table stream). Injected
# by CloudFormation; never hardcoded (PYQUALITY-06 / NAMING).
ENV_ESM_UUIDS = "RULE_ENGINE_ESM_UUIDS"

# Bounded un-quiesce retry policy. Un-quiesce must eventually succeed or shout
# (Requirement 5.5); it retries a small number of times with a short backoff so
# a transient Lambda API blip does not leave PULSE suppressed.
UNQUIESCE_MAX_ATTEMPTS = 4
UNQUIESCE_BACKOFF_SEC = 1.0

# ESM state values reported by the Lambda control-plane API.
_STATE_ENABLED = "Enabled"
_STATE_DISABLED = "Disabled"

# Mechanism tag surfaced in the structured result so the orchestrator step
# summary records which suppression mechanism was used.
MECHANISM = "stream-esm-toggle"


class QuiesceError(PulseError):
    """Raised when the quiesce / un-quiesce mechanism cannot be applied.

    Carries the list of event-source-mapping UUIDs that could not be toggled so
    a handler (or the CRITICAL log on un-quiesce failure) can name exactly which
    mappings are in the wrong state.

    Attributes:
        uuids: The ESM UUIDs that failed to toggle, if known.
    """

    def __init__(self, message: str, uuids: Optional[list[str]] = None) -> None:
        """Initialize the quiesce error.

        Args:
            message: Human-readable description of the failure.
            uuids: The event-source-mapping UUIDs that failed to toggle.
        """
        super().__init__(message)
        self.uuids = uuids or []


class EsmGateway(Protocol):
    """Injectable seam over the Lambda event-source-mapping control plane.

    Abstracting the two ``lambda`` client calls behind a Protocol keeps the
    quiesce logic pure and unit-testable (a fake gateway records toggles) while
    the default production implementation wraps the real boto3 client.
    """

    def set_enabled(self, uuid: str, enabled: bool) -> str:
        """Enable or disable one event-source-mapping.

        Args:
            uuid: The event-source-mapping UUID to toggle.
            enabled: ``True`` to enable consumption, ``False`` to pause it.

        Returns:
            The mapping's reported state after the request (e.g. ``"Disabling"``
            / ``"Enabling"`` / ``"Disabled"`` / ``"Enabled"``).
        """
        ...


@dataclass
class LambdaEsmGateway:
    """Default :class:`EsmGateway` backed by the shared boto3 ``lambda`` client.

    Uses the cached, adaptively-retrying client from :mod:`pulse.common.aws` so
    connections are reused across warm invocations (PYQUALITY-06).
    """

    _client: Any = field(default=None, repr=False)

    def _lambda(self) -> Any:
        """Return the cached boto3 ``lambda`` client, creating it on first use.

        Returns:
            The shared, adaptively-retrying ``lambda`` control-plane client.
        """
        if self._client is None:
            self._client = get_client("lambda")
        return self._client

    def set_enabled(self, uuid: str, enabled: bool) -> str:
        """Toggle one event-source-mapping via ``update_event_source_mapping``.

        Args:
            uuid: The event-source-mapping UUID to toggle.
            enabled: ``True`` to resume consumption, ``False`` to pause it.

        Returns:
            The mapping's reported ``State`` after the update request.

        Raises:
            QuiesceError: If the mapping UUID does not exist, or if the Lambda
                control plane rejects the toggle with a retryable error
                (``ResourceConflictException`` when the mapping is still
                ``Enabling``/``Disabling`` - the common race -,
                ``TooManyRequestsException``, or ``ServiceException``). Mapping
                all of these to ``QuiesceError`` is what lets the caller's
                bounded-retry + CRITICAL-log path engage so PULSE is never left
                silently suppressed (Requirement 5.5, review finding CR-1).
        """
        client = self._lambda()
        # Retryable Lambda control-plane errors. ResourceConflictException is
        # the common one: update_event_source_mapping rejects a toggle while the
        # mapping is still transitioning (Enabling/Disabling), which happens when
        # quiesce and un-quiesce bracket a short window. Throttling
        # (TooManyRequestsException) and transient service faults
        # (ServiceException) are likewise worth retrying rather than escaping.
        retryable_exceptions = (
            client.exceptions.ResourceNotFoundException,
            client.exceptions.ResourceConflictException,
            client.exceptions.TooManyRequestsException,
            client.exceptions.ServiceException,
        )
        try:
            # Enabled=False moves the mapping to Disabled: the rule-engine
            # Lambda stops polling the stream, so bulk upserts are not evaluated.
            response = client.update_event_source_mapping(
                UUID=uuid, Enabled=enabled
            )
        except retryable_exceptions as exc:
            # Map every retryable control-plane failure to QuiesceError so
            # _toggle_all records the UUID as failed and the un-quiesce retry /
            # CRITICAL-log path (and quiesce's fail-loud path) engages instead of
            # this exception escaping unhandled.
            raise QuiesceError(
                f"could not toggle event-source-mapping {uuid!r} "
                f"(enabled={enabled}): {type(exc).__name__}: {exc}",
                uuids=[uuid],
            ) from exc
        return str(response.get("State", "Unknown"))


def _resolve_esm_uuids(raw: Optional[str] = None) -> list[str]:
    """Resolve the rule-engine ESM UUIDs from configuration.

    Args:
        raw: An explicit comma-separated UUID string; when omitted the value is
            read from the :data:`ENV_ESM_UUIDS` environment variable.

    Returns:
        The list of non-empty ESM UUIDs, order preserved.

    Raises:
        QuiesceError: If no UUIDs are configured (the seam cannot operate).
    """
    source = raw if raw is not None else os.environ.get(ENV_ESM_UUIDS, "")
    uuids = [part.strip() for part in source.split(",") if part.strip()]
    if not uuids:
        raise QuiesceError(
            f"no rule-engine ESM UUIDs configured; set {ENV_ESM_UUIDS}"
        )
    return uuids


def _toggle_all(gateway: EsmGateway, uuids: list[str], enabled: bool) -> list[str]:
    """Toggle every mapping, collecting the UUIDs that failed.

    All mappings are attempted even if one fails, so a single bad UUID does not
    leave the remaining mappings in an inconsistent state.

    Args:
        gateway: The ESM gateway to toggle through.
        uuids: The mapping UUIDs to toggle.
        enabled: Target enabled state.

    Returns:
        The UUIDs that failed to toggle (empty when all succeeded).
    """
    failed: list[str] = []
    for uuid in uuids:
        try:
            state = gateway.set_enabled(uuid, enabled)
            logger.info(
                "toggled rule-engine event-source-mapping",
                extra={"uuid": uuid, "enabled": enabled, "state": state},
            )
        except QuiesceError as exc:
            logger.error(
                "failed to toggle rule-engine event-source-mapping",
                extra={"uuid": uuid, "enabled": enabled, "error": exc.message},
            )
            failed.append(uuid)
    return failed


def quiesce_rule_engine(
    gateway: Optional[EsmGateway] = None,
    esm_uuids: Optional[str] = None,
) -> dict[str, Any]:
    """Suppress rule-engine evaluation by disabling its stream mappings.

    Disables every configured rule-engine event-source-mapping so the daily
    bulk roll-forward upserts are not consumed and produce zero alerts
    (Requirements 5.1, 5.3). Bounded and reversible: the orchestrator calls
    :func:`unquiesce_rule_engine` after reconciliation (Requirement 5.2).

    Args:
        gateway: ESM gateway to toggle through; defaults to the boto3-backed
            :class:`LambdaEsmGateway`. Injectable for tests.
        esm_uuids: Explicit comma-separated UUIDs; defaults to the
            :data:`ENV_ESM_UUIDS` environment variable.

    Returns:
        Structured detail: ``quiesced`` (True on full success), ``mechanism``,
        the ``uuids`` targeted, and any ``failed`` UUIDs.

    Raises:
        QuiesceError: If no UUIDs are configured, or if any mapping could not be
            disabled (partial suppression is unsafe, so we fail loudly and the
            orchestrator ``Catch`` will run un-quiesce).
    """
    active_gateway = gateway if gateway is not None else LambdaEsmGateway()
    uuids = _resolve_esm_uuids(esm_uuids)

    logger.info(
        "quiescing PULSE rule engine (disabling stream mappings)",
        extra={"mechanism": MECHANISM, "uuidCount": len(uuids)},
    )

    failed = _toggle_all(active_gateway, uuids, enabled=False)
    if failed:
        # Partial quiesce would let some upserts through: fail so the caller
        # (and the state machine Catch) treats it as an error.
        raise QuiesceError(
            f"failed to disable {len(failed)} rule-engine mapping(s)", uuids=failed
        )

    return {
        "quiesced": True,
        "mechanism": MECHANISM,
        "uuids": uuids,
        "failed": [],
    }


def unquiesce_rule_engine(
    gateway: Optional[EsmGateway] = None,
    esm_uuids: Optional[str] = None,
    max_attempts: int = UNQUIESCE_MAX_ATTEMPTS,
    backoff_sec: float = UNQUIESCE_BACKOFF_SEC,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Resume rule-engine evaluation by re-enabling its stream mappings.

    Re-enables every configured mapping so normal evaluation resumes and
    subsequent genuine changes fire alerts again (Requirement 5.4). This is the
    orchestrator ``Catch`` target and MUST NOT leave PULSE permanently
    suppressed (Requirement 5.5): it retries the enable with bounded attempts
    and, on continued failure, emits a CRITICAL log naming every mapping still
    disabled instead of raising, so the state machine can still complete its
    failure path while an operator is paged to re-enable manually.

    Args:
        gateway: ESM gateway to toggle through; defaults to the boto3-backed
            :class:`LambdaEsmGateway`. Injectable for tests.
        esm_uuids: Explicit comma-separated UUIDs; defaults to the
            :data:`ENV_ESM_UUIDS` environment variable.
        max_attempts: Maximum enable attempts across all mappings.
        backoff_sec: Base seconds to sleep between attempts (linear backoff).
        sleep: Sleep function, injectable so tests do not wait in real time.

    Returns:
        Structured detail: ``quiesced`` (False on full success), ``mechanism``,
        the ``uuids`` targeted, the number of ``attempts`` made, and any
        ``failed`` UUIDs still disabled.
    """
    active_gateway = gateway if gateway is not None else LambdaEsmGateway()
    uuids = _resolve_esm_uuids(esm_uuids)

    logger.info(
        "un-quiescing PULSE rule engine (re-enabling stream mappings)",
        extra={"mechanism": MECHANISM, "uuidCount": len(uuids)},
    )

    remaining = list(uuids)
    attempts = 0
    while remaining and attempts < max_attempts:
        attempts += 1
        remaining = _toggle_all(active_gateway, remaining, enabled=True)
        if remaining:
            logger.warning(
                "un-quiesce incomplete; retrying",
                extra={
                    "attempt": attempts,
                    "maxAttempts": max_attempts,
                    "pending": remaining,
                },
            )
            if attempts < max_attempts:
                sleep(backoff_sec * attempts)

    if remaining:
        # Requirement 5.5: do not raise (the Catch path must complete), but
        # shout so PULSE is not silently left suppressed. A CRITICAL log names
        # every mapping an operator must re-enable before the next demo.
        logger.critical(
            "UN-QUIESCE FAILED after retries; PULSE rule engine may remain "
            "suppressed. Manually re-enable these event-source-mappings.",
            extra={
                "mechanism": MECHANISM,
                "attempts": attempts,
                "stillDisabled": remaining,
            },
        )

    return {
        "quiesced": False,
        "mechanism": MECHANISM,
        "uuids": uuids,
        "attempts": attempts,
        "failed": remaining,
    }


__all__ = [
    "ENV_ESM_UUIDS",
    "MECHANISM",
    "UNQUIESCE_MAX_ATTEMPTS",
    "UNQUIESCE_BACKOFF_SEC",
    "QuiesceError",
    "EsmGateway",
    "LambdaEsmGateway",
    "quiesce_rule_engine",
    "unquiesce_rule_engine",
]
