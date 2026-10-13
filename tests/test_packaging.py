import importlib
import importlib.metadata as metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import whatif_engine
from git_helpers import GitRepositoryTestCase

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTION = "whatif-engine"
COMMANDS = ("scan", "graph", "impact", "changes")


def load_pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def installed_distribution() -> metadata.Distribution | None:
    try:
        return metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def console_script() -> Path | None:
    """The `whatif` script installed next to this interpreter, if any."""
    name = "whatif.exe" if os.name == "nt" else "whatif"
    candidate = Path(sys.executable).parent / name
    return candidate if candidate.is_file() else None


def run_outside_repository(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """
    Run without PYTHONPATH from a directory outside the repository, so
    only the installed package can be imported.
    """
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        command, cwd=cwd, env=environment,
        capture_output=True, text=True, timeout=60,
    )


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


SAMPLE_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/core.py": "VALUE = 1\n",
    "pkg/service.py": "from pkg.core import VALUE\n",
}


class TestProjectMetadata(unittest.TestCase):
    def setUp(self):
        self.pyproject = load_pyproject()
        self.project = self.pyproject["project"]

    def test_build_backend_is_a_single_bounded_dependency(self):
        self.assertEqual(self.pyproject["build-system"], {
            "requires": ["flit_core >=3.12,<5"],
            "build-backend": "flit_core.buildapi",
        })

    def test_distribution_metadata(self):
        self.assertEqual(self.project["name"], DISTRIBUTION)
        # Python 3.10 reached end of life on 2026-10-01, and the test
        # suite itself needs tomllib (3.11+).
        self.assertEqual(self.project["requires-python"], ">=3.11")
        self.assertEqual(self.project["readme"], "README.md")
        self.assertTrue((ROOT / "README.md").is_file())
        self.assertTrue(self.project["description"])

    def test_license_is_mit(self):
        self.assertEqual(self.project["license"], "MIT")
        self.assertEqual(self.project["license-files"], ["LICENSE"])

        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 Sambit Biswas\n", license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)

    def test_no_invented_identity_or_urls(self):
        # Only the owner adds these, with real values.
        for field in ("authors", "maintainers", "urls"):
            self.assertNotIn(field, self.project)

    def test_no_runtime_dependencies(self):
        self.assertEqual(self.project["dependencies"], [])
        self.assertNotIn("optional-dependencies", self.project)

    def test_version_has_a_single_source(self):
        # Declared dynamic, read by the backend from __version__, which
        # the JSON report also uses.
        self.assertEqual(self.project["dynamic"], ["version"])
        self.assertNotIn("version", self.project)
        self.assertRegex(whatif_engine.__version__, r"^\d+\.\d+\.\d+$")

    def test_package_is_found_in_the_src_layout(self):
        self.assertEqual(
            self.pyproject["tool"]["flit"]["module"]["name"], "whatif_engine"
        )
        self.assertTrue((ROOT / "src" / "whatif_engine" / "__init__.py").is_file())

    def test_console_script_targets_the_existing_cli(self):
        self.assertEqual(
            self.project["scripts"], {"whatif": "whatif_engine.cli:main"}
        )
        module_name, _, attribute = self.project["scripts"]["whatif"].partition(":")
        self.assertTrue(callable(getattr(importlib.import_module(module_name), attribute)))


class TestContinuousIntegration(unittest.TestCase):
    """Safety and consistency properties of the GitHub Actions workflow."""

    def setUp(self):
        self.workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

    def test_permissions_are_read_only(self):
        self.assertRegex(self.workflow, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.workflow, r"(?m):\s*write\b")

    def test_actions_are_official_and_pinned_to_full_commit_shas(self):
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", self.workflow)
        self.assertTrue(uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$")

    def test_checkout_does_not_persist_credentials(self):
        self.assertIn("persist-credentials: false", self.workflow)

    def test_matrix_starts_at_the_supported_minimum(self):
        minimum = load_pyproject()["project"]["requires-python"].removeprefix(">=")
        versions = re.findall(r'"(3\.\d+)"', self.workflow)
        self.assertEqual(min(versions, key=lambda v: tuple(map(int, v.split(".")))), minimum)
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', self.workflow)
        self.assertIn("macos-latest", self.workflow)
        self.assertIn("ubuntu-latest", self.workflow)

    def test_regular_install_full_suite_and_cli_smoke_test(self):
        self.assertIn("-m pip install .\n", self.workflow)
        self.assertNotIn("pip install -e", self.workflow)
        self.assertIn("-m unittest discover -s tests -v", self.workflow)
        self.assertIn("working-directory: ${{ runner.temp }}", self.workflow)
        for command in ("--help", "scan", "graph", "impact", "changes"):
            self.assertRegex(self.workflow, rf"/whatif\" {re.escape(command)}")


@unittest.skipUnless(
    installed_distribution(),
    f"{DISTRIBUTION} is not installed; run `python -m pip install -e .`",
)
class TestInstalledDistribution(unittest.TestCase):
    def test_installed_version_matches_the_source(self):
        self.assertEqual(metadata.version(DISTRIBUTION), whatif_engine.__version__)

    def test_console_script_entry_point_is_registered(self):
        [entry_point] = metadata.entry_points(group="console_scripts", name="whatif")
        self.assertEqual(entry_point.value, "whatif_engine.cli:main")
        self.assertEqual(entry_point.dist.name, DISTRIBUTION)

    def test_installed_metadata_declares_no_dependencies(self):
        self.assertIsNone(installed_distribution().requires)
        self.assertEqual(
            installed_distribution().metadata["Requires-Python"], ">=3.11"
        )

    def test_installed_metadata_carries_the_license(self):
        meta = installed_distribution().metadata
        self.assertEqual(meta["License-Expression"], "MIT")
        self.assertEqual(meta.get_all("License-File"), ["LICENSE"])

    def test_editable_install_points_at_this_source_tree(self):
        direct_url = installed_distribution().read_text("direct_url.json")
        if not direct_url or not json.loads(direct_url).get("dir_info", {}).get("editable"):
            self.skipTest("not an editable install; source location not checked")

        with tempfile.TemporaryDirectory() as temporary:
            result = run_outside_repository(
                [sys.executable, "-c",
                 "import whatif_engine; print(whatif_engine.__file__)"],
                Path(temporary),
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            Path(result.stdout.strip()).resolve().is_relative_to(ROOT / "src"),
            result.stdout,
        )


@unittest.skipUnless(console_script(), "the `whatif` console script is not installed")
class TestConsoleScript(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outside = Path(temporary.name).resolve()
        self.project = self.outside / "project"
        write_files(self.project, SAMPLE_PROJECT)
        self.assertFalse(self.outside.is_relative_to(ROOT))

    def whatif(self, *args: str) -> subprocess.CompletedProcess:
        return run_outside_repository([str(console_script()), *args], self.outside)

    def test_help_lists_every_command(self):
        result = self.whatif("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("usage: whatif "))
        for command in COMMANDS:
            self.assertIn(command, result.stdout)

    def test_scan_graph_and_impact(self):
        scan = self.whatif("scan", str(self.project))
        self.assertEqual(scan.returncode, 0, scan.stderr)
        self.assertIn("pkg/service.py", scan.stdout)

        graph = self.whatif("graph", str(self.project))
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertIn("-> pkg.core", graph.stdout)
        self.assertIn("-> pkg (parent package initializer)", graph.stdout)

        impact = self.whatif("impact", "pkg.core", str(self.project))
        self.assertEqual(impact.returncode, 0, impact.stderr)
        self.assertIn("pkg.service — direct, dependency distance 1", impact.stdout)

    def test_errors_keep_their_exit_codes(self):
        unknown = self.whatif("impact", "no.such.module", str(self.project))
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("Unknown project module", unknown.stderr)

        write_files(self.project, {"broken.py": "def broken(:\n"})
        broken = self.whatif("graph", str(self.project))
        self.assertEqual(broken.returncode, 1)

    def test_python_dash_m_works_without_pythonpath(self):
        result = run_outside_repository(
            [sys.executable, "-m", "whatif_engine", "--help"], self.outside
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changes", result.stdout)


@unittest.skipUnless(console_script(), "the `whatif` console script is not installed")
class TestConsoleScriptChanges(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        write_files(self.repo, {"pyproject.toml": "[project]\n", **SAMPLE_PROJECT})
        self.commit("base")
        write_files(self.repo, {"pkg/core.py": "VALUE = 2\n"})
        self.assertFalse(self.repo.is_relative_to(ROOT))

    def whatif(self, *args: str) -> subprocess.CompletedProcess:
        return run_outside_repository([str(console_script()), *args], self.repo)

    def test_text_output(self):
        result = self.whatif("changes")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("modified    pkg/core.py  [pkg.core]", result.stdout)
        self.assertIn("pkg.service -> pkg.core", result.stdout)

    def test_json_output_is_the_only_thing_on_stdout(self):
        result = self.whatif("changes", "--json")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        report = json.loads(result.stdout)
        self.assertEqual(report["tool"]["version"], whatif_engine.__version__)
        self.assertEqual(
            [entry["module"] for entry in report["predicted"]["affected_modules"]],
            ["pkg.service"],
        )

    def test_fatal_errors_go_to_stderr(self):
        result = self.whatif("changes", "--json", "--base", "no-such-branch")

        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn("not a valid commit", result.stderr)


if __name__ == "__main__":
    unittest.main()
