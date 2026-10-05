"""RegenerateBrief step Lambda for the StayOS Unified Data Orchestrator.

After Un-Quiesce, trigger LUMI brief regeneration for each rolled-forward
property by reusing the existing LUMI orchestrator ``generate-single`` entry.
The brief therefore reads the freshly re-anchored (post-Roll-Forward) data for
that property (Requirements 4.1, 4.2).

Two guards protect the demo-ready invariant:

* Brief_Ordering_Guard - before invoking, compute (from the property's
  ``schedule_manager`` metadata: IANA timezone + delivery_time) whether the
  Roll-Forward completes before that property's next GM brief cron. On a breach
  we log a warning identifying the property and proceed without corrupting data
  (Requirements 3.3, 3.4).
* Brief-failure isolation - the LUMI orchestrator is invoked via a boto3 Lambda
  ``Invoke`` (never by importing LUMI code, to respect the feature boundary).
  On invoke or pipeline failure we log with ``propertyId`` context and return a
  ``failed`` step result WITHOUT raising in a way that rolls back the
  already-written operational data (Requirement 4.3), while still surfacing the
  failure in the result envelope so it is not masked (Requirement 9.3).

The LUMI orchestrator function name and the settings table name come from
environment variables (PYQUALITY-06 / NAMING); nothing is hardcoded.

Satisfies: Requirements 3.3, 3.4, 4.1, 4.2, 4.3, 9.1, 9.2, 9.3.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from dataset_generator.config import PROPERTY_IDS
from orchestrator_common import (
    SERVICE_NAME,
    STATUS_FAILED,
    STATUS_OK,
    StepInput,
    build_step_result,
    parse_step_input,
    resolve_target_properties,
)

logger = Logger(service=SERVICE_NAME)

STEP_NAME = "RegenerateBrief"

# Full pilot estate, sourced from the generator config (never hardcoded here)
# so seed fan-out stays in sync with the rest of the orchestrator.
PILOT_PROPERTY_IDS: List[str] = list(PROPERTY_IDS)

# Default GM brief delivery time when a property's settings omit it. Mirrors the
# LUMI orchestrator/historical-briefs default of 06:30 local.
DEFAULT_BRIEF_DELIVERY_TIME = "06:30"
# Default IANA timezone when a property's settings omit one.
DEFAULT_TIMEZONE = "UTC"

# Environment variable names (never hardcode the physical identifiers).
LUMI_ORCHESTRATOR_FUNCTION_ENV = "LUMI_ORCHESTRATOR_FUNCTION_NAME"
LUMI_ORCHESTRATOR_ARN_ENV = "ORCHESTRATOR_ARN"
SETTINGS_TABLE_ENV = "SETTINGS_TABLE_NAME"

# Explicit standard retries so a transient LUMI/DynamoDB blip is retried rather
# than failing the brief step on first error (PYQUALITY-06).
_boto_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
)

# Module-level clients for connection reuse across warm invocations
# (PYQUALITY-06). Created lazily so unit tests can inject mocks/moto first.
_lambda_client: Optional[Any] = None
_dynamodb_resource: Optional[Any] = None


def _get_lambda_client() -> Any:
    """Return the shared Lambda client, creating it on first use.

    Returns:
        A boto3 Lambda client configured with standard retries.
    """
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", config=_boto_config)
    return _lambda_client


def _get_settings_table() -> Any:
    """Return the DynamoDB settings table resource, resolved from the environment.

    Returns:
        A boto3 DynamoDB ``Table`` bound to the configured settings table.

    Raises:
        BriefRegenerationConfigError: If ``SETTINGS_TABLE_NAME`` is unset.
    """
    global _dynamodb_resource
    table_name = os.environ.get(SETTINGS_TABLE_ENV)
    if not table_name:
        raise BriefRegenerationConfigError(
            f"environment variable {SETTINGS_TABLE_ENV} is not set"
        )
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb", config=_boto_config)
    return _dynamodb_resource.Table(table_name)


class BriefRegenerationConfigError(RuntimeError):
    """Raised when required brief-regeneration configuration is missing.

    A domain-specific exception (PYQUALITY-02) lets the state machine
    distinguish a misconfiguration (missing function name / table) from a
    downstream LUMI or DynamoDB failure.
    """


@dataclass
class PropertySchedule:
    """A property's GM brief schedule metadata, as ``schedule_manager`` reads it.

    Attributes:
        gm_alias: The GM's unique alias (partition key of ``stayos-settings``);
            also the ``gmAlias`` in the ``generate-single`` payload.
        property_id: The property identifier the brief is generated for.
        delivery_time: Preferred brief delivery time in ``HH:MM`` (the same
            field ``schedule_manager`` turns into the brief cron).
        timezone: IANA timezone string (e.g. ``America/Chicago``); the cron's
            ``ScheduleExpressionTimezone``.
    """

    gm_alias: str
    property_id: str
    delivery_time: str
    timezone: str


def resolve_lumi_orchestrator_function() -> str:
    """Resolve the LUMI orchestrator function name/ARN from the environment.

    Prefers :data:`LUMI_ORCHESTRATOR_FUNCTION_ENV`; falls back to
    :data:`LUMI_ORCHESTRATOR_ARN_ENV` (the same variable ``schedule_manager``
    targets) so the orchestrator and the per-GM schedules stay in sync.

    Returns:
        The LUMI orchestrator Lambda function name or ARN.

    Raises:
        BriefRegenerationConfigError: If neither variable is set.
    """
    function_ref = os.environ.get(LUMI_ORCHESTRATOR_FUNCTION_ENV) or os.environ.get(
        LUMI_ORCHESTRATOR_ARN_ENV
    )
    if not function_ref:
        raise BriefRegenerationConfigError(
            f"set {LUMI_ORCHESTRATOR_FUNCTION_ENV} or {LUMI_ORCHESTRATOR_ARN_ENV} "
            "to the LUMI orchestrator function name/ARN"
        )
    return function_ref


def _load_zoneinfo(timezone_str: str) -> ZoneInfo:
    """Load a ZoneInfo, falling back to UTC on an unknown timezone.

    Args:
        timezone_str: An IANA timezone string.

    Returns:
        A :class:`~zoneinfo.ZoneInfo` for ``timezone_str`` or UTC if unknown.
    """
    try:
        return ZoneInfo(timezone_str)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "unknown IANA timezone; defaulting to UTC for the ordering guard",
            extra={"timezone": timezone_str},
        )
        return ZoneInfo(DEFAULT_TIMEZONE)


def _delivery_datetime_on(day: date, delivery_time: str, tz: ZoneInfo) -> datetime:
    """Build the local brief-delivery datetime for a given day.

    Args:
        day: The local calendar date the brief would be delivered on.
        delivery_time: Brief delivery time in ``HH:MM``.
        tz: The property's timezone.

    Returns:
        A timezone-aware ``datetime`` at ``delivery_time`` on ``day``.

    Raises:
        ValueError: If ``delivery_time`` is not a well-formed ``HH:MM`` string
            with in-range hour (0-23) and minute (0-59). Raised explicitly so a
            malformed settings value is a clear, catchable error rather than an
            opaque unpacking/``int`` failure (review finding CR-4).
    """
    parts = delivery_time.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"briefDeliveryTime must be 'HH:MM'; got {delivery_time!r}"
        )
    hour_str, minute_str = parts
    try:
        hour, minute = int(hour_str), int(minute_str)
    except ValueError as exc:
        raise ValueError(
            f"briefDeliveryTime must be numeric 'HH:MM'; got {delivery_time!r}"
        ) from exc
    # time() enforces 0<=hour<=23 and 0<=minute<=59, rejecting e.g. '24:00'.
    fire_time = time(hour=hour, minute=minute)
    return datetime.combine(day, fire_time, tzinfo=tz)


def next_brief_cron_fire(
    completion: datetime, delivery_time: str, timezone_str: str
) -> datetime:
    """Compute the property's next GM brief cron fire at/after ``completion``.

    ``schedule_manager`` builds a daily cron (``cron(minute hour * * ? *)``)
    with ``ScheduleExpressionTimezone`` set to the property's IANA timezone, so
    the brief fires every day at ``delivery_time`` local. This returns the first
    such local fire time that is at or after ``completion``.

    Args:
        completion: The instant the Roll-Forward completes (timezone-aware).
        delivery_time: Brief delivery time in ``HH:MM``.
        timezone_str: The property's IANA timezone.

    Returns:
        A timezone-aware ``datetime`` (in the property's zone) of the next fire.
    """
    tz = _load_zoneinfo(timezone_str)
    completion_local = completion.astimezone(tz)
    candidate = _delivery_datetime_on(completion_local.date(), delivery_time, tz)
    if candidate < completion_local:
        # Today's brief already fired before completion; the next one is tomorrow.
        candidate = _delivery_datetime_on(
            completion_local.date() + timedelta(days=1), delivery_time, tz
        )
    return candidate


def is_ordering_guard_satisfied(
    completion: datetime, delivery_time: str, timezone_str: str
) -> bool:
    """Return whether Roll-Forward completes before that day's GM brief cron.

    The Brief_Ordering_Guard holds when the property's Roll-Forward completion
    instant is strictly before the property's brief cron on the completion's
    local date (Requirement 3.3). If completion lands at or after today's local
    delivery time, this morning's brief has already fired (or fires now) on
    pre-Roll-Forward data - a breach. The comparison is done in the property's
    local zone so an IANA timezone + ``delivery_time`` (the real metadata shape
    ``schedule_manager`` uses) drives it.

    Args:
        completion: The instant the Roll-Forward completes (timezone-aware).
        delivery_time: Brief delivery time in ``HH:MM``.
        timezone_str: The property's IANA timezone.

    Returns:
        ``True`` if completion precedes today's brief cron; ``False`` on breach.
    """
    tz = _load_zoneinfo(timezone_str)
    completion_local = completion.astimezone(tz)
    todays_cron = _delivery_datetime_on(completion_local.date(), delivery_time, tz)
    return completion_local < todays_cron


def check_ordering_guard(schedule: PropertySchedule, completion: datetime) -> bool:
    """Evaluate the ordering guard for one property and warn on breach.

    On a breach the orchestrator logs a warning identifying the property and
    proceeds without corrupting data (Requirement 3.4); it never blocks the
    brief regeneration.

    Args:
        schedule: The property's brief schedule metadata.
        completion: The instant the Roll-Forward completes (timezone-aware).

    Returns:
        ``True`` if the guard holds; ``False`` if it was breached.
    """
    satisfied = is_ordering_guard_satisfied(
        completion, schedule.delivery_time, schedule.timezone
    )
    if not satisfied:
        logger.warning(
            "Brief_Ordering_Guard breach: roll-forward may not complete before "
            "the property's GM brief cron; proceeding without corrupting data",
            extra={
                "propertyId": schedule.property_id,
                "gmAlias": schedule.gm_alias,
                "deliveryTime": schedule.delivery_time,
                "timezone": schedule.timezone,
                "completion": completion.isoformat(),
            },
        )
    return satisfied


def _read_property_schedule(property_id: str) -> Optional[PropertySchedule]:
    """Read a property's GM brief schedule metadata from ``stayos-settings``.

    Reads the same source ``schedule_manager`` builds the brief cron from: the
    settings item keyed by ``gmAlias`` carrying ``propertyId``, ``timezone``, and
    ``briefDeliveryTime``. The property is located via a fully paginated settings
    table scan (the table's partition key is ``gmAlias``, not ``propertyId``, and
    the data model defines no ``propertyId`` GSI). Pagination matters even at
    pilot scale because a Scan ``FilterExpression`` is applied only after each
    1 MB page is read, so the match can legitimately land past page 1.

    Args:
        property_id: The property whose GM schedule metadata to resolve.

    Returns:
        A :class:`PropertySchedule`, or ``None`` if no settings item matches.

    Raises:
        BriefRegenerationConfigError: If the settings table is not configured.
    """
    table = _get_settings_table()
    # Pilot scale (single-digit GMs), but DynamoDB applies a Scan
    # FilterExpression only AFTER reading each 1 MB page, so a single
    # table.scan(...) can return an empty page while a matching item sits on a
    # later page - silently reporting "no GM settings" for a real property
    # (review finding CR-3). Paginate the scan and collect every match across
    # all pages instead of trusting page 1. (The settings table's partition key
    # is gmAlias and the data model defines no propertyId GSI, so a Query is not
    # available without a schema change.)
    matches: List[Dict[str, Any]] = []
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "propertyId = :pid",
        "ExpressionAttributeValues": {":pid": property_id},
    }
    try:
        while True:
            response = table.scan(**scan_kwargs)
            matches.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
    except ClientError as error:
        logger.error(
            "failed to read GM settings for ordering guard / gmAlias resolution",
            extra={"propertyId": property_id, "error": str(error)},
        )
        return None

    if not matches:
        logger.warning(
            "no GM settings found for property; cannot resolve gmAlias/schedule",
            extra={"propertyId": property_id},
        )
        return None

    if len(matches) > 1:
        # More than one GM alias maps to this property. Pick deterministically
        # (lowest gmAlias) and warn so the ambiguity is visible rather than
        # depending on scan/page ordering.
        matches.sort(key=lambda entry: str(entry.get("gmAlias", "")))
        logger.warning(
            "multiple GM settings rows match property; using the lowest gmAlias",
            extra={
                "propertyId": property_id,
                "matchCount": len(matches),
                "gmAliases": [str(entry.get("gmAlias", "")) for entry in matches],
            },
        )

    item = matches[0]
    return PropertySchedule(
        gm_alias=str(item.get("gmAlias", "")),
        property_id=property_id,
        delivery_time=str(item.get("briefDeliveryTime", DEFAULT_BRIEF_DELIVERY_TIME)),
        timezone=str(item.get("timezone", DEFAULT_TIMEZONE)),
    )


def _invoke_generate_single(function_ref: str, gm_alias: str, property_id: str) -> None:
    """Invoke the LUMI orchestrator ``generate-single`` for one property.

    Mirrors the ``schedule_manager`` payload
    ``{"source","action":"generate-single","gmAlias","propertyId"}`` but calls
    the LUMI orchestrator through a boto3 Lambda ``Invoke`` (RequestResponse) so
    a pipeline failure is observable and can be surfaced. LUMI's orchestrator
    code is never imported, respecting the feature boundary.

    Args:
        function_ref: The LUMI orchestrator function name or ARN.
        gm_alias: The GM alias for the target property.
        property_id: The target property identifier.

    Raises:
        BriefRegenerationError: If the invoke fails at transport level or the
            LUMI pipeline returns a non-2xx ``statusCode``.
    """
    payload = {
        "source": "scheduler",
        "action": "generate-single",
        "gmAlias": gm_alias,
        "propertyId": property_id,
    }
    try:
        response = _get_lambda_client().invoke(
            FunctionName=function_ref,
            # RequestResponse (synchronous) so brief failures surface in the
            # step result rather than being fire-and-forget.
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as error:
        raise BriefRegenerationError(
            f"Lambda invoke of LUMI generate-single failed for {property_id}: {error}"
        ) from error

    # A function-level error (unhandled exception in LUMI) is signalled by
    # FunctionError; the pipeline's own failure is signalled by statusCode.
    function_error = response.get("FunctionError")
    raw_payload = response.get("Payload")
    body = raw_payload.read().decode("utf-8") if raw_payload is not None else ""

    if function_error:
        raise BriefRegenerationError(
            f"LUMI generate-single raised {function_error} for {property_id}: {body}"
        )

    result = json.loads(body) if body else {}
    status_code = result.get("statusCode")
    if status_code is not None and int(status_code) >= 400:
        raise BriefRegenerationError(
            f"LUMI generate-single returned {status_code} for {property_id}: "
            f"{result.get('error', 'unknown error')}"
        )


class BriefRegenerationError(RuntimeError):
    """Raised when a single property's brief regeneration fails.

    Caught per property so one failure does not abort the whole step and,
    critically, never rolls back the already-written operational data
    (Requirement 4.3). The failure is still surfaced in the result envelope
    (Requirement 9.3).
    """


def regenerate_briefs(
    step_input: StepInput,
    pilot_property_ids: List[str],
    completion: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Trigger LUMI brief regeneration for the target properties.

    For each target property: resolve its ``gmAlias``/schedule from settings,
    evaluate the Brief_Ordering_Guard (warn on breach, proceed), then invoke the
    LUMI orchestrator ``generate-single``. Per-property failures are logged with
    context and collected; they never raise out of this function so written data
    is not rolled back (Requirement 4.3).

    Args:
        step_input: The parsed step input (mode, propertyId, referenceDate).
        pilot_property_ids: Full pilot property list for ``seed`` fan-out.
        completion: The Roll-Forward completion instant used for the ordering
            guard; defaults to now (UTC) when omitted.

    Returns:
        Structured detail: per-property outcomes, counts, guard breaches, and
        whether any brief failed.
    """
    targets = resolve_target_properties(step_input, pilot_property_ids)
    completion_at = completion or datetime.now(tz=ZoneInfo("UTC"))

    # Resolve the LUMI orchestrator reference defensively. A missing brief
    # configuration is an operational misconfiguration, NOT a reason to abort
    # the whole roll-forward or roll back the already-written operational data
    # (Requirement 4.3). Treat it like a brief failure: record every target as
    # failed and return a normal envelope so the state machine's RegenerateBrief
    # Catch is not even needed and PrimeBaseline still runs. The failure is
    # surfaced (not masked) in the result envelope (Requirement 9.3).
    try:
        function_ref = resolve_lumi_orchestrator_function()
    except BriefRegenerationConfigError as config_error:
        logger.error(
            "brief regeneration not configured; skipping briefs without rolling "
            "back operational data",
            extra={"error": str(config_error), **step_input.to_context()},
        )
        return {
            "targetProperties": targets,
            "briefsRequested": len(targets),
            "briefsSucceeded": 0,
            "briefsFailed": len(targets),
            "orderingGuardBreaches": [],
            "failedProperties": list(targets),
            "results": [
                {"propertyId": property_id, "status": STATUS_FAILED, "reason": "not-configured"}
                for property_id in targets
            ],
            "notConfigured": True,
        }

    logger.info(
        "triggering LUMI brief regeneration",
        extra={
            "step": STEP_NAME,
            "targetProperties": targets,
            **step_input.to_context(),
        },
    )

    results: List[Dict[str, Any]] = []
    guard_breaches: List[str] = []
    failures: List[str] = []

    for property_id in targets:
        schedule = _read_property_schedule(property_id)
        if schedule is None or not schedule.gm_alias:
            # Missing gmAlias means we cannot invoke generate-single meaningfully.
            logger.error(
                "cannot regenerate brief: unresolved gmAlias for property",
                extra={"propertyId": property_id},
            )
            failures.append(property_id)
            results.append(
                {"propertyId": property_id, "status": STATUS_FAILED, "reason": "no-gm-alias"}
            )
            continue

        # Per-property isolation (Requirement 4.3 / review finding CR-4): the
        # ordering-guard evaluation parses this property's briefDeliveryTime, so
        # a single malformed settings value must NOT abort the remaining
        # properties. Keep the guard call inside the per-property try and treat a
        # parse failure as this property's own BriefRegenerationError.
        guard_ok = False
        try:
            try:
                guard_ok = check_ordering_guard(schedule, completion_at)
            except ValueError as guard_error:
                raise BriefRegenerationError(
                    f"invalid briefDeliveryTime for {property_id}: {guard_error}"
                ) from guard_error
            if not guard_ok:
                guard_breaches.append(property_id)

            _invoke_generate_single(function_ref, schedule.gm_alias, property_id)
            results.append(
                {
                    "propertyId": property_id,
                    "gmAlias": schedule.gm_alias,
                    "status": STATUS_OK,
                    "orderingGuardOk": guard_ok,
                }
            )
        except BriefRegenerationError as error:
            # Log with property context; DO NOT re-raise in a way that rolls back
            # the already-written operational data (Requirement 4.3).
            logger.error(
                "LUMI brief regeneration failed; operational data left intact",
                extra={"propertyId": property_id, "gmAlias": schedule.gm_alias, "error": str(error)},
            )
            failures.append(property_id)
            results.append(
                {
                    "propertyId": property_id,
                    "gmAlias": schedule.gm_alias,
                    "status": STATUS_FAILED,
                    "orderingGuardOk": guard_ok,
                    "reason": str(error),
                }
            )

    return {
        "targetProperties": targets,
        "briefsRequested": len(targets),
        "briefsSucceeded": len(targets) - len(failures),
        "briefsFailed": len(failures),
        "orderingGuardBreaches": guard_breaches,
        "failedProperties": failures,
        "results": results,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin RegenerateBrief handler: parse input, delegate, wrap the result.

    The step status is ``failed`` when any property's brief did not regenerate
    so the per-execution summary does not mask it (Requirement 9.3), yet the
    handler still returns a normal envelope (no raise) so the state machine does
    not treat a brief failure as a reason to roll back data (Requirement 4.3).

    Args:
        event: Step Functions state input carrying the step contract.
        context: Lambda context object.

    Returns:
        A serialized step-result envelope for the next state.
    """
    step_input = parse_step_input(event)
    details = regenerate_briefs(step_input, pilot_property_ids=PILOT_PROPERTY_IDS)
    status = STATUS_FAILED if details["briefsFailed"] else STATUS_OK
    summary = (
        "Requested LUMI brief regeneration for rolled-forward properties"
        if status == STATUS_OK
        else f"LUMI brief regeneration failed for {details['briefsFailed']} propert(y/ies)"
    )
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary=summary,
        details=details,
        status=status,
    )


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
