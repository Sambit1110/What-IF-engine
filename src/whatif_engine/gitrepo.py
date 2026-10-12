"""
Read-only access to a Git repository through the git command line.

Every command is run with an argument list (never a shell), a timeout,
and settings that keep it read-only: optional index locks are disabled
and a repository-configured fsmonitor command is never started.

Comparing working-tree files can make Git run configured clean/process
filter commands. Unless explicitly allowed, that comparison is refused
when any such filter applies to a tracked file (see UnsafeGitFilters).
"""

import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from whatif_engine.graph import PROJECT_MARKERS

GIT_TIMEOUT_SECONDS = 30.0

# `git check-attr --source=<tree-ish>`, used to read a commit's filter
# attributes, first shipped in Git 2.40.0.
MINIMUM_GIT_VERSION = (2, 40, 0)

_GIT_VERSION = re.compile(r"^git version (\d+)\.(\d+)(?:\.(\d+))?")

_STATUSES = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
    "U": "unmerged",
    "X": "unknown",
}

_SUBMODULE_MODE = "160000"
_SYMLINK_MODE = "120000"
_REGULAR_FILE_MODES = {"100644", "100755"}
_MAX_SYMLINK_HOPS = 8


class GitError(Exception):
    """A git command could not be run or reported a failure."""

    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class UnsafeGitFilters(GitError):
    """
    Comparing working-tree files would let Git run external filter
    commands configured for tracked files.
    """

    def __init__(self, risks: list[tuple[str, str]]):
        self.risks = risks
        drivers = ", ".join(f"'{d}'" for d in sorted({d for _, d in risks}))
        examples = ", ".join(path for path, _ in risks[:3])
        super().__init__(
            "refusing to compare working-tree files: Git would run external "
            f"filter commands (driver {drivers}) on {len(risks)} tracked "
            f"file(s), e.g. {examples}. Re-run with --allow-git-filters to "
            "permit this."
        )


@dataclass(frozen=True)
class SnapshotProblem:
    """A base-commit file that could not be materialized faithfully."""

    path: str
    message: str


@dataclass(frozen=True)
class FileChange:
    # added, modified, deleted, renamed, copied, type-changed,
    # unmerged, unknown, or untracked
    status: str

    # Repository-relative POSIX path; the new path for renames and copies
    path: str

    old_path: str | None = None
    similarity: int | None = None
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def is_submodule(self) -> bool:
        return _SUBMODULE_MODE in (self.old_mode, self.new_mode)


def parse_nul_list(data: bytes) -> list[str]:
    """Split NUL-terminated paths, as printed by git's -z options."""
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def parse_raw_diff(data: bytes) -> list[FileChange]:
    """
    Parse `git diff --raw -z` output.

    Each record is `:oldmode newmode oldsha newsha STATUS` followed by
    one NUL-terminated path, or two for renames and copies.
    """
    fields = data.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()

    changes = []
    index = 0

    while index < len(fields):
        header = fields[index].decode("ascii", errors="replace")
        parts = header.split(" ")

        if not header.startswith(":") or len(parts) != 5:
            raise ValueError(f"Unexpected git diff record: {header!r}")

        old_mode, new_mode = parts[0][1:], parts[1]
        code, score = parts[4][:1], parts[4][1:]
        status = _STATUSES.get(code, "unknown")
        path_count = 2 if code in ("R", "C") else 1

        paths = fields[index + 1:index + 1 + path_count]
        if len(paths) != path_count or not all(paths):
            raise ValueError(f"Missing path in git diff record: {header!r}")

        decoded = [os.fsdecode(path) for path in paths]
        changes.append(FileChange(
            status=status,
            path=decoded[-1],
            old_path=decoded[0] if path_count == 2 else None,
            similarity=int(score) if score.isdigit() else None,
            old_mode=old_mode,
            new_mode=new_mode,
        ))
        index += 1 + path_count

    return changes


def parse_git_version(output: str) -> tuple[int, int, int]:
    """
    Parse `git --version` output, e.g. "git version 2.39.3 (Apple Git-146)"
    or "git version 2.40.0.windows.1". Unrecognized output is an error.
    """
    match = _GIT_VERSION.match(output.strip())
    if match is None:
        raise GitError(
            f"Could not determine the Git version from {output.strip()!r}"
        )

    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def require_supported_git(output: str) -> None:
    """Raise GitError unless `git --version` output meets the minimum."""
    version = parse_git_version(output)

    if version < MINIMUM_GIT_VERSION:
        required = ".".join(map(str, MINIMUM_GIT_VERSION))
        found = ".".join(map(str, version))
        raise GitError(
            f"Git {required} or newer is required (found {found}); "
            "change analysis uses `git check-attr --source`"
        )


def validate_ref(ref: str) -> None:
    """
    Reject refs that could be read as options or that git itself would
    refuse. The ref is still verified by git afterwards.
    """
    if not ref:
        raise GitError("Git reference must not be empty")

    if ref.startswith("-"):
        raise GitError(f"Invalid Git reference {ref!r}: must not start with '-'")

    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in ref):
        raise GitError(
            f"Invalid Git reference {ref!r}: contains whitespace "
            "or control characters"
        )


def _run_git(
    args: list[str],
    cwd: Path,
    timeout: float,
    stdin: bytes | None = None,
    extra_environment: dict[str, str] | None = None,
) -> bytes:
    command = ["git", "-c", "core.fsmonitor=false", *args]
    environment = {
        **os.environ,
        **(extra_environment or {}),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {args[0]} timed out after {timeout:g} seconds"
        ) from exc

    if result.returncode != 0:
        message = os.fsdecode(result.stderr).strip().splitlines()
        detail = message[-1] if message else f"exit code {result.returncode}"
        raise GitError(
            f"git {args[0]} failed: {detail}", returncode=result.returncode
        )

    return result.stdout


@dataclass(frozen=True)
class Repository:
    root: Path
    timeout: float = GIT_TIMEOUT_SECONDS

    def run(
        self,
        *args: str,
        stdin: bytes | None = None,
        environment: dict[str, str] | None = None,
    ) -> bytes:
        return _run_git(list(args), self.root, self.timeout, stdin, environment)

    @contextmanager
    def _private_index(self) -> Iterator[dict[str, str]]:
        """
        Environment pointing Git at a throwaway copy of the index.

        Porcelain `git diff` may rewrite the index to refresh its stat
        cache, even with GIT_OPTIONAL_LOCKS=0. With a copy, the real
        index is never locked or written.
        """
        index = Path(os.fsdecode(self.run("rev-parse", "--git-path", "index").strip()))
        if not index.is_absolute():
            index = self.root / index

        with tempfile.TemporaryDirectory(prefix="whatif-index-") as temporary:
            if not index.is_file():
                yield {}
                return

            copy = Path(temporary) / "index"
            # copy2 keeps the index's mtime. Git compares it with file
            # mtimes to decide which "unchanged" entries to re-read
            # ("racy git"); a fresh mtime would hide same-size edits.
            shutil.copy2(index, copy)
            yield {"GIT_INDEX_FILE": str(copy)}

    def resolve_commit(self, ref: str) -> str:
        """Return the full commit ID that ref names."""
        validate_ref(ref)

        try:
            output = self.run(
                "rev-parse", "--verify", "--quiet", "--end-of-options",
                f"{ref}^{{commit}}",
            )
        except GitError as exc:
            raise GitError(f"{ref!r} is not a valid commit") from exc

        return output.decode("ascii").strip()

    def merge_base(self, first: str, second: str) -> str:
        validate_ref(first)
        validate_ref(second)

        try:
            output = self.run("merge-base", "--end-of-options", first, second)
        except GitError as exc:
            raise GitError(
                f"{first} and {second} have no common ancestor"
            ) from exc

        return output.decode("ascii").strip()

    def working_tree_changes(
        self,
        base: str,
        include_untracked: bool,
        allow_filters: bool = False,
    ) -> list[FileChange]:
        """
        Compare working-tree file contents with the base commit.

        Staged edits are included because the working tree contains
        them; if a staged edit was later reverted on disk, the file on
        disk is what counts.

        Raises UnsafeGitFilters, before running the comparison, if a
        clean/process filter applies to a tracked file and
        allow_filters is False. Submodule working trees are never
        inspected (their own filters could run); new submodule commits
        are still reported.
        """
        validate_ref(base)

        if not allow_filters:
            risks = self.external_filter_risks()
            if risks:
                raise UnsafeGitFilters(risks)

        with self._private_index() as environment:
            changes = parse_raw_diff(self.run(
                "diff", "--raw", "-z", "-M", "--no-ext-diff", "--no-color",
                "--no-relative", "--ignore-submodules=dirty",
                "--end-of-options", base, "--",
                environment=environment,
            ))

        # Diffing against a commit reports a conflicted file as plain
        # "M"; only the index knows it is unmerged.
        unmerged = set(self.unmerged_files())
        changes = [
            replace(change, status="unmerged")
            if change.path in unmerged else change
            for change in changes
        ]
        changes.extend(
            FileChange("unmerged", path)
            for path in sorted(unmerged - {change.path for change in changes})
        )

        if include_untracked:
            changes.extend(
                FileChange("untracked", path)
                for path in self._untracked_files()
            )

        return sorted(changes, key=lambda change: change.path)

    def configured_filters(self) -> dict[str, set[str]]:
        """
        Filter driver -> configured commands ("clean", "smudge",
        "process") across all of Git's configuration sources.
        """
        try:
            output = self.run(
                "config", "-z", "--get-regexp",
                r"^filter\..+\.(clean|smudge|process)$",
            )
        except GitError as exc:
            # Exit code 1 means "no matching keys"; anything else means
            # the configuration could not be read, so fail closed.
            if exc.returncode == 1:
                return {}
            raise

        drivers: dict[str, set[str]] = {}

        for entry in output.split(b"\0"):
            if not entry:
                continue
            key = os.fsdecode(entry.partition(b"\n")[0])
            driver, _, command = key[len("filter."):].rpartition(".")
            drivers.setdefault(driver, set()).add(command)

        return drivers

    def filter_drivers(
        self,
        paths: list[str],
        source: str | None = None,
    ) -> dict[str, str]:
        """
        Path -> filter driver named by its `filter` attribute. Attributes
        come from the working tree, or from the source commit if given.
        """
        if not paths:
            return {}

        args = ["check-attr", "-z", "--stdin"]
        if source is not None:
            validate_ref(source)
            args.append(f"--source={source}")
        args.append("filter")

        output = self.run(
            *args, stdin=b"".join(os.fsencode(p) + b"\0" for p in paths)
        )
        fields = output.split(b"\0")
        drivers: dict[str, str] = {}

        for index in range(0, len(fields) - 1, 3):
            path, _, value = fields[index:index + 3]
            driver = os.fsdecode(value)
            if driver not in ("unspecified", "unset", "set"):
                drivers[os.fsdecode(path)] = driver

        return drivers

    def external_filter_risks(self) -> list[tuple[str, str]]:
        """
        (path, driver) for tracked files whose filter has a clean or
        process command, which Git may run while comparing the file.
        """
        drivers = {
            driver
            for driver, commands in self.configured_filters().items()
            if commands & {"clean", "process"}
        }
        if not drivers:
            return []

        tracked = sorted(set(parse_nul_list(self.run("ls-files", "-z", "--cached"))))
        return sorted(
            (path, driver)
            for path, driver in self.filter_drivers(tracked).items()
            if driver in drivers
        )

    def visible_files(self, include_untracked: bool) -> list[str]:
        """Tracked files present on disk, plus untracked non-ignored ones."""
        tracked = parse_nul_list(self.run("ls-files", "-z", "--cached"))
        files = set(tracked)

        if include_untracked:
            files.update(self._untracked_files())

        return sorted(
            path for path in files
            if (self.root / path).is_file()
        )

    def export_python_snapshot(
        self,
        commit: str,
        destination: Path,
    ) -> list[SnapshotProblem]:
        """
        Write the commit's Python and project-marker files under
        destination, reading blobs directly from the object database.

        Unlike `git archive`, export-ignore and export-subst attributes
        do not apply, so the files are the committed blobs. Symlinks are
        materialized as copies of their target when the target is a
        regular file inside the same commit. Files that cannot be
        reproduced faithfully are returned as problems, not written:
        symlinks leaving the tree, loops, files checked out through a
        smudge/process filter, and paths that collide on disk.
        """
        validate_ref(commit)

        listing = self.run("ls-tree", "-r", "-z", "--end-of-options", commit)
        entries: dict[str, tuple[str, str]] = {}

        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, _, object_id = meta.decode("ascii").split(" ")
            entries[os.fsdecode(raw_path)] = (mode, object_id)

        links = self._read_blobs([
            object_id for mode, object_id in entries.values()
            if mode == _SYMLINK_MODE
        ])

        wanted: list[tuple[str, str, str]] = []   # path, source path, blob
        problems: list[SnapshotProblem] = []

        for path in sorted(entries):
            name = PurePosixPath(path).name
            if not (name.endswith(".py") or name in PROJECT_MARKERS):
                continue

            source, problem = _resolve_tree_path(path, entries, links)
            if problem is not None:
                problems.append(SnapshotProblem(path, problem))
            else:
                wanted.append((path, source, entries[source][1]))

        checkout_drivers = {
            driver
            for driver, commands in self.configured_filters().items()
            if commands & {"smudge", "process"}
        }
        if checkout_drivers:
            # Git filters the regular file that is checked out, never a
            # symlink blob, so the resolved source decides.
            attributes = self.filter_drivers(
                sorted({source for _, source, _ in wanted}), source=commit
            )
            faithful = []
            for path, source, object_id in wanted:
                driver = attributes.get(source)
                if driver in checkout_drivers:
                    problems.append(SnapshotProblem(path, (
                        f"checked out through Git filter '{driver}', so the "
                        "committed blob is not the source on disk; not analyzed"
                    )))
                else:
                    faithful.append((path, source, object_id))
            wanted = faithful

        contents = self._read_blobs([object_id for _, _, object_id in wanted])
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()

        for path, _, object_id in wanted:
            target = safe_join(destination, path)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.resolve().is_relative_to(root):
                    raise GitError(f"Refusing to write outside the snapshot: {path!r}")
                # Exclusive creation: never overwrite another tree entry,
                # e.g. pkg/A.py and pkg/a.py on a case-insensitive disk.
                with open(target, "xb") as handle:
                    handle.write(contents[object_id])
            except (FileExistsError, NotADirectoryError, IsADirectoryError):
                problems.append(SnapshotProblem(path, (
                    "collides with another path in the commit on this file "
                    "system (case-insensitive names?); not analyzed"
                )))

        return sorted(problems, key=lambda problem: problem.path)

    def unmerged_files(self) -> list[str]:
        """Paths with merge conflicts recorded in the index."""
        output = self.run("ls-files", "-z", "--unmerged")
        return sorted({
            os.fsdecode(entry.partition(b"\t")[2])
            for entry in output.split(b"\0")
            if entry
        })

    def _untracked_files(self) -> list[str]:
        return parse_nul_list(self.run(
            "ls-files", "-z", "--others", "--exclude-standard",
        ))

    def _read_blobs(self, object_ids: list[str]) -> dict[str, bytes]:
        if not object_ids:
            return {}

        unique = list(dict.fromkeys(object_ids))
        output = self.run(
            "cat-file", "--batch",
            stdin="".join(f"{object_id}\n" for object_id in unique).encode("ascii"),
        )

        blobs: dict[str, bytes] = {}
        position = 0

        for object_id in unique:
            end = output.index(b"\n", position)
            header = output[position:end].decode("ascii").split(" ")
            position = end + 1

            if len(header) != 3 or header[1] != "blob":
                raise GitError(f"Could not read Git object {object_id}")

            size = int(header[2])
            blobs[object_id] = output[position:position + size]
            position += size + 1

        return blobs


def safe_join(destination: Path, relative: str) -> Path:
    """Join a repository path under destination, refusing escapes."""
    parts = relative.split("/")

    if any(part in ("", ".", "..") or "\0" in part for part in parts):
        raise GitError(f"Refusing unsafe repository path: {relative!r}")

    return destination.joinpath(*parts)


def _resolve_tree_path(
    path: str,
    entries: dict[str, tuple[str, str]],
    links: dict[str, bytes],
) -> tuple[str, str | None]:
    """
    Follow symlinks inside a commit's tree without touching the disk.
    Returns the regular file that path stands for, or a problem.
    """
    current = path
    seen: set[str] = set()

    for _ in range(_MAX_SYMLINK_HOPS + 1):
        mode, object_id = entries[current]

        if mode in _REGULAR_FILE_MODES:
            return current, None

        if mode != _SYMLINK_MODE:
            return current, f"not a regular file (mode {mode}); not analyzed"

        if current in seen:
            return current, "symlink loop; not analyzed"
        seen.add(current)

        target = os.fsdecode(links[object_id])
        if target.startswith("/"):
            return current, "symlink points outside the repository; not analyzed"

        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(current), target)
        )
        if resolved in (".", "..") or resolved.startswith("../"):
            return current, "symlink points outside the repository; not analyzed"

        if resolved not in entries:
            return current, (
                f"symlink target {resolved} is not a file in the commit "
                "(missing, or a directory); not analyzed"
            )

        current = resolved

    return current, (
        f"symlink chain longer than {_MAX_SYMLINK_HOPS} links; not analyzed"
    )


def open_repository(
    path: str | Path,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Repository:
    """
    Find the repository containing path, after checking that the
    installed Git meets MINIMUM_GIT_VERSION.
    """
    path = Path(path).resolve()

    if not path.is_dir():
        raise GitError(f"Not a directory: {path}")

    require_supported_git(
        os.fsdecode(_run_git(["--version"], path, timeout))
    )

    try:
        output = _run_git(["rev-parse", "--show-toplevel"], path, timeout)
    except GitError as exc:
        if "timed out" in str(exc) or "not found" in str(exc):
            raise
        raise GitError(f"Not inside a Git repository: {path}") from exc

    root = Path(os.fsdecode(output.strip())).resolve()
    return Repository(root=root, timeout=timeout)
