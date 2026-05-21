import contextlib
import errno
import io
import runpy
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from storage_dashboard import cli


class CliTests(unittest.TestCase):
    def test_pyproject_exposes_lighthouse_console_script(self) -> None:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(data["project"]["scripts"]["lighthouse"], "storage_dashboard.cli:main")

    def test_default_dispatches_to_tui(self) -> None:
        with patch.object(sys, "argv", ["lighthouse"]), patch("storage_dashboard.cli.tui.run") as run_tui:
            cli.main()

        run_tui.assert_called_once_with()

    def test_tui_dispatches_to_tui(self) -> None:
        with patch.object(sys, "argv", ["lighthouse", "tui"]), patch("storage_dashboard.cli.tui.run") as run_tui:
            cli.main()

        run_tui.assert_called_once_with()

    def test_web_dispatches_to_existing_server(self) -> None:
        with (
            patch.object(sys, "argv", ["lighthouse", "web", "--port", "9000"]),
            patch("storage_dashboard.cli.server.run") as run_server,
        ):
            cli.main()

        run_server.assert_called_once_with(port=9000)

    def test_web_port_conflict_reports_lighthouse_command(self) -> None:
        error = OSError(errno.EADDRINUSE, "Address already in use")
        with (
            patch.object(sys, "argv", ["lighthouse", "web"]),
            patch("storage_dashboard.cli.server.run", side_effect=error),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as exit_error:
                cli.main()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("lighthouse web --port 8766", stderr.getvalue())

    def test_app_py_delegates_to_cli(self) -> None:
        with patch("storage_dashboard.cli.main") as main:
            runpy.run_path("app.py", run_name="__main__")

        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
