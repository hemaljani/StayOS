"""Regression tests for password preservation and real Makefile entry points."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

from stayos_deploy.config import DeploymentConfig
from stayos_deploy.output import CommandFailed
from stayos_deploy.password import (
    PasswordAction,
    PasswordError,
    read_stack,
    resolve_action,
    verify_accounts,
)

ACCOUNT = "123456789012"
STATE = {"status": "UPDATE_COMPLETE", "parameterKeys": ["AppPassword", "AccountId"]}

# This fake accepts only the calls expected by the policy / root orchestrator.
# Every AWS "write" is recorded locally; unexpected calls fail without a network.
FAKE_AWS = r"""
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
def argument(key):
    return args[args.index(key) + 1]
record = {"args": args, "hasPasswordEnv": bool(os.environ.get("APP_PASSWORD"))}
data = None
if "deploy" in args:
    assert argument("--parameter-overrides") == "file:///dev/stdin"
    data = json.load(sys.stdin)
    record["parameters"] = data
with open(os.environ["MOCK_CALLS"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
account = "123456789012"
if "get-caller-identity" in args:
    profile = argument("--profile")
    print(os.environ.get("MOCK_CFN_ACCOUNT", account)
          if profile == "fixture-cfn" else account)
elif "describe-stack-resources" in args:
    print("stayos-data-nested")
elif "describe-stacks" in args:
    query = argument("--query")
    if "parameterKeys" in query:
        mode = os.environ.get("MOCK_STATE", "existing")
        counter = Path(os.environ["MOCK_COUNTER"])
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        if count and os.environ.get("MOCK_AFTER_STATE"):
            mode = os.environ["MOCK_AFTER_STATE"]
        if mode in {"missing", "denied", "validation"}:
            message = {
                "missing": "An error occurred (ValidationError) when calling the "
                    "DescribeStacks operation: Stack with id stayos-us-east-1 "
                    "does not exist",
                "denied": "An error occurred (AccessDenied) when calling the "
                    "DescribeStacks operation: access denied",
                "validation": "An error occurred (ValidationError) when calling the "
                    "DescribeStacks operation: invalid request",
            }[mode]
            print(message, file=sys.stderr)
            sys.exit(19)
        elif mode == "malformed":
            print("not-json")
        elif mode == "empty":
            print("null")
        else:
            print(json.dumps({
                "status": "UPDATE_IN_PROGRESS" if mode == "busy" else "UPDATE_COMPLETE",
                "parameterKeys": ([] if mode == "no-password"
                                  else ["AppPassword", "AccountId"]),
            }))
    elif "StackStatus" in query:
        print("None" if os.environ.get("MOCK_LEGACY_STACK_ABSENT")
              and argument("--stack-name") == "stayos-dependencies-build"
              else "UPDATE_COMPLETE")
    else:
        values = {
            "UserPoolId": "pool-id", "UserPoolClientId": "client-id",
            "ToolLambdaArn": "arn:tool", "ReservationsStreamArn": "arn:reservations",
            "RoomsStreamArn": "arn:rooms", "GuestsStreamArn": "arn:guests",
            "RevenuesStreamArn": "arn:revenues",
            "WorkOrdersStreamArn": "arn:work-orders",
            "FrontendUrl": "https://stayos.example/",
            "ApiEndpoint": "https://api.example/",
            "RealtimeHttpEndpoint": "https://realtime.example/",
        }
        key = query.split("OutputKey=='")[1].split("'")[0]
        print(values[key])
elif "get-parameter" in args:
    name = argument("--name")
    values = {
        "/stayos/gateway/endpoint-url": "https://gateway.example/",
        "/stayos/voice/runtime-id": "voice-id", "/stayos/chat/runtime-id": "chat-id",
        "/pulse/triage/runtime-arn": "arn:triage",
        "/pulse/forecast/runtime-arn": "arn:forecast",
    }
    print(values[name])
elif "deploy" in args:
    password = next((p["ParameterValue"] for p in data
                     if p["ParameterKey"] == "AppPassword"), "")
    if os.environ.get("MOCK_DEPLOY_ERROR"):
        print("ERROR: rejected " + json.dumps(password), file=sys.stderr)
        sys.exit(23)
    if os.environ.get("MOCK_DEPLOY_WARNING"):
        print("WARNING: fixture deployment warning", file=sys.stderr)
    print("Successfully deployed fixture stack")
else:
    print("Unexpected fake AWS command: " + str(args), file=sys.stderr)
    sys.exit(99)
"""

FAKE_MAKE = r"""
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["MOCK_MAKES"], "a") as stream:
    stream.write(json.dumps({
        "args": args,
        "hasPasswordEnv": bool(os.environ.get("APP_PASSWORD")),
        "change": os.environ.get("CHANGE_APP_PASSWORD"),
    }) + "\n")
if "-C" not in args:
    print("Fixture root make completed")
    sys.exit(0)
directory = args[args.index("-C") + 1]
if directory == "lumi":
    # Exercise the real infrastructure recipe without artifact uploads/builds.
    target = "password-preflight" if args[2] == "password-preflight" else "deploy-infra"
    command = [os.environ["REAL_MAKE"], "-C", os.environ["MOCK_ROOT"] + "/lumi",
               "-o", "package-infra", target] + args[3:]
    sys.exit(subprocess.call(command))
print("Fixture make completed")
"""


class PasswordPolicyTest(unittest.TestCase):
    def test_creation_preservation_and_explicit_replacement(self):
        cases = [
            (None, "fixture", "0", PasswordAction.CREATE),
            (STATE, "", "0", PasswordAction.PRESERVE),
            (STATE, "fixture", "1", PasswordAction.REPLACE),
        ]
        for state, password, flag, expected in cases:
            with self.subTest(action=expected):
                self.assertEqual(expected, resolve_action(state, password, flag))

    def test_unsafe_or_ambiguous_intent_is_rejected(self):
        cases = [
            (None, "", "0"),
            (None, "   ", "0"),
            (None, "fixture", "1"),
            (STATE, "fixture", "0"),
            (STATE, "", "1"),
            (STATE, " ", "1"),
            (STATE, "", ""),
            (STATE, "", "true"),
            (STATE, "", "2"),
            ({"status": "UPDATE_COMPLETE", "parameterKeys": []}, "", "0"),
        ]
        for state, password, flag in cases:
            with self.subTest(state=state, flag=flag):
                with self.assertRaises(PasswordError):
                    resolve_action(state, password, flag)

    def test_only_allowed_completed_states_can_update(self):
        for status in (
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        ):
            self.assertEqual(
                PasswordAction.PRESERVE,
                resolve_action({**STATE, "status": status}, ""),
            )
        for status in (
            "CREATE_IN_PROGRESS",
            "UPDATE_IN_PROGRESS",
            "ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_FAILED",
            "DELETE_COMPLETE",
            "IMPORT_COMPLETE",
            "UNKNOWN",
        ):
            with self.subTest(status=status), self.assertRaises(PasswordError):
                resolve_action({**STATE, "status": status}, "")

    def test_inspection_uses_cfn_profile_and_projects_only_parameter_keys(self):
        config = DeploymentConfig.load(
            profile="fixture-primary", cloudformation_profile="fixture-cfn", environ={}
        )
        commands = []

        def read(command):
            commands.append(command)
            return json.dumps(STATE)

        self.assertEqual(STATE, read_stack(config, read))
        command = commands[0]
        self.assertEqual("fixture-cfn", command[command.index("--profile") + 1])
        self.assertEqual("us-east-1", command[command.index("--region") + 1])
        query = command[command.index("--query") + 1]
        self.assertNotIn("ParameterValue", query)
        self.assertNotIn("Outputs", query)

    def test_only_exact_missing_stack_error_counts_as_creation(self):
        config = DeploymentConfig.load(environ={})
        for message, missing in [
            (
                "An error occurred (ValidationError) when calling the DescribeStacks "
                "operation: Stack with id stayos-us-east-1 does not exist",
                True,
            ),
            ("AccessDenied: Stack with id stayos-us-east-1 does not exist", False),
            ("(ValidationError): Stack with id other-stack does not exist", False),
            ("(ValidationError): invalid request", False),
            ("Network connection failed", False),
        ]:

            def read(command):
                raise CommandFailed(command, 17, message)

            with self.subTest(message=message):
                if missing:
                    self.assertIsNone(read_stack(config, read))
                else:
                    with self.assertRaises(CommandFailed):
                        read_stack(config, read)

    def test_malformed_inspection_fails_closed(self):
        for response in (
            "not-json",
            "null",
            "[]",
            "{}",
            '{"status":"UPDATE_COMPLETE"}',
            '{"status":"UPDATE_COMPLETE","parameterKeys":[42]}',
        ):
            with self.subTest(response=response), self.assertRaises(PasswordError):
                read_stack(DeploymentConfig.load(environ={}), lambda command: response)

    def test_account_mismatch_fails_before_stack_inspection(self):
        config = DeploymentConfig.load(expected_account_id=ACCOUNT, environ={})
        with self.assertRaisesRegex(PasswordError, "AWS account mismatch"):
            verify_accounts(config, lambda command: "999999999999")
        identities = iter((ACCOUNT, "999999999999"))
        with self.assertRaisesRegex(PasswordError, "CloudFormation account mismatch"):
            verify_accounts(config, lambda command: next(identities))
        with self.assertRaises(PasswordError):
            verify_accounts(config, lambda command: "None")


class PasswordEntryPointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.real_make = shutil.which("make")
        self.assertIsNotNone(self.real_make)
        for name, content in (("aws", FAKE_AWS), ("make", FAKE_MAKE)):
            executable = self.directory / name
            executable.write_text(f"#!{sys.executable}\n" + content)
            executable.chmod(0o700)
        shutil.copyfile(ROOT / "Makefile", self.directory / "Makefile")
        (self.directory / "tools").symlink_to(TOOLS_DIR, target_is_directory=True)
        self.environment = dict(os.environ)
        for name in (
            "APP_PASSWORD",
            "CHANGE_APP_PASSWORD",
            "MAKEFLAGS",
            "MAKEOVERRIDES",
            "MFLAGS",
            "MOCK_AFTER_STATE",
            "MOCK_DEPLOY_ERROR",
        ):
            self.environment.pop(name, None)
        self.environment.update(
            {
                "PATH": f"{self.directory}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(TOOLS_DIR),
                "MOCK_CALLS": str(self.directory / "calls.jsonl"),
                "MOCK_MAKES": str(self.directory / "makes.jsonl"),
                "MOCK_COUNTER": str(self.directory / "counter"),
                "MOCK_ROOT": str(ROOT),
                "REAL_MAKE": self.real_make,
                "NO_COLOR": "1",
                "VERBOSE": "1",
            }
        )
        self.context = [
            "PROFILE=fixture-primary",
            "CLOUDFORMATION_PROFILE=fixture-cfn",
            "REGION=us-east-1",
            f"EXPECTED_ACCOUNT_ID={ACCOUNT}",
        ]

    def records(self, name="MOCK_CALLS"):
        path = Path(self.environment[name])
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def run_entry(self, target, *, direct=False, extra=()):
        command = [self.real_make]
        if direct:
            command.extend(["-C", str(ROOT / "lumi")])
        else:
            command.append(f"MAKE={self.directory / 'make'}")
        if target in {"deploy-infra", "lumi-deploy-infra"}:
            command.extend(["-o", "package-infra"])
        command.extend([target, *self.context, *extra])
        return subprocess.run(
            command,
            cwd=self.directory,
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )

    def assert_no_writes(self):
        self.assertTrue(
            all(
                any(
                    name in record["args"]
                    for name in ("get-caller-identity", "describe-stacks")
                )
                for record in self.records()
            )
        )

    def test_root_redeploy_without_password_completes_all_stages(self):
        result = self.run_entry("deploy-all")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("APP_PASSWORD step skipped", result.stdout)
        self.assertIn("Verification: PASSED", result.stdout)
        deploy = [r for r in self.records() if "deploy" in r["args"]]
        self.assertEqual(1, len(deploy))
        keys = [p["ParameterKey"] for p in deploy[0]["parameters"]]
        self.assertEqual(
            ["StackPrefix", "AccountId", "BedrockModelId", "DependenciesLayerS3Key"],
            keys,
        )
        self.assertEqual("fixture-cfn", deploy[0]["args"][1])
        self.assertIn("CAPABILITY_AUTO_EXPAND", deploy[0]["args"])
        self.assertIn("--no-fail-on-empty-changeset", deploy[0]["args"])
        self.assertEqual(9, len(self.records("MOCK_MAKES")))
        self.assertTrue(all(not r["hasPasswordEnv"] for r in self.records()))

    def test_successful_deploy_keeps_aws_warnings_in_root_summary_and_log(self):
        self.environment["MOCK_DEPLOY_WARNING"] = "1"
        result = self.run_entry("deploy-all")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("PASSED WITH WARNINGS", result.stdout)
        logs = list((self.directory / "logs").glob("*.log"))
        self.assertEqual(1, len(logs))
        self.assertIn("WARNING: fixture deployment warning", logs[0].read_text())

    def test_direct_and_root_lumi_infra_preserve_password(self):
        for target, direct in (("deploy-infra", True), ("lumi-deploy-infra", False)):
            with self.subTest(target=target):
                result = self.run_entry(target, direct=direct)
                self.assertEqual(0, result.returncode, result.stdout)
        for record in self.records():
            if "deploy" in record["args"]:
                self.assertNotIn(
                    "AppPassword", [p["ParameterKey"] for p in record["parameters"]]
                )

    def test_creation_and_replacement_use_stdin_without_shell_interpretation(self):
        password = 'fixture "$value=quoted" $(touch no-such-file) `literal` password'
        for mode, flag in (("missing", "0"), ("existing", "1")):
            with self.subTest(mode=mode):
                self.environment.update(
                    {
                        "MOCK_STATE": mode,
                        "APP_PASSWORD": password,
                        "CHANGE_APP_PASSWORD": flag,
                    }
                )
                result = self.run_entry("deploy-infra", direct=True)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertNotIn(password, result.stdout)
        for record in self.records():
            self.assertFalse(record["hasPasswordEnv"])
            self.assertNotIn(password, " ".join(record["args"]))
            if "deploy" in record["args"]:
                parameters = {
                    p["ParameterKey"]: p["ParameterValue"] for p in record["parameters"]
                }
                self.assertEqual(password, parameters["AppPassword"])
        self.assertFalse((ROOT / "lumi" / "no-such-file").exists())

    def test_root_replacement_does_not_leak_to_other_feature_environments_or_logs(self):
        password = 'fixture "$value=quoted" password'
        self.environment.update({"APP_PASSWORD": password, "CHANGE_APP_PASSWORD": "1"})
        result = self.run_entry("deploy-all")
        self.assertEqual(0, result.returncode, result.stdout)
        for record in self.records("MOCK_MAKES"):
            is_lumi = record["args"][1] == "lumi"
            self.assertEqual(is_lumi, record["hasPasswordEnv"])
        for output in (
            result.stdout,
            *(p.read_text() for p in (self.directory / "logs").glob("*.log")),
        ):
            self.assertNotIn(password, output)
            self.assertNotIn("$value", output)

    def test_rejected_inputs_block_root_and_direct_workflows_before_writes(self):
        cases = (
            ("missing", "", "0"),
            ("existing", "fixture", "0"),
            ("existing", "", "1"),
            ("existing", "", "invalid"),
            ("denied", "", "0"),
            ("validation", "", "0"),
            ("malformed", "", "0"),
            ("empty", "", "0"),
            ("no-password", "", "0"),
            ("busy", "", "0"),
        )
        for target, direct in (
            ("deploy-all", False),
            ("deploy", True),
            ("lumi-deploy", False),
            ("deploy-infra", True),
            ("lumi-deploy-infra", False),
            ("_deploy-all-legacy", False),
        ):
            for mode, password, flag in cases:
                with self.subTest(target=target, mode=mode, flag=flag):
                    self.environment.update(
                        {
                            "MOCK_STATE": mode,
                            "APP_PASSWORD": password,
                            "CHANGE_APP_PASSWORD": flag,
                        }
                    )
                    result = self.run_entry(target, direct=direct)
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assert_no_writes()

    def test_parallel_deploy_still_validates_before_writes(self):
        self.environment["MOCK_STATE"] = "missing"
        result = self.run_entry("deploy", direct=True, extra=("-j8",))
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assert_no_writes()

    def test_standalone_cleanup_does_not_require_a_deployment_password(self):
        self.environment.update(
            {
                "MOCK_STATE": "busy",
                "MOCK_LEGACY_STACK_ABSENT": "1",
            }
        )
        result = self.run_entry("remove-legacy-layer-build-stack", direct=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assert_no_writes()
        self.assertFalse(
            any(
                "parameterKeys" in " ".join(record["args"]) for record in self.records()
            )
        )

    def test_account_mismatch_cannot_be_bypassed_with_make_account_overrides(self):
        self.environment["MOCK_CFN_ACCOUNT"] = "999999999999"
        result = self.run_entry(
            "deploy-infra",
            direct=True,
            extra=(f"ACCOUNT_ID={ACCOUNT}", f"CFN_ACCOUNT_ID={ACCOUNT}"),
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("CloudFormation account mismatch", result.stdout)
        self.assert_no_writes()

    def test_account_overrides_must_match_credentials_even_without_expected_account(
        self,
    ):
        result = self.run_entry(
            "deploy-infra",
            direct=True,
            extra=(
                "EXPECTED_ACCOUNT_ID=",
                "ACCOUNT_ID=999999999999",
                "CFN_ACCOUNT_ID=999999999999",
            ),
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("AWS account mismatch", result.stdout)
        self.assert_no_writes()

    def test_disappearing_stack_stops_before_deploy(self):
        self.environment["MOCK_AFTER_STATE"] = "missing"
        result = self.run_entry("deploy-infra", direct=True)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assert_no_writes()

    def test_aws_errors_redact_password_and_keep_exit_code(self):
        password = 'fixture "quote" $value=password'
        self.environment.update(
            {
                "APP_PASSWORD": password,
                "CHANGE_APP_PASSWORD": "1",
                "MOCK_DEPLOY_ERROR": "1",
            }
        )
        result = self.run_entry("deploy-all")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("<redacted>", result.stdout)
        for output in (
            result.stdout,
            *(p.read_text() for p in (self.directory / "logs").glob("*.log")),
        ):
            self.assertNotIn(password, output)
            self.assertNotIn("$value", output)
        helper = subprocess.run(
            [
                sys.executable,
                "-m",
                "stayos_deploy.password",
                "deploy",
                "--profile",
                "fixture-primary",
                "--cloudformation-profile",
                "fixture-cfn",
                "--region",
                "us-east-1",
                "--expected-account-id",
                ACCOUNT,
                "--template-file",
                "fixture.yaml",
            ],
            cwd=self.directory,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(23, helper.returncode, helper.stderr)
        self.assertIn("<redacted>", helper.stderr)
        self.assertNotIn(password, helper.stderr)

    def test_dry_run_never_prints_command_line_password(self):
        password = "fixture-dollar$$=password"
        result = self.run_entry(
            "deploy-infra", direct=True, extra=("-n", f"APP_PASSWORD={password}")
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertNotIn(password, result.stdout)
        self.assertNotIn("AppPassword=", result.stdout)


if __name__ == "__main__":
    unittest.main()
