import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
WARNING = re.compile(r"\bwarning\b", re.IGNORECASE)
ERROR = re.compile(
    r"\b(error|failed|failure|denied|exception|invalid|timed out)\b",
    re.IGNORECASE,
)


def _enabled(value: str) -> bool:
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def strip_color(value: str) -> str:
    return ANSI_ESCAPE.sub("", value)


def redact_password(value: str, password: str) -> str:
    """Redact both literal and JSON-escaped password values from diagnostics."""
    if password:
        for form in sorted(
            {password, json.dumps(password)[1:-1]}, key=len, reverse=True
        ):
            value = value.replace(form, "<redacted>")
    return value


def format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, remaining = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining}s"
    if minutes:
        return f"{minutes}m {remaining}s"
    return f"{remaining}s"


@dataclass(frozen=True)
class OutputSettings:
    verbose: bool
    no_color: bool
    ci: bool
    interactive: bool

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
        stream: TextIO | None = None,
    ) -> "OutputSettings":
        values = os.environ if environ is None else environ
        output = sys.stdout if stream is None else stream
        ci = _enabled(values.get("CI", ""))
        is_tty = bool(getattr(output, "isatty", lambda: False)())
        return cls(
            verbose=values.get("VERBOSE", "") == "1",
            no_color=_enabled(values.get("NO_COLOR", "")) or not is_tty,
            ci=ci,
            interactive=is_tty and not ci,
        )


class Console:
    def __init__(
        self,
        settings: OutputSettings,
        stream: TextIO | None = None,
    ) -> None:
        self.settings = settings
        self.stream = sys.stdout if stream is None else stream

    def _paint(self, text: str, code: str) -> str:
        if self.settings.no_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def write(self, text: str = "") -> None:
        if self.settings.no_color:
            text = strip_color(text)
        print(text, file=self.stream, flush=True)

    def stage(self, number: int, title: str) -> None:
        self.write("")
        self.write(self._paint(f"[{number}/8] {title}", "1;36"))

    def result(
        self,
        message: str,
        seconds: float,
        status: str = "success",
    ) -> None:
        symbols = {"success": "✓", "warning": "!", "failure": "✗"}
        colors = {"success": "32", "warning": "33", "failure": "31"}
        symbol = self._paint(symbols[status], colors[status])
        self.write(f"  {symbol} {message} ({format_duration(seconds)})")

    def detail(self, label: str, value: str) -> None:
        self.write(f"    {label}: {value}")

    def progress(self, message: str) -> None:
        self.write(f"  … {message}")

    def command_output(self, line: str) -> None:
        self.write(line.rstrip("\n"))


class CommandFailed(RuntimeError):
    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        summary: str,
    ) -> None:
        super().__init__(summary)
        self.command = list(command)
        self.returncode = returncode
        self.summary = summary


@dataclass
class CommandResult:
    output: str
    warnings: list[str]


class CommandRunner:
    def __init__(
        self,
        console: Console,
        log_path: Path,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.console = console
        self.log_path = log_path
        self.environ = dict(os.environ if environ is None else environ)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")

    def _environment(
        self,
        extra: Mapping[str, str] | None,
    ) -> Mapping[str, str]:
        values = dict(self.environ)
        if extra:
            values.update(extra)
        values["AWS_PAGER"] = ""
        values["AWS_CLI_AUTO_PROMPT"] = "off"
        if self.console.settings.no_color:
            values["NO_COLOR"] = "1"
            values["CLICOLOR"] = "0"
            values["TERM"] = "dumb"
        return values

    def _log_header(self, command: Sequence[str], cwd: Path) -> None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[{timestamp}] cwd={cwd}\n")
            log.write(f"$ {shlex.join(command)}\n")

    @staticmethod
    def _error_summary(lines: deque[str], returncode: int) -> str:
        candidates = [
            strip_color(line).strip()
            for line in lines
            if ERROR.search(strip_color(line))
            and not strip_color(line).lstrip().startswith("make")
        ]
        if candidates:
            return candidates[-1]
        nonempty = [strip_color(line).strip() for line in lines if line.strip()]
        if nonempty:
            return nonempty[-1]
        return f"command exited with status {returncode}"

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        extra_env: Mapping[str, str] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> CommandResult:
        password = (extra_env or {}).get("APP_PASSWORD") or self.environ.get(
            "APP_PASSWORD", ""
        )
        self._log_header(
            [redact_password(argument, password) for argument in command], cwd
        )
        tail: deque[str] = deque(maxlen=80)
        warnings: list[str] = []
        captured: list[str] = []

        try:
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                env=self._environment(extra_env),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            message = redact_password(str(error), password)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"{message}\n")
            raise CommandFailed(command, 127, message) from error

        assert process.stdout is not None
        with process.stdout, self.log_path.open("a", encoding="utf-8") as log:
            for line in process.stdout:
                line = redact_password(line, password)
                log.write(line)
                log.flush()
                captured.append(line)
                tail.append(line)
                if WARNING.search(strip_color(line)):
                    warnings.append(strip_color(line).strip())
                if self.console.settings.verbose:
                    self.console.command_output(line)
                elif progress:
                    progress(strip_color(line).strip())

        returncode = process.wait()
        if returncode:
            raise CommandFailed(
                command,
                returncode,
                self._error_summary(tail, returncode),
            )
        return CommandResult("".join(captured).strip(), warnings)
