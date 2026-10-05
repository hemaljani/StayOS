"""Unit tests for the Demo Scenario Simulator (Component 7).

Exercises every scenario in the declarative catalog end-to-end against moto:

    * ``plan_mutation(run)`` is pure, deterministic, and repeatable (the same
      plan and item on repeated calls).
    * ``apply_scenario(run)`` writes an item whose attributes raise the exact
      trigger condition the matching rule evaluator reads (so run -> alert
      closes the loop), and ``apply_scenario(reset)`` either clears the
      condition (a put restoring a pre-run state) or deletes the item (when no
      pre-run record existed), restoring the pre-run state deterministically.

The attribute-match assertions pin the simulator's written attributes to the
attributes the evaluators in :mod:`pulse.rule_engine.evaluators` read, so the
simulator and evaluators cannot drift apart.
"""

from __future__ import annotations

from typing import Any

from pulse.common.models import AlertType
from pulse.demo_simulator.simulator import (
    ACTION_RESET,
    ACTION_RUN,
    SCENARIOS,
    apply_scenario,
    get_scenario,
    plan_mutation,
)

# The attributes each scenario's run item must carry for its evaluator to read
# the triggering condition (mirrors pulse.rule_engine.evaluators). Keeping this
# here makes a simulator/evaluator drift fail the test.
_EVALUATOR_READ_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "walk-risk": ("confirmedReservations", "availableRooms"),
    "vip-room-not-ready": ("etaMinutes", "assignedRoomStatus"),
    "complaint-escalation": ("complaintEscalationFlag",),
    "ooo-cluster": ("oooRooms", "groupBlocks"),
    "premium-cancellation": ("isPremium", "reservationStatus"),
    "vip-checkin": ("guestId", "stayId"),
}


def _get_item(resource: Any, scenario: Any) -> dict[str, Any] | None:
    """Return the scenario's target item from moto, or ``None`` if absent."""
    from pulse.demo_simulator import simulator as sim

    table_name = {
        "reservations": sim.ops.reservations_table_name(),
        "rooms": sim.ops.rooms_table_name(),
        "guests": sim.ops.guests_table_name(),
    }[scenario.table_kind]
    return resource.Table(table_name).get_item(Key=scenario.key).get("Item")


def test_plan_mutation_run_is_pure_and_deterministic() -> None:
    """Planning the same run twice yields identical, repeatable plans."""
    for scenario in SCENARIOS.values():
        plan_a = plan_mutation(scenario, ACTION_RUN)
        plan_b = plan_mutation(scenario, ACTION_RUN)
        assert plan_a == plan_b
        assert plan_a.operation == "put"
        assert plan_a.item is not None
        # The plan is built purely from the declarative definition (no I/O).
        assert plan_a.key == scenario.key


def test_variant_run_produces_a_distinct_item_per_variant() -> None:
    """A variant run yields a NEW distinct key/condition vs base and other variants.

    This is what lets a repeated "Generate Alerts" press stack fresh alerts
    (a new dedupeKey -> new alertId) instead of deduping onto an existing one.
    The condition-driving attributes the evaluator reads are still present.
    """
    for scenario_id, scenario in SCENARIOS.items():
        base = plan_mutation(scenario, ACTION_RUN)
        v1 = plan_mutation(scenario, ACTION_RUN, variant="aaa")
        v2 = plan_mutation(scenario, ACTION_RUN, variant="bbb")

        assert v1.operation == "put" and v1.item is not None
        # A variant changes the item vs the base run.
        assert v1.item != base.item, f"{scenario_id}: variant must differ from base"
        # Two different variants differ from each other.
        assert v1.item != v2.item, f"{scenario_id}: distinct variants must differ"
        # ooo-cluster keys on a fixed snapshot row (blockId varies inside the
        # item), so its KEY is stable; every other scenario also varies the key.
        if scenario_id != "ooo-cluster":
            assert v1.key != base.key, f"{scenario_id}: variant should vary the key"
        # The evaluator-read attributes are still present on the variant item.
        for attr in _EVALUATOR_READ_ATTRIBUTES[scenario_id]:
            assert attr in v1.item, f"{scenario_id}: variant item missing {attr}"


def test_run_targets_the_given_property_for_every_scenario() -> None:
    """A run scoped to a target property keys + tags the item to THAT property.

    This is what lets each logged-in GM's "Generate Alerts" populate their OWN
    hotel's feed instead of the hardcoded canonical demo property. The item's
    propertyId and its key's propertyId both reflect the target; the base run
    (default property) still targets the canonical demo property.
    """
    target = "ALOHA-TYO-001"
    for scenario_id, scenario in SCENARIOS.items():
        # Base + variant runs, both scoped to a non-default property.
        base = plan_mutation(scenario, ACTION_RUN, property_id=target)
        variant = plan_mutation(
            scenario, ACTION_RUN, variant="zzz", property_id=target
        )
        for plan in (base, variant):
            assert plan.operation == "put" and plan.item is not None
            assert plan.item["propertyId"] == target, scenario_id
            assert plan.key["propertyId"] == target, scenario_id
        # Default (no property_id) still targets the canonical demo property.
        default_plan = plan_mutation(scenario, ACTION_RUN)
        assert default_plan.item is not None
        assert default_plan.item["propertyId"] == "ALOHA-CHI-001", scenario_id


def test_reset_targets_the_given_property() -> None:
    """A reset scoped to a target property clears/deletes THAT property's item."""
    target = "ALOHA-TYO-001"
    for scenario_id, scenario in SCENARIOS.items():
        plan = plan_mutation(scenario, ACTION_RESET, property_id=target)
        assert plan.key["propertyId"] == target, scenario_id
        if scenario.reset_attributes is None:
            assert plan.operation == "delete", scenario_id
        else:
            assert plan.operation == "put" and plan.item is not None
            assert plan.item["propertyId"] == target, scenario_id


def test_mint_variant_is_nonempty_and_varies() -> None:
    """mint_variant returns a short token that changes across calls."""
    import time

    from pulse.demo_simulator.simulator import mint_variant

    a = mint_variant()
    time.sleep(0.002)
    b = mint_variant()
    assert a and b
    assert a != b


def test_run_then_reset_restores_or_clears_each_scenario(
    operational_tables: Any,
) -> None:
    """For every scenario, run raises the condition and reset restores it.

    Confirms the simulator writes the exact attributes the evaluators read
    (attribute-match) and that reset either clears the condition or deletes the
    item, deterministically restoring the pre-run state.
    """
    resource = operational_tables

    def table_getter(name: str) -> Any:
        return resource.Table(name)

    for scenario_id, scenario in SCENARIOS.items():
        # --- run: raise the triggering condition ---------------------------
        run_plan = apply_scenario(scenario, ACTION_RUN, table_getter=table_getter)
        assert run_plan.operation == "put"
        item = _get_item(resource, scenario)
        assert item is not None, f"{scenario_id}: run must write an item"

        # Attribute-match: the run item carries what the evaluator reads.
        for attr in _EVALUATOR_READ_ATTRIBUTES[scenario_id]:
            assert attr in item, f"{scenario_id}: run item missing {attr!r}"

        # --- reset: clear the condition or delete the item -----------------
        reset_plan = apply_scenario(scenario, ACTION_RESET, table_getter=table_getter)
        after = _get_item(resource, scenario)
        if scenario.reset_attributes is None:
            # No pre-run record existed: reset deletes it.
            assert reset_plan.operation == "delete"
            assert after is None, f"{scenario_id}: reset must delete the item"
        else:
            assert reset_plan.operation == "put"
            assert after is not None
            for key in scenario.reset_attributes:
                assert key in after, f"{scenario_id}: reset item missing {key!r}"


def test_run_is_repeatable_over_the_table(operational_tables: Any) -> None:
    """Applying run twice leaves the same deterministic item (idempotent write)."""
    resource = operational_tables

    def table_getter(name: str) -> Any:
        return resource.Table(name)

    scenario = get_scenario("walk-risk")
    apply_scenario(scenario, ACTION_RUN, table_getter=table_getter)
    first = _get_item(resource, scenario)
    apply_scenario(scenario, ACTION_RUN, table_getter=table_getter)
    second = _get_item(resource, scenario)
    assert first == second


def test_walk_risk_run_raises_and_reset_clears_condition(
    operational_tables: Any,
) -> None:
    """Walk Risk run makes confirmed exceed available; reset clears it."""
    resource = operational_tables

    def table_getter(name: str) -> Any:
        return resource.Table(name)

    scenario = get_scenario("walk-risk")
    apply_scenario(scenario, ACTION_RUN, table_getter=table_getter)
    run_item = _get_item(resource, scenario)
    assert int(run_item["confirmedReservations"]) > int(run_item["availableRooms"])

    apply_scenario(scenario, ACTION_RESET, table_getter=table_getter)
    reset_item = _get_item(resource, scenario)
    assert int(reset_item["confirmedReservations"]) <= int(
        reset_item["availableRooms"]
    )


def test_scenario_conditions_clear_on_reset(operational_tables: Any) -> None:
    """Each resettable scenario clears its specific trigger attribute on reset."""
    resource = operational_tables

    def table_getter(name: str) -> Any:
        return resource.Table(name)

    # (scenario_id, attribute, cleared-value predicate)
    checks = [
        ("vip-room-not-ready", "assignedRoomStatus", lambda v: v == "Ready"),
        ("complaint-escalation", "complaintEscalationFlag", lambda v: v is False),
        ("ooo-cluster", "oooRooms", lambda v: list(v) == []),
        ("premium-cancellation", "reservationStatus", lambda v: v == "Confirmed"),
    ]
    for scenario_id, attr, is_cleared in checks:
        scenario = get_scenario(scenario_id)
        apply_scenario(scenario, ACTION_RUN, table_getter=table_getter)
        apply_scenario(scenario, ACTION_RESET, table_getter=table_getter)
        item = _get_item(resource, scenario)
        assert is_cleared(item[attr]), f"{scenario_id}: {attr!r} not cleared on reset"


def test_scenario_expected_alert_types_are_distinct_and_complete() -> None:
    """The catalog covers all six MVP scenarios with distinct expected types."""
    expected = {scenario.expected_alert_type for scenario in SCENARIOS.values()}
    assert expected == {
        AlertType.WALK_RISK,
        AlertType.VIP_ROOM_NOT_READY,
        AlertType.COMPLAINT_ESCALATION,
        AlertType.OOO_CLUSTER,
        AlertType.PREMIUM_CANCELLATION,
        AlertType.VIP_CHECKIN,
    }
