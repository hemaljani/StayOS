"""Release authorization changes to existing StayOS without local scratch files.

The root review holds resumable state. Durable release inputs and rollback
artifacts use existing deployment buckets. Private signing material travels only
from an in-memory generator to Secrets Manager, never into release artifacts.
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
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
import zipfile
from datetime import UTC, datetime

from .realtime_release import RealtimeRelease, pin_dependencies, read_report
from .voice import ROOT, RUNTIME_FIELDS, load_template

REPORT = ROOT / "AWS_WELL_ARCHITECTED_REVIEW.md"
START = "<!-- security-release-state:start -->"
END = "<!-- security-release-state:end -->"
RUNTIMES = {
    "voice": "stayos_voice_agent-RUrWPW9Mm0",
    "chat": "stayos_chat_agent-f08a2QF3fL",
}
CORS_METHODS = frozenset({
    "BriefsOptionsMethod", "BriefsByDateOptionsMethod",
    "BriefsHistoryOptionsMethod", "SettingsOptionsMethod",
})
SECURITY_RESOURCES = {
    "AudioSigningSecret": "AWS::SecretsManager::Secret",
    "AudioSigningPublicKey": "AWS::CloudFront::PublicKey",
    "AudioSigningKeyGroup": "AWS::CloudFront::KeyGroup",
}
TEST_SOURCE_FOLDERS = (
    "lumi/frontend", "shared/auth", "lumi/backend/functions/api", "lumi/backend/tests",
    "lumi/backend/services/voice-agent", "lumi/backend/services/chat-agent",
)


def archive_paths(folders: tuple[str, ...], *, strip: str = "") -> bytes:
    """Create a deterministic ZIP in memory, excluding local credentials/caches."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in folders:
            for path in sorted((ROOT / folder).rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(ROOT)
                if any(part in {
                    "node_modules", ".next", "out", "__pycache__", ".pytest_cache",
                    ".venv", ".aws", ".git", ".hypothesis", "agentcore",
                } for part in relative.parts) or (
                    path.name.startswith(".env")
                    or path.name in {".npmrc", ".pypirc", ".netrc"}
                    or path.suffix == ".pyc"
                ):
                    continue
                name = path.relative_to(ROOT / strip).as_posix() if strip else relative.as_posix()
                member = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, path.read_bytes())
    return stream.getvalue()


def patch_data(baseline: dict, source: dict, phase: str) -> dict:
    """Bootstrap the secret first; later enable viewer signing with its public key."""
    result = copy.deepcopy(baseline)
    result["Resources"]["AudioSigningSecret"] = copy.deepcopy(source["Resources"]["AudioSigningSecret"])
    result.setdefault("Outputs", {})["AudioSigningSecretArn"] = copy.deepcopy(
        source["Outputs"]["AudioSigningSecretArn"]
    )
    if phase in ("signer", "audio"):
        result.setdefault("Parameters", {})["AudioSigningPublicKeyPem"] = copy.deepcopy(
            source["Parameters"]["AudioSigningPublicKeyPem"]
        )
        result.setdefault("Conditions", {})["AudioSigningConfigured"] = copy.deepcopy(
            source["Conditions"]["AudioSigningConfigured"]
        )
        for logical in ("AudioSigningPublicKey", "AudioSigningKeyGroup"):
            result["Resources"][logical] = copy.deepcopy(source["Resources"][logical])
        result["Outputs"]["AudioSigningKeyId"] = copy.deepcopy(source["Outputs"]["AudioSigningKeyId"])
        if phase == "audio":
            result["Resources"]["AudioDistribution"]["Properties"]["DistributionConfig"][
                "DefaultCacheBehavior"
            ]["TrustedKeyGroups"] = copy.deepcopy(
                source["Resources"]["AudioDistribution"]["Properties"]["DistributionConfig"][
                    "DefaultCacheBehavior"
                ]["TrustedKeyGroups"]
            )
    return result


def patch_compute(baseline: dict, source: dict, artifact: str) -> dict:
    """Update only the existing API package and its signing configuration."""
    result = copy.deepcopy(baseline)
    for key in ("AudioSigningKeyId", "AudioSigningSecretArn", "ApiCodeS3Key"):
        result.setdefault("Parameters", {})[key] = copy.deepcopy(source["Parameters"][key])
    result.setdefault("Conditions", {})["AudioSigningConfigured"] = copy.deepcopy(
        source["Conditions"]["AudioSigningConfigured"]
    )
    api = result["Resources"]["ApiFunction"]["Properties"]
    api["Code"]["S3Key"] = {"Ref": "ApiCodeS3Key"}
    desired = source["Resources"]["ApiFunction"]["Properties"]["Environment"]["Variables"]
    for key in (
        "AUDIO_SIGNING_KEY_ID", "AUDIO_SIGNING_PRIVATE_KEY", "COGNITO_CLIENT_ID",
        "POWERTOOLS_TRACER_CAPTURE_RESPONSE",
    ):
        api["Environment"]["Variables"][key] = copy.deepcopy(desired[key])
    gateway = result["Resources"]["ApiGateway"]["Properties"]
    desired_gateway = source["Resources"]["ApiGateway"]["Properties"]
    for key in ("SecurityPolicy", "EndpointAccessMode"):
        gateway[key] = desired_gateway[key]
    return result


def patch_frontend_domain(root: dict, source: dict, domain: str) -> dict:
    """Configure the browser hostname without changing the CloudFront distribution."""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) or "." not in domain:
        raise RuntimeError("Frontend domain must be a hostname without a scheme, path, or credentials")
    result = copy.deepcopy(root)
    result.setdefault("Parameters", {})["FrontendDomain"] = copy.deepcopy(
        source["Parameters"]["FrontendDomain"]
    )
    # The release requires an explicit hostname, so no fallback or new dependency
    # on DataStack is needed in the installed release template.
    result["Resources"]["ComputeStack"]["Properties"]["Parameters"]["FrontendDistributionDomain"] = {
        "Ref": "FrontendDomain",
    }
    return result


def separate_api_dependencies(
    changes: list, baseline: dict, candidate: dict, *, direct_methods: frozenset[str] = frozenset(),
) -> tuple[list, list]:
    """Review unchanged route references separately from directly changed leaves.

    Updating a non-replaced API/Lambda causes dynamic reference entries for
    RootResourceId and integration ARN. Route definitions must remain exactly
    equal and every detail must be a dependency on these stable identifiers.
    Explicit replacements, direct route edits and unknown dependencies fail.
    """
    direct, references = [], []
    paths = {
        name for name, resource in baseline["Resources"].items()
        if resource["Type"] == "AWS::ApiGateway::Resource"
    }
    causes = paths | {"ApiGateway.RootResourceId", "ApiFunction.Arn"}
    for item in changes:
        change = item["ResourceChange"]
        name = change["LogicalResourceId"]
        if (
            change["ResourceType"] not in ("AWS::ApiGateway::Resource", "AWS::ApiGateway::Method")
            or name in direct_methods
        ):
            direct.append(item)
            continue
        if (
            change["Action"] != "Modify"
            or change.get("Replacement") not in ("False", "Conditional")
            or baseline["Resources"].get(name) != candidate["Resources"].get(name)
            or not change.get("Details")
            or any(
                detail.get("Evaluation") != "Dynamic"
                or detail.get("ChangeSource") not in ("ResourceReference", "ResourceAttribute")
                or detail.get("CausingEntity") not in causes
                for detail in change["Details"]
            )
        ):
            raise RuntimeError(f"Unsafe API route change: {name}")
        references.append(name)
    return direct, references


def separate_voice_role_dependency(changes: list, baseline: dict, candidate: dict) -> tuple[list, list]:
    """Review the unchanged Cognito role attachment's propagated role ARN only."""
    direct, dependencies = [], []
    logical = "VoiceIdentityPoolRoleAttachment"
    for item in changes:
        change = item["ResourceChange"]
        if change["LogicalResourceId"] != logical:
            direct.append(item)
            continue
        details = change.get("Details", [])
        if (
            change["ResourceType"] != "AWS::Cognito::IdentityPoolRoleAttachment"
            or change["Action"] != "Modify" or change.get("Replacement") != "False"
            or baseline["Resources"].get(logical) != candidate["Resources"].get(logical)
            or not details
            or any(
                detail.get("Evaluation") != "Dynamic"
                or detail.get("ChangeSource") != "ResourceAttribute"
                or detail.get("CausingEntity") != "VoiceIdentityPoolAuthRole.Arn"
                or detail.get("Target", {}).get("Name") != "Roles"
                or detail.get("Target", {}).get("RequiresRecreation") != "Never"
                for detail in details
            )
        ):
            raise RuntimeError("Unexpected Cognito role attachment mutation")
        dependencies.append(logical)
    return direct, dependencies


def simulation_decisions(evaluation: list) -> dict[tuple[str, str], str]:
    """Use per-resource IAM decisions when a multi-resource action is aggregated."""
    decisions = {}
    for entry in evaluation:
        action = entry["EvalActionName"]
        specifics = entry.get("ResourceSpecificResults")
        if specifics:
            rows = [(row["EvalResourceName"], row["EvalResourceDecision"]) for row in specifics]
        else:
            rows = [(entry["EvalResourceName"], entry["EvalDecision"])]
        for resource, decision in rows:
            key = (action, resource)
            if key in decisions and decisions[key] != decision:
                raise RuntimeError("Contradictory IAM simulation results")
            decisions[key] = decision
    return decisions


def validate_security_changes(changes: list, phase: str, child: str) -> list[str]:
    """Reject removals, replacements, and resources outside the authorized scope."""
    plans = {
        "bootstrap": {"DataStack": {
            "AudioSigningSecret": ("Add", SECURITY_RESOURCES["AudioSigningSecret"]),
        }},
        "signer": {
            "DataStack": {
                "AudioSigningPublicKey": ("Add", SECURITY_RESOURCES["AudioSigningPublicKey"]),
                "AudioSigningKeyGroup": ("Add", SECURITY_RESOURCES["AudioSigningKeyGroup"]),
            },
            "ComputeStack": {
                "ApiFunction": ("Modify", "AWS::Lambda::Function"),
                "ApiGateway": ("Modify", "AWS::ApiGateway::RestApi"),
            },
        },
        "audio": {"DataStack": {
            "AudioDistribution": ("Modify", "AWS::CloudFront::Distribution"),
        }},
        "controls": {
            "VoiceStack": {"VoiceIdentityPoolAuthRole": ("Modify", "AWS::IAM::Role")},
            "ChatStack": {"ChatAgentCoreRole": ("Modify", "AWS::IAM::Role")},
        },
        "pulse": {"ApiStack": {"PulseApiStage": ("Modify", "AWS::ApiGatewayV2::Stage")}},
        "cors": {"ComputeStack": {
            "ApiFunction": ("Modify", "AWS::Lambda::Function"),
            "GatewayResponse4XX": ("Modify", "AWS::ApiGateway::GatewayResponse"),
            **{name: ("Modify", "AWS::ApiGateway::Method") for name in CORS_METHODS},
        }},
    }
    allowed = plans.get(phase, {}).get(child)
    if allowed is None:
        raise RuntimeError(f"Unapproved security phase/child: {phase}/{child}")
    names = []
    for item in changes:
        change = item["ResourceChange"]
        name = change["LogicalResourceId"]
        if (
            allowed.get(name) != (change["Action"], change["ResourceType"])
            or (change["Action"] == "Modify" and change.get("Replacement") != "False")
        ):
            raise RuntimeError(f"Unapproved security change: {child}/{name}")
        names.append(name)
    if set(names) != set(allowed):
        raise RuntimeError(f"Required security changes missing in {child}")
    return names


class SecurityRelease(RealtimeRelease):
    """Account-guarded, resumable build and nested CloudFormation release."""

    def read_state(self) -> dict:
        content = REPORT.read_text()
        block = content.split(START, 1)[1].split(END, 1)[0].strip()
        state = json.loads(block.removeprefix("```json").removesuffix("```").strip())
        if (state["account"], state["region"], state["stack"]) != (
            self.args.expected_account_id, self.args.region, self.stack
        ):
            raise RuntimeError("Security release belongs to another target")
        return state

    def save(self, state: dict) -> None:
        content = read_report(REPORT)
        block = f"{START}\n```json\n{json.dumps(state, indent=2)}\n```\n{END}"
        if START in content:
            content = content.split(START, 1)[0] + block + content.split(END, 1)[1]
        else:
            content += "\n\n### Finding 9 security release state\n\n" + block + "\n"
        REPORT.write_text(content)

    def prepare(self) -> None:
        self.identity()
        if START in read_report(REPORT):
            raise RuntimeError("Existing security release; resume it")
        stack = self.aws("cloudformation", "describe-stacks", "--stack-name", self.stack)["Stacks"][0]
        if stack["StackStatus"] != "UPDATE_COMPLETE":
            raise RuntimeError("StayOS stack must be stable")
        resources = self.aws("cloudformation", "list-stack-resources", "--stack-name", self.stack)[
            "StackResourceSummaries"
        ]
        state = {
            "account": self.args.expected_account_id, "region": self.args.region, "stack": self.stack,
            "bucket": f"stayos-deploy-{self.args.expected_account_id}",
            "testBucket": f"pulse-deploy-{self.args.expected_account_id}",
            "release": "security/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            "children": {
                r["LogicalResourceId"]: r["PhysicalResourceId"] for r in resources
                if r["ResourceType"] == "AWS::CloudFormation::Stack"
            },
            "tags": stack.get("Tags", []),
            "outputs": {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]},
            "functions": self.functions(),
            "parameters": [p["ParameterKey"] for p in stack["Parameters"]],
            "runtimeBefore": {},
        }
        for name, runtime_id in RUNTIMES.items():
            runtime = self.aws("bedrock-agentcore-control", "get-agent-runtime",
                               "--agent-runtime-id", runtime_id)
            # Only public identifiers/configuration are recorded locally.
            state["runtimeBefore"][name] = {
                k: runtime[k] for k in ("agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion",
                                       "agentRuntimeArtifact", "roleArn", "status")
            }
        self.save(state)
        for logical, arn in {"root": self.stack, **state["children"]}.items():
            template = self.template(arn)
            if any(key in template["Resources"] for key in SECURITY_RESOURCES):
                raise RuntimeError("Signing resources already exist; review before release")
            self.put(state["bucket"], f"{state['release']}/baseline-{logical}.json",
                     json.dumps(template).encode())
        print("Captured existing StayOS templates, function hashes and runtime image digests.")

    def test_build(self) -> None:
        self.identity()
        state = self.read_state()
        if state.get("buildId"):
            previous = self.aws("codebuild", "batch-get-builds", "--ids", state["buildId"])["builds"][0]
            if previous["buildStatus"] == "IN_PROGRESS":
                raise RuntimeError("Security test build is still running")
            state.setdefault("buildAttempts", []).append({
                "id": previous["id"], "status": previous["buildStatus"],
                "sourceSha256": state["sourceSha256"],
            })
        body = archive_paths(TEST_SOURCE_FOLDERS)
        digest = hashlib.sha256(body).hexdigest()
        source_key = f"{state['release']}/test-source-{digest}.zip"
        self.put(state["testBucket"], source_key, body)
        artifact = f"{state['release']}/lumi-frontend-{digest}.zip"
        # Isolated cloud CI honors the local no-scratch-file requirement.
        # Real JWT checks run alongside existing voice and API regression tests.
        dummy = (
            "AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing "
            "AWS_SESSION_TOKEN=testing AWS_EC2_METADATA_DISABLED=true "
            "AWS_DEFAULT_REGION=us-east-1 AWS_XRAY_SDK_ENABLED=false"
        )
        buildspec = {
            "version": "0.2",
            "phases": {
                "install": {
                    "runtime-versions": {"python": "3.12", "nodejs": 22},
                    "commands": [
                        "python3 -m venv /opt/stayos-security-ci",
                        ("/opt/stayos-security-ci/bin/pip install --quiet --disable-pip-version-check "
                        "'pytest>=8,<9' 'pytest-asyncio>=0.24,<1' 'moto[dynamodb]>=5,<6' "
                        "'hypothesis>=6,<7' 'pyjwt[crypto]==2.15.0' 'boto3==1.35.0' "
                        "'aws-lambda-powertools==2.40.0' 'aws-xray-sdk==2.14.0' "
                        "'requests==2.33.0'"),
                        ("/opt/stayos-security-ci/bin/pip install --quiet --disable-pip-version-check "
                          "-r lumi/backend/services/voice-agent/requirements.txt"),
                        # Mirror the separate containers: Nova Sonic and the MCP
                        # proxy require different compatible AWS CRT dependency sets.
                        "python3 -m venv /opt/stayos-chat-ci",
                        ("/opt/stayos-chat-ci/bin/pip install --quiet --disable-pip-version-check "
                         "'pytest>=8,<9' 'pytest-asyncio>=0.24,<1' "
                         "-r lumi/backend/services/chat-agent/requirements.txt"),
                        "cd lumi/frontend && npm ci --legacy-peer-deps --no-audit --no-fund && cd ../..",
                    ],
                },
                "build": {"commands": [
                    (f"{dummy} PYTHONDONTWRITEBYTECODE=1 "
                    "/opt/stayos-security-ci/bin/python -m pytest -q -p no:cacheprovider "
                    "lumi/backend/services/voice-agent/tests"),
                    (f"{dummy} PYTHONDONTWRITEBYTECODE=1 "
                     "/opt/stayos-chat-ci/bin/python -m pytest -q -p no:cacheprovider "
                     "lumi/backend/services/chat-agent/tests"),
                    (f"{dummy} AUDIO_CLOUDFRONT_DOMAIN=d1234567890.cloudfront.net "
                    "BRIEFS_TABLE_NAME=stayos-briefs-test SETTINGS_TABLE_NAME=stayos-settings-test "
                    "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=lumi/backend/functions/api "
                    "/opt/stayos-security-ci/bin/python -m pytest -q -p no:cacheprovider "
                    "lumi/backend/tests/unit/test_audio_signing.py "
                    "lumi/backend/tests/unit/test_briefs_handler.py "
                    "lumi/backend/tests/unit/test_api_identity.py "
                    "lumi/backend/tests/unit/test_settings_handler.py "
                    "lumi/backend/tests/unit/test_settings_validator.py"),
                    "cd lumi/frontend && npm run test:run",
                    "npx tsc --noEmit --incremental false", "npm run build",
                    "cd out && zip -qr ../../../lumi-frontend.zip . && cd ../../..",
                    f"aws s3 cp lumi-frontend.zip s3://{state['testBucket']}/{artifact} --only-show-errors",
                ]},
            },
        }
        env = {
            "NEXT_PUBLIC_API_URL": state["outputs"]["ApiGatewayUrl"],
            "NEXT_PUBLIC_COGNITO_USER_POOL_ID": state["outputs"]["UserPoolId"],
            "NEXT_PUBLIC_COGNITO_CLIENT_ID": state["outputs"]["UserPoolClientId"],
            "NEXT_PUBLIC_COGNITO_REGION": self.args.region,
            "NEXT_PUBLIC_AWS_REGION": self.args.region,
            "NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID": state["outputs"]["VoiceIdentityPoolId"],
            "NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN": state["runtimeBefore"]["voice"]["agentRuntimeArn"],
            "NEXT_PUBLIC_CHAT_RUNTIME_ARN": state["runtimeBefore"]["chat"]["agentRuntimeArn"],
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        build = self.aws(
            "codebuild", "start-build", "--project-name", "pulse-backend-build",
            "--source-location-override", f"{state['testBucket']}/{source_key}",
            "--buildspec-override", json.dumps(buildspec),
            "--environment-variables-override",
            json.dumps([{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()]),
        )["build"]
        state.update(buildId=build["id"], buildStatus="IN_PROGRESS",
                     sourceSha256=digest, artifactKey=artifact)
        state.pop("artifactSha256", None)
        self.save(state)
        print(f"Security tests and LUMI export started: {build['id']}")

    def build_status(self) -> None:
        self.identity()
        state = self.read_state()
        build = self.aws("codebuild", "batch-get-builds", "--ids", state["buildId"])["builds"][0]
        state["buildStatus"] = build["buildStatus"]
        print(f"Security build: {build['buildStatus']}; phase: {build.get('currentPhase')}")
        if build.get("logs", {}).get("streamName"):
            events = self.aws("logs", "get-log-events", "--log-group-name", build["logs"]["groupName"],
                              "--log-stream-name", build["logs"]["streamName"], "--limit", "100")["events"]
            print("\n".join(e["message"].rstrip() for e in events))
        if build["buildStatus"] == "SUCCEEDED":
            body = self.get(state["testBucket"], state["artifactKey"])
            state["artifactSha256"] = hashlib.sha256(body).hexdigest()
        self.save(state)

    def build_agents(self) -> None:
        """Use the existing ARM build projects and their existing buildspecs."""
        self.identity()
        state = self.read_state()
        if state.get("agentBuilds"):
            unchanged = True
            for name, previous in state["agentBuilds"].items():
                build = self.aws("codebuild", "batch-get-builds", "--ids", previous["id"])["builds"][0]
                if build["buildStatus"] == "IN_PROGRESS":
                    raise RuntimeError("Agent builds are still running; inspect their status")
                folder = f"lumi/backend/services/{name}-agent"
                current_hash = hashlib.sha256(archive_paths((folder,), strip=folder)).hexdigest()
                unchanged = unchanged and (
                    previous.get("status") == "SUCCEEDED" and previous["sourceSha256"] == current_hash
                )
            if unchanged:
                print("Both successful agent images already match current source.")
                return
            state.setdefault("agentBuildAttempts", []).append(copy.deepcopy(state["agentBuilds"]))
            state["agentBuilds"] = {}
            self.save(state)
        builds = {}
        for name in ("voice", "chat"):
            folder = f"lumi/backend/services/{name}-agent"
            body = archive_paths((folder,), strip=folder)
            digest = hashlib.sha256(body).hexdigest()
            key = f"{name}-agent-source/{state['release'].replace('/', '-')}-{digest}.zip"
            self.put(state["bucket"], key, body)
            build = self.aws(
                "codebuild", "start-build", "--project-name", f"stayos-{name}-agent-build",
                "--source-location-override", f"{state['bucket']}/{key}",
            )["build"]
            builds[name] = {"id": build["id"], "sourceSha256": digest,
                            "sourceKey": key, "status": "IN_PROGRESS"}
            state["agentBuilds"] = builds
            self.save(state)
            print(f"{name} ARM image build started: {build['id']}")

    def agent_build_status(self) -> None:
        self.identity()
        state = self.read_state()
        for name, record in state["agentBuilds"].items():
            build = self.aws("codebuild", "batch-get-builds", "--ids", record["id"])["builds"][0]
            record["status"] = build["buildStatus"]
            print(f"{name} image: {build['buildStatus']}; phase: {build.get('currentPhase')}")
            logs = build.get("logs", {})
            if not logs.get("streamName"):
                continue
            events = self.aws("logs", "get-log-events", "--log-group-name", logs["groupName"],
                              "--log-stream-name", logs["streamName"], "--limit", "100")["events"]
            text = "\n".join(e["message"].rstrip() for e in events)
            print(text)
            if build["buildStatus"] == "SUCCEEDED":
                # Identify this exact build's immutable tag, rather than a
                # mutable latest tag that another build could overwrite.
                tags = re.findall(rf"stayos-{name}-agent:(\d{{14}})", text)
                if not tags:
                    raise RuntimeError(f"No immutable tag found for {name}")
                tag = tags[-1]
                image = self.aws("ecr", "describe-images", "--repository-name", f"stayos-{name}-agent",
                                 "--image-ids", f"imageTag={tag}")["imageDetails"][0]
                record["imageUri"] = (
                    f"{state['account']}.dkr.ecr.{state['region']}.amazonaws.com/"
                    f"stayos-{name}-agent@{image['imageDigest']}"
                )
                record["imageTag"] = tag
        self.save(state)

    def update_runtimes(self) -> None:
        """Bind both existing runtime handshakes to this Cognito pool and client."""
        self.identity()
        state = self.read_state()
        if state.get("buildStatus") != "SUCCEEDED" or not state.get("verifiedPhases", {}).get("controls"):
            raise RuntimeError("Passing tests and verified CloudFormation controls required")
        if hashlib.sha256(archive_paths(TEST_SOURCE_FOLDERS)).hexdigest() != state["sourceSha256"]:
            raise RuntimeError("Application source changed since the regression build")
        for name, before in state["runtimeBefore"].items():
            record = state["agentBuilds"][name]
            folder = f"lumi/backend/services/{name}-agent"
            if record.get("status") != "SUCCEEDED" or hashlib.sha256(
                archive_paths((folder,), strip=folder)
            ).hexdigest() != record["sourceSha256"]:
                raise RuntimeError(f"{name} image is not a successful build of current source")
            runtime = self.aws("bedrock-agentcore-control", "get-agent-runtime",
                               "--agent-runtime-id", before["agentRuntimeId"])
            previous_update = state.get("runtimeUpdates", {}).get(name)
            if previous_update:
                config = runtime.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
                previous_image = previous_update.get("imageUri", record["imageUri"])
                if (
                    runtime["agentRuntimeVersion"] != previous_update["version"]
                    or runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] != previous_image
                    or runtime["roleArn"] != before["roleArn"]
                    or config.get("allowedClients") != [state["outputs"]["UserPoolClientId"]]
                    or config.get("discoveryUrl") != (
                        f"https://cognito-idp.{state['region']}.amazonaws.com/"
                        f"{state['outputs']['UserPoolId']}/.well-known/openid-configuration"
                    )
                    or runtime.get("requestHeaderConfiguration", {}).get("requestHeaderAllowlist") != ["Authorization"]
                    or runtime.get("environmentVariables", {}).get("COGNITO_CLIENT_ID") != state["outputs"]["UserPoolClientId"]
                    or runtime["status"] not in ("READY", "UPDATING")
                ):
                    raise RuntimeError(f"{name} recorded runtime update differs from the live configuration")
                if previous_image == record["imageUri"]:
                    # A retry after one successful update must not issue another version.
                    print(f"Existing {name} update already submitted: {runtime['status']}")
                    continue
            if runtime["status"] != "READY":
                raise RuntimeError(f"{name} runtime is not ready")
            if not previous_update and runtime["agentRuntimeArtifact"] != before["agentRuntimeArtifact"]:
                raise RuntimeError(f"{name} runtime changed outside this release")
            request = {k: runtime[k] for k in RUNTIME_FIELDS if k in runtime}
            request["agentRuntimeArtifact"] = {"containerConfiguration": {
                "containerUri": record["imageUri"],
            }}
            request.setdefault("environmentVariables", {})["COGNITO_CLIENT_ID"] = state["outputs"]["UserPoolClientId"]
            request["environmentVariables"]["COGNITO_USER_POOL_ID"] = state["outputs"]["UserPoolId"]
            request["authorizerConfiguration"] = {"customJWTAuthorizer": {
                "discoveryUrl": (
                    f"https://cognito-idp.{state['region']}.amazonaws.com/"
                    f"{state['outputs']['UserPoolId']}/.well-known/openid-configuration"
                ),
                "allowedClients": [state["outputs"]["UserPoolClientId"]],
            }}
            request["requestHeaderConfiguration"] = {"requestHeaderAllowlist": ["Authorization"]}
            updated = self.aws("bedrock-agentcore-control", "update-agent-runtime",
                               "--cli-input-json", json.dumps(request))
            if previous_update:
                state.setdefault("runtimeUpdateHistory", {}).setdefault(name, []).append(
                    copy.deepcopy(previous_update)
                )
            state.setdefault("runtimeUpdates", {})[name] = {
                "version": updated["agentRuntimeVersion"], "status": updated["status"],
                "imageUri": record["imageUri"], "updatedAt": datetime.now(UTC).isoformat(),
            }
            state["runtimesVerified"] = False
            self.save(state)
            print(f"Updating existing {name} runtime to OAuth: version {updated['agentRuntimeVersion']}")

    def verify_runtimes(self) -> None:
        self.identity()
        state = self.read_state()
        for name, before in state["runtimeBefore"].items():
            runtime = self.aws("bedrock-agentcore-control", "get-agent-runtime",
                               "--agent-runtime-id", before["agentRuntimeId"])
            expected = state["runtimeUpdates"][name]
            config = runtime.get("authorizerConfiguration", {}).get("customJWTAuthorizer", {})
            if (
                runtime["status"] != "READY"
                or runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] != expected["imageUri"]
                or runtime["roleArn"] != before["roleArn"]
                or config.get("allowedClients") != [state["outputs"]["UserPoolClientId"]]
                or config.get("discoveryUrl") != (
                    f"https://cognito-idp.{state['region']}.amazonaws.com/"
                    f"{state['outputs']['UserPoolId']}/.well-known/openid-configuration"
                )
                or runtime.get("requestHeaderConfiguration", {}).get("requestHeaderAllowlist") != ["Authorization"]
                or runtime.get("environmentVariables", {}).get("COGNITO_CLIENT_ID") != state["outputs"]["UserPoolClientId"]
            ):
                raise RuntimeError(f"{name} runtime is not the ready, reviewed OAuth configuration")
            endpoint = self.aws(
                "bedrock-agentcore-control", "get-agent-runtime-endpoint",
                "--agent-runtime-id", before["agentRuntimeId"], "--endpoint-name", "DEFAULT",
            )
            if endpoint["status"] != "READY" or endpoint["liveVersion"] != expected["version"]:
                raise RuntimeError(f"{name} DEFAULT endpoint is not serving the reviewed runtime version")
            expected["status"] = runtime["status"]
            print(f"Verified existing {name} runtime: READY, expected image and Cognito OAuth/header settings")
        state["runtimesVerified"] = True
        self.save(state)

    def publish_frontend(self) -> None:
        """Publish only the passing LUMI export and retain rollback artifacts."""
        self.verify_runtimes()
        state = self.read_state()
        if hashlib.sha256(archive_paths(TEST_SOURCE_FOLDERS)).hexdigest() != state["sourceSha256"]:
            raise RuntimeError("Application source changed after cloud regression tests")
        body = self.get(state["testBucket"], state["artifactKey"])
        if hashlib.sha256(body).hexdigest() != state["artifactSha256"]:
            raise RuntimeError("Frontend artifact changed")
        bucket = state["outputs"]["FrontendBucketName"]
        distribution = state["outputs"]["FrontendDistributionId"]
        listing = self.aws("s3api", "list-objects-v2", "--bucket", bucket).get("Contents", [])
        unchanged = sorted((obj["Key"], obj["ETag"]) for obj in listing if not obj["Key"].startswith("lumi/"))
        state["otherFrontendPrefixesSha256"] = hashlib.sha256(json.dumps(unchanged).encode()).hexdigest()
        backups = []
        for obj in listing:
            if not obj["Key"].startswith("lumi/"):
                continue
            response = self.s3().get_object(Bucket=bucket, Key=obj["Key"])
            try:
                data = response["Body"].read()
            finally:
                response["Body"].close()
            metadata = {k: response[k] for k in ("ContentType", "CacheControl", "ContentEncoding") if k in response}
            backup = f"{state['release']}/lumi-before/{obj['Key']}"
            self.put(state["bucket"], backup, data, **metadata)
            backups.append({"key": obj["Key"], "backup": backup,
                            "sha256": hashlib.sha256(data).hexdigest(), **metadata})
        self.put(state["bucket"], f"{state['release']}/lumi-before.json", json.dumps(backups).encode())
        hashes = {}
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if "index.html" not in archive.namelist() or "sw.js" not in archive.namelist():
                raise RuntimeError("LUMI export is missing its entry point or worker")
            for name in sorted(
                (n for n in archive.namelist() if not n.endswith("/")),
                key=lambda n: (n == "sw.js", n.endswith((".html", ".txt", ".json"))),
            ):
                if name.startswith("/") or ".." in name.split("/"):
                    raise RuntimeError("Unsafe frontend archive path")
                data = archive.read(name)
                key = "lumi/" + name
                self.put(bucket, key, data, CacheControl="no-cache",
                         ContentType=mimetypes.guess_type(name)[0] or "application/octet-stream")
                hashes[key] = hashlib.sha256(data).hexdigest()
        self.put(state["bucket"], f"{state['release']}/lumi-hashes.json", json.dumps(hashes).encode())
        invalidation = self.aws("cloudfront", "create-invalidation", "--distribution-id",
                                distribution, "--paths", "/lumi/*")["Invalidation"]
        state.update(frontendPublished=True, frontendFileCount=len(hashes), invalidationId=invalidation["Id"])
        self.save(state)
        print(f"Published {len(hashes)} tested files only under /lumi/; invalidation {invalidation['Id']}")

    def verify_frontend(self) -> None:
        self.identity()
        state = self.read_state()
        bucket = state["outputs"]["FrontendBucketName"]
        hashes = json.loads(self.get(state["bucket"], f"{state['release']}/lumi-hashes.json"))
        for key, digest in hashes.items():
            if hashlib.sha256(self.get(bucket, key)).hexdigest() != digest:
                raise RuntimeError(f"LUMI frontend mismatch: {key}")
        listing = self.aws("s3api", "list-objects-v2", "--bucket", bucket).get("Contents", [])
        unchanged = sorted((obj["Key"], obj["ETag"]) for obj in listing if not obj["Key"].startswith("lumi/"))
        if hashlib.sha256(json.dumps(unchanged).encode()).hexdigest() != state["otherFrontendPrefixesSha256"]:
            raise RuntimeError("StayOS shell or another frontend prefix changed")
        invalidation = self.aws(
            "cloudfront", "get-invalidation", "--distribution-id",
            state["outputs"]["FrontendDistributionId"], "--id", state["invalidationId"],
        )["Invalidation"]
        if invalidation["Status"] != "Completed":
            raise RuntimeError(f"CloudFront invalidation: {invalidation['Status']}")
        state["frontendVerified"] = True
        self.save(state)
        print(f"Verified {len(hashes)} LUMI files; StayOS shell and PULSE objects unchanged.")

    def invoke_probe(self, event: dict) -> dict:
        """Read an API response in anonymous memory, never a local/customer file."""
        fd = os.memfd_create("stayos-security-probe")
        try:
            metadata = self.aws(
                "lambda", "invoke", "--function-name", "stayos-api",
                "--cli-binary-format", "raw-in-base64-out", "--payload", json.dumps(event),
                f"/proc/self/fd/{fd}", pass_fds=(fd,),
            )
            if metadata.get("FunctionError"):
                raise RuntimeError("API probe invocation failed; response content withheld")
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as stream:
                return json.load(stream)
        finally:
            os.close(fd)

    @staticmethod
    def probe_http(
        url: str, headers: dict | None = None, *, method: str = "GET",
    ) -> tuple[int, dict]:
        """Return status/headers only; capabilities and response bodies stay private."""
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers or {}, method=method), timeout=25,
            ) as response:
                response.read(1)
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                return error.code, dict(error.headers)
            finally:
                error.close()
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise RuntimeError(f"HTTP probe transport failed: {type(error).__name__}") from None

    def authorized_audio_probe(self) -> tuple[dict, str, int]:
        """Exercise deployed handler ownership without claiming a real Cognito login."""
        state = self.read_state()
        if not state.get("verifiedPhases", {}).get("audio"):
            raise RuntimeError("Verified CloudFront audio authorization deployment required")
        event = {
            "httpMethod": "GET", "path": "/briefs/ALOHA-TYO-001",
            "requestContext": {"authorizer": {"claims": {
                "aud": state["outputs"]["UserPoolClientId"], "token_use": "id",
                "custom:propertyId": "ALOHA-TYO-001",
            }}},
        }
        response = self.invoke_probe(event)
        if response.get("statusCode") != 200 or response.get("headers", {}).get("Cache-Control") != "private, no-store":
            raise RuntimeError("Authorized handler probe or private cache header failed")
        brief = json.loads(response["body"])
        audio = brief.get("audioBrief", {})
        url = audio.get("audioUrl", "")
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        if (
            parts.scheme != "https" or parts.hostname != state["outputs"]["AudioDistributionDomain"]
            or set(query) != {"Expires", "Signature", "Key-Pair-Id"}
            or "ALOHA-TYO-001" not in parts.path
        ):
            raise RuntimeError("Handler did not issue the expected property-scoped CloudFront capability")
        expiry = int(query["Expires"][0])
        if not 850 <= expiry - time.time() <= 900:
            raise RuntimeError("Audio capability does not have the expected 900-second lifetime")
        return event, url, expiry

    def verify_audio(self) -> None:
        self.identity()
        event, url, expiry = self.authorized_audio_probe()
        parts = urllib.parse.urlsplit(url)
        signed_status, _ = self.probe_http(url, {"Range": "bytes=0-0"})
        unsigned_status, _ = self.probe_http(urllib.parse.urlunsplit(parts._replace(query="")))
        query = urllib.parse.parse_qs(parts.query)
        query["Expires"] = [str(expiry - 1)]
        tampered_status, _ = self.probe_http(urllib.parse.urlunsplit(
            parts._replace(query=urllib.parse.urlencode(query, doseq=True))
        ))
        other_property_status, _ = self.probe_http(urllib.parse.urlunsplit(
            parts._replace(path=parts.path.replace("ALOHA-TYO-001", "ALOHA-LON-001"))
        ))
        if signed_status not in (200, 206) or any(
            status != 403 for status in (unsigned_status, tampered_status, other_property_status)
        ):
            raise RuntimeError(
                f"Audio boundary failed: signed={signed_status}, unsigned={unsigned_status}, "
                f"tampered={tampered_status}, foreignPath={other_property_status}"
            )
        negative_statuses = {}
        for label, claim, value, expected in [
            ("wrongClient", "aud", "different-client", 401),
            ("wrongTokenUse", "token_use", "access", 401),
            ("foreignProperty", "custom:propertyId", "ALOHA-LON-001", 403),
        ]:
            negative = copy.deepcopy(event)
            negative["requestContext"]["authorizer"]["claims"][claim] = value
            status = self.invoke_probe(negative).get("statusCode")
            if status != expected:
                raise RuntimeError(f"API handler negative authorization failed: {label}={status}")
            negative_statuses[label] = status
        direct_s3_status, _ = self.probe_http(
            f"https://stayos-audio-{self.args.expected_account_id}.s3.{self.args.region}.amazonaws.com{parts.path}"
        )
        if direct_s3_status != 403:
            raise RuntimeError(f"Direct unsigned S3 origin access was not denied: {direct_s3_status}")
        latest = json.loads(self.invoke_probe(event)["body"])
        date = latest.get("dailyKPIs", {}).get("date", "")
        if (
            latest.get("property", {}).get("propertyId") != "ALOHA-TYO-001"
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
        ):
            raise RuntimeError("Latest brief lacks the frontend's property/date renewal identity")
        renewal = copy.deepcopy(event)
        renewal["path"] += "/" + date
        renewed = self.invoke_probe(renewal)
        if (
            renewed.get("statusCode") != 200
            or renewed.get("headers", {}).get("Cache-Control") != "private, no-store"
        ):
            raise RuntimeError("Existing dated-brief renewal route did not return an authorized uncached brief")
        audio = json.loads(renewed["body"]).get("audioBrief", {})
        renewed_parts = urllib.parse.urlsplit(audio.get("audioUrl", ""))
        renewed_query = urllib.parse.parse_qs(renewed_parts.query)
        if (
            renewed_parts.hostname != parts.hostname or renewed_parts.path != parts.path
            or set(renewed_query) != {"Expires", "Signature", "Key-Pair-Id"}
            or not audio.get("audioUrlExpiresAt")
        ):
            raise RuntimeError("Dated renewal did not issue the same authorized object's playback capability")
        renewed_status, _ = self.probe_http(audio["audioUrl"], {"Range": "bytes=0-0"})
        if renewed_status not in (200, 206):
            raise RuntimeError(f"Renewed dated-brief capability is not playable: {renewed_status}")
        state = self.read_state()
        state["audioLiveChecks"] = {
            "checkedAt": datetime.now(UTC).isoformat(),
            "authorizedHandler": 200, "signedRange": signed_status,
            "unsignedAfterSignedRequest": unsigned_status, "tamperedExpiry": tampered_status,
            "foreignPropertyPath": other_property_status, "directS3": direct_s3_status,
            "handlerNegativeClaims": negative_statuses,
            "datedRenewal": {"handler": 200, "signedRange": renewed_status, "privateNoStore": True},
            "scope": "Synthetic API Gateway claims test the deployed handler, not real Cognito authentication",
        }
        self.save(state)
        print("Live audio passed: signed playback; unsigned, tampered, foreign path and S3 access denied.")
        print("Deployed API handler passed wrong-client/token-use/property checks; no content or URLs recorded.")

    def verify_audio_expiry(self) -> None:
        """Observe a genuine signature expiring without reading or minting private keys."""
        self.identity()
        _, url, expiry = self.authorized_audio_probe()
        status, _ = self.probe_http(url, {"Range": "bytes=0-0"})
        if status not in (200, 206):
            raise RuntimeError("Audio capability was not valid before its expiry")
        print("Captured one valid capability in process memory; waiting for its real 900-second expiry.", flush=True)
        while time.time() <= expiry + 5:
            time.sleep(min(30, max(1, expiry + 6 - time.time())))
        expired_status, _ = self.probe_http(url, {"Range": "bytes=0-0"})
        if expired_status != 403:
            raise RuntimeError(f"Expired CloudFront capability was not denied: {expired_status}")
        state = self.read_state()
        state["audioExpiryCheck"] = {
            "checkedAt": datetime.now(UTC).isoformat(), "beforeExpiry": status,
            "afterExpiry": expired_status, "signedLifetimeSeconds": 900,
        }
        self.save(state)
        print("Genuine expiry verified: previously playable URL returns 403 after its 900-second lifetime.")

    def verify_cors(self) -> None:
        """Check custom-origin preflight, rejection, handler headers and signed audio."""
        self.identity()
        state = self.read_state()
        origin = state.get("frontendOrigin")
        if not origin or not state.get("verifiedPhases", {}).get("cors"):
            raise RuntimeError("Verified CORS deployment and explicit browser origin required")
        base = state["outputs"]["ApiGatewayUrl"]
        paths = (
            "/briefs/ALOHA-TYO-001", "/briefs/ALOHA-TYO-001/2026-11-03",
            "/briefs/history", "/settings/browser-probe",
        )
        checks = {}
        for path in paths:
            status, headers = self.probe_http(base + path, {
                "Origin": origin, "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            }, method="OPTIONS")
            normalized = {key.lower(): value for key, value in headers.items()}
            if status != 200 or normalized.get("access-control-allow-origin") != origin:
                raise RuntimeError("Browser preflight does not allow the configured frontend origin")
            checks[path] = status
        anonymous, headers = self.probe_http(base + paths[0], {"Origin": origin})
        if (
            anonymous != 401
            or {key.lower(): value for key, value in headers.items()}.get("access-control-allow-origin") != origin
        ):
            raise RuntimeError("Anonymous rejection lost authentication or browser CORS")
        event, url, _ = self.authorized_audio_probe()
        event["headers"] = {"Origin": origin}
        latest = self.invoke_probe(event)
        if latest.get("statusCode") != 200 or latest.get("headers", {}).get("Access-Control-Allow-Origin") != origin:
            raise RuntimeError("Latest handler does not return the configured browser origin")
        brief = json.loads(latest["body"])
        date = brief.get("dailyKPIs", {}).get("date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise RuntimeError("Latest brief cannot establish a dated-handler probe")
        event["path"] += "/" + date
        dated = self.invoke_probe(event)
        if (
            dated.get("statusCode") != 200
            or dated.get("headers", {}).get("Access-Control-Allow-Origin") != origin
            or dated.get("headers", {}).get("Cache-Control") != "private, no-store"
        ):
            raise RuntimeError("Dated handler browser origin or private cache policy failed")
        audio, _ = self.probe_http(url, {"Origin": origin, "Range": "bytes=0-0"})
        if audio != 206:
            raise RuntimeError("Fresh signed audio failed with browser Origin and Range headers")
        state["corsLiveChecks"] = {
            "checkedAt": datetime.now(UTC).isoformat(), "browserOrigin": origin,
            "preflight": checks, "anonymous": anonymous,
            "latestHandler": latest["statusCode"], "datedHandler": dated["statusCode"],
            "signedAudioRange": audio,
            "scope": "Public preflight/rejection and synthetic handler claims; signed-in browser retest required",
        }
        self.save(state)
        print("Custom-origin CORS verified: four preflights, anonymous 401, latest/dated handler headers and signed audio 206.")

    def verify_controls_live(self) -> None:
        """Verify public API denial, scoped IAM decisions and runtime handshake denial."""
        self.identity()
        state = self.read_state()
        if not state.get("runtimesVerified"):
            raise RuntimeError("Verified runtime OAuth deployment required")
        checks = {}
        for name, url in [
            ("lumiAnonymous", state["outputs"]["ApiGatewayUrl"] + "/briefs/ALOHA-TYO-001"),
            ("pulseAnonymous", "https://mxwmulf5kb.execute-api.us-east-1.amazonaws.com/v1/alerts?propertyId=ALOHA-TYO-001"),
        ]:
            status, _ = self.probe_http(url)
            if status not in (401, 403):
                raise RuntimeError(f"Anonymous API request not denied: {name}={status}")
            checks[name] = status
        api = self.aws("apigateway", "get-rest-api", "--rest-api-id", "31e2m37mej")
        if api.get("securityPolicy") != "SecurityPolicy_TLS13_1_2_2021_06" or api.get("endpointAccessMode") != "BASIC":
            raise RuntimeError("REST API security policy differs from reviewed configuration")
        checks["restSecurityPolicy"] = api["securityPolicy"]
        checks["restApiStatus"] = api.get("apiStatus")
        host = "31e2m37mej.execute-api.us-east-1.amazonaws.com"
        tls_checks = {}
        for version in (
            ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1,
            ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3,
        ):
            context = ssl.create_default_context()
            # Lower client restrictions only for a negative legacy TLS probe.
            # Certificate and hostname validation remain enabled.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                context.minimum_version = version
                context.maximum_version = version
            if version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
                context.set_ciphers("DEFAULT:@SECLEVEL=0")
            try:
                with (
                    socket.create_connection((host, 443), timeout=20) as connection,
                    context.wrap_socket(connection, server_hostname=host) as secure,
                ):
                    negotiated = secure.version()
            except ssl.SSLError as error:
                if (
                    version not in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1)
                    or (
                        error.reason != "TLSV1_ALERT_PROTOCOL_VERSION"
                        and not isinstance(error, ssl.SSLZeroReturnError)
                    )
                ):
                    raise RuntimeError(f"TLS probe inconclusive or failed: {version.name}/{error.reason}") from None
                tls_checks[version.name] = (
                    "server closed negotiation" if isinstance(error, ssl.SSLZeroReturnError)
                    else "server rejected protocol"
                )
            else:
                if version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
                    raise RuntimeError(f"REST API still accepts {version.name}")
                tls_checks[version.name] = negotiated
        checks["tlsHandshakeChecks"] = tls_checks
        for name, runtime in state["runtimeBefore"].items():
            encoded = urllib.parse.quote(runtime["agentRuntimeArn"], safe="")
            url = (
                f"https://bedrock-agentcore.{self.args.region}.amazonaws.com/runtimes/{encoded}/ws"
                f"?X-Amzn-Bedrock-AgentCore-Runtime-Session-Id={uuid.uuid4()}"
            )
            headers = {
                "Connection": "Upgrade", "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            }
            for mode in ("missing", "invalid"):
                request_headers = dict(headers)
                if mode == "invalid":
                    request_headers["Sec-WebSocket-Protocol"] = (
                        "base64UrlBearerAuthorization.aW52YWxpZA, base64UrlBearerAuthorization"
                    )
                status, _ = self.probe_http(url, request_headers)
                if status not in (401, 403):
                    raise RuntimeError(f"{name} OAuth handshake negative failed: {mode}={status}")
                checks[f"{name}Handshake_{mode}"] = status
        role = f"arn:aws:iam::{self.args.expected_account_id}:role/stayos-voice-identity-auth-us-east-1"
        resources = [
            state["runtimeBefore"]["voice"]["agentRuntimeArn"],
            state["runtimeBefore"]["chat"]["agentRuntimeArn"],
            f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:runtime/pulse_triage_agent-F4VoR84jUL",
            f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:runtime/pulse_forecaster-t48H8mDkhc",
            f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:runtime/unrelated_agent-example",
        ]
        decisions = self.aws(
            "iam", "simulate-principal-policy", "--policy-source-arn", role,
            "--action-names", "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
            "--resource-arns", *resources,
        )["EvaluationResults"]
        normalized = simulation_decisions(decisions)
        action = "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream"
        if any(normalized.get((action, resource)) != ("allowed" if i < 2 else "implicitDeny")
               for i, resource in enumerate(resources)):
            raise RuntimeError("Browser IAM scope does not match required runtime boundaries")
        checks["browserIam"] = dict(zip(("voice", "chat", "triage", "forecaster", "unrelated"), (
            normalized[(action, resource)] for resource in resources
        )))
        chat_role = state["outputs"]["ChatAgentCoreRoleArn"]
        for label, action, allowed, denied in [
            (
                "chatGateway", "bedrock-agentcore:InvokeGateway",
                f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:gateway/stayos-gateway-ejqvqhp7fe",
                f"arn:aws:bedrock-agentcore:{self.args.region}:{self.args.expected_account_id}:gateway/unrelated-example",
            ),
            (
                "chatProfile", "bedrock:InvokeModel",
                f"arn:aws:bedrock:{self.args.region}:{self.args.expected_account_id}:inference-profile/us.anthropic.claude-sonnet-4-6",
                f"arn:aws:bedrock:{self.args.region}:{self.args.expected_account_id}:inference-profile/us.anthropic.claude-opus-4-6-v1",
            ),
        ]:
            evaluation = self.aws(
                "iam", "simulate-principal-policy", "--policy-source-arn", chat_role,
                "--action-names", action, "--resource-arns", allowed, denied,
            )["EvaluationResults"]
            decisions = simulation_decisions(evaluation)
            if decisions.get((action, allowed)) != "allowed" or decisions.get((action, denied)) != "implicitDeny":
                raise RuntimeError(f"{label} IAM boundary failed")
            checks[label] = {"required": "allowed", "unrelated": "implicitDeny"}
        required_models = [
            f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-6"
            for region in ("us-east-1", "us-east-2", "us-west-2")
        ]
        denied_model = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
        evaluation = self.aws(
            "iam", "simulate-principal-policy", "--policy-source-arn", chat_role,
            "--action-names", "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
            "--resource-arns", *required_models, denied_model,
        )["EvaluationResults"]
        decisions = simulation_decisions(evaluation)
        if any(decisions.get((action, resource)) != ("implicitDeny" if resource == denied_model else "allowed")
               for action in ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream")
               for resource in (*required_models, denied_model)):
            raise RuntimeError("Chat foundation-model IAM boundary failed")
        checks["chatModels"] = {"requiredUSRegions": "allowed", "unrelatedModel": "implicitDeny"}
        state = self.read_state()
        state["liveControlChecks"] = {"checkedAt": datetime.now(UTC).isoformat(), **checks}
        self.save(state)
        print("Live controls passed: anonymous APIs and invalid runtime handshakes denied; browser IAM scoped.")

    def verify_pulse_logs(self) -> None:
        """Confirm actual access-log delivery using an anonymous probe and counts only."""
        self.identity()
        state = self.read_state()
        if not state.get("verifiedPhases", {}).get("pulse"):
            raise RuntimeError("Verified PULSE stage deployment required")
        stage = self.aws("apigatewayv2", "get-stage", "--api-id", "mxwmulf5kb", "--stage-name", "v1")
        expected_group = "/aws/lambda/pulse-api"
        expected_arn = f"arn:aws:logs:{state['region']}:{state['account']}:log-group:{expected_group}"
        logging = stage.get("AccessLogSettings", {})
        throttle = stage.get("DefaultRouteSettings", {})
        if (
            logging.get("DestinationArn") != expected_arn
            or throttle.get("ThrottlingRateLimit") != 50
            or throttle.get("ThrottlingBurstLimit") != 100
            or set(json.loads(logging.get("Format", "{}"))) != {
                "service", "requestId", "httpMethod", "routeKey", "status",
                "responseLength", "integrationLatency",
            }
        ):
            raise RuntimeError("PULSE stage differs from the reviewed access-log/throttle configuration")
        start = int((time.time() - 60) * 1000)
        status, headers = self.probe_http(
            "https://mxwmulf5kb.execute-api.us-east-1.amazonaws.com/v1/alerts?propertyId=ALOHA-TYO-001"
        )
        request_id = {key.lower(): value for key, value in headers.items()}.get("apigw-requestid", "")
        if status not in (401, 403) or not re.fullmatch(r"[A-Za-z0-9_=-]+", request_id):
            raise RuntimeError("PULSE logging probe lacks an expected denied response/request ID")
        pattern = '{ $.service = "pulse-api-gateway" && $.requestId = "' + request_id + '" }'
        deadline = time.monotonic() + 300
        while True:
            count = self.aws(
                "logs", "filter-log-events", "--log-group-name", expected_group,
                "--start-time", str(start), "--filter-pattern", pattern,
                "--query", "length(events)",
            )
            if count:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PULSE probe access log has not arrived within five minutes")
            print("Waiting for delivery of this specific PULSE access-log request.", flush=True)
            time.sleep(15)
        state = self.read_state()
        state["pulseAccessLogCheck"] = {
            "checkedAt": datetime.now(UTC).isoformat(), "group": expected_group,
            "requestStatus": status, "matchingAccessLogEntries": count,
            "ratePerSecond": 50, "burst": 100,
            "scope": "Log delivery verified by count; no request body, credentials or customer logs retrieved",
        }
        self.save(state)
        print("PULSE access logging verified by request ID and count in its existing log group; throttling verified.")

    def preview(self) -> None:
        self.identity()
        state = self.read_state()
        phase = self.args.template_phase
        if state.get("buildStatus") != "SUCCEEDED" or not state.get("artifactSha256"):
            raise RuntimeError("Passing security regression build required")
        if hashlib.sha256(archive_paths(TEST_SOURCE_FOLDERS)).hexdigest() != state["sourceSha256"]:
            raise RuntimeError("Application source changed after the tested build")
        if phase in ("signer", "audio") and not state.get("signingSecretPopulated"):
            raise RuntimeError("Signing secret must be populated before enabling audio authorization")
        target = self.stack if phase != "pulse" else f"pulse-{self.args.region}"
        if phase == "pulse" and "pulseChildren" not in state:
            stack = self.aws("cloudformation", "describe-stacks", "--stack-name", target)["Stacks"][0]
            resources = self.aws("cloudformation", "list-stack-resources", "--stack-name", target)[
                "StackResourceSummaries"
            ]
            state["pulseChildren"] = {
                r["LogicalResourceId"]: r["PhysicalResourceId"] for r in resources
                if r["ResourceType"] == "AWS::CloudFormation::Stack"
            }
            state["pulseFunctions"] = self.aws(
                "lambda", "list-functions", "--query",
                "Functions[?starts_with(FunctionName, 'pulse-')]."
                "{name:FunctionName,hash:CodeSha256,handler:Handler,role:Role}",
            )
            state["pulseParameters"] = [p["ParameterKey"] for p in stack["Parameters"]]
            state["pulseTags"] = stack.get("Tags", [])
            for logical, arn in {"root": target, **state["pulseChildren"]}.items():
                self.put(state["bucket"], f"{state['release']}/pulse-baseline-{logical}.json",
                         json.dumps(self.template(arn)).encode())
            self.save(state)
        children = state["children"] if phase != "pulse" else state["pulseChildren"]
        root = self.template(target)
        current = {}
        for logical, arn in children.items():
            stack = self.aws("cloudformation", "describe-stacks", "--stack-name", arn)["Stacks"][0]
            if stack["StackStatus"] not in ("UPDATE_COMPLETE", "CREATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
                raise RuntimeError(f"Child stack is not stable: {logical}")
            current[logical] = {p["ParameterKey"]: p["ParameterValue"] for p in stack["Parameters"]}
        root, pinned = pin_dependencies(root, current)
        state["pinnedInputs"] = pinned
        templates = {}
        parameter_overrides = {}
        if phase == "cors":
            source_root = load_template((ROOT / "lumi/infrastructure/root-stack.yaml").read_text())
            root = patch_frontend_domain(root, source_root, self.args.frontend_domain)
            parameter_overrides["FrontendDomain"] = self.args.frontend_domain
            templates["ComputeStack"] = self.template(children["ComputeStack"])
            state["frontendOrigin"] = f"https://{self.args.frontend_domain}"
        if phase in ("bootstrap", "signer", "audio"):
            data_source = load_template((ROOT / "lumi/infrastructure/nested-stacks/data.yaml").read_text())
            data = patch_data(self.template(children["DataStack"]), data_source, phase)
            templates["DataStack"] = data
        if phase == "controls":
            attachment = self.aws(
                "cloudformation", "describe-stack-resource", "--stack-name", children["VoiceStack"],
                "--logical-resource-id", "VoiceIdentityPoolRoleAttachment",
            )["StackResourceDetail"]
            state["identityRoleAttachmentBefore"] = attachment["PhysicalResourceId"]
            for logical, filename, role, policies in [
                ("VoiceStack", "voice", "VoiceIdentityPoolAuthRole", ("AgentCoreInvoke",)),
                ("ChatStack", "chat", "ChatAgentCoreRole", ("BedrockConverse", "AgentCoreGateway")),
            ]:
                template = self.template(children[logical])
                source = load_template((ROOT / f"lumi/infrastructure/nested-stacks/{filename}.yaml").read_text())
                desired = source["Resources"][role]["Properties"]["Policies"]
                for policy in template["Resources"][role]["Properties"]["Policies"]:
                    if policy["PolicyName"] not in policies:
                        continue
                    replacement = next(p for p in desired if p["PolicyName"] == policy["PolicyName"])
                    statements = policy["PolicyDocument"]["Statement"]
                    if len(statements) != 1 or statements[0]["Action"] != replacement["PolicyDocument"]["Statement"][0]["Action"]:
                        raise RuntimeError(f"Unexpected installed IAM policy: {logical}/{policy['PolicyName']}")
                    statements[0]["Resource"] = copy.deepcopy(
                        replacement["PolicyDocument"]["Statement"][0]["Resource"]
                    )
                templates[logical] = template
        if phase == "pulse":
            template = self.template(children["ApiStack"])
            source = load_template((ROOT / "pulse/infrastructure/nested-stacks/pulse-api.yaml").read_text())
            stage = template["Resources"]["PulseApiStage"]["Properties"]
            for key in ("AccessLogSettings", "DefaultRouteSettings"):
                desired = source["Resources"]["PulseApiStage"]["Properties"][key]
                if key == "DefaultRouteSettings":
                    stage.setdefault(key, {}).update(copy.deepcopy(desired))
                else:
                    stage[key] = copy.deepcopy(desired)
            templates["ApiStack"] = template
        if phase in ("signer", "audio"):
            public_pem = self.get(state["bucket"], state["publicKeyArtifact"]).decode()
            source_root = load_template((ROOT / "lumi/infrastructure/root-stack.yaml").read_text())
            root.setdefault("Parameters", {})["AudioSigningPublicKeyPem"] = copy.deepcopy(
                source_root["Parameters"]["AudioSigningPublicKeyPem"]
            )
            root["Resources"]["DataStack"]["Properties"]["Parameters"]["AudioSigningPublicKeyPem"] = {
                "Ref": "AudioSigningPublicKeyPem",
            }
            parameter_overrides["AudioSigningPublicKeyPem"] = public_pem
        if phase == "signer":
            source = load_template((ROOT / "lumi/infrastructure/nested-stacks/compute.yaml").read_text())
            api_body = archive_paths(("lumi/backend/functions/api",), strip="lumi/backend/functions/api")
            api_hash = hashlib.sha256(api_body).hexdigest()
            api_key = f"{state['release']}/api-{api_hash}.zip"
            self.put(state["bucket"], api_key, api_body)
            state.update(apiArtifact=api_key, apiArtifactSha256=api_hash)
            root["Parameters"]["ApiCodeS3Key"] = copy.deepcopy(source_root["Parameters"]["ApiCodeS3Key"])
            parameter_overrides["ApiCodeS3Key"] = api_key
            compute = patch_compute(self.template(children["ComputeStack"]), source, api_key)
            installed = self.aws("cloudformation", "list-stack-resources",
                                 "--stack-name", children["ComputeStack"])["StackResourceSummaries"]
            state["apiResourceIdsBefore"] = {
                item["LogicalResourceId"]: item["PhysicalResourceId"] for item in installed
                if item["ResourceType"].startswith("AWS::ApiGateway::")
            }
            templates["ComputeStack"] = compute
            parameters = root["Resources"]["ComputeStack"]["Properties"]["Parameters"]
            parameters["AudioSigningSecretArn"] = state["signingSecretArn"]
            parameters["AudioSigningKeyId"] = {"Fn::GetAtt": ["DataStack", "Outputs.AudioSigningKeyId"]}
            parameters["ApiCodeS3Key"] = {"Ref": "ApiCodeS3Key"}
        for logical, template in templates.items():
            key = f"{state['release']}/{phase}-{logical}.json"
            self.put(state["bucket"], key, json.dumps(template).encode())
            root["Resources"][logical]["Properties"]["TemplateURL"] = (
                f"https://s3.{self.args.region}.amazonaws.com/{state['bucket']}/{key}"
            )
        candidate = json.dumps(root).encode()
        self.put(state["bucket"], f"{state['release']}/{phase}-root.json", candidate)
        fd = os.memfd_create("stayos-security-template")
        try:
            os.write(fd, candidate)
            override_args = (
                ["--parameter-overrides", *[f"{k}={v}" for k, v in parameter_overrides.items()]]
                if parameter_overrides else []
            )
            output = self.aws(
                "cloudformation", "deploy", "--stack-name", target,
                "--template-file", f"/proc/self/fd/{fd}", "--capabilities", "CAPABILITY_NAMED_IAM",
                "--no-execute-changeset", *override_args, cfn=True, raw=True, pass_fds=(fd,),
            )
        finally:
            os.close(fd)
        match = re.search(r"arn:[^\s]+:changeSet/[^\s]+", output)
        if not match:
            raise RuntimeError("CloudFormation deploy returned no change set")
        request = {
            "StackName": target, "ChangeSetType": "UPDATE",
            "ChangeSetName": state["release"].replace("/", "-") + "-" + phase
            + f"-{len(state.get('previewAttempts', [])) + 1}",
            "TemplateBody": candidate.decode(),
            "Parameters": [
                {"ParameterKey": k, "ParameterValue": v} for k, v in parameter_overrides.items()
            ] + [{
                "ParameterKey": k, "UsePreviousValue": True,
            } for k in (state["parameters"] if phase != "pulse" else state["pulseParameters"])
                if k not in parameter_overrides],
            "Capabilities": ["CAPABILITY_NAMED_IAM"], "IncludeNestedStacks": True,
            "Tags": state["tags"] if phase != "pulse" else state["pulseTags"],
        }
        reviewed = self.aws("cloudformation", "create-change-set", "--cli-input-json",
                            json.dumps(request), cfn=True)
        if state.get("changeSet"):
            state.setdefault("previewAttempts", []).append({
                "phase": state.get("templatePhase"), "changeSet": state["changeSet"],
                "previewPassed": state.get("previewPassed", False),
                "executed": any(e["changeSet"] == state["changeSet"] for e in state.get("executions", [])),
            })
        state.update(templatePhase=phase, deployChangeSet=match.group(0),
                     changeSet=reviewed["Id"], previewPassed=False, targetStack=target,
                     targetChildren=children, expectedChildren=list(templates))
        self.save(state)
        print(f"Created {phase} nested preview: {reviewed['Id']}")

    def inspect(self) -> None:
        self.identity()
        state = self.read_state()
        preview = self.aws("cloudformation", "describe-change-set", "--change-set-name",
                           state["changeSet"], cfn=True)
        if preview["Status"] != "CREATE_COMPLETE":
            raise RuntimeError(f"Security preview is {preview['Status']}: {preview.get('StatusReason', '')}")
        if preview.get("ExecutionStatus") != "AVAILABLE":
            raise RuntimeError("Security preview is not available for execution")
        expected = set(state["expectedChildren"])
        seen, verified = set(), []
        for item in preview.get("Changes", []):
            change = item["ResourceChange"]
            logical = change["LogicalResourceId"]
            if logical not in expected or change["Action"] != "Modify" or change.get("Replacement") != "False":
                raise RuntimeError(f"Unrelated root change: {logical}")
            child = self.aws("cloudformation", "describe-change-set", "--change-set-name",
                             change["ChangeSetId"], cfn=True)
            leaves = child.get("Changes", [])
            if state["templatePhase"] in ("signer", "cors") and logical == "ComputeStack":
                baseline = self.template(state["children"]["ComputeStack"])
                phase = state["templatePhase"]
                candidate = json.loads(self.get(state["bucket"], f"{state['release']}/{phase}-ComputeStack.json"))
                leaves, dependencies = separate_api_dependencies(
                    leaves, baseline, candidate,
                    direct_methods=CORS_METHODS if phase == "cors" else frozenset(),
                )
                state["unchangedApiDependencies"] = dependencies
            if state["templatePhase"] == "controls" and logical == "VoiceStack":
                baseline = self.template(state["children"]["VoiceStack"])
                candidate = json.loads(self.get(state["bucket"], f"{state['release']}/controls-VoiceStack.json"))
                leaves, dependencies = separate_voice_role_dependency(leaves, baseline, candidate)
                state["unchangedIdentityDependencies"] = dependencies
                attachment = self.aws(
                    "cloudformation", "describe-stack-resource", "--stack-name", state["children"]["VoiceStack"],
                    "--logical-resource-id", "VoiceIdentityPoolRoleAttachment",
                )["StackResourceDetail"]
                state["identityRoleAttachmentBefore"] = attachment["PhysicalResourceId"]
            names = validate_security_changes(leaves, state["templatePhase"], logical)
            verified.extend(f"{logical}/{n}" for n in names)
            seen.add(logical)
        if seen != expected:
            raise RuntimeError("Required nested changes missing")
        state.update(previewPassed=True, verifiedChanges=verified)
        self.save(state)
        print("Reviewed changes: " + ", ".join(verified))

    def deploy(self) -> None:
        self.inspect()
        state = self.read_state()
        self.aws("cloudformation", "execute-change-set", "--change-set-name", state["changeSet"], cfn=True)
        state.setdefault("executions", []).append({
            "phase": state["templatePhase"], "changeSet": state["changeSet"],
            "startedAt": datetime.now(UTC).isoformat(),
        })
        self.save(state)
        print(f"Executing reviewed {state['templatePhase']} security release.")

    def verify(self) -> None:
        self.identity()
        state = self.read_state()
        for logical, arn in {"root": state["targetStack"], **state["targetChildren"]}.items():
            stack = self.aws("cloudformation", "describe-stacks", "--stack-name", arn)["Stacks"][0]
            if stack["StackStatus"] not in ("UPDATE_COMPLETE", "CREATE_COMPLETE"):
                raise RuntimeError(f"{logical}: {stack['StackStatus']}")
            if logical == "root" and state["targetStack"] == self.stack:
                state["parameters"] = [p["ParameterKey"] for p in stack["Parameters"]]
        before = {f["name"]: f for f in state["functions"]}
        after = {f["name"]: f for f in self.functions()}
        allowed = {"stayos-api"} if state.get("verifiedPhases", {}).get("signer") or state["templatePhase"] == "signer" else set()
        if set(before) != set(after) or any(after[n] != f for n, f in before.items() if n not in allowed):
            raise RuntimeError("An unrelated StayOS function changed")
        if "stayos-api" in allowed:
            import base64

            actual = after["stayos-api"]["hash"]
            expected = base64.b64encode(bytes.fromhex(state["apiArtifactSha256"])).decode()
            if actual != expected:
                raise RuntimeError("API hash differs from tested release artifact")
            resources = self.aws("cloudformation", "list-stack-resources",
                                 "--stack-name", state["children"]["ComputeStack"])["StackResourceSummaries"]
            current_ids = {
                item["LogicalResourceId"]: item["PhysicalResourceId"] for item in resources
                if item["ResourceType"].startswith("AWS::ApiGateway::")
            }
            if current_ids != state["apiResourceIdsBefore"]:
                raise RuntimeError("An existing API resource identifier changed")
        child = self.aws("cloudformation", "describe-stacks", "--stack-name",
                         state["children"]["DataStack"])["Stacks"][0]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in child["Outputs"]}
        state["signingSecretArn"] = outputs["AudioSigningSecretArn"]
        if state["templatePhase"] == "controls":
            attachment = self.aws(
                "cloudformation", "describe-stack-resource", "--stack-name", state["children"]["VoiceStack"],
                "--logical-resource-id", "VoiceIdentityPoolRoleAttachment",
            )["StackResourceDetail"]
            if attachment["PhysicalResourceId"] != state["identityRoleAttachmentBefore"]:
                raise RuntimeError("Existing Cognito role attachment identifier changed")
            identity_roles = self.aws(
                "cognito-identity", "get-identity-pool-roles",
                "--identity-pool-id", state["outputs"]["VoiceIdentityPoolId"],
            )
            if identity_roles.get("Roles", {}).get("authenticated") != (
                f"arn:aws:iam::{state['account']}:role/stayos-voice-identity-auth-us-east-1"
            ):
                raise RuntimeError("Existing authenticated browser role attachment changed")
        if state["templatePhase"] == "pulse":
            after_pulse = self.aws(
                "lambda", "list-functions", "--query",
                "Functions[?starts_with(FunctionName, 'pulse-')]."
                "{name:FunctionName,hash:CodeSha256,handler:Handler,role:Role}",
            )
            if {f["name"]: f for f in after_pulse} != {f["name"]: f for f in state["pulseFunctions"]}:
                raise RuntimeError("An unrelated PULSE function changed")
        state.setdefault("verifiedPhases", {})[state["templatePhase"]] = datetime.now(UTC).isoformat()
        self.save(state)
        print("Security stack update verified; unrelated StayOS functions preserved.")

    def signing_key(self) -> None:
        self.identity()
        state = self.read_state()
        if not state.get("verifiedPhases", {}).get("bootstrap"):
            raise RuntimeError("Verified signing-secret bootstrap required")
        if state.get("signingSecretPopulated"):
            raise RuntimeError("Signing key already initialized; rotation needs a separate reviewed release")
        import boto3
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
        # No secret retrieval. PutSecretValue excludes SecretString from
        # CloudTrail, and neither value nor SDK request is printed or persisted.
        client = boto3.Session(profile_name=self.args.profile, region_name=self.args.region).client("secretsmanager")
        response = client.put_secret_value(
            SecretId=state["signingSecretArn"], SecretString=json.dumps({"privateKey": private}),
        )
        del private, key
        key_path = f"{state['release']}/audio-public-key.pem"
        self.put(state["bucket"], key_path, public)
        state.update(signingSecretPopulated=True, signingSecretVersion=response["VersionId"],
                     publicKeyArtifact=key_path, publicKeySha256=hashlib.sha256(public).hexdigest())
        self.save(state)
        print("RSA key generated in memory and written to the authorized secret; public PEM recorded.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=[
        "prepare", "test-build", "build-status", "preview", "inspect", "deploy", "verify", "signing-key",
        "build-agents", "agent-build-status", "update-runtimes",
        "verify-runtimes", "publish-frontend", "verify-frontend",
        "verify-audio", "verify-audio-expiry", "verify-controls-live",
        "verify-pulse-logs", "verify-cors",
    ])
    parser.add_argument("--template-phase", choices=["bootstrap", "signer", "audio", "controls", "pulse", "cors"], default="bootstrap")
    parser.add_argument("--frontend-domain", default="", help="Explicit browser hostname for the CORS repair")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--cloudformation-profile", required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-prefix", default="stayos")
    release = SecurityRelease(parser.parse_args())
    getattr(release, release.args.phase.replace("-", "_"))()


if __name__ == "__main__":
    main()
