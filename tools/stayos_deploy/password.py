"""Validate deployment password intent and preserve it on existing stacks.

All deployment entry points use this policy before AWS writes. Only stack status
and parameter keys are inspected; the existing password is never retrieved.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from enum import Enum

from .config import DeploymentConfig
from .output import CommandFailed, redact_password

STACK_QUERY = "Stacks[0].{status:StackStatus,parameterKeys:Parameters[].ParameterKey}"
UPDATEABLE_STATUSES = {
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
}
Reader = Callable[[list[str]], str]


class PasswordError(ValueError):
    """An unsafe or ambiguous deployment must stop before AWS writes."""


class PasswordAction(Enum):
    CREATE = "create"
    PRESERVE = "preserve"
    REPLACE = "replace"

    @property
    def message(self) -> str:
        return {
            "create": "New stack: APP_PASSWORD supplied for creation.",
            "preserve": (
                "Existing stack: APP_PASSWORD step skipped; preserving AppPassword."
            ),
            "replace": "Existing stack: explicit AppPassword replacement requested.",
        }[self.value]


def aws_command(
    config: DeploymentConfig, *arguments: str, cloudformation: bool = False
) -> list[str]:
    profile = config.cloudformation_profile if cloudformation else config.profile
    command = ["aws"]
    if profile:
        command.extend(["--profile", profile])
    return command + ["--region", config.region, *arguments]


def verify_accounts(config: DeploymentConfig, read: Reader) -> str:
    """Resolve both identities rather than trusting Makefile account overrides."""
    arguments = ("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    account = read(aws_command(config, *arguments)).strip()
    cfn_account = read(aws_command(config, *arguments, cloudformation=True)).strip()
    if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"\d{12}", cfn_account):
        raise PasswordError("Unable to resolve both deployment account identities.")
    if config.expected_account_id and account != config.expected_account_id:
        raise PasswordError(
            f"AWS account mismatch: expected {config.expected_account_id}, "
            f"resolved {account}"
        )
    if cfn_account != account:
        raise PasswordError(
            f"CloudFormation account mismatch: target {account}, resolved {cfn_account}"
        )
    return account


def read_stack(config: DeploymentConfig, read: Reader) -> dict | None:
    """Only an explicit missing-stack ValidationError authorizes creation."""
    try:
        output = read(
            aws_command(
                config,
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                config.lumi_stack,
                "--query",
                STACK_QUERY,
                "--output",
                "json",
                cloudformation=True,
            )
        )
    except CommandFailed as error:
        missing = f"Stack with id {config.lumi_stack} does not exist"
        if "(ValidationError)" in error.summary and missing in error.summary:
            return None
        raise
    try:
        state = json.loads(output)
    except (ValueError, TypeError) as error:
        raise PasswordError(
            "Unable to inspect LUMI stack: invalid JSON response."
        ) from error
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("status"), str)
        or not isinstance(state.get("parameterKeys"), list)
        or not all(isinstance(key, str) for key in state["parameterKeys"])
    ):
        raise PasswordError(
            "Unable to inspect LUMI stack: missing status or parameter keys."
        )
    return state


def resolve_action(
    state: dict | None, password: str, change_password: str = "0"
) -> PasswordAction:
    """Require explicit replacement intent; normal updates omit the override."""
    if change_password not in {"0", "1"}:
        raise PasswordError("CHANGE_APP_PASSWORD must be 0 or 1.")
    supplied = bool(password.strip())
    if state is None:
        if change_password == "1":
            raise PasswordError(
                "CHANGE_APP_PASSWORD=1 requires an existing LUMI stack."
            )
        if not supplied:
            raise PasswordError(
                "APP_PASSWORD is required for stack creation; "
                "supply it through the environment and retry."
            )
        return PasswordAction.CREATE
    if state["status"] not in UPDATEABLE_STATUSES:
        raise PasswordError(
            f"LUMI stack is not ready for an update ({state['status']}); "
            "resolve the stack operation before retrying."
        )
    if "AppPassword" not in state["parameterKeys"]:
        raise PasswordError(
            "Existing LUMI stack has no AppPassword parameter; stopping."
        )
    if change_password == "1":
        if not supplied:
            raise PasswordError(
                "CHANGE_APP_PASSWORD=1 requires APP_PASSWORD in the environment."
            )
        return PasswordAction.REPLACE
    if password:
        raise PasswordError(
            "Existing-stack updates preserve AppPassword. Unset APP_PASSWORD to "
            "redeploy, or set CHANGE_APP_PASSWORD=1 to intentionally replace "
            "the parameter."
        )
    return PasswordAction.PRESERVE


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Do not forward a password or Make's command-line assignments to AWS CLI."""
    values = dict(environ)
    for name in ("APP_PASSWORD", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        values.pop(name, None)
    values["AWS_PAGER"] = ""
    values["AWS_CLI_AUTO_PROMPT"] = "off"
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "deploy"))
    parser.add_argument("--profile")
    parser.add_argument("--cloudformation-profile")
    parser.add_argument("--region")
    parser.add_argument("--expected-account-id")
    parser.add_argument("--stack-prefix")
    parser.add_argument("--template-file")
    parser.add_argument("--parameter", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    password = os.environ.get("APP_PASSWORD", "")
    change_password = os.environ.get("CHANGE_APP_PASSWORD", "0")
    environment = child_environment(os.environ)

    def invoke(command: list[str], input_text: str | None = None) -> str:
        # JSON travels through a pipe, never a secret-bearing argv or disk file.
        result = subprocess.run(
            command,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        stdout = redact_password(result.stdout, password)
        stderr = redact_password(result.stderr, password)
        if result.returncode:
            raise CommandFailed(
                command,
                result.returncode,
                "\n".join(part.strip() for part in (stdout, stderr) if part.strip()),
            )
        if stderr.strip():
            print(stderr.strip(), file=sys.stderr, flush=True)
        return stdout.strip()

    try:
        config = DeploymentConfig.load(
            profile=args.profile,
            cloudformation_profile=args.cloudformation_profile,
            region=args.region,
            expected_account_id=args.expected_account_id,
            lumi_stack_prefix=args.stack_prefix,
        )
        account = verify_accounts(config, invoke)
        action = resolve_action(read_stack(config, invoke), password, change_password)
        print(action.message, flush=True)
        if args.action == "check":
            return
        if not args.template_file:
            raise PasswordError("--template-file is required for deployment.")
        parameters = []
        for item in args.parameter:
            key, separator, value = item.partition("=")
            if not separator or not key or key == "AppPassword":
                raise PasswordError(
                    "Deployment parameters must be Key=Value; "
                    "AppPassword is managed by the password policy."
                )
            if key == "AccountId" and value != account:
                raise PasswordError(
                    "AccountId parameter does not match the deployment account."
                )
            parameters.append({"ParameterKey": key, "ParameterValue": value})
        if action != PasswordAction.PRESERVE:
            parameters.append(
                {"ParameterKey": "AppPassword", "ParameterValue": password}
            )
        # Reinspect after packaging / validation, immediately before the deploy.
        current_action = resolve_action(
            read_stack(config, invoke), password, change_password
        )
        if current_action != action:
            raise PasswordError(
                "LUMI stack changed since password validation; retry deployment."
            )
        output = invoke(
            aws_command(
                config,
                "cloudformation",
                "deploy",
                "--stack-name",
                config.lumi_stack,
                "--template-file",
                args.template_file,
                "--parameter-overrides",
                "file:///dev/stdin",
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "CAPABILITY_AUTO_EXPAND",
                "--no-fail-on-empty-changeset",
                cloudformation=True,
            ),
            json.dumps(parameters),
        )
        if output:
            print(output)
    except (PasswordError, CommandFailed, OSError, ValueError) as error:
        print(f"ERROR: {redact_password(str(error), password)}", file=sys.stderr)
        raise SystemExit(
            error.returncode if isinstance(error, CommandFailed) else 2
        ) from None


if __name__ == "__main__":
    main()
