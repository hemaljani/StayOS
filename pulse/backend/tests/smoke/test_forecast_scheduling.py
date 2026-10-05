"""Smoke tests over the forecaster scheduling + invoker wiring (Task 14.3).

Single-execution CloudFormation assertions on ``pulse-pipeline`` covering the
per-property daily forecaster schedules and their invoker target:

    * exactly five ``AWS::Scheduler::Schedule`` resources belong to the
      forecaster ``PulseForecasterScheduleGroup`` (Req 1.1),
    * each fires daily at midnight local time (cron), with
      ``FlexibleTimeWindow`` OFF and a Target ``RetryPolicy`` of 3 attempts /
      3600s max event age (Req 1.6),
    * the five ``ScheduleExpressionTimezone`` values cover the five pilot IANA
      zones exactly once each, correctly paired with their propertyIds
      (Req 1.1, 1.3),
    * every schedule targets the ``PulseForecasterInvokerFunction`` via
      ``!GetAtt ...Arn`` and carries the matching propertyId in Target.Input,
    * the invoker function is defined with the expected name, runtime ARN env
      var, and active tracing,
    * the forecaster scheduler role trusts ``scheduler.amazonaws.com`` with an
      ``aws:SourceAccount`` condition and grants ``lambda:InvokeFunction`` on
      only the invoker (Req 1.6),
    * the template parses to a structurally sane document (parse-clean check).
"""

from __future__ import annotations

import json
from typing import Any

# Logical ID of the dedicated forecaster schedule group.
_FORECASTER_GROUP_ID = "PulseForecasterScheduleGroup"

# Logical ID of the Lambda every forecaster schedule targets.
_INVOKER_FUNCTION_ID = "PulseForecasterInvokerFunction"

# Logical ID of the scheduler-assumed role that invokes the target.
_SCHEDULER_ROLE_ID = "PulseForecasterSchedulerRole"

# Daily-at-midnight cron every forecaster schedule must use.
_EXPECTED_CRON = "cron(0 0 * * ? *)"

# The five pilot properties paired with their IANA timezone (Req 1.1, 1.3).
_EXPECTED_PROPERTY_TIMEZONES = {
    "ALOHA-CHI-001": "America/Chicago",
    "ALOHA-MIA-001": "America/New_York",
    "ALOHA-TYO-001": "Asia/Tokyo",
    "ALOHA-MAD-001": "Europe/Madrid",
    "ALOHA-BOM-001": "Asia/Kolkata",
}


def _resources(template: dict[str, Any]) -> dict[str, Any]:
    """Return the ``Resources`` block of a parsed template.

    Args:
        template: A parsed CloudFormation template.

    Returns:
        The resources mapping (logical id -> resource).
    """
    return template.get("Resources", {})


def _ref_target(node: Any) -> Any:
    """Return the logical id referenced by a ``{"Ref": ...}`` passthrough.

    Args:
        node: A parsed value that may be a ``{"Ref": "<LogicalId>"}`` dict.

    Returns:
        The referenced logical id, or ``None`` if the node is not a Ref.
    """
    if isinstance(node, dict) and "Ref" in node:
        return node["Ref"]
    return None


def _getatt_target(node: Any) -> tuple[str, str] | None:
    """Return the ``(logicalId, attribute)`` of a ``!GetAtt`` passthrough.

    The CFN loader represents ``!GetAtt A.Arn`` as either
    ``{"GetAtt": ["A", "Arn"]}`` (sequence form) or ``{"GetAtt": "A.Arn"}``
    (scalar dotted form); both are handled.

    Args:
        node: A parsed value that may be a ``GetAtt`` passthrough dict.

    Returns:
        A ``(logical_id, attribute)`` tuple, or ``None`` if not a GetAtt.
    """
    if not isinstance(node, dict) or "GetAtt" not in node:
        return None
    body = node["GetAtt"]
    if isinstance(body, list) and len(body) == 2:
        return str(body[0]), str(body[1])
    if isinstance(body, str) and "." in body:
        logical, _, attr = body.partition(".")
        return logical, attr
    return None


def _forecaster_schedules(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the schedule resources belonging to the forecaster group.

    Filters ``AWS::Scheduler::Schedule`` resources down to those whose
    ``GroupName`` references ``PulseForecasterScheduleGroup``, excluding the
    other schedules in the same template (escalation, info-batch, sweeper,
    demo simulator) that live in the pipeline group.

    Args:
        template: A parsed CloudFormation template.

    Returns:
        A mapping of logical id -> resource for the forecaster schedules.
    """
    schedules: dict[str, dict[str, Any]] = {}
    for logical_id, resource in _resources(template).items():
        if resource.get("Type") != "AWS::Scheduler::Schedule":
            continue
        props = resource.get("Properties", {})
        if _ref_target(props.get("GroupName")) == _FORECASTER_GROUP_ID:
            schedules[logical_id] = resource
    return schedules


def test_exactly_five_forecaster_schedules(pulse_pipeline_template: dict[str, Any]) -> None:
    """Exactly five schedules belong to the forecaster group (Req 1.1)."""
    schedules = _forecaster_schedules(pulse_pipeline_template)
    assert len(schedules) == 5, (
        f"expected 5 forecaster-group schedules, found {len(schedules)}: "
        f"{sorted(schedules)}"
    )


def test_each_schedule_cron_flexwindow_and_retry(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """Each forecaster schedule uses daily cron, OFF window, 3/3600 retry (Req 1.6)."""
    schedules = _forecaster_schedules(pulse_pipeline_template)
    assert schedules, "no forecaster schedules found"

    for logical_id, resource in schedules.items():
        props = resource.get("Properties", {})

        assert props.get("ScheduleExpression") == _EXPECTED_CRON, (
            f"{logical_id}: unexpected ScheduleExpression "
            f"{props.get('ScheduleExpression')!r}"
        )

        flex = props.get("FlexibleTimeWindow", {})
        assert flex.get("Mode") == "OFF", (
            f"{logical_id}: FlexibleTimeWindow.Mode must be OFF, got {flex.get('Mode')!r}"
        )

        retry = props.get("Target", {}).get("RetryPolicy", {})
        assert retry.get("MaximumRetryAttempts") == 3, (
            f"{logical_id}: MaximumRetryAttempts must be 3, got "
            f"{retry.get('MaximumRetryAttempts')!r}"
        )
        assert retry.get("MaximumEventAgeInSeconds") == 3600, (
            f"{logical_id}: MaximumEventAgeInSeconds must be 3600, got "
            f"{retry.get('MaximumEventAgeInSeconds')!r}"
        )


def test_timezone_set_matches_pilot_zones(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """The five timezones cover the pilot IANA zones exactly once each (Req 1.3)."""
    schedules = _forecaster_schedules(pulse_pipeline_template)

    # ScheduleExpressionTimezone values are literal strings in the template; a
    # !Sub would parse to a dict, so guard for both shapes defensively.
    timezones = []
    for resource in schedules.values():
        tz_node = resource.get("Properties", {}).get("ScheduleExpressionTimezone")
        if isinstance(tz_node, dict) and "Sub" in tz_node:
            body = tz_node["Sub"]
            tz_node = body[0] if isinstance(body, list) else body
        timezones.append(tz_node)

    expected_zones = set(_EXPECTED_PROPERTY_TIMEZONES.values())
    assert set(timezones) == expected_zones, (
        f"timezone set mismatch: got {sorted(timezones)}, "
        f"expected {sorted(expected_zones)}"
    )
    # Each zone appears exactly once (no duplicates collapsing the set check).
    assert len(timezones) == len(expected_zones) == 5


def test_each_schedule_targets_invoker_with_matching_property(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """Each schedule targets the invoker and pairs propertyId to timezone (Req 1.1)."""
    schedules = _forecaster_schedules(pulse_pipeline_template)

    observed_pairs: dict[str, str] = {}
    for logical_id, resource in schedules.items():
        target = resource.get("Properties", {}).get("Target", {})

        # Target.Arn must be a GetAtt on the invoker function's Arn.
        assert _getatt_target(target.get("Arn")) == (_INVOKER_FUNCTION_ID, "Arn"), (
            f"{logical_id}: Target.Arn must GetAtt {_INVOKER_FUNCTION_ID}.Arn, "
            f"got {target.get('Arn')!r}"
        )

        # Target.Input is a JSON string carrying the propertyId.
        raw_input = target.get("Input")
        assert isinstance(raw_input, str), (
            f"{logical_id}: Target.Input must be a JSON string, got {type(raw_input)}"
        )
        parsed = json.loads(raw_input)
        property_id = parsed.get("propertyId")
        assert property_id, f"{logical_id}: Target.Input missing propertyId"

        tz_node = resource.get("Properties", {}).get("ScheduleExpressionTimezone")
        observed_pairs[property_id] = tz_node

    # The five propertyIds match the expected set, and each is paired with its
    # designed timezone.
    assert observed_pairs == _EXPECTED_PROPERTY_TIMEZONES, (
        f"propertyId/timezone pairing mismatch: got {observed_pairs}, "
        f"expected {_EXPECTED_PROPERTY_TIMEZONES}"
    )


def test_invoker_function_definition(pulse_pipeline_template: dict[str, Any]) -> None:
    """The invoker function has the expected name, runtime-arn env, tracing."""
    invoker = _resources(pulse_pipeline_template).get(_INVOKER_FUNCTION_ID)
    assert invoker is not None, f"{_INVOKER_FUNCTION_ID} not found"
    assert invoker.get("Type") == "AWS::Lambda::Function"

    props = invoker.get("Properties", {})

    # FunctionName is a !Sub over ${StackPrefix}-forecaster-invoker.
    name_node = props.get("FunctionName")
    name_body = name_node["Sub"] if isinstance(name_node, dict) else name_node
    assert "forecaster-invoker" in str(name_body), (
        f"unexpected FunctionName {name_node!r}"
    )

    env_vars = props.get("Environment", {}).get("Variables", {})
    assert "FORECAST_AGENT_RUNTIME_ARN" in env_vars, (
        "invoker missing FORECAST_AGENT_RUNTIME_ARN env var"
    )

    assert props.get("TracingConfig", {}).get("Mode") == "Active", (
        "invoker TracingConfig.Mode must be Active"
    )


def test_scheduler_role_trust_and_scoped_invoke(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """The scheduler role trusts scheduler + scopes invoke to the invoker (Req 1.6)."""
    role = _resources(pulse_pipeline_template).get(_SCHEDULER_ROLE_ID)
    assert role is not None, f"{_SCHEDULER_ROLE_ID} not found"
    assert role.get("Type") == "AWS::IAM::Role"

    props = role.get("Properties", {})

    # Trust policy: scheduler.amazonaws.com principal with SourceAccount guard.
    trust_stmt = props["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust_stmt["Principal"]["Service"] == "scheduler.amazonaws.com"
    condition = trust_stmt.get("Condition", {}).get("StringEquals", {})
    assert "aws:SourceAccount" in condition, (
        "trust policy missing aws:SourceAccount condition"
    )

    # Permissions policy: lambda:InvokeFunction on only the invoker (one resource).
    statements = props["Policies"][0]["PolicyDocument"]["Statement"]
    invoke_statements = [s for s in statements if s.get("Action") == "lambda:InvokeFunction"]
    assert len(invoke_statements) == 1, "expected a single InvokeFunction statement"

    resources = invoke_statements[0]["Resource"]
    if not isinstance(resources, list):
        resources = [resources]
    assert len(resources) == 1, "invoke grant must target exactly one resource"

    # The single resource is the invoker ARN (built by !Sub over its name).
    resource_node = resources[0]
    resource_body = (
        resource_node["Sub"] if isinstance(resource_node, dict) else resource_node
    )
    assert "forecaster-invoker" in str(resource_body), (
        f"invoke grant must scope to the invoker, got {resource_node!r}"
    )


def test_template_parses_clean(pulse_pipeline_template: dict[str, Any]) -> None:
    """Parse-sanity: the pipeline template loaded into a structured document."""
    assert isinstance(pulse_pipeline_template, dict), "template did not parse to a dict"
    resources = pulse_pipeline_template.get("Resources")
    assert isinstance(resources, dict) and resources, (
        "template has no Resources block"
    )
