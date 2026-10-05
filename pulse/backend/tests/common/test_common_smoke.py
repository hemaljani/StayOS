"""Smoke tests for the shared ``pulse.common`` foundations.

These verify the Task 1 scaffolding is importable and internally consistent:
the package imports cleanly, the domain enums expose the exact values defined
in the spec, the config loader reads/validates environment variables, and the
exception hierarchy is wired correctly. They intentionally avoid any live AWS
calls.
"""

from __future__ import annotations

import importlib

import pytest

from pulse.common import aws, dynamo
from pulse.common.config import PulseConfig, load_config
from pulse.common.errors import (
    ConfigurationError,
    PulseError,
    RuleEvaluationError,
    TriageFailure,
    WriteBackError,
)
from pulse.common.logging import get_logger
from pulse.common.models import (
    Alert,
    AlertDraft,
    AlertStatus,
    AlertTier,
    AlertType,
    EscalationReason,
    RankedOption,
)

# Environment variables required by load_config(); reused across tests.
REQUIRED_ENV = {
    "ALERTS_TABLE_NAME": "pulse-alerts",
    "RULES_TABLE_NAME": "pulse-rules",
    "ALERT_HISTORY_TABLE_NAME": "pulse-alert-history",
    "PUSH_SUBSCRIPTIONS_TABLE_NAME": "pulse-push-subscriptions",
    "KITCHEN_TABLE_NAME": "pulse-kitchen",
    "TRIAGE_MODEL_ID": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "AWS_REGION": "us-east-1",
}


def test_package_imports_cleanly() -> None:
    """The top-level package and every sub-package import without error."""
    for module in [
        "pulse",
        "pulse.common",
        "pulse.rule_engine",
        "pulse.triage",
        "pulse.escalation",
        "pulse.delivery",
        "pulse.action_executor",
        "pulse.demo_simulator",
        "pulse.api",
    ]:
        assert importlib.import_module(module) is not None


def test_alert_tier_values() -> None:
    """AlertTier exposes exactly CRITICAL, WARNING, INFO."""
    assert {t.value for t in AlertTier} == {"CRITICAL", "WARNING", "INFO"}


def test_alert_status_values() -> None:
    """AlertStatus exposes the five documented lifecycle states."""
    assert {s.value for s in AlertStatus} == {
        "UNACKNOWLEDGED",
        "ACKNOWLEDGED",
        "RESOLVED",
        "ESCALATED",
        "ESCALATION_EXHAUSTED",
    }


def test_escalation_reason_tokens() -> None:
    """EscalationReason values match the hyphenated spec tokens."""
    assert EscalationReason.LEGAL_SAFETY.value == "legal-safety"
    assert EscalationReason.THRESHOLD_UNAVAILABLE.value == "threshold-unavailable"


def test_dataclasses_construct() -> None:
    """Core dataclasses construct with the expected field shapes."""
    option = RankedOption(label="A", rank=1, title="Rush clean", detail="...")
    assert option.recommended is False

    draft = AlertDraft(
        alert_id="alert-1",
        property_id="ALOHA-CHI-001",
        tier=AlertTier.CRITICAL,
        type=AlertType.WALK_RISK,
        title="Walk Risk",
        detail="374 confirmed vs 368 available",
        created_at="2026-08-17T14:30:00Z",
        dedupe_key="WALK_RISK#ALOHA-CHI-001#2026-08-17",
        source_entity_ref={"table": "stayos-reservations"},
    )
    assert draft.incomplete_input_data is False

    alert = Alert(
        alert_id="alert-1",
        property_id="ALOHA-CHI-001",
        tier=AlertTier.CRITICAL,
        type=draft.type,
        title=draft.title,
        detail=draft.detail,
        status=AlertStatus.UNACKNOWLEDGED,
        created_at=draft.created_at,
        dedupe_key=draft.dedupe_key,
        source_entity_ref=draft.source_entity_ref,
    )
    # A fresh alert defaults to an empty approval gate in PENDING state.
    assert alert.approval.state.value == "PENDING"


def test_error_hierarchy() -> None:
    """All PULSE errors derive from PulseError."""
    for err in (
        ConfigurationError("x"),
        RuleEvaluationError("x"),
        TriageFailure("x"),
        WriteBackError("x"),
    ):
        assert isinstance(err, PulseError)


def test_load_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config() returns a populated PulseConfig from the environment."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    # Clear optional threshold overrides so defaults are exercised.
    for key in (
        "TRIAGE_CONFIDENCE_THRESHOLD",
        "INFO_BATCH_INTERVAL_MIN",
        "ESCALATION_TIMEOUT_MIN",
    ):
        monkeypatch.delenv(key, raising=False)

    config = load_config()
    assert isinstance(config, PulseConfig)
    assert config.alerts_table == "pulse-alerts"
    assert config.kitchen_table == "pulse-kitchen"
    assert config.confidence_threshold == 85
    assert config.info_batch_interval_min == 15
    assert config.escalation_timeout_min == 5


def test_load_config_missing_required_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing required variable raises ConfigurationError naming it."""
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigurationError) as excinfo:
        load_config()
    assert excinfo.value.variable == "ALERTS_TABLE_NAME"


def test_logger_factory_returns_named_logger() -> None:
    """get_logger returns a Powertools logger tagged with the service name."""
    logger = get_logger("pulse-test")
    assert logger.service == "pulse-test"


def test_aws_client_and_table_helpers_are_cached() -> None:
    """The boto3 client/resource and table helpers return cached instances."""
    # No network call is made by client/resource/Table construction.
    assert aws.get_client("sns") is aws.get_client("sns")
    assert aws.get_resource("dynamodb") is aws.get_resource("dynamodb")
    assert dynamo.get_table("pulse-alerts") is dynamo.get_table("pulse-alerts")
