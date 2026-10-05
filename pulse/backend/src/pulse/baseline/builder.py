"""Deterministic, bounded curated PULSE baseline builder (reset-then-prime).

This module produces the Curated_Baseline for a property: a small, fixed set of
``pulse-alerts`` items spanning multiple :class:`~pulse.common.models.AlertTier`
and :class:`~pulse.common.models.AlertType` values, including at least one alert
carrying an attached triage brief and at least one escalated alert
(Requirement 6.2). Priming is idempotent via reset-then-prime and cannot grow
the table on repeat runs (Requirement 6.3), and it never depends on the quiesced
bulk stream fallout because it writes the alert items directly (Requirement 6.4).

Determinism (Property 5):
    * Every attribute of every baseline item is derived from a fixed catalog and
      the ``property_id`` alone. No timestamps-of-now, no randomness, no
      environment lookups leak into the item body, so two primings of the same
      property yield byte-identical items.
    * The ``alertId`` is derived with the same deterministic UUIDv5 helper the
      rule engine uses (:func:`~pulse.rule_engine.alert_factory.derive_alert_id`),
      seeded from a baseline-owned dedupe key. The same dedupe key always yields
      the same id, so a re-prime overwrites in place rather than appending.

Boundedness / safe reset (Requirement 6.3):
    * Each baseline item is marked with :data:`BASELINE_MANAGED_ATTRIBUTE` and its
      ``dedupeKey`` starts with :data:`BASELINE_ID_PREFIX`. Reset deletes exactly
      the deterministic ids this catalog produces for the property, so only
      baseline-owned items are removed. Real or presenter-fired live alerts,
      which never carry a baseline dedupe key, are never touched.
    * Because reset targets a fixed, computed key set and prime writes that same
      fixed set, repeat priming is a no-op with respect to item count: the table
      never grows.

The serialized item shape mirrors ``draft_to_item`` (the rule engine's persist
path) plus the lifecycle/triage/escalation attributes the read path and UI
expect (camelCase attributes, NAMING-05), so a seeded baseline alert is
indistinguishable to the feed, detail, and history read paths from a genuinely
fired one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import (
    AlertStatus,
    AlertTier,
    AlertType,
    EscalationReason,
    EscalationStatus,
)
from pulse.rule_engine.alert_factory import derive_alert_id

logger = get_logger("pulse-baseline")

# Marker attribute stamped on every baseline-owned alert item so the reset step
# (and any future audit) can tell curated-baseline items apart from genuinely
# fired ones. camelCase per NAMING-05.
BASELINE_MANAGED_ATTRIBUTE = "baselineManaged"

# Deterministic dedupe-key prefix that seeds every baseline alert id. Baseline
# ids therefore never collide with rule-engine dedupe keys (which describe real
# operational conditions), so reset-then-prime can never delete a real alert.
BASELINE_ID_PREFIX = "baseline"

# A fixed, deterministic creation timestamp for baseline items. Using a constant
# (not "now") is what makes two primings byte-identical (Property 5 / Property 3
# determinism). It is a plausible in-window instant for the demo story.
BASELINE_CREATED_AT = "2026-08-17T06:00:00Z"


@dataclass(frozen=True)
class CuratedAlertSpec:
    """A single curated baseline alert, declared independently of any property.

    The spec is a pure template: :func:`build_baseline_item` binds it to a
    concrete ``property_id`` to produce the DynamoDB item. Keeping the catalog
    property-independent guarantees every property gets the same shape of
    baseline (Requirement 6.2) while the ids stay property-scoped.

    Attributes:
        slug: Stable, unique short id for this baseline entry within a property.
            Feeds the deterministic ``dedupeKey`` / ``alertId``.
        tier: The alert tier.
        type: The alert type.
        title: Human-readable title (1-200 chars).
        detail: Human-readable detail (1-2000 chars).
        status: The lifecycle status the baseline alert is seeded in.
        triage_brief: Optional serialized ``triageBrief`` object (camelCase
            keys) attached to the item; ``None`` leaves the alert brief-less.
        escalation_status: Whether the alert is flagged for mandatory GM review.
        escalation_reasons: The recorded escalation-reason tokens (non-empty iff
            escalated).
        escalation_chain: Ordered recipient aliases for an escalated alert.
        escalation_position: 0-based index of the current escalation recipient.
    """

    slug: str
    tier: AlertTier
    type: AlertType
    title: str
    detail: str
    status: AlertStatus = AlertStatus.UNACKNOWLEDGED
    triage_brief: Optional[dict[str, Any]] = None
    escalation_status: EscalationStatus = EscalationStatus.NONE
    escalation_reasons: list[EscalationReason] = field(default_factory=list)
    escalation_chain: list[str] = field(default_factory=list)
    escalation_position: int = 0


# ---------------------------------------------------------------------------
# The curated catalog
# ---------------------------------------------------------------------------

# A bounded, deterministic set of five alerts spanning three tiers
# (CRITICAL / WARNING / INFO) and five distinct alert types. Exactly one alert
# carries an attached triage brief (the walk-risk CRITICAL) and exactly one is
# escalated to mandatory GM review (the complaint-escalation CRITICAL), so the
# baseline satisfies Requirement 6.2's "at least one" clauses with a predictable
# demo story. The set is intentionally small so priming is bounded and the feed
# stays readable.
_CURATED_CATALOG: tuple[CuratedAlertSpec, ...] = (
    CuratedAlertSpec(
        slug="walk-risk",
        tier=AlertTier.CRITICAL,
        type=AlertType.WALK_RISK,
        title="Walk risk: confirmed arrivals exceed available rooms",
        detail=(
            "Tonight's confirmed reservations exceed sellable inventory. Review "
            "the walk strategy and approve a relocation plan before arrivals peak."
        ),
        # This alert carries an attached triage brief (Requirement 6.2). The
        # shape mirrors the serialized triageBrief the rule/triage path writes:
        # a summary, an integer confidence, ranked options, and a walkStrategy.
        triage_brief={
            "summary": (
                "6-room shortfall projected. Two ranked mitigation options; "
                "recommended plan walks the lowest-loyalty confirmed guests with "
                "compensation."
            ),
            "confidence": 88,
            "options": [
                {
                    "label": "A",
                    "rank": 1,
                    "title": "Walk 6 lowest-loyalty guests with comp package",
                    "detail": (
                        "Relocate to partner overflow, cover transport plus one "
                        "night, and apply a loyalty goodwill credit."
                    ),
                    "recommended": True,
                },
                {
                    "label": "B",
                    "rank": 2,
                    "title": "Hold inventory and monitor no-shows",
                    "detail": (
                        "Delay any walk until the 6pm no-show cutoff; higher risk "
                        "if arrivals materialize."
                    ),
                    "recommended": False,
                },
            ],
            "walkStrategy": {
                "sisterPropertyId": None,
                "sisterPropertyAvailable": False,
                "walkableGuests": [
                    {
                        "guestId": "G-BASE-1",
                        "loyaltyTier": "Member",
                        "reservationId": "R-BASE-1",
                    }
                ],
                "compensation": [
                    {
                        "guestId": "G-BASE-1",
                        "description": "One night + transport at partner property",
                        "estimatedCost": Decimal("220.00"),
                    }
                ],
            },
            "executeLabel": "Approve walk plan",
        },
    ),
    CuratedAlertSpec(
        slug="complaint-escalation",
        tier=AlertTier.CRITICAL,
        type=AlertType.COMPLAINT_ESCALATION,
        title="Escalated guest complaint awaiting GM review",
        detail=(
            "A guest complaint was flagged for mandatory GM review and has not "
            "been acknowledged within the escalation window."
        ),
        # This alert is escalated to mandatory GM review (Requirement 6.2).
        status=AlertStatus.ESCALATED,
        escalation_status=EscalationStatus.MANDATORY_GM_REVIEW,
        escalation_reasons=[EscalationReason.LEGAL_SAFETY],
        escalation_chain=["gm", "agm", "mod"],
        escalation_position=1,
    ),
    CuratedAlertSpec(
        slug="vip-room-not-ready",
        tier=AlertTier.CRITICAL,
        type=AlertType.VIP_ROOM_NOT_READY,
        title="VIP arriving soon with a room not ready",
        detail=(
            "An Ambassador-tier VIP is arriving within the arrival threshold and "
            "the assigned room is not yet Ready."
        ),
    ),
    CuratedAlertSpec(
        slug="ooo-cluster",
        tier=AlertTier.WARNING,
        type=AlertType.OOO_CLUSTER,
        title="Out-of-order room cluster overlaps a group block",
        detail=(
            "Three out-of-order rooms overlap an upcoming group block. Confirm "
            "maintenance timelines to avoid a placement conflict."
        ),
    ),
    CuratedAlertSpec(
        slug="premium-cancellation",
        tier=AlertTier.INFO,
        type=AlertType.PREMIUM_CANCELLATION,
        title="Premium reservation cancelled",
        detail=(
            "A premium reservation was cancelled. No action required; surfaced "
            "for revenue awareness."
        ),
    ),
)


def baseline_specs_for_property(property_id: str) -> tuple[CuratedAlertSpec, ...]:
    """Return the curated alert specs for a property (the fixed catalog).

    The catalog is property-independent, so every property receives the same
    bounded, deterministic set. Exposed as a function (rather than the raw tuple)
    so callers depend on a stable seam and the count can be asserted in tests.

    Args:
        property_id: The property the baseline is for (unused today; present so
            the seam can specialize per property later without a signature
            change).

    Returns:
        The immutable tuple of curated alert specs.
    """
    del property_id  # Same catalog for every property (Requirement 6.2).
    return _CURATED_CATALOG


def _baseline_dedupe_key(property_id: str, slug: str) -> str:
    """Build the deterministic, baseline-owned dedupe key for one alert.

    The key is prefixed with :data:`BASELINE_ID_PREFIX` so it can never collide
    with a rule-engine dedupe key (which encodes a real operational condition),
    guaranteeing reset only ever targets curated items.

    Args:
        property_id: The owning property.
        slug: The catalog entry slug.

    Returns:
        A stable dedupe key such as ``"baseline#ALOHA-CHI-001#walk-risk"``.
    """
    return f"{BASELINE_ID_PREFIX}#{property_id}#{slug}"


def build_baseline_item(spec: CuratedAlertSpec, property_id: str) -> dict[str, Any]:
    """Serialize one curated spec into a deterministic ``pulse-alerts`` item.

    The item shape mirrors ``draft_to_item`` (the rule engine's persist path)
    plus the triage/escalation/lifecycle attributes so the read path, detail
    view, and history writer treat a baseline alert exactly like a fired one.
    Every value derives only from ``spec`` and ``property_id`` (plus fixed
    constants), so the result is byte-identical across primings (Property 5).

    Args:
        spec: The curated alert spec (template).
        property_id: The owning property.

    Returns:
        A DynamoDB item dict (native Python types) ready for ``put_item``.
    """
    dedupe_key = _baseline_dedupe_key(property_id, spec.slug)
    item: dict[str, Any] = {
        "alertId": derive_alert_id(dedupe_key),
        "propertyId": property_id,
        "tier": spec.tier.value,
        "type": spec.type.value,
        "title": spec.title,
        "detail": spec.detail,
        "status": spec.status.value,
        "dedupeKey": dedupe_key,
        # A curated baseline is not correlated to a real operational record; the
        # ref points back at the baseline itself so the shape stays populated.
        "sourceEntityRef": {
            "table": "baseline",
            "propertyId": property_id,
            "entityKey": spec.slug,
            "ruleType": spec.type.value,
        },
        "incompleteInputData": False,
        "escalationStatus": spec.escalation_status.value,
        "escalationReasons": [reason.value for reason in spec.escalation_reasons],
        "escalationChain": list(spec.escalation_chain),
        "escalationPosition": spec.escalation_position,
        "approval": {
            "state": "PENDING",
            "selectedOption": None,
            "decidedBy": None,
            "decidedAt": None,
        },
        "acknowledgedBy": None,
        "acknowledgedAt": None,
        "resolvedBy": None,
        "resolvedAt": None,
        "createdAt": BASELINE_CREATED_AT,
        "lastStatusChangeAt": BASELINE_CREATED_AT,
        # Marker so reset targets only curated items, never real/live alerts.
        BASELINE_MANAGED_ATTRIBUTE: True,
    }
    if spec.triage_brief is not None:
        item["triageBrief"] = spec.triage_brief
    return item


def build_baseline_items(property_id: str) -> list[dict[str, Any]]:
    """Build the full, ordered list of baseline items for a property.

    Args:
        property_id: The owning property.

    Returns:
        The deterministic list of ``pulse-alerts`` items (one per catalog entry).
    """
    return [
        build_baseline_item(spec, property_id)
        for spec in baseline_specs_for_property(property_id)
    ]


def reset_property_baseline(
    property_id: str,
    *,
    table_getter: Callable[[str], Any],
    alerts_table_name: str,
) -> int:
    """Delete only the curated baseline items for a property (bounded reset).

    Reset computes the exact deterministic ids the catalog produces for the
    property and deletes those keys, so it removes precisely the baseline-owned
    items and nothing else - real or presenter-fired live alerts are never
    touched (Requirement 6.3). It performs no scan or query, so the delete set is
    fixed and bounded regardless of how many live alerts exist.

    Args:
        property_id: The property whose baseline to clear.
        table_getter: Table-resource getter seam (injectable for tests).
        alerts_table_name: The ``pulse-alerts`` physical table name.

    Returns:
        The number of baseline items whose delete was issued.
    """
    table = table_getter(alerts_table_name)
    items = build_baseline_items(property_id)
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"alertId": item["alertId"]})
    logger.info(
        "Reset curated baseline",
        extra={"propertyId": property_id, "deletedBaselineAlerts": len(items)},
    )
    return len(items)


def prime_property_baseline(
    property_id: str,
    *,
    table_getter: Callable[[str], Any] = get_table,
    alerts_table_name: str,
) -> dict[str, Any]:
    """Reset-then-prime the curated baseline for one property (idempotent).

    First deletes the property's prior baseline-owned items, then writes the
    fixed catalog. Because both steps operate on the same deterministic key set,
    repeat priming leaves the table at the same item count (no unbounded growth,
    Requirement 6.3) and yields byte-identical items (Property 5). This never
    reads or depends on the quiesced stream fallout (Requirement 6.4).

    Args:
        property_id: The property to prime.
        table_getter: Table-resource getter seam (injectable for tests); defaults
            to the shared cached ``get_table``.
        alerts_table_name: The ``pulse-alerts`` physical table name (resolved by
            the caller from configuration; never hardcoded here).

    Returns:
        A structured summary: the property id, the number of alerts primed, and
        the ordered list of primed alert ids (for observability/assertions).
    """
    reset_property_baseline(
        property_id,
        table_getter=table_getter,
        alerts_table_name=alerts_table_name,
    )
    table = table_getter(alerts_table_name)
    items = build_baseline_items(property_id)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    primed_ids = [item["alertId"] for item in items]
    logger.info(
        "Primed curated baseline",
        extra={
            "propertyId": property_id,
            "baselineAlertsPrimed": len(items),
            "tiers": sorted({item["tier"] for item in items}),
            "types": sorted({item["type"] for item in items}),
        },
    )
    return {
        "propertyId": property_id,
        "baselineAlertsPrimed": len(items),
        "alertIds": primed_ids,
    }


__all__ = [
    "BASELINE_MANAGED_ATTRIBUTE",
    "BASELINE_ID_PREFIX",
    "BASELINE_CREATED_AT",
    "CuratedAlertSpec",
    "baseline_specs_for_property",
    "build_baseline_item",
    "build_baseline_items",
    "reset_property_baseline",
    "prime_property_baseline",
]
