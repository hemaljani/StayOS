import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from stayos_deploy.output import (
    CommandFailed,
    CommandRunner,
    Console,
    OutputSettings,
    format_duration,
)


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class OutputTest(unittest.TestCase):
    def test_output_modes_cover_verbose_ci_and_no_color(self):
        tty = TtyBuffer()
        default = OutputSettings.load({}, tty)
        self.assertTrue(default.interactive)
        self.assertFalse(default.verbose)
        self.assertFalse(default.no_color)

        ci = OutputSettings.load({"CI": "true"}, tty)
        self.assertTrue(ci.ci)
        self.assertFalse(ci.interactive)

        verbose = OutputSettings.load({"VERBOSE": "1"}, tty)
        self.assertTrue(verbose.verbose)

        plain = OutputSettings.load({"NO_COLOR": "1"}, tty)
        self.assertTrue(plain.no_color)

    def test_result_formats_success_warning_and_failure_without_color(self):
        stream = io.StringIO()
        console = Console(OutputSettings.load({"NO_COLOR": "1"}, stream), stream)

        console.result("complete", 3.2)
        console.result("review", 61, "warning")
        console.result("stopped", 0, "failure")

        output = stream.getvalue()
        self.assertIn("✓ complete (3s)", output)
        self.assertIn("! review (1m 1s)", output)
        self.assertIn("✗ stopped (0s)", output)
        self.assertNotIn("\033[", output)
        self.assertEqual("1h 1m 1s", format_duration(3661))

    def test_concise_hides_command_output_but_log_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            settings = OutputSettings.load({"NO_COLOR": "1"}, stream)
            console = Console(settings, stream)
            log_path = Path(directory) / "deploy.log"
            runner = CommandRunner(console, log_path, {})

            result = runner.run(
                [sys.executable, "-c", "print('raw diagnostic')"],
                cwd=Path(directory),
            )

            self.assertEqual("raw diagnostic", result.output)
            self.assertNotIn("raw diagnostic", stream.getvalue())
            self.assertIn("raw diagnostic", log_path.read_text())

    def test_concise_progress_reports_selected_lines_only(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            settings = OutputSettings.load({"NO_COLOR": "1"}, stream)
            console = Console(settings, stream)
            log_path = Path(directory) / "deploy.log"
            runner = CommandRunner(console, log_path, {})

            def progress(line):
                if line.startswith("STATUS:"):
                    console.progress(line.removeprefix("STATUS:").strip())

            runner.run(
                [
                    sys.executable,
                    "-c",
                    "print('raw diagnostic'); print('STATUS: build running')",
                ],
                cwd=Path(directory),
                progress=progress,
            )

            output = stream.getvalue()
            self.assertIn("… build running", output)
            self.assertNotIn("raw diagnostic", output)
            self.assertIn("raw diagnostic", log_path.read_text())

    def test_verbose_streams_command_output(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = TtyBuffer()
            settings = OutputSettings.load(
                {"VERBOSE": "1", "NO_COLOR": "1"},
                stream,
            )
            console = Console(settings, stream)
            runner = CommandRunner(
                console,
                Path(directory) / "deploy.log",
                {},
            )

            progress_lines = []
            runner.run(
                [sys.executable, "-c", "print('visible diagnostic')"],
                cwd=Path(directory),
                progress=progress_lines.append,
            )

            self.assertIn("visible diagnostic", stream.getvalue())
            self.assertEqual([], progress_lines)

    def test_warning_is_reported_and_failure_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            settings = OutputSettings.load({"NO_COLOR": "1"}, stream)
            runner = CommandRunner(
                Console(settings, stream),
                Path(directory) / "deploy.log",
                {},
            )

            warning = runner.run(
                [sys.executable, "-c", "print('WARNING: retry later')"],
                cwd=Path(directory),
            )
            self.assertEqual(["WARNING: retry later"], warning.warnings)

            with self.assertRaises(CommandFailed) as raised:
                runner.run(
                    [
                        sys.executable,
                        "-c",
                        "print('ERROR: stage broke'); raise SystemExit(17)",
                    ],
                    cwd=Path(directory),
                )
            self.assertEqual(17, raised.exception.returncode)
            self.assertEqual("ERROR: stage broke", raised.exception.summary)

    def test_password_is_redacted_from_verbose_output_logs_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            password = 'fixture "$value=quoted" password'
            stream = io.StringIO()
            runner = CommandRunner(
                Console(OutputSettings.load({"VERBOSE": "1"}, stream), stream),
                Path(directory) / "deploy.log",
                {"APP_PASSWORD": password},
            )
            with self.assertRaises(CommandFailed) as raised:
                runner.run(
                    [
                        sys.executable,
                        "-c",
                        "import os,json; p=os.environ['APP_PASSWORD']; "
                        "print(p); print('ERROR: '+json.dumps(p)); "
                        "raise SystemExit(17)",
                    ],
                    cwd=Path(directory),
                )
            for output in (
                stream.getvalue(),
                runner.log_path.read_text(),
                raised.exception.summary,
            ):
                self.assertNotIn(password, output)
                self.assertNotIn("$value", output)
                self.assertIn("<redacted>", output)


if __name__ == "__main__":
    unittest.main()
