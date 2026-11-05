"""Verify that the forecast release preserves unrelated deployed resources."""

import copy
import json

import pytest
from stayos_deploy.forecast_release import (
    ALLOWED,
    patch_observability,
    patch_pipeline,
    validate_leaf_changes,
)
from stayos_deploy.voice import ROOT, load_template


def source(name):
    return load_template(
        (ROOT / "pulse/infrastructure/nested-stacks" / name).read_text()
    )


def test_pipeline_preserves_every_unrelated_resource_and_permission():
    desired = source("pulse-pipeline.yaml")
    baseline = copy.deepcopy(desired)
    baseline["Resources"]["PulseForecasterInvokerRole"]["Properties"]["Policies"][0] = {
        "PolicyName": "InvokeForecasterRuntime",
        "PolicyDocument": {"Version": "2012-10-17", "Statement": []},
    }
    baseline["Resources"]["PulseForecasterInvokerFunction"]["Properties"]["Handler"] = (
        "lambda_function.lambda_handler"
    )
    baseline["Resources"]["PulseBackendBuildProject"]["Properties"]["Source"][
        "BuildSpec"
    ] = "old build"
    baseline["Resources"]["DeployedOnlyResource"] = {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"RetentionInDays": 90},
    }
    untouched = copy.deepcopy(baseline)
    updated = patch_pipeline(baseline, desired, "functions/forecast-test.zip")
    assert baseline == untouched
    for logical, resource in baseline["Resources"].items():
        if logical not in ALLOWED["PipelineStack"]:
            assert updated["Resources"][logical] == resource
    original_role = baseline["Resources"]["PulseForecasterInvokerRole"]["Properties"]
    updated_role = updated["Resources"]["PulseForecasterInvokerRole"]["Properties"]
    assert updated_role["Policies"][1:] == original_role["Policies"][1:]
    assert (
        updated_role["AssumeRolePolicyDocument"]
        == original_role["AssumeRolePolicyDocument"]
    )
    function = updated["Resources"]["PulseForecasterInvokerFunction"]["Properties"]
    assert function["Code"]["S3Key"] == "functions/forecast-test.zip"
    assert function["Handler"] == "pulse.forecast_invoker.lambda_handler"
    assert (
        function["Environment"]
        == baseline["Resources"]["PulseForecasterInvokerFunction"]["Properties"][
            "Environment"
        ]
    )


def test_observability_preserves_custom_deployed_dashboard_widgets():
    desired = source("pulse-observability.yaml")
    baseline = copy.deepcopy(desired)
    baseline["Resources"].pop("ForecastRunCompletedMetricFilter")
    dashboard = baseline["Resources"]["PulseOverviewDashboard"]["Properties"][
        "DashboardBody"
    ]
    body = json.loads(dashboard["Fn::Sub"])
    for widget in body["widgets"]:
        if "metrics" in widget.get("properties", {}):
            widget["properties"]["metrics"] = [
                metric
                for metric in widget["properties"]["metrics"]
                if not isinstance(metric, list) or "ForecastRunCompleted" not in metric
            ]
    custom = {"type": "text", "properties": {"markdown": "Deployed operator note"}}
    body["widgets"].append(custom)
    dashboard["Fn::Sub"] = json.dumps(body)
    untouched = copy.deepcopy(baseline)
    updated = patch_observability(baseline, desired)
    assert baseline == untouched
    for logical, resource in baseline["Resources"].items():
        if logical != "PulseOverviewDashboard":
            assert updated["Resources"][logical] == resource
    changed = json.loads(
        updated["Resources"]["PulseOverviewDashboard"]["Properties"]["DashboardBody"][
            "Fn::Sub"
        ]
    )
    assert changed["widgets"][-1] == custom
    assert (
        sum(
            isinstance(metric, list) and "ForecastRunCompleted" in metric
            for widget in changed["widgets"]
            for metric in widget.get("properties", {}).get("metrics", [])
        )
        == 1
    )


@pytest.mark.parametrize("alteration", ["unrelated", "replace", "remove", "missing"])
def test_change_set_gate_refuses_scope_expansion(alteration):
    expected = ALLOWED["PipelineStack"]
    changes = [
        {
            "ResourceChange": {
                "LogicalResourceId": name,
                "Action": action,
                "ResourceType": resource_type,
                "Replacement": "False",
            }
        }
        for name, (action, resource_type) in expected.items()
    ]
    validate_leaf_changes(changes, expected)
    if alteration == "unrelated":
        changes.append(
            {
                "ResourceChange": {
                    "LogicalResourceId": "OtherLambda",
                    "Action": "Modify",
                    "ResourceType": "AWS::Lambda::Function",
                    "Replacement": "False",
                }
            }
        )
    elif alteration == "replace":
        changes[0]["ResourceChange"]["Replacement"] = "True"
    elif alteration == "remove":
        changes[0]["ResourceChange"]["Action"] = "Remove"
    else:
        changes.pop()
    with pytest.raises(RuntimeError):
        validate_leaf_changes(changes, expected)
