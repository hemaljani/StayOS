"""Release the forecast invoker through the existing nested CloudFormation stack.

Preserve deployed templates instead of replaying unrelated checkout differences.
AWS operations use the CLI and explicit profiles; prepare/preview/deploy are
separate, resumable phases. No data seeding or other Lambda refresh occurs.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import re
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .voice import ROOT, load_template

STABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
ALLOWED = {
    "PipelineStack": {
        "PulseForecasterInvokerRole": ("Modify", "AWS::IAM::Role"),
        "PulseForecasterInvokerFunction": ("Modify", "AWS::Lambda::Function"),
        "PulseBackendBuildProject": ("Modify", "AWS::CodeBuild::Project"),
    },
    "ObservabilityStack": {
        "ForecastRunCompletedMetricFilter": ("Add", "AWS::Logs::MetricFilter"),
        "PulseOverviewDashboard": ("Modify", "AWS::CloudWatch::Dashboard"),
    },
}


def patch_pipeline(
    baseline: dict[str, Any], source: dict[str, Any], code_key: str
) -> dict[str, Any]:
    """Apply only forecast permission, handler, code, timeout and build guard."""
    result = copy.deepcopy(baseline)
    resources = result["Resources"]
    desired = source["Resources"]
    result.setdefault("Conditions", {})["HasForecastRuntimeArn"] = source["Conditions"][
        "HasForecastRuntimeArn"
    ]
    policies = resources["PulseForecasterInvokerRole"]["Properties"]["Policies"]
    matches = [
        i
        for i, policy in enumerate(policies)
        if policy.get("PolicyName") == "InvokeForecasterRuntime"
    ]
    if len(matches) != 1:
        raise RuntimeError("Expected one existing forecast invocation policy")
    policies[matches[0]] = desired["PulseForecasterInvokerRole"]["Properties"][
        "Policies"
    ][0]
    function = resources["PulseForecasterInvokerFunction"]["Properties"]
    if function["Runtime"] != "python3.12":
        raise RuntimeError("Existing forecast Lambda must use Python 3.12")
    for key in ("Handler", "Timeout", "Description"):
        function[key] = desired["PulseForecasterInvokerFunction"]["Properties"][key]
    function["Code"] = {"S3Bucket": {"Ref": "DeployBucket"}, "S3Key": code_key}
    resources["PulseBackendBuildProject"]["Properties"]["Source"]["BuildSpec"] = (
        desired["PulseBackendBuildProject"]["Properties"]["Source"]["BuildSpec"]
    )
    return result


def patch_observability(
    baseline: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Add only completion monitoring while preserving the deployed dashboard."""
    result = copy.deepcopy(baseline)
    logical = "ForecastRunCompletedMetricFilter"
    if logical in result["Resources"]:
        raise RuntimeError("Completion metric already exists; review before rerelease")
    result["Resources"][logical] = source["Resources"][logical]
    dashboard = result["Resources"]["PulseOverviewDashboard"]["Properties"]
    body = dashboard["DashboardBody"]
    if not isinstance(body, dict) or set(body) != {"Fn::Sub"}:
        raise RuntimeError("Expected the existing substituted dashboard")
    document = json.loads(body["Fn::Sub"])
    widgets = [
        widget
        for widget in document["widgets"]
        if any(
            isinstance(metric, list) and "ForecastRunFailures" in metric
            for metric in widget.get("properties", {}).get("metrics", [])
        )
    ]
    if len(widgets) != 1:
        raise RuntimeError("Expected one existing forecast dashboard widget")
    widgets[0]["properties"]["metrics"].insert(
        1,
        ["PULSE/Pipeline", "ForecastRunCompleted", {"label": "Completed invocations"}],
    )
    body["Fn::Sub"] = json.dumps(document)
    return result


def validate_leaf_changes(
    changes: list[dict[str, Any]], expected: dict[str, tuple[str, str]]
) -> None:
    """Reject unrelated leaves, deletions or replacements before execution."""
    seen = set()
    for item in changes:
        change = item["ResourceChange"]
        logical = change["LogicalResourceId"]
        if logical not in expected:
            raise RuntimeError(f"Unrelated change: {logical}")
        action, resource_type = expected[logical]
        if (change["Action"], change["ResourceType"]) != (action, resource_type):
            raise RuntimeError(f"Unexpected change: {logical}")
        if change.get("Replacement") not in (None, "False"):
            raise RuntimeError(f"Replacement refused: {logical}")
        seen.add(logical)
    if seen != set(expected):
        raise RuntimeError(f"Expected changes missing: {set(expected) - seen}")


class ForecastRelease:
    """Capture, build and apply a narrowly validated existing-stack release."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.directory = Path(args.directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stack = f"{args.stack_prefix}-{args.region}"
        self.base = ["aws", "--profile", args.profile, "--region", args.region]

    def aws(self, *args: str, cfn: bool = False, text: bool = False) -> Any:
        command = list(self.base)
        if cfn:
            command[2] = self.args.cloudformation_profile
        command += list(args) + [
            "--no-cli-pager",
            "--output",
            "text" if text else "json",
        ]
        process = subprocess.run(command, check=False, capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError(process.stderr.strip())
        return process.stdout if text else json.loads(process.stdout or "{}")

    def write(self, name: str, value: Any) -> Path:
        path = self.directory / name
        path.write_text(json.dumps(value, indent=2))
        return path

    def read(self, name: str) -> Any:
        return json.loads((self.directory / name).read_text())

    def identity(self) -> None:
        for cfn in (False, True):
            identity = self.aws("sts", "get-caller-identity", cfn=cfn)
            if identity["Account"] != self.args.expected_account_id:
                raise RuntimeError("AWS account mismatch; no writes permitted")
            print(
                f"Verified {'CloudFormation' if cfn else 'build'} role: {identity['Arn']}",
                flush=True,
            )
        print(f"Target: {self.stack}; region: {self.args.region}", flush=True)
        if (self.directory / "manifest.json").exists():
            manifest = self.read("manifest.json")
            if (
                manifest["account"] != self.args.expected_account_id
                or manifest["region"] != self.args.region
                or manifest["stack"] != self.stack
            ):
                raise RuntimeError("Release manifest belongs to a different target")

    def current_functions(self) -> list[dict[str, Any]]:
        return self.aws(
            "lambda",
            "list-functions",
            "--query",
            f"Functions[?starts_with(FunctionName, '{self.args.stack_prefix}-')]."
            "{name:FunctionName,sha:CodeSha256,handler:Handler}",
        )

    def template(self, stack: str) -> dict[str, Any]:
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

    def prepare(self) -> None:
        self.identity()
        if (self.directory / "manifest.json").exists():
            raise RuntimeError("Release already prepared; use preview or deploy")
        state = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", self.stack, cfn=True
        )["Stacks"][0]
        if state["StackStatus"] not in STABLE:
            raise RuntimeError("Root stack is not stable")
        parameters = {
            p["ParameterKey"]: p["ParameterValue"] for p in state["Parameters"]
        }
        bucket = parameters["DeployBucket"]
        runtime = parameters["ForecastRuntimeArn"]
        if not runtime.startswith(
            f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:runtime/"
        ):
            raise RuntimeError("Existing forecast runtime account/region is incorrect")
        release = "forecast-invoker/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        code_key = f"functions/{release}/pulse-backend.zip"
        resources = self.aws(
            "cloudformation",
            "describe-stack-resources",
            "--stack-name",
            self.stack,
            cfn=True,
        )["StackResources"]
        children = {
            r["LogicalResourceId"]: r["PhysicalResourceId"]
            for r in resources
            if r["ResourceType"] == "AWS::CloudFormation::Stack"
        }
        baseline_root = self.template(self.stack)
        self.write("baseline-root.json", baseline_root)
        baseline_children = {}
        for logical, arn in children.items():
            baseline_children[logical] = self.template(arn)
            self.write(f"baseline-{logical}.json", baseline_children[logical])
        source_dir = ROOT / "pulse/infrastructure/nested-stacks"
        pipeline_source = load_template(
            (source_dir / "pulse-pipeline.yaml").read_text()
        )
        observability_source = load_template(
            (source_dir / "pulse-observability.yaml").read_text()
        )
        pipeline = patch_pipeline(
            baseline_children["PipelineStack"], pipeline_source, code_key
        )
        observability = patch_observability(
            baseline_children["ObservabilityStack"], observability_source
        )
        self.write("pipeline.json", pipeline)
        self.write("observability.json", observability)
        manifest = {
            "account": self.args.expected_account_id,
            "region": self.args.region,
            "stack": self.stack,
            "bucket": bucket,
            "release": release,
            "codeKey": code_key,
            "runtime": runtime,
            "children": children,
            "functions": self.current_functions(),
            "tags": state.get("Tags", []),
            "parameterKeys": sorted(parameters),
        }
        self.write("manifest.json", manifest)
        # Stage the same inputs as make package-backend, at a unique release key.
        source_zip = self.directory / "backend-source.zip"
        backend = ROOT / "pulse/backend"
        with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in [
                backend / "pyproject.toml",
                backend / "README.md",
                *sorted((backend / "src").rglob("*")),
            ]:
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and not any(part.endswith(".egg-info") for part in path.parts)
                    and path.suffix != ".pyc"
                ):
                    archive.write(path, path.relative_to(backend))
        source_key = f"backend-source/{release}/backend-source.zip"
        self.aws(
            "s3",
            "cp",
            str(source_zip),
            f"s3://{bucket}/{source_key}",
            "--only-show-errors",
            text=True,
        )
        build = self.aws(
            "codebuild",
            "start-build",
            "--project-name",
            f"{self.args.stack_prefix}-backend-build",
            "--source-location-override",
            f"{bucket}/{source_key}",
            "--buildspec-override",
            pipeline_source["Resources"]["PulseBackendBuildProject"]["Properties"][
                "Source"
            ]["BuildSpec"],
            "--environment-variables-override",
            json.dumps(
                [
                    {"name": "DEPLOY_BUCKET", "value": bucket, "type": "PLAINTEXT"},
                    {
                        "name": "LAMBDA_CODE_S3_KEY",
                        "value": code_key,
                        "type": "PLAINTEXT",
                    },
                ]
            ),
        )["build"]
        manifest["buildId"] = build["id"]
        self.write("manifest.json", manifest)
        print(f"CodeBuild started: {build['id']}", flush=True)
        self.finish_build()

    def finish_build(self) -> None:
        """Poll the recorded build; never restart a timed-out observer."""
        manifest = self.read("manifest.json")
        while True:
            build = self.aws(
                "codebuild", "batch-get-builds", "--ids", manifest["buildId"]
            )["builds"][0]
            status = build["buildStatus"]
            print(f"Build: {status}; phase: {build.get('currentPhase')}", flush=True)
            if status == "SUCCEEDED":
                break
            if status != "IN_PROGRESS":
                raise RuntimeError(
                    f"Build failed: {status}; inspect {manifest['buildId']}"
                )
            time.sleep(15)
        archive = self.directory / "pulse-backend.zip"
        self.aws(
            "s3",
            "cp",
            f"s3://{manifest['bucket']}/{manifest['codeKey']}",
            str(archive),
            "--only-show-errors",
            text=True,
        )
        with zipfile.ZipFile(archive) as package:
            expected = ROOT / "pulse/backend/src/pulse/forecast_invoker.py"
            if package.read("pulse/forecast_invoker.py") != expected.read_bytes():
                raise RuntimeError("Built handler differs from the reviewed source")
        manifest["codeSha256"] = base64.b64encode(
            hashlib.sha256(archive.read_bytes()).digest()
        ).decode()
        self.write("manifest.json", manifest)
        print(
            "Built archive matches the current handler; isolated import passed in CodeBuild Python 3.12.",
            flush=True,
        )

    def preview(self) -> None:
        self.identity()
        manifest = self.read("manifest.json")
        if "codeSha256" not in manifest:
            raise RuntimeError("Build must succeed before preview")
        if self.template(self.stack) != self.read("baseline-root.json"):
            raise RuntimeError("Root changed since baseline capture")
        for logical, arn in manifest["children"].items():
            if self.template(arn) != self.read(f"baseline-{logical}.json"):
                raise RuntimeError(f"{logical} changed since capture")
        if manifest.get("hierarchyChangeSet"):
            self.inspect_preview(manifest)
            return
        root = self.read("baseline-root.json")
        for logical, filename in [
            ("PipelineStack", "pipeline.json"),
            ("ObservabilityStack", "observability.json"),
        ]:
            key = f"cfn-templates/{manifest['release']}/{filename}"
            self.aws(
                "s3",
                "cp",
                str(self.directory / filename),
                f"s3://{manifest['bucket']}/{key}",
                "--only-show-errors",
                text=True,
            )
            root["Resources"][logical]["Properties"]["TemplateURL"] = (
                f"https://s3.{self.args.region}.amazonaws.com/{manifest['bucket']}/{key}"
            )
        root_path = self.write("root.json", root)
        # Use the project's CloudFormation deploy convention without executing.
        output = self.aws(
            "cloudformation",
            "deploy",
            "--stack-name",
            self.stack,
            "--template-file",
            str(root_path),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--no-execute-changeset",
            "--s3-bucket",
            manifest["bucket"],
            "--s3-prefix",
            f"cfn-templates/{manifest['release']}",
            cfn=True,
            text=True,
        )
        match = re.search(r"arn:[^\s]+:changeSet/[^\s]+", output)
        if not match:
            raise RuntimeError("CloudFormation deploy returned no change set")
        manifest["deployChangeSet"] = match.group(0)
        self.write("manifest.json", manifest)
        # A nested preview proves the leaf scope before execution.
        request = {
            "StackName": self.stack,
            "ChangeSetName": manifest["release"].replace("/", "-"),
            "ChangeSetType": "UPDATE",
            "TemplateBody": root_path.read_text(),
            "Parameters": [
                {"ParameterKey": key, "UsePreviousValue": True}
                for key in manifest["parameterKeys"]
            ],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "IncludeNestedStacks": True,
            "Tags": manifest["tags"],
        }
        request_path = self.write("hierarchy-request.json", request)
        arn = self.aws(
            "cloudformation",
            "create-change-set",
            "--cli-input-json",
            f"file://{request_path}",
            cfn=True,
        )["Id"]
        manifest["hierarchyChangeSet"] = arn
        self.write("manifest.json", manifest)
        self.aws(
            "cloudformation",
            "wait",
            "change-set-create-complete",
            "--change-set-name",
            arn,
            cfn=True,
        )
        self.inspect_preview(manifest)

    def inspect_preview(self, manifest: dict[str, Any]) -> None:
        """Inspect resolved properties, excluding unchanged dynamic references."""
        arn = manifest["hierarchyChangeSet"]
        hierarchy = self.aws(
            "cloudformation",
            "describe-change-set",
            "--change-set-name",
            arn,
            "--include-property-values",
            cfn=True,
        )
        if (
            hierarchy.get("Status") != "CREATE_COMPLETE"
            or hierarchy.get("ExecutionStatus") != "AVAILABLE"
        ):
            raise RuntimeError("Hierarchy change set is not ready for execution")
        self.write("hierarchy.json", hierarchy)
        seen = set()
        for item in hierarchy.get("Changes", []):
            change = item["ResourceChange"]
            logical = change["LogicalResourceId"]
            if (
                change["Action"] != "Modify"
                or change["ResourceType"] != "AWS::CloudFormation::Stack"
                or change.get("Replacement") != "False"
            ):
                raise RuntimeError(f"Root replacement or unrelated resource: {logical}")
            child_arn = change.get("ChangeSetId")
            if not child_arn:
                raise RuntimeError("Nested preview missing child change set")
            child = self.aws(
                "cloudformation",
                "describe-change-set",
                "--change-set-name",
                child_arn,
                "--include-property-values",
                cfn=True,
            )
            self.write(f"preview-{logical}.json", child)
            validate_leaf_changes(child.get("Changes", []), ALLOWED.get(logical, {}))
            if child.get("Changes"):
                seen.add(logical)
            for leaf in child.get("Changes", []):
                print(
                    f"Preview: {leaf['ResourceChange']['Action']} {logical}/{leaf['ResourceChange']['LogicalResourceId']}; no replacement",
                    flush=True,
                )
        if seen != set(ALLOWED):
            raise RuntimeError("Expected forecast changes missing from hierarchy")
        manifest["previewPassed"] = True
        self.write("manifest.json", manifest)

    def deploy(self) -> None:
        self.identity()
        manifest = self.read("manifest.json")
        if not manifest.get("previewPassed"):
            raise RuntimeError("Reviewed hierarchy preview is required")
        # Deploy the reviewed nested change set, not a newly generated preview.
        self.aws(
            "cloudformation",
            "execute-change-set",
            "--change-set-name",
            manifest["hierarchyChangeSet"],
            cfn=True,
        )
        manifest["executionStarted"] = True
        self.write("manifest.json", manifest)
        print("Executing reviewed forecast change set.", flush=True)

    def verify(self) -> None:
        self.identity()
        manifest = self.read("manifest.json")
        stack = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", self.stack, cfn=True
        )["Stacks"][0]
        print(f"Stack status: {stack['StackStatus']}", flush=True)
        if stack["StackStatus"] != "UPDATE_COMPLETE":
            raise RuntimeError("Deployment has not completed successfully")
        function = self.aws(
            "lambda",
            "get-function-configuration",
            "--function-name",
            f"{self.args.stack_prefix}-forecaster-invoker",
        )
        for key, expected in [
            ("Handler", "pulse.forecast_invoker.lambda_handler"),
            ("Timeout", 600),
            ("CodeSha256", manifest["codeSha256"]),
            ("State", "Active"),
            ("LastUpdateStatus", "Successful"),
        ]:
            if function.get(key) != expected:
                raise RuntimeError(f"Deployed {key} mismatch")
        if (
            function["Environment"]["Variables"]["FORECAST_AGENT_RUNTIME_ARN"]
            != manifest["runtime"]
        ):
            raise RuntimeError("Deployed runtime changed")
        expected_templates = {
            "PipelineStack": "pipeline.json",
            "ObservabilityStack": "observability.json",
        }
        if self.template(self.stack) != self.read("root.json"):
            raise RuntimeError("Deployed root template differs from the release")
        for logical, arn in manifest["children"].items():
            expected = expected_templates.get(logical, f"baseline-{logical}.json")
            if self.template(arn) != self.read(expected):
                raise RuntimeError(f"Deployed {logical} template differs")
        role_name = function["Role"].rsplit("/", 1)[-1]
        policy = self.aws(
            "iam",
            "get-role-policy",
            "--role-name",
            role_name,
            "--policy-name",
            "InvokeForecasterRuntime",
        )["PolicyDocument"]
        if policy["Statement"] != [
            {
                "Effect": "Allow",
                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                "Resource": [
                    manifest["runtime"],
                    manifest["runtime"] + "/runtime-endpoint/DEFAULT",
                ],
            }
        ]:
            raise RuntimeError("Deployed forecast permission differs")
        before = {f["name"]: f for f in manifest["functions"]}
        after = {f["name"]: f for f in self.current_functions()}
        target = f"{self.args.stack_prefix}-forecaster-invoker"
        if set(before) != set(after) or any(
            after[name] != value for name, value in before.items() if name != target
        ):
            raise RuntimeError("An unrelated PULSE Lambda changed")
        manifest["deploymentVerified"] = True
        self.write("manifest.json", manifest)
        print(
            "PASS: forecast configuration/hash verified; every other PULSE Lambda hash/handler preserved.",
            flush=True,
        )


def main() -> None:
    """Run one explicit release phase without committing or seeding data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "finish-build", "preview", "deploy", "verify"]
    )
    parser.add_argument("--directory", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cloudformation-profile", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-prefix", default="pulse")
    args = parser.parse_args()
    release = ForecastRelease(args)
    getattr(release, args.phase.replace("-", "_"))()


if __name__ == "__main__":
    main()
