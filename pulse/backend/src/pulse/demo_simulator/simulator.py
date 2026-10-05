"""Demo Scenario Simulator (``pulse-demo-simulator``), Component 7.

Produces the operational-data changes that make PULSE observable in a demo by
applying scripted, deterministic mutations to the LUMI operational tables.
Because those tables are seeded once and read-only at runtime, without this
component nothing changes and no alert can fire. The simulator writes through
the *same* item schema a real PMS/SPOG feed would use, so the resulting Stream
records are indistinguishable from genuine changes and exercise the real
pipeline (design Component 7).

Design points (PYQUALITY):
    * **Declarative catalog.** :data:`SCENARIOS` maps a scenario id to a
      :class:`ScenarioDefinition` declaring the target table, the deterministic
      ``run`` mutation, its ``reset`` inverse, and the expected alert type. The
      keys/attributes match the rule evaluators
      (:mod:`pulse.rule_engine.evaluators`) and the Action Executor
      (:mod:`pulse.action_executor.executor`) via the shared conventions in
      :mod:`pulse.common.operational_schema`, so run -> alert -> approve ->
      write-back -> resolve closes the loop.
    * **Deterministic & repeatable.** Fixed target entities and fixed deltas, so
      each scenario produces the same alert every run; ``reset`` restores the
      pre-run state for a clean re-run.
    * **Pure mutation planning.** :func:`plan_mutation` builds the mutation
      (a put or delete) with no I/O; the DynamoDB write sits behind the
      injectable ``table_getter`` seam.
    * **Demo-only.** The whole component (Lambda, ambient schedule, demo API
      routes, write IAM) is gated behind the ``EnableDemoSimulator``
      CloudFormation condition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from pulse.common import operational_schema as ops
from pulse.common.dynamo import get_table
from pulse.common.errors import PulseError
from pulse.common.logging import get_logger
from pulse.common.models import AlertType
from pulse.common.tracing import get_tracer

logger = get_logger("pulse-demo-simulator")
tracer = get_tracer("pulse-demo-simulator")

# The single demo property all scenarios target (deterministic).
DEMO_PROPERTY_ID = "ALOHA-CHI-001"

# Scenario actions.
ACTION_RUN = "run"
ACTION_RESET = "reset"


def mint_variant() -> str:
    """Mint a short, unique per-run variant token.

    Uses a millisecond epoch rendered in base36 so it is compact and
    monotonically increasing (distinct across button presses within a demo).

    Returns:
        A short alphanumeric token, e.g. ``"k1a2b3c"``.
    """
    import time

    millis = int(time.time() * 1000)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    token = ""
    while millis:
        millis, remainder = divmod(millis, 36)
        token = digits[remainder] + token
    return token or "0"

# Per-scenario builders that rebuild a run's key + condition-driving attributes
# from a variant-suffixed entity id, so a fresh button press generates a NEW,
# distinct alert (a new dedupeKey/alertId) rather than deduping onto an existing
# one. Keyed by scenario id. Each builder takes the TARGET property and the
# variant token so the demo can target the logged-in GM's own property (not just
# the canonical demo property). Only the entity id that drives the alert's dedupe
# key is varied; the delta that raises the condition is preserved. Returns
# ``(key, run_attributes)``. Reset is unaffected (it always clears the BASE
# entity via the declarative catalog), which is intentional: a variant run is a
# brand-new synthetic entity that the 30-minute timeout sweeper cleans up, so no
# reset is needed for it.
_VARIANT_RUN_BUILDERS: dict[
    str, Callable[[str, str], tuple[dict[str, Any], dict[str, Any]]]
] = {
    "walk-risk": lambda p, v: (
        ops.walk_reservation_key(p, f"2026-08-17-{v}"),
        {
            "arrivalDate": f"2026-08-17-{v}",
            "confirmedReservations": 374,
            "availableRooms": 368,
        },
    ),
    "vip-room-not-ready": lambda p, v: (
        ops.vip_arrival_key(p, f"G-VIP-1-{v}"),
        {
            "guestId": f"G-VIP-1-{v}",
            "etaMinutes": 20,
            "assignedRoomStatus": "Dirty",
            "assignedRoomId": "R-101",
            "vipTier": "Ambassador",
        },
    ),
    "complaint-escalation": lambda p, v: (
        ops.complaint_key(p, f"C-1001-{v}"),
        {"complaintId": f"C-1001-{v}", "complaintEscalationFlag": True},
    ),
    "ooo-cluster": lambda p, v: (
        ops.ooo_snapshot_key(p),
        {
            "oooRooms": _OOO_ROOMS,
            "groupBlocks": [{**_OOO_BLOCK, "blockId": f"BLOCK-778-{v}"}],
        },
    ),
    "premium-cancellation": lambda p, v: (
        ops.premium_reservation_key(p, f"R-PREM-9-{v}"),
        {
            "reservationId": f"R-PREM-9-{v}",
            "reservationStatus": "Cancelled",
            "isPremium": True,
        },
    ),
    "vip-checkin": lambda p, v: (
        ops.vip_checkin_key(p, f"G-VIP-2-{v}", f"S-5001-{v}"),
        {
            "guestId": f"G-VIP-2-{v}",
            "stayId": f"S-5001-{v}",
            "isVip": True,
            "vipTier": "Gold",
        },
    ),
}

# Per-scenario builders for the BASE (non-variant) key, rebuilt for a target
# property. Mirrors each scenario's declarative ``key`` but lets the demo target
# any property (the SCENARIOS catalog keys are precomputed for DEMO_PROPERTY_ID).
_BASE_KEY_BUILDERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "walk-risk": lambda p: ops.walk_reservation_key(p, "2026-08-17"),
    "vip-room-not-ready": lambda p: ops.vip_arrival_key(p, "G-VIP-1"),
    "complaint-escalation": lambda p: ops.complaint_key(p, "C-1001"),
    "ooo-cluster": lambda p: ops.ooo_snapshot_key(p),
    "premium-cancellation": lambda p: ops.premium_reservation_key(p, "R-PREM-9"),
    "vip-checkin": lambda p: ops.vip_checkin_key(p, "G-VIP-2", "S-5001"),
}

# Table-kind selectors mapped to their operational-schema name resolvers.
_TABLE_RESOLVERS: dict[str, Callable[[], str]] = {
    "reservations": ops.reservations_table_name,
    "rooms": ops.rooms_table_name,
    "guests": ops.guests_table_name,
}


class ScenarioError(PulseError):
    """Raised for an unknown scenario id, invalid action, or missing table.

    Attributes:
        scenario_id: The offending scenario id, when known.
    """

    def __init__(self, message: str, scenario_id: Optional[str] = None) -> None:
        """Initialize the scenario error.

        Args:
            message: Human-readable description of the failure.
            scenario_id: The offending scenario id, when known.
        """
        super().__init__(message)
        self.scenario_id = scenario_id


@dataclass(frozen=True)
class ScenarioDefinition:
    """A declarative demo scenario.

    Attributes:
        scenario_id: The scenario identifier used by the API/handler.
        description: Human-readable summary of what the scenario demonstrates.
        table_kind: The operational table selector
            (``"reservations"``/``"rooms"``/``"guests"``).
        key: The item primary key (deterministic, per operational-schema).
        run_attributes: Business attributes that raise the trigger condition.
        reset_attributes: Business attributes that clear the condition on reset;
            ``None`` means the reset deletes the item (restoring a no-record
            pre-run state).
        expected_alert_type: The alert type the scenario is expected to produce.
    """

    scenario_id: str
    description: str
    table_kind: str
    key: dict[str, Any]
    run_attributes: dict[str, Any]
    expected_alert_type: AlertType
    reset_attributes: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class MutationPlan:
    """A planned, deterministic operational write for one scenario action.

    Attributes:
        scenario_id: The scenario the plan belongs to.
        action: The action (``"run"`` or ``"reset"``).
        table_kind: The operational table selector.
        operation: The DynamoDB operation, ``"put"`` or ``"delete"``.
        item: The full item to put (``operation == "put"``), else ``None``.
        key: The primary key (always present; used for delete and reporting).
    """

    scenario_id: str
    action: str
    table_kind: str
    operation: str
    key: dict[str, Any]
    item: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Scenario catalog
# ---------------------------------------------------------------------------

# One overlapping OOO cluster (3 rooms) against one group block, for the OOO
# scenario snapshot. Dates are fixed so the overlap is deterministic.
_OOO_BLOCK = {
    "blockId": "BLOCK-778",
    "roomType": "King",
    "startDate": "2026-08-18",
    "endDate": "2026-08-21",
}
_OOO_ROOMS = [
    {
        "roomId": f"R-40{index}",
        "roomType": "King",
        "startDate": "2026-08-18",
        "endDate": "2026-08-20",
    }
    for index in range(1, 4)
]

SCENARIOS: dict[str, ScenarioDefinition] = {
    "walk-risk": ScenarioDefinition(
        scenario_id="walk-risk",
        description="Confirmed reservations exceed available rooms (UC-01).",
        table_kind="reservations",
        key=ops.walk_reservation_key(DEMO_PROPERTY_ID, "2026-08-17"),
        run_attributes={
            "arrivalDate": "2026-08-17",
            "confirmedReservations": 374,
            "availableRooms": 368,
        },
        reset_attributes={
            "arrivalDate": "2026-08-17",
            "confirmedReservations": 360,
            "availableRooms": 368,
        },
        expected_alert_type=AlertType.WALK_RISK,
    ),
    "vip-room-not-ready": ScenarioDefinition(
        scenario_id="vip-room-not-ready",
        description="VIP arriving within threshold with a non-Ready room (UC-02).",
        table_kind="reservations",
        key=ops.vip_arrival_key(DEMO_PROPERTY_ID, "G-VIP-1"),
        run_attributes={
            "guestId": "G-VIP-1",
            "etaMinutes": 20,
            "assignedRoomStatus": "Dirty",
            "assignedRoomId": "R-101",
            "vipTier": "Ambassador",
        },
        reset_attributes={
            "guestId": "G-VIP-1",
            "etaMinutes": 20,
            "assignedRoomStatus": "Ready",
            "assignedRoomId": "R-101",
            "vipTier": "Ambassador",
        },
        expected_alert_type=AlertType.VIP_ROOM_NOT_READY,
    ),
    "complaint-escalation": ScenarioDefinition(
        scenario_id="complaint-escalation",
        description="Guest complaint escalation flag raised via SPOG (UC-04).",
        table_kind="guests",
        key=ops.complaint_key(DEMO_PROPERTY_ID, "C-1001"),
        run_attributes={"complaintId": "C-1001", "complaintEscalationFlag": True},
        reset_attributes={"complaintId": "C-1001", "complaintEscalationFlag": False},
        expected_alert_type=AlertType.COMPLAINT_ESCALATION,
    ),
    "ooo-cluster": ScenarioDefinition(
        scenario_id="ooo-cluster",
        description="Cluster of 3+ OOO rooms overlapping a group block (UC-03).",
        table_kind="rooms",
        key=ops.ooo_snapshot_key(DEMO_PROPERTY_ID),
        run_attributes={"oooRooms": _OOO_ROOMS, "groupBlocks": [_OOO_BLOCK]},
        reset_attributes={"oooRooms": [], "groupBlocks": [_OOO_BLOCK]},
        expected_alert_type=AlertType.OOO_CLUSTER,
    ),
    "premium-cancellation": ScenarioDefinition(
        scenario_id="premium-cancellation",
        description="A premium reservation is cancelled (UC-05, INFO).",
        table_kind="reservations",
        key=ops.premium_reservation_key(DEMO_PROPERTY_ID, "R-PREM-9"),
        run_attributes={
            "reservationId": "R-PREM-9",
            "reservationStatus": "Cancelled",
            "isPremium": True,
        },
        reset_attributes={
            "reservationId": "R-PREM-9",
            "reservationStatus": "Confirmed",
            "isPremium": True,
        },
        expected_alert_type=AlertType.PREMIUM_CANCELLATION,
    ),
    "vip-checkin": ScenarioDefinition(
        scenario_id="vip-checkin",
        description="A VIP guest checks in (UC-06, INFO).",
        table_kind="guests",
        key=ops.vip_checkin_key(DEMO_PROPERTY_ID, "G-VIP-2", "S-5001"),
        run_attributes={
            "guestId": "G-VIP-2",
            "stayId": "S-5001",
            "isVip": True,
            "vipTier": "Gold",
        },
        # No pre-run record for a check-in: reset deletes it.
        reset_attributes=None,
        expected_alert_type=AlertType.VIP_CHECKIN,
    ),
}


# ---------------------------------------------------------------------------
# Pure mutation planning
# ---------------------------------------------------------------------------


def _full_item(
    scenario: ScenarioDefinition,
    attributes: dict[str, Any],
    *,
    property_id: str,
    key: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Merge a scenario key with business attributes for a target property.

    Args:
        scenario: The scenario definition (for its base key when ``key`` is None).
        attributes: The business attributes to apply.
        property_id: The target property (partition key value).
        key: The primary key to use; when ``None``, the scenario's base key is
            rebuilt for ``property_id`` via :data:`_BASE_KEY_BUILDERS`.

    Returns:
        The full DynamoDB item: the key first, then ``propertyId``, then the
        business attributes. Business attributes are applied LAST so an evaluator
        that reads a business id whose name equals the table's sort-key attribute
        (e.g. ``guestId`` on stayos-guests) sees the business value, not a
        synthetic key value. The key builders guarantee the sort-key attribute is
        always present so PutItem is schema-valid (see BUG-014).
    """
    item_key = key if key is not None else _base_key(scenario, property_id)
    return {**item_key, "propertyId": property_id, **attributes}


def _base_key(scenario: ScenarioDefinition, property_id: str) -> dict[str, Any]:
    """Rebuild a scenario's base (non-variant) key for a target property.

    Falls back to the scenario's precomputed key (built for DEMO_PROPERTY_ID)
    when no per-property builder is registered, preserving back-compatibility.

    Args:
        scenario: The scenario definition.
        property_id: The target property.

    Returns:
        The scenario's primary key scoped to ``property_id``.
    """
    builder = _BASE_KEY_BUILDERS.get(scenario.scenario_id)
    if builder is None:
        return dict(scenario.key)
    return builder(property_id)


def plan_mutation(
    scenario: ScenarioDefinition,
    action: str,
    *,
    variant: Optional[str] = None,
    property_id: str = DEMO_PROPERTY_ID,
) -> MutationPlan:
    """Plan the deterministic operational write for a scenario action (pure).

    Args:
        scenario: The scenario definition.
        action: ``"run"`` (raise the condition) or ``"reset"`` (clear it).
        variant: Optional per-run uniqueness token. When set on a ``run``, the
            scenario's condition-driving entity id is suffixed with the variant
            so the run raises a NEW, distinct condition (a fresh dedupeKey ->
            fresh alertId) instead of deduping onto an existing alert. ``None``
            preserves the original deterministic behavior. Ignored for ``reset``
            (reset always clears the base entity).
        property_id: The target property to scope the mutation to. Defaults to
            the canonical demo property so existing callers are unchanged; the
            demo API threads the logged-in GM's own property so each GM's
            "Generate Alerts" populates their OWN hotel's feed.

    Returns:
        A :class:`MutationPlan` describing the put/delete to apply.

    Raises:
        ScenarioError: If the action is not ``run`` or ``reset``.
    """
    if action == ACTION_RUN:
        if variant:
            builder = _VARIANT_RUN_BUILDERS.get(scenario.scenario_id)
            if builder is not None:
                key, run_attributes = builder(property_id, variant)
                return MutationPlan(
                    scenario_id=scenario.scenario_id,
                    action=action,
                    table_kind=scenario.table_kind,
                    operation="put",
                    key=key,
                    item={**key, "propertyId": property_id, **run_attributes},
                )
        base_key = _base_key(scenario, property_id)
        return MutationPlan(
            scenario_id=scenario.scenario_id,
            action=action,
            table_kind=scenario.table_kind,
            operation="put",
            key=base_key,
            item=_full_item(
                scenario, scenario.run_attributes, property_id=property_id, key=base_key
            ),
        )
    if action == ACTION_RESET:
        base_key = _base_key(scenario, property_id)
        if scenario.reset_attributes is None:
            # No pre-run record existed: reset removes the item.
            return MutationPlan(
                scenario_id=scenario.scenario_id,
                action=action,
                table_kind=scenario.table_kind,
                operation="delete",
                key=base_key,
            )
        return MutationPlan(
            scenario_id=scenario.scenario_id,
            action=action,
            table_kind=scenario.table_kind,
            operation="put",
            key=base_key,
            item=_full_item(
                scenario,
                scenario.reset_attributes,
                property_id=property_id,
                key=base_key,
            ),
        )
    raise ScenarioError(
        f"Unknown action {action!r}; expected 'run' or 'reset'",
        scenario_id=scenario.scenario_id,
    )


# ---------------------------------------------------------------------------
# Application (I/O behind the table_getter seam)
# ---------------------------------------------------------------------------


def _resolve_table_name(table_kind: str, scenario_id: str) -> str:
    """Resolve an operational table name from its kind, or fail.

    Args:
        table_kind: The table selector (``reservations``/``rooms``/``guests``).
        scenario_id: The scenario id, for error context.

    Returns:
        The configured table name.

    Raises:
        ScenarioError: If the table kind is unknown or the name is unconfigured.
    """
    resolver = _TABLE_RESOLVERS.get(table_kind)
    if resolver is None:
        raise ScenarioError(
            f"Unknown table kind {table_kind!r}", scenario_id=scenario_id
        )
    name = resolver()
    if not name:
        raise ScenarioError(
            f"Operational table {table_kind!r} name is not configured",
            scenario_id=scenario_id,
        )
    return name


def apply_scenario(
    scenario: ScenarioDefinition,
    action: str,
    *,
    table_getter: Callable[[str], Any] = get_table,
    variant: Optional[str] = None,
    property_id: str = DEMO_PROPERTY_ID,
) -> MutationPlan:
    """Apply a scenario action to the operational table (put or delete).

    Args:
        scenario: The scenario definition.
        action: ``"run"`` or ``"reset"``.
        table_getter: Table-resource getter seam (injectable for tests).
        variant: Optional per-run uniqueness token (see :func:`plan_mutation`).
        property_id: The target property to scope the mutation to (defaults to
            the canonical demo property).

    Returns:
        The :class:`MutationPlan` that was applied.

    Raises:
        ScenarioError: If the action is invalid or the table is unconfigured.
    """
    plan = plan_mutation(scenario, action, variant=variant, property_id=property_id)
    table_name = _resolve_table_name(plan.table_kind, scenario.scenario_id)
    table = table_getter(table_name)
    if plan.operation == "put":
        table.put_item(Item=plan.item)
    else:
        table.delete_item(Key=plan.key)
    logger.info(
        "Applied demo scenario mutation",
        extra={
            "scenarioId": scenario.scenario_id,
            "action": action,
            "operation": plan.operation,
            "table": table_name,
            "propertyId": property_id,
            "expectedAlertType": scenario.expected_alert_type.value,
        },
    )
    return plan


def get_scenario(scenario_id: str) -> ScenarioDefinition:
    """Return the scenario definition for an id, or raise.

    Args:
        scenario_id: The scenario identifier.

    Returns:
        The matching :class:`ScenarioDefinition`.

    Raises:
        ScenarioError: If no scenario matches the id.
    """
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ScenarioError(
            f"Unknown scenario {scenario_id!r}; known: {sorted(SCENARIOS)}",
            scenario_id=scenario_id,
        )
    return scenario


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Demo Simulator Lambda entry point (thin dispatcher).

    Dispatches ``{scenarioId, action}`` to the declarative catalog and applies
    the deterministic mutation. Invoked on-demand from the demo API route or by
    the optional (disabled-by-default) ambient schedule.

    Args:
        event: ``{"scenarioId": str, "action": "run" | "reset"}``. When invoked
            by the ambient schedule the event may carry only a ``scenarioId``;
            ``action`` defaults to ``"run"``.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A summary dict with the scenario id, action, applied operation, target
        table kind, and the expected alert type.

    Raises:
        ScenarioError: If the scenario id is unknown, the action invalid, or the
            target table is unconfigured.
    """
    scenario_id = str(event.get("scenarioId", ""))
    action = str(event.get("action", ACTION_RUN))
    scenario = get_scenario(scenario_id)
    # Optional target property (defaults to the canonical demo property so the
    # ambient schedule and existing direct invokes are unchanged). The demo API
    # threads the logged-in GM's own property here.
    property_id = str(event.get("propertyId") or "").strip() or DEMO_PROPERTY_ID
    # For a run, mint a short per-invocation variant so each trigger raises a
    # NEW, distinct condition (fresh dedupeKey -> fresh alert) that stacks on top
    # of any existing alerts, rather than deduping onto them. An explicit
    # event "variant" wins (useful for tests / deterministic demos); otherwise a
    # compact time-based token is used. Reset ignores the variant.
    variant: Optional[str] = None
    if action == ACTION_RUN:
        variant = str(event.get("variant") or "").strip() or mint_variant()
    plan = apply_scenario(scenario, action, variant=variant, property_id=property_id)
    return {
        "scenarioId": scenario.scenario_id,
        "action": plan.action,
        "operation": plan.operation,
        "tableKind": plan.table_kind,
        "propertyId": property_id,
        "expectedAlertType": scenario.expected_alert_type.value,
    }


__all__ = [
    "DEMO_PROPERTY_ID",
    "ACTION_RUN",
    "ACTION_RESET",
    "ScenarioError",
    "ScenarioDefinition",
    "MutationPlan",
    "SCENARIOS",
    "mint_variant",
    "plan_mutation",
    "apply_scenario",
    "get_scenario",
    "lambda_handler",
]
