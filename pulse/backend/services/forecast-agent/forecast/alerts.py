"""Alert Writer for the PULSE Forecasting Agent.

This module turns actionable oversell predictions into ``pulse-alerts`` writes,
following the existing ``dedupeKey`` -> ``alertId`` update-in-place pattern used
across PULSE. It is intentionally built up in stages that mirror the Alert
Writer design (design.md Components -> Alert Writer):

- **Task 8.1 (this task):** ``dedupeKey`` derivation and the derivation-failure
  skip. A ``dedupeKey`` is a deterministic function of the property, condition
  type, and the anticipated date normalized to a single local calendar day, so
  identical inputs always produce an identical key across runs (Req 7.1). When
  any of those inputs is missing or invalid, derivation raises
  :class:`DedupeKeyError` so the caller can skip that prediction, record an
  error identifying it, and leave existing records unchanged (Req 7.6).
- **Later tasks (8.2-8.4):** threshold gate / severity / record assembly,
  create-vs-update-in-place, and resolve/delivery. Those are NOT implemented
  here; this module is left cleanly extendable for them.

The ``dedupeKey`` string format is the literal spec form
``forecast#{propertyId}#OVERSELL#{anticipated_local_day}`` (Req 7.1). Nothing in
this module creates, updates, or reads any DynamoDB record: task 8.1 is pure
derivation and validation.

Identifiers use snake_case (NAMING-03) and carry full type hints
(PYQUALITY-01). Errors use a specific domain exception rather than a generic
one (PYQUALITY-02), and the module logs skip conditions with context
(PYQUALITY-03).

Requirements: 7.1, 7.6.
Design: Components -> Alert Writer (dedupeKey derivation).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional, Union

if TYPE_CHECKING:
    # Imported for type checking only to avoid any runtime import coupling; the
    # RemediationPlan annotation on build_forecast_alert_item resolves here.
    from forecast.narrative import RemediationPlan

logger = logging.getLogger(__name__)

# The condition token used in the dedupeKey for this feature. The public
# derivation accepts a condition_type parameter, but the emitted key uses this
# canonical uppercase token so logically-equivalent oversell forecasts collapse
# to one key (Req 7.1).
DEFAULT_CONDITION_TYPE: str = "OVERSELL"

# Literal dedupeKey format from the spec (Req 7.1). Kept as a module-level
# constant so the exact "forecast#...#OVERSELL#..." shape lives in one place.
DEDUPE_KEY_FORMAT: str = "forecast#{property_id}#{condition}#{anticipated_local_day}"

# The three named inputs to the derivation, used to report which one failed.
INPUT_PROPERTY_ID: str = "property_id"
INPUT_CONDITION_TYPE: str = "condition_type"
INPUT_ANTICIPATED_DATE: str = "anticipated_date"

# Accepted anticipated_date input types: an ISO datetime/date string, or a
# native date/datetime object. All are normalized to a single local calendar
# day (YYYY-MM-DD).
AnticipatedDate = Union[str, date, datetime]


class DedupeKeyError(ValueError):
    """Raised when a ``dedupeKey`` cannot be derived from a prediction.

    Signals that the property identifier, condition type, or anticipated date is
    missing, empty, or unparseable, so the caller MUST skip creating or updating
    a Predictive_Alert for that prediction, record an error identifying it, and
    leave existing records unchanged (Req 7.6).

    Attributes:
        failed_input: The name of the offending input, one of
            :data:`INPUT_PROPERTY_ID`, :data:`INPUT_CONDITION_TYPE`, or
            :data:`INPUT_ANTICIPATED_DATE`, so the caller can identify the failed
            prediction.
        value: The offending raw value, retained for the error record. May be
            ``None``.
    """

    def __init__(self, failed_input: str, message: str, value: Any = None) -> None:
        """Initialize the error with the failed input and a human message.

        Args:
            failed_input: Which named input failed (see class attributes).
            message: A human-readable explanation of why derivation failed.
            value: The offending raw value, retained for context. Defaults to
                ``None``.
        """
        # Prefix the failed input so both the string form and structured field
        # identify the failed prediction input (Req 7.6).
        super().__init__(f"{failed_input}: {message}")
        self.failed_input: str = failed_input
        self.value: Any = value


def _normalize_anticipated_day(anticipated_date: AnticipatedDate) -> str:
    """Normalize an anticipated date to a single local calendar day.

    Accepts a native ``date``/``datetime`` or an ISO string (date or datetime)
    and returns just the calendar day as ``YYYY-MM-DD``. Any time component is
    discarded so an anticipated night collapses to one deterministic day, which
    is what makes the ``dedupeKey`` stable across runs (Req 7.1).

    Args:
        anticipated_date: The anticipated date as a ``date``, ``datetime``, or
            ISO ``YYYY-MM-DD`` / ``YYYY-MM-DDTHH:MM:SS`` string.

    Returns:
        The anticipated calendar day formatted as ``YYYY-MM-DD``.

    Raises:
        DedupeKeyError: If ``anticipated_date`` is missing, empty, or not a
            parseable date. Carries ``failed_input=INPUT_ANTICIPATED_DATE``.
    """
    if anticipated_date is None:
        raise DedupeKeyError(
            INPUT_ANTICIPATED_DATE, "anticipated date is missing", anticipated_date
        )

    # Native datetime/date: take the date part directly. datetime is a subclass
    # of date, so check datetime first to drop the time component.
    if isinstance(anticipated_date, datetime):
        return anticipated_date.date().isoformat()
    if isinstance(anticipated_date, date):
        return anticipated_date.isoformat()

    if isinstance(anticipated_date, str):
        candidate = anticipated_date.strip()
        if not candidate:
            raise DedupeKeyError(
                INPUT_ANTICIPATED_DATE, "anticipated date is empty", anticipated_date
            )
        # Take only the date part of an ISO datetime (split on 'T' or space) so
        # a full timestamp normalizes to its calendar day.
        day_part = candidate.replace(" ", "T").split("T", 1)[0]
        try:
            # date.fromisoformat validates a strict YYYY-MM-DD calendar day.
            parsed = date.fromisoformat(day_part)
        except ValueError as exc:
            raise DedupeKeyError(
                INPUT_ANTICIPATED_DATE,
                f"anticipated date is not a parseable ISO date: {anticipated_date!r}",
                anticipated_date,
            ) from exc
        return parsed.isoformat()

    raise DedupeKeyError(
        INPUT_ANTICIPATED_DATE,
        f"anticipated date has unsupported type {type(anticipated_date).__name__}",
        anticipated_date,
    )


def _require_token(value: Any, failed_input: str) -> str:
    """Validate and normalize a required string token for the dedupeKey.

    Ensures the value is a non-empty string once trimmed, so the derived key can
    never contain an empty or ``None`` segment (Req 7.6).

    Args:
        value: The raw property id or condition type to validate.
        failed_input: The named input, used in the raised error for the caller
            to identify the failed prediction.

    Returns:
        The trimmed token string.

    Raises:
        DedupeKeyError: If ``value`` is missing, not a string, or empty after
            trimming.
    """
    if value is None:
        raise DedupeKeyError(failed_input, f"{failed_input} is missing", value)
    if not isinstance(value, str):
        raise DedupeKeyError(
            failed_input,
            f"{failed_input} must be a string, got {type(value).__name__}",
            value,
        )
    token = value.strip()
    if not token:
        raise DedupeKeyError(failed_input, f"{failed_input} is empty", value)
    return token


def derive_dedupe_key(
    property_id: str,
    condition_type: str,
    anticipated_date: AnticipatedDate,
) -> str:
    """Derive the deterministic ``dedupeKey`` for a predicted condition.

    Computes ``dedupeKey`` as a deterministic function of the property
    identifier, condition type, and the anticipated date normalized to a single
    local calendar day, producing an identical value across runs for identical
    inputs and distinct values for a different property, condition, or day
    (Req 7.1). The emitted key uses the literal spec format
    ``forecast#{propertyId}#OVERSELL#{anticipated_local_day}``.

    Args:
        property_id: The target Property identifier (the platform data-isolation
            boundary). Must be a non-empty string.
        condition_type: The predicted condition token, e.g. ``"OVERSELL"``. Must
            be a non-empty string; it is uppercased in the emitted key so
            equivalent conditions collapse to one key.
        anticipated_date: The anticipated date as a ``date``, ``datetime``, or
            ISO string; normalized to a single ``YYYY-MM-DD`` local calendar day.

    Returns:
        The ``dedupeKey`` string in the form
        ``forecast#{property_id}#{CONDITION}#{YYYY-MM-DD}``.

    Raises:
        DedupeKeyError: If the property id, condition type, or anticipated date
            is missing or invalid. The caller SHOULD skip this prediction,
            record an error identifying it, and leave existing records unchanged
            (Req 7.6).
    """
    normalized_property_id = _require_token(property_id, INPUT_PROPERTY_ID)
    # Uppercase the condition token so logically-equivalent conditions produce
    # the same key (the spec uses the "OVERSELL" token for this feature).
    normalized_condition = _require_token(condition_type, INPUT_CONDITION_TYPE).upper()
    anticipated_local_day = _normalize_anticipated_day(anticipated_date)

    dedupe_key = DEDUPE_KEY_FORMAT.format(
        property_id=normalized_property_id,
        condition=normalized_condition,
        anticipated_local_day=anticipated_local_day,
    )
    logger.debug(
        "Derived dedupeKey %s for property_id=%s condition=%s day=%s",
        dedupe_key,
        normalized_property_id,
        normalized_condition,
        anticipated_local_day,
    )
    return dedupe_key


def dedupe_key_for_prediction(
    prediction: Mapping[str, Any],
    property_id: str,
    condition_type: str = DEFAULT_CONDITION_TYPE,
) -> str:
    """Derive the ``dedupeKey`` for a prediction-like mapping.

    Convenience helper that reads the anticipated date from a prediction-like
    object (accepting either an ``anticipated_date`` or ``anticipatedDate`` key)
    and delegates to :func:`derive_dedupe_key`. On any missing or invalid input
    it raises :class:`DedupeKeyError`, which the orchestrator / task 8.4 uses to
    skip the prediction and record an error rather than writing anything here
    (Req 7.6).

    Args:
        prediction: A prediction-like mapping carrying the anticipated date
            under ``anticipated_date`` or ``anticipatedDate``.
        property_id: The target Property identifier.
        condition_type: The predicted condition token. Defaults to
            :data:`DEFAULT_CONDITION_TYPE` (``"OVERSELL"``).

    Returns:
        The derived ``dedupeKey`` string.

    Raises:
        DedupeKeyError: If the anticipated date key is absent, or any input is
            missing or invalid. Carries the failed input for the error record.
    """
    if "anticipated_date" in prediction:
        anticipated_date: Optional[Any] = prediction["anticipated_date"]
    elif "anticipatedDate" in prediction:
        anticipated_date = prediction["anticipatedDate"]
    else:
        # No anticipated-date field at all: identify it as the failed input so
        # the caller can skip and record the failed prediction (Req 7.6).
        raise DedupeKeyError(
            INPUT_ANTICIPATED_DATE,
            "prediction has no anticipated_date/anticipatedDate field",
            None,
        )

    return derive_dedupe_key(property_id, condition_type, anticipated_date)

# ---------------------------------------------------------------------------
# Task 8.2: threshold gate, severity mapping, predictive record assembly
# (Req 4.1, 4.2, 4.7, 4.8, 6.2, 5.3).
#
# This section turns an actionable oversell prediction plus its authored
# RemediationPlan into a fully-formed ``pulse-alerts`` item, ready for the
# create-vs-update-in-place write in task 8.3. Nothing here touches DynamoDB:
# assembly is pure, so the write path (task 8.3) can decide create-vs-update.
# ---------------------------------------------------------------------------

import uuid
from datetime import timezone
from decimal import Decimal
from typing import Callable

# The alert type token for predictive oversell alerts on ``pulse-alerts``
# (Req 4.7). Distinct from reactive alert types so the PWA and downstream
# consumers can recognize a forecast-originated alert.
FORECAST_ALERT_TYPE: str = "FORECAST_OVERSELL"

# Severity tiering boundary (Req 4.2). Confidence at or above this floor is a
# WARNING; a confidence that meets the threshold but sits below this floor is an
# INFO. Kept as a module constant so the single 85 boundary lives in one place.
WARNING_CONFIDENCE_FLOOR: int = 85

# Severity tier tokens (Req 4.2).
SEVERITY_INFO: str = "INFO"
SEVERITY_WARNING: str = "WARNING"

# The platform "open" status for a newly created alert. Mirrors
# ``pulse.common.models.AlertStatus.UNACKNOWLEDGED`` (the non-terminal status a
# reactive alert is created in); reused here so a forecast alert enters the same
# lifecycle the triage/attach conventions expect.
OPEN_ALERT_STATUS: str = "UNACKNOWLEDGED"

# Advisory-only markers (Req 5.3): a forecast alert is a reviewable proposal and
# is never executed, so it is created not-applied with a pending approval gate.
NOT_APPLIED: bool = False
PENDING_APPROVAL_STATE: str = "PENDING"


def _to_decimal(value: Any) -> Any:
    """Recursively convert Python floats to ``Decimal`` for DynamoDB writes.

    The boto3 DynamoDB *resource* interface rejects native ``float`` values
    (``TypeError: Float types are not supported. Use Decimal types instead``), so
    any float nested in the assembled item must become a ``Decimal`` before the
    write in task 8.3. This mirrors the triage-agent's ``attach._to_decimal``
    helper so both writers coerce floats identically.

    Forecast inputs are integer-valued (``rooms_oversold``, ``confidence``,
    ``overflow_available_rooms``), so in practice no conversion happens; the
    helper is applied defensively in case a caller passes a float.

    Uses ``Decimal(str(value))`` so the stored number matches the human-readable
    float (e.g. ``4.0``) rather than its binary expansion. ``bool`` is left
    untouched (a valid DynamoDB type and an ``int`` subclass).

    Args:
        value: Any JSON-like value (dict, list, float, int, str, bool, None).

    Returns:
        The value with every nested ``float`` converted to ``Decimal``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: _to_decimal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_decimal(item) for item in value]
    return value


def meets_threshold(confidence: Union[int, float], threshold: Union[int, float]) -> bool:
    """Return whether a prediction's confidence clears the write threshold.

    A predictive alert is written only when confidence is at or above the
    configured threshold; below the threshold the caller writes nothing and
    leaves existing records unchanged (Req 4.1, 4.8). This is the pure gate; the
    actual write / no-write decision belongs to task 8.3.

    Args:
        confidence: The prediction confidence (typically an integer in [0, 100]).
        threshold: The configured minimum confidence to warrant a write.

    Returns:
        ``True`` if ``confidence >= threshold`` (a record should be written),
        ``False`` otherwise (no write).
    """
    return confidence >= threshold


def severity_for_confidence(
    confidence: Union[int, float], threshold: Union[int, float]
) -> str:
    """Map a prediction's confidence to its severity tier (Req 4.2).

    Within the writable band (confidence at or above ``threshold``), a confidence
    below :data:`WARNING_CONFIDENCE_FLOOR` is an :data:`SEVERITY_INFO` and a
    confidence at or above it is a :data:`SEVERITY_WARNING`. The threshold is
    accepted so the caller can gate first, then tier; callers should only tier a
    confidence that already :func:`meets_threshold`.

    Args:
        confidence: The prediction confidence (typically an integer in [0, 100]).
        threshold: The configured minimum confidence for a write; the INFO band
            starts here.

    Returns:
        :data:`SEVERITY_WARNING` when ``confidence >= WARNING_CONFIDENCE_FLOOR``,
        otherwise :data:`SEVERITY_INFO`.
    """
    # Threshold is intentionally not used to narrow the band here: severity is a
    # pure function of confidence vs the WARNING floor (Req 4.2). It is accepted
    # for a symmetric call signature with meets_threshold and to make the "tier
    # only within the writable band" contract explicit to callers.
    if confidence >= WARNING_CONFIDENCE_FLOOR:
        return SEVERITY_WARNING
    return SEVERITY_INFO


def _render_recommendation(
    plan: "RemediationPlan",
) -> dict[str, Any]:
    """Render a RemediationPlan into the persisted ``recommendation`` shape.

    Projects the authored plan onto the camelCase attributes the ``pulse-alerts``
    item stores (NAMING-05): the narrative prose, the ordered walk-plan steps,
    the optional overflow option, and the deterministic ``roomsOversold`` count.
    The overflow option is included only when the plan carries a sister-property
    candidate (Req 2.3, 6.6); it is omitted otherwise so absent overflow data is
    not represented as a null option.

    Args:
        plan: The authored (or template-fallback) remediation plan carrying the
            deterministic numeric facts and the narrative/steps.

    Returns:
        A camelCase ``recommendation`` dict with ``narrative``, ``steps``,
        ``roomsOversold`` and, when available, ``overflowOption``.
    """
    recommendation: dict[str, Any] = {
        "narrative": plan.narrative,
        # Persist steps as a list (DynamoDB has no tuple type); order preserved.
        "steps": list(plan.steps),
        "roomsOversold": plan.rooms_oversold,
    }
    # Include the overflow option only when a sister-property candidate exists.
    if plan.overflow_sister_property_id is not None:
        recommendation["overflowOption"] = {
            "sisterPropertyId": plan.overflow_sister_property_id,
            "availableRooms": plan.overflow_available_rooms,
        }
    return recommendation


def build_forecast_alert_item(
    *,
    property_id: str,
    plan: "RemediationPlan",
    rooms_oversold: int,
    anticipated_date: AnticipatedDate,
    confidence: int,
    dedupe_key: str,
    based_on_partial_data: bool,
    threshold: int,
    condition_type: str = DEFAULT_CONDITION_TYPE,
    alert_id: Optional[str] = None,
    now: Optional[str] = None,
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> dict[str, Any]:
    """Assemble the ``FORECAST_OVERSELL`` ``pulse-alerts`` item (no write).

    Builds a fully-formed predictive alert item scoped to the bound run's
    ``propertyId`` (Req 6.2), with camelCase attribute keys (NAMING-05) and
    floats coerced to ``Decimal`` for the DynamoDB resource interface. The item
    is created in the platform open status with the severity tier derived from
    confidence (Req 4.2), and is advisory-only: ``applied`` is ``False`` and the
    approval gate is ``PENDING`` (Req 5.3). This function performs no DynamoDB
    write; the create-vs-update-in-place decision belongs to task 8.3.

    Args:
        property_id: The bound run's Property identifier; the data-isolation
            boundary this alert is scoped to (Req 6.2).
        plan: The authored (or template-fallback) remediation plan; its
            narrative, steps, overflow option, and ``roomsOversold`` are rendered
            into the ``forecast.recommendation`` sub-object.
        rooms_oversold: The deterministic forecasted shortfall (>= 1).
        anticipated_date: The anticipated oversold date (``date``, ``datetime``,
            or ISO string); normalized to a ``YYYY-MM-DD`` local calendar day for
            the stored ``anticipatedDate``.
        confidence: The deterministic prediction confidence (integer in [0, 100]).
        dedupe_key: The deterministic dedupeKey from :func:`derive_dedupe_key`.
        based_on_partial_data: Whether the prediction was made on partial input
            data (surfaced as ``forecast.basedOnPartialData``).
        threshold: The configured minimum confidence; used to tier severity.
        condition_type: The predicted condition token stored on the forecast
            sub-object. Defaults to :data:`DEFAULT_CONDITION_TYPE` (``"OVERSELL"``).
        alert_id: An explicit alert id for a NEW record. When ``None``, one is
            generated via ``id_factory`` (task 8.3 handles create-vs-update, so
            this id is only used when creating).
        now: An ISO 8601 timestamp for ``createdAt`` / ``lastStatusChangeAt``.
            When ``None``, defaults to the current UTC time. Injectable for tests.
        id_factory: A zero-arg factory returning a new alert id. Defaults to a
            uuid4 string. Injectable/overridable for tests.

    Returns:
        The assembled ``pulse-alerts`` item as a dict with camelCase keys and
        ``Decimal``-coerced numeric values, ready for the task 8.3 write.
    """
    # Timestamp seam: injectable ISO string for deterministic tests, else now.
    timestamp = now if now is not None else datetime.now(timezone.utc).isoformat()
    # Alert id seam: an explicit id for a NEW record, else a generated uuid4.
    resolved_alert_id = alert_id if alert_id is not None else id_factory()

    # Normalize the anticipated date to a single local calendar day so the stored
    # value matches the dedupeKey's day component (Req 7.1 normalization).
    anticipated_local_day = _normalize_anticipated_day(anticipated_date)

    # Severity tier from confidence vs the WARNING floor (Req 4.2).
    tier = severity_for_confidence(confidence, threshold)

    # The forecast sub-object: deterministic numeric facts plus the rendered
    # advisory recommendation (Req 4.7, 5.3).
    forecast_detail: dict[str, Any] = {
        "anticipatedDate": anticipated_local_day,
        "conditionType": condition_type,
        "confidence": confidence,
        "roomsOversold": rooms_oversold,
        "recommendation": _render_recommendation(plan),
        "basedOnPartialData": based_on_partial_data,
    }

    item: dict[str, Any] = {
        "alertId": resolved_alert_id,
        "propertyId": property_id,
        "type": FORECAST_ALERT_TYPE,
        # tier and severity carry the same value; both keys are populated so
        # consumers reading either attribute see a consistent tier (Req 4.2).
        "tier": tier,
        "severity": tier,
        "status": OPEN_ALERT_STATUS,
        "dedupeKey": dedupe_key,
        "createdAt": timestamp,
        "lastStatusChangeAt": timestamp,
        "forecast": forecast_detail,
        # Advisory-only: never executed; pending human approval (Req 5.3).
        "applied": NOT_APPLIED,
        "approval": {"state": PENDING_APPROVAL_STATE},
    }

    logger.debug(
        "Assembled FORECAST_OVERSELL alert item alertId=%s propertyId=%s tier=%s "
        "confidence=%s dedupeKey=%s",
        resolved_alert_id,
        property_id,
        tier,
        confidence,
        dedupe_key,
    )

    # Coerce any nested float to Decimal for the DynamoDB resource interface
    # (defensive; forecast inputs are integer-valued).
    return _to_decimal(item)

# ---------------------------------------------------------------------------
# Task 8.3: create-vs-update-in-place with a conditional guard against duplicates
# (Req 4.3, 4.4, 7.2, 7.3, 7.4).
#
# This section is the only part of the Alert Writer that touches DynamoDB. It
# takes a threshold-meeting prediction's already-assembled item (from
# build_forecast_alert_item), its dedupeKey, and the bound propertyId, and
# either CREATEs a new predictive record or UPDATEs the existing open record in
# place. It never creates a second record for the same (dedupeKey, propertyId).
#
# All DynamoDB access goes through an injectable table-getter seam (default
# pulse.common.dynamo.get_table by table name), mirroring the triage-agent's
# ``attach.attach_and_publish(table_getter=get_table)`` idiom so tests can inject
# a fake table without any real boto3 client at import time. Conditional writes
# reuse the same idioms as ``triage-agent/attach.py``: a ConditionExpression on
# the write and a catch of
# ``table.meta.client.exceptions.ConditionalCheckFailedException``.
# ---------------------------------------------------------------------------

from dataclasses import dataclass

from boto3.dynamodb.conditions import Attr, Key

from pulse.common.dynamo import get_table

# The GSI used to find an existing open predictive record for a property, and
# the open status that partitions its sort key. The index is
# (propertyId HASH, status RANGE); a Query on it returns every open alert for a
# property, which is then narrowed to the matching dedupeKey in code. Kept as
# module constants so the single index name / open status live in one place
# (NAMING-02).
PROPERTY_STATUS_INDEX_NAME: str = "propertyId-status-index"

# Attribute keys touched by the write paths (camelCase DynamoDB attrs,
# NAMING-05). The nested forecast sub-object is overwritten wholesale on an
# update-in-place so confidence / condition fields / recommendation are all
# refreshed together, while the top-level identity attributes are retained.
ATTR_ALERT_ID: str = "alertId"
ATTR_PROPERTY_ID: str = "propertyId"
ATTR_STATUS: str = "status"
ATTR_DEDUPE_KEY: str = "dedupeKey"
ATTR_FORECAST: str = "forecast"
ATTR_SEVERITY: str = "severity"
ATTR_TIER: str = "tier"
ATTR_LAST_STATUS_CHANGE_AT: str = "lastStatusChangeAt"


@dataclass(frozen=True)
class UpsertResult:
    """Outcome of an :func:`upsert_forecast_alert` write.

    Lets the caller (task 8.4 resolve/delivery, and tests) act on whether a new
    record was created or an existing open record was updated in place, and on
    which ``alertId`` now holds the forecast.

    Attributes:
        created: ``True`` when a new predictive record was written, ``False``
            when an existing open record was updated in place.
        alert_id: The ``alertId`` of the created or updated record. For an
            update this is the retained original id, not the candidate id.
    """

    created: bool
    alert_id: str


def _find_open_alert_for_dedupe_key(
    table: Any,
    property_id: str,
    dedupe_key: str,
) -> Optional[Mapping[str, Any]]:
    """Return the existing open predictive record for a dedupeKey, if any.

    Queries the ``propertyId-status-index`` GSI for the property's open
    (:data:`OPEN_ALERT_STATUS`) records and filters to the one carrying the
    given ``dedupeKey``. This is how the writer decides create-vs-update: a hit
    means update-in-place, a miss means create (Req 4.3, 4.4).

    Args:
        table: The bound ``pulse-alerts`` table resource (from the table getter).
        property_id: The bound run's Property identifier (the GSI partition and
            the data-isolation boundary, Req 6.2).
        dedupe_key: The deterministic dedupeKey to match on.

    Returns:
        The matching open item, or ``None`` when no open record exists for the
        ``(dedupeKey, propertyId)`` pair.
    """
    # Query the GSI for this property's open records, then narrow to the exact
    # dedupeKey server-side via a FilterExpression. Mirrors the info_batcher
    # query idiom (KeyConditionExpression on propertyId + status).
    response = table.query(
        IndexName=PROPERTY_STATUS_INDEX_NAME,
        KeyConditionExpression=(
            Key(ATTR_PROPERTY_ID).eq(property_id)
            & Key(ATTR_STATUS).eq(OPEN_ALERT_STATUS)
        ),
        FilterExpression=Attr(ATTR_DEDUPE_KEY).eq(dedupe_key),
    )
    items = response.get("Items", [])
    if not items:
        return None
    # A correct upsert keeps at most one open record per dedupeKey, so the first
    # hit is authoritative; log if the invariant is somehow violated.
    if len(items) > 1:
        logger.warning(
            "Multiple open predictive records for one dedupeKey; using the first "
            "(propertyId=%s dedupeKey=%s count=%d)",
            property_id,
            dedupe_key,
            len(items),
        )
    return items[0]


def _update_forecast_in_place(
    table: Any,
    alert_id: str,
    item: Mapping[str, Any],
    now: str,
) -> None:
    """Overwrite an existing open record's forecast payload in place.

    Refreshes the mutable fields of the existing record -- the whole
    ``forecast`` sub-object (confidence, condition fields, recommendation), the
    severity/tier tokens, and ``lastStatusChangeAt`` -- while RETAINING the
    record's identity: ``dedupeKey``, ``alertId``, and ``createdAt`` are never
    written here, so no second record is created (Req 4.4, 7.3, 7.4).

    The write is guarded on the record still being open so a concurrent resolve
    is not silently clobbered; a lost guard is surfaced to the caller.

    Args:
        table: The bound ``pulse-alerts`` table resource.
        alert_id: The retained id of the existing open record to update.
        item: The freshly assembled candidate item; its ``forecast`` /
            ``severity`` / ``tier`` values are the new values written in place.
        now: ISO 8601 UTC timestamp recorded as the new ``lastStatusChangeAt``.

    Raises:
        botocore exceptions from ``update_item`` other than the guarded
        conditional failure propagate to the caller.
    """
    # Overwrite only the mutable forecast payload + status-change timestamp;
    # identity attrs (dedupeKey/alertId/createdAt) are intentionally omitted so
    # they are retained (Req 7.3, 7.4). Attribute names are aliased to avoid the
    # reserved word "status" in the condition.
    table.update_item(
        Key={ATTR_ALERT_ID: alert_id},
        UpdateExpression=(
            "SET #forecast = :forecast, #severity = :severity, #tier = :tier, "
            "#lastStatusChangeAt = :ts"
        ),
        # Only update while the record is still open; a resolved record is left
        # untouched rather than resurrected.
        ConditionExpression=Attr(ATTR_STATUS).eq(OPEN_ALERT_STATUS),
        ExpressionAttributeNames={
            "#forecast": ATTR_FORECAST,
            "#severity": ATTR_SEVERITY,
            "#tier": ATTR_TIER,
            "#lastStatusChangeAt": ATTR_LAST_STATUS_CHANGE_AT,
        },
        ExpressionAttributeValues={
            ":forecast": item[ATTR_FORECAST],
            ":severity": item[ATTR_SEVERITY],
            ":tier": item[ATTR_TIER],
            ":ts": now,
        },
    )


def upsert_forecast_alert(
    item: Mapping[str, Any],
    *,
    dedupe_key: str,
    property_id: str,
    alerts_table_name: str,
    now: Optional[str] = None,
    table_getter: Callable[[str], Any] = get_table,
) -> UpsertResult:
    """Create or update-in-place the predictive alert for one prediction.

    For a threshold-meeting prediction's assembled ``item`` (from
    :func:`build_forecast_alert_item`), its ``dedupe_key``, and the bound
    ``property_id``, this looks up an existing open record for
    ``(dedupeKey, propertyId)`` on the ``propertyId-status-index`` GSI and then:

    - **CREATE** (no existing open record): ``put_item`` the new item with a
      conditional guard so a race cannot create a duplicate for the same
      ``alertId`` key. On :class:`ConditionalCheckFailedException` it re-reads
      the open record and falls back to an update-in-place (Req 4.3, 7.2).
    - **UPDATE-in-place** (existing open record): overwrite the ``forecast``
      payload, ``severity``/``tier``, and ``lastStatusChangeAt`` on that record,
      retaining its ``dedupeKey`` / ``alertId`` / ``createdAt``; no second
      record is created (Req 4.4, 7.3, 7.4).

    All table access goes through ``table_getter`` (default
    :func:`pulse.common.dynamo.get_table`) so no boto3 client is created at
    import for the fake-testable path, mirroring the triage-agent seam.

    Args:
        item: The fully assembled ``pulse-alerts`` item for this prediction, as
            returned by :func:`build_forecast_alert_item` (camelCase attrs,
            Decimal-coerced numerics, carrying a candidate ``alertId``).
        dedupe_key: The deterministic dedupeKey identifying the logical
            prediction (from :func:`derive_dedupe_key`).
        property_id: The bound run's Property identifier; the GSI partition and
            data-isolation boundary (Req 6.2).
        alerts_table_name: The ``pulse-alerts`` physical table name (from
            ``ALERTS_TABLE_NAME`` config); never hardcoded (PYQUALITY-06).
        now: ISO 8601 UTC timestamp used as ``lastStatusChangeAt`` on an
            update-in-place. When ``None``, the current UTC time is used.
            Injectable for deterministic tests.
        table_getter: Table-resource getter seam (injectable for tests);
            defaults to :func:`pulse.common.dynamo.get_table`.

    Returns:
        An :class:`UpsertResult` indicating whether a record was created or an
        existing open record was updated, and the authoritative ``alertId``.

    Raises:
        botocore exceptions from the underlying DynamoDB calls (other than the
        guarded conditional failure, which is handled internally) propagate to
        the caller.
    """
    table = table_getter(alerts_table_name)
    # Update-in-place timestamp seam: injectable ISO string, else now.
    timestamp = now if now is not None else datetime.now(timezone.utc).isoformat()

    # Decide create-vs-update: an open record for this dedupeKey means update.
    existing = _find_open_alert_for_dedupe_key(table, property_id, dedupe_key)
    if existing is not None:
        existing_alert_id = str(existing[ATTR_ALERT_ID])
        _update_forecast_in_place(table, existing_alert_id, item, timestamp)
        logger.info(
            "Updated predictive alert in place (propertyId=%s dedupeKey=%s "
            "alertId=%s)",
            property_id,
            dedupe_key,
            existing_alert_id,
        )
        return UpsertResult(created=False, alert_id=existing_alert_id)

    # CREATE path. Guard the put on the candidate alertId not already existing so
    # a race cannot overwrite a record under the same key. The dedupeKey query
    # above is the primary duplicate guard; this condition guards the write
    # itself (Req 7.2).
    candidate_alert_id = str(item[ATTR_ALERT_ID])
    try:
        table.put_item(
            Item=dict(item),
            ConditionExpression=Attr(ATTR_ALERT_ID).not_exists(),
        )
        logger.info(
            "Created predictive alert (propertyId=%s dedupeKey=%s alertId=%s)",
            property_id,
            dedupe_key,
            candidate_alert_id,
        )
        return UpsertResult(created=True, alert_id=candidate_alert_id)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # A concurrent writer created the record between our query and put.
        # Re-read the open record for this dedupeKey and update it in place so we
        # never leave two records for one logical prediction (Req 7.2).
        logger.info(
            "Create raced with a concurrent write; falling back to update "
            "(propertyId=%s dedupeKey=%s)",
            property_id,
            dedupe_key,
        )
        raced = _find_open_alert_for_dedupe_key(table, property_id, dedupe_key)
        if raced is None:
            # The racing write is not visible as an open record (e.g. already
            # resolved). Treat as already-created for the candidate id rather
            # than creating a duplicate.
            logger.warning(
                "Create race left no open record to update; treating as "
                "already-created (propertyId=%s dedupeKey=%s)",
                property_id,
                dedupe_key,
            )
            return UpsertResult(created=False, alert_id=candidate_alert_id)
        raced_alert_id = str(raced[ATTR_ALERT_ID])
        _update_forecast_in_place(table, raced_alert_id, item, timestamp)
        return UpsertResult(created=False, alert_id=raced_alert_id)


# ---------------------------------------------------------------------------
# Task 8.4: resolve-when-no-longer-forecast + best-effort delivery integration
# (Req 4.5, 4.6, 7.5).
#
# This section closes the Alert Writer loop. It adds two independent, composable
# capabilities on top of the task 8.3 upsert:
#
#   1. resolve-when-no-longer-forecast (Req 7.5): given the dedupeKeys predicted
#      THIS run and the bound propertyId, query the currently-open predictive
#      (FORECAST_OVERSELL) alerts for that property on the propertyId-status-index
#      GSI and transition each open predictive alert whose dedupeKey is NOT in
#      this run's predicted set to RESOLVED. Only FORECAST_OVERSELL-type alerts
#      are ever touched (reactive alerts are left untouched), and each is
#      resolved exactly once via a conditional UpdateItem guarded on still-open.
#
#   2. best-effort delivery integration (Req 4.5, 4.6): after a create/update
#      commit the caller publishes an ALERT_UPDATED event, and on resolve an
#      ALERT_RESOLVED event, through the SHARED pulse.delivery.realtime_publish
#      helper (never a new delivery path). Delivery is POST-COMMIT and
#      best-effort: a publish failure never rolls back or fails the persisted
#      alert. publish_alert_event wraps the shared helper in a try/except that
#      logs a delivery-failure indication and swallows the exception (Req 4.6).
#
# All DynamoDB access reuses the same injectable table-getter seam as task 8.3,
# and the realtime publisher is exposed as an injectable seam (mirroring the
# triage-agent's ``realtime_publisher`` param) so tests can inject a failing
# publisher without any real boto3 client or network call.
# ---------------------------------------------------------------------------

from typing import Sequence

from pulse.delivery import realtime_publish as rt

# Terminal lifecycle status a no-longer-forecast open predictive alert is moved
# to (Req 7.5). Mirrors ``pulse.common.models.AlertStatus.RESOLVED``; kept as a
# local constant so the single token lives in one place (NAMING-03).
RESOLVED_ALERT_STATUS: str = "RESOLVED"

# The alert-type attribute key on ``pulse-alerts`` (camelCase attr, NAMING-05).
# The resolve path filters on this so ONLY forecast-originated predictive alerts
# are ever resolved here; reactive alerts are never touched (Req 7.5).
ATTR_TYPE: str = "type"


def publish_alert_event(
    item: Mapping[str, Any],
    event_type: str,
    *,
    unicast_gm_alias: Optional[str] = None,
    realtime_publisher: Optional["rt.PublisherFn"] = None,
) -> bool:
    """Publish one alert event best-effort; never raise (Req 4.5, 4.6).

    Post-commit delivery seam: given an already-persisted ``pulse-alerts`` item
    and an event type (``ALERT_UPDATED`` on create/update, ``ALERT_RESOLVED`` on
    resolve), publishes a full-enough realtime event to the property channel via
    the SHARED :func:`pulse.delivery.realtime_publish.realtime_publish` helper.
    A publish failure is caught, logged as a delivery-failure indication, and
    swallowed so the persisted alert is never rolled back or failed (Req 4.6);
    the shared publisher is itself best-effort, but this wrapper additionally
    guards against any exception (including an injected failing publisher) so the
    caller can never be broken by delivery.

    Web Push is delivered where the platform helper does it (the shared
    ``pulse-push-service`` delivery path driven off the realtime event / stream);
    this function does not open a second Web Push path.

    Args:
        item: The persisted ``pulse-alerts`` item to build the event from
            (camelCase attributes; must carry ``alertId`` and ``propertyId``).
        event_type: The realtime event type to publish, e.g.
            :data:`rt.EVENT_ALERT_UPDATED` or :data:`rt.EVENT_ALERT_RESOLVED`.
        unicast_gm_alias: When set, the shared helper also publishes to that
            recipient's per-user channel; unused for forecast broadcasts.
        realtime_publisher: Injectable realtime publisher seam (mirrors the
            triage-agent's ``realtime_publisher`` param). When omitted, the
            shared helper resolves its default SigV4 publisher. Tests inject a
            failing publisher here to exercise the best-effort guarantee.

    Returns:
        ``True`` if every event batch published successfully, ``False`` if
        delivery failed (a batch failed, no publisher was configured, or an
        unexpected exception was caught and swallowed). A ``False`` return is the
        delivery-failure indication; the persisted alert is retained either way.
    """
    try:
        # Call the shared lower-level publisher directly (rather than the void
        # realtime_publish convenience helper) so the delivery outcome is
        # OBSERVABLE: rt.publish returns True only when every batch published,
        # and False when a batch failed or no publisher was configured. rt.publish
        # already catches per-batch publisher exceptions internally, so a delivery
        # failure surfaces as a False return, not an exception - which is exactly
        # the delivery-failure indication Req 4.6 requires.
        event = rt.event_from_item(item, event_type)
        property_id = item[ATTR_PROPERTY_ID]
        published = rt.publish(
            rt.broadcast_channel(property_id),
            [event],
            publisher=realtime_publisher,
        )
        # Also nudge the escalation-targeted recipient's per-user channel when
        # requested; the combined result is a success only if both published.
        if unicast_gm_alias:
            published = (
                rt.publish(
                    rt.unicast_channel(property_id, unicast_gm_alias),
                    [event],
                    publisher=realtime_publisher,
                )
                and published
            )
    except Exception as exc:  # noqa: BLE001 - best-effort, must never fail caller
        # Post-commit, best-effort: the alert is already persisted; ANY unexpected
        # delivery error (beyond the per-batch failures rt.publish already
        # swallows) must never roll back or fail it (Req 4.6). Record a
        # delivery-failure indication and swallow.
        logger.error(
            "Alert event publish raised; persisted alert retained, open clients "
            "reconcile on reconnect",
            extra={
                "alertId": item.get(ATTR_ALERT_ID),
                "propertyId": item.get(ATTR_PROPERTY_ID),
                "eventType": event_type,
                "error": str(exc),
            },
        )
        return False

    if not published:
        # Delivery failed (a batch failed or no publisher configured). The alert
        # is already persisted and is retained unchanged; record the
        # delivery-failure indication and report it via the return value (Req 4.6).
        logger.error(
            "Alert event publish failed; persisted alert retained, open clients "
            "reconcile on reconnect",
            extra={
                "alertId": item.get(ATTR_ALERT_ID),
                "propertyId": item.get(ATTR_PROPERTY_ID),
                "eventType": event_type,
            },
        )
    return published


def _find_open_forecast_alerts(
    table: Any,
    property_id: str,
) -> list[Mapping[str, Any]]:
    """Return every currently-open FORECAST_OVERSELL alert for a property.

    Queries the ``propertyId-status-index`` GSI for the property's open
    (:data:`OPEN_ALERT_STATUS`) records and narrows to the predictive
    (:data:`FORECAST_ALERT_TYPE`) type server-side, so reactive alerts are never
    returned and can never be resolved by the forecast run (Req 7.5).

    Args:
        table: The bound ``pulse-alerts`` table resource (from the table getter).
        property_id: The bound run's Property identifier (the GSI partition and
            data-isolation boundary, Req 6.2).

    Returns:
        The list of open predictive items for the property (possibly empty).
    """
    # Query the GSI for this property's open records, filtered to the forecast
    # type only. This mirrors _find_open_alert_for_dedupe_key but returns the
    # full open predictive set (no dedupeKey narrowing) for the diff.
    response = table.query(
        IndexName=PROPERTY_STATUS_INDEX_NAME,
        KeyConditionExpression=(
            Key(ATTR_PROPERTY_ID).eq(property_id)
            & Key(ATTR_STATUS).eq(OPEN_ALERT_STATUS)
        ),
        FilterExpression=Attr(ATTR_TYPE).eq(FORECAST_ALERT_TYPE),
    )
    return list(response.get("Items", []))


def _resolve_alert(
    table: Any,
    alert_id: str,
    now: str,
) -> bool:
    """Transition one open predictive alert to RESOLVED, exactly once.

    Performs a conditional ``UpdateItem`` that sets ``status`` to
    :data:`RESOLVED_ALERT_STATUS` and refreshes ``lastStatusChangeAt``, guarded
    on the record still being open (:data:`OPEN_ALERT_STATUS`). The guard makes
    the resolve idempotent: a record already resolved (by a prior run or a
    concurrent writer) fails the condition and is left untouched, so exactly one
    RESOLVED transition is applied per alert (Req 7.5).

    Args:
        table: The bound ``pulse-alerts`` table resource.
        alert_id: The id of the open predictive record to resolve.
        now: ISO 8601 UTC timestamp recorded as the new ``lastStatusChangeAt``.

    Returns:
        ``True`` if this call performed the RESOLVED transition, ``False`` if the
        record was no longer open (already terminal) and was left unchanged.

    Raises:
        botocore exceptions from ``update_item`` other than the guarded
        conditional failure propagate to the caller.
    """
    try:
        # Only resolve while still open; "status" is reserved so it is aliased.
        table.update_item(
            Key={ATTR_ALERT_ID: alert_id},
            UpdateExpression="SET #status = :resolved, #lastStatusChangeAt = :ts",
            ConditionExpression=Attr(ATTR_STATUS).eq(OPEN_ALERT_STATUS),
            ExpressionAttributeNames={
                "#status": ATTR_STATUS,
                "#lastStatusChangeAt": ATTR_LAST_STATUS_CHANGE_AT,
            },
            ExpressionAttributeValues={
                ":resolved": RESOLVED_ALERT_STATUS,
                ":ts": now,
            },
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Already terminal (resolved by a prior run or concurrent writer); leave
        # it unchanged so the resolve happens exactly once (Req 7.5).
        logger.info(
            "Predictive alert already terminal before resolve; skipping "
            "(idempotent) (alertId=%s)",
            alert_id,
        )
        return False


def reconcile_resolutions(
    predicted_dedupe_keys: set[str],
    *,
    property_id: str,
    alerts_table_name: str,
    now: Optional[str] = None,
    table_getter: Callable[[str], Any] = get_table,
    realtime_publisher: Optional["rt.PublisherFn"] = None,
) -> list[Mapping[str, Any]]:
    """Resolve open predictive alerts no longer forecast this run (Req 7.5).

    Diffs the currently-open FORECAST_OVERSELL alerts for ``property_id`` (read
    from the ``propertyId-status-index`` GSI) against the set of ``dedupeKey``
    values predicted THIS run. Every open predictive alert whose ``dedupeKey`` is
    NOT in ``predicted_dedupe_keys`` is transitioned to RESOLVED exactly once via
    a conditional ``UpdateItem`` (guarded on still-open), then an
    ``ALERT_RESOLVED`` event is published best-effort for it (Req 4.5, 4.6).
    Only FORECAST_OVERSELL-type alerts are considered, so reactive alerts are
    never resolved. Delivery is post-commit and never rolls back a resolve.

    Args:
        predicted_dedupe_keys: The ``dedupeKey`` values predicted this run
            (typically the keys upserted by :func:`reconcile_alerts`); any open
            predictive alert whose key is absent here is stale and gets resolved.
        property_id: The bound run's Property identifier (GSI partition and
            data-isolation boundary, Req 6.2).
        alerts_table_name: The ``pulse-alerts`` physical table name (from
            ``ALERTS_TABLE_NAME`` config); never hardcoded (PYQUALITY-06).
        now: ISO 8601 UTC timestamp recorded as ``lastStatusChangeAt`` on each
            resolve. When ``None``, the current UTC time is used. Injectable for
            deterministic tests.
        table_getter: Table-resource getter seam (injectable for tests);
            defaults to :func:`pulse.common.dynamo.get_table`.
        realtime_publisher: Injectable realtime publisher seam passed through to
            :func:`publish_alert_event`; a failing publisher never fails a
            resolve (Req 4.6).

    Returns:
        The list of items that were transitioned to RESOLVED this call (each
        already reflecting the RESOLVED status and new timestamp), in GSI order.
        Empty when nothing was stale.
    """
    table = table_getter(alerts_table_name)
    timestamp = now if now is not None else datetime.now(timezone.utc).isoformat()

    open_alerts = _find_open_forecast_alerts(table, property_id)
    resolved_items: list[Mapping[str, Any]] = []
    for alert in open_alerts:
        dedupe_key = alert.get(ATTR_DEDUPE_KEY)
        # Still forecast this run: leave the open alert as-is.
        if dedupe_key in predicted_dedupe_keys:
            continue
        alert_id = str(alert[ATTR_ALERT_ID])
        # Transition to RESOLVED exactly once (conditional on still-open).
        if not _resolve_alert(table, alert_id, timestamp):
            continue
        logger.info(
            "Resolved no-longer-forecast predictive alert (propertyId=%s "
            "dedupeKey=%s alertId=%s)",
            property_id,
            dedupe_key,
            alert_id,
        )
        # Build the post-resolve item for the event so the payload reflects the
        # committed RESOLVED status / timestamp, then publish best-effort.
        resolved_item: dict[str, Any] = {
            **alert,
            ATTR_STATUS: RESOLVED_ALERT_STATUS,
            ATTR_LAST_STATUS_CHANGE_AT: timestamp,
        }
        publish_alert_event(
            resolved_item,
            rt.EVENT_ALERT_RESOLVED,
            realtime_publisher=realtime_publisher,
        )
        resolved_items.append(resolved_item)
    return resolved_items


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a full :func:`reconcile_alerts` run.

    Attributes:
        upserts: The per-prediction upsert outcomes, in input order (each an
            :class:`UpsertResult` carrying created-vs-updated and the alertId).
        resolved: The items transitioned to RESOLVED because they were no longer
            forecast this run (Req 7.5).
    """

    upserts: list[UpsertResult]
    resolved: list[Mapping[str, Any]]


def reconcile_alerts(
    predictions: Sequence[Mapping[str, Any]],
    *,
    property_id: str,
    alerts_table_name: str,
    now: Optional[str] = None,
    table_getter: Callable[[str], Any] = get_table,
    realtime_publisher: Optional["rt.PublisherFn"] = None,
) -> ReconcileResult:
    """Upsert this run's actionable predictions, then resolve the stale ones.

    Composable orchestration over the task 8.3 upsert and the task 8.4 resolve,
    with post-commit best-effort delivery woven in (Req 4.5, 4.6, 7.5):

    1. For each prediction, :func:`upsert_forecast_alert` creates or updates the
       predictive record in place, then an ``ALERT_UPDATED`` event is published
       best-effort for the persisted item.
    2. The set of predicted ``dedupeKey`` values is diffed against the open
       predictive alerts via :func:`reconcile_resolutions`, resolving those no
       longer forecast and publishing one ``ALERT_RESOLVED`` best-effort each.

    Every publish is post-commit and swallowed on failure, so neither an upsert
    nor a resolve is ever rolled back by a delivery hiccup (Req 4.6).

    Args:
        predictions: The threshold-meeting predictions to write, each a mapping
            carrying its assembled ``item`` (from
            :func:`build_forecast_alert_item`) under ``item`` and its
            ``dedupeKey`` under ``dedupe_key`` / ``dedupeKey``.
        property_id: The bound run's Property identifier (data-isolation
            boundary, Req 6.2).
        alerts_table_name: The ``pulse-alerts`` physical table name; never
            hardcoded (PYQUALITY-06).
        now: ISO 8601 UTC timestamp used for update / resolve timestamps. When
            ``None``, the current UTC time is used. Injectable for tests.
        table_getter: Table-resource getter seam (injectable for tests).
        realtime_publisher: Injectable realtime publisher seam; a failing
            publisher never fails an upsert or a resolve (Req 4.6).

    Returns:
        A :class:`ReconcileResult` with the per-prediction upsert outcomes and
        the items resolved because they were no longer forecast.
    """
    upserts: list[UpsertResult] = []
    predicted_dedupe_keys: set[str] = set()
    for prediction in predictions:
        # Accept either snake_case or camelCase for the key alongside the item.
        dedupe_key = prediction.get("dedupe_key")
        if dedupe_key is None:
            dedupe_key = prediction.get("dedupeKey")
        item = prediction["item"]
        result = upsert_forecast_alert(
            item,
            dedupe_key=str(dedupe_key),
            property_id=property_id,
            alerts_table_name=alerts_table_name,
            now=now,
            table_getter=table_getter,
        )
        upserts.append(result)
        predicted_dedupe_keys.add(str(dedupe_key))
        # Post-commit best-effort ALERT_UPDATED for the persisted record. Use the
        # authoritative alertId so the event points at the retained record on an
        # update-in-place.
        published_item: dict[str, Any] = {**item, ATTR_ALERT_ID: result.alert_id}
        publish_alert_event(
            published_item,
            rt.EVENT_ALERT_UPDATED,
            realtime_publisher=realtime_publisher,
        )

    resolved = reconcile_resolutions(
        predicted_dedupe_keys,
        property_id=property_id,
        alerts_table_name=alerts_table_name,
        now=now,
        table_getter=table_getter,
        realtime_publisher=realtime_publisher,
    )
    return ReconcileResult(upserts=upserts, resolved=resolved)


__all__ = [
    "DedupeKeyError",
    "derive_dedupe_key",
    "dedupe_key_for_prediction",
    "meets_threshold",
    "severity_for_confidence",
    "build_forecast_alert_item",
    "UpsertResult",
    "upsert_forecast_alert",
    "RESOLVED_ALERT_STATUS",
    "publish_alert_event",
    "reconcile_resolutions",
    "ReconcileResult",
    "reconcile_alerts",
]
