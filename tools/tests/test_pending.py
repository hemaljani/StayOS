"""Guard the reviewed deployment scope and shared Gateway schema."""

import copy
from io import BytesIO
import json
from types import SimpleNamespace
import time
from unittest.mock import Mock, patch

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from stayos_deploy.pending import (
    GATEWAY_NAMES,
    PendingDeployment,
    compute_update,
    gateway_update,
    snapshot_fingerprint,
    resolve_parameter_value,
    validate_chat_predictions,
    validate_frontend_export,
    validate_parent,
)


def test_compute_change_preserves_layer_code_iam_and_other_properties():
    baseline = {
        "Resources": {
            "OrchestratorFunction": {
                "Properties": {
                    "Environment": {
                        "Variables": {"MOCK_MODE": "true", "OTHER": "keep"}
                    },
                    "Code": {"S3Key": "existing"},
                    "Layers": ["existing-layer"],
                }
            },
            "Role": {"Properties": {"Policy": "unchanged"}},
        },
    }
    source = copy.deepcopy(baseline)
    source["Resources"]["OrchestratorFunction"]["Properties"]["Environment"][
        "Variables"
    ]["MOCK_MODE"] = "false"
    assert compute_update(baseline, source) == source
    assert (
        baseline["Resources"]["OrchestratorFunction"]["Properties"]["Environment"][
            "Variables"
        ]["MOCK_MODE"]
        == "true"
    )


def test_gateway_preserves_eight_tools_and_changes_only_description():
    tools = [
        {"name": name, "description": "original", "inputSchema": {"type": "object"}}
        for name in sorted(GATEWAY_NAMES)
    ]
    target = {
        "gatewayIdentifier": "gateway",
        "targetId": "target",
        "name": "tools",
        "credentialProviderConfigurations": [
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": "existing",
                    "toolSchema": {"inlinePayload": tools},
                }
            }
        },
    }
    expected = copy.deepcopy(target)
    next(
        t
        for t in expected["targetConfiguration"]["mcp"]["lambda"]["toolSchema"][
            "inlinePayload"
        ]
        if t["name"] == "get_occupancy"
    )["description"] = "inventory"
    assert gateway_update(target, "inventory") == expected
    target["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"].pop()
    with pytest.raises(RuntimeError, match="eight"):
        gateway_update(target, "inventory")


@pytest.mark.parametrize(
    "field,value",
    [
        ("Action", "Add"),
        ("Replacement", "True"),
        ("ResourceType", "AWS::IAM::Role"),
    ],
)
def test_preview_refuses_resources_outside_existing_nested_stacks(field, value):
    change = {
        "ResourceChange": {
            "Action": "Modify",
            "Replacement": "False",
            "ResourceType": "AWS::CloudFormation::Stack",
            "LogicalResourceId": "ComputeStack",
            field: value,
        }
    }
    with pytest.raises(RuntimeError):
        validate_parent([change])


def test_mismatched_identity_stops_before_any_mutation():
    # Construct without SDK initialization; identity checks are inherited and pure.
    deploy = object.__new__(PendingDeployment)
    deploy.args = SimpleNamespace(
        profile="target", cloudformation_profile="cfn", region="us-east-1"
    )
    deploy.account = "111111111111"
    with patch.object(deploy, "aws", return_value={"Account": "222222222222"}) as aws:
        with pytest.raises(RuntimeError, match="mismatch"):
            deploy.context()
        aws.assert_called_once_with("sts", "get-caller-identity", cfn=False)


def test_cfn_preview_resolves_property_values_and_preserves_parameters(tmp_path):
    """Raw CFN ARN predictions must be resolved before checking exact scope."""
    deploy = object.__new__(PendingDeployment)
    deploy.directory = tmp_path
    deploy.bucket = "existing-deploy-bucket"
    arn = "arn:aws:cloudformation:us-east-1:111111111111:changeSet/review/id"
    detail = {
        "Parameters": [{"ParameterKey": "ExistingLayerKey"}],
        "Changes": [
            {
                "ResourceChange": {
                    "LogicalResourceId": "OrchestratorFunction",
                    "ResourceType": "AWS::Lambda::Function",
                    "Replacement": "False",
                    "Action": "Modify",
                    "Scope": ["Properties"],
                    "Details": [
                        {
                            "ChangeSource": "DirectModification",
                            "Target": {
                                "Name": "Environment",
                                "Path": "/Properties/Environment/Variables/MOCK_MODE",
                            },
                        }
                    ],
                }
            }
        ],
    }
    with patch.object(
        deploy, "stack_state", return_value={"parameterKeys": ["ExistingLayerKey"]}
    ), patch.object(deploy, "aws", side_effect=[arn, detail]) as aws:
        assert (
            deploy.preview(
                "existing-child",
                tmp_path / "template.json",
                "OrchestratorFunction",
                "AWS::Lambda::Function",
            )
            == arn
        )
        assert "--include-property-values" in aws.call_args.args
        assert "--parameter-overrides" not in aws.call_args_list[0].args


def predicted_role():
    """A supplier placeholder is safe only when its unchanged source resolves."""
    document = {"Statement": [{"Action": "read", "Resource": ["arn:table:existing"]}]}
    properties = {
        "RoleName": "existing",
        "Policies": [
            {
                "PolicyName": "read",
                "PolicyDocument": document,
            }
        ],
    }
    before = {"Properties": properties}
    after = copy.deepcopy(before)
    after["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]["Resource"][
        0
    ] = "{{changeSet:KNOWN_AFTER_APPLY}}"
    path = "/Properties/Policies/0/PolicyDocument/Statement/0/Resource/0"
    change = {
        "ResourceChange": {
            "LogicalResourceId": "ToolLambdaRole",
            "ResourceType": "AWS::IAM::Role",
            "Action": "Modify",
            "Replacement": "False",
            "Scope": ["Properties"],
            "BeforeContext": json.dumps(before),
            "AfterContext": json.dumps(after),
            "Details": [
                {
                    "ChangeSource": "ParameterReference",
                    "CausingEntity": "TableArn",
                    "Target": {"Path": path},
                }
            ],
        }
    }
    source = {"Resources": {"ToolLambdaRole": copy.deepcopy(before)}}
    source["Resources"]["ToolLambdaRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"][0]["Resource"][0] = {"Fn::Sub": "${TableArn}"}
    return change, source, {"TableArn": "arn:table:existing"}, properties


def test_supplier_placeholder_resolves_to_exact_existing_policy():
    change, source, parameters, properties = predicted_role()
    assert validate_chat_predictions([change], source, parameters) == {
        "ToolLambdaRole": properties
    }


@pytest.mark.parametrize("mutation", ["supplier", "policy", "replacement", "direct"])
def test_supplier_placeholder_never_allows_real_scope_changes(mutation):
    change, source, parameters, _ = predicted_role()
    resource = change["ResourceChange"]
    if mutation == "supplier":
        parameters["TableArn"] = "arn:table:different"
    elif mutation == "policy":
        after = json.loads(resource["AfterContext"])
        after["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0][
            "Action"
        ] = "write"
        resource["AfterContext"] = json.dumps(after)
    elif mutation == "replacement":
        resource["Replacement"] = "True"
    else:
        resource["Details"][0]["ChangeSource"] = "DirectModification"
    with pytest.raises(RuntimeError):
        validate_chat_predictions([change], source, parameters)


def test_sub_mapping_resolves_only_verified_parameters():
    assert (
        resolve_parameter_value(
            {"Fn::Sub": ["${Alias}/index/*", {"Alias": {"Ref": "TableArn"}}]},
            {"TableArn": "arn:existing"},
        )
        == "arn:existing/index/*"
    )
    with pytest.raises(KeyError):
        resolve_parameter_value({"Ref": "Unknown"}, {})


@pytest.mark.parametrize("fault", [None, "env", "compiled", "override"])
def test_frontend_publication_requires_live_config_in_built_export(tmp_path, fault):
    expected = {"NEXT_PUBLIC_API_URL": "https://existing-api.example/v1"}
    (tmp_path / ".env.production").write_text(
        "NEXT_PUBLIC_API_URL="
        + (
            "https://stale.example"
            if fault == "env"
            else expected["NEXT_PUBLIC_API_URL"]
        )
        + "\n"
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "out/main.js").write_text(
        "stale" if fault == "compiled" else expected["NEXT_PUBLIC_API_URL"]
    )
    if fault == "override":
        (tmp_path / ".env.local").write_text("NEXT_PUBLIC_API_URL=stale\n")
    if fault:
        with pytest.raises(RuntimeError):
            validate_frontend_export(tmp_path, expected)
    else:
        validate_frontend_export(tmp_path, expected)


def test_repair_correction_refuses_an_unexpected_live_code_hash():
    deploy = object.__new__(PendingDeployment)
    deploy.args = SimpleNamespace(stack_prefix="stayos")
    deploy.lambdas = SimpleNamespace(
        get_function_configuration=Mock(return_value={"CodeSha256": "unexpected"}),
    )
    with patch.object(
        deploy,
        "saved",
        side_effect=[{}, {}, {"stayos-orchestrator": {"codeHash": "reviewed"}}],
    ), patch.object(deploy, "package_lumi") as package:
        with pytest.raises(RuntimeError, match="outside"):
            deploy.refresh_orchestrator()
        package.assert_not_called()


@pytest.mark.parametrize("mock_mode", ["false", "true"])
def test_verify_checks_typed_lambda_configuration(mock_mode):
    """CLI text 'false' can be decoded as a bool; SDK config stays a string."""
    deploy = object.__new__(PendingDeployment)
    deploy.args = SimpleNamespace(stack_prefix="stayos")
    config = {
        "CodeSha256": "reviewed",
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "Environment": {"Variables": {"MOCK_MODE": mock_mode}},
    }
    deploy.lambdas = SimpleNamespace(
        get_function_configuration=Mock(return_value=config),
    )
    deploy.db = Mock()
    values = {
        "baseline.json": {},
        "functions-after.json": {"stayos-orchestrator": {"codeHash": "reviewed"}},
        "versions-after.json": {},
        "repair-manifest.json": [],
    }
    with patch.object(deploy, "saved", side_effect=values.__getitem__), patch.object(
        deploy, "write"
    ) as write:
        if mock_mode == "false":
            deploy.verify()
            write.assert_called_once()
        else:
            with pytest.raises(RuntimeError, match="mock mode"):
                deploy.verify()
            write.assert_not_called()


@pytest.mark.parametrize("concurrent_change", [None, "before-read", "after-read"])
def test_rollback_preserves_original_data_and_refuses_concurrent_edits(
    tmp_path, concurrent_change
):
    """Exercise the real conditional restore; all external deployment calls mock."""
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        table = db.create_table(
            TableName="stayos-briefs",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "propertyId", "KeyType": "HASH"},
                {"AttributeName": "briefDate", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "propertyId", "AttributeType": "S"},
                {"AttributeName": "briefDate", "AttributeType": "S"},
            ],
        )
        key = {"propertyId": "ALOHA-CHI-001", "briefDate": "2026-10-01"}
        original = {
            **key,
            "ttl": int(time.time()) + 3600,
            "generatedAt": "original",
            "narrative": "original",
            "audioBrief": {"s3Key": "original.mp3"},
        }
        repaired = {
            **original,
            "generatedAt": "repaired",
            "narrative": "repaired",
            "repairMetadata": {"runId": "reviewed-run"},
        }
        table.put_item(Item=repaired)
        result = {
            **key,
            "status": "REPAIRED",
            "fingerprint": snapshot_fingerprint(repaired),
        }
        changed = {**repaired, "generatedAt": "concurrent", "narrative": "keep"}
        if concurrent_change == "before-read":
            table.put_item(Item=changed)
        serializer = TypeSerializer()
        payload = json.dumps(
            {
                "brief": {
                    field: serializer.serialize(value)
                    for field, value in original.items()
                }
            }
        ).encode()

        def backup(**kwargs):
            if concurrent_change == "after-read":
                table.put_item(Item=changed)
            return {"Body": BytesIO(payload)}

        deploy = object.__new__(PendingDeployment)
        deploy.args = SimpleNamespace(stack_prefix="stayos")
        deploy.directory = tmp_path
        (tmp_path / "repair-results.json").write_text("[]")
        deploy.bucket = "existing"
        deploy.db = db
        deploy.s3 = SimpleNamespace(get_object=Mock(side_effect=backup))
        deploy.config_update = Mock()
        deploy.update_runtime = Mock()
        deploy.write = Mock(return_value=tmp_path / "request.json")
        deploy.aws = Mock(return_value={"Invalidation": {"Id": "reviewed"}})
        images = {
            agent: {
                "agentRuntimeId": agent,
                "agentRuntimeArtifact": {
                    "containerConfiguration": {
                        "containerUri": "repo@sha256:" + digit * 64
                    }
                },
            }
            for agent, digit in (("voice", "1"), ("chat", "2"))
        }
        values = {
            "baseline.json": {
                "runId": "reviewed-run",
                "functions": {},
                "frontendBucket": "existing",
                "distribution": "existing",
            },
            "repair-results.json": [result],
            "repair-manifest.json": [{**key, "backupKey": "reviewed-backup"}],
            "gateway-before.json": {
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "toolSchema": {
                                "inlinePayload": [
                                    {"name": name, "description": "original"}
                                    for name in GATEWAY_NAMES
                                ]
                            }
                        }
                    }
                }
            },
            **{f"{agent}-before.json": request for agent, request in images.items()},
        }
        with patch.object(deploy, "saved", side_effect=values.__getitem__):
            if concurrent_change:
                expected_error = (
                    RuntimeError if concurrent_change == "before-read" else ClientError
                )
                with pytest.raises(expected_error):
                    deploy.rollback()
                assert table.get_item(Key=key)["Item"] == changed
                deploy.config_update.assert_not_called()
                deploy.update_runtime.assert_not_called()
            else:
                deploy.rollback()
                assert table.get_item(Key=key)["Item"] == original
                deploy.config_update.assert_called_once_with(rollback=True)
                assert deploy.update_runtime.call_args_list[0].args == (
                    images["voice"],
                )
                assert deploy.update_runtime.call_args_list[1].args == (images["chat"],)
