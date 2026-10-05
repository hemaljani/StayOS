"""Shared PULSE domain models and enums (the ubiquitous language).

This module defines the typed vocabulary every PULSE component speaks: the
alert lifecycle, rule definitions, triage briefs and ranked options, walk
strategies, escalation decisions, and the operational-change record that
enters the pipeline from DynamoDB Streams.

Naming convention (per project NAMING rules):
    * Python identifiers (dataclass fields, enum members) are ``snake_case``.
    * The corresponding DynamoDB item attributes are ``camelCase`` (see
      ``design.md`` Data Models). Serialization/deserialization between the two
      is the responsibility of the ``common.dynamo`` layer (added in a later
      task); these dataclasses model the in-memory Python shape only. The
      camelCase attribute name each field maps to is documented inline so the
      mapping is unambiguous when the serializer is written.

Enum string values are chosen to match the exact tokens used in ``design.md``
and ``requirements.md`` (for example ``EscalationReason`` values are the
hyphenated tokens recorded on an alert), so they can be persisted directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional, TypedDict

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AlertTier(StrEnum):
    """Severity classification of an alert.

    Uses ``enum.StrEnum`` (Python 3.11+) so members are ``str`` instances that
    serialize directly to their string value when written to DynamoDB or JSON
    without an explicit conversion step.
    """

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class AlertStatus(StrEnum):
    """Lifecycle state of an alert.

    Transitions are constrained by the API business logic (see Property 19):
    UNACKNOWLEDGED -> ACKNOWLEDGED, UNACKNOWLEDGED/ACKNOWLEDGED -> RESOLVED,
    and the escalation state machine sets ESCALATED / ESCALATION_EXHAUSTED.
    RESOLVED is terminal.
    """

    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    ESCALATION_EXHAUSTED = "ESCALATION_EXHAUSTED"


class EscalationStatus(StrEnum):
    """Whether an alert has been surfaced for mandatory GM review.

    Set to ``MANDATORY_GM_REVIEW`` when one or more escalation triggers fire
    (see ``EscalationReason`` and Requirement 11); otherwise ``NONE``.
    """

    NONE = "NONE"
    MANDATORY_GM_REVIEW = "MANDATORY_GM_REVIEW"


class EscalationReason(StrEnum):
    """A reason an alert was flagged for mandatory GM review.

    Values are the exact hyphenated tokens recorded on the alert per
    Requirement 11 (and the fail-safe ``threshold-unavailable`` per 11.10).
    An alert may record more than one reason.
    """

    LEGAL_SAFETY = "legal-safety"
    VIP_AMBASSADOR = "vip-ambassador"
    DOLLAR_THRESHOLD = "dollar-threshold"
    LOW_CONFIDENCE = "low-confidence"
    REPEAT_ISSUE = "repeat-issue"
    SLA_OR_ETA = "sla-or-eta"
    MANAGER_REQUESTED = "manager-requested"
    THRESHOLD_UNAVAILABLE = "threshold-unavailable"


class ReviewRisk(StrEnum):
    """Discrete review-risk level attached to a complaint ranked option.

    See Requirement 5.2 (each complaint option carries a review-risk level in
    {Low, Medium, High}).
    """

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class ApprovalState(StrEnum):
    """State of the human-approval gate for a CRITICAL alert's ranked option.

    Enforces the EU AI Act Article 14 "human approves, agent executes" pattern
    (see Requirement 10.3): no ranked-option action executes without a recorded
    GM approval.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AlertType(StrEnum):
    """The eight MVP alert types (UC-01 through UC-06 plus platform types),
    plus the predictive ``FORECAST_OVERSELL`` type.

    The value doubles as the ``ruleType`` sort key in ``pulse-rules`` and the
    ``type`` attribute on ``pulse-alerts``.
    """

    WALK_RISK = "WALK_RISK"  # UC-01, CRITICAL
    VIP_ROOM_NOT_READY = "VIP_ROOM_NOT_READY"  # UC-02, CRITICAL
    COMPLAINT_ESCALATION = "COMPLAINT_ESCALATION"  # UC-04, CRITICAL
    OOO_CLUSTER = "OOO_CLUSTER"  # UC-03, WARNING
    PREMIUM_CANCELLATION = "PREMIUM_CANCELLATION"  # UC-05, INFO
    VIP_CHECKIN = "VIP_CHECKIN"  # UC-06, INFO
    FORECAST_OVERSELL = "FORECAST_OVERSELL"  # Predictive/advisory oversell heads-up (forecasting agent), INFO/WARNING


# ---------------------------------------------------------------------------
# Supporting TypedDicts (loosely-shaped nested payloads)
# ---------------------------------------------------------------------------


class TriggerCondition(TypedDict, total=False):
    """Declarative, non-eval trigger-condition model stored on a rule.

    Maps to the ``triggerCondition`` attribute on ``pulse-rules``. A small,
    safe expression model (never ``eval``-ed code): ``operator`` names a
    comparison, ``left``/``right`` name operands resolved from the operational
    change context. Marked ``total=False`` because different rule types supply
    different operand shapes.
    """

    operator: str  # e.g. "gt", "gte", "eq", "overlaps"
    left: str  # e.g. "reservations.confirmed"
    right: str  # e.g. "rooms.available"


class WalkableGuest(TypedDict, total=False):
    """A confirmed guest identified as walkable in a Walk_Strategy.

    Maps to an entry in ``triageBrief.walkStrategy.walkableGuests``.
    """

    guest_id: str  # -> guestId
    loyalty_tier: str  # -> loyaltyTier
    reservation_id: str  # -> reservationId


class CompensationPackage(TypedDict, total=False):
    """A drafted compensation package for a single walkable guest.

    Maps to an entry in ``triageBrief.walkStrategy.compensation``.
    """

    guest_id: str  # -> guestId
    description: str  # -> description
    estimated_cost: float  # -> estimatedCost (property currency)


class SourceEntityRef(TypedDict, total=False):
    """Correlation pointer from an alert back to the operational record.

    Maps to the ``sourceEntityRef`` attribute on ``pulse-alerts``. Lets the
    closed loop resolve the *originating* alert rather than creating a new one
    (see design Decision 6, Properties 26-27).
    """

    table: str  # -> table
    property_id: str  # -> propertyId
    entity_key: str  # -> entityKey
    rule_type: str  # -> ruleType


# ---------------------------------------------------------------------------
# Ranked options and triage brief
# ---------------------------------------------------------------------------


@dataclass
class RankedOption:
    """A single GM-selectable action produced by the Triage Agent.

    Ranked options are ordered from highest to lowest rank, each with a unique
    label (A, B, C, ...) and a unique rank position; at most one is marked
    recommended (Requirement 10.2). Complaint options additionally carry a
    numeric ``estimated_cost`` and a ``review_risk`` level (Requirement 5.2).

    Attributes:
        label: Unique option label such as "A". Maps to ``label``.
        rank: Unique 1-based rank position, 1 = highest. Maps to ``rank``.
        title: Short option title. Maps to ``title``.
        detail: Longer option description. Maps to ``detail``.
        recommended: Whether this is the single recommended option. Maps to
            ``recommended``.
        estimated_cost: Optional numeric cost in the property currency. Maps to
            ``estimatedCost``.
        review_risk: Optional discrete review-risk level. Maps to
            ``reviewRisk``.
    """

    label: str
    rank: int
    title: str
    detail: str
    recommended: bool = False
    estimated_cost: Optional[float] = None
    review_risk: Optional[ReviewRisk] = None


@dataclass
class WalkStrategy:
    """A GM-approvable plan to relocate guests when a property is oversold.

    Produced by the Triage Agent for a Walk Risk alert (Requirements 3.3-3.6).
    Maps to the ``triageBrief.walkStrategy`` object on ``pulse-alerts``.

    Attributes:
        sister_property_id: Selected sister property, or ``None`` if none
            available. Maps to ``sisterPropertyId``.
        sister_property_available: Whether a sister property with availability
            was found. Maps to ``sisterPropertyAvailable``.
        walkable_guests: Guests selected as walkable (loyalty at or below the
            protection threshold, capped at the shortfall). Maps to
            ``walkableGuests``.
        compensation: One compensation package per walkable guest. Maps to
            ``compensation``.
    """

    sister_property_id: Optional[str]
    sister_property_available: bool
    walkable_guests: list[WalkableGuest] = field(default_factory=list)
    compensation: list[CompensationPackage] = field(default_factory=list)


@dataclass
class TriageBrief:
    """Agent-generated decision package for a CRITICAL or WARNING alert.

    Contains a summary (1-500 chars), an integer confidence percentage
    (0-100), and 2-5 ranked options ordered highest to lowest
    (Requirement 10.1). Walk Risk alerts additionally carry a
    ``walk_strategy``. Maps to the ``triageBrief`` object on ``pulse-alerts``.

    Attributes:
        summary: Human-readable situation summary. Maps to ``summary``.
        confidence: Integer confidence percentage 0-100. Maps to
            ``confidence``.
        options: Ranked options, ordered highest to lowest rank. Maps to
            ``options``.
        walk_strategy: Optional Walk_Strategy for Walk Risk alerts. Maps to
            ``walkStrategy``.
        execute_label: Optional label for the approval action control. Maps to
            ``executeLabel``.
    """

    summary: str
    confidence: int
    options: list[RankedOption] = field(default_factory=list)
    walk_strategy: Optional[WalkStrategy] = None
    execute_label: Optional[str] = None


# ---------------------------------------------------------------------------
# Approval record
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRecord:
    """The human-approval gate state recorded on an alert.

    Maps to the ``approval`` object on ``pulse-alerts``. Enforces that no
    CRITICAL ranked-option action executes without a recorded GM approval
    (Property 7).

    Attributes:
        state: Current approval state. Maps to ``state``.
        selected_option: Label of the approved option, if any. Maps to
            ``selectedOption``.
        decided_by: User identifier who approved/rejected. Maps to
            ``decidedBy``.
        decided_at: ISO 8601 UTC decision timestamp. Maps to ``decidedAt``.
    """

    state: ApprovalState = ApprovalState.PENDING
    selected_option: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------


@dataclass
class RuleDefinition:
    """An admin-editable alert rule definition stored in ``pulse-rules``.

    Keyed by ``property_id`` (partition) + ``rule_type`` (sort). Validation
    rejects out-of-range or missing attributes and retains the prior definition
    (Requirements 2.5, 2.6).

    Attributes:
        property_id: Property the rule applies to. Maps to ``propertyId``.
        rule_type: The alert type this rule produces. Maps to ``ruleType``.
        tier: Alert tier the rule creates. Maps to ``tier``.
        trigger_condition: Declarative trigger condition. Maps to
            ``triggerCondition``.
        parameters: Rule-type-specific thresholds (e.g. arrival threshold,
            loyalty protection tier). Maps to ``parameters``.
        agent_triage_enabled: Whether to request a triage brief. Maps to
            ``agentTriageEnabled``.
        escalation_timeout_sec: Escalation timeout in seconds, validated to
            60-86400 inclusive (Requirement 2.6). Maps to
            ``escalationTimeoutSec``.
        enabled: Whether the rule participates in evaluation
            (Requirement 2.3). Maps to ``enabled``.
        updated_at: ISO 8601 UTC last-update timestamp. Maps to ``updatedAt``.
        updated_by: User identifier of the last editor. Maps to ``updatedBy``.
    """

    property_id: str
    rule_type: AlertType
    tier: AlertTier
    trigger_condition: TriggerCondition
    agent_triage_enabled: bool
    escalation_timeout_sec: int
    enabled: bool
    parameters: dict[str, Any] = field(default_factory=dict)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


# ---------------------------------------------------------------------------
# Operational change (pipeline input)
# ---------------------------------------------------------------------------


@dataclass
class OperationalChange:
    """A single normalized operational-table change from DynamoDB Streams.

    The Rule Engine's thin handler parses a raw stream record into this shape
    before delegating to pure evaluation logic. ``NEW_AND_OLD_IMAGES`` is
    required so evaluators can detect transitions (see design "Streams
    enablement").

    Attributes:
        table: Source operational table name (e.g. ``stayos-reservations``).
        event_name: DynamoDB stream event name (INSERT, MODIFY, REMOVE).
        property_id: Property the changed entity belongs to, if resolvable.
        new_image: The item image after the change (deserialized Python types),
            or ``None`` for REMOVE.
        old_image: The item image before the change, or ``None`` for INSERT.
    """

    table: str
    event_name: str
    property_id: Optional[str] = None
    new_image: Optional[dict[str, Any]] = None
    old_image: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Alert draft and alert
# ---------------------------------------------------------------------------


@dataclass
class AlertDraft:
    """A not-yet-persisted alert produced by rule evaluation.

    The Rule Engine builds an ``AlertDraft`` (unique ``alert_id``,
    ``property_id``, tier, title, detail, ISO 8601 ``created_at``, dedupe key,
    source correlation) before optional triage and persistence to
    ``pulse-alerts`` (Requirements 1.2, 1.3).

    Attributes:
        alert_id: Unique alert identifier. Maps to ``alertId``.
        property_id: Owning property. Maps to ``propertyId``.
        tier: Alert tier. Maps to ``tier``.
        type: Alert type. Maps to ``type``.
        title: Title, 1-200 characters. Maps to ``title``.
        detail: Detail, 1-2000 characters. Maps to ``detail``.
        created_at: ISO 8601 creation timestamp. Maps to ``createdAt``.
        dedupe_key: At-most-one dedupe key for conditional writes. Maps to
            ``dedupeKey``.
        source_entity_ref: Correlation back to the triggering record. Maps to
            ``sourceEntityRef``.
        gm_alias: Owning GM alias, if known at draft time. Maps to ``gmAlias``.
        incomplete_input_data: Whether the alert was built from partial inputs
            (Requirement 4.4). Maps to ``incompleteInputData``.
    """

    alert_id: str
    property_id: str
    tier: AlertTier
    type: AlertType
    title: str
    detail: str
    created_at: str
    dedupe_key: str
    source_entity_ref: SourceEntityRef
    gm_alias: Optional[str] = None
    incomplete_input_data: bool = False


@dataclass
class Alert:
    """A persisted alert record (the ``pulse-alerts`` item).

    Extends the draft with lifecycle, triage, escalation, and approval state.
    See ``design.md`` Data Models for the full item shape.

    Attributes:
        alert_id: Unique alert identifier. Maps to ``alertId``.
        property_id: Owning property. Maps to ``propertyId``.
        tier: Alert tier. Maps to ``tier``.
        type: Alert type. Maps to ``type``.
        title: Title, 1-200 characters. Maps to ``title``.
        detail: Detail, 1-2000 characters. Maps to ``detail``.
        status: Current lifecycle status. Maps to ``status``.
        created_at: ISO 8601 creation timestamp. Maps to ``createdAt``.
        dedupe_key: At-most-one dedupe key. Maps to ``dedupeKey``.
        source_entity_ref: Correlation back to the triggering record. Maps to
            ``sourceEntityRef``.
        gm_alias: Owning GM alias. Maps to ``gmAlias``.
        triage_brief: Optional agent-generated triage brief. Maps to
            ``triageBrief``.
        escalation_status: Mandatory-review indicator. Maps to
            ``escalationStatus``.
        escalation_reasons: Recorded set of trigger reasons. Maps to
            ``escalationReasons``.
        escalation_chain: Ordered recipient aliases [GM, AGM, MOD]. Maps to
            ``escalationChain``.
        escalation_position: 0-based index of the current recipient. Maps to
            ``escalationPosition``.
        escalation_timeout_min: Effective escalation timeout in minutes. Maps
            to ``escalationTimeoutMin``.
        escalation_next_check_at: ISO 8601 next-checkpoint timestamp. Maps to
            ``escalationNextCheckAt``.
        incomplete_input_data: Whether built from partial inputs. Maps to
            ``incompleteInputData``.
        acknowledged_by: Acknowledging user identifier. Maps to
            ``acknowledgedBy``.
        acknowledged_at: ISO 8601 UTC acknowledgment timestamp. Maps to
            ``acknowledgedAt``.
        resolved_by: Resolving user identifier. Maps to ``resolvedBy``.
        resolved_at: ISO 8601 UTC resolution timestamp. Maps to ``resolvedAt``.
        approval: Human-approval gate record. Maps to ``approval``.
        last_status_change_at: ISO 8601 last-status-change timestamp. Maps to
            ``lastStatusChangeAt``.
    """

    alert_id: str
    property_id: str
    tier: AlertTier
    type: AlertType
    title: str
    detail: str
    status: AlertStatus
    created_at: str
    dedupe_key: str
    source_entity_ref: SourceEntityRef
    gm_alias: Optional[str] = None
    triage_brief: Optional[TriageBrief] = None
    escalation_status: EscalationStatus = EscalationStatus.NONE
    escalation_reasons: list[EscalationReason] = field(default_factory=list)
    escalation_chain: list[str] = field(default_factory=list)
    escalation_position: int = 0
    escalation_timeout_min: Optional[int] = None
    escalation_next_check_at: Optional[str] = None
    incomplete_input_data: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    approval: ApprovalRecord = field(default_factory=ApprovalRecord)
    last_status_change_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Escalation decision (trigger hierarchy output)
# ---------------------------------------------------------------------------


@dataclass
class EscalationDecision:
    """Result of evaluating the escalation-trigger hierarchy for an alert.

    A pure function maps alert attributes plus brand policy to this decision
    (Requirement 11, Property 9). ``escalation_status`` is
    ``MANDATORY_GM_REVIEW`` if and only if ``reasons`` is non-empty.

    Attributes:
        escalation_status: Whether the alert requires mandatory GM review.
        reasons: The exact set of satisfied trigger reasons (order-insensitive;
            represented as a list for JSON/DynamoDB friendliness). Maps to
            ``escalationReasons``.
    """

    escalation_status: EscalationStatus
    reasons: list[EscalationReason] = field(default_factory=list)


__all__ = [
    "AlertTier",
    "AlertStatus",
    "EscalationStatus",
    "EscalationReason",
    "ReviewRisk",
    "ApprovalState",
    "AlertType",
    "TriggerCondition",
    "WalkableGuest",
    "CompensationPackage",
    "SourceEntityRef",
    "RankedOption",
    "WalkStrategy",
    "TriageBrief",
    "ApprovalRecord",
    "RuleDefinition",
    "OperationalChange",
    "AlertDraft",
    "Alert",
    "EscalationDecision",
]
