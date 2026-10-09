import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import DeploymentConfig
from .output import (
    CommandFailed,
    CommandResult,
    CommandRunner,
    Console,
    OutputSettings,
    format_duration,
)
from .password import (
    PasswordAction,
    PasswordError,
    read_stack,
    resolve_action,
    verify_accounts,
)


@dataclass(frozen=True)
class Stage:
    number: int
    title: str
    success: str
    recovery: str
    action: Callable[[], None]


class DeploymentFailed(RuntimeError):
    def __init__(self, returncode: int) -> None:
        super().__init__("deployment failed")
        self.returncode = returncode


class MakeProgress:
    def __init__(
        self,
        console: Console,
        directory: str,
        target: str,
    ) -> None:
        self.console = console
        self.workflow = (directory, target)
        self.emitted: set[str] = set()
        self.lambda_count = 0

    def _emit(self, message: str) -> None:
        if message not in self.emitted:
            self.emitted.add(message)
            self.console.progress(message)

    def _lumi(self, line: str) -> None:
        if "verifying agentcore" in line or "verifying bedrock" in line:
            self._emit("Checking LUMI deployment prerequisites")
        elif "[layer] packaging dependency" in line:
            self._emit("Preparing dependency-layer build input")
        elif "[layer] computestack will build or reuse" in line:
            self._emit("Building or reusing the x86_64 layer in ComputeStack")
        elif "deploying infrastructure" in line:
            self._emit("Updating LUMI CloudFormation infrastructure")
        elif line.startswith("updating "):
            self.lambda_count += 1
            if self.lambda_count in {1, 4}:
                self._emit(f"Refreshing LUMI Lambda functions ({self.lambda_count}/4)")
        elif "[gateway] fetching" in line:
            self._emit("Configuring the shared AgentCore Gateway")
        elif "[gateway] reading tool schema" in line:
            self._emit("Synchronizing shared Gateway tools")
        elif "[gateway] gateway target is ready" in line:
            self._emit("Shared Gateway and tools ready")
        elif "[voice] starting codebuild" in line:
            self._emit("Building the voice runtime image on CodeBuild")
        elif "[voice] waiting for runtime" in line:
            self._emit("Waiting for the voice runtime")
        elif "[voice] runtime is ready" in line:
            self._emit("Voice runtime ready")
        elif "[chat] starting codebuild" in line:
            self._emit("Building the chat runtime image on CodeBuild")
        elif "[chat] waiting for runtime" in line:
            self._emit("Waiting for the chat runtime")
        elif "[chat] runtime is ready" in line:
            self._emit("Chat runtime ready")
        elif "building frontend" in line:
            self._emit("Building the LUMI frontend")
        elif "deploying frontend to s3" in line:
            self._emit("Publishing the LUMI frontend")
        elif "generating ai briefs" in line:
            self._emit("Requesting asynchronous AI briefs")

    def _pulse_initial(self, line: str) -> None:
        if line.startswith("deploying pulse-"):
            self._emit("Updating PULSE CloudFormation infrastructure")
        elif "[backend] starting codebuild" in line:
            self._emit("Building the PULSE Lambda package on CodeBuild")
        elif "[backend] build succeeded" in line:
            self._emit("PULSE Lambda package ready")
        elif line.startswith("updating "):
            self.lambda_count += 1
            if self.lambda_count == 1:
                self._emit("Refreshing PULSE Lambda functions")
        elif "lambda code refresh complete" in line:
            self._emit("PULSE Lambda functions refreshed")
        elif "[seed] invoking" in line:
            self._emit("Running PULSE data seeds")

    def _runtime(self, line: str, tag: str, title: str) -> None:
        if f"[{tag}] starting shared codebuild" in line:
            self._emit(f"Building the {title} runtime image on CodeBuild")
        elif (
            f"[{tag}] resolving runtime configuration" in line
            or f"[{tag}] creating new agentcore runtime" in line
            or f"[{tag}] updating existing runtime" in line
        ):
            self._emit(f"Configuring the {title} AgentCore runtime")
        elif f"[{tag}] runtime arn stored" in line:
            self._emit(f"{title.capitalize()} runtime configuration stored")

    def _frontend(self, line: str, title: str) -> None:
        if "building" in line and "frontend" in line:
            self._emit(f"Building the {title} frontend")
        elif "deploying" in line and ("s3" in line or "bucket" in line):
            self._emit(f"Publishing the {title} frontend")

    def _data(self, line: str) -> None:
        if "packaging orchestrator lambda" in line:
            self._emit("Packaging the Data Orchestrator")
        elif "deploying orchestrator stack" in line:
            self._emit("Updating Data Orchestrator infrastructure")
        elif line.startswith("updating "):
            self.lambda_count += 1
            if self.lambda_count == 1:
                self._emit("Refreshing Data Orchestrator Lambda functions")
        elif "priming today's data" in line:
            self._emit("Priming today's estate data")
        elif "verified roll-forward" in line:
            self._emit("Verifying estate roll-forward and PULSE baselines")

    def __call__(self, raw_line: str) -> None:
        line = raw_line.strip().lower()
        if not line:
            return
        if self.workflow == ("lumi", "deploy"):
            self._lumi(line)
        elif self.workflow == ("pulse", "deploy-initial"):
            self._pulse_initial(line)
        elif self.workflow == ("pulse", "gateway-deploy"):
            if "[gateway] reading shared gateway" in line:
                self._emit("Synchronizing PULSE tools with the shared Gateway")
        elif self.workflow == ("pulse", "triage-deploy"):
            self._runtime(line, "triage", "triage")
        elif self.workflow == ("pulse", "forecast-deploy"):
            self._runtime(line, "forecast", "forecast")
        elif self.workflow == ("pulse", "deploy-runtime-wiring"):
            if line.startswith("updating pulse-"):
                self._emit("Wiring PULSE runtime references in CloudFormation")
        elif self.workflow == ("pulse", "deploy-frontend"):
            self._frontend(line, "PULSE")
        elif self.workflow == ("stayos-shell", "deploy"):
            self._frontend(line, "StayOS shell")
        elif self.workflow == ("shared/data-orchestrator", "deploy"):
            self._data(line)


class DeploymentOrchestrator:
    def __init__(
        self,
        config: DeploymentConfig,
        *,
        app_password: str,
        root: Path,
        log_path: Path,
        environ: Mapping[str, str] | None = None,
        console: Console | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.app_password = app_password
        self.root = root
        self.log_path = log_path
        self.environ = dict(os.environ if environ is None else environ)
        self.change_app_password = self.environ.get("CHANGE_APP_PASSWORD", "0")
        self.password_action = PasswordAction.PRESERVE
        # Make command-line assignments may contain the password. Explicit
        # context arguments below replace inherited overrides for child makes.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
            self.environ.pop(name, None)
        self.environ["APP_PASSWORD"] = app_password
        settings = OutputSettings.load(self.environ)
        self.console = console or Console(settings)
        self.runner = runner or CommandRunner(
            self.console,
            self.log_path,
            self.environ,
        )
        self.account_id = ""
        self.values: dict[str, str] = {}
        self.stage_warnings: list[str] = []
        self.all_warnings: list[str] = []

    @property
    def relative_log_path(self) -> str:
        try:
            return str(self.log_path.relative_to(self.root))
        except ValueError:
            return str(self.log_path)

    def _aws(self, *arguments: str, cloudformation: bool = False) -> list[str]:
        profile = (
            self.config.cloudformation_profile
            if cloudformation
            else self.config.profile
        )
        command = ["aws"]
        if profile:
            command.extend(["--profile", profile])
        command.extend(["--region", self.config.region])
        command.extend(arguments)
        return command

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> CommandResult:
        result = self.runner.run(
            command,
            cwd=self.root if cwd is None else cwd,
            extra_env=(
                {"APP_PASSWORD": "", "CHANGE_APP_PASSWORD": "0"}
                if extra_env is None
                else extra_env
            ),
            progress=progress,
        )
        self.stage_warnings.extend(result.warnings)
        self.all_warnings.extend(result.warnings)
        return result

    def _capture(
        self,
        command: list[str],
    ) -> str:
        return self._run(command).output.strip()

    @staticmethod
    def _required(name: str, value: str) -> str:
        if not value or value == "None":
            message = f"required value {name} was not resolved"
            raise CommandFailed([], 1, message)
        return value

    def _stack_output(self, stack: str, key: str) -> str:
        value = self._capture(
            self._aws(
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                stack,
                "--query",
                f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
                "--output",
                "text",
            )
        )
        return self._required(key, value)

    def _ssm_value(self, name: str) -> str:
        value = self._capture(
            self._aws(
                "ssm",
                "get-parameter",
                "--name",
                name,
                "--query",
                "Parameter.Value",
                "--output",
                "text",
            )
        )
        return self._required(name, value)

    def _make_context(self) -> list[str]:
        return [
            f"PROFILE={self.config.profile}",
            f"CLOUDFORMATION_PROFILE={self.config.cloudformation_profile}",
            f"REGION={self.config.region}",
            f"ENVIRONMENT={self.config.environment}",
            f"EXPECTED_ACCOUNT_ID={self.config.expected_account_id}",
        ]

    def _make(
        self,
        directory: str,
        target: str,
        *,
        variables: Mapping[str, str] | None = None,
    ) -> None:
        command = ["make", "-C", directory, target]
        command.extend(self._make_context())
        command.append("ORCHESTRATED=1")
        if variables:
            command.extend(f"{name}={value}" for name, value in variables.items())
        self._run(
            command,
            extra_env={
                "APP_PASSWORD": self.app_password if directory == "lumi" else "",
                "CHANGE_APP_PASSWORD": (
                    self.change_app_password if directory == "lumi" else "0"
                ),
            },
            progress=MakeProgress(self.console, directory, target),
        )

    def _preflight(self) -> None:
        try:
            self.account_id = verify_accounts(self.config, self._capture)
            self.password_action = resolve_action(
                read_stack(self.config, self._capture),
                self.app_password,
                self.change_app_password,
            )
        except PasswordError as error:
            raise CommandFailed([], 2, str(error)) from error
        self.console.progress(self.password_action.message)

    def _deploy_lumi(self) -> None:
        self._make(
            "lumi",
            "deploy",
            variables={"STACK_PREFIX": self.config.lumi_stack_prefix},
        )

    def _capture_lumi_values(self) -> None:
        lumi_stack = self.config.lumi_stack
        self.values["USER_POOL_ID"] = self._stack_output(lumi_stack, "UserPoolId")
        self.values["USER_POOL_CLIENT_ID"] = self._stack_output(
            lumi_stack, "UserPoolClientId"
        )
        self.values["TOOL_LAMBDA_ARN"] = self._stack_output(lumi_stack, "ToolLambdaArn")
        self.values["USER_POOL_ARN"] = "arn:aws:cognito-idp:{}:{}:userpool/{}".format(
            self.config.region,
            self.account_id,
            self.values["USER_POOL_ID"],
        )
        self.values["GATEWAY_ENDPOINT_URL"] = self._ssm_value(
            f"/{self.config.lumi_stack_prefix}/gateway/endpoint-url"
        )
        data_stack = self._capture(
            self._aws(
                "cloudformation",
                "describe-stack-resources",
                "--stack-name",
                lumi_stack,
                "--logical-resource-id",
                "DataStack",
                "--query",
                "StackResources[0].PhysicalResourceId",
                "--output",
                "text",
            )
        )
        data_stack = self._required("DataStack", data_stack)
        for resource in [
            "Reservations",
            "Rooms",
            "Guests",
            "Revenues",
            "WorkOrders",
        ]:
            stream_name = f"{resource.upper()}_STREAM_ARN"
            stream_arn = self._stack_output(
                data_stack,
                f"{resource}StreamArn",
            )
            self.values[stream_name] = stream_arn
            table_arn = stream_arn.split("/stream/", 1)[0]
            self.values[f"{resource.upper()}_TABLE_ARN"] = table_arn
            if resource in {"Reservations", "Rooms", "Guests"}:
                self.values[f"{resource.upper()}_TABLE_NAME"] = table_arn.split(
                    ":table/", 1
                )[-1]

    def _pulse_variables(
        self,
        *,
        triage_arn: str = "",
        forecast_arn: str = "",
    ) -> dict[str, str]:
        names = [
            "USER_POOL_ID",
            "USER_POOL_ARN",
            "USER_POOL_CLIENT_ID",
            "GATEWAY_ENDPOINT_URL",
            "RESERVATIONS_STREAM_ARN",
            "ROOMS_STREAM_ARN",
            "GUESTS_STREAM_ARN",
            "REVENUES_STREAM_ARN",
            "WORKORDERS_STREAM_ARN",
            "RESERVATIONS_TABLE_ARN",
            "ROOMS_TABLE_ARN",
            "GUESTS_TABLE_ARN",
            "REVENUES_TABLE_ARN",
            "WORKORDERS_TABLE_ARN",
            "RESERVATIONS_TABLE_NAME",
            "ROOMS_TABLE_NAME",
            "GUESTS_TABLE_NAME",
        ]
        variables = {name: self.values[name] for name in names}
        variables["WORK_ORDERS_STREAM_ARN"] = variables.pop("WORKORDERS_STREAM_ARN")
        variables["WORK_ORDERS_TABLE_ARN"] = variables.pop("WORKORDERS_TABLE_ARN")
        variables.update(
            {
                "STACK_PREFIX": self.config.pulse_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
                "TRIAGE_RUNTIME_ARN": triage_arn,
                "FORECAST_RUNTIME_ARN": forecast_arn,
            }
        )
        return variables

    def _deploy_pulse_initial(self) -> None:
        self._capture_lumi_values()
        self._make(
            "pulse",
            "deploy-initial",
            variables=self._pulse_variables(),
        )

    def _deploy_gateway_tools(self) -> None:
        self._make(
            "pulse",
            "gateway-deploy",
            variables={
                "STACK_PREFIX": self.config.pulse_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
                "TOOL_LAMBDA_ARN": self.values["TOOL_LAMBDA_ARN"],
            },
        )

    def _deploy_triage(self) -> None:
        self._make(
            "pulse",
            "triage-deploy",
            variables={
                "STACK_PREFIX": self.config.pulse_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
            },
        )
        self.values["TRIAGE_RUNTIME_ARN"] = self._ssm_value(
            f"/{self.config.pulse_stack_prefix}/triage/runtime-arn"
        )

    def _deploy_forecast_and_wire(self) -> None:
        self._make(
            "pulse",
            "forecast-deploy",
            variables={
                "STACK_PREFIX": self.config.pulse_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
            },
        )
        self.values["FORECAST_RUNTIME_ARN"] = self._ssm_value(
            f"/{self.config.pulse_stack_prefix}/forecast/runtime-arn"
        )
        self._make(
            "pulse",
            "deploy-runtime-wiring",
            variables=self._pulse_variables(
                triage_arn=self.values["TRIAGE_RUNTIME_ARN"],
                forecast_arn=self.values["FORECAST_RUNTIME_ARN"],
            ),
        )

    def _deploy_pulse_frontend(self) -> None:
        self._make(
            "pulse",
            "deploy-frontend",
            variables={
                "STACK_PREFIX": self.config.pulse_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
                "USER_POOL_CLIENT_ID": self.values["USER_POOL_CLIENT_ID"],
                "COGNITO_REGION": self.config.region,
            },
        )

    def _deploy_shell(self) -> None:
        self._make(
            "stayos-shell",
            "deploy",
            variables={"STACK_PREFIX": self.config.lumi_stack_prefix},
        )

    def _deploy_data(self) -> None:
        self._make(
            "shared/data-orchestrator",
            "deploy",
            variables={
                "STACK_PREFIX": self.config.data_stack_prefix,
                "LUMI_STACK_PREFIX": self.config.lumi_stack_prefix,
                "PULSE_STACK_PREFIX": self.config.pulse_stack_prefix,
            },
        )

    def _stages(self) -> list[Stage]:
        context = "PROFILE={} REGION={}".format(
            self.config.profile or "<profile>",
            self.config.region,
        )
        password_hint = (
            " (supply APP_PASSWORD through the environment if the stack needs creation)"
            if self.password_action == PasswordAction.CREATE
            else ""
        )
        change_flag = (
            " CHANGE_APP_PASSWORD=1"
            if self.password_action == PasswordAction.REPLACE
            else ""
        )
        return [
            Stage(
                1,
                "LUMI",
                "Foundation, Gateway, runtimes, and frontend ready",
                f"make lumi-deploy {context}{change_flag}{password_hint}",
                self._deploy_lumi,
            ),
            Stage(
                2,
                "PULSE infrastructure",
                "Infrastructure, Lambda package, refresh, and seeds ready",
                f"make deploy-all {context}",
                self._deploy_pulse_initial,
            ),
            Stage(
                3,
                "Shared Gateway tools",
                "PULSE tools registered",
                f"make pulse-gateway-deploy TOOL_LAMBDA_ARN=<arn> {context}",
                self._deploy_gateway_tools,
            ),
            Stage(
                4,
                "Triage runtime",
                "Triage runtime ready",
                f"make pulse-triage-deploy {context}",
                self._deploy_triage,
            ),
            Stage(
                5,
                "Forecast runtime and PULSE wiring",
                "Forecast runtime ready; both runtime ARNs wired",
                f"make pulse-forecast-deploy {context}, then rerun deploy-all",
                self._deploy_forecast_and_wire,
            ),
            Stage(
                6,
                "PULSE frontend",
                "PWA published at /pulse",
                f"make pulse-deploy-frontend {context}",
                self._deploy_pulse_frontend,
            ),
            Stage(
                7,
                "StayOS shell",
                "Login and launcher published at /",
                f"make shell-deploy {context}",
                self._deploy_shell,
            ),
            Stage(
                8,
                "Data Orchestrator",
                "Roll-forward deployed and estate primed",
                f"make data-deploy {context}",
                self._deploy_data,
            ),
        ]

    def _run_stage(self, stage: Stage) -> None:
        self.console.stage(stage.number, stage.title)
        self.stage_warnings = []
        started = time.monotonic()
        try:
            stage.action()
        except CommandFailed as error:
            duration = time.monotonic() - started
            self.console.result(
                f"{stage.title} failed",
                duration,
                status="failure",
            )
            self.console.detail("Error", error.summary)
            self.console.detail("Recovery", stage.recovery)
            self.console.detail("Log", self.relative_log_path)
            raise DeploymentFailed(error.returncode) from error
        duration = time.monotonic() - started
        if self.stage_warnings:
            self.console.result(
                f"{stage.success} (warning)",
                duration,
                status="warning",
            )
            self.console.detail("Warning", self.stage_warnings[0])
            self.console.detail("Log", self.relative_log_path)
        else:
            self.console.result(stage.success, duration)

    def _stack_status(self, stack: str) -> str:
        return self._required(
            f"{stack} status",
            self._capture(
                self._aws(
                    "cloudformation",
                    "describe-stacks",
                    "--stack-name",
                    stack,
                    "--query",
                    "Stacks[0].StackStatus",
                    "--output",
                    "text",
                )
            ),
        )

    def _verify_and_summarize(self, duration: float) -> None:
        statuses = {
            stack: self._stack_status(stack)
            for stack in [
                self.config.lumi_stack,
                self.config.pulse_stack,
                self.config.data_stack,
            ]
        }
        for stack, status in statuses.items():
            if status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                raise CommandFailed(
                    [],
                    1,
                    f"stack {stack} is not healthy ({status})",
                )

        frontend_url = self._stack_output(
            self.config.lumi_stack,
            "FrontendUrl",
        ).rstrip("/")
        pulse_api = self._stack_output(
            self.config.pulse_stack,
            "ApiEndpoint",
        )
        realtime_url = self._stack_output(
            self.config.pulse_stack,
            "RealtimeHttpEndpoint",
        )
        voice_id = self._ssm_value(f"/{self.config.lumi_stack_prefix}/voice/runtime-id")
        chat_id = self._ssm_value(f"/{self.config.lumi_stack_prefix}/chat/runtime-id")
        triage_arn = self.values.get("TRIAGE_RUNTIME_ARN") or self._ssm_value(
            f"/{self.config.pulse_stack_prefix}/triage/runtime-arn"
        )
        forecast_arn = self.values.get("FORECAST_RUNTIME_ARN") or self._ssm_value(
            f"/{self.config.pulse_stack_prefix}/forecast/runtime-arn"
        )
        gateway_url = self.values.get("GATEWAY_ENDPOINT_URL") or self._ssm_value(
            f"/{self.config.lumi_stack_prefix}/gateway/endpoint-url"
        )

        self.console.write("")
        self.console.write("StayOS deployment summary")
        verification = "PASSED WITH WARNINGS" if self.all_warnings else "PASSED"
        self.console.detail("Verification", verification)
        self.console.detail("Duration", format_duration(duration))
        for stack, status in statuses.items():
            self.console.detail(stack, status)
        self.console.detail("Shell", frontend_url + "/")
        self.console.detail("LUMI", frontend_url + "/lumi/")
        self.console.detail("PULSE", frontend_url + "/pulse/")
        self.console.detail(
            "AI briefs",
            "generation requested asynchronously; estate roll-forward primed",
        )
        self.console.detail("Log", self.relative_log_path)

        if self.console.settings.verbose:
            self.console.detail("PULSE API", pulse_api)
            self.console.detail("Realtime", realtime_url)
            self.console.detail(
                "Voice runtime",
                f"arn:aws:bedrock-agentcore:{self.config.region}:{self.account_id}:runtime/{voice_id}",
            )
            self.console.detail(
                "Chat runtime",
                f"arn:aws:bedrock-agentcore:{self.config.region}:{self.account_id}:runtime/{chat_id}",
            )
            self.console.detail("Triage runtime", triage_arn)
            self.console.detail("Forecast runtime", forecast_arn)
            self.console.detail("Gateway", gateway_url)
            self.console.detail(
                "LUMI artifacts",
                f"s3://{self.config.lumi_stack_prefix}-deploy-{self.account_id}/functions/",
            )
            self.console.detail(
                "PULSE artifacts",
                f"s3://{self.config.pulse_stack_prefix}-deploy-{self.account_id}/functions/pulse-backend.zip",
            )

    def deploy(self) -> None:
        started = time.monotonic()
        try:
            self._preflight()
        except CommandFailed as error:
            self.console.result("Deployment preflight failed", 0, "failure")
            self.console.detail("Error", error.summary)
            self.console.detail("Log", self.relative_log_path)
            raise DeploymentFailed(error.returncode) from error

        self.console.write(
            f"StayOS deployment: {self.config.environment} / {self.config.region} / account {self.account_id}"
        )
        self.console.write(f"Full log: {self.relative_log_path}")

        for stage in self._stages():
            self._run_stage(stage)

        try:
            self._verify_and_summarize(time.monotonic() - started)
        except CommandFailed as error:
            self.console.write("")
            self.console.result(
                "Post-deployment verification failed",
                time.monotonic() - started,
                "failure",
            )
            self.console.detail("Error", error.summary)
            self.console.detail(
                "Recovery",
                "make verify-deployment PROFILE={} REGION={}".format(
                    self.config.profile or "<profile>",
                    self.config.region,
                ),
            )
            self.console.detail("Log", self.relative_log_path)
            raise DeploymentFailed(error.returncode) from error


def default_log_path(root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    return root / "logs" / f"deploy-{timestamp}.log"
