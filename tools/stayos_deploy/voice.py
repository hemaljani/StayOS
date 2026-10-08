"""Upgrade an existing voice deployment without repackaging unrelated stacks.

AWS calls use the CLI so profiles and CloudFormation's previous-value handling
match the project Makefiles. Baselines contain no CloudFormation parameter values.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml

MODEL_ID = "amazon.nova-2-5-sonic"
PREVIOUS_MODEL_ID = "amazon.nova-2-sonic-v1:0"
ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FIELDS = (
    "agentRuntimeId",
    "agentRuntimeArtifact",
    "roleArn",
    "networkConfiguration",
    "description",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "environmentVariables",
    "metadataConfiguration",
    "filesystemConfigurations",
)


class TemplateLoader(yaml.SafeLoader):
    """Read CloudFormation intrinsic tags without evaluating them."""


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if tag == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag if tag in ("Ref", "Condition") else "Fn::" + tag: value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def load_template(body):
    return yaml.load(body, Loader=TemplateLoader) if isinstance(body, str) else body


def sonic_policy(template):
    policies = template["Resources"]["VoiceAgentCoreRole"]["Properties"]["Policies"]
    return next(p for p in policies if p["PolicyName"] == "BedrockNovaSonicStreaming")


def upgrade_template(baseline, source):
    """Change only the model resources, taking their scope from the source."""
    result = copy.deepcopy(baseline)
    policy = sonic_policy(result)
    desired = sonic_policy(source)
    before = policy["PolicyDocument"]["Statement"]
    after = desired["PolicyDocument"]["Statement"]
    if len(before) != 1 or len(after) != 1:
        raise RuntimeError("Expected exactly one Sonic streaming statement")
    # The authorized change must retain every action, condition and ARN scope.
    expected = json.loads(json.dumps(policy).replace(PREVIOUS_MODEL_ID, MODEL_ID))
    if desired != expected:
        raise RuntimeError("Voice policy differs beyond the authorized model resources")
    if not all(MODEL_ID in json.dumps(arn) for arn in after[0]["Resource"]):
        raise RuntimeError("Source voice policy does not select Sonic 2.5")
    policy["PolicyDocument"]["Statement"][0]["Resource"] = after[0]["Resource"]
    # Attribution metadata does not change any resource configuration.
    result.setdefault("Metadata", {}).setdefault("AWSToolsMetrics", {})[
        "AWSAgentToolkit"
    ] = "aws-cloudformation@3"
    return result


def validate_changes(changes, logical_id, resource_type):
    """Fail closed on additions, removals, replacements or unrelated changes."""
    if len(changes) != 1:
        raise RuntimeError("Expected exactly one resource change")
    change = changes[0]["ResourceChange"]
    if (
        change["LogicalResourceId"] != logical_id
        or change["ResourceType"] != resource_type
        or change["Action"] != "Modify"
        or change.get("Replacement") != "False"
    ):
        raise RuntimeError("Change set includes an unrelated change or replacement")
    return change


def description_template(baseline, source):
    """Refresh the description without changing resource properties or policies."""
    result = copy.deepcopy(baseline)
    result["Description"] = source["Description"]
    result["Resources"]["VoiceAgentCoreRole"].setdefault("Metadata", {})[
        "StayOSDescription"
    ] = source["Description"]
    return result


def validate_parent_changes(changes, parent):
    """Accept nested wrappers only; a hierarchy preview verifies their leaves."""
    nested = {
        key
        for key, value in parent["Resources"].items()
        if value["Type"] == "AWS::CloudFormation::Stack"
    }
    voice = None
    for item in changes:
        change = item["ResourceChange"]
        if (
            change["LogicalResourceId"] not in nested
            or change["ResourceType"] != "AWS::CloudFormation::Stack"
            or change["Action"] != "Modify"
            or change.get("Replacement") != "False"
            or set(change.get("Scope", [])) != {"Properties"}
        ):
            raise RuntimeError("Parent preview includes an unrelated change")
        for detail in change.get("Details", []):
            if detail.get("ChangeSource") == "DirectModification" and (
                change["LogicalResourceId"] != "VoiceStack"
                or detail["Target"].get("Name") != "TemplateURL"
            ):
                raise RuntimeError("Parent preview modifies unrelated properties")
        if change["LogicalResourceId"] == "VoiceStack":
            voice = change
    if voice is None:
        raise RuntimeError("Parent preview has no VoiceStack change")
    return voice


def source_fingerprint():
    """Tie a resumable build to the voice source, excluding test/cache files."""
    service = ROOT / "lumi/backend/services/voice-agent"
    digest = hashlib.sha256()
    for location in sorted(service.rglob("*")):
        relative = location.relative_to(service)
        if (
            not location.is_file()
            or any(
                p in ("tests", "__pycache__", "agentcore", ".pytest_cache")
                for p in relative.parts
            )
            or location.suffix == ".pyc"
        ):
            continue
        digest.update(str(relative).encode())
        digest.update(location.read_bytes())
    return digest.hexdigest()


class VoiceUpgrade:
    def __init__(self, args):
        self.args = args
        self.stack = f"{args.stack_prefix}-{args.region}"
        self.account = args.expected_account_id
        self.bucket = f"{args.stack_prefix}-deploy-{self.account}"
        self.repo = f"{args.stack_prefix}-voice-agent"
        self.directory = None
        self.stack_update_started = False

    def aws(self, *args, cfn=False):
        profile = self.args.cloudformation_profile if cfn else self.args.profile
        completed = subprocess.run(
            [
                "aws",
                "--profile",
                profile,
                "--region",
                self.args.region,
                "--no-cli-pager",
                *args,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "AWS_CLI_AUTO_PROMPT": "off"},
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "AWS command failed")
        output = completed.stdout.strip()
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    def write(self, name, value):
        location = self.directory / name
        location.write_text(json.dumps(value, indent=2) + "\n")
        location.chmod(0o600)
        return location

    def context(self):
        if not all((self.args.profile, self.args.cloudformation_profile, self.account)):
            raise RuntimeError("Explicit profiles and EXPECTED_ACCOUNT_ID are required")
        if not re.fullmatch(r"\d{12}", self.account):
            raise RuntimeError("EXPECTED_ACCOUNT_ID must be a numeric AWS account ID")
        for cfn in (False, True):
            identity = self.aws("sts", "get-caller-identity", cfn=cfn)
            if identity["Account"] != self.account:
                raise RuntimeError("AWS account mismatch; no upgrade started")
        print(
            f"Verified both identities: account {self.account}, {self.args.region}",
            flush=True,
        )

    def template(self, stack):
        return load_template(
            self.aws(
                "cloudformation",
                "get-template",
                "--stack-name",
                stack,
                "--template-stage",
                "Original",
                cfn=True,
            )["TemplateBody"]
        )

    def stack_state(self, stack):
        # Parameter values, including AppPassword, never enter this process.
        return self.aws(
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack,
            "--query",
            "Stacks[0].{id:StackId,status:StackStatus,"
            "updated:LastUpdatedTime,outputs:Outputs,tags:Tags,"
            "parameterKeys:Parameters[].ParameterKey}",
            cfn=True,
        )

    def runtime(self, runtime_id):
        return self.aws(
            "bedrock-agentcore-control",
            "get-agent-runtime",
            "--agent-runtime-id",
            runtime_id,
        )

    def image_digest(self, uri):
        repo_uri = uri.split("@")[0].rsplit(":", 1)[0]
        if "@" in uri:
            repo_uri, digest = uri.split("@", 1)
            image_id = "imageDigest=" + digest
        else:
            image_id = "imageTag=" + uri.rsplit(":", 1)[1]
        if (
            repo_uri
            != f"{self.account}.dkr.ecr.{self.args.region}.amazonaws.com/{self.repo}"
        ):
            raise RuntimeError("Runtime does not use the expected voice repository")
        image = self.aws(
            "ecr",
            "describe-images",
            "--repository-name",
            self.repo,
            "--image-ids",
            image_id,
        )["imageDetails"][0]
        return repo_uri + "@" + image["imageDigest"], image["imagePushedAt"]

    def capture(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = ROOT / "logs" / f"voice-upgrade-{stamp}"
        self.directory.mkdir(parents=True, mode=0o700)
        parent = self.template(self.stack)
        resources = self.aws(
            "cloudformation",
            "list-stack-resources",
            "--stack-name",
            self.stack,
            cfn=True,
        )["StackResourceSummaries"]
        child = next(
            r["PhysicalResourceId"]
            for r in resources
            if r["LogicalResourceId"] == "VoiceStack"
        )
        voice = self.template(child)
        state = self.stack_state(self.stack)
        child_state = self.stack_state(child)
        for current in (state, child_state):
            if current["status"] not in (
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            ):
                raise RuntimeError("Stack is not stable")
        runtime_id = self.aws(
            "ssm",
            "get-parameter",
            "--name",
            f"/{self.args.stack_prefix}/voice/runtime-id",
            "--query",
            "Parameter.Value",
            "--output",
            "text",
        )
        runtime = self.runtime(runtime_id)
        endpoint = self.aws(
            "bedrock-agentcore-control",
            "get-agent-runtime-endpoint",
            "--agent-runtime-id",
            runtime_id,
            "--endpoint-name",
            "DEFAULT",
        )
        if runtime["status"] != "READY" or endpoint["status"] != "READY":
            raise RuntimeError("Existing runtime and DEFAULT endpoint must be READY")
        if endpoint["liveVersion"] != runtime["agentRuntimeVersion"]:
            raise RuntimeError("DEFAULT does not reference the current runtime version")
        uri = runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        pinned, pushed = self.image_digest(uri)
        if "@" not in uri and (
            datetime.fromisoformat(pushed)
            > datetime.fromisoformat(runtime["lastUpdatedAt"])
        ):
            raise RuntimeError(
                "Image tag changed after runtime update; rollback digest is ambiguous"
            )
        request = {k: runtime[k] for k in RUNTIME_FIELDS if k in runtime}
        # Stop rather than persist credentials if configuration ever adds secrets.
        if any(
            re.search(r"password|secret|token|credential|access_key", k, re.I)
            for k in request.get("environmentVariables", {})
        ):
            raise RuntimeError(
                "Runtime has secret environment variables; baseline cannot be saved"
            )
        request["agentRuntimeArtifact"]["containerConfiguration"][
            "containerUri"
        ] = pinned
        self.write("parent-before.json", parent)
        self.write("voice-before.json", voice)
        self.write("runtime-before.json", request)
        baseline = {
            "account": self.account,
            "region": self.args.region,
            "stack": self.stack,
            "child": child,
            "runtimeId": runtime_id,
            "version": runtime["agentRuntimeVersion"],
            "image": pinned,
            "parentState": state,
            "childState": child_state,
        }
        self.write("baseline.json", baseline)
        print(
            f"Rollback baseline: {self.directory}\n"
            f"Current runtime version: {baseline['version']}; image pinned by digest",
            flush=True,
        )
        return baseline, parent, voice

    def preview(
        self, stack, template_file, logical_id, resource_type, metadata_only=False
    ):
        state = self.stack_state(stack)
        tags = [f"{t['Key']}={t['Value']}" for t in state.get("tags", [])]
        output = self.aws(
            "cloudformation",
            "deploy",
            "--stack-name",
            stack,
            "--template-file",
            str(template_file),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--no-execute-changeset",
            "--s3-bucket",
            self.bucket,
            "--s3-prefix",
            "voice-upgrade",
            *(["--tags", *tags] if tags else []),
            cfn=True,
        )
        match = re.search(r"arn:[^\s]+:changeSet/[^\s]+", output)
        if not match:
            raise RuntimeError("CloudFormation deploy did not return a change-set ARN")
        arn = match.group(0)
        details = self.aws(
            "cloudformation",
            "describe-change-set",
            "--change-set-name",
            arn,
            cfn=True,
        )
        # DescribeChangeSet omits UsePreviousValue in actual service responses.
        # CLI deploy, invoked without overrides, sends previous values for every
        # existing parameter. Check the full key set and reject explicit resets.
        parameters = details.get("Parameters", [])
        if {p["ParameterKey"] for p in parameters} != set(
            state["parameterKeys"]
        ) or any(p.get("UsePreviousValue") is False for p in parameters):
            raise RuntimeError("Change set does not preserve all parameter values")
        if logical_id == "VoiceStack":
            change = validate_parent_changes(
                details.get("Changes", []),
                json.loads(template_file.read_text()),
            )
        else:
            change = validate_changes(
                details.get("Changes", []), logical_id, resource_type
            )
            if metadata_only and set(change.get("Scope", [])) != {"Metadata"}:
                raise RuntimeError("Description preview changes resource properties")
        self.write(
            f"preview-{logical_id}.json",
            {
                "changeSetId": arn,
                "changes": details["Changes"],
                "parametersUsePreviousValue": True,
            },
        )
        print(
            f"Preview: Modify {change['LogicalResourceId']} "
            f"({change['ResourceType']}), no replacement",
            flush=True,
        )
        return arn

    def preview_hierarchy(self, parent_file, metadata_only=False):
        """Resolve automatic nested wrappers into actual resource changes."""
        state = self.stack_state(self.stack)
        request = {
            "StackName": self.stack,
            "ChangeSetName": "voice-hierarchy-"
            + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
            "ChangeSetType": "UPDATE",
            "TemplateBody": parent_file.read_text(),
            "Parameters": [
                {"ParameterKey": key, "UsePreviousValue": True}
                for key in state["parameterKeys"]
            ],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "IncludeNestedStacks": True,
            "Tags": state.get("tags", []),
        }
        location = self.write("hierarchy-request.json", request)
        arn = self.aws(
            "cloudformation",
            "create-change-set",
            "--cli-input-json",
            f"file://{location}",
            cfn=True,
        )["Id"]
        self.aws(
            "cloudformation",
            "wait",
            "change-set-create-complete",
            "--change-set-name",
            arn,
            cfn=True,
        )
        root = self.aws(
            "cloudformation",
            "describe-change-set",
            "--change-set-name",
            arn,
            cfn=True,
        )
        validate_parent_changes(root["Changes"], json.loads(parent_file.read_text()))
        leaves = []
        for item in root["Changes"]:
            wrapper = item["ResourceChange"]
            child_arn = wrapper.get("ChangeSetId")
            if not child_arn:
                raise RuntimeError("Hierarchy preview did not resolve a nested stack")
            child = self.aws(
                "cloudformation",
                "describe-change-set",
                "--change-set-name",
                child_arn,
                cfn=True,
            )
            changes = child.get("Changes", [])
            if wrapper["LogicalResourceId"] == "VoiceStack":
                change = validate_changes(
                    changes, "VoiceAgentCoreRole", "AWS::IAM::Role"
                )
                if metadata_only and set(change.get("Scope", [])) != {"Metadata"}:
                    raise RuntimeError(
                        "Description hierarchy changes resource properties"
                    )
                leaves.extend(changes)
            elif changes:
                raise RuntimeError("Hierarchy preview includes unrelated resources")
            self.write(
                "hierarchy-" + wrapper["LogicalResourceId"] + ".json",
                {
                    "changeSetId": child_arn,
                    "changes": changes,
                },
            )
        if len(leaves) != 1:
            raise RuntimeError("Expected exactly one voice resource in the hierarchy")
        self.write(
            "hierarchy-root.json",
            {
                "changeSetId": arn,
                "changes": root["Changes"],
            },
        )
        self.aws(
            "cloudformation",
            "delete-change-set",
            "--change-set-name",
            arn,
            cfn=True,
        )
        print(
            "Hierarchy preview: only VoiceAgentCoreRole changes; "
            "all other nested resources unchanged",
            flush=True,
        )

    def update_stack(
        self, parent, voice, baseline, rollback=False, metadata_only=False
    ):
        if rollback:
            voice_file = self.directory / "voice-before.json"
        else:
            voice_file = self.write("voice-upgrade.json", voice)
        # Preview the nested resource without executing a direct child update.
        nested_preview = self.preview(
            baseline["child"],
            voice_file,
            "VoiceAgentCoreRole",
            "AWS::IAM::Role",
            metadata_only=metadata_only,
        )
        self.aws(
            "cloudformation",
            "delete-change-set",
            "--change-set-name",
            nested_preview,
            cfn=True,
        )
        if rollback:
            parent_file = self.directory / "parent-before.json"
        else:
            digest = hashlib.sha256(voice_file.read_bytes()).hexdigest()
            key = f"voice-upgrade/{digest}.template"
            self.aws(
                "s3", "cp", str(voice_file), f"s3://{self.bucket}/{key}", "--quiet"
            )
            updated = copy.deepcopy(parent)
            updated["Resources"]["VoiceStack"]["Properties"][
                "TemplateURL"
            ] = f"https://s3.{self.args.region}.amazonaws.com/{self.bucket}/{key}"
            parent_file = self.write("parent-upgrade.json", updated)
        parent_preview = self.preview(
            self.stack,
            parent_file,
            "VoiceStack",
            "AWS::CloudFormation::Stack",
        )
        self.preview_hierarchy(parent_file, metadata_only=metadata_only)
        # Guard against concurrent stack updates after taking the baseline.
        for stack, saved in (
            (self.stack, "parentState"),
            (baseline["child"], "childState"),
        ):
            current = self.stack_state(stack)
            if not rollback and any(
                current[k] != value for k, value in baseline[saved].items()
            ):
                raise RuntimeError(
                    "Stack changed since baseline; refusing stale upgrade"
                )
        print(
            "Applying reviewed parent change set; parameters retain previous values",
            flush=True,
        )
        self.stack_update_started = True
        self.aws(
            "cloudformation",
            "execute-change-set",
            "--change-set-name",
            parent_preview,
            cfn=True,
        )
        self.aws(
            "cloudformation",
            "wait",
            "stack-update-complete",
            "--stack-name",
            self.stack,
            cfn=True,
        )

    def update_runtime(self, request):
        location = self.write("runtime-update.json", request)
        result = self.aws(
            "bedrock-agentcore-control",
            "update-agent-runtime",
            "--cli-input-json",
            f"file://{location}",
        )
        version = result["agentRuntimeVersion"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            runtime = self.runtime(request["agentRuntimeId"])
            endpoint = self.aws(
                "bedrock-agentcore-control",
                "get-agent-runtime-endpoint",
                "--agent-runtime-id",
                request["agentRuntimeId"],
                "--endpoint-name",
                "DEFAULT",
            )
            if runtime["status"] == "FAILED" or endpoint["status"] == "FAILED":
                raise RuntimeError("Runtime or DEFAULT endpoint failed")
            if (
                runtime["status"] == endpoint["status"] == "READY"
                and endpoint["liveVersion"] == version
            ):
                print(f"Voice runtime version {version}; DEFAULT READY", flush=True)
                return version
            print(
                f"Waiting for voice runtime/DEFAULT: "
                f"{runtime['status']}/{endpoint['status']}",
                flush=True,
            )
            time.sleep(10)
        raise RuntimeError("Timed out waiting for the new DEFAULT runtime version")

    def rollback(self, baseline):
        parent = json.loads((self.directory / "parent-before.json").read_text())
        voice = json.loads((self.directory / "voice-before.json").read_text())
        # CFN may already have rolled back a failed parent update.
        current = self.template(self.stack)
        if current != parent:
            expected = copy.deepcopy(current)
            expected["Resources"]["VoiceStack"]["Properties"]["TemplateURL"] = parent[
                "Resources"
            ]["VoiceStack"]["Properties"]["TemplateURL"]
            if expected != parent:
                raise RuntimeError(
                    "Unrelated parent template changes prevent a scoped rollback"
                )
            self.update_stack(parent, voice, baseline, rollback=True)
        request = json.loads((self.directory / "runtime-before.json").read_text())
        version = self.update_runtime(request)
        self.write(
            "rollback-result.json", {"version": version, "image": baseline["image"]}
        )
        print(
            "Previous voice policy and digest restored; functional testing required",
            flush=True,
        )

    def upgrade(self):
        subprocess.run(
            [
                "make",
                "plan",
                f"PROFILE={self.args.profile}",
                f"CLOUDFORMATION_PROFILE={self.args.cloudformation_profile}",
                f"REGION={self.args.region}",
                f"EXPECTED_ACCOUNT_ID={self.account}",
                f"LUMI_STACK_PREFIX={self.args.stack_prefix}",
            ],
            cwd=ROOT,
            check=True,
        )
        model = self.aws(
            "bedrock", "get-foundation-model", "--model-identifier", MODEL_ID
        )["modelDetails"]
        availability = self.aws(
            "bedrock", "get-foundation-model-availability", "--model-id", MODEL_ID
        )
        if (
            model["modelLifecycle"]["status"] != "ACTIVE"
            or not model["responseStreamingSupported"]
            or availability["regionAvailability"] != "AVAILABLE"
            or availability["authorizationStatus"] != "AUTHORIZED"
        ):
            raise RuntimeError("Sonic 2.5 is not active, available and authorized")
        if self.args.baseline:
            baseline = self.load_baseline()
            parent = json.loads((self.directory / "parent-before.json").read_text())
            voice = json.loads((self.directory / "voice-before.json").read_text())
        else:
            baseline, parent, voice = self.capture()
        source = load_template(
            (ROOT / "lumi/infrastructure/nested-stacks/voice.yaml").read_text()
        )
        updated = upgrade_template(voice, source)
        if self.args.baseline:
            built = json.loads((self.directory / "built-image.json").read_text())
            if built["sourceFingerprint"] != source_fingerprint():
                raise RuntimeError("Voice source changed after the saved build")
            image, _ = self.image_digest(built["image"])
            print("Reusing the saved voice build, pinned by digest", flush=True)
        else:
            print("Building only the existing voice CodeBuild project", flush=True)
            subprocess.run(
                [
                    "make",
                    "-C",
                    str(ROOT / "lumi"),
                    "voice-build",
                    f"PROFILE={self.args.profile}",
                    f"CLOUDFORMATION_PROFILE={self.args.cloudformation_profile}",
                    f"REGION={self.args.region}",
                    f"ACCOUNT_ID={self.account}",
                    f"EXPECTED_ACCOUNT_ID={self.account}",
                    f"STACK_PREFIX={self.args.stack_prefix}",
                ],
                check=True,
            )
            image, _ = self.image_digest(
                f"{self.account}.dkr.ecr.{self.args.region}.amazonaws.com/"
                f"{self.repo}:latest"
            )
            self.write(
                "built-image.json",
                {
                    "image": image,
                    "sourceFingerprint": source_fingerprint(),
                },
            )
        if (
            self.runtime(baseline["runtimeId"])["agentRuntimeVersion"]
            != baseline["version"]
        ):
            raise RuntimeError("Runtime changed since baseline; refusing stale upgrade")
        try:
            self.update_stack(parent, updated, baseline)
            request = json.loads((self.directory / "runtime-before.json").read_text())
            request["agentRuntimeArtifact"]["containerConfiguration"][
                "containerUri"
            ] = image
            version = self.update_runtime(request)
        except Exception:
            if self.stack_update_started:
                print(
                    "Upgrade failed; restoring saved policy and pinned image",
                    flush=True,
                )
                self.rollback(baseline)
            else:
                print(
                    "Preview failed; no stack or runtime update was started", flush=True
                )
            raise
        self.write(
            "result.json", {"version": version, "image": image, "model": MODEL_ID}
        )
        print(
            f"Deployment readiness verified. Live voice testing remains required.\n"
            f"Baseline: {self.directory}",
            flush=True,
        )

    def refresh_description(self):
        """Apply only stack/resource metadata; never build or update a runtime."""
        subprocess.run(
            [
                "make",
                "plan",
                f"PROFILE={self.args.profile}",
                f"CLOUDFORMATION_PROFILE={self.args.cloudformation_profile}",
                f"REGION={self.args.region}",
                f"EXPECTED_ACCOUNT_ID={self.account}",
                f"LUMI_STACK_PREFIX={self.args.stack_prefix}",
            ],
            cwd=ROOT,
            check=True,
        )
        baseline, parent, voice = self.capture()
        source = load_template(
            (ROOT / "lumi/infrastructure/nested-stacks/voice.yaml").read_text()
        )
        updated = description_template(voice, source)
        if updated == voice:
            print("Voice description is already current", flush=True)
            return
        self.update_stack(parent, updated, baseline, metadata_only=True)
        actual = self.template(baseline["child"])
        if actual["Description"] != updated["Description"]:
            raise RuntimeError("Deployed voice description did not match the source")
        if (
            self.runtime(baseline["runtimeId"])["agentRuntimeVersion"]
            != baseline["version"]
        ):
            raise RuntimeError("Voice runtime changed during the metadata update")
        self.write(
            "description-result.json",
            {
                "description": actual["Description"],
                "runtimeVersion": baseline["version"],
            },
        )
        print(
            "Voice description updated; permissions and runtime unchanged", flush=True
        )

    def load_baseline(self):
        self.directory = self.args.baseline.resolve()
        baseline = json.loads((self.directory / "baseline.json").read_text())
        if (baseline["account"], baseline["region"], baseline["stack"]) != (
            self.account,
            self.args.region,
            self.stack,
        ):
            raise RuntimeError("Baseline does not match deployment context")
        return baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "rollback", "description"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cloudformation-profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--stack-prefix", default="stayos")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    upgrade = VoiceUpgrade(args)
    try:
        upgrade.context()
        if args.action == "upgrade":
            upgrade.upgrade()
        elif args.action == "description":
            upgrade.refresh_description()
        else:
            if not args.baseline:
                raise RuntimeError("VOICE_BASELINE is required for rollback")
            upgrade.rollback(upgrade.load_baseline())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
