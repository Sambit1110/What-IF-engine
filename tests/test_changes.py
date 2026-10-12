import os
import unittest

from git_helpers import GitRepositoryTestCase
from whatif_engine.changes import analyze_changes
from whatif_engine.gitrepo import GitError, UnsafeGitFilters

PROJECT = {
    "pyproject.toml": "[project]\nname = 'app'\n",
    "README.md": "# App\n",
    "config.json": "{}\n",
    "src/app/__init__.py": "",
    "src/app/core.py": (
        "VALUE = 1\n"
        "\n"
        "def compute(x):\n"
        "    total = x * VALUE\n"
        "    return total + 1\n"
    ),
    "src/app/service.py": "from app.core import VALUE\n",
    "src/app/api.py": "from app import service\n",
    "src/app/util.py": "",
    "tests/helpers.py": "from app.api import service\n",
    "tests/test_api.py": "import helpers\n",
    "tests/test_util.py": "from app.util import thing\n",
}


class ChangeAnalysisTestCase(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.write(PROJECT)
        self.base = self.commit("base")

    def analyze(self, **options):
        return analyze_changes(self.repo, **options)

    @staticmethod
    def affected(report):
        return {entry.module: entry for entry in report.affected}


class TestBasics(ChangeAnalysisTestCase):
    def test_clean_working_tree_has_no_changes(self):
        report = self.analyze()

        self.assertEqual(report.base.ref, "HEAD")
        self.assertEqual(report.base.commit, self.base)
        self.assertEqual(report.changes, ())
        self.assertEqual(report.affected, ())
        self.assertEqual(report.suggested_tests, ())
        self.assertTrue(report.complete)

    def test_modified_module_reports_dependents_with_shortest_chains(self):
        self.write({"src/app/core.py": "VALUE = 2\n"})

        report = self.analyze()

        [change] = report.changes
        self.assertEqual(
            (change.status, change.path, change.module),
            ("modified", "src/app/core.py", "app.core"),
        )

        affected = self.affected(report)
        self.assertEqual(affected["app.service"].chain, ("app.service", "app.core"))
        self.assertEqual(affected["app.service"].distance, 1)
        self.assertEqual(affected["app.service"].path, "src/app/service.py")
        self.assertEqual(affected["app.service"].graph, "working-tree")
        self.assertEqual(
            affected["tests.test_api"].chain,
            ("tests.test_api", "tests.helpers", "app.api",
             "app.service", "app.core"),
        )
        self.assertNotIn("tests.test_util", affected)

        self.assertEqual(
            [(t.module, t.reason) for t in report.suggested_tests],
            [("tests.test_api", "depends on changed code")],
        )
        self.assertEqual(report.full_suite_reasons, ())

    def test_package_initializer_change_reaches_its_submodules(self):
        self.write({"src/app/__init__.py": "SETTING = 1\n"})

        affected = self.affected(self.analyze())

        self.assertEqual(affected["app.core"].chain, ("app.core", "app"))
        self.assertEqual(affected["tests.test_util"].distance, 1)

    def test_changed_test_file_is_suggested_directly(self):
        self.write({"tests/test_util.py": "from app.util import other\n"})

        report = self.analyze()

        self.assertEqual(
            [(t.module, t.reason, t.chain) for t in report.suggested_tests],
            [("tests.test_util", "test file changed", ())],
        )

    def test_staged_and_unstaged_edits_are_both_included(self):
        self.write({"src/app/util.py": "A = 1\n"})
        self.git("add", "src/app/util.py")
        self.write({"src/app/core.py": "VALUE = 3\n"})

        report = self.analyze()

        self.assertEqual(
            [c.path for c in report.changes],
            ["src/app/core.py", "src/app/util.py"],
        )

    def test_repository_code_is_never_executed(self):
        marker = self.tmp / "executed"
        self.write({
            "src/app/core.py": f"open({str(marker)!r}, 'w').close()\n",
        })

        self.analyze()

        self.assertFalse(marker.exists())


class TestUntrackedFiles(ChangeAnalysisTestCase):
    def setUp(self):
        super().setUp()
        self.write({
            "src/app/core.py": "VALUE = 2\n",
            "src/app/extra.py": "from app.core import VALUE\n",
        })

    def test_untracked_files_are_included_by_default(self):
        report = self.analyze()

        self.assertIn(
            ("untracked", "src/app/extra.py", "app.extra"),
            [(c.status, c.path, c.module) for c in report.changes],
        )
        self.assertIn("app.extra", report.changed_modules)
        self.assertTrue(report.include_untracked)

    def test_untracked_files_can_be_left_out_entirely(self):
        report = self.analyze(include_untracked=False)

        self.assertNotIn("src/app/extra.py", [c.path for c in report.changes])
        self.assertNotIn("app.extra", self.affected(report))
        self.assertFalse(report.include_untracked)

    def test_ignored_files_are_neither_reported_nor_analyzed(self):
        self.write({
            ".gitignore": "scratch_*.py\n",
            "scratch_tool.py": "from app.core import VALUE\n",
        })

        report = self.analyze()

        self.assertNotIn("scratch_tool.py", [c.path for c in report.changes])
        self.assertNotIn("scratch_tool", self.affected(report))


class TestDeletionsAndRenames(ChangeAnalysisTestCase):
    def test_deleted_module_reports_likely_broken_imports(self):
        self.git("rm", "-q", "src/app/core.py")

        report = self.analyze()

        [change] = report.changes
        self.assertEqual(
            (change.status, change.old_module, change.module),
            ("deleted", "app.core", None),
        )
        self.assertEqual(report.removed_modules, ("app.core",))

        [broken] = report.likely_broken_imports
        self.assertEqual(
            (broken.module, broken.path, broken.line, broken.removed_module),
            ("app.service", "src/app/service.py", 1, "app.core"),
        )
        self.assertIn("deleted", broken.reason)

        # Dependents come from the base snapshot, where the edge existed.
        affected = self.affected(report)
        self.assertEqual(affected["app.service"].graph, "base")
        self.assertEqual(affected["app.service"].chain, ("app.service", "app.core"))
        self.assertEqual(affected["app.api"].distance, 2)
        self.assertTrue(report.complete)

    def test_rename_without_updating_importers_is_flagged(self):
        self.git("mv", "src/app/core.py", "src/app/engine.py")

        report = self.analyze()

        [change] = report.changes
        self.assertEqual(
            (change.status, change.old_path, change.path,
             change.old_module, change.module, change.similarity),
            ("renamed", "src/app/core.py", "src/app/engine.py",
             "app.core", "app.engine", 100),
        )
        [broken] = report.likely_broken_imports
        self.assertEqual(broken.module, "app.service")
        self.assertIn("renamed to app.engine", broken.reason)

    def test_rename_with_updated_importers_is_not_flagged(self):
        self.git("mv", "src/app/core.py", "src/app/engine.py")
        self.write({"src/app/service.py": "from app.engine import VALUE\n"})

        report = self.analyze()

        self.assertEqual(report.likely_broken_imports, ())
        affected = self.affected(report)
        self.assertEqual(affected["app.api"].chain, ("app.api", "app.service"))

    def test_rename_with_edits_keeps_similarity(self):
        self.git("mv", "src/app/core.py", "src/app/engine.py")
        self.write({"src/app/engine.py": PROJECT["src/app/core.py"] + "EXTRA = 1\n"})

        [change] = self.analyze().changes

        self.assertEqual(change.status, "renamed")
        self.assertLess(change.similarity, 100)

    def test_deleted_package_initializer_is_explained(self):
        self.write({
            "src/app/__init__.py": "SETTING = 1\n",
            "src/app/settings_user.py": "from app import SETTING\n",
        })
        self.commit("initializer defines a name")
        self.git("rm", "-q", "src/app/__init__.py")

        report = self.analyze()

        self.assertIn("app", report.removed_modules)
        reasons = {b.module: b.reason for b in report.likely_broken_imports}
        self.assertIn("namespace package", reasons["app.settings_user"])
        # `from app import service` still works without the initializer.
        self.assertNotIn("app.api", reasons)


class TestBaseSelection(ChangeAnalysisTestCase):
    def setUp(self):
        super().setUp()
        self.git("switch", "-q", "-c", "feature")
        self.write({"src/app/util.py": "FEATURE = 1\n"})
        self.commit("feature work")
        self.git("switch", "-q", "main")
        self.write({"src/app/core.py": "VALUE = 99\n"})
        self.main_tip = self.commit("main moves on")
        self.git("switch", "-q", "feature")

    def test_base_ref_compares_against_that_commit(self):
        report = self.analyze(base="main")

        self.assertEqual(report.base.commit, self.main_tip)
        self.assertEqual(
            [c.path for c in report.changes],
            ["src/app/core.py", "src/app/util.py"],
        )

    def test_merge_base_shows_only_this_branch(self):
        report = self.analyze(base="main", merge_base=True)

        self.assertEqual(report.base.commit, self.base)
        self.assertEqual(report.base.merge_base_with, "main")
        self.assertEqual([c.path for c in report.changes], ["src/app/util.py"])

    def test_invalid_base_is_an_error(self):
        with self.assertRaisesRegex(GitError, "not a valid commit"):
            self.analyze(base="no-such-branch")


class TestScope(ChangeAnalysisTestCase):
    def test_scan_path_subdirectory_keeps_repository_relative_paths(self):
        self.write({
            "src/app/core.py": "VALUE = 2\n",
            "tests/test_api.py": "import helpers\nX = 1\n",
        })

        report = analyze_changes(self.repo / "src")

        self.assertEqual(report.scan_path, "src")
        by_path = {c.path: c for c in report.changes}
        self.assertEqual(by_path["src/app/core.py"].module, "app.core")
        self.assertIsNone(by_path["tests/test_api.py"].module)
        self.assertIn("outside the scanned path", by_path["tests/test_api.py"].note)
        self.assertTrue(any(
            "outside the scanned path" in reason
            for reason in report.full_suite_reasons
        ))

    def test_non_python_change_is_unknown_and_recommends_full_suite(self):
        self.write({"config.json": '{"debug": true}\n'})

        report = self.analyze()

        [change] = report.changes
        self.assertEqual(change.note, "not a Python file; impact not analyzed")
        self.assertTrue(any("config.json" in r for r in report.full_suite_reasons))

    def test_documentation_change_alone_does_not_require_full_suite(self):
        self.write({"README.md": "# App\n\nMore docs.\n"})

        self.assertEqual(self.analyze().full_suite_reasons, ())


class TestDiagnostics(ChangeAnalysisTestCase):
    def test_syntax_error_makes_the_report_incomplete(self):
        self.write({"src/app/service.py": "def broken(:\n"})

        report = self.analyze()

        self.assertFalse(report.complete)
        [error] = report.errors
        self.assertEqual(error.module, "app.service")
        self.assertEqual(error.path, "src/app/service.py")
        self.assertTrue(any("incomplete" in r for r in report.full_suite_reasons))

    def test_shadowing_introduced_by_the_change_is_flagged(self):
        self.write({"app/__init__.py": ""})

        report = self.analyze()

        introduced = [w for w in report.warnings if w.introduced]
        self.assertTrue(any(
            "src/app/__init__.py" in w.message for w in introduced
        ))

    def test_dynamic_imports_recommend_the_full_suite(self):
        self.write({
            "src/app/plugins.py": (
                "import importlib\nimportlib.import_module('app.core')\n"
            ),
        })

        report = self.analyze()

        self.assertTrue(any(
            "dynamic imports" in r and "app.plugins" in r
            for r in report.full_suite_reasons
        ))

    def test_conflicted_files_are_warned_about(self):
        self.git("switch", "-q", "-c", "other")
        self.write({"src/app/util.py": "A = 1\n"})
        self.commit("other")
        self.git("switch", "-q", "main")
        self.write({"src/app/util.py": "A = 2\n"})
        self.commit("main")
        with self.assertRaises(Exception):
            self.git("merge", "-q", "other")

        report = self.analyze()

        self.assertEqual(report.changes[0].status, "unmerged")
        self.assertTrue(any("conflict" in w.message for w in report.warnings))
        self.assertTrue(any("conflict" in r for r in report.full_suite_reasons))


class TestBaseSnapshotFidelity(ChangeAnalysisTestCase):
    def test_export_attributes_do_not_change_the_base_analysis(self):
        self.write({
            ".gitattributes": (
                "src/app/core.py export-ignore\n"
                "src/app/*.py export-subst\n"
            ),
        })
        self.commit("export attributes")
        self.git("rm", "-q", "src/app/core.py")

        report = self.analyze()

        # core.py is in the base tree even though `git archive` would
        # leave it out, so its removal and stale importer are found.
        self.assertEqual(report.removed_modules, ("app.core",))
        self.assertEqual(
            [b.module for b in report.likely_broken_imports], ["app.service"]
        )
        self.assertTrue(report.complete)

    def test_filter_checked_out_base_file_makes_the_report_incomplete(self):
        marker = self.tmp / "smudge-ran"
        self.write({
            "src/app/stored.py": "version https://git-lfs.github.com/spec/v1\n",
            ".gitattributes": "src/app/stored.py filter=lfsish\n",
        })
        self.commit("filtered file")
        self.git("config", "filter.lfsish.smudge", f"touch '{marker}'; cat")
        self.write({"src/app/new.py": ""})

        report = self.analyze()

        self.assertFalse(report.complete)
        [error] = [e for e in report.errors if "base snapshot" in e.message]
        self.assertEqual(error.path, "src/app/stored.py")
        self.assertIn("filter 'lfsish'", error.message)
        self.assertFalse(marker.exists())
        self.assertTrue(any("incomplete" in r for r in report.full_suite_reasons))

    def test_base_symlink_leaving_the_repository_makes_the_report_incomplete(self):
        os.symlink("../../../elsewhere.py", self.repo / "src" / "app" / "linked.py")
        self.commit("outward symlink")
        self.git("rm", "-q", "src/app/util.py")

        report = self.analyze()

        self.assertFalse(report.complete)
        self.assertTrue(any(
            e.path == "src/app/linked.py" and "symlink" in e.message
            for e in report.errors
        ))


class TestGitFilters(ChangeAnalysisTestCase):
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

    def test_refused_by_default_without_running_the_filter(self):
        with self.assertRaises(UnsafeGitFilters):
            self.analyze()

        self.assertFalse(self.marker.exists())

    def test_explicit_opt_in_is_recorded_and_warned_about(self):
        report = self.analyze(allow_git_filters=True)

        self.assertTrue(self.marker.exists())
        self.assertTrue(report.allow_git_filters)
        self.assertTrue(any(
            "'probe'" in w.message and "may have run" in w.message
            for w in report.warnings
        ))
        self.assertIn("app.service", self.affected(report))


if __name__ == "__main__":
    unittest.main()
