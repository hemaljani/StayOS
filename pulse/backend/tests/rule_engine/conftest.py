"""Shared fixtures and builders for Rule Engine tests.

Provides dummy AWS credentials so moto-backed tests can create clients, and
small factory helpers for constructing rule definitions and operational-change
records without repeating boilerplate in every test.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from pulse.common.models import (
    AlertTier,
    AlertType,
    OperationalChange,
    RuleDefinition,
    TriggerCondition,
)


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials and region for moto-backed tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def make_rule(
    rule_type: AlertType,
    tier: AlertTier,
    trigger_condition: Optional[TriggerCondition] = None,
    *,
    agent_triage_enabled: bool = True,
    enabled: bool = True,
    escalation_timeout_sec: int = 300,
    parameters: Optional[dict[str, Any]] = None,
    property_id: str = "ALOHA-CHI-001",
) -> RuleDefinition:
    """Construct a :class:`RuleDefinition` with sensible test defaults.

    Args:
        rule_type: The rule/alert type.
        tier: The alert tier the rule creates.
        trigger_condition: Declarative trigger condition; a permissive default
            is supplied when omitted.
        agent_triage_enabled: Whether triage is enabled.
        enabled: Whether the rule participates in evaluation.
        escalation_timeout_sec: Escalation timeout in seconds.
        parameters: Rule-type parameters.
        property_id: The owning property id.

    Returns:
        A populated :class:`RuleDefinition`.
    """
    default_trigger: TriggerCondition = {"operator": "eq", "left": 1, "right": 1}
    return RuleDefinition(
        property_id=property_id,
        rule_type=rule_type,
        tier=tier,
        trigger_condition=trigger_condition or default_trigger,
        agent_triage_enabled=agent_triage_enabled,
        escalation_timeout_sec=escalation_timeout_sec,
        enabled=enabled,
        parameters=parameters or {},
    )


def make_change(
    table: str,
    new_image: Optional[dict[str, Any]],
    *,
    event_name: str = "MODIFY",
    old_image: Optional[dict[str, Any]] = None,
    property_id: Optional[str] = None,
) -> OperationalChange:
    """Construct an :class:`OperationalChange` for evaluator tests.

    Args:
        table: The source table name.
        new_image: The item image after the change.
        event_name: The DynamoDB stream event name.
        old_image: The item image before the change.
        property_id: The property id; inferred from the new image when omitted.

    Returns:
        A populated :class:`OperationalChange`.
    """
    resolved = property_id
    if resolved is None and new_image is not None:
        resolved = new_image.get("propertyId")
    return OperationalChange(
        table=table,
        event_name=event_name,
        property_id=resolved,
        new_image=new_image,
        old_image=old_image,
    )
