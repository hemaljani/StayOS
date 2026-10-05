"""Tests for the curated deterministic PULSE baseline builder (Component 5).

Covers the Requirement 6.2/6.3 invariants and Property 5:

    * Composition (Requirement 6.2): the baseline spans multiple tiers and
      types, carries at least one alert with an attached triage brief, and at
      least one escalated alert.
    * Determinism (Property 5): two builds of the same property are byte-
      identical (same ids, tiers, statuses, and every other attribute).
    * Bounded reset-then-prime (Requirement 6.3): priming twice against a moto
      ``pulse-alerts`` table yields the same items and the same item count (no
      unbounded growth), and reset removes only baseline-owned items.

External boundaries (DynamoDB) are mocked with moto per repository testing
conventions.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.baseline import builder
from pulse.baseline.builder import (
    BASELINE_MANAGED_ATTRIBUTE,
    baseline_specs_for_property,
    build_baseline_items,
    prime_property_baseline,
    reset_property_baseline,
)
from pulse.common.models import AlertStatus, AlertTier, EscalationStatus
from tests.baseline.conftest import ALERTS_TABLE_NAME

_PROPERTY_ID = "ALOHA-CHI-001"

# A conservative, valid property-id shape for the property-based test: the
# generator constrains inputs to the space the builder actually sees (uppercase
# tokens joined by hyphens), keeping generated ids realistic and fast.
_property_id_strategy = st.from_regex(r"[A-Z]{3,5}-[A-Z]{2,4}-\d{3}", fullmatch=True)


def _scan_all(table: Any) -> list[dict[str, Any]]:
    """Return every item currently in the moto-backed alerts table."""
    return table.scan().get("Items", [])


class TestBaselineComposition:
    """Requirement 6.2: tiers/types span, one triage brief, one escalated."""

    def test_baseline_is_bounded_and_nonempty(self) -> None:
        items = build_baseline_items(_PROPERTY_ID)
        # A small, fixed set: bounded (no unbounded growth) yet populated.
        assert 1 <= len(items) <= 10
        assert len(items) == len(baseline_specs_for_property(_PROPERTY_ID))

    def test_spans_multiple_tiers_and_types(self) -> None:
        items = build_baseline_items(_PROPERTY_ID)
        tiers = {item["tier"] for item in items}
        types = {item["type"] for item in items}
        # Multiple tiers (Requirement 6.2), including all three severities.
        assert tiers == {
            AlertTier.CRITICAL.value,
            AlertTier.WARNING.value,
            AlertTier.INFO.value,
        }
        # Multiple distinct types (one per catalog entry).
        assert len(types) == len(items)

    def test_has_at_least_one_triage_brief(self) -> None:
        items = build_baseline_items(_PROPERTY_ID)
        with_brief = [item for item in items if item.get("triageBrief")]
        assert len(with_brief) >= 1
        # The attached brief is well-formed for the read path/UI: summary,
        # integer confidence, and ranked options.
        brief = with_brief[0]["triageBrief"]
        assert isinstance(brief["summary"], str) and brief["summary"]
        assert isinstance(brief["confidence"], int)
        assert len(brief["options"]) >= 2

    def test_has_at_least_one_escalated_alert(self) -> None:
        items = build_baseline_items(_PROPERTY_ID)
        escalated = [
            item
            for item in items
            if item["escalationStatus"] == EscalationStatus.MANDATORY_GM_REVIEW.value
        ]
        assert len(escalated) >= 1
        # An escalated alert carries the ESCALATED status and a non-empty reason
        # set and chain, matching what the escalation read path expects.
        alert = escalated[0]
        assert alert["status"] == AlertStatus.ESCALATED.value
        assert alert["escalationReasons"]
        assert alert["escalationChain"]

    def test_every_item_is_baseline_marked(self) -> None:
        items = build_baseline_items(_PROPERTY_ID)
        assert all(item[BASELINE_MANAGED_ATTRIBUTE] is True for item in items)
        assert all(
            item["dedupeKey"].startswith(builder.BASELINE_ID_PREFIX) for item in items
        )


class TestBaselineDeterminism:
    """Property 5: identical across runs for the same property."""

    # Feature: data-Orchestrator, Property 5: the curated baseline is identical
    # across runs for the same property (same ids/tiers/states/counts) and does
    # not grow on repeat priming.
    # Validates: Requirements 6.2, 6.3
    def test_two_builds_are_byte_identical(self) -> None:
        first = build_baseline_items(_PROPERTY_ID)
        second = build_baseline_items(_PROPERTY_ID)
        assert first == second

    def test_ids_are_property_scoped_and_stable(self) -> None:
        a_ids = [item["alertId"] for item in build_baseline_items("ALOHA-CHI-001")]
        b_ids = [item["alertId"] for item in build_baseline_items("ALOHA-MIA-001")]
        # Stable within a property, distinct across properties.
        rebuilt = [item["alertId"] for item in build_baseline_items("ALOHA-CHI-001")]
        assert a_ids == rebuilt
        assert set(a_ids).isdisjoint(set(b_ids))

    # Feature: data-Orchestrator, Property 5: build determinism across arbitrary
    # property ids.
    # Validates: Requirements 6.2, 6.3
    @settings(max_examples=25, deadline=None)
    @given(property_id=_property_id_strategy)
    def test_build_is_deterministic_for_any_property(self, property_id: str) -> None:
        assert build_baseline_items(property_id) == build_baseline_items(property_id)


class TestResetThenPrimeIdempotence:
    """Requirement 6.3: idempotent reset-then-prime with no unbounded growth."""

    def test_prime_writes_the_full_baseline(self, alerts_table: Any) -> None:
        summary = prime_property_baseline(
            _PROPERTY_ID,
            table_getter=lambda _name: alerts_table,
            alerts_table_name=ALERTS_TABLE_NAME,
        )
        expected = build_baseline_items(_PROPERTY_ID)
        assert summary["baselineAlertsPrimed"] == len(expected)

        stored = _scan_all(alerts_table)
        assert len(stored) == len(expected)
        stored_by_id = {item["alertId"]: item for item in stored}
        for item in expected:
            assert stored_by_id[item["alertId"]] == item

    # Feature: data-Orchestrator, Property 5: priming twice yields an identical
    # baseline (same ids, tiers, states, counts) and the table does not grow.
    # Validates: Requirements 6.2, 6.3
    def test_priming_twice_is_identical_and_bounded(self, alerts_table: Any) -> None:
        table_getter = lambda _name: alerts_table  # noqa: E731 - test seam

        prime_property_baseline(
            _PROPERTY_ID, table_getter=table_getter, alerts_table_name=ALERTS_TABLE_NAME
        )
        first = sorted(_scan_all(alerts_table), key=lambda i: i["alertId"])

        prime_property_baseline(
            _PROPERTY_ID, table_getter=table_getter, alerts_table_name=ALERTS_TABLE_NAME
        )
        second = sorted(_scan_all(alerts_table), key=lambda i: i["alertId"])

        # Byte-identical item set and, crucially, the same count: no growth.
        assert first == second
        assert len(second) == len(build_baseline_items(_PROPERTY_ID))
        # Ids, tiers, and states are stable across the two primings.
        assert [i["alertId"] for i in first] == [i["alertId"] for i in second]
        assert [i["tier"] for i in first] == [i["tier"] for i in second]
        assert [i["status"] for i in first] == [i["status"] for i in second]

    def test_reset_removes_only_baseline_items(self, alerts_table: Any) -> None:
        table_getter = lambda _name: alerts_table  # noqa: E731 - test seam
        # A genuine (non-baseline) alert must survive a baseline reset.
        live_alert = {
            "alertId": "alert-live-1",
            "propertyId": _PROPERTY_ID,
            "tier": AlertTier.CRITICAL.value,
            "type": "WALK_RISK",
            "status": AlertStatus.UNACKNOWLEDGED.value,
        }
        alerts_table.put_item(Item=live_alert)

        prime_property_baseline(
            _PROPERTY_ID, table_getter=table_getter, alerts_table_name=ALERTS_TABLE_NAME
        )
        deleted = reset_property_baseline(
            _PROPERTY_ID, table_getter=table_getter, alerts_table_name=ALERTS_TABLE_NAME
        )

        assert deleted == len(build_baseline_items(_PROPERTY_ID))
        remaining = _scan_all(alerts_table)
        # Only the live alert remains; every baseline item was removed.
        assert len(remaining) == 1
        assert remaining[0]["alertId"] == "alert-live-1"

    def test_priming_two_properties_does_not_cross_contaminate(
        self, alerts_table: Any
    ) -> None:
        table_getter = lambda _name: alerts_table  # noqa: E731 - test seam

        def _prime(property_id: str) -> None:
            prime_property_baseline(
                property_id,
                table_getter=table_getter,
                alerts_table_name=ALERTS_TABLE_NAME,
            )

        _prime("ALOHA-CHI-001")
        _prime("ALOHA-MIA-001")
        stored = _scan_all(alerts_table)
        # Both properties' baselines coexist; total equals the sum.
        assert len(stored) == 2 * len(build_baseline_items("ALOHA-CHI-001"))

        # Re-priming ONE property leaves the other's baseline intact.
        _prime("ALOHA-CHI-001")
        stored_after = _scan_all(alerts_table)
        assert len(stored_after) == len(stored)
