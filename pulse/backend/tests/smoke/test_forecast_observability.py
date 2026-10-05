"""Smoke tests over the forecast-agent observability wiring (Task 16.2).

Single-execution CloudFormation assertions that the predictive-forecasting
agent is observable end-to-end:

    * three log metric filters on the shared ``/${StackPrefix}/pipeline`` log
      group derive the forecast run-outcome counters into ``PULSE/Pipeline``,
    * the two forecast alarms fire into the ``pulse-alarms`` SNS topic,
    * the ``pulse-overview`` dashboard renders the forecast run-outcome widget,
    * the forecaster AgentCore role, the invoker Lambda role, and the invoker
      function itself carry X-Ray tracing (TracingConfig Active + the
      ``xray:Put*`` grants) so the forecaster segment joins the single
      end-to-end trace.

Validates: Requirements 8.1, 8.4
"""

from __future__ import annotations

from typing import Any


def _sub_value(node: Any) -> str:
    """Return the string body of a ``!Sub`` node (or a plain string).

    Args:
        node: A parsed template value that may be ``{"Sub": "..."}`` or a str.

    Returns:
        The underlying string.
    """
    if isinstance(node, dict) and "Sub" in node:
        body = node["Sub"]
        return body[0] if isinstance(body, list) else str(body)
    return str(node)


def _xray_actions(role_properties: dict[str, Any]) -> set[str]:
    """Return the set of IAM actions granted across all of a role's policies.

    Args:
        role_properties: The ``Properties`` block of an ``AWS::IAM::Role``.

    Returns:
        Every action string granted by an Allow statement in the role.
    """
    actions: set[str] = set()
    for policy in role_properties.get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            # Action may be a single string or a list; normalize to a list.
            actions.update([action] if isinstance(action, str) else action)
    return actions


# ---------------------------------------------------------------------------
# Requirement 8.1 - forecast run-outcome metric filters on the pipeline group
# ---------------------------------------------------------------------------


def test_forecast_metric_filters_on_pipeline_log_group(
    pulse_observability_template: dict[str, Any],
) -> None:
    """The three forecast metric filters derive PULSE/Pipeline counters.

    Each filter lives on the shared ``/${StackPrefix}/pipeline`` log group and
    increments its named count metric in the ``PULSE/Pipeline`` namespace.

    Validates: Requirement 8.1
    """
    resources = pulse_observability_template["Resources"]

    # logical id -> expected derived metric name.
    expected_filters = {
        "ForecastRunFailureMetricFilter": "ForecastRunFailures",
        "ForecastToolExhaustedMetricFilter": "ForecastToolExhausted",
        "ForecastNoAlertMetricFilter": "ForecastNoAlert",
    }

    for logical_id, metric_name in expected_filters.items():
        assert logical_id in resources, f"missing metric filter {logical_id}"
        resource = resources[logical_id]
        assert resource["Type"] == "AWS::Logs::MetricFilter"

        props = resource["Properties"]
        assert _sub_value(props["LogGroupName"]) == "/${StackPrefix}/pipeline"

        transform = props["MetricTransformations"][0]
        assert transform["MetricName"] == metric_name
        assert transform["MetricNamespace"] == "PULSE/Pipeline"


# ---------------------------------------------------------------------------
# Requirement 8.4 - forecast alarms wired to the pulse-alarms SNS topic
# ---------------------------------------------------------------------------


def test_forecast_alarms_target_pulse_alarms_topic(
    pulse_observability_template: dict[str, Any],
) -> None:
    """Both forecast alarms are CloudWatch alarms publishing to PulseAlarmsTopic.

    Validates: Requirement 8.4
    """
    resources = pulse_observability_template["Resources"]

    # logical id -> expected physical alarm name.
    expected_alarms = {
        "PulseForecastRunFailuresAlarm": "${StackPrefix}-forecast-run-failures-high",
        "PulseForecastToolExhaustedAlarm": "${StackPrefix}-forecast-tool-exhausted-high",
    }

    for logical_id, alarm_name in expected_alarms.items():
        assert logical_id in resources, f"missing alarm {logical_id}"
        resource = resources[logical_id]
        assert resource["Type"] == "AWS::CloudWatch::Alarm"

        props = resource["Properties"]
        assert _sub_value(props["AlarmName"]) == alarm_name
        # AlarmActions entries are {"Ref": "PulseAlarmsTopic"} passthrough dicts.
        assert {"Ref": "PulseAlarmsTopic"} in props["AlarmActions"]


# ---------------------------------------------------------------------------
# Requirement 8.4 - dashboard renders the forecast run-outcome widget
# ---------------------------------------------------------------------------


def test_dashboard_includes_forecast_widget(
    pulse_observability_template: dict[str, Any],
) -> None:
    """The overview dashboard body plots the three forecast run-outcome metrics.

    Validates: Requirement 8.4
    """
    dashboard = pulse_observability_template["Resources"]["PulseOverviewDashboard"][
        "Properties"
    ]
    body = _sub_value(dashboard["DashboardBody"])

    for metric in ("ForecastRunFailures", "ForecastToolExhausted", "ForecastNoAlert"):
        assert metric in body, f"dashboard missing {metric} on the forecast widget"


# ---------------------------------------------------------------------------
# Requirement 8.1 - X-Ray tracing on the forecaster AgentCore role
# ---------------------------------------------------------------------------


def test_forecaster_agentcore_role_grants_xray(
    pulse_api_template: dict[str, Any],
) -> None:
    """The forecaster AgentCore role can emit X-Ray trace + telemetry segments.

    Validates: Requirement 8.1
    """
    role = pulse_api_template["Resources"]["PulseForecasterAgentCoreRole"]["Properties"]
    actions = _xray_actions(role)
    assert "xray:PutTraceSegments" in actions
    assert "xray:PutTelemetryRecords" in actions


# ---------------------------------------------------------------------------
# Requirement 8.1 - invoker Lambda tracing + X-Ray grants
# ---------------------------------------------------------------------------


def test_forecaster_invoker_function_tracing_active(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """The forecaster invoker Lambda enables active X-Ray tracing.

    Validates: Requirement 8.1
    """
    function = pulse_pipeline_template["Resources"]["PulseForecasterInvokerFunction"][
        "Properties"
    ]
    assert function["TracingConfig"]["Mode"] == "Active"


def test_forecaster_invoker_role_grants_xray(
    pulse_pipeline_template: dict[str, Any],
) -> None:
    """The forecaster invoker role can emit X-Ray trace + telemetry segments.

    Validates: Requirement 8.1
    """
    role = pulse_pipeline_template["Resources"]["PulseForecasterInvokerRole"][
        "Properties"
    ]
    actions = _xray_actions(role)
    assert "xray:PutTraceSegments" in actions
    assert "xray:PutTelemetryRecords" in actions
