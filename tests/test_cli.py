import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fifo_helpers import FIFO_SUPPORTED, make_fifo
from whatif_engine.cli import main

SRC = Path(__file__).resolve().parents[1] / "src"


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def run_cli(*args: str) -> tuple[int, str]:
    """Run the CLI in-process and return (exit code, stdout + stderr)."""
    output = io.StringIO()

    with (
        mock.patch.object(sys, "argv", ["whatif", *args]),
        redirect_stdout(output),
        redirect_stderr(output),
    ):
        try:
            code = main()
        except SystemExit as exc:
            code = exc.code

    return code, output.getvalue()


class TestImpactExitCodes(unittest.TestCase):
    def test_clean_analysis_exits_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "core.py": "",
                "user.py": "import core\n",
            })

            code, output = run_cli("impact", "core", str(root))

            self.assertEqual(code, 0)
            self.assertIn("user", output)
            self.assertNotIn("incomplete", output.lower())

    def test_analysis_errors_make_impact_exit_nonzero(self):
        # user.py cannot be parsed, so whether it depends on core
        # is unknown and the impact result may be incomplete.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "core.py": "",
                "user.py": "import core\ndef broken(:\n",
            })

            code, output = run_cli("impact", "core", str(root))

            self.assertEqual(code, 1)
            self.assertIn("user", output)
            self.assertIn("incomplete", output.lower())

    def test_warnings_alone_do_not_change_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "core.py": "",
                "pkg.py": "",
                "pkg/__init__.py": "import core\n",
            })

            code, output = run_cli("impact", "core", str(root))

            self.assertEqual(code, 0)
            self.assertIn("Warnings", output)
            self.assertIn("pkg.py", output)

    def test_unknown_target_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"core.py": ""})

            code, output = run_cli("impact", "missing", str(root))

            self.assertEqual(code, 2)
            self.assertIn("Unknown project module", output)


class TestGraphOutput(unittest.TestCase):
    def test_parent_package_edges_are_labelled(self):
        # The edge comes from Python's import system, not an import
        # statement, so the output must say why it exists.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/leaf.py": "",
            })

            code, output = run_cli("graph", str(root))

            self.assertEqual(code, 0)
            self.assertIn("-> pkg (parent package initializer)", output)


@unittest.skipUnless(FIFO_SUPPORTED, "named pipes are not supported")
class TestNamedPipeSmoke(unittest.TestCase):
    def test_scan_and_graph_finish_and_report_the_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"ok.py": "import json\n"})
            make_fifo(root / "pipe.py")

            for command in ("scan", "graph"):
                with self.subTest(command=command):
                    # A real process with a timeout: a regression fails
                    # here instead of hanging the suite.
                    result = subprocess.run(
                        [sys.executable, "-m", "whatif_engine", command, str(root)],
                        env={**os.environ, "PYTHONPATH": str(SRC)},
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("not a regular file", result.stdout)


class TestGraphExitCodes(unittest.TestCase):
    def test_analysis_errors_make_graph_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"broken.py": "def broken(:\n"})

            code, output = run_cli("graph", str(root))

            self.assertEqual(code, 1)
            self.assertIn("broken", output)


if __name__ == "__main__":
    unittest.main()
