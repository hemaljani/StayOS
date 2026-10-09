import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = Path(TOOLS_DIR).parent
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from stayos_deploy.config import DeploymentConfig
from stayos_deploy.deploy import (
    DeploymentFailed,
    DeploymentOrchestrator,
)
from stayos_deploy.output import (
    CommandFailed,
    CommandResult,
    Console,
    OutputSettings,
)


class StubRunner:
    def __init__(self, fail_target=None, warning_target=None):
        self.commands = []
        self.environments = []
        self.fail_target = fail_target
        self.warning_target = warning_target

    @staticmethod
    def _argument(command, name):
        return command[command.index(name) + 1]

    def run(self, command, *, cwd, extra_env=None, progress=None):
        del cwd
        self.commands.append(list(command))
        self.environments.append(extra_env)
        if command[0] == "make":
            directory = command[2]
            target = command[3]
            progress_lines = {
                ("lumi", "deploy"): [
                    "  - Verifying AgentCore CLI support...",
                    "  [layer] Packaging dependency build input...",
                    "  [layer] ComputeStack will build or reuse the Python 3.12 x86_64 layer...",
                    "  - Deploying infrastructure...",
                    "  updating stayos-api",
                    "  updating stayos-orchestrator",
                    "  updating stayos-seed-data",
                    "  updating stayos-tools",
                    "  [gateway] Fetching Tool Lambda ARN...",
                    "  [gateway] Gateway Target is READY",
                    "  [voice] Starting CodeBuild...",
                    "  [voice] Waiting for build to complete...",
                    "  [voice] Waiting for runtime READY status...",
                    "  [voice] Runtime is READY",
                    "  [chat] Starting CodeBuild...",
                    "  [chat] Waiting for build to complete...",
                    "  [chat] Waiting for runtime READY status...",
                    "  [chat] Runtime is READY",
                    "  - Building frontend...",
                    "  - Deploying frontend to S3...",
                ],
                ("pulse", "deploy-initial"): [
                    "Deploying pulse-us-east-1...",
                    "  [backend] Starting CodeBuild project pulse-backend-build...",
                    "  [backend] Build succeeded",
                    "  updating pulse-api",
                    "Lambda code refresh complete.",
                    "  [seed] Invoking pulse-kitchen-seed",
                ],
                ("pulse", "gateway-deploy"): [
                    "  [gateway] Reading shared Gateway id from SSM...",
                ],
                ("pulse", "triage-deploy"): [
                    "  [triage] Starting SHARED CodeBuild project...",
                    "  [triage] Resolving runtime configuration...",
                ],
                ("pulse", "forecast-deploy"): [
                    "  [forecast] Starting SHARED CodeBuild project...",
                    "  [forecast] Resolving runtime configuration...",
                ],
                ("pulse", "deploy-runtime-wiring"): [
                    "Updating pulse-us-east-1 runtime wiring...",
                ],
                ("pulse", "deploy-frontend"): [
                    "Building PULSE frontend...",
                    "Deploying PULSE frontend to S3...",
                ],
                ("stayos-shell", "deploy"): [
                    "Building shell frontend...",
                    "Deploying shell to S3...",
                ],
                ("shared/data-orchestrator", "deploy"): [
                    "  - Packaging orchestrator Lambda...",
                    "  - Deploying orchestrator stack...",
                    "  updating stayos-data-generate",
                    "  - Priming today's data for the estate...",
                    "  verified roll-forward and PULSE baseline",
                ],
            }
            if progress:
                for line in progress_lines.get((directory, target), []):
                    progress(line)
            if target == self.fail_target:
                raise CommandFailed(command, 23, "ERROR: stubbed stage failure")
            warnings = (
                ["WARNING: stubbed unresolved warning"]
                if target == self.warning_target
                else []
            )
            return CommandResult("make completed", warnings)

        joined = " ".join(command)
        if "sts get-caller-identity" in joined:
            return CommandResult("123456789012", [])
        if "parameterKeys:Parameters[].ParameterKey" in joined:
            return CommandResult(
                json.dumps(
                    {"status": "UPDATE_COMPLETE", "parameterKeys": ["AppPassword"]}
                ),
                [],
            )
        if "describe-stack-resources" in joined:
            return CommandResult("stayos-data-nested", [])
        if "ssm get-parameter" in joined:
            name = self._argument(command, "--name")
            values = {
                "/stayos/gateway/endpoint-url": "https://gateway.example",
                "/stayos/voice/runtime-id": "voice-id",
                "/stayos/chat/runtime-id": "chat-id",
                "/pulse/triage/runtime-arn": "arn:triage",
                "/pulse/forecast/runtime-arn": "arn:forecast",
            }
            return CommandResult(values[name], [])
        if "Stacks[0].StackStatus" in joined:
            return CommandResult("UPDATE_COMPLETE", [])

        output_keys = {
            "UserPoolId": "pool-id",
            "UserPoolClientId": "client-id",
            "ToolLambdaArn": "arn:tool",
            "ReservationsStreamArn": (
                "arn:aws:dynamodb:r:a:table/reservations/stream/1"
            ),
            "RoomsStreamArn": "arn:aws:dynamodb:r:a:table/rooms/stream/1",
            "GuestsStreamArn": "arn:aws:dynamodb:r:a:table/guests/stream/1",
            "RevenuesStreamArn": ("arn:aws:dynamodb:r:a:table/revenues/stream/1"),
            "WorkOrdersStreamArn": ("arn:aws:dynamodb:r:a:table/work-orders/stream/1"),
            "FrontendUrl": "https://stayos.example/",
            "ApiEndpoint": "https://api.example/",
            "RealtimeHttpEndpoint": "https://realtime.example/",
        }
        for key, value in output_keys.items():
            if f"OutputKey=='{key}'" in joined:
                return CommandResult(value, [])
        raise AssertionError(f"unexpected command: {command}")


class DeploymentTest(unittest.TestCase):
    def _orchestrator(self, runner, stream, *, verbose=False, password="", change="0"):
        config = DeploymentConfig.load(
            profile="test",
            cloudformation_profile="test-cfn",
            expected_account_id="123456789012",
            environ={},
        )
        output_environment = {
            "NO_COLOR": "1",
            "VERBOSE": "1" if verbose else "0",
            "CHANGE_APP_PASSWORD": change,
        }
        console = Console(
            OutputSettings.load(output_environment, stream),
            stream,
        )
        return DeploymentOrchestrator(
            config,
            app_password=password,
            root=ROOT,
            log_path=ROOT / "logs" / "stubbed.log",
            environ=output_environment,
            console=console,
            runner=runner,
        )

    def test_stubbed_deploy_has_eight_ordered_concise_stages(self):
        stream = io.StringIO()
        runner = StubRunner()

        self._orchestrator(runner, stream).deploy()

        output = stream.getvalue()
        stages = re.findall(r"^\[(\d)/8\]", output, re.MULTILINE)
        self.assertEqual([str(number) for number in range(1, 9)], stages)
        self.assertLessEqual(len(output.splitlines()), 80)
        self.assertNotIn("None", output)
        self.assertNotIn('{"', output)
        self.assertNotIn("upload:", output)
        self.assertIn("Preparing dependency-layer build input", output)
        self.assertIn("Building or reusing the x86_64 layer in ComputeStack", output)
        self.assertIn("Refreshing PULSE Lambda functions", output)

        make_targets = [
            command[3] for command in runner.commands if command[0] == "make"
        ]
        self.assertEqual(
            [
                "deploy",
                "deploy-initial",
                "gateway-deploy",
                "triage-deploy",
                "forecast-deploy",
                "deploy-runtime-wiring",
                "deploy-frontend",
                "deploy",
                "deploy",
            ],
            make_targets,
        )
        self.assertEqual(1, make_targets.count("deploy-initial"))
        self.assertEqual(1, make_targets.count("deploy-runtime-wiring"))
        self.assertIn("APP_PASSWORD step skipped", output)

    def test_explicit_replacement_supplies_password_only_to_lumi(self):
        runner = StubRunner()
        self._orchestrator(
            runner, io.StringIO(), password="fixture-password", change="1"
        ).deploy()
        for command, environment in zip(runner.commands, runner.environments):
            is_lumi = command[0] == "make" and command[2] == "lumi"
            self.assertEqual(
                "fixture-password" if is_lumi else "", environment["APP_PASSWORD"]
            )
            self.assertEqual(
                "1" if is_lumi else "0", environment["CHANGE_APP_PASSWORD"]
            )
            self.assertNotIn("fixture-password", " ".join(command))

    def test_ambiguous_password_stops_before_any_make_stage(self):
        runner = StubRunner()
        with self.assertRaises(DeploymentFailed):
            self._orchestrator(
                runner, io.StringIO(), password="fixture-password"
            ).deploy()
        self.assertTrue(all(command[0] != "make" for command in runner.commands))

    def test_verbose_summary_includes_detailed_diagnostics(self):
        stream = io.StringIO()

        self._orchestrator(StubRunner(), stream, verbose=True).deploy()

        output = stream.getvalue()
        self.assertIn("PULSE API: https://api.example/", output)
        self.assertIn("Triage runtime: arn:triage", output)
        self.assertIn("PULSE artifacts: s3://pulse-deploy-", output)

    def test_warning_does_not_report_plain_success(self):
        stream = io.StringIO()
        runner = StubRunner(warning_target="deploy")

        self._orchestrator(runner, stream).deploy()

        output = stream.getvalue()
        self.assertIn("warning", output.lower())
        self.assertIn("PASSED WITH WARNINGS", output)
        self.assertNotIn("✓ Foundation, Gateway", output)

    def test_failure_prints_recovery_and_preserves_exit_code(self):
        stream = io.StringIO()
        runner = StubRunner(fail_target="gateway-deploy")

        with self.assertRaises(DeploymentFailed) as raised:
            self._orchestrator(runner, stream).deploy()

        self.assertEqual(23, raised.exception.returncode)
        output = stream.getvalue()
        self.assertIn("Shared Gateway tools failed", output)
        self.assertIn("Recovery:", output)
        self.assertIn("Log:", output)
        self.assertNotIn("[4/8]", output)

    def test_runtime_wiring_make_target_is_cloudformation_only(self):
        variables = {
            "ACCOUNT_ID": "123456789012",
            "CFN_ACCOUNT_ID": "123456789012",
            "PROFILE": "test",
            "CLOUDFORMATION_PROFILE": "test",
            "USER_POOL_ID": "pool",
            "USER_POOL_ARN": "arn:pool",
            "USER_POOL_CLIENT_ID": "client",
            "GATEWAY_ENDPOINT_URL": "https://gateway",
            "RESERVATIONS_STREAM_ARN": "arn:reservations-stream",
            "ROOMS_STREAM_ARN": "arn:rooms-stream",
            "GUESTS_STREAM_ARN": "arn:guests-stream",
            "REVENUES_STREAM_ARN": "arn:revenues-stream",
            "WORK_ORDERS_STREAM_ARN": "arn:work-orders-stream",
            "RESERVATIONS_TABLE_ARN": "arn:reservations",
            "ROOMS_TABLE_ARN": "arn:rooms",
            "GUESTS_TABLE_ARN": "arn:guests",
            "REVENUES_TABLE_ARN": "arn:revenues",
            "WORK_ORDERS_TABLE_ARN": "arn:work-orders",
            "RESERVATIONS_TABLE_NAME": "reservations",
            "ROOMS_TABLE_NAME": "rooms",
            "GUESTS_TABLE_NAME": "guests",
            "TRIAGE_RUNTIME_ARN": "arn:triage",
            "FORECAST_RUNTIME_ARN": "arn:forecast",
            "ORCHESTRATED": "1",
        }
        command = ["make", "-n", "-C", "pulse", "deploy-runtime-wiring"]
        command.extend(f"{name}={value}" for name, value in variables.items())

        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )

        output = completed.stdout
        self.assertIn("cloudformation deploy", output)
        self.assertNotIn("codebuild start-build", output)
        self.assertNotIn("update-function-code", output)
        self.assertNotIn("seed-run", output)
        self.assertNotIn("lambda invoke", output)

    def test_dependency_installs_are_isolated_for_deployment_packages(self):
        lumi_makefile = (ROOT / "lumi" / "Makefile").read_text()
        data_makefile = (ROOT / "shared" / "data-orchestrator" / "Makefile").read_text()
        layer_buildspec = (
            ROOT / "lumi" / "backend" / "layers" / "dependencies" / "buildspec.yml"
        ).read_text()
        compute_template = (
            ROOT / "lumi" / "infrastructure" / "nested-stacks" / "compute.yaml"
        ).read_text()
        chat_template = (
            ROOT / "lumi" / "infrastructure" / "nested-stacks" / "chat.yaml"
        ).read_text()

        self.assertNotIn(
            "python3 -m pip install --ignore-installed",
            lumi_makefile,
        )
        self.assertNotIn("DEPENDENCY_BUILD_PROJECT", lumi_makefile)
        self.assertNotIn(
            "--project-name $(DEPENDENCY_BUILD_PROJECT)",
            lumi_makefile,
        )
        self.assertIn("LINUX_LAMBDA_CONTAINER", compute_template)
        self.assertIn("BUILD_LAMBDA_2GB", compute_template)
        self.assertIn(
            "aws/codebuild/amazonlinux-x86_64-lambda-standard:python3.12",
            compute_template,
        )
        self.assertNotIn("TimeoutInMinutes", compute_template)
        self.assertNotIn("QueuedTimeoutInMinutes", compute_template)
        self.assertNotIn("PrivilegedMode", compute_template)
        self.assertIn("Type: Custom::DependencyLayerBuild", compute_template)
        self.assertIn("DependsOn: DependencyLayerBuild", compute_template)
        self.assertIn("Action: s3:ListBucket", compute_template)
        self.assertIn('s3:prefix: "layers/*"', compute_template)
        self.assertFalse(
            (ROOT / "lumi" / "infrastructure" / "dependency-layer-build.yaml").exists()
        )
        self.assertIn("--only-binary=:all:", layer_buildspec)
        self.assertIn("--no-warn-conflicts", layer_buildspec)
        self.assertIn("python3 -m pip install --ignore-installed", data_makefile)
        self.assertIn("--no-warn-conflicts", data_makefile)
        self.assertIn(
            "DEPENDENCY_LAYER_S3_KEY := layers/stayos-dependencies-$(", lumi_makefile
        )
        self.assertIn(
            "DependenciesLayerS3Key=$(DEPENDENCY_LAYER_S3_KEY)",
            lumi_makefile,
        )
        self.assertIn("remove-legacy-layer-build-stack", lumi_makefile)
        self.assertIn("CompatibleArchitectures:\n        - x86_64", compute_template)
        self.assertNotIn("Architectures:\n        - arm64", compute_template)
        self.assertNotIn("Architectures:\n        - arm64", chat_template)

    def test_gateway_destroy_waits_for_targets_before_deleting_gateway(self):
        lumi_makefile = (ROOT / "lumi" / "Makefile").read_text()
        gateway_destroy = lumi_makefile.split("gateway-destroy:", 1)[1].split(
            "# ─── Voice Agent Build", 1
        )[0]

        delete_target = gateway_destroy.index("delete-gateway-target")
        count_targets = gateway_destroy.index("--query 'length(items)'")
        verify_targets = gateway_destroy.index(
            "Verified all Gateway Targets are removed"
        )
        delete_gateway = gateway_destroy.index(
            "delete-gateway \\\n\t\t\t--gateway-identifier"
        )

        self.assertLess(delete_target, count_targets)
        self.assertLess(count_targets, verify_targets)
        self.assertLess(verify_targets, delete_gateway)
        self.assertIn("ResourceNotFoundException", gateway_destroy)
        self.assertIn("Verified Gateway deletion", gateway_destroy)
        self.assertNotIn("|| true", gateway_destroy[delete_target:count_targets])


if __name__ == "__main__":
    unittest.main()
