import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from git_helpers import GitRepositoryTestCase
from whatif_engine import gitrepo
from whatif_engine.cli import main

SRC = Path(__file__).resolve().parents[1] / "src"

PROJECT = {
    "pyproject.toml": "[project]\nname = 'app'\n",
    "src/app/__init__.py": "",
    "src/app/core.py": "VALUE = 1\n",
    "src/app/service.py": "from app.core import VALUE\n",
    "tests/test_service.py": "from app.service import VALUE\n",
}


def run_cli(*args: str) -> tuple[int, str, str]:
    """Run the CLI in-process; return (exit code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()

    with (
        mock.patch.object(sys, "argv", ["whatif", *args]),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        try:
            code = main()
        except SystemExit as exc:
            code = exc.code

    return code, stdout.getvalue(), stderr.getvalue()


class ChangesCliTestCase(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.write(PROJECT)
        self.base = self.commit("base")


class TestTextOutput(ChangesCliTestCase):
    def test_clean_tree(self):
        code, stdout, stderr = run_cli("changes", str(self.repo))

        self.assertEqual(code, 0)
        self.assertIn("No changes", stdout)
        self.assertEqual(stderr, "")

    def test_modified_module_shows_sections_and_chains(self):
        self.write({"src/app/core.py": "VALUE = 2\n"})

        code, stdout, _ = run_cli("changes", str(self.repo))

        self.assertEqual(code, 0)
        self.assertIn(f"Base: HEAD ({self.base[:12]})", stdout)
        self.assertIn("Observed changes (from Git): 1", stdout)
        self.assertIn("modified    src/app/core.py  [app.core]", stdout)
        self.assertIn("Predicted impact (static estimate", stdout)
        self.assertIn("app.service — direct, distance 1", stdout)
        self.assertIn(
            "tests.test_service -> app.service -> app.core", stdout
        )
        self.assertIn("Suggested tests (static estimate", stdout)
        self.assertIn("tests/test_service.py", stdout)

    def test_deletion_lists_likely_broken_imports(self):
        self.git("rm", "-q", "src/app/core.py")

        code, stdout, _ = run_cli("changes", str(self.repo))

        self.assertEqual(code, 0)
        self.assertIn("Likely broken imports (predicted)", stdout)
        self.assertIn("src/app/service.py:1", stdout)
        self.assertIn("app.core was deleted", stdout)
        self.assertIn("(base snapshot)", stdout)

    def test_incomplete_analysis_exits_one(self):
        self.write({"src/app/service.py": "def broken(:\n"})

        code, stdout, _ = run_cli("changes", str(self.repo))

        self.assertEqual(code, 1)
        self.assertIn("Errors (analysis incomplete)", stdout)
        self.assertIn("Run the full test suite: recommended", stdout)


class TestJsonOutput(ChangesCliTestCase):
    def test_json_is_the_only_thing_on_stdout(self):
        self.write({
            "src/app/core.py": "VALUE = 2\n",
            "src/app/new.py": "",
        })

        code, stdout, stderr = run_cli("changes", str(self.repo), "--json")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)

        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["base"], {
            "ref": "HEAD", "commit": self.base, "merge_base_with": None,
        })
        self.assertTrue(report["include_untracked"])
        self.assertEqual(
            [(c["status"], c["path"], c["module"])
             for c in report["observed"]["changes"]],
            [("modified", "src/app/core.py", "app.core"),
             ("untracked", "src/app/new.py", "app.new")],
        )

        [service, test] = report["predicted"]["affected_modules"]
        self.assertEqual(service, {
            "module": "app.service",
            "path": "src/app/service.py",
            "distance": 1,
            "relationship": "direct",
            "chain": ["app.service", "app.core"],
            "graph": "working-tree",
        })
        self.assertEqual(test["relationship"], "indirect")
        self.assertEqual(
            report["predicted"]["suggested_tests"][0]["path"],
            "tests/test_service.py",
        )
        self.assertIn("static estimate", report["predicted"]["note"])
        self.assertEqual(
            report["predicted"]["run_full_suite"],
            {"recommended": False, "reasons": []},
        )
        self.assertEqual(report["unknown"], [])
        self.assertEqual(report["errors"], [])

    def test_incomplete_analysis_is_still_valid_json(self):
        self.write({"src/app/service.py": "def broken(:\n"})

        code, stdout, stderr = run_cli("changes", str(self.repo), "--json")

        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"][0]["module"], "app.service")
        self.assertTrue(report["predicted"]["run_full_suite"]["recommended"])

    def test_unknown_changes_and_warnings_are_structured(self):
        self.write({"config.json": "{}\n", "app/__init__.py": ""})

        _, stdout, _ = run_cli("changes", str(self.repo), "--json")
        report = json.loads(stdout)

        self.assertIn(
            {"path": "config.json", "status": "untracked",
             "reason": "not a Python file; impact not analyzed"},
            report["unknown"],
        )
        self.assertTrue(any(w["introduced"] for w in report["warnings"]))

    def test_no_untracked_flag(self):
        self.write({"src/app/new.py": ""})

        _, stdout, _ = run_cli(
            "changes", str(self.repo), "--json", "--no-untracked"
        )
        report = json.loads(stdout)

        self.assertFalse(report["include_untracked"])
        self.assertEqual(report["observed"]["changes"], [])

    def test_fatal_errors_go_to_stderr_with_empty_stdout(self):
        outside = self.tmp / "outside"
        outside.mkdir()

        for args, message in (
            (("--base", "no-such-branch"), "not a valid commit"),
            # argparse refuses an option-like value in this form...
            (("--base", "--output=x"), "expected one argument"),
            # ...and this form reaches the ref validator.
            (("--base=--output=x",), "must not start with '-'"),
        ):
            with self.subTest(args=args):
                code, stdout, stderr = run_cli(
                    "changes", str(self.repo), "--json", *args
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn(message, stderr)

        code, stdout, stderr = run_cli("changes", str(outside), "--json")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Not inside a Git repository", stderr)


    def test_unparseable_git_output_is_a_clean_failure(self):
        with mock.patch(
            "whatif_engine.gitrepo.Repository.working_tree_changes",
            side_effect=ValueError("Unexpected git diff record: 'x'"),
        ):
            code, stdout, stderr = run_cli("changes", str(self.repo), "--json")

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Unexpected git diff record", stderr)
        self.assertNotIn("Traceback", stderr)


class TestOldGit(ChangesCliTestCase):
    def test_old_git_fails_the_same_way_in_both_formats(self):
        real = gitrepo._run_git

        def fake(args, *rest, **options):
            if args == ["--version"]:
                return b"git version 2.39.3\n"
            return real(args, *rest, **options)

        for extra in ((), ("--json",)):
            with self.subTest(args=extra):
                with mock.patch(
                    "whatif_engine.gitrepo._run_git", side_effect=fake
                ):
                    code, stdout, stderr = run_cli(
                        "changes", str(self.repo), *extra
                    )

                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(
                    "Git 2.40.0 or newer is required (found 2.39.3)", stderr
                )


class TestOptions(ChangesCliTestCase):
    def test_merge_base_requires_base(self):
        code, stdout, stderr = run_cli("changes", str(self.repo), "--merge-base")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--merge-base requires --base", stderr)

    def test_merge_base_is_shown_in_both_formats(self):
        self.git("switch", "-q", "-c", "feature")
        self.write({"src/app/core.py": "VALUE = 2\n"})
        self.commit("feature")

        code, stdout, _ = run_cli(
            "changes", str(self.repo), "--base", "main", "--merge-base"
        )
        self.assertEqual(code, 0)
        self.assertIn(
            f"Base: merge base of HEAD and main ({self.base[:12]})", stdout
        )

        _, stdout, _ = run_cli(
            "changes", str(self.repo), "--base", "main", "--merge-base", "--json"
        )
        self.assertEqual(json.loads(stdout)["base"]["merge_base_with"], "main")


class TestGitFilterSafety(ChangesCliTestCase):
    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "clean-ran"
        self.write({".gitattributes": "src/app/core.py filter=probe\n"})
        self.commit("filter attribute")
        self.git(
            "config", "filter.probe.clean",
            f"sh -c 'touch \"{self.marker}\"; cat'",
        )
        self.write({"src/app/core.py": "VALUE = 2\n"})

    def test_text_and_json_fail_closed_the_same_way(self):
        for extra in ((), ("--json",)):
            with self.subTest(args=extra):
                code, stdout, stderr = run_cli("changes", str(self.repo), *extra)

                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("refusing to compare working-tree files", stderr)
                self.assertIn("'probe'", stderr)
                self.assertIn("--allow-git-filters", stderr)

        self.assertFalse(self.marker.exists())

    def test_opt_in_flag_runs_and_says_so(self):
        code, stdout, stderr = run_cli(
            "changes", str(self.repo), "--json", "--allow-git-filters"
        )

        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertTrue(report["allow_git_filters"])
        self.assertTrue(any("'probe'" in w["message"] for w in report["warnings"]))

        code, stdout, _ = run_cli("changes", str(self.repo), "--allow-git-filters")
        self.assertEqual(code, 0)
        self.assertIn("Git filters: allowed", stdout)


class TestIncompleteBaseSnapshot(ChangesCliTestCase):
    def test_both_formats_exit_one_with_the_problem_reported(self):
        self.write({
            "src/app/stored.py": "version https://git-lfs.github.com/spec/v1\n",
            ".gitattributes": "src/app/stored.py filter=lfsish\n",
        })
        self.commit("filtered file")
        self.git("config", "filter.lfsish.smudge", "cat")
        self.write({"src/app/new.py": ""})

        code, stdout, stderr = run_cli("changes", str(self.repo))
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("Errors (analysis incomplete)", stdout)
        self.assertIn("base snapshot", stdout)

        code, stdout, stderr = run_cli("changes", str(self.repo), "--json")
        self.assertEqual((code, stderr), (1, ""))
        report = json.loads(stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"][0]["path"], "src/app/stored.py")
        self.assertFalse(report["allow_git_filters"])


class TestEndToEnd(ChangesCliTestCase):
    def run_module(self, *args: str) -> subprocess.CompletedProcess:
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        return subprocess.run(
            [sys.executable, "-m", "whatif_engine", *args],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_real_process_json_and_text(self):
        self.git("mv", "src/app/core.py", "src/app/engine.py")

        result = self.run_module("changes", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        [change] = report["observed"]["changes"]
        self.assertEqual(
            (change["status"], change["old_path"], change["path"]),
            ("renamed", "src/app/core.py", "src/app/engine.py"),
        )
        self.assertEqual(
            report["predicted"]["likely_broken_imports"][0]["module"],
            "app.service",
        )

        text = self.run_module("changes")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("renamed to app.engine", text.stdout)


if __name__ == "__main__":
    unittest.main()
