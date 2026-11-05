"""Verify realtime overlays preserve deployed code, claims and unrelated grants."""
from __future__ import annotations

import copy
import json

import pytest
from stayos_deploy.realtime_release import (
    API_ROLES,
    PIPELINE_ROLES,
    ROOT,
    patch_template,
    pin_dependencies,
    simulation_decisions,
    source_archive,
)
from stayos_deploy.voice import load_template


@pytest.mark.parametrize("api,name,roles", [
    (True, "pulse-api", API_ROLES),
    (False, "pulse-pipeline", PIPELINE_ROLES),
])
def test_overlay_changes_only_auth_and_publish_resource(api, name, roles):
    source = load_template((ROOT / f"pulse/infrastructure/nested-stacks/{name}.yaml").read_text())
    baseline = copy.deepcopy(source)
    if api:
        baseline["Resources"].pop("PulseRealtimePublishProbe")
        baseline["Resources"].pop("PulseRealtimeDeniedProbe")
    # Model deployed differences that must survive a focused update.
    for key, resource in baseline["Resources"].items():
        if resource["Type"] == "AWS::Lambda::Function":
            resource["Properties"]["Code"] = {"S3Bucket": "existing", "S3Key": f"{key}/pinned.zip"}
    for role in roles:
        for policy in baseline["Resources"][role]["Properties"]["Policies"]:
            for statement in policy.get("PolicyDocument", {}).get("Statement", []):
                if "appsync:EventPublish" in statement.get("Action", []):
                    statement["Resource"] = "old-api-arn"
    if api:
        config = baseline["Resources"]["PulseRealtimeEventApi"]["Properties"]["EventConfig"]
        config["AuthProviders"] = [config["AuthProviders"][0]]
        config["DefaultPublishAuthModes"] = [{"AuthType": "AMAZON_COGNITO_USER_POOLS"}]
        baseline["Resources"]["PulseRealtimeNamespace"]["Properties"]["PublishAuthModes"] = [
            {"AuthType": "AMAZON_COGNITO_USER_POOLS"}
        ]
    before = copy.deepcopy(baseline)
    candidate = patch_template(baseline, source, api)
    assert baseline == before
    allowed = roles | ({"PulseRealtimeEventApi", "PulseRealtimeNamespace"} if api else set())
    for key, resource in baseline["Resources"].items():
        if key not in allowed:
            assert candidate["Resources"][key] == resource
    assert candidate.get("Parameters") == baseline.get("Parameters")
    if api:
        namespace = candidate["Resources"]["PulseRealtimeNamespace"]["Properties"]
        assert namespace["CodeHandlers"] == baseline["Resources"]["PulseRealtimeNamespace"]["Properties"]["CodeHandlers"]
        assert namespace["SubscribeAuthModes"] == [{"AuthType": "AMAZON_COGNITO_USER_POOLS"}]


def test_missing_publisher_grant_is_refused():
    source = load_template((ROOT / "pulse/infrastructure/nested-stacks/pulse-api.yaml").read_text())
    baseline = copy.deepcopy(source)
    baseline["Resources"]["PulseApiRole"]["Properties"]["Policies"] = []
    with pytest.raises(RuntimeError, match="exactly one"):
        patch_template(baseline, source, True)


def test_source_archive_is_stable_and_contains_no_credentials():
    import io
    import zipfile

    archive = source_archive()
    assert source_archive() == archive
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
    assert "pulse/frontend/src/__tests__/realtimeClient.test.ts" in names
    assert all(".env" not in n and ".npmrc" not in n and "node_modules/" not in n for n in names)


def test_all_source_publishers_have_only_the_namespace_resource():
    for name, api, roles in [
        ("pulse-api", True, API_ROLES), ("pulse-pipeline", False, PIPELINE_ROLES),
    ]:
        template = load_template((ROOT / f"pulse/infrastructure/nested-stacks/{name}.yaml").read_text())
        for role in roles:
            policies = template["Resources"][role]["Properties"]["Policies"]
            statements = [
                s for p in policies for s in p.get("PolicyDocument", {}).get("Statement", [])
                if "appsync:EventPublish" in s.get("Action", [])
            ]
            assert len(statements) == 1
            scope = json.dumps(statements[0]["Resource"])
            assert "/channelNamespace/pulse" in scope
            assert "/*" not in scope


def test_demo_probes_ignore_input_and_report_only_safe_response_fields(monkeypatch):
    import urllib.request
    from types import SimpleNamespace

    import boto3
    from botocore.credentials import Credentials

    source = load_template((ROOT / "pulse/infrastructure/nested-stacks/pulse-api.yaml").read_text())
    code = source["Mappings"]["RealtimeProbeCode"]["Python"]["Source"]
    namespace = {}
    # Execute only the repository-owned inline handler, with SDK/HTTP seams mocked.
    exec(compile(code, "realtime-probe", "exec"), namespace)  # noqa: S102
    monkeypatch.setenv("REALTIME_HTTP_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(
        get_credentials=lambda: Credentials("testing", "testing"),
    ))
    requests = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"successful":[{"index":0}],"failed":[]}'

    def send(request, timeout):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", send)
    result = namespace["lambda_handler"]({"channel": "/pulse/alerts/customer"}, None)
    assert requests[0]["channel"] == "/pulse/integration/authorization"
    assert result == {"httpStatus": 200, "successful": 1, "failed": 0}
    for name in ["PulseRealtimePublishProbe", "PulseRealtimeDeniedProbe"]:
        assert source["Resources"][name]["Condition"] == "ShouldEnableDemoSimulator"


def test_dependency_pinning_preserves_installed_values_and_root_parameter_refs():
    root = {"Resources": {
        "ApiStack": {"Type": "AWS::CloudFormation::Stack", "Properties": {"Parameters": {}}},
        "PipelineStack": {"Type": "AWS::CloudFormation::Stack", "Properties": {"Parameters": {
            "RealtimeApiArn": {"Fn::GetAtt": ["ApiStack", "Outputs.RealtimeApiArn"]},
            "DeployBucket": {"Ref": "DeployBucket"},
        }}},
    }}
    before = copy.deepcopy(root)
    candidate, pins = pin_dependencies(root, {
        "ApiStack": {}, "PipelineStack": {"RealtimeApiArn": "existing-api"},
    })
    assert root == before
    assert candidate["Resources"]["PipelineStack"]["Properties"]["Parameters"] == {
        "RealtimeApiArn": "existing-api", "DeployBucket": {"Ref": "DeployBucket"},
    }
    assert len(pins) == 1
    with pytest.raises(RuntimeError, match="Cannot safely preserve"):
        pin_dependencies(root, {"ApiStack": {}, "PipelineStack": {"RealtimeApiArn": "****"}})


def test_simulator_grouped_results_use_the_resource_specific_decisions():
    results = [{
        "EvalResourceName": "arn:aws:appsync:${Region}:${Account}:${ResourceType}/${ResourcePath}",
        "EvalDecision": "implicitDeny",
        "ResourceSpecificResults": [
            {"EvalResourceName": "pulse-namespace", "EvalResourceDecision": "allowed"},
            {"EvalResourceName": "unrelated-namespace", "EvalResourceDecision": "implicitDeny"},
        ],
    }]
    assert simulation_decisions(results) == {
        "pulse-namespace": "allowed", "unrelated-namespace": "implicitDeny",
    }
