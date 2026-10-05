"""Tests for the PULSE-owned quiesce / un-quiesce seam (data-Orchestrator T4).

Mechanism under test (design "Component 3", preferred option): the seam toggles
the rule-engine DynamoDB Streams event-source-mapping(s) disabled around the
daily bulk roll-forward and re-enabled afterwards. Disabling the ESM pauses the
rule-engine Lambda's *consumption* of the stream, so bulk upserts are never
evaluated and produce zero alerts; re-enabling resumes normal evaluation.

Covers:
    * Property 4: while quiesced, bulk upserts produce zero alerts; after
      un-quiesce a genuine change still fires.
    * Un-quiesce reversibility guarantee (Requirement 5.5): bounded retry, and a
      CRITICAL log rather than a raise on continued failure so the orchestrator
      Catch path always completes.
    * Quiesce fails loudly on a partial disable (Requirement 5.1/5.3: no partial
      suppression that could leak alerts).

External boundaries are mocked: a fake ESM gateway records toggles (no boto3),
and a moto-backed ``pulse-alerts`` table receives any persisted alerts.
"""

from __future__ import annotations

import uuid
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from pulse.common.models import AlertTier, AlertType, OperationalChange
from pulse.rule_engine.handler import evaluate_rules, persist_alert_draft
from pulse.rule_engine.quiesce import (
    MECHANISM,
    LambdaEsmGateway,
    QuiesceError,
    quiesce_rule_engine,
    unquiesce_rule_engine,
)
from tests.rule_engine.conftest import make_rule

# Anti-hang guardrail: cap examples and drop the deadline; the shared moto
# table fixture is reused across examples on purpose.
PROPERTY_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# This test seeds the premium-cancellation rule with triage disabled (see
# agent_triage_enabled=False below), so the alert fires end to end through
# evaluate_rules + persist_alert_draft with no Bedrock/delivery boundary. (INFO
# alerts CAN be triaged now via an advisory brief, but that is out of scope for
# this quiesce test, which only exercises the persistence path.)
_PREMIUM_TRIGGER = {"operator": "eq", "left": "reservation.isPremium", "right": True}

# ESM UUIDs mirroring the rule engine's 5 consumed LUMI table streams.
_ESM_UUIDS = "uuid-rooms,uuid-guests,uuid-revenues,uuid-reservations,uuid-work-orders"


# ---------------------------------------------------------------------------
# Fakes: the ESM control plane and the stream consumer it gates
# ---------------------------------------------------------------------------


class FakeEsmGateway:
    """In-memory :class:`EsmGateway` recording per-UUID enabled state.

    Starts every mapping enabled (the steady-state deployed condition), so a
    quiesce disables and an un-quiesce re-enables, mirroring the real Lambda
    event-source-mapping control plane without boto3.
    """

    def __init__(
        self, uuids: list[str], fail_uuids: frozenset[str] = frozenset()
    ) -> None:
        """Initialize the fake gateway.

        Args:
            uuids: The mapping UUIDs this gateway knows about (start enabled).
            fail_uuids: UUIDs whose toggle always raises, to model a control
                plane that will not accept the change (drives retry/CRITICAL).
        """
        self.enabled = {u: True for u in uuids}
        self.fail_uuids = fail_uuids
        self.calls: list[tuple[str, bool]] = []

    def set_enabled(self, uuid: str, enabled: bool) -> str:
        """Toggle one mapping, recording the call.

        Args:
            uuid: The mapping UUID.
            enabled: Target enabled state.

        Returns:
            The resulting state string.

        Raises:
            QuiesceError: If the UUID is configured to fail.
        """
        self.calls.append((uuid, enabled))
        if uuid in self.fail_uuids:
            raise QuiesceError(f"control plane rejected {uuid}", uuids=[uuid])
        self.enabled[uuid] = enabled
        return "Enabled" if enabled else "Disabled"


class GatedStreamConsumer:
    """Models the rule-engine Lambda consuming a stream gated by ESM state.

    A record is evaluated and persisted ONLY when its mapping is enabled -- the
    faithful behavior of a DynamoDB Streams event-source-mapping. While the
    mapping is disabled (quiesced), delivered records are dropped, so no alert is
    ever created from them (Requirements 5.1, 5.3).
    """

    def __init__(
        self, gateway: FakeEsmGateway, uuid: str, rules: list, alerts_table: Any
    ) -> None:
        """Initialize the gated consumer.

        Args:
            gateway: The fake ESM gateway holding enabled state.
            uuid: The mapping UUID that gates this consumer.
            rules: The enabled rules to evaluate each change against.
            alerts_table: The moto-backed ``pulse-alerts`` table.
        """
        self._gateway = gateway
        self._uuid = uuid
        self._rules = rules
        self._alerts_table = alerts_table
        self.created = 0

    def deliver(self, change: OperationalChange) -> int:
        """Deliver one operational change to the (possibly paused) engine.

        Args:
            change: The operational change from the stream.

        Returns:
            The number of new alerts created (0 while quiesced).
        """
        # ESM disabled -> the Lambda does not poll the stream; the record is not
        # consumed and no evaluation happens.
        if not self._gateway.enabled.get(self._uuid, False):
            return 0
        drafts = evaluate_rules(change, self._rules)
        created = 0
        for draft in drafts:
            if persist_alert_draft(draft, self._alerts_table):
                created += 1
        self.created += created
        return created


def _premium_cancellation_change(
    property_id: str, reservation_id: str
) -> OperationalChange:
    """Build a genuine premium-cancellation change that fires an INFO alert."""
    return OperationalChange(
        table="stayos-reservations",
        event_name="MODIFY",
        property_id=property_id,
        new_image={
            "propertyId": property_id,
            "reservationId": reservation_id,
            "reservationStatus": "Cancelled",
            "isPremium": True,
        },
        old_image=None,
    )


def _create_alerts_table() -> Any:
    """Create and return a moto-backed ``pulse-alerts`` table resource."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="pulse-alerts",
        KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "alertId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("pulse-alerts")


# ---------------------------------------------------------------------------
# Property 4: quiesce suppresses; un-quiesce resumes
# ---------------------------------------------------------------------------


# Feature: data-Orchestrator, Property 4: while quiesced, bulk upserts produce
# zero alerts; after un-quiesce, a genuine change still fires.
@PROPERTY_SETTINGS
@given(
    bulk_count=st.integers(min_value=1, max_value=40),
    property_id=st.text(
        alphabet=st.characters(min_codepoint=65, max_codepoint=90),
        min_size=1,
        max_size=12,
    ),
)
def test_property_4_quiesce_suppresses_unquiesce_resumes(
    bulk_count: int, property_id: str
) -> None:
    """Bulk upserts while quiesced create zero alerts; a later change fires.

    A fresh moto table is created per generated example so exact alert counts
    are isolated (deterministic alertIds otherwise persist across examples).

    Validates: Requirements 5.1, 5.3, 5.4
    """
    with mock_aws():
        alerts_table = _create_alerts_table()
        uuids = _ESM_UUIDS.split(",")
        gateway = FakeEsmGateway(uuids)
        # The reservations stream carries the bulk premium changes.
        rules = [
            make_rule(
                AlertType.PREMIUM_CANCELLATION,
                AlertTier.INFO,
                _PREMIUM_TRIGGER,
                agent_triage_enabled=False,
            )
        ]
        consumer = GatedStreamConsumer(
            gateway, "uuid-reservations", rules, alerts_table
        )

        # 1. Orchestrator quiesces PULSE before the bulk rewrite.
        q_result = quiesce_rule_engine(gateway=gateway, esm_uuids=_ESM_UUIDS)
        assert q_result["quiesced"] is True
        assert q_result["mechanism"] == MECHANISM
        assert all(gateway.enabled[u] is False for u in uuids)

        # 2. The bulk roll-forward streams many premium-cancellation upserts.
        #    Each WOULD fire an INFO alert, but the engine is paused -> zero.
        for i in range(bulk_count):
            created = consumer.deliver(
                _premium_cancellation_change(
                    property_id, f"BULK-{i}-{uuid.uuid4().hex}"
                )
            )
            assert created == 0
        assert consumer.created == 0
        assert alerts_table.scan()["Count"] == 0

        # 3. Orchestrator un-quiesces after reconciliation.
        u_result = unquiesce_rule_engine(
            gateway=gateway, esm_uuids=_ESM_UUIDS, sleep=lambda _s: None
        )
        assert u_result["quiesced"] is False
        assert u_result["failed"] == []
        assert all(gateway.enabled[u] is True for u in uuids)

        # 4. A genuine change after un-quiesce still fires an alert.
        created = consumer.deliver(
            _premium_cancellation_change(property_id, f"LIVE-{uuid.uuid4().hex}")
        )
        assert created == 1
        assert alerts_table.scan()["Count"] == 1


# ---------------------------------------------------------------------------
# Unit tests: seam mechanics
# ---------------------------------------------------------------------------


def test_quiesce_disables_all_mappings() -> None:
    """Quiesce disables every configured mapping and reports success."""
    uuids = _ESM_UUIDS.split(",")
    gateway = FakeEsmGateway(uuids)

    result = quiesce_rule_engine(gateway=gateway, esm_uuids=_ESM_UUIDS)

    assert result["quiesced"] is True
    assert result["mechanism"] == MECHANISM
    assert result["failed"] == []
    assert all(enabled is False for enabled in gateway.enabled.values())


def test_quiesce_raises_on_partial_disable() -> None:
    """A mapping that will not disable makes quiesce fail loudly (no partial)."""
    uuids = _ESM_UUIDS.split(",")
    gateway = FakeEsmGateway(uuids, fail_uuids=frozenset({"uuid-guests"}))

    with pytest.raises(QuiesceError) as exc_info:
        quiesce_rule_engine(gateway=gateway, esm_uuids=_ESM_UUIDS)

    # The failing mapping is named so an operator can see which one leaked.
    assert "uuid-guests" in exc_info.value.uuids


def test_quiesce_raises_when_no_uuids_configured() -> None:
    """With no ESM UUIDs configured the seam cannot operate and raises."""
    gateway = FakeEsmGateway([])
    with pytest.raises(QuiesceError):
        quiesce_rule_engine(gateway=gateway, esm_uuids="")


def test_unquiesce_reenables_all_mappings() -> None:
    """Un-quiesce re-enables every mapping and reports no failures."""
    uuids = _ESM_UUIDS.split(",")
    gateway = FakeEsmGateway(uuids)
    # Start from the quiesced state.
    quiesce_rule_engine(gateway=gateway, esm_uuids=_ESM_UUIDS)

    result = unquiesce_rule_engine(
        gateway=gateway, esm_uuids=_ESM_UUIDS, sleep=lambda _s: None
    )

    assert result["quiesced"] is False
    assert result["failed"] == []
    assert all(enabled is True for enabled in gateway.enabled.values())


def test_unquiesce_retries_then_critical_logs_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On continued failure un-quiesce retries, logs CRITICAL, and does not raise.

    Validates: Requirement 5.5 -- PULSE is never left silently suppressed and the
    orchestrator Catch path must still complete.
    """
    import pulse.rule_engine.quiesce as quiesce_module

    # Capture CRITICAL logs directly on the seam's Powertools logger (Powertools
    # loggers do not propagate to caplog by default).
    critical_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        quiesce_module.logger,
        "critical",
        lambda msg, **kwargs: critical_calls.append(kwargs.get("extra", {})),
    )

    uuids = _ESM_UUIDS.split(",")
    # One mapping never re-enables, forcing the retry loop to exhaust.
    gateway = FakeEsmGateway(uuids, fail_uuids=frozenset({"uuid-work-orders"}))

    result = unquiesce_rule_engine(
        gateway=gateway,
        esm_uuids=_ESM_UUIDS,
        max_attempts=3,
        sleep=lambda _s: None,
    )

    # Does not raise; reports the still-disabled mapping and the attempts made.
    assert result["failed"] == ["uuid-work-orders"]
    assert result["attempts"] == 3
    # The other mappings did get re-enabled.
    assert gateway.enabled["uuid-rooms"] is True
    # The failing mapping was retried on every attempt but never succeeded, so
    # the seam reports it as still-disabled (in result["failed"]) rather than
    # silently claiming success.
    enable_attempts = [c for c in gateway.calls if c == ("uuid-work-orders", True)]
    assert len(enable_attempts) == 3
    # A CRITICAL log names the mapping an operator must re-enable.
    assert critical_calls, "expected a CRITICAL log on continued un-quiesce failure"
    assert critical_calls[-1].get("stillDisabled") == ["uuid-work-orders"]


def test_unquiesce_succeeds_after_transient_then_recovers() -> None:
    """Un-quiesce keeps retrying only the pending mapping until it succeeds."""
    uuids = _ESM_UUIDS.split(",")

    class FlakyGateway(FakeEsmGateway):
        """Fails the first enable of one UUID, then succeeds."""

        def __init__(self, uuids: list[str]) -> None:
            super().__init__(uuids)
            self._flaky_seen = 0

        def set_enabled(self, uuid: str, enabled: bool) -> str:
            if uuid == "uuid-revenues" and enabled and self._flaky_seen == 0:
                self._flaky_seen += 1
                self.calls.append((uuid, enabled))
                raise QuiesceError("transient", uuids=[uuid])
            return super().set_enabled(uuid, enabled)

    gateway = FlakyGateway(uuids)
    result = unquiesce_rule_engine(
        gateway=gateway, esm_uuids=_ESM_UUIDS, max_attempts=3, sleep=lambda _s: None
    )

    assert result["failed"] == []
    assert result["attempts"] == 2
    assert all(enabled is True for enabled in gateway.enabled.values())



# ---------------------------------------------------------------------------
# CR-1: LambdaEsmGateway maps retryable control-plane errors to QuiesceError
# ---------------------------------------------------------------------------


class _FakeLambdaExceptions:
    """Stand-in for ``client.exceptions`` exposing the Lambda error classes."""

    class ResourceNotFoundException(Exception):
        pass

    class ResourceConflictException(Exception):
        pass

    class TooManyRequestsException(Exception):
        pass

    class ServiceException(Exception):
        pass


class _RaisingLambdaClient:
    """Fake boto3 ``lambda`` client whose update raises a chosen exception."""

    def __init__(self, exc: Exception) -> None:
        self.exceptions = _FakeLambdaExceptions()
        self._exc = exc
        self.calls = 0

    def update_event_source_mapping(self, **kwargs):
        self.calls += 1
        raise self._exc


class _OkLambdaClient:
    """Fake boto3 ``lambda`` client whose update succeeds."""

    def __init__(self) -> None:
        self.exceptions = _FakeLambdaExceptions()

    def update_event_source_mapping(self, **kwargs):
        return {"State": "Disabling" if not kwargs.get("Enabled") else "Enabling"}


@pytest.mark.parametrize(
    "exc_name",
    [
        "ResourceNotFoundException",
        "ResourceConflictException",
        "TooManyRequestsException",
        "ServiceException",
    ],
)
def test_gateway_maps_retryable_lambda_errors_to_quiesce_error(exc_name: str) -> None:
    """Each retryable control-plane error becomes a QuiesceError naming the UUID.

    Validates CR-1: before the fix only ResourceNotFoundException was caught, so
    ResourceConflictException (the still-Enabling/Disabling race),
    TooManyRequestsException, and ServiceException escaped raw and bypassed the
    bounded-retry / CRITICAL-log path, silently leaving PULSE suppressed.
    """
    exc_cls = getattr(_FakeLambdaExceptions, exc_name)
    fake_client = _RaisingLambdaClient(exc_cls("boom"))
    gateway = LambdaEsmGateway(_client=fake_client)

    with pytest.raises(QuiesceError) as exc_info:
        gateway.set_enabled("uuid-rooms", True)

    assert exc_info.value.uuids == ["uuid-rooms"]
    assert exc_name in str(exc_info.value)


def test_gateway_conflict_during_unquiesce_engages_retry_and_critical_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ResourceConflictException now flows through the retry/CRITICAL path.

    End-to-end CR-1 check at the seam level: with the real LambdaEsmGateway
    wrapping a client that always raises ResourceConflictException, un-quiesce
    must NOT raise (Requirement 5.5 Catch path completes), must report the UUID
    as still-disabled, and must emit a CRITICAL log naming it.
    """
    import pulse.rule_engine.quiesce as quiesce_module

    critical_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        quiesce_module.logger,
        "critical",
        lambda msg, **kwargs: critical_calls.append(kwargs.get("extra", {})),
    )

    conflict = _FakeLambdaExceptions.ResourceConflictException("still Disabling")
    gateway = LambdaEsmGateway(_client=_RaisingLambdaClient(conflict))

    result = unquiesce_rule_engine(
        gateway=gateway,
        esm_uuids="uuid-rooms",
        max_attempts=2,
        sleep=lambda _s: None,
    )

    assert result["failed"] == ["uuid-rooms"]
    assert result["attempts"] == 2
    assert critical_calls, "expected CRITICAL log when un-quiesce cannot complete"
    assert critical_calls[-1].get("stillDisabled") == ["uuid-rooms"]


def test_gateway_returns_state_on_success() -> None:
    """A successful toggle returns the reported control-plane State string."""
    gateway = LambdaEsmGateway(_client=_OkLambdaClient())
    assert gateway.set_enabled("uuid-rooms", False) == "Disabling"
    assert gateway.set_enabled("uuid-rooms", True) == "Enabling"
