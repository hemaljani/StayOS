import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .config import DeploymentConfig
from .deploy import (
    DeploymentFailed,
    DeploymentOrchestrator,
    default_log_path,
)


class AwsReader:
    def __init__(
        self,
        config: DeploymentConfig,
        profile: Optional[str] = None,
    ) -> None:
        self.config = config
        self.profile = config.profile if profile is None else profile

    def _command(self, *args: str) -> List[str]:
        command = ["aws"]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(["--region", self.config.region])
        command.extend(args)
        return command

    def read(self, *args: str) -> str:
        completed = subprocess.run(
            self._command(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "AWS command failed")
        return completed.stdout.strip()

    def account_id(self) -> str:
        return self.read(
            "sts",
            "get-caller-identity",
            "--query",
            "Account",
            "--output",
            "text",
        )

    def stack_status(self, stack_name: str) -> str:
        completed = subprocess.run(
            self._command(
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack_name,
                "--query",
                "Stacks[0].StackStatus",
                "--output",
                "text",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        if completed.returncode != 0:
            return "NOT_DEPLOYED"
        return completed.stdout.strip() or "UNKNOWN"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stayos-deploy")
    subparsers = parser.add_subparsers(dest="command")
    plan = subparsers.add_parser(
        "plan",
        help="inspect deployment configuration and AWS state without changes",
    )
    deploy = subparsers.add_parser(
        "deploy",
        help="run the eight-stage StayOS deployment",
    )
    for command in (plan, deploy):
        command.add_argument("--environment")
        command.add_argument("--profile")
        command.add_argument("--cloudformation-profile")
        command.add_argument("--region")
        command.add_argument("--expected-account-id")
        command.add_argument("--lumi-stack-prefix")
        command.add_argument("--pulse-stack-prefix")
        command.add_argument("--data-stack-prefix")
    plan.add_argument("--offline", action="store_true")
    plan.add_argument("--json", action="store_true")
    deploy.add_argument("--log-dir", default="logs")
    return parser


def create_plan(
    config: DeploymentConfig,
    reader: Optional[AwsReader],
    cloudformation_reader: Optional[AwsReader] = None,
) -> Dict[str, object]:
    account_id = "not-resolved"
    cloudformation_account_id = "not-resolved"
    stack_statuses = {
        config.lumi_stack: "NOT_INSPECTED",
        config.pulse_stack: "NOT_INSPECTED",
        config.data_stack: "NOT_INSPECTED",
    }
    required_tools = ["aws", "python3", "node", "npm", "zip"]
    tooling = {
        name: shutil.which(name) or "MISSING" for name in required_tools
    }
    prerequisites = [
        "{} executable is missing".format(name)
        for name, location in tooling.items()
        if location == "MISSING"
    ]

    if reader is not None:
        account_id = reader.account_id()
        cfn_reader = cloudformation_reader or reader
        cloudformation_account_id = cfn_reader.account_id()
        if (
            config.expected_account_id
            and account_id != config.expected_account_id
        ):
            raise RuntimeError(
                "AWS account mismatch: expected {}, resolved {}".format(
                    config.expected_account_id, account_id
                )
            )
        if cloudformation_account_id != account_id:
            raise RuntimeError(
                "CloudFormation account mismatch: target {}, resolved {}".format(
                    account_id, cloudformation_account_id
                )
            )
        stack_statuses = {
            name: reader.stack_status(name) for name in stack_statuses
        }
    else:
        prerequisites.append("AWS account and stack state were not inspected")

    return {
        "changesPlanned": False,
        "environment": config.environment,
        "profile": config.profile or "<default credential chain>",
        "cloudformationProfile": (
            config.cloudformation_profile or "<default credential chain>"
        ),
        "region": config.region,
        "accountId": account_id,
        "cloudformationAccountId": cloudformation_account_id,
        "expectedAccountId": config.expected_account_id or "<not set>",
        "stacks": stack_statuses,
        "tooling": tooling,
        "stages": [
            "LUMI infrastructure, agents, and frontend",
            "PULSE infrastructure, backend package, Lambda refresh, and seeds",
            "PULSE tools on the shared Gateway",
            "Triage runtime",
            "Forecast runtime and final PULSE runtime wiring",
            "PULSE frontend",
            "StayOS shell",
            "Data Orchestrator",
        ],
        "prerequisites": prerequisites,
    }


def print_plan(plan: Dict[str, object]) -> None:
    print("StayOS deployment plan (read-only)")
    print("  Environment: {}".format(plan["environment"]))
    print("  Profile:     {}".format(plan["profile"]))
    print("  CFN profile: {}".format(plan["cloudformationProfile"]))
    print("  Region:      {}".format(plan["region"]))
    print("  Account:     {}".format(plan["accountId"]))
    print("  CFN account: {}".format(plan["cloudformationAccountId"]))
    print("  Expected:    {}".format(plan["expectedAccountId"]))
    print("")
    print("Required tools:")
    for name, location in plan["tooling"].items():
        print("  {:12} {}".format(name, location))
    print("")
    print("Observed stacks:")
    for name, status in plan["stacks"].items():
        print("  {:36} {}".format(name, status))
    print("")
    print("Deployment stages:")
    for number, stage in enumerate(plan["stages"], 1):
        print("  {}. {}".format(number, stage))
    if plan["prerequisites"]:
        print("")
        print("Notes:")
        for prerequisite in plan["prerequisites"]:
            print("  - {}".format(prerequisite))
    print("")
    print("No AWS resources were changed.")


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"plan", "deploy"}:
        parser.print_help()
        raise SystemExit(2)

    try:
        config = DeploymentConfig.load(
            environment=args.environment,
            profile=args.profile,
            cloudformation_profile=args.cloudformation_profile,
            region=args.region,
            expected_account_id=args.expected_account_id,
            lumi_stack_prefix=args.lumi_stack_prefix,
            pulse_stack_prefix=args.pulse_stack_prefix,
            data_stack_prefix=args.data_stack_prefix,
        )
        if args.command == "deploy":
            root = Path.cwd()
            default_path = default_log_path(root)
            log_path = root / args.log_dir / default_path.name
            orchestrator = DeploymentOrchestrator(
                config,
                app_password=os.environ.get("APP_PASSWORD", ""),
                root=root,
                log_path=log_path,
            )
            orchestrator.deploy()
            return
        if args.offline:
            plan = create_plan(config, None)
        else:
            plan = create_plan(
                config,
                AwsReader(config),
                AwsReader(config, profile=config.cloudformation_profile),
            )
    except DeploymentFailed as error:
        raise SystemExit(error.returncode)
    except (RuntimeError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        raise SystemExit(1)

    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print_plan(plan)
