"""Unit tests for the Rule Engine's triage dispatch seam.

These tests exercise ``pulse.rule_engine.triage_invoker.invoke_triage_async``,
the fast, non-blocking hand-off the Rule Engine makes after a CRITICAL/WARNING
alert is delivered (design Decision 8). The evaluator does NOT call
``InvokeAgentRuntime`` itself -- that is synchronous and slow -- it issues an
asynchronous ``lambda:Invoke`` (``InvocationType="Event"``) to the dedicated
``pulse-triage-invoker`` Lambda, which owns the blocking runtime call. These
tests assert:

    * exactly one async ``lambda:Invoke`` is made with the ``{alertId,
      alertType, propertyId, tier}`` event and ``InvocationType="Event"``;
    * a missing ``TRIAGE_INVOKER_FUNCTION_NAME`` skips the dispatch without
      error (Requirement 1.4 - delivery must not depend on triage);
    * a dispatch error is swallowed and never propagates (Requirement 1.7);
    * the invoker function name is resolved from the environment when not
      injected.

INFO-never-triaged (Property 1) and the routing decision are covered in
``test_rule_engine_core.py``. The blocking ``InvokeAgentRuntime`` call itself is
covered in ``test_triage_invoker_handler.py``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from pulse.common.models import AlertTier, AlertType
from pulse.rule_engine.alert_factory import build_alert_draft
from pulse.rule_engine.triage_invoker import (
    ENV_TRIAGE_INVOKER_FUNCTION_NAME,
    build_invoker_event,
    invoke_triage_async,
)

_INVOKER_NAME = "pulse-triage-invoker"


class _FakeLambdaClient:
    """A fake ``lambda`` client recording ``invoke`` calls."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        """Initialize the fake.

        Args:
            error: Optional exception to raise on invoke, to simulate a
                control-plane failure.
        """
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call and optionally raise the configured error.

        Args:
            **kwargs: The Lambda ``invoke`` keyword arguments.

        Returns:
            A stand-in async-accept response (never read by the seam).

        Raises:
            Exception: The configured error, if any.
        """
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"StatusCode": 202}


def _make_draft(tier: AlertTier, alert_type: AlertType) -> Any:
    """Build a persisted alert draft for the given tier/type."""
    return build_alert_draft(
        property_id="ALOHA-CHI-001",
        tier=tier,
        alert_type=alert_type,
        title="t",
        detail="d",
        dedupe_key=f"{alert_type.value}#ALOHA-CHI-001#2026-08-17",
        source_entity_ref={"table": "lumi", "ruleType": alert_type.value},
    )


def test_dispatches_once_with_correct_event() -> None:
    """A CRITICAL alert triggers exactly one async invoke with the right event."""
    client = _FakeLambdaClient()
    draft = _make_draft(AlertTier.CRITICAL, AlertType.WALK_RISK)

    invoke_triage_async(draft, client=client, function_name=_INVOKER_NAME)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["FunctionName"] == _INVOKER_NAME
    # Must be an asynchronous ("Event") invoke so the handler never blocks.
    assert call["InvocationType"] == "Event"
    payload = json.loads(call["Payload"].decode("utf-8"))
    assert payload == {
        "alertId": draft.alert_id,
        "alertType": "WALK_RISK",
        "propertyId": "ALOHA-CHI-001",
        "tier": "CRITICAL",
    }


def test_warning_tier_event_is_correct() -> None:
    """A WARNING alert also dispatches once with its tier in the event."""
    client = _FakeLambdaClient()
    draft = _make_draft(AlertTier.WARNING, AlertType.OOO_CLUSTER)

    invoke_triage_async(draft, client=client, function_name=_INVOKER_NAME)

    assert len(client.calls) == 1
    payload = json.loads(client.calls[0]["Payload"].decode("utf-8"))
    assert payload["tier"] == "WARNING"
    assert payload["alertType"] == "OOO_CLUSTER"


def test_missing_invoker_name_skips_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset TRIAGE_INVOKER_FUNCTION_NAME skips the dispatch without raising."""
    monkeypatch.delenv(ENV_TRIAGE_INVOKER_FUNCTION_NAME, raising=False)
    client = _FakeLambdaClient()
    draft = _make_draft(AlertTier.CRITICAL, AlertType.WALK_RISK)

    # No function_name injected and none in the environment -> no dispatch, and
    # crucially no exception (delivery already happened).
    invoke_triage_async(draft, client=client)

    assert client.calls == []


def test_dispatch_error_is_swallowed() -> None:
    """A dispatch error must not propagate (best-effort, Requirement 1.7)."""
    client = _FakeLambdaClient(error=RuntimeError("lambda unavailable"))
    draft = _make_draft(AlertTier.CRITICAL, AlertType.VIP_ROOM_NOT_READY)

    # Must not raise despite the client error.
    invoke_triage_async(draft, client=client, function_name=_INVOKER_NAME)

    # The call was attempted exactly once even though it failed.
    assert len(client.calls) == 1


def test_invoker_name_resolved_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invoker name is read from TRIAGE_INVOKER_FUNCTION_NAME when not injected."""
    monkeypatch.setenv(ENV_TRIAGE_INVOKER_FUNCTION_NAME, _INVOKER_NAME)
    client = _FakeLambdaClient()
    draft = _make_draft(AlertTier.CRITICAL, AlertType.COMPLAINT_ESCALATION)

    invoke_triage_async(draft, client=client)

    assert len(client.calls) == 1
    assert client.calls[0]["FunctionName"] == _INVOKER_NAME



def test_ooo_event_includes_block_id_from_dedupe_key() -> None:
    """OOO_CLUSTER events carry blockId parsed from the dedupe key.

    Regression for the "the affected group block" placeholder leak: the OOO
    triage situation builder needs the real block id, which travels in the
    invoker event as the trailing dedupe-key segment
    (OOO_CLUSTER#<propertyId>#<blockId>).
    """
    draft = build_alert_draft(
        property_id="ALOHA-CHI-001",
        tier=AlertTier.WARNING,
        alert_type=AlertType.OOO_CLUSTER,
        title="t",
        detail="d",
        dedupe_key="OOO_CLUSTER#ALOHA-CHI-001#BLOCK-778-mtn00agc",
        source_entity_ref={"table": "lumi", "ruleType": "OOO_CLUSTER"},
    )
    event = build_invoker_event(draft)
    assert event["blockId"] == "BLOCK-778-mtn00agc"
    assert event["alertType"] == "OOO_CLUSTER"


def test_non_ooo_event_has_no_block_id() -> None:
    """Non-OOO events do not carry a blockId (it is OOO-specific)."""
    draft = build_alert_draft(
        property_id="ALOHA-CHI-001",
        tier=AlertTier.CRITICAL,
        alert_type=AlertType.WALK_RISK,
        title="t",
        detail="d",
        dedupe_key="WALK_RISK#ALOHA-CHI-001#2026-09-04",
        source_entity_ref={"table": "lumi", "ruleType": "WALK_RISK"},
    )
    event = build_invoker_event(draft)
    assert "blockId" not in event
