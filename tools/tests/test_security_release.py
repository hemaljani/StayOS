"""Verify the security release rejects unrelated infrastructure mutations."""

from pathlib import Path

import pytest
from stayos_deploy.security_release import (
    patch_compute,
    patch_data,
    patch_frontend_domain,
    separate_api_dependencies,
    separate_voice_role_dependency,
    simulation_decisions,
    validate_security_changes,
)
from stayos_deploy.voice import load_template

ROOT = Path(__file__).resolve().parents[2]


def change(name, kind, action="Modify", replacement="False"):
    return {"ResourceChange": {
        "LogicalResourceId": name, "ResourceType": kind,
        "Action": action, "Replacement": replacement,
    }}


def test_bootstrap_permits_only_the_authorized_secret():
    changes = [change("AudioSigningSecret", "AWS::SecretsManager::Secret", "Add")]
    assert validate_security_changes(changes, "bootstrap", "DataStack") == ["AudioSigningSecret"]


@pytest.mark.parametrize("unexpected", [
    change("UnrelatedSecret", "AWS::SecretsManager::Secret", "Add"),
    change("AudioBucket", "AWS::S3::Bucket"),
    change("AudioSigningSecret", "AWS::SecretsManager::Secret", "Remove"),
])
def test_bootstrap_rejects_unrelated_or_destructive_changes(unexpected):
    with pytest.raises(RuntimeError):
        validate_security_changes([unexpected], "bootstrap", "DataStack")


def test_audio_rejects_api_replacement():
    with pytest.raises(RuntimeError):
        validate_security_changes(
            [change("ApiFunction", "AWS::Lambda::Function", replacement="True")],
            "signer", "ComputeStack",
        )


def test_frontend_domain_patch_preserves_distribution_and_other_parameters():
    source = load_template((ROOT / "lumi/infrastructure/root-stack.yaml").read_text())
    root = {"Parameters": {"Existing": {"Type": "String"}}, "Resources": {
        "DataStack": {"Properties": {"sentinel": "unchanged"}},
        "ComputeStack": {"Properties": {"Parameters": {
            "FrontendDistributionDomain": "old.cloudfront.net", "UserPoolId": "existing-pool",
        }}},
    }}
    patched = patch_frontend_domain(root, source, "stayos.example.com")
    assert patched["Resources"]["DataStack"] == root["Resources"]["DataStack"]
    assert patched["Resources"]["ComputeStack"]["Properties"]["Parameters"] == {
        "FrontendDistributionDomain": {"Ref": "FrontendDomain"}, "UserPoolId": "existing-pool",
    }
    assert root["Resources"]["ComputeStack"]["Properties"]["Parameters"]["FrontendDistributionDomain"] == "old.cloudfront.net"


@pytest.mark.parametrize("domain", ["", "https://stayos.example.com", "stayos.example.com/path", "user@stayos.example.com"])
def test_frontend_domain_patch_rejects_non_hostnames(domain):
    with pytest.raises(RuntimeError, match="hostname"):
        patch_frontend_domain({}, {}, domain)


def test_cors_scope_rejects_replacement_and_resource_addition():
    from stayos_deploy.security_release import CORS_METHODS

    expected = [
        change("ApiFunction", "AWS::Lambda::Function"),
        change("GatewayResponse4XX", "AWS::ApiGateway::GatewayResponse"),
        *[change(name, "AWS::ApiGateway::Method") for name in CORS_METHODS],
    ]
    assert len(validate_security_changes(expected, "cors", "ComputeStack")) == 6
    with pytest.raises(RuntimeError):
        validate_security_changes(
            expected + [change("NewResource", "AWS::ApiGateway::Resource", action="Add")],
            "cors", "ComputeStack",
        )
    expected[0]["ResourceChange"]["Replacement"] = "True"
    with pytest.raises(RuntimeError):
        validate_security_changes(expected, "cors", "ComputeStack")


def test_bootstrap_preserves_audio_access_and_other_resources():
    source = load_template((ROOT / "lumi/infrastructure/nested-stacks/data.yaml").read_text())
    baseline = {"Resources": {"AudioDistribution": {"Type": "AWS::CloudFront::Distribution",
                                                   "Properties": {"sentinel": "unchanged"}}}}
    patched = patch_data(baseline, source, "bootstrap")
    assert patched["Resources"]["AudioDistribution"] == baseline["Resources"]["AudioDistribution"]
    assert set(patched["Resources"]) == {"AudioDistribution", "AudioSigningSecret"}
    assert "AudioSigningPublicKey" not in patched["Resources"]


def test_compute_patch_preserves_existing_runtime_and_unrelated_function():
    source = load_template((ROOT / "lumi/infrastructure/nested-stacks/compute.yaml").read_text())
    baseline = {"Resources": {
        "ApiFunction": {"Type": "AWS::Lambda::Function", "Properties": {
            "Runtime": "python3.12", "Code": {"S3Bucket": "existing", "S3Key": "old"},
            "Environment": {"Variables": {"BRIEFS_TABLE_NAME": "existing-table"}},
        }},
        "OrchestratorFunction": {"Properties": {"Code": {"S3Key": "unchanged"}}},
        "ApiGateway": {"Properties": {"Name": "existing-api"}},
    }}
    patched = patch_compute(baseline, source, "security/api.zip")
    assert patched["Resources"]["OrchestratorFunction"] == baseline["Resources"]["OrchestratorFunction"]
    api = patched["Resources"]["ApiFunction"]["Properties"]
    assert api["Runtime"] == "python3.12"
    assert api["Code"] == {"S3Bucket": "existing", "S3Key": {"Ref": "ApiCodeS3Key"}}
    assert api["Environment"]["Variables"]["BRIEFS_TABLE_NAME"] == "existing-table"
    assert api["Environment"]["Variables"]["POWERTOOLS_TRACER_CAPTURE_RESPONSE"] == "false"
    gateway = patched["Resources"]["ApiGateway"]["Properties"]
    assert gateway["SecurityPolicy"] == "SecurityPolicy_TLS13_1_2_2021_06"
    assert gateway["EndpointAccessMode"] == "BASIC"


def api_dependency(replacement="Conditional"):
    entry = change("Briefs", "AWS::ApiGateway::Resource", replacement=replacement)
    entry["ResourceChange"]["Details"] = [{
        "Evaluation": "Dynamic", "ChangeSource": "ResourceAttribute",
        "CausingEntity": "ApiGateway.RootResourceId",
        "Target": {"Name": "ParentId", "RequiresRecreation": "Always"},
    }]
    return entry


def test_unchanged_route_dependency_can_be_reviewed():
    template = {"Resources": {"Briefs": {"Type": "AWS::ApiGateway::Resource",
                                       "Properties": {"ParentId": "existing"}}}}
    direct, refs = separate_api_dependencies([api_dependency()], template, template)
    assert direct == []
    assert refs == ["Briefs"]


@pytest.mark.parametrize("violation", ["replacement", "edit", "direct", "unknown"])
def test_route_dependency_review_rejects_mutations(violation):
    import copy

    before = {"Resources": {"Briefs": {"Type": "AWS::ApiGateway::Resource",
                                     "Properties": {"ParentId": "existing"}}}}
    after = copy.deepcopy(before)
    entry = api_dependency()
    if violation == "replacement":
        entry["ResourceChange"]["Replacement"] = "True"
    elif violation == "edit":
        after["Resources"]["Briefs"]["Properties"]["ParentId"] = "new"
    elif violation == "direct":
        entry["ResourceChange"]["Details"][0]["ChangeSource"] = "DirectModification"
    else:
        entry["ResourceChange"]["Details"][0]["CausingEntity"] = "Unrelated"
    with pytest.raises(RuntimeError):
        separate_api_dependencies([entry], before, after)


@pytest.mark.parametrize("changed", [False, True])
def test_runtime_retry_skips_matching_update_and_rejects_drift(monkeypatch, changed):
    """A partial rollout can resume without silently updating a drifted runtime."""
    import hashlib

    from stayos_deploy import security_release

    digest = hashlib.sha256(b"source").hexdigest()
    state = {
        "buildStatus": "SUCCEEDED", "verifiedPhases": {"controls": "verified"},
        "sourceSha256": digest, "region": "us-east-1",
        "outputs": {"UserPoolClientId": "client", "UserPoolId": "pool"},
        "runtimeBefore": {"voice": {"agentRuntimeId": "voice", "roleArn": "role"}},
        "agentBuilds": {"voice": {"status": "SUCCEEDED", "sourceSha256": digest, "imageUri": "tested"}},
        "runtimeUpdates": {"voice": {"version": "17"}},
    }
    runtime = {
        "agentRuntimeVersion": "17", "roleArn": "role", "status": "UPDATING",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "other" if changed else "tested"}},
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "allowedClients": ["client"],
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration",
        }},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "environmentVariables": {"COGNITO_CLIENT_ID": "client"},
    }
    release = object.__new__(security_release.SecurityRelease)
    monkeypatch.setattr(release, "identity", lambda: None)
    monkeypatch.setattr(release, "read_state", lambda: state)
    monkeypatch.setattr(security_release, "archive_paths", lambda *args, **kwargs: b"source")

    def get_only(service, operation, *args):
        assert operation == "get-agent-runtime", "Retries must not submit a duplicate update"
        return runtime

    monkeypatch.setattr(release, "aws", get_only)
    if changed:
        with pytest.raises(RuntimeError, match="differs from"):
            release.update_runtimes()
    else:
        release.update_runtimes()


@pytest.mark.parametrize("drift", [None, "image", "client", "role"])
def test_runtime_followup_updates_only_the_known_previous_deployment(monkeypatch, drift):
    """A tested follow-up image must not overwrite an unexpected live runtime."""
    import hashlib
    from argparse import Namespace

    from stayos_deploy import security_release

    digest = hashlib.sha256(b"source").hexdigest()
    state = {
        "buildStatus": "SUCCEEDED", "verifiedPhases": {"controls": "verified"},
        "sourceSha256": digest, "region": "us-east-1",
        "outputs": {"UserPoolClientId": "client", "UserPoolId": "pool"},
        "runtimeBefore": {"voice": {"agentRuntimeId": "voice", "roleArn": "role"}},
        "agentBuilds": {"voice": {"status": "SUCCEEDED", "sourceSha256": digest, "imageUri": "new-tested"}},
        "runtimeUpdates": {"voice": {"version": "17", "imageUri": "previous-tested"}},
    }
    runtime = {
        "agentRuntimeVersion": "17", "roleArn": "unexpected" if drift == "role" else "role",
        "status": "READY",
        "agentRuntimeArtifact": {"containerConfiguration": {
            "containerUri": "unexpected" if drift == "image" else "previous-tested",
        }},
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "allowedClients": ["unexpected" if drift == "client" else "client"],
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration",
        }},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["Authorization"]},
        "environmentVariables": {"COGNITO_CLIENT_ID": "client"},
    }
    release = object.__new__(security_release.SecurityRelease)
    release.args = Namespace(region="us-east-1")
    monkeypatch.setattr(release, "identity", lambda: None)
    monkeypatch.setattr(release, "read_state", lambda: state)
    monkeypatch.setattr(release, "save", lambda record: None)
    monkeypatch.setattr(security_release, "archive_paths", lambda *args, **kwargs: b"source")
    updates = []

    def aws(service, operation, *args):
        if operation == "get-agent-runtime":
            return runtime
        assert operation == "update-agent-runtime"
        import json

        request = json.loads(args[1])
        assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == "new-tested"
        assert request["roleArn"] == "role"
        updates.append(request)
        return {"agentRuntimeVersion": "18", "status": "UPDATING"}

    monkeypatch.setattr(release, "aws", aws)
    if drift:
        with pytest.raises(RuntimeError, match="differs from"):
            release.update_runtimes()
        assert not updates
    else:
        release.update_runtimes()
        assert len(updates) == 1
        assert state["runtimeUpdates"]["voice"]["version"] == "18"
        assert state["runtimeUpdateHistory"]["voice"][0]["imageUri"] == "previous-tested"


def test_probe_transport_error_does_not_expose_capability(monkeypatch):
    import urllib.error

    from stayos_deploy import security_release

    capability = "https://example.invalid/audio?Signature=must-remain-private"

    def fail(*args, **kwargs):
        raise urllib.error.URLError(capability)

    monkeypatch.setattr(security_release.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError) as error:
        security_release.SecurityRelease.probe_http(capability)
    assert "must-remain-private" not in str(error.value)
    assert "URLError" in str(error.value)


def test_probe_response_memory_is_not_printed_or_written_to_disk(monkeypatch, capsys):
    import json
    import os

    from stayos_deploy import security_release

    release = object.__new__(security_release.SecurityRelease)

    def invoke(service, operation, *args, **kwargs):
        assert service == "lambda" and operation == "invoke"
        fd = kwargs["pass_fds"][0]
        assert f"/proc/self/fd/{fd}" in args
        os.write(fd, json.dumps({"statusCode": 200, "body": "private-customer-content"}).encode())
        return {"StatusCode": 200}

    monkeypatch.setattr(release, "aws", invoke)
    assert release.invoke_probe({"httpMethod": "GET"})["body"] == "private-customer-content"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("violation", [None, "edit", "replacement", "unrelated", "static"])
def test_cognito_role_reference_requires_identical_attachment(violation):
    import copy

    logical = "VoiceIdentityPoolRoleAttachment"
    before = {"Resources": {logical: {
        "Type": "AWS::Cognito::IdentityPoolRoleAttachment",
        "Properties": {"Roles": {"authenticated": {"Fn::GetAtt": ["VoiceIdentityPoolAuthRole", "Arn"]}}},
    }}}
    after = copy.deepcopy(before)
    entry = change(logical, "AWS::Cognito::IdentityPoolRoleAttachment")
    entry["ResourceChange"]["Details"] = [{
        "Evaluation": "Dynamic", "ChangeSource": "ResourceAttribute",
        "CausingEntity": "VoiceIdentityPoolAuthRole.Arn",
        "Target": {"Name": "Roles", "RequiresRecreation": "Never"},
    }]
    if violation == "edit":
        after["Resources"][logical]["Properties"]["Roles"]["authenticated"] = "different-role"
    elif violation == "replacement":
        entry["ResourceChange"]["Replacement"] = "Conditional"
    elif violation == "unrelated":
        entry["ResourceChange"]["Details"][0]["CausingEntity"] = "AnotherRole.Arn"
    elif violation == "static":
        entry["ResourceChange"]["Details"][0]["Evaluation"] = "Static"
    if violation:
        with pytest.raises(RuntimeError, match="Unexpected Cognito"):
            separate_voice_role_dependency([entry], before, after)
    else:
        assert separate_voice_role_dependency([entry], before, after) == ([], [logical])


def test_iam_aggregate_denial_does_not_replace_resource_specific_results():
    evaluation = [{
        "EvalActionName": "invoke", "EvalResourceName": "placeholder",
        "EvalDecision": "implicitDeny",
        "ResourceSpecificResults": [
            {"EvalResourceName": "required", "EvalResourceDecision": "allowed"},
            {"EvalResourceName": "unrelated", "EvalResourceDecision": "implicitDeny"},
        ],
    }]
    assert simulation_decisions(evaluation) == {
        ("invoke", "required"): "allowed", ("invoke", "unrelated"): "implicitDeny",
    }


def test_iam_decisions_preserve_action_and_reject_contradictions():
    rows = [
        {"EvalActionName": "invoke", "EvalResourceName": "required", "EvalDecision": "allowed"},
        {"EvalActionName": "stream", "EvalResourceName": "required", "EvalDecision": "implicitDeny"},
    ]
    assert simulation_decisions(rows) == {
        ("invoke", "required"): "allowed", ("stream", "required"): "implicitDeny",
    }
    rows.append({"EvalActionName": "invoke", "EvalResourceName": "required", "EvalDecision": "implicitDeny"})
    with pytest.raises(RuntimeError, match="Contradictory"):
        simulation_decisions(rows)


@pytest.mark.parametrize("module_name,class_name", [
    ("realtime_release", "RealtimeRelease"), ("security_release", "SecurityRelease"),
])
@pytest.mark.parametrize("existing", [None, "# Existing local review\n\nKeep these notes.\n"])
def test_ignored_report_initializes_and_preserves_local_notes(
    monkeypatch, module_name, class_name, existing,
):
    """A clean clone must work without publishing the deployment journal."""
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module("stayos_deploy." + module_name)

    class MemoryReport:
        content = existing

        def exists(self):
            return self.content is not None

        def read_text(self):
            if self.content is None:
                raise FileNotFoundError("Ignored journal has not been initialized")
            return self.content

        def write_text(self, content):
            self.content = content

    report = MemoryReport()
    monkeypatch.setattr(module, "REPORT", report)
    release = object.__new__(getattr(module, class_name))
    release.args = SimpleNamespace(
        expected_account_id="123456789012", region="us-east-1",
    )
    release.stack = "example-us-east-1"
    state = {
        "account": "123456789012", "region": "us-east-1",
        "stack": release.stack, "release": "test",
    }
    release.save(state)
    assert release.read_state() == state
    if existing:
        assert report.content.startswith(existing)
    else:
        assert report.content.startswith("# StayOS local deployment review")
    # Updating the state must retain notes and exactly one state block.
    release.save({**state, "release": "resumed"})
    assert report.content.count(module.START) == 1
    assert release.read_state()["release"] == "resumed"


@pytest.mark.parametrize("module_name", ["realtime_release", "security_release"])
def test_source_archives_never_read_auth_files_or_symlink_targets(monkeypatch, module_name):
    """Exercise archive creation with entirely in-memory source fixtures."""
    import importlib
    import io
    import zipfile
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    module = importlib.import_module("stayos_deploy." + module_name)
    private_reads = []

    class MemorySource(SimpleNamespace):
        def __lt__(self, other):
            return self.name < other.name

    class MemoryRoot:
        def __truediv__(self, folder):
            files = []
            for name in ["source.py", ".env.local", ".npmrc", ".pypirc", ".netrc", "linked.py"]:
                read = MagicMock(return_value=b"# public source\n")
                if name != "source.py":
                    private_reads.append(read)
                    read.side_effect = AssertionError("Sensitive source must never be read")
                files.append(MemorySource(
                    name=name, suffix=Path(name).suffix,
                    is_file=lambda: True,
                    is_symlink=lambda name=name: name == "linked.py",
                    relative_to=lambda root, name=name: Path(folder) / name,
                    read_bytes=read,
                ))
            return SimpleNamespace(rglob=lambda pattern: files)

    monkeypatch.setattr(module, "ROOT", MemoryRoot())
    body = (
        module.source_archive() if module_name == "realtime_release"
        else module.archive_paths(("lumi/frontend",))
    )
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert all(name.endswith("/source.py") for name in archive.namelist())
        assert archive.read(archive.namelist()[0]) == b"# public source\n"
    assert private_reads
    for read in private_reads:
        read.assert_not_called()
