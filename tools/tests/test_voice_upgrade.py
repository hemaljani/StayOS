"""Verify focused voice upgrades reject changes outside the authorized scope."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stayos_deploy.voice import (
    MODEL_ID,
    PREVIOUS_MODEL_ID,
    ROOT,
    VoiceUpgrade,
    description_template,
    load_template,
    sonic_policy,
    upgrade_template,
    validate_changes,
    validate_parent_changes,
)


@pytest.fixture
def source():
    return load_template(
        (ROOT / "lumi/infrastructure/nested-stacks/voice.yaml").read_text()
    )


def previous(source):
    return json.loads(json.dumps(source).replace(MODEL_ID, PREVIOUS_MODEL_ID))


def test_upgrade_preserves_resources_actions_and_regional_scope(source):
    baseline = previous(source)
    changed = upgrade_template(baseline, source)
    assert changed == source
    assert baseline == previous(source)
    assert sonic_policy(changed)["PolicyDocument"]["Statement"][0]["Action"] == (
        sonic_policy(baseline)["PolicyDocument"]["Statement"][0]["Action"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("Action", ["bedrock:*"]),
        ("Resource", ["*"]),
        ("Condition", {"StringEquals": {"aws:RequestedRegion": "us-west-2"}}),
    ],
)
def test_upgrade_rejects_permission_scope_changes(source, field, value):
    baseline = previous(source)
    altered = copy.deepcopy(source)
    sonic_policy(altered)["PolicyDocument"]["Statement"][0][field] = value
    with pytest.raises(RuntimeError, match="beyond"):
        upgrade_template(baseline, altered)


def change(**values):
    return {
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": "VoiceAgentCoreRole",
            "ResourceType": "AWS::IAM::Role",
            "Replacement": "False",
            **values,
        }
    }


def test_preview_accepts_existing_voice_role_update():
    validate_changes([change()], "VoiceAgentCoreRole", "AWS::IAM::Role")


@pytest.mark.parametrize(
    "changes",
    [
        [],
        [change(), change()],
        [change(Action="Add")],
        [change(Action="Remove")],
        [change(Replacement="True")],
        [change(Replacement="Conditional")],
        [change(LogicalResourceId="AuthStack")],
    ],
)
def test_preview_rejects_unrelated_changes_and_replacements(changes):
    with pytest.raises(RuntimeError):
        validate_changes(changes, "VoiceAgentCoreRole", "AWS::IAM::Role")


def test_intrinsic_loader_preserves_json_meaning():
    template = load_template("x: !GetAtt VoiceStack.Outputs.Role\ny: !Ref AWS::Region")
    assert template == {
        "x": {"Fn::GetAtt": ["VoiceStack", "Outputs.Role"]},
        "y": {"Ref": "AWS::Region"},
    }


def test_description_refresh_preserves_every_resource_property(source):
    baseline = previous(source)
    baseline["Description"] = "Old description"
    baseline["Resources"]["VoiceAgentCoreRole"]["Metadata"] = {"Existing": "keep"}
    updated = description_template(baseline, source)
    expected = copy.deepcopy(baseline)
    expected["Description"] = source["Description"]
    expected["Resources"]["VoiceAgentCoreRole"]["Metadata"]["StayOSDescription"] = (
        source["Description"]
    )
    assert updated == expected
    assert description_template(updated, source) == updated
    assert baseline["Description"] == "Old description"


@pytest.mark.parametrize("scope", [["Metadata"], ["Metadata", "Properties"]])
def test_description_preview_rejects_resource_property_changes(tmp_path, scope):
    upgrade = VoiceUpgrade(
        SimpleNamespace(
            profile="target",
            cloudformation_profile="target-cfn",
            expected_account_id="111111111111",
            region="us-east-1",
            stack_prefix="stayos",
        )
    )
    upgrade.directory = tmp_path
    arn = "arn:aws:cloudformation:us-east-1:111111111111:changeSet/voice/id"
    with patch.object(
        upgrade,
        "aws",
        side_effect=[
            {"parameterKeys": ["AppPassword"], "tags": []},
            f"execute-change-set --change-set-name {arn}",
            {
                "Parameters": [{"ParameterKey": "AppPassword"}],
                "Changes": [change(Scope=scope)],
            },
        ],
    ) as aws:
        if scope == ["Metadata"]:
            assert (
                upgrade.preview(
                    "child",
                    Path("voice.json"),
                    "VoiceAgentCoreRole",
                    "AWS::IAM::Role",
                    metadata_only=True,
                )
                == arn
            )
        else:
            with pytest.raises(RuntimeError, match="resource properties"):
                upgrade.preview(
                    "child",
                    Path("voice.json"),
                    "VoiceAgentCoreRole",
                    "AWS::IAM::Role",
                    metadata_only=True,
                )
    assert "--parameter-overrides" not in aws.call_args_list[1].args


def test_description_action_does_not_build_or_update_runtime(tmp_path, source):
    args = SimpleNamespace(
        profile="target",
        cloudformation_profile="target-cfn",
        expected_account_id="111111111111",
        region="us-east-1",
        stack_prefix="stayos",
    )
    upgrade = VoiceUpgrade(args)
    upgrade.directory = tmp_path
    baseline = {"child": "child", "runtimeId": "voice", "version": "14"}
    voice = copy.deepcopy(source)
    voice["Description"] = "Old description"
    desired = description_template(voice, source)
    with patch("stayos_deploy.voice.subprocess.run") as run, patch.object(
        upgrade, "capture", return_value=(baseline, {}, voice)
    ), patch.object(upgrade, "update_stack") as update, patch.object(
        upgrade, "template", return_value=desired
    ), patch.object(
        upgrade, "runtime", return_value={"agentRuntimeVersion": "14"}
    ), patch.object(
        upgrade, "update_runtime"
    ) as runtime_update:
        upgrade.refresh_description()
    assert run.call_args.args[0][:2] == ["make", "plan"]
    assert run.call_count == 1
    update.assert_called_once_with({}, desired, baseline, metadata_only=True)
    runtime_update.assert_not_called()


def test_identity_mismatch_stops_before_writes():
    upgrade = VoiceUpgrade(
        SimpleNamespace(
            profile="target",
            cloudformation_profile="target-cfn",
            expected_account_id="111111111111",
            region="us-east-1",
            stack_prefix="stayos",
        )
    )
    with patch.object(upgrade, "aws", return_value={"Account": "222222222222"}) as aws:
        with pytest.raises(RuntimeError, match="account mismatch"):
            upgrade.context()
    assert aws.call_count == 1
    assert aws.call_args.args == ("sts", "get-caller-identity")


def test_deploy_preview_preserves_parameters(tmp_path):
    upgrade = VoiceUpgrade(
        SimpleNamespace(
            profile="target",
            cloudformation_profile="target-cfn",
            expected_account_id="111111111111",
            region="us-east-1",
            stack_prefix="stayos",
        )
    )
    upgrade.directory = tmp_path
    arn = "arn:aws:cloudformation:us-east-1:111111111111:changeSet/voice/id"
    with patch.object(
        upgrade,
        "aws",
        side_effect=[
            {
                "parameterKeys": ["AppPassword"],
                "tags": [
                    {"Key": "Component", "Value": "voice"},
                ],
            },
            f"aws cloudformation execute-change-set --change-set-name {arn}",
            {
                "Parameters": [{"ParameterKey": "AppPassword"}],
                "Changes": [change()],
            },
        ],
    ) as aws:
        assert (
            upgrade.preview(
                "existing-child",
                Path("voice.json"),
                "VoiceAgentCoreRole",
                "AWS::IAM::Role",
            )
            == arn
        )
    deploy = aws.call_args_list[1].args
    assert deploy[:2] == ("cloudformation", "deploy")
    assert "--no-execute-changeset" in deploy
    assert "--parameter-overrides" not in deploy
    assert deploy[-2:] == ("--tags", "Component=voice")
    assert (
        "AppPassword" not in (tmp_path / "preview-VoiceAgentCoreRole.json").read_text()
    )


def test_preview_rejects_parameter_reset(tmp_path):
    upgrade = VoiceUpgrade(
        SimpleNamespace(
            profile="target",
            cloudformation_profile="target-cfn",
            expected_account_id="111111111111",
            region="us-east-1",
            stack_prefix="stayos",
        )
    )
    upgrade.directory = tmp_path
    arn = "arn:aws:cloudformation:us-east-1:111111111111:changeSet/voice/id"
    with patch.object(
        upgrade,
        "aws",
        side_effect=[
            {"parameterKeys": ["AppPassword"], "tags": []},
            f"execute-change-set --change-set-name {arn}",
            {
                "Parameters": [
                    {"ParameterKey": "AppPassword", "UsePreviousValue": False}
                ],
                "Changes": [change()],
            },
        ],
    ):
        with pytest.raises(RuntimeError, match="preserve"):
            upgrade.preview(
                "existing-child",
                Path("voice.json"),
                "VoiceAgentCoreRole",
                "AWS::IAM::Role",
            )


@pytest.mark.parametrize(
    "unrelated_changes", [[], [change(LogicalResourceId="OtherRole")]]
)
def test_hierarchy_preview_checks_underlying_resources(tmp_path, unrelated_changes):
    """Automatic wrappers are safe only when their child changes are empty."""
    upgrade = VoiceUpgrade(
        SimpleNamespace(
            profile="target",
            cloudformation_profile="target-cfn",
            expected_account_id="111111111111",
            region="us-east-1",
            stack_prefix="stayos",
        )
    )
    upgrade.directory = tmp_path
    parent = {
        "Resources": {
            "VoiceStack": {"Type": "AWS::CloudFormation::Stack"},
            "AuthStack": {"Type": "AWS::CloudFormation::Stack"},
        }
    }
    parent_file = tmp_path / "parent.json"
    parent_file.write_text(json.dumps(parent))

    def wrapper(name):
        return change(
            LogicalResourceId=name,
            ResourceType="AWS::CloudFormation::Stack",
            Scope=["Properties"],
            ChangeSetId=name + "-preview",
            Details=[
                {"ChangeSource": "Automatic", "Target": {"Attribute": "Properties"}}
            ],
        )

    root = {"Changes": [wrapper("VoiceStack"), wrapper("AuthStack")]}
    with patch.object(
        upgrade,
        "stack_state",
        return_value={
            "parameterKeys": ["AppPassword"],
            "tags": [],
        },
    ), patch.object(
        upgrade,
        "aws",
        side_effect=[
            {"Id": "root-preview"},
            "",
            root,
            {"Changes": [change()]},
            {"Changes": unrelated_changes},
            "",
        ],
    ):
        if unrelated_changes:
            with pytest.raises(RuntimeError, match="unrelated resources"):
                upgrade.preview_hierarchy(parent_file)
        else:
            upgrade.preview_hierarchy(parent_file)
    request = json.loads((tmp_path / "hierarchy-request.json").read_text())
    assert request["IncludeNestedStacks"] is True
    assert request["Parameters"] == [
        {"ParameterKey": "AppPassword", "UsePreviousValue": True}
    ]


def test_parent_preview_rejects_direct_change_to_another_template():
    parent = {
        "Resources": {
            "AuthStack": {"Type": "AWS::CloudFormation::Stack"},
        }
    }
    changes = [
        change(
            LogicalResourceId="AuthStack",
            ResourceType="AWS::CloudFormation::Stack",
            Scope=["Properties"],
            Details=[
                {
                    "ChangeSource": "DirectModification",
                    "Target": {"Attribute": "Properties", "Name": "TemplateURL"},
                }
            ],
        )
    ]
    with pytest.raises(RuntimeError, match="unrelated properties"):
        validate_parent_changes(changes, parent)
