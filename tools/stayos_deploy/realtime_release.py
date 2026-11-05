"""Release PULSE realtime auth and recovery without local temporary files.

Build artifacts and CloudFormation rollback inputs remain in the deploy bucket.
The root review is the sole local release record. Deployed templates are patched
selectively so unrelated application/configuration differences are preserved.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .forecast_release import validate_leaf_changes
from .voice import ROOT, load_template

REPORT = ROOT / "AWS_WELL_ARCHITECTED_REVIEW.md"
START = "<!-- realtime-release-state:start -->"
END = "<!-- realtime-release-state:end -->"
API_ROLES = {"PulseApiRole", "PulseTriageAgentCoreRole", "PulseForecasterAgentCoreRole"}
PIPELINE_ROLES = {
    "PulseEscalationServiceRole", "PulsePushServiceRole", "PulseInfoBatcherRole",
    "PulseAlertTimeoutSweeperRole", "PulseActionExecutorRole",
}
ALLOWED = {
    "ApiStack": {
        **{name: ("Modify", "AWS::IAM::Role") for name in API_ROLES},
        "PulseRealtimeEventApi": ("Modify", "AWS::AppSync::Api"),
        "PulseRealtimeNamespace": ("Modify", "AWS::AppSync::ChannelNamespace"),
        "PulseRealtimePublishProbe": ("Add", "AWS::Lambda::Function"),
        "PulseRealtimeDeniedProbe": ("Add", "AWS::Lambda::Function"),
    },
    "PipelineStack": {
        name: ("Modify", "AWS::IAM::Role") for name in PIPELINE_ROLES
    },
}


def read_report(path: Path) -> str:
    """Initialize the ignored local journal without publishing deployment state."""
    if path.exists():
        return path.read_text()
    return (
        "# StayOS local deployment review\n\n"
        "This ignored file records deployment state and validation results.\n"
    )


def patch_template(baseline: dict, source: dict, api: bool) -> dict:
    """Change only publish auth and the resource of existing publish grants."""
    result = copy.deepcopy(baseline)
    roles = API_ROLES if api else PIPELINE_ROLES
    for role in roles:
        count = 0
        for policy in result["Resources"][role]["Properties"]["Policies"]:
            for statement in policy.get("PolicyDocument", {}).get("Statement", []):
                actions = statement.get("Action", [])
                if "appsync:EventPublish" not in actions:
                    continue
                if actions not in ("appsync:EventPublish", ["appsync:EventPublish"]):
                    raise RuntimeError(f"Mixed publish grant in {role}")
                statement["Resource"] = {
                    "Fn::Sub": (
                        "${PulseRealtimeEventApi.ApiArn}/channelNamespace/pulse"
                        if api else "${RealtimeApiArn}/channelNamespace/pulse"
                    )
                }
                count += 1
        if count != 1:
            raise RuntimeError(f"Expected exactly one publish grant in {role}")
    if api:
        result.setdefault("Mappings", {})["RealtimeProbeCode"] = copy.deepcopy(
            source["Mappings"]["RealtimeProbeCode"]
        )
        config = result["Resources"]["PulseRealtimeEventApi"]["Properties"]["EventConfig"]
        desired = source["Resources"]["PulseRealtimeEventApi"]["Properties"]["EventConfig"]
        config["AuthProviders"] = copy.deepcopy(desired["AuthProviders"])
        config["DefaultPublishAuthModes"] = [{"AuthType": "AWS_IAM"}]
        result["Resources"]["PulseRealtimeNamespace"]["Properties"]["PublishAuthModes"] = [
            {"AuthType": "AWS_IAM"}
        ]
        for logical in ("PulseRealtimePublishProbe", "PulseRealtimeDeniedProbe"):
            if logical in baseline["Resources"]:
                raise RuntimeError("Validation probes already exist; review before rerelease")
            result["Resources"][logical] = copy.deepcopy(source["Resources"][logical])
    return result


def source_archive() -> bytes:
    """Package public frontend source in memory, excluding credentials/caches."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("pulse/frontend", "shared/auth"):
            for path in sorted((ROOT / folder).rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(ROOT)
                if any(part in {
                    "node_modules", ".next", "out", ".git", "__pycache__"
                } for part in relative.parts):
                    continue
                if path.name.startswith(".env") or path.name in {
                    ".npmrc", ".pypirc", ".netrc", "tsconfig.tsbuildinfo",
                }:
                    continue
                # Stable ZIP metadata makes subsequent source-drift checks exact.
                entry = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, path.read_bytes())
    return stream.getvalue()


def pin_dependencies(root: dict, parameters: dict[str, dict[str, str]]) -> tuple[dict, list]:
    """Keep existing child inputs concrete during a release with no replacements.

    CloudFormation otherwise treats every output of a modified nested stack as
    unknown, proposing updates/replacements of consumers whose inputs stay the
    same. The normal source root retains its dynamic output wiring.
    """
    result = copy.deepcopy(root)
    pinned = []
    for logical, resource in result["Resources"].items():
        if resource["Type"] != "AWS::CloudFormation::Stack":
            continue
        for key, node in resource["Properties"].get("Parameters", {}).items():
            if not isinstance(node, dict) or set(node) != {"Fn::GetAtt"}:
                continue
            reference = node["Fn::GetAtt"]
            producer = reference[0] if isinstance(reference, list) else reference.split(".", 1)[0]
            if producer not in parameters:
                continue
            value = parameters.get(logical, {}).get(key)
            if value is None or value == "****":
                raise RuntimeError(f"Cannot safely preserve {logical}/{key}")
            resource["Properties"]["Parameters"][key] = value
            pinned.append({"child": logical, "parameter": key, "source": node, "value": value})
    return result, pinned


def simulation_decisions(results: list[dict]) -> dict[str, str]:
    """Read each resource decision instead of a multi-resource aggregate denial."""
    decisions = {}
    for result in results:
        if result.get("ResourceSpecificResults"):
            for resource in result["ResourceSpecificResults"]:
                decisions[resource["EvalResourceName"]] = resource["EvalResourceDecision"]
        else:
            decisions[result["EvalResourceName"]] = result["EvalDecision"]
    return decisions


class RealtimeRelease:
    """Run resumable release phases using the existing account-owned services."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stack = f"{args.stack_prefix}-{args.region}"
        self.base = ["aws", "--profile", args.profile, "--region", args.region]
        self._s3 = None

    def aws(self, *args: str, cfn: bool = False, raw: bool = False,
            pass_fds: tuple[int, ...] = ()) -> Any:
        command = list(self.base)
        if cfn:
            command[2] = self.args.cloudformation_profile
        process = subprocess.run(
            command + list(args) + ["--no-cli-pager", "--output", "json"],
            text=True, capture_output=True, check=False, pass_fds=pass_fds,
        )
        if process.returncode:
            raise RuntimeError(process.stderr.strip())
        return process.stdout if raw else json.loads(process.stdout or "{}")

    def identity(self) -> None:
        for cfn in (False, True):
            identity = self.aws("sts", "get-caller-identity", cfn=cfn)
            if identity["Account"] != self.args.expected_account_id:
                raise RuntimeError("AWS account mismatch; writes refused")
            print(f"Verified role: {identity['Arn']}", flush=True)
        print(f"Target: {self.stack}; region: {self.args.region}", flush=True)

    def read_state(self) -> dict:
        content = REPORT.read_text()
        block = content.split(START, 1)[1].split(END, 1)[0].strip()
        state = json.loads(block.removeprefix("```json").removesuffix("```").strip())
        if (state["account"], state["region"], state["stack"]) != (
            self.args.expected_account_id, self.args.region, self.stack
        ):
            raise RuntimeError("Release record belongs to another target")
        return state

    def save(self, state: dict) -> None:
        content = read_report(REPORT)
        block = f"{START}\n```json\n{json.dumps(state, indent=2)}\n```\n{END}"
        if START in content:
            content = content.split(START, 1)[0] + block + content.split(END, 1)[1]
        else:
            content += "\n\n### Realtime release state\n\n" + block + "\n"
        REPORT.write_text(content)

    def s3(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.Session(
                profile_name=self.args.profile, region_name=self.args.region
            ).client("s3")
        return self._s3

    def put(self, bucket: str, key: str, body: bytes, **metadata: Any) -> None:
        self.s3().put_object(Bucket=bucket, Key=key, Body=body, **metadata)

    def get(self, bucket: str, key: str) -> bytes:
        response = self.s3().get_object(Bucket=bucket, Key=key)
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def template(self, stack: str) -> dict:
        return load_template(self.aws(
            "cloudformation", "get-template", "--stack-name", stack, cfn=True
        )["TemplateBody"])

    def functions(self) -> list:
        return self.aws(
            "lambda", "list-functions", "--query",
            f"Functions[?starts_with(FunctionName, '{self.args.stack_prefix}-')]."
            "{name:FunctionName,hash:CodeSha256,handler:Handler,role:Role}",
        )

    def prepare(self) -> None:
        self.identity()
        if START in read_report(REPORT):
            raise RuntimeError("Existing release record; resume rather than restart")
        stack = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", self.stack, cfn=True
        )["Stacks"][0]
        if stack["StackStatus"] != "UPDATE_COMPLETE":
            raise RuntimeError("Existing stack must be stable")
        root = self.template(self.stack)
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        release = "realtime/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        state = {
            "account": self.args.expected_account_id, "region": self.args.region,
            "stack": self.stack, "release": release, "bucket": parameters["DeployBucket"],
            "parameters": list(parameters), "tags": stack.get("Tags", []),
            "children": {}, "functions": self.functions(),
        }
        resources = self.aws(
            "cloudformation", "list-stack-resources", "--stack-name", self.stack, cfn=True
        )["StackResourceSummaries"]
        for resource in resources:
            if resource["ResourceType"] == "AWS::CloudFormation::Stack":
                state["children"][resource["LogicalResourceId"]] = resource["PhysicalResourceId"]
        self.save(state)
        for logical, arn in {"root": self.stack, **state["children"]}.items():
            baseline = self.template(arn)
            self.put(
                state["bucket"], f"{release}/baseline-{logical}.json",
                json.dumps(baseline).encode(),
            )
            if logical in ALLOWED:
                source_name = "pulse-api" if logical == "ApiStack" else "pulse-pipeline"
                source = load_template(
                    (ROOT / f"pulse/infrastructure/nested-stacks/{source_name}.yaml").read_text()
                )
                candidate = patch_template(baseline, source, logical == "ApiStack")
                key = f"{release}/candidate-{logical}.json"
                self.put(state["bucket"], key, json.dumps(candidate).encode())
                root["Resources"][logical]["Properties"]["TemplateURL"] = (
                    f"https://s3.{self.args.region}.amazonaws.com/{state['bucket']}/{key}"
                )
        self.put(state["bucket"], f"{release}/candidate-root.json", json.dumps(root).encode())
        self.start_build(state)

    def rebuild(self) -> None:
        self.identity()
        state = self.read_state()
        if state.get("executionStarted"):
            raise RuntimeError("Do not rebuild an executing release")
        previous = self.aws("codebuild", "batch-get-builds", "--ids", state["buildId"])["builds"][0]
        if previous["buildStatus"] == "IN_PROGRESS":
            raise RuntimeError("Existing build is still live; poll it")
        state.setdefault("buildAttempts", []).append({
            "id": previous["id"], "status": previous["buildStatus"],
            "sourceSha256": state["sourceSha256"],
        })
        self.start_build(state)

    def start_build(self, state: dict) -> None:
        stack = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", self.stack, cfn=True
        )["Stacks"][0]
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        release = state["release"]
        archive = source_archive()
        state["sourceSha256"] = hashlib.sha256(archive).hexdigest()
        source_key = f"{release}/frontend-source-{state['sourceSha256']}.zip"
        self.put(state["bucket"], source_key, archive)
        artifact_key = f"{release}/frontend-{state['sourceSha256']}.zip"
        # Run tests and the final export in the existing isolated CodeBuild job.
        # Only this build receives these overrides; its project is unchanged.
        buildspec = {
            "version": "0.2",
            "phases": {
                "install": {
                    "runtime-versions": {"nodejs": 22},
                    "commands": ["cd pulse/frontend", "npm ci --legacy-peer-deps --no-audit --no-fund"],
                },
                "build": {
                    "commands": [
                        "npm run test:run", "npx tsc --noEmit --incremental false",
                        "npm run build", "cd out && zip -qr ../../../frontend.zip . && cd ../../..",
                        f"aws s3 cp frontend.zip s3://{state['bucket']}/{artifact_key} --only-show-errors",
                    ]
                },
            },
        }
        env = {
            "NEXT_PUBLIC_API_URL": next(
                o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == "ApiEndpoint"
            ),
            "NEXT_PUBLIC_COGNITO_CLIENT_ID": parameters["UserPoolClientId"],
            "NEXT_PUBLIC_COGNITO_REGION": self.args.region,
            "NEXT_PUBLIC_AWS_REGION": self.args.region,
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        build = self.aws(
            "codebuild", "start-build", "--project-name", f"{self.args.stack_prefix}-backend-build",
            "--source-location-override", f"{state['bucket']}/{source_key}",
            "--buildspec-override", json.dumps(buildspec),
            "--environment-variables-override",
            json.dumps([{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()]),
        )["build"]
        state.update(buildId=build["id"], artifactKey=artifact_key, buildStatus="IN_PROGRESS")
        state.pop("artifactSha256", None)
        self.save(state)
        print(f"Frontend test/build started: {state['buildId']}", flush=True)

    def build_status(self) -> None:
        self.identity()
        state = self.read_state()
        build = self.aws("codebuild", "batch-get-builds", "--ids", state["buildId"])["builds"][0]
        state["buildStatus"] = build["buildStatus"]
        self.save(state)
        print(f"Build: {build['buildStatus']}; phase: {build.get('currentPhase')}", flush=True)
        logs = build.get("logs", {})
        if logs.get("groupName") and logs.get("streamName"):
            events = self.aws(
                "logs", "get-log-events", "--log-group-name", logs["groupName"],
                "--log-stream-name", logs["streamName"], "--limit", "80",
            )["events"]
            for event in events:
                print(event["message"].rstrip())
        if build["buildStatus"] == "SUCCEEDED":
            body = self.get(state["bucket"], state["artifactKey"])
            state["artifactSha256"] = hashlib.sha256(body).hexdigest()
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                if "index.html" not in archive.namelist():
                    raise RuntimeError("Frontend archive has no index")
                state["frontendFileCount"] = len(archive.namelist())
            self.save(state)

    def preview(self) -> None:
        self.identity()
        state = self.read_state()
        if state.get("buildStatus") != "SUCCEEDED" or "artifactSha256" not in state:
            raise RuntimeError("Passing frontend test/build required")
        if hashlib.sha256(source_archive()).hexdigest() != state["sourceSha256"]:
            raise RuntimeError("Frontend source changed since the tested build")
        for logical, arn in {"root": self.stack, **state["children"]}.items():
            baseline = json.loads(self.get(state["bucket"], f"{state['release']}/baseline-{logical}.json"))
            if self.template(arn) != baseline:
                raise RuntimeError(f"{logical} changed since capture")
        # Regenerate the candidate from the original pinned baseline so source
        # validation additions can be made while the frontend CI build runs.
        root = json.loads(self.get(state["bucket"], f"{state['release']}/baseline-root.json"))
        installed_parameters = {}
        for logical, arn in state["children"].items():
            child = self.aws("cloudformation", "describe-stacks", "--stack-name", arn, cfn=True)["Stacks"][0]
            installed_parameters[logical] = {
                p["ParameterKey"]: p["ParameterValue"] for p in child["Parameters"]
            }
        root, pinned = pin_dependencies(root, installed_parameters)
        state["pinnedInputs"] = pinned
        for logical in ALLOWED:
            baseline = json.loads(self.get(state["bucket"], f"{state['release']}/baseline-{logical}.json"))
            name = "pulse-api" if logical == "ApiStack" else "pulse-pipeline"
            source = load_template((ROOT / f"pulse/infrastructure/nested-stacks/{name}.yaml").read_text())
            candidate = patch_template(baseline, source, logical == "ApiStack")
            key = f"{state['release']}/candidate-{logical}.json"
            self.put(state["bucket"], key, json.dumps(candidate).encode())
            root["Resources"][logical]["Properties"]["TemplateURL"] = (
                f"https://s3.{self.args.region}.amazonaws.com/{state['bucket']}/{key}"
            )
        self.put(state["bucket"], f"{state['release']}/candidate-root.json", json.dumps(root).encode())
        candidate = self.get(state["bucket"], f"{state['release']}/candidate-root.json")
        # Linux memfd is an anonymous in-memory file, with no filesystem entry.
        # This satisfies the project's deploy entry point without a scratch file.
        fd = os.memfd_create("stayos-realtime-template")
        try:
            os.write(fd, candidate)
            output = self.aws(
                "cloudformation", "deploy", "--stack-name", self.stack,
                "--template-file", f"/proc/self/fd/{fd}", "--capabilities", "CAPABILITY_NAMED_IAM",
                "--no-execute-changeset", cfn=True, raw=True, pass_fds=(fd,),
            )
        finally:
            os.close(fd)
        print(output, flush=True)
        preliminary = re.search(r"arn:[^\s]+:changeSet/[^\s]+", output)
        if not preliminary:
            raise RuntimeError("Project deploy command returned no change-set ARN")
        state["deployChangeSet"] = preliminary.group(0)
        self.save(state)
        request = {
            "StackName": self.stack,
            "ChangeSetName": state["release"].replace("/", "-") + f"-review-{len(state.get('previewAttempts', [])) + 1}",
            "ChangeSetType": "UPDATE", "TemplateBody": candidate.decode(),
            "Parameters": [{"ParameterKey": k, "UsePreviousValue": True} for k in state["parameters"]],
            "Capabilities": ["CAPABILITY_NAMED_IAM"], "IncludeNestedStacks": True,
            "Tags": state["tags"],
        }
        change_set = self.aws(
            "cloudformation", "create-change-set", "--cli-input-json", json.dumps(request), cfn=True
        )
        if state.get("changeSet"):
            state.setdefault("previewAttempts", []).append({
                "changeSet": state["changeSet"], "executed": False,
                "reason": "Rejected unknown nested-output propagation; inputs pinned to existing values",
            })
        state["changeSet"] = change_set["Id"]
        self.save(state)
        print(f"Nested preview started: {change_set['Id']}", flush=True)

    def inspect(self) -> None:
        self.identity()
        state = self.read_state()
        preview = self.aws(
            "cloudformation", "describe-change-set", "--change-set-name", state["changeSet"],
            "--include-property-values", cfn=True,
        )
        if preview["Status"] != "CREATE_COMPLETE":
            raise RuntimeError(f"Preview status: {preview['Status']}: {preview.get('StatusReason', '')}")
        seen = set()
        verified = []
        for item in preview.get("Changes", []):
            change = item["ResourceChange"]
            logical = change["LogicalResourceId"]
            if logical not in state["children"] or change["Action"] != "Modify" or change.get("Replacement") != "False":
                raise RuntimeError(f"Unrelated root change/replacement: {logical}")
            child = self.aws(
                "cloudformation", "describe-change-set", "--change-set-name", change["ChangeSetId"],
                "--include-property-values", cfn=True,
            )
            validate_leaf_changes(child.get("Changes", []), ALLOWED.get(logical, {}))
            if child.get("Changes"):
                seen.add(logical)
            for leaf in child.get("Changes", []):
                name = f"{logical}/{leaf['ResourceChange']['LogicalResourceId']}"
                verified.append(name)
                print(f"Verified preview: {name}; no replacement", flush=True)
        if seen != set(ALLOWED):
            raise RuntimeError("Expected realtime changes missing")
        state.update(previewPassed=True, verifiedChanges=verified)
        self.save(state)

    def deploy(self) -> None:
        self.inspect()
        state = self.read_state()
        self.aws(
            "cloudformation", "execute-change-set", "--change-set-name", state["changeSet"], cfn=True
        )
        state["executionStarted"] = True
        self.save(state)
        print("Executing the verified realtime change set.", flush=True)

    def verify(self) -> None:
        self.identity()
        state = self.read_state()
        for logical, arn in {"root": self.stack, **state["children"]}.items():
            stack = self.aws(
                "cloudformation", "describe-stacks", "--stack-name", arn, cfn=True
            )["Stacks"][0]
            if stack["StackStatus"] != "UPDATE_COMPLETE":
                raise RuntimeError(f"{logical}: {stack['StackStatus']}")
            name = "candidate" if logical in {"root", *ALLOWED} else "baseline"
            expected = json.loads(self.get(state["bucket"], f"{state['release']}/{name}-{logical}.json"))
            if self.template(arn) != expected:
                raise RuntimeError(f"{logical} differs from its reviewed template")
        probes = {
            f"{self.args.stack_prefix}-realtime-publish-probe",
            f"{self.args.stack_prefix}-realtime-denied-probe",
        }
        before = {f["name"]: f for f in state["functions"]}
        after = {f["name"]: f for f in self.functions()}
        if set(after) != set(before) | probes or any(after[name] != value for name, value in before.items()):
            raise RuntimeError("An existing PULSE Lambda changed or an unexpected function appeared")
        state["infrastructureVerified"] = True
        self.save(state)
        print("All templates match; all PULSE Lambda hashes/handlers/roles preserved.", flush=True)

    def publish_frontend(self) -> None:
        self.verify()
        state = self.read_state()
        body = self.get(state["bucket"], state["artifactKey"])
        if hashlib.sha256(body).hexdigest() != state["artifactSha256"]:
            raise RuntimeError("Tested frontend artifact hash mismatch")
        bucket = f"stayos-frontend-{self.args.expected_account_id}"
        distribution = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", f"stayos-{self.args.region}"
        )["Stacks"][0]
        distribution_id = next(
            o["OutputValue"] for o in distribution["Outputs"] if o["OutputKey"] == "FrontendDistributionId"
        )
        before = self.aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", "pulse/").get("Contents", [])
        # Preserve existing objects for rollback; no old chunks or other modules
        # are deleted. Backups are durable release artifacts, not scratch files.
        backups = []
        for obj in before:
            key = obj["Key"]
            response = self.s3().get_object(Bucket=bucket, Key=key)
            try:
                data = response["Body"].read()
            finally:
                response["Body"].close()
            backup_key = f"{state['release']}/frontend-before/{key}"
            metadata = {
                k: response[k] for k in ("ContentType", "CacheControl", "ContentEncoding") if k in response
            }
            self.put(state["bucket"], backup_key, data, **metadata)
            backups.append({"key": key, "backup": backup_key, "sha256": hashlib.sha256(data).hexdigest(), **metadata})
        self.put(state["bucket"], f"{state['release']}/frontend-before.json", json.dumps(backups).encode())
        hashes = {}
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = [n for n in archive.namelist() if not n.endswith("/")]
            # Immutable chunks first, mutable documents last: existing sessions
            # retain their old chunks while newly loaded pages see the new build.
            for name in sorted(files, key=lambda n: n.endswith((".html", ".txt", ".json"))):
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError("Unsafe frontend archive member")
                data = archive.read(name)
                key = "pulse/" + name
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self.put(bucket, key, data, ContentType=content_type, CacheControl="no-cache")
                hashes[key] = hashlib.sha256(data).hexdigest()
        self.put(state["bucket"], f"{state['release']}/frontend-hashes.json", json.dumps(hashes).encode())
        invalidation = self.aws(
            "cloudfront", "create-invalidation", "--distribution-id", distribution_id, "--paths", "/pulse/*"
        )["Invalidation"]
        state.update(frontendPublished=True, frontendBucket=bucket,
                     distributionId=distribution_id, invalidationId=invalidation["Id"])
        self.save(state)
        print(f"Published {len(hashes)} files under /pulse/; invalidation {invalidation['Id']}", flush=True)

    def verify_auth(self) -> None:
        self.verify()
        state = self.read_state()
        outcomes = {}
        for kind in ("publish", "denied"):
            # /dev/stdout is a stream, not a local output artifact. The CLI emits
            # the Lambda payload followed by invocation metadata; parse both.
            output = self.aws(
                "lambda", "invoke", "--function-name",
                f"{self.args.stack_prefix}-realtime-{kind}-probe",
                "--payload", "{}", "--cli-binary-format", "raw-in-base64-out",
                "/dev/stdout", raw=True,
            ).strip()
            result, position = json.JSONDecoder().raw_decode(output)
            metadata = json.loads(output[position:])
            if metadata.get("FunctionError") or metadata.get("StatusCode") != 200:
                raise RuntimeError(f"{kind} probe invocation failed")
            outcomes[kind] = result
            print(f"Native {kind} probe: {json.dumps(result)}", flush=True)
        if outcomes["publish"] != {"httpStatus": 200, "successful": 1, "failed": 0}:
            raise RuntimeError("Authorized backend publishing was not successful")
        if outcomes["denied"].get("httpStatus") not in (401, 403):
            raise RuntimeError("Unrelated role was not denied")
        stack = self.aws(
            "cloudformation", "describe-stacks", "--stack-name", self.stack
        )["Stacks"][0]
        api_arn = next(o["OutputValue"] for o in stack["Outputs"] if o["OutputKey"] == "RealtimeApiArn")
        api_id = api_arn.rsplit("/", 1)[-1]
        config = self.aws("appsync", "get-api", "--api-id", api_id)["api"]["eventConfig"]
        namespace = self.aws(
            "appsync", "get-channel-namespace", "--api-id", api_id, "--name", "pulse"
        )["channelNamespace"]
        cognito = [{"authType": "AMAZON_COGNITO_USER_POOLS"}]
        iam = [{"authType": "AWS_IAM"}]
        if (
            config["connectionAuthModes"] != cognito
            or config["defaultSubscribeAuthModes"] != cognito
            or config["defaultPublishAuthModes"] != iam
            or namespace["publishAuthModes"] != iam
            or namespace["subscribeAuthModes"] != cognito
        ):
            raise RuntimeError("Live auth modes differ from the intended configuration")
        candidate = json.loads(self.get(state["bucket"], f"{state['release']}/candidate-ApiStack.json"))
        if namespace["codeHandlers"] != candidate["Resources"]["PulseRealtimeNamespace"]["Properties"]["CodeHandlers"]:
            raise RuntimeError("Live property authorization handler changed")
        namespace_arn = api_arn + "/channelNamespace/pulse"
        decisions = {}
        for logical, roles in (("ApiStack", API_ROLES), ("PipelineStack", PIPELINE_ROLES)):
            resources = self.aws(
                "cloudformation", "list-stack-resources", "--stack-name", state["children"][logical]
            )["StackResourceSummaries"]
            for resource in resources:
                if resource["LogicalResourceId"] not in roles:
                    continue
                role_arn = f"arn:aws:iam::{state['account']}:role/{resource['PhysicalResourceId']}"
                result = self.aws(
                    "iam", "simulate-principal-policy", "--policy-source-arn", role_arn,
                    "--action-names", "appsync:EventPublish", "--resource-arns",
                    namespace_arn, api_arn + "/channelNamespace/unrelated",
                )["EvaluationResults"]
                values = simulation_decisions(result)
                if values.get(namespace_arn) != "allowed" or values.get(
                    api_arn + "/channelNamespace/unrelated"
                ) not in ("implicitDeny", "explicitDeny"):
                    raise RuntimeError(f"Unexpected publisher scope: {resource['LogicalResourceId']}")
                decisions[resource["LogicalResourceId"]] = values
        if set(decisions) != API_ROLES | PIPELINE_ROLES:
            raise RuntimeError("A publisher role was not verified")
        state.update(authorizationVerified=True, nativeProbeResults=outcomes, publisherScope=decisions)
        self.save(state)
        print("All eight publisher roles allow pulse and deny unrelated namespaces.", flush=True)

    def verify_frontend(self) -> None:
        self.identity()
        state = self.read_state()
        hashes = json.loads(self.get(state["bucket"], f"{state['release']}/frontend-hashes.json"))
        for key, digest in hashes.items():
            if hashlib.sha256(self.get(state["frontendBucket"], key)).hexdigest() != digest:
                raise RuntimeError(f"Frontend mismatch: {key}")
        invalidation = self.aws(
            "cloudfront", "get-invalidation", "--distribution-id", state["distributionId"],
            "--id", state["invalidationId"],
        )["Invalidation"]
        if invalidation["Status"] != "Completed":
            raise RuntimeError(f"Invalidation: {invalidation['Status']}")
        state["frontendVerified"] = True
        self.save(state)
        print(f"Verified {len(hashes)} deployed files; CloudFront invalidation completed.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=[
        "prepare", "rebuild", "build-status", "preview", "inspect", "deploy", "verify",
        "publish-frontend", "verify-frontend", "verify-auth",
    ])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cloudformation-profile", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-prefix", default="pulse")
    args = parser.parse_args()
    release = RealtimeRelease(args)
    getattr(release, args.phase.replace("-", "_"))()


if __name__ == "__main__":
    main()
