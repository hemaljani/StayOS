"""Publish five reviewed StayOS fixes using existing resources and safe baselines.

Phases are explicit and resumable. Original brief contents stay in the encrypted
AWS deploy bucket; local manifests contain only identifiers, hashes and status.
"""

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
import certifi

from .voice import ROOT, RUNTIME_FIELDS, VoiceUpgrade, validate_changes

ORCHESTRATOR = ROOT / "lumi/backend/functions/orchestrator"
sys.path.insert(0, str(ORCHESTRATOR))
from historical_repair import snapshot_fingerprint  # noqa: E402

FUNCTION_SUFFIXES = {
    "lumi": ("orchestrator", "seed-data", "tools"),
    "pulse": ("api", "ops-read", "kitchen-seed"),
}
GATEWAY_NAMES = {
    "get_occupancy",
    "get_revenue",
    "get_vip_guests",
    "get_room_status",
    "get_work_orders",
    "get_sister_property_availability",
    "get_walkable_guests",
    "get_room_move_candidates",
}
STABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}


def compute_update(baseline, source):
    """Only toggle mock mode; preserve code keys, layer and all IAM resources."""
    result = copy.deepcopy(baseline)
    desired = source["Resources"]["OrchestratorFunction"]["Properties"]["Environment"][
        "Variables"
    ]["MOCK_MODE"]
    if str(desired).lower() != "false":
        raise RuntimeError("Source must disable MOCK_MODE")
    result["Resources"]["OrchestratorFunction"]["Properties"]["Environment"][
        "Variables"
    ]["MOCK_MODE"] = desired
    return result


def gateway_update(target, description):
    """Preserve shared PULSE tools and every configuration except description."""
    result = copy.deepcopy(target)
    tools = result["targetConfiguration"]["mcp"]["lambda"]["toolSchema"][
        "inlinePayload"
    ]
    if len(tools) != 8 or {item["name"] for item in tools} != GATEWAY_NAMES:
        raise RuntimeError("Expected the existing eight shared Gateway tools")
    next(item for item in tools if item["name"] == "get_occupancy")[
        "description"
    ] = description
    return {
        key: result[key]
        for key in (
            "gatewayIdentifier",
            "targetId",
            "name",
            "description",
            "targetConfiguration",
            "credentialProviderConfigurations",
        )
        if key in result
    }


def validate_parent(changes):
    """CFN may include unchanged nested wrappers; their leaves are checked."""
    found = False
    for item in changes:
        change = item["ResourceChange"]
        if (
            change["Action"] != "Modify"
            or change["ResourceType"] != "AWS::CloudFormation::Stack"
            or change.get("Replacement") != "False"
        ):
            raise RuntimeError("Unrelated resource or replacement in parent preview")
        for detail in change.get("Details", []):
            if detail.get("ChangeSource") == "DirectModification" and (
                change["LogicalResourceId"] != "ComputeStack"
                or detail["Target"].get("Name") != "TemplateURL"
            ):
                raise RuntimeError("Unrelated parent property change")
        found |= change["LogicalResourceId"] == "ComputeStack"
    if not found:
        raise RuntimeError("Missing ComputeStack change")


def pointer_value(value, path):
    """Read a property-value preview's JSON pointer without evaluating code."""
    for part in path.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def resolve_parameter_value(value, parameters):
    """Resolve the small Ref/Sub subset used by the unchanged ChatStack."""
    if isinstance(value, dict) and "Ref" in value:
        return parameters[value["Ref"]]
    if isinstance(value, dict) and "Fn::Sub" in value:
        expression = value["Fn::Sub"]
        substitutions = dict(parameters)
        if isinstance(expression, list):
            expression, mapping = expression
            substitutions.update(
                {
                    key: resolve_parameter_value(item, parameters)
                    for key, item in mapping.items()
                }
            )
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda match: str(substitutions[match.group(1)]),
            expression,
        )
    if isinstance(value, dict):
        raise RuntimeError("Unsupported intrinsic in predicted ChatStack change")
    return value


def validate_chat_predictions(changes, source, parameters):
    """Accept only placeholders that resolve to the exact existing properties.

    Nested previews mark unchanged supplier-stack outputs as KNOWN_AFTER_APPLY.
    Reconstruct each value from the unchanged template and verified suppliers;
    never accept a placeholder by simply copying its before value.
    """
    allowed = {
        "ChatAgentCoreRole": "AWS::IAM::Role",
        "ToolLambdaRole": "AWS::IAM::Role",
        "ToolLambdaFunction": "AWS::Lambda::Function",
    }
    verified = {}
    for item in changes:
        change = item["ResourceChange"]
        logical = change["LogicalResourceId"]
        if (
            logical not in allowed
            or change["ResourceType"] != allowed[logical]
            or change["Action"] != "Modify"
            or change.get("Replacement") != "False"
            or change.get("Scope") != ["Properties"]
        ):
            raise RuntimeError("Unrelated ChatStack resource change")
        before = json.loads(change["BeforeContext"])
        after = json.loads(change["AfterContext"])
        details = change.get("Details", [])
        parameter_paths = {
            detail["Target"]["Path"]
            for detail in details
            if detail["ChangeSource"] == "ParameterReference"
            and detail.get("CausingEntity") in parameters
        }
        paths = {detail["Target"].get("Path") for detail in details}
        if not paths or None in paths or paths != parameter_paths:
            raise RuntimeError("ChatStack prediction has unverified dependencies")
        for path in paths:
            if pointer_value(after, path) != "{{changeSet:KNOWN_AFTER_APPLY}}":
                raise RuntimeError("ChatStack contains an actual property change")
            resolved = resolve_parameter_value(
                pointer_value(source["Resources"][logical], path), parameters
            )
            if resolved != pointer_value(before, path):
                raise RuntimeError("Supplier binding changes a ChatStack property")
            parts = path.strip("/").split("/")
            parent = after
            for part in parts[:-1]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            if isinstance(parent, list):
                parent[int(parts[-1])] = resolved
            else:
                parent[parts[-1]] = resolved
        if after != before:
            raise RuntimeError("ChatStack differs beyond the verified placeholders")
        verified[logical] = before["Properties"]
    return verified


def validate_frontend_export(directory, expected):
    """Refuse stale public deployment settings before publishing an export."""
    for filename in (".env.local", ".env.production.local"):
        if (directory / filename).exists():
            raise RuntimeError("A local environment override needs explicit review")
    configured = {}
    for line in (directory / ".env.production").read_text().splitlines():
        if line.startswith("NEXT_PUBLIC_") and "=" in line:
            key, value = line.split("=", 1)
            configured[key] = value
    if configured != expected:
        raise RuntimeError("Frontend environment differs from live deployment")
    scripts = "\n".join(
        location.read_text() for location in (directory / "out").rglob("*.js")
    )
    required = set(expected) - {"NEXT_PUBLIC_COGNITO_USER_POOL_ID"}
    if any(expected[key] not in scripts for key in required):
        raise RuntimeError("Compiled frontend contains incomplete deployment settings")


class PendingDeployment(VoiceUpgrade):
    """Reuse profile-safe CLI/CFN/runtime helpers from the voice upgrade."""

    def __init__(self, args):
        super().__init__(args)
        if args.baseline:
            self.directory = Path(args.baseline).resolve()
        self.sdk = boto3.Session(profile_name=args.profile, region_name=args.region)
        config = Config(
            retries={"mode": "standard", "total_max_attempts": 3},
            connect_timeout=10,
            read_timeout=910,
        )
        self.s3 = self.sdk.client("s3", config=config)
        self.db = self.sdk.resource("dynamodb", config=config)
        self.lambdas = self.sdk.client("lambda", config=config)

    def saved(self, name):
        return json.loads((self.directory / name).read_text())

    def make(self, target, **values):
        command = [
            "make",
            target,
            f"PROFILE={self.args.profile}",
            f"CLOUDFORMATION_PROFILE={self.args.cloudformation_profile}",
            f"REGION={self.args.region}",
            f"EXPECTED_ACCOUNT_ID={self.account}",
            f"LUMI_STACK_PREFIX={self.args.stack_prefix}",
            f"PULSE_STACK_PREFIX={self.args.pulse_stack_prefix}",
            *(f"{key}={value}" for key, value in values.items()),
        ]
        with (self.directory / f"{target}.log").open("a") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log)
        if result.returncode:
            raise RuntimeError(
                f"{target} failed; see {self.directory / (target + '.log')}"
            )

    def capture(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.directory = ROOT / "logs" / f"pending-fixes-{stamp}"
        self.directory.mkdir(mode=0o700, parents=True)
        self.run_id = f"fixes-{stamp}"
        for bucket in (
            self.bucket,
            f"{self.args.pulse_stack_prefix}-deploy-{self.account}",
        ):
            self.s3.head_bucket(Bucket=bucket)
        self.make("plan")
        parent = self.template(self.stack)
        children = self.aws(
            "cloudformation",
            "list-stack-resources",
            "--stack-name",
            self.stack,
            cfn=True,
        )["StackResourceSummaries"]
        child = next(
            item["PhysicalResourceId"]
            for item in children
            if item["LogicalResourceId"] == "ComputeStack"
        )
        baseline = {
            "account": self.account,
            "region": self.args.region,
            "stack": self.stack,
            "child": child,
            "runId": self.run_id,
            "backupPrefix": f"pending-fixes/{self.run_id}",
            "parentState": self.stack_state(self.stack),
            "childState": self.stack_state(child),
            "functions": {},
            "runtimes": {},
        }
        for state in (baseline["parentState"], baseline["childState"]):
            if state["status"] not in STABLE:
                raise RuntimeError("Stack is not stable")
        self.write("parent-before.json", parent)
        self.write("compute-before.json", self.template(child))
        for feature, suffixes in FUNCTION_SUFFIXES.items():
            prefix = (
                self.args.stack_prefix
                if feature == "lumi"
                else self.args.pulse_stack_prefix
            )
            for suffix in suffixes:
                name = f"{prefix}-{suffix}"
                function = self.lambdas.get_function(FunctionName=name)
                configuration = function["Configuration"]
                if (
                    configuration["State"] != "Active"
                    or configuration["LastUpdateStatus"] != "Successful"
                ):
                    raise RuntimeError(f"Function is not stable: {name}")
                location = self.directory / f"{name}-before.zip"
                # The short-lived download URL is used in memory, never logged.
                with urllib.request.urlopen(
                    function["Code"]["Location"],
                    context=ssl.create_default_context(cafile=certifi.where()),
                    timeout=90,
                ) as response:
                    location.write_bytes(response.read())
                actual = base64.b64encode(
                    hashlib.sha256(location.read_bytes()).digest()
                ).decode()
                if actual != configuration["CodeSha256"]:
                    raise RuntimeError(f"Backup hash mismatch: {name}")
                location.chmod(0o600)
                baseline["functions"][name] = {
                    "codeHash": actual,
                    "revision": configuration["RevisionId"],
                    "modified": configuration["LastModified"],
                }
        for agent in ("voice", "chat"):
            runtime_id = self.aws(
                "ssm",
                "get-parameter",
                "--name",
                f"/{self.args.stack_prefix}/{agent}/runtime-id",
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
            if (
                runtime["status"] != endpoint["status"]
                or runtime["status"] != "READY"
                or endpoint["liveVersion"] != runtime["agentRuntimeVersion"]
            ):
                raise RuntimeError(f"{agent} runtime endpoint is not stable")
            self.repo = f"{self.args.stack_prefix}-{agent}-agent"
            uri = runtime["agentRuntimeArtifact"]["containerConfiguration"][
                "containerUri"
            ]
            pinned, pushed = self.image_digest(uri)
            if "@" not in uri and datetime.fromisoformat(
                pushed
            ) > datetime.fromisoformat(runtime["lastUpdatedAt"]):
                raise RuntimeError("Mutable image tag changed since runtime deployment")
            request = {
                key: copy.deepcopy(runtime[key])
                for key in RUNTIME_FIELDS
                if key in runtime
            }
            if any(
                re.search(r"password|secret|token|credential|access_key", key, re.I)
                for key in request.get("environmentVariables", {})
            ):
                raise RuntimeError("Refusing to persist secret runtime configuration")
            request["agentRuntimeArtifact"]["containerConfiguration"][
                "containerUri"
            ] = pinned
            self.write(f"{agent}-before.json", request)
            baseline["runtimes"][agent] = {
                "id": runtime_id,
                "version": runtime["agentRuntimeVersion"],
                "image": pinned,
            }
        gateway_id = self.aws(
            "ssm",
            "get-parameter",
            "--name",
            f"/{self.args.stack_prefix}/gateway/gateway-id",
            "--query",
            "Parameter.Value",
            "--output",
            "text",
        )
        targets = self.aws(
            "bedrock-agentcore-control",
            "list-gateway-targets",
            "--gateway-identifier",
            gateway_id,
        )["items"]
        target_id = next(
            item["targetId"] for item in targets if item["name"] == "tools"
        )
        target = self.aws(
            "bedrock-agentcore-control",
            "get-gateway-target",
            "--gateway-identifier",
            gateway_id,
            "--target-id",
            target_id,
        )
        target["gatewayIdentifier"] = gateway_id
        gateway_update(target, "baseline validation")
        self.write("gateway-before.json", target)
        outputs = {
            item["OutputKey"]: item["OutputValue"]
            for item in baseline["parentState"]["outputs"]
        }
        baseline["frontendBucket"] = outputs["FrontendBucketName"]
        baseline["distribution"] = outputs["FrontendDistributionId"]
        baseline["audioBucket"] = f"{self.args.stack_prefix}-audio-{self.account}"
        for feature in ("lumi", "pulse"):
            self.aws(
                "s3",
                "sync",
                f"s3://{baseline['frontendBucket']}/{feature}/",
                str(self.directory / f"{feature}-frontend-before"),
                "--quiet",
            )
        self.write("baseline.json", baseline)
        self.backup_records(baseline)
        print(f"Verified rollback baseline: {self.directory}", flush=True)

    def backup_records(self, baseline):
        manifest = json.loads(Path(self.args.manifest).read_text())
        if manifest["count"] > 153 or len(manifest["records"]) != manifest["count"]:
            raise RuntimeError("Repair scope exceeds approved manifest")
        briefs = self.db.Table(f"{self.args.stack_prefix}-briefs")
        revenues = self.db.Table(f"{self.args.stack_prefix}-revenues")
        serializer = TypeSerializer()
        records = []
        for candidate in manifest["records"]:
            key = {field: candidate[field] for field in ("propertyId", "briefDate")}
            original = briefs.get_item(Key=key, ConsistentRead=True).get("Item")
            if not original or int(original.get("ttl", 0)) <= time.time():
                records.append({**key, "status": "SKIPPED_EXPIRED_OR_REMOVED"})
                continue
            revenue = revenues.get_item(
                Key={"propertyId": key["propertyId"], "date": key["briefDate"]},
                ConsistentRead=True,
            ).get("Item")
            if not revenue:
                raise RuntimeError(f"Missing historical source: {key}")
            backup_key = f"{baseline['backupPrefix']}/briefs/{key['propertyId']}/{key['briefDate']}.json"
            body = json.dumps(
                {
                    "brief": {
                        field: serializer.serialize(val)
                        for field, val in original.items()
                    },
                    "revenue": {
                        field: serializer.serialize(val)
                        for field, val in revenue.items()
                    },
                }
            ).encode()
            self.s3.put_object(
                Bucket=self.bucket,
                Key=backup_key,
                Body=body,
                ServerSideEncryption="AES256",
                ContentType="application/json",
            )
            if self.s3.head_object(Bucket=self.bucket, Key=backup_key)[
                "ContentLength"
            ] != len(body):
                raise RuntimeError("Brief backup verification failed")
            audio_key = original.get("audioBrief", {}).get("s3Key")
            if audio_key:
                audio_backup = f"{baseline['backupPrefix']}/audio/{audio_key}"
                self.s3.copy_object(
                    Bucket=self.bucket,
                    Key=audio_backup,
                    CopySource={"Bucket": baseline["audioBucket"], "Key": audio_key},
                    ServerSideEncryption="AES256",
                )
                previous = self.s3.head_object(
                    Bucket=baseline["audioBucket"], Key=audio_key
                )
                copied = self.s3.head_object(Bucket=self.bucket, Key=audio_backup)
                if copied["ContentLength"] != previous["ContentLength"]:
                    raise RuntimeError("Audio backup verification failed")
            records.append(
                {
                    **key,
                    "status": "BACKED_UP",
                    "backupKey": backup_key,
                    "expectedFingerprint": snapshot_fingerprint(original),
                    "revenueFingerprint": snapshot_fingerprint(revenue),
                    "audioKey": audio_key,
                }
            )
        self.write("repair-manifest.json", records)
        print(
            f"Historical backups verified: {sum(r['status'] == 'BACKED_UP' for r in records)}",
            flush=True,
        )

    def build(self):
        for target in ("lumi-voice-build", "lumi-chat-build", "pulse-package-backend"):
            print(f"Building with existing automation: {target}", flush=True)
            self.make(target)
        # Existing LUMI layer is reused; package only the three selected functions.
        for suffix in FUNCTION_SUFFIXES["lumi"]:
            self.package_lumi(suffix)
        self.s3.download_file(
            f"{self.args.pulse_stack_prefix}-deploy-{self.account}",
            "functions/pulse-backend.zip",
            str(self.directory / "pulse-backend-after.zip"),
        )
        images = {}
        for agent in ("voice", "chat"):
            self.repo = f"{self.args.stack_prefix}-{agent}-agent"
            images[agent] = self.image_digest(
                f"{self.account}.dkr.ecr.{self.args.region}.amazonaws.com/{self.repo}:latest"
            )[0]
        self.write("images-after.json", images)
        self.write("build-complete.json", {"images": images})

    def package_lumi(self, suffix):
        """Package current selected source using the existing dependency layer."""
        source = ROOT / "lumi/backend/functions" / suffix
        output = self.directory / f"{self.args.stack_prefix}-{suffix}-after.zip"
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"
                ):
                    archive.write(path, str(path.relative_to(source)))
            if suffix == "seed-data":
                archive.write(ORCHESTRATOR / "revenue_kpis.py", "revenue_kpis.py")
        return output

    def refresh_orchestrator(self):
        """Publish a repair correction without rebuilding other artifacts."""
        self.saved("backend-complete.json")
        baseline = self.saved("baseline.json")
        after = self.saved("functions-after.json")
        name = f"{self.args.stack_prefix}-orchestrator"
        current = self.lambdas.get_function_configuration(FunctionName=name)
        if current["CodeSha256"] != after[name]["codeHash"]:
            raise RuntimeError("Orchestrator changed outside this deployment")
        location = self.package_lumi("orchestrator")
        expected = base64.b64encode(
            hashlib.sha256(location.read_bytes()).digest()
        ).decode()
        key = f"{baseline['backupPrefix']}/code/{name}.zip"
        self.s3.upload_file(str(location), self.bucket, key)
        updated = self.lambdas.update_function_code(
            FunctionName=name,
            S3Bucket=self.bucket,
            S3Key=key,
            RevisionId=current["RevisionId"],
        )
        self.lambdas.get_waiter("function_updated_v2").wait(FunctionName=name)
        if updated["CodeSha256"] != expected:
            raise RuntimeError("Orchestrator code hash differs")
        after[name] = {"codeHash": expected}
        self.write("functions-after.json", after)
        completed = self.saved("backend-complete.json")
        completed["functions"] = after
        self.write("backend-complete.json", completed)
        print("Published and verified repair correction: orchestrator", flush=True)

    def preview(self, stack, template_file, logical_id, resource_type):
        state = self.stack_state(stack)
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
            "pending-fixes",
            *(
                ["--tags", *(f"{t['Key']}={t['Value']}" for t in state["tags"])]
                if state.get("tags")
                else []
            ),
            cfn=True,
        )
        match = re.search(r"arn:[^\s]+:changeSet/[^\s]+", output)
        if not match:
            raise RuntimeError("Preview did not produce a change-set ARN")
        arn = match.group(0)
        detail = self.aws(
            "cloudformation",
            "describe-change-set",
            "--change-set-name",
            arn,
            "--include-property-values",
            cfn=True,
        )
        if {item["ParameterKey"] for item in detail["Parameters"]} != set(
            state["parameterKeys"]
        ):
            raise RuntimeError("Preview changed parameter keys")
        if logical_id == "ComputeStack":
            validate_parent(detail["Changes"])
        else:
            change = validate_changes(detail["Changes"], logical_id, resource_type)
            if set(change.get("Scope", [])) != {"Properties"}:
                raise RuntimeError("Unexpected nested change scope")
            for item in change.get("Details", []):
                if (
                    item.get("ChangeSource") == "DirectModification"
                    and item["Target"].get("Name") != "Environment"
                ):
                    raise RuntimeError("Unexpected function property change")
        self.write(
            f"preview-{logical_id}.json", {"id": arn, "changes": detail["Changes"]}
        )
        return arn

    def config_update(self, rollback=False):
        baseline = self.saved("baseline.json")
        original = self.saved("compute-before.json")
        if rollback:
            desired = original
        else:
            from .voice import load_template

            desired = compute_update(
                original,
                load_template(
                    (
                        ROOT / "lumi/infrastructure/nested-stacks/compute.yaml"
                    ).read_text()
                ),
            )
        if self.template(baseline["child"]) == desired:
            return
        if not rollback:
            for stack, saved in (
                (self.stack, "parentState"),
                (baseline["child"], "childState"),
            ):
                if self.stack_state(stack) != baseline[saved]:
                    raise RuntimeError("Stack changed since baseline; refusing update")
        path = self.write("compute-desired.json", desired)
        preview = self.preview(
            baseline["child"], path, "OrchestratorFunction", "AWS::Lambda::Function"
        )
        self.aws(
            "cloudformation",
            "delete-change-set",
            "--change-set-name",
            preview,
            cfn=True,
        )
        parent = self.saved("parent-before.json")
        if not rollback:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = f"{baseline['backupPrefix']}/compute-{digest}.json"
            self.s3.upload_file(str(path), self.bucket, key)
            parent["Resources"]["ComputeStack"]["Properties"][
                "TemplateURL"
            ] = f"https://s3.{self.args.region}.amazonaws.com/{self.bucket}/{key}"
        parent_path = self.write("parent-desired.json", parent)
        arn = self.preview(
            self.stack, parent_path, "ComputeStack", "AWS::CloudFormation::Stack"
        )
        # Resolve all nested wrappers before executing the CLI deploy preview.
        request = {
            "StackName": self.stack,
            "ChangeSetName": f"pending-hierarchy-{int(time.time())}",
            "ChangeSetType": "UPDATE",
            "TemplateBody": parent_path.read_text(),
            "Parameters": [
                {"ParameterKey": key, "UsePreviousValue": True}
                for key in baseline["parentState"]["parameterKeys"]
            ],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "IncludeNestedStacks": True,
            "Tags": baseline["parentState"].get("tags", []),
        }
        request_path = self.write("hierarchy-request.json", request)
        hierarchy = self.aws(
            "cloudformation",
            "create-change-set",
            "--cli-input-json",
            f"file://{request_path}",
            cfn=True,
        )["Id"]
        self.aws(
            "cloudformation",
            "wait",
            "change-set-create-complete",
            "--change-set-name",
            hierarchy,
            cfn=True,
        )
        root = self.aws(
            "cloudformation",
            "describe-change-set",
            "--change-set-name",
            hierarchy,
            "--include-property-values",
            cfn=True,
        )
        validate_parent(root["Changes"])
        # Auth/Data suppliers have no leaf changes; Compute changes only its
        # orchestrator environment. Their exported identifiers stay unchanged.
        resources = self.aws(
            "cloudformation",
            "list-stack-resources",
            "--stack-name",
            self.stack,
            cfn=True,
        )["StackResourceSummaries"]
        stacks = {
            item["LogicalResourceId"]: item["PhysicalResourceId"]
            for item in resources
            if item["ResourceType"] == "AWS::CloudFormation::Stack"
        }
        nested_previews = {}
        for wrapper in root["Changes"]:
            change = wrapper["ResourceChange"]
            if not change.get("ChangeSetId"):
                raise RuntimeError("Nested hierarchy was not resolved")
            nested = self.aws(
                "cloudformation",
                "describe-change-set",
                "--change-set-name",
                change["ChangeSetId"],
                "--include-property-values",
                cfn=True,
            )
            changes = nested.get("Changes", [])
            nested_previews[change["LogicalResourceId"]] = changes
            self.write(
                f"hierarchy-{change['LogicalResourceId']}.json", {"changes": changes}
            )
        validate_changes(
            nested_previews["ComputeStack"],
            "OrchestratorFunction",
            "AWS::Lambda::Function",
        )
        for supplier in ("AuthStack", "DataStack"):
            if nested_previews.get(supplier):
                raise RuntimeError("Supplier stack has unrelated resource changes")
        for name, changes in nested_previews.items():
            if name not in ("ChatStack", "ComputeStack") and changes:
                raise RuntimeError("Unrelated nested resource change")
        chat_verified = {}
        if nested_previews.get("ChatStack"):
            before_parent = self.saved("parent-before.json")
            if (
                parent["Resources"]["ChatStack"]
                != before_parent["Resources"]["ChatStack"]
            ):
                raise RuntimeError(
                    "ChatStack template or parameter expressions changed"
                )
            parameters = {
                "StackPrefix": self.args.stack_prefix,
                "AccountId": self.account,
                "AWS::Region": self.args.region,
                "AWS::AccountId": self.account,
            }
            supplier_outputs = {
                name: {
                    item["OutputKey"]: item["OutputValue"]
                    for item in self.stack_state(stacks[name])["outputs"]
                }
                for name in ("AuthStack", "DataStack", "ComputeStack")
            }
            bindings = parent["Resources"]["ChatStack"]["Properties"]["Parameters"]
            for key, expression in bindings.items():
                if isinstance(expression, dict) and "Fn::GetAtt" in expression:
                    supplier, output = expression["Fn::GetAtt"]
                    if supplier not in supplier_outputs or not output.startswith(
                        "Outputs."
                    ):
                        raise RuntimeError("Unexpected ChatStack output dependency")
                    parameters[key] = supplier_outputs[supplier][
                        output.split(".", 1)[1]
                    ]
            chat_verified = validate_chat_predictions(
                nested_previews["ChatStack"],
                self.template(stacks["ChatStack"]),
                parameters,
            )
            self.write("chat-predictions-verified.json", chat_verified)
        self.aws(
            "cloudformation",
            "delete-change-set",
            "--change-set-name",
            hierarchy,
            cfn=True,
        )
        self.aws(
            "cloudformation",
            "execute-change-set",
            "--change-set-name",
            arn,
            "--no-disable-rollback",
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
        if self.template(baseline["child"]) != desired:
            raise RuntimeError(
                "Deployed nested template differs from reviewed template"
            )
        # Verify the real IAM and Lambda configuration, not just the preview.
        for logical, properties in chat_verified.items():
            if logical.endswith("Role"):
                for policy in properties["Policies"]:
                    actual = self.aws(
                        "iam",
                        "get-role-policy",
                        "--role-name",
                        properties["RoleName"],
                        "--policy-name",
                        policy["PolicyName"],
                    )["PolicyDocument"]
                    if actual != policy["PolicyDocument"]:
                        raise RuntimeError(
                            "An unchanged IAM policy differs after update"
                        )
            else:
                actual = self.lambdas.get_function_configuration(
                    FunctionName=properties["FunctionName"]
                )
                if [layer["Arn"] for layer in actual.get("Layers", [])] != properties[
                    "Layers"
                ] or actual["Environment"]["Variables"] != properties["Environment"][
                    "Variables"
                ]:
                    raise RuntimeError("Tool configuration changed after stack update")
        print(
            "CloudFormation verified: only MOCK_MODE changed; parameters preserved",
            flush=True,
        )

    def backend(self):
        self.saved("build-complete.json")
        baseline = self.saved("baseline.json")
        # Adapter first; it accepts old and canonical occupancy output.
        ordered = [
            f"{self.args.pulse_stack_prefix}-{name}"
            for name in FUNCTION_SUFFIXES["pulse"]
        ] + [f"{self.args.stack_prefix}-{name}" for name in FUNCTION_SUFFIXES["lumi"]]
        after = (
            self.saved("functions-after.json")
            if (self.directory / "functions-after.json").exists()
            else {}
        )
        for name in ordered:
            current = self.lambdas.get_function_configuration(FunctionName=name)
            if name in after and current["CodeSha256"] == after[name]["codeHash"]:
                continue
            if current["CodeSha256"] != baseline["functions"][name]["codeHash"]:
                raise RuntimeError(f"Function changed since baseline: {name}")
            location = (
                self.directory / "pulse-backend-after.zip"
                if name.startswith(self.args.pulse_stack_prefix + "-")
                else self.directory / f"{name}-after.zip"
            )
            key = f"{baseline['backupPrefix']}/code/{name}.zip"
            self.s3.upload_file(str(location), self.bucket, key)
            updated = self.lambdas.update_function_code(
                FunctionName=name,
                S3Bucket=self.bucket,
                S3Key=key,
                RevisionId=current["RevisionId"],
            )
            self.lambdas.get_waiter("function_updated_v2").wait(FunctionName=name)
            expected = base64.b64encode(
                hashlib.sha256(location.read_bytes()).digest()
            ).decode()
            if updated["CodeSha256"] != expected:
                raise RuntimeError(f"Published code hash mismatch: {name}")
            after[name] = {"codeHash": expected}
            self.write("functions-after.json", after)
            print(f"Published and verified: {name}", flush=True)
        self.config_update()
        target = self.saved("gateway-before.json")
        schema = json.loads((ROOT / "lumi/backend/tools/tool-schema.json").read_text())
        if isinstance(schema, dict):
            schema = schema.get("tools", schema.get("inlinePayload", []))
        description = next(
            tool["description"] for tool in schema if tool["name"] == "get_occupancy"
        )
        request = gateway_update(target, description)
        path = self.write("gateway-after.json", request)
        self.aws(
            "bedrock-agentcore-control",
            "update-gateway-target",
            "--cli-input-json",
            f"file://{path}",
        )
        for _ in range(60):
            live = self.aws(
                "bedrock-agentcore-control",
                "get-gateway-target",
                "--gateway-identifier",
                request["gatewayIdentifier"],
                "--target-id",
                request["targetId"],
            )
            if live["status"] == "READY":
                live["gatewayIdentifier"] = request["gatewayIdentifier"]
                if gateway_update(live, description) != request:
                    raise RuntimeError("Published Gateway configuration differs")
                break
            if live["status"] == "FAILED":
                raise RuntimeError("Gateway target failed")
            time.sleep(5)
        else:
            raise RuntimeError("Gateway target did not become READY")
        versions = {}
        for agent, image in self.saved("images-after.json").items():
            request = self.saved(f"{agent}-before.json")
            current = self.runtime(request["agentRuntimeId"])
            current_image = current["agentRuntimeArtifact"]["containerConfiguration"][
                "containerUri"
            ]
            if current_image == image and current["status"] == "READY":
                versions[agent] = current["agentRuntimeVersion"]
                continue
            if current["agentRuntimeVersion"] != baseline["runtimes"][agent]["version"]:
                raise RuntimeError("Runtime changed since baseline")
            request["agentRuntimeArtifact"]["containerConfiguration"][
                "containerUri"
            ] = image
            versions[agent] = self.update_runtime(request)
            self.write("versions-after.json", versions)
        self.write("backend-complete.json", {"versions": versions, "functions": after})

    def repair(self):
        self.saved("backend-complete.json")
        baseline = self.saved("baseline.json")
        results = (
            self.saved("repair-results.json")
            if (self.directory / "repair-results.json").exists()
            else []
        )
        completed = {
            (item["propertyId"], item["briefDate"])
            for item in results
            if item["status"] == "REPAIRED"
        }
        for item in self.saved("repair-manifest.json"):
            key = (item["propertyId"], item["briefDate"])
            if key in completed:
                continue
            if item["status"] != "BACKED_UP":
                if key not in {(r["propertyId"], r["briefDate"]) for r in results}:
                    results.append(item)
                    self.write("repair-results.json", results)
                continue
            self.s3.head_object(Bucket=self.bucket, Key=item["backupKey"])
            event = {
                "action": "repair-existing",
                "repairRunId": baseline["runId"],
                "backupVerified": True,
                **{
                    field: item[field]
                    for field in (
                        "propertyId",
                        "briefDate",
                        "expectedFingerprint",
                        "revenueFingerprint",
                    )
                },
            }
            response = self.lambdas.invoke(
                FunctionName=f"{self.args.stack_prefix}-orchestrator",
                InvocationType="RequestResponse",
                Payload=json.dumps(event).encode(),
            )
            with response["Payload"] as body:
                result = json.loads(body.read())
            if response.get("FunctionError") or result.get("statusCode") != 200:
                self.write("repair-failure.json", {"key": list(key), "result": result})
                raise RuntimeError(
                    f"Historical repair failed for {key}; see repair-failure.json"
                )
            result.update({"propertyId": key[0], "briefDate": key[1]})
            if result["status"] == "REPAIRED":
                metadata = self.s3.head_object(
                    Bucket=baseline["audioBucket"], Key=result["audioKey"]
                )
                if metadata["ContentLength"] < 100 or not metadata[
                    "ContentType"
                ].startswith("audio/"):
                    raise RuntimeError("Repaired audio is not readable audio")
            results.append(result)
            self.write("repair-results.json", results)
            print(
                f"Repair {len(results)}/{len(self.saved('repair-manifest.json'))}: "
                f"{key[0]} {key[1]} {result['status']}",
                flush=True,
            )

    def frontends(self):
        self.saved("backend-complete.json")
        baseline = self.saved("baseline.json")
        outputs = {
            item["OutputKey"]: item["OutputValue"]
            for item in self.stack_state(self.stack)["outputs"]
        }
        pulse_outputs = {
            item["OutputKey"]: item["OutputValue"]
            for item in self.stack_state(
                f"{self.args.pulse_stack_prefix}-{self.args.region}"
            )["outputs"]
        }
        common = {
            "NEXT_PUBLIC_COGNITO_CLIENT_ID": outputs["UserPoolClientId"],
            "NEXT_PUBLIC_COGNITO_REGION": self.args.region,
            "NEXT_PUBLIC_AWS_REGION": self.args.region,
        }
        expected = {
            "lumi": {
                **common,
                "NEXT_PUBLIC_API_URL": outputs["ApiGatewayUrl"],
                "NEXT_PUBLIC_COGNITO_USER_POOL_ID": outputs["UserPoolId"],
                "NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID": outputs["VoiceIdentityPoolId"],
                "NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN": (
                    f"arn:aws:bedrock-agentcore:{self.args.region}:{self.account}:runtime/"
                    f"{baseline['runtimes']['voice']['id']}"
                ),
                "NEXT_PUBLIC_CHAT_RUNTIME_ARN": (
                    f"arn:aws:bedrock-agentcore:{self.args.region}:{self.account}:runtime/"
                    f"{baseline['runtimes']['chat']['id']}"
                ),
            },
            "pulse": {**common, "NEXT_PUBLIC_API_URL": pulse_outputs["ApiEndpoint"]},
        }
        self.make("lumi-write-frontend-env")
        self.make(
            "pulse-write-frontend-env", COGNITO_CLIENT_ID=outputs["UserPoolClientId"]
        )
        for feature in ("lumi", "pulse"):
            self.make(f"{feature}-build-frontend")
            validate_frontend_export(ROOT / feature / "frontend", expected[feature])
            self.write(
                f"{feature}-configuration-verified.json", {"status": "MATCHES_LIVE"}
            )
            # Keep old immutable assets during the publication transition.
            self.aws(
                "s3",
                "sync",
                str(ROOT / feature / "frontend/out"),
                f"s3://{baseline['frontendBucket']}/{feature}/",
                "--quiet",
            )
            invalidation = self.aws(
                "cloudfront",
                "create-invalidation",
                "--distribution-id",
                baseline["distribution"],
                "--paths",
                f"/{feature}/*",
            )["Invalidation"]["Id"]
            self.write(f"{feature}-invalidation.json", {"id": invalidation})
            self.aws(
                "cloudfront",
                "wait",
                "invalidation-completed",
                "--distribution-id",
                baseline["distribution"],
                "--id",
                invalidation,
            )
            print(
                f"Published frontend and completed invalidation: {feature}", flush=True
            )

    def verify(self):
        """Prove published hashes and every repaired KPI/audio input match."""
        baseline = self.saved("baseline.json")
        for name, expected in self.saved("functions-after.json").items():
            actual = self.lambdas.get_function_configuration(FunctionName=name)
            if (
                actual["CodeSha256"] != expected["codeHash"]
                or actual["State"] != "Active"
                or actual["LastUpdateStatus"] != "Successful"
            ):
                raise RuntimeError(f"Published function verification failed: {name}")
        mock_mode = self.lambdas.get_function_configuration(
            FunctionName=f"{self.args.stack_prefix}-orchestrator"
        )["Environment"]["Variables"]["MOCK_MODE"]
        if mock_mode != "false":
            raise RuntimeError("Deployed orchestrator still uses mock mode")
        versions = self.saved("versions-after.json")
        for agent, version in versions.items():
            runtime = self.runtime(baseline["runtimes"][agent]["id"])
            endpoint = self.aws(
                "bedrock-agentcore-control",
                "get-agent-runtime-endpoint",
                "--agent-runtime-id",
                runtime["agentRuntimeId"],
                "--endpoint-name",
                "DEFAULT",
            )
            if (
                runtime["status"] != "READY"
                or endpoint["status"] != "READY"
                or endpoint["liveVersion"] != version
                or runtime["agentRuntimeArtifact"]["containerConfiguration"][
                    "containerUri"
                ]
                != self.saved("images-after.json")[agent]
            ):
                raise RuntimeError("Runtime verification failed")
        from data_validator import validate_narrative

        deserializer = TypeDeserializer()
        briefs = self.db.Table(f"{self.args.stack_prefix}-briefs")
        revenues = self.db.Table(f"{self.args.stack_prefix}-revenues")
        checks = []
        for item in self.saved("repair-manifest.json"):
            key = {field: item[field] for field in ("propertyId", "briefDate")}
            current = briefs.get_item(Key=key, ConsistentRead=True).get("Item")
            if not current or int(current["ttl"]) <= time.time():
                checks.append({**key, "status": "SKIPPED_EXPIRED_OR_REMOVED"})
                continue
            source = revenues.get_item(
                Key={"propertyId": key["propertyId"], "date": key["briefDate"]},
                ConsistentRead=True,
            )["Item"]
            rates = [
                current["dailyKPIs"][metric]["current"]
                for metric in ("occupancy", "adr", "revPAR")
            ]
            expected = [source[metric] for metric in ("occupancyPct", "adr", "revpar")]
            if rates != expected:
                raise RuntimeError(f"Brief/source KPI mismatch: {key}")
            validation = json.loads(json.dumps(current, default=float))
            day = datetime.fromisoformat(key["briefDate"])
            validation["briefDateComponents"] = [day.year, day.month, day.day]
            if (
                not current["narrative"].strip()
                or not validate_narrative(
                    current["narrative"],
                    validation,
                    language=current.get("language", "en-US"),
                )[0]
            ):
                raise RuntimeError(f"Narrative verification failed: {key}")
            audio_key = current["audioBrief"]["s3Key"]
            head = self.s3.head_object(Bucket=baseline["audioBucket"], Key=audio_key)
            if (
                head.get("Metadata", {}).get("narrative-sha256")
                != hashlib.sha256(current["narrative"].encode()).hexdigest()
            ):
                raise RuntimeError(f"Audio/narrative input hash mismatch: {key}")
            with self.s3.get_object(
                Bucket=baseline["audioBucket"], Key=audio_key, Range="bytes=0-31"
            )["Body"] as body:
                header = body.read()
            if not header or not (header.startswith(b"ID3") or header[0] == 0xFF):
                raise RuntimeError("Repaired object is not an MP3")
            with self.s3.get_object(Bucket=self.bucket, Key=item["backupKey"])[
                "Body"
            ] as body:
                backup = json.loads(body.read())
            original = {
                field: deserializer.deserialize(value)
                for field, value in backup["brief"].items()
            }
            for field in ("ttl", "property", "actionItems", "vipArrivals", "briefDate"):
                if current.get(field) != original.get(field):
                    raise RuntimeError(f"Historical snapshot changed: {field} {key}")
            checks.append(
                {
                    **key,
                    "status": "VERIFIED",
                    "rates": [str(value) for value in rates],
                    "audioBytes": head["ContentLength"],
                    "historicalSnapshotPreserved": True,
                    "audioInputHashMatches": True,
                }
            )
        self.write(
            "live-data-verification.json",
            {
                "versions": versions,
                "mockMode": False,
                "records": checks,
                "verified": sum(item["status"] == "VERIFIED" for item in checks),
                "skipped": sum(item["status"] != "VERIFIED" for item in checks),
            },
        )
        print(
            f"Verified {sum(item['status'] == 'VERIFIED' for item in checks)} "
            "briefs against revenue, historical snapshots and MP3 input hashes",
            flush=True,
        )

    def rollback(self):
        baseline = self.saved("baseline.json")
        # Restore records only when they still equal this run's repaired output.
        deserializer = TypeDeserializer()
        table = self.db.Table(f"{self.args.stack_prefix}-briefs")
        repaired = (
            {
                (item["propertyId"], item["briefDate"]): item
                for item in self.saved("repair-results.json")
                if item["status"] == "REPAIRED"
            }
            if (self.directory / "repair-results.json").exists()
            else {}
        )
        for item in self.saved("repair-manifest.json"):
            key = {field: item[field] for field in ("propertyId", "briefDate")}
            result = repaired.get((key["propertyId"], key["briefDate"]))
            if not result:
                continue
            current = table.get_item(Key=key, ConsistentRead=True).get("Item")
            if not current or int(current["ttl"]) <= time.time():
                continue
            if snapshot_fingerprint(current) != result["fingerprint"]:
                raise RuntimeError(
                    f"Refusing rollback of concurrently changed record: {key}"
                )
            with self.s3.get_object(Bucket=self.bucket, Key=item["backupKey"])[
                "Body"
            ] as body:
                backup = json.loads(body.read())
            original = {
                field: deserializer.deserialize(val)
                for field, val in backup["brief"].items()
            }
            table.put_item(
                Item=original,
                ConditionExpression=Attr("repairMetadata.runId").eq(baseline["runId"])
                & Attr("generatedAt").eq(current["generatedAt"])
                & Attr("ttl").gt(int(time.time())),
            )
        self.config_update(rollback=True)
        for name in baseline["functions"]:
            current = self.lambdas.get_function_configuration(FunctionName=name)
            key = f"{baseline['backupPrefix']}/rollback/{name}.zip"
            self.s3.upload_file(
                str(self.directory / f"{name}-before.zip"), self.bucket, key
            )
            self.lambdas.update_function_code(
                FunctionName=name,
                S3Bucket=self.bucket,
                S3Key=key,
                RevisionId=current["RevisionId"],
            )
            self.lambdas.get_waiter("function_updated_v2").wait(FunctionName=name)
        target = self.saved("gateway-before.json")
        description = next(
            item["description"]
            for item in target["targetConfiguration"]["mcp"]["lambda"]["toolSchema"][
                "inlinePayload"
            ]
            if item["name"] == "get_occupancy"
        )
        path = self.write("gateway-rollback.json", gateway_update(target, description))
        self.aws(
            "bedrock-agentcore-control",
            "update-gateway-target",
            "--cli-input-json",
            f"file://{path}",
        )
        for agent in ("voice", "chat"):
            self.update_runtime(self.saved(f"{agent}-before.json"))
        for feature in ("lumi", "pulse"):
            self.aws(
                "s3",
                "sync",
                str(self.directory / f"{feature}-frontend-before"),
                f"s3://{baseline['frontendBucket']}/{feature}/",
                "--quiet",
            )
            result = self.aws(
                "cloudfront",
                "create-invalidation",
                "--distribution-id",
                baseline["distribution"],
                "--paths",
                f"/{feature}/*",
            )
            self.aws(
                "cloudfront",
                "wait",
                "invalidation-completed",
                "--distribution-id",
                baseline["distribution"],
                "--id",
                result["Invalidation"]["Id"],
            )
        self.write("rollback-complete.json", {"status": "RESTORED"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "capture",
            "build",
            "backend",
            "repair",
            "frontends",
            "verify",
            "refresh-orchestrator",
            "rollback",
        ),
    )
    for name in (
        "profile",
        "cloudformation-profile",
        "region",
        "expected-account-id",
        "stack-prefix",
        "pulse-stack-prefix",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--manifest")
    args = parser.parse_args()
    if args.phase != "capture" and not args.baseline:
        parser.error("--baseline is required after capture")
    if args.phase == "capture" and not args.manifest:
        parser.error("--manifest is required for bounded capture")
    deploy = PendingDeployment(args)
    deploy.context()
    if args.phase != "capture":
        saved = deploy.saved("baseline.json")
        if (saved["account"], saved["region"], saved["stack"]) != (
            deploy.account,
            args.region,
            deploy.stack,
        ):
            raise RuntimeError("Baseline target differs from requested deployment")
    getattr(deploy, args.phase.replace("-", "_"))()
    return 0


if __name__ == "__main__":
    sys.exit(main())
