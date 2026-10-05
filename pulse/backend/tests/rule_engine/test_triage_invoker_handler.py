"""Unit tests for the dedicated Triage Invoker Lambda.

These tests exercise ``pulse.rule_engine.triage_invoker_handler``, the async
hop that owns the blocking ``bedrock-agentcore:InvokeAgentRuntime`` call
(design Decision 8). They assert:

    * exactly one ``InvokeAgentRuntime`` is made forwarding the ``{alertId,
      alertType, propertyId, tier}`` event at the DEFAULT endpoint with a
      >= 33-character session id;
    * a missing ``TRIAGE_RUNTIME_ARN`` skips the invocation without error
      (Requirement 1.4 - delivery must not depend on triage);
    * the runtime ARN is resolved from the environment when not injected.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from pulse.rule_engine.triage_invoker_handler import (
    ENV_TRIAGE_RUNTIME_ARN,
    invoke_runtime,
)

_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/pulse-triage-abc"
)

_EVENT = {
    "alertId": "alert-abc123",
    "alertType": "WALK_RISK",
    "propertyId": "ALOHA-CHI-001",
    "tier": "CRITICAL",
}


class _FakeAgentCoreClient:
    """A fake ``bedrock-agentcore`` client recording invoke calls."""

    def __init__(self, error: Optional[Exception] = None) -> None:
        """Initialize the fake.

        Args:
            error: Optional exception to raise on invocation, to simulate a
                runtime/API failure.
        """
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call and optionally raise the configured error.

        Args:
            **kwargs: The InvokeAgentRuntime keyword arguments.

        Returns:
            A stand-in response (never read by the handler).

        Raises:
            Exception: The configured error, if any.
        """
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"response": object()}


def test_invokes_runtime_once_forwarding_event() -> None:
    """A triage event triggers exactly one InvokeAgentRuntime with the event."""
    client = _FakeAgentCoreClient()

    result = invoke_runtime(dict(_EVENT), client=client, runtime_arn=_RUNTIME_ARN)

    assert result == {"invoked": True, "alertId": "alert-abc123"}
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["agentRuntimeArn"] == _RUNTIME_ARN
    assert call["qualifier"] == "DEFAULT"
    # runtimeSessionId must be at least 33 characters.
    assert len(call["runtimeSessionId"]) >= 33
    payload = json.loads(call["payload"].decode("utf-8"))
    assert payload == _EVENT


def test_missing_runtime_arn_skips_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset TRIAGE_RUNTIME_ARN skips the invoke without raising."""
    monkeypatch.delenv(ENV_TRIAGE_RUNTIME_ARN, raising=False)
    client = _FakeAgentCoreClient()

    result = invoke_runtime(dict(_EVENT), client=client)

    assert result == {"invoked": False, "reason": "runtime-arn-not-configured"}
    assert client.calls == []


def test_runtime_arn_resolved_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime ARN is read from TRIAGE_RUNTIME_ARN when not injected."""
    monkeypatch.setenv(ENV_TRIAGE_RUNTIME_ARN, _RUNTIME_ARN)
    client = _FakeAgentCoreClient()

    result = invoke_runtime(dict(_EVENT), client=client)

    assert result["invoked"] is True
    assert len(client.calls) == 1
    assert client.calls[0]["agentRuntimeArn"] == _RUNTIME_ARN
