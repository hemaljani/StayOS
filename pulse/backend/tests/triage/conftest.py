"""Shared fixtures for Triage Agent tests.

Sets a dummy ``TRIAGE_MODEL_ID`` and AWS region so the Bedrock client can
resolve a model id without a live deployment, and provides small builders for
alert drafts used across the triage tests. Bedrock itself is always mocked via
an injected invoker; no live model is ever called.
"""

from __future__ import annotations

import pytest

from pulse.common.models import AlertDraft, AlertTier, AlertType


@pytest.fixture(autouse=True)
def _triage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the model id and dummy AWS credentials for triage tests."""
    monkeypatch.setenv("TRIAGE_MODEL_ID", "anthropic.claude-3-5-sonnet-test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def make_draft(
    alert_type: AlertType,
    tier: AlertTier,
    *,
    alert_id: str = "alert-test-1",
    property_id: str = "ALOHA-CHI-001",
) -> AlertDraft:
    """Construct an :class:`AlertDraft` for triage tests.

    Args:
        alert_type: The alert type being triaged.
        tier: The alert tier (selects the latency budget).
        alert_id: The alert identifier.
        property_id: The owning property id.

    Returns:
        A populated :class:`AlertDraft`.
    """
    return AlertDraft(
        alert_id=alert_id,
        property_id=property_id,
        tier=tier,
        type=alert_type,
        title="Test alert",
        detail="Test alert detail",
        created_at="2026-08-17T14:30:00Z",
        dedupe_key=f"{alert_type.value}#{property_id}#test",
        source_entity_ref={
            "table": "stayos-reservations",
            "propertyId": property_id,
            "entityKey": "test",
            "ruleType": alert_type.value,
        },
    )


def make_invoker(raw_json: str, *, delay: float = 0.0) -> tuple:
    """Build a fake Bedrock invoker returning fixed text and a monotonic clock.

    Args:
        raw_json: The raw text the fake model returns.
        delay: Simulated elapsed time (seconds) the clock reports for the call.

    Returns:
        A ``(invoker, clock)`` pair for :func:`generate_triage_brief`.
    """
    calls: list[float] = [0.0]

    def _invoker(model_id: str, prompt: str) -> str:
        # Advance the fake clock as if the call took ``delay`` seconds.
        calls[0] += delay
        return raw_json

    def _clock() -> float:
        return calls[0]

    return _invoker, _clock
