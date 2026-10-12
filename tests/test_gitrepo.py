import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from git_helpers import GitRepositoryTestCase
from whatif_engine import gitrepo
from whatif_engine.gitrepo import (
    MINIMUM_GIT_VERSION,
    FileChange,
    GitError,
    UnsafeGitFilters,
    open_repository,
    parse_git_version,
    parse_nul_list,
    parse_raw_diff,
    require_supported_git,
    safe_join,
    validate_ref,
)


def pretend_git_version(output: bytes):
    """Patch `git --version` only; every other git command really runs."""
    real = gitrepo._run_git

    def fake(args, *rest, **options):
        if args == ["--version"]:
            return output
        return real(args, *rest, **options)

    return mock.patch("whatif_engine.gitrepo._run_git", side_effect=fake)


def is_case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()

# Captured from `git diff --raw -z -M HEAD` (git 2.54).
RAW_FIXTURE = (
    b":100644 000000 bd2c89c 0000000 D\0pkg/gone.py\0"
    b":100644 100644 48daf64 0000000 M\0pkg/mod.py\0"
    b":100644 100644 6b67108 6b67108 R100\0pkg/old.py\0pkg/new.py\0"
    b":000000 100644 0000000 7ebef33 A\0pkg/staged.py\0"
)

SHA = re.compile(r"^[0-9a-f]{40}$")


class TestParseRawDiff(unittest.TestCase):
    def test_parses_real_git_output(self):
        self.assertEqual(parse_raw_diff(RAW_FIXTURE), [
            FileChange("deleted", "pkg/gone.py",
                       old_mode="100644", new_mode="000000"),
            FileChange("modified", "pkg/mod.py",
                       old_mode="100644", new_mode="100644"),
            FileChange("renamed", "pkg/new.py", old_path="pkg/old.py",
                       similarity=100,
                       old_mode="100644", new_mode="100644"),
            FileChange("added", "pkg/staged.py",
                       old_mode="000000", new_mode="100644"),
        ])

    def test_partial_rename_and_copy_keep_similarity(self):
        changes = parse_raw_diff(
            b":100644 100644 a b R087\0old.py\0new.py\0"
            b":100644 100644 a b C075\0src.py\0copy.py\0"
        )

        self.assertEqual(
            [(c.status, c.old_path, c.path, c.similarity) for c in changes],
            [("renamed", "old.py", "new.py", 87),
             ("copied", "src.py", "copy.py", 75)],
        )

    def test_type_change_unmerged_and_submodule(self):
        changes = parse_raw_diff(
            b":100644 120000 a b T\0link.py\0"
            b":100644 100644 a b U\0conflict.py\0"
            b":160000 160000 a b M\0vendor/lib\0"
        )

        self.assertEqual(
            [c.status for c in changes],
            ["type-changed", "unmerged", "modified"],
        )
        self.assertFalse(changes[0].is_submodule)
        self.assertTrue(changes[2].is_submodule)

    def test_paths_with_spaces_and_non_ascii_characters(self):
        changes = parse_raw_diff(
            b":000000 100644 a b A\0pkg/sp ace \xc3\xa9.py\0"
        )

        self.assertEqual(changes[0].path, "pkg/sp ace é.py")

    def test_empty_output_means_no_changes(self):
        self.assertEqual(parse_raw_diff(b""), [])

    def test_malformed_output_is_rejected(self):
        for data in (
            b"M\0pkg/mod.py\0",                       # no ':' header
            b":100644 100644 a b M\0",                # missing path
            b":100644 100644 a b R100\0old.py\0",     # missing new path
            b":100644 a b M\0x.py\0",                 # short header
        ):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    parse_raw_diff(data)


class TestParseNulList(unittest.TestCase):
    def test_splits_nul_terminated_paths(self):
        self.assertEqual(
            parse_nul_list(b"a.py\0dir/b c.py\0"), ["a.py", "dir/b c.py"]
        )
        self.assertEqual(parse_nul_list(b""), [])


class TestGitVersion(unittest.TestCase):
    def test_minimum_is_the_release_that_added_check_attr_source(self):
        self.assertEqual(MINIMUM_GIT_VERSION, (2, 40, 0))

    def test_parses_real_world_version_strings(self):
        for output, expected in (
            ("git version 2.54.0\n", (2, 54, 0)),
            ("git version 2.39.3 (Apple Git-146)\n", (2, 39, 3)),
            ("git version 2.40.0.windows.1\n", (2, 40, 0)),
            ("git version 2.41.0-rc2\n", (2, 41, 0)),
            ("git version 3.0\n", (3, 0, 0)),
        ):
            with self.subTest(output=output):
                self.assertEqual(parse_git_version(output), expected)

    def test_unrecognized_output_fails_closed(self):
        for output in ("", "hub version 2.14.2\n", "git version x.y\n"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(GitError, "Could not determine"):
                    parse_git_version(output)

    def test_versions_below_the_minimum_are_rejected(self):
        # 2.4.0 guards against comparing versions as strings.
        for output, found in (
            ("git version 2.39.9\n", "2.39.9"),
            ("git version 2.4.0\n", "2.4.0"),
            ("git version 1.99.99\n", "1.99.99"),
        ):
            with self.subTest(output=output):
                with self.assertRaisesRegex(
                    GitError, rf"Git 2\.40\.0 or newer is required \(found {found}\)"
                ):
                    require_supported_git(output)

    def test_minimum_and_newer_versions_are_accepted(self):
        for output in (
            "git version 2.40.0\n", "git version 2.40.1\n",
            "git version 2.100.0\n", "git version 3.0.0\n",
        ):
            with self.subTest(output=output):
                require_supported_git(output)


class TestValidateRef(unittest.TestCase):
    def test_rejects_unsafe_or_empty_refs(self):
        for ref in ("", "-x", "--output=x", "a\nb", "a\0b", " HEAD"):
            with self.subTest(ref=ref):
                with self.assertRaises(GitError):
                    validate_ref(ref)

    def test_accepts_ordinary_refs(self):
        for ref in ("HEAD", "main", "origin/main", "v1.0", "HEAD~2", "abc123"):
            with self.subTest(ref=ref):
                validate_ref(ref)


class TestOpenRepository(GitRepositoryTestCase):
    def test_finds_repository_root_from_a_subdirectory(self):
        (self.repo / "pkg").mkdir()

        repository = open_repository(self.repo / "pkg")

        self.assertEqual(repository.root, self.repo)

    def test_directory_outside_a_repository_is_an_error(self):
        outside = self.tmp / "outside"
        outside.mkdir()

        with self.assertRaises(GitError):
            open_repository(outside)

    def test_missing_git_executable_is_an_error(self):
        empty = self.tmp / "empty-bin"
        empty.mkdir()

        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaisesRegex(GitError, "not found"):
                open_repository(self.repo)

    def test_old_git_is_rejected_before_any_repository_command(self):
        with pretend_git_version(b"git version 2.39.3 (Apple Git-146)\n"):
            with self.assertRaisesRegex(GitError, r"2\.40\.0 or newer.*2\.39\.3"):
                open_repository(self.repo)

    def test_installed_git_meets_the_minimum(self):
        self.assertEqual(open_repository(self.repo).root, self.repo)

    def test_timeouts_are_reported(self):
        with self.assertRaisesRegex(GitError, "timed out"):
            open_repository(self.repo, timeout=1e-6)


class TestResolveCommit(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.write({"a.py": ""})
        self.head = self.commit()
        self.repository = open_repository(self.repo)

    def test_resolves_head_and_branch_names(self):
        self.assertEqual(self.repository.resolve_commit("HEAD"), self.head)
        self.assertEqual(self.repository.resolve_commit("main"), self.head)
        self.assertRegex(self.repository.resolve_commit("HEAD"), SHA)

    def test_unknown_ref_is_an_error(self):
        with self.assertRaisesRegex(GitError, "not a valid commit"):
            self.repository.resolve_commit("no-such-branch")

    def test_tree_object_is_not_a_commit(self):
        tree = self.git("rev-parse", "HEAD^{tree}").strip()

        with self.assertRaisesRegex(GitError, "not a valid commit"):
            self.repository.resolve_commit(tree)

    def test_option_like_ref_is_rejected_without_side_effects(self):
        with self.assertRaises(GitError):
            self.repository.resolve_commit("--output=injected")

        self.assertFalse((self.repo / "injected").exists())

    def test_repository_without_commits_has_no_head(self):
        empty = self.tmp / "empty-repo"
        empty.mkdir()
        self.git("init", "-q", str(empty))

        with self.assertRaisesRegex(GitError, "not a valid commit"):
            open_repository(empty).resolve_commit("HEAD")

    def test_merge_base_of_diverged_branches(self):
        base = self.head
        self.git("switch", "-q", "-c", "feature")
        self.write({"feature.py": ""})
        self.commit()
        self.git("switch", "-q", "main")
        self.write({"main.py": ""})
        main = self.commit()

        self.assertEqual(
            self.repository.merge_base(main, self.repository.resolve_commit("feature")),
            base,
        )

    def test_unrelated_histories_have_no_merge_base(self):
        self.git("switch", "-q", "--orphan", "unrelated")
        self.write({"other.py": ""})
        other = self.commit()

        with self.assertRaisesRegex(GitError, "no common ancestor"):
            self.repository.merge_base(self.head, other)


class TestWorkingTreeChanges(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.write({
            ".gitignore": "*.log\nignored_*.py\n",
            "pkg/__init__.py": "",
            "pkg/old.py": "X = 1\nY = 2\nZ = 3\n",
            "pkg/gone.py": "A = 1\n",
            "pkg/mod.py": "B = 1\n",
            "pkg/unstaged_gone.py": "C = 1\n",
        })
        self.base = self.commit("base")

        self.git("mv", "pkg/old.py", "pkg/new.py")
        self.git("rm", "-q", "pkg/gone.py")
        (self.repo / "pkg" / "unstaged_gone.py").unlink()
        self.write({
            "pkg/mod.py": "B = 2\n",
            "pkg/staged.py": "S = 1\n",
            "pkg/untracked.py": "U = 1\n",
            "debug.log": "noise\n",
            "ignored_generated.py": "G = 1\n",
        })
        self.git("add", "pkg/staged.py")
        self.repository = open_repository(self.repo)

    def test_reports_index_and_working_tree_against_base(self):
        changes = self.repository.working_tree_changes(
            self.base, include_untracked=True
        )

        self.assertEqual(
            [(c.status, c.path, c.old_path) for c in changes],
            [
                ("deleted", "pkg/gone.py", None),
                ("modified", "pkg/mod.py", None),
                ("renamed", "pkg/new.py", "pkg/old.py"),
                ("added", "pkg/staged.py", None),
                ("deleted", "pkg/unstaged_gone.py", None),
                ("untracked", "pkg/untracked.py", None),
            ],
        )

    def test_untracked_files_can_be_excluded(self):
        changes = self.repository.working_tree_changes(
            self.base, include_untracked=False
        )

        self.assertNotIn("untracked", [c.status for c in changes])
        self.assertNotIn("pkg/untracked.py", [c.path for c in changes])

    def test_visible_files_exclude_ignored_and_deleted_files(self):
        self.assertEqual(
            self.repository.visible_files(include_untracked=True),
            [".gitignore", "pkg/__init__.py", "pkg/mod.py",
             "pkg/new.py", "pkg/staged.py", "pkg/untracked.py"],
        )
        self.assertNotIn(
            "pkg/untracked.py",
            self.repository.visible_files(include_untracked=False),
        )

    def test_does_not_write_to_the_index(self):
        # A touched-but-unchanged file makes porcelain `git diff` refresh
        # the index's stat cache; GIT_OPTIONAL_LOCKS=0 does not stop it,
        # and it happens only some of the time, so try repeatedly.
        index = self.repo / ".git" / "index"

        for attempt in range(10):
            with self.subTest(attempt=attempt):
                # Same content, different mtime: stale stat cache.
                stamp = 1_000_000_000 + attempt
                os.utime(self.repo / "pkg" / "__init__.py", (stamp, stamp))
                before = (index.read_bytes(), index.stat().st_mtime_ns)

                self.repository.working_tree_changes(
                    self.base, include_untracked=True
                )

                self.assertEqual(
                    (index.read_bytes(), index.stat().st_mtime_ns), before
                )

    def test_same_size_edit_with_unchanged_timestamp_is_detected(self):
        # Git re-reads a file whose cached stat still matches only when
        # the file is not older than the index ("racy git"). A private
        # copy of the index must keep the original index's mtime, or
        # this edit is silently missed.
        self.git("config", "core.trustctime", "false")
        path = self.repo / "pkg" / "mod.py"
        self.write({"pkg/mod.py": "B = 1\n"})
        # Older than the index write, so `git add` records the stat
        # normally instead of marking the entry racily clean.
        recorded = time.time_ns() - 100 * 10**9
        os.utime(path, ns=(recorded, recorded))
        self.git("add", "pkg/mod.py")
        index = self.repo / ".git" / "index"
        os.utime(index, ns=(recorded, recorded))

        path.write_text("B = 3\n", encoding="utf-8")   # same size
        os.utime(path, ns=(recorded, recorded))         # same mtime

        changes = self.repository.working_tree_changes(
            self.base, include_untracked=False
        )

        self.assertEqual(
            [c.path for c in changes if c.path == "pkg/mod.py"],
            ["pkg/mod.py"],
        )
        self.assertEqual(path.read_text(encoding="utf-8"), "B = 3\n")

    def test_repository_fsmonitor_command_is_not_executed(self):
        marker = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "fsmonitor-hook"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))

        # Control: the same diff without the wrapper's override does
        # run the configured command.
        self.git("diff", "--raw", self.base, "--")
        self.assertTrue(marker.exists())
        marker.unlink()

        self.repository.working_tree_changes(self.base, include_untracked=True)

        self.assertFalse(marker.exists())


class TestUnmergedFiles(GitRepositoryTestCase):
    def test_conflicted_paths_are_reported_as_unmerged(self):
        # `git diff <commit>` shows a conflicted file as a plain "M";
        # the index is what knows it is unmerged.
        self.write({"a.py": "V = 1\n", "b.py": "W = 1\n"})
        self.commit("base")
        self.git("switch", "-q", "-c", "other")
        self.write({"a.py": "V = 2\n"})
        self.commit("other")
        self.git("switch", "-q", "main")
        self.write({"a.py": "V = 3\n", "b.py": "W = 2\n"})
        head = self.commit("main")

        with self.assertRaises(Exception):
            self.git("merge", "-q", "other")

        changes = open_repository(self.repo).working_tree_changes(
            head, include_untracked=True
        )

        self.assertEqual(
            [(c.status, c.path) for c in changes], [("unmerged", "a.py")]
        )


class TestSnapshot(GitRepositoryTestCase):
    def export(self, commit: str):
        destination = self.tmp / "snapshot"
        problems = open_repository(self.repo).export_python_snapshot(
            commit, destination
        )
        exported = sorted(
            p.relative_to(destination).as_posix()
            for p in destination.rglob("*") if p.is_file()
        )
        return destination, exported, problems

    def test_exports_python_and_marker_files_at_a_commit(self):
        # Rewritten: an in-tree symlink used to be skipped; it is now
        # materialized from its target, which is what a checkout holds.
        self.write({
            "pyproject.toml": "[project]\n",
            "README.md": "docs\n",
            "pkg/__init__.py": "",
            "pkg/a.py": "VALUE = 'base'\n",
        })
        os.symlink("a.py", self.repo / "pkg" / "link.py")
        base = self.commit()
        self.write({"pkg/a.py": "VALUE = 'working tree'\n"})

        destination, exported, problems = self.export(base)

        self.assertEqual(
            exported,
            ["pkg/__init__.py", "pkg/a.py", "pkg/link.py", "pyproject.toml"],
        )
        for name in ("a.py", "link.py"):
            self.assertEqual(
                (destination / "pkg" / name).read_text(encoding="utf-8"),
                "VALUE = 'base'\n",
            )
            self.assertFalse((destination / "pkg" / name).is_symlink())
        self.assertEqual(problems, [])

    def test_export_ignore_does_not_hide_tracked_sources(self):
        self.write({
            "pkg/__init__.py": "",
            "pkg/hidden.py": "H = 1\n",
            "pkg/visible.py": "V = 1\n",
            ".gitattributes": "pkg/hidden.py export-ignore\npkg/* export-ignore\n",
        })
        base = self.commit()

        _, exported, problems = self.export(base)

        tracked = sorted(
            p for p in self.git("ls-tree", "-r", "--name-only", base).split()
            if p.endswith(".py")
        )
        self.assertEqual(exported, tracked)
        self.assertEqual(problems, [])

    def test_export_subst_content_is_the_committed_blob(self):
        # `git archive` would replace $Format:%H$ with the commit ID.
        source = 'VERSION = "$Format:%H$"\nimport pkg\n'
        self.write({
            "pkg/__init__.py": "",
            "pkg/version.py": source,
            ".gitattributes": "pkg/version.py export-subst\n",
        })
        base = self.commit()

        destination, _, problems = self.export(base)

        self.assertEqual(
            (destination / "pkg" / "version.py").read_text(encoding="utf-8"),
            source,
        )
        self.assertEqual(problems, [])

    def test_symlink_chain_inside_the_tree_is_followed(self):
        self.write({"pkg/real.py": "R = 1\n"})
        os.symlink("real.py", self.repo / "pkg" / "first.py")
        (self.repo / "other").mkdir()
        os.symlink("../pkg/first.py", self.repo / "other" / "second.py")
        base = self.commit()

        destination, _, problems = self.export(base)

        self.assertEqual(
            (destination / "other" / "second.py").read_text(encoding="utf-8"),
            "R = 1\n",
        )
        self.assertEqual(problems, [])

    def test_unresolvable_symlinks_are_problems_not_files(self):
        outside = self.tmp / "outside.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        self.write({"pkg/ok.py": ""})
        os.symlink("../../outside.py", self.repo / "pkg" / "escapes.py")
        os.symlink(str(outside), self.repo / "pkg" / "absolute.py")
        os.symlink("loop_b.py", self.repo / "pkg" / "loop_a.py")
        os.symlink("loop_a.py", self.repo / "pkg" / "loop_b.py")
        os.symlink("missing.py", self.repo / "pkg" / "dangling.py")
        os.symlink(".", self.repo / "pkg" / "directory.py")
        base = self.commit()

        destination, exported, problems = self.export(base)

        self.assertEqual(exported, ["pkg/ok.py"])
        self.assertEqual(
            [problem.path for problem in problems],
            ["pkg/absolute.py", "pkg/dangling.py", "pkg/directory.py",
             "pkg/escapes.py", "pkg/loop_a.py", "pkg/loop_b.py"],
        )
        for problem in problems:
            self.assertIn("symlink", problem.message)
        self.assertFalse(any(p.is_symlink() for p in destination.rglob("*")))

    def test_case_colliding_paths_are_a_problem(self):
        if not is_case_insensitive(self.tmp):
            self.skipTest("file system is case-sensitive")

        first = subprocess_hash(self.repo, "A = 1\n")
        second = subprocess_hash(self.repo, "a = 2\n")
        self.git("update-index", "--add", "--cacheinfo", f"100644,{first},pkg/A.py")
        self.git("update-index", "--add", "--cacheinfo", f"100644,{second},pkg/a.py")
        self.git("commit", "-q", "-m", "case collision")
        base = self.git("rev-parse", "HEAD").strip()

        _, _, problems = self.export(base)

        self.assertEqual(len(problems), 1)
        self.assertIn("collides", problems[0].message)

    def test_smudge_filtered_source_is_a_problem_and_never_run(self):
        marker = self.tmp / "smudge-ran"
        self.write({
            "pkg/stored.py": "version https://git-lfs.github.com/spec/v1\n",
            "pkg/plain.py": "",
            ".gitattributes": "pkg/stored.py filter=lfsish\n",
        })
        base = self.commit()
        self.git("config", "filter.lfsish.smudge", f"touch '{marker}'; cat")

        _, exported, problems = self.export(base)

        self.assertEqual(exported, ["pkg/plain.py"])
        [problem] = problems
        self.assertEqual(problem.path, "pkg/stored.py")
        self.assertIn("filter 'lfsish'", problem.message)
        self.assertFalse(marker.exists())

    def test_unconfigured_filter_attribute_keeps_the_blob(self):
        # Without a smudge or process command, checkout writes the blob
        # unchanged, so the blob is the source.
        self.write({
            "pkg/a.py": "A = 1\n",
            ".gitattributes": "*.py filter=nothing-configured\n",
        })
        base = self.commit()

        _, exported, problems = self.export(base)

        self.assertEqual((exported, problems), (["pkg/a.py"], []))


class TestFilterPreflight(GitRepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "filter-ran"
        self.write({
            "app.py": "A = 1\n",
            "data.py": "D = 1\n",
            ".gitattributes": "app.py filter=probe\n",
        })
        self.base = self.commit()
        self.write({"app.py": "A = 2\n"})
        self.repository = open_repository(self.repo)

    def configure(self, key: str) -> None:
        self.git("config", key, f"sh -c 'touch \"{self.marker}\"; cat'")

    def test_applicable_clean_filter_is_refused_and_never_run(self):
        self.configure("filter.probe.clean")

        with self.assertRaises(UnsafeGitFilters) as caught:
            self.repository.working_tree_changes(self.base, include_untracked=True)

        self.assertFalse(self.marker.exists())
        self.assertEqual(caught.exception.risks, [("app.py", "probe")])
        self.assertIn("--allow-git-filters", str(caught.exception))

    def test_process_filter_is_refused_too(self):
        self.configure("filter.probe.process")

        with self.assertRaises(UnsafeGitFilters):
            self.repository.working_tree_changes(self.base, include_untracked=True)

        self.assertFalse(self.marker.exists())

    def test_explicit_opt_in_lets_git_run_the_filter(self):
        # Control: proves the refusal above prevents a real execution.
        self.configure("filter.probe.clean")

        changes = self.repository.working_tree_changes(
            self.base, include_untracked=True, allow_filters=True
        )

        self.assertTrue(self.marker.exists())
        self.assertEqual([c.path for c in changes], ["app.py"])

    def test_configured_filter_that_no_file_uses_does_not_block(self):
        self.configure("filter.unused.clean")

        changes = self.repository.working_tree_changes(
            self.base, include_untracked=True
        )

        self.assertEqual([c.path for c in changes], ["app.py"])
        self.assertFalse(self.marker.exists())

    def test_smudge_only_filter_does_not_block_the_comparison(self):
        # Comparing files runs clean/process filters, never smudge.
        self.configure("filter.probe.smudge")

        self.repository.working_tree_changes(self.base, include_untracked=True)

        self.assertFalse(self.marker.exists())

    def test_unreadable_configuration_fails_closed(self):
        with mock.patch(
            "whatif_engine.gitrepo._run_git",
            side_effect=GitError("git config failed: bad config", returncode=128),
        ):
            with self.assertRaisesRegex(GitError, "bad config"):
                self.repository.configured_filters()

    def test_submodule_filters_are_not_run(self):
        submodule = self.tmp / "sub"
        submodule.mkdir()
        self.git("init", "-q", "-b", "main", str(submodule))
        (submodule / "s.py").write_text("S = 1\n", encoding="utf-8")
        (submodule / ".gitattributes").write_text("*.py filter=subprobe\n", encoding="utf-8")
        self.git("-C", str(submodule), "add", "-A")
        self.git("-C", str(submodule), "commit", "-q", "-m", "sub")
        self.git("-c", "protocol.file.allow=always",
                 "submodule", "add", "-q", str(submodule), "sub")
        base = self.commit("add submodule")

        checkout = self.repo / "sub"
        self.git("-C", str(checkout), "config", "filter.subprobe.clean",
                 f"sh -c 'touch \"{self.marker}\"; cat'")
        (checkout / "s.py").write_text("S = 2\n", encoding="utf-8")

        # Control: a plain superproject diff runs the submodule's filter.
        self.git("diff", "--raw", base, "--")
        self.assertTrue(self.marker.exists())
        self.marker.unlink()

        self.repository.working_tree_changes(base, include_untracked=True)

        self.assertFalse(self.marker.exists())


def subprocess_hash(repo: Path, content: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo, input=content, capture_output=True, text=True,
        check=True, timeout=30,
    ).stdout.strip()


class TestSafeJoin(unittest.TestCase):
    def test_rejects_paths_that_could_escape(self):
        destination = Path("/tmp/snapshot")
        for unsafe in ("../x.py", "/abs.py", "a/../../b.py", "a/./b.py", ""):
            with self.subTest(path=unsafe):
                with self.assertRaises(GitError):
                    safe_join(destination, unsafe)

    def test_accepts_ordinary_nested_paths(self):
        self.assertEqual(
            safe_join(Path("/tmp/snapshot"), "pkg/sub/mod.py"),
            Path("/tmp/snapshot/pkg/sub/mod.py"),
        )


if __name__ == "__main__":
    unittest.main()
