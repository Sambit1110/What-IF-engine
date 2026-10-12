"""
Static impact analysis of the changes in a Git working tree.

The report separates three kinds of statement:

- observed: what Git reports (changed files) and what the source
  says (module names, import lines);
- predicted: what static analysis infers (affected modules, likely
  broken imports, suggested tests);
- diagnostics: errors (analysis incomplete) and warnings (choices made
  or risks spotted), plus changes whose impact is unknown.

Repository code is parsed with `ast`, never imported or executed.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from whatif_engine.gitrepo import (
    GIT_TIMEOUT_SECONDS,
    FileChange,
    Repository,
    SnapshotProblem,
    open_repository,
)
from whatif_engine.graph import (
    DependencyGraph,
    build_dependency_graph,
    trace_impact,
)

# Changes to these never call for the full test suite on their own.
DOCUMENTATION_SUFFIXES = (".md", ".rst")

# Statuses that can add or remove modules, so the base must be analyzed.
_STRUCTURAL_STATUSES = {
    "added", "deleted", "renamed", "copied", "untracked", "type-changed",
}

_LISTED_NAMES = 5


@dataclass(frozen=True)
class BaseCommit:
    # The ref as given, e.g. "HEAD" or "main"
    ref: str

    # Full ID of the commit actually compared against
    commit: str

    # Set when commit is the merge base of HEAD and this ref
    merge_base_with: str | None = None


@dataclass(frozen=True)
class ChangedFile:
    status: str
    path: str                       # repository-relative
    old_path: str | None = None     # renames and copies
    similarity: int | None = None   # renames and copies, percent
    module: str | None = None       # module in the working tree
    old_module: str | None = None   # module at the base (deleted/renamed)
    note: str | None = None         # why the file was not analyzed, if so


@dataclass(frozen=True)
class AffectedModule:
    module: str
    path: str
    distance: int

    # Shortest dependency chain, dependent first, ending at a changed or
    # removed module
    chain: tuple[str, ...]

    # "working-tree", or "base" when the chain only exists in the base
    # snapshot (dependents of deleted or renamed modules)
    graph: str


@dataclass(frozen=True)
class LikelyBrokenImport:
    module: str
    path: str
    line: int
    removed_module: str
    reason: str


@dataclass(frozen=True)
class SuggestedTest:
    module: str
    path: str
    reason: str
    chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnostic:
    message: str
    module: str | None = None
    path: str | None = None

    # Warnings only: True if absent from the base snapshot, None when
    # the base was not analyzed
    introduced: bool | None = None


@dataclass(frozen=True)
class ChangeReport:
    repository: str
    scan_path: str
    base: BaseCommit
    include_untracked: bool

    # True if Git was allowed to run configured clean/process filters
    allow_git_filters: bool

    # Observed
    changes: tuple[ChangedFile, ...]
    changed_modules: tuple[str, ...]
    removed_modules: tuple[str, ...]

    # Predicted
    affected: tuple[AffectedModule, ...]
    likely_broken_imports: tuple[LikelyBrokenImport, ...]
    suggested_tests: tuple[SuggestedTest, ...]
    full_suite_reasons: tuple[str, ...]

    # Diagnostics
    errors: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]

    @property
    def complete(self) -> bool:
        return not self.errors

    @property
    def unknown(self) -> tuple[ChangedFile, ...]:
        """Changes whose impact was not analyzed."""
        return tuple(change for change in self.changes if change.note)


def _is_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:_LISTED_NAMES])
    extra = len(names) - _LISTED_NAMES
    return f"{shown} and {extra} more" if extra > 0 else shown


class _Scope:
    """Translate between repository-relative and scan-relative paths."""

    def __init__(self, repository: Repository, scan_root: Path):
        relative = scan_root.relative_to(repository.root).as_posix()
        self.scan_path = relative

    def to_scan(self, repo_path: str) -> str | None:
        if self.scan_path == ".":
            return repo_path
        try:
            return PurePosixPath(repo_path).relative_to(self.scan_path).as_posix()
        except ValueError:
            return None

    def to_repo(self, scan_path: str) -> str:
        if self.scan_path == ".":
            return scan_path
        return f"{self.scan_path}/{scan_path}"


def _module_path(graph: DependencyGraph, scope: _Scope, module: str) -> str:
    """Repository-relative path of a module in graph."""
    return scope.to_repo(graph.modules[module])


def _resolve_base(
    repository: Repository,
    base: str,
    merge_base: bool,
) -> BaseCommit:
    if not merge_base:
        return BaseCommit(ref=base, commit=repository.resolve_commit(base))

    target = repository.resolve_commit(base)
    head = repository.resolve_commit("HEAD")
    return BaseCommit(
        ref=base,
        commit=repository.merge_base(head, target),
        merge_base_with=base,
    )


def _base_graph(
    repository: Repository,
    scope: _Scope,
    commit: str,
) -> tuple[DependencyGraph, list[SnapshotProblem]]:
    """Analyze the base commit from a snapshot of its Python files."""
    with tempfile.TemporaryDirectory(prefix="whatif-base-") as temporary:
        snapshot = Path(temporary).resolve()
        problems = repository.export_python_snapshot(commit, snapshot)
        root = snapshot if scope.scan_path == "." else snapshot / scope.scan_path

        if not root.is_dir():
            return DependencyGraph(modules={}, dependencies={}, errors={}), problems

        return build_dependency_graph(root, project_root=snapshot), problems


def _describe(
    path: str,
    scope: _Scope,
    modules_by_path: dict[str, str],
    shadowed: dict[str, str],
    graph: DependencyGraph,
) -> tuple[str | None, str | None]:
    """Module for a repository path, or a note saying why there is none."""
    if scope.to_scan(path) is None:
        return None, "outside the scanned path; impact not analyzed"

    if not path.endswith(".py"):
        return None, "not a Python file; impact not analyzed"

    if path in modules_by_path:
        return modules_by_path[path], None

    if path in shadowed:
        winner = shadowed[path]
        return None, (
            f"shadowed: module {winner} is loaded from "
            f"{scope.to_repo(graph.modules[winner])}, not this file"
        )

    return None, "in an excluded directory; impact not analyzed"


def analyze_changes(
    path: str | Path = ".",
    *,
    base: str = "HEAD",
    merge_base: bool = False,
    include_untracked: bool = True,
    allow_git_filters: bool = False,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> ChangeReport:
    """
    Compare the working tree (which includes staged edits) with a base
    commit and predict the impact of the difference.

    base: commit to compare against.
    merge_base: compare against the merge base of HEAD and base instead,
        i.e. only the changes made on this branch.
    include_untracked: treat untracked, non-ignored files as changes.
    allow_git_filters: let Git run configured clean/process filter
        commands while comparing files. Off by default: the analysis is
        refused (UnsafeGitFilters) if such a filter applies.

    Raises GitError when Git cannot be run, the base is invalid, or Git
    filters would run without permission.
    """
    scan_root = Path(path).resolve()

    if not scan_root.is_dir():
        raise NotADirectoryError(f"Not a directory: {scan_root}")

    repository = open_repository(scan_root, timeout=timeout)
    scope = _Scope(repository, scan_root)
    base_commit = _resolve_base(repository, base, merge_base)
    warnings: list[Diagnostic] = []

    if allow_git_filters:
        risks = repository.external_filter_risks()
        for driver in sorted({driver for _, driver in risks}):
            count = sum(1 for _, d in risks if d == driver)
            warnings.append(Diagnostic(
                f"external Git filter commands may have run (driver "
                f"'{driver}', {count} tracked file(s)): allowed by "
                "--allow-git-filters"
            ))

    file_changes = repository.working_tree_changes(
        base_commit.commit, include_untracked, allow_filters=allow_git_filters
    )

    def is_structural(change: FileChange) -> bool:
        return change.status in _STRUCTURAL_STATUSES and any(
            p is not None and p.endswith(".py") and scope.to_scan(p) is not None
            for p in (change.path, change.old_path)
        )

    before: DependencyGraph | None = None
    snapshot_problems: list[SnapshotProblem] = []

    if any(is_structural(change) for change in file_changes):
        before, snapshot_problems = _base_graph(
            repository, scope, base_commit.commit
        )

    include = {
        scan_path
        for visible in repository.visible_files(include_untracked)
        if (scan_path := scope.to_scan(visible)) is not None
    }
    after = build_dependency_graph(
        scan_root,
        include=include,
        project_root=repository.root,
        removed_modules=before.modules.keys() if before else (),
    )

    def by_path(graph: DependencyGraph) -> dict[str, str]:
        return {scope.to_repo(p): m for m, p in graph.modules.items()}

    after_modules = by_path(after)
    after_shadowed = {scope.to_repo(p): m for p, m in after.shadowed.items()}
    before_modules = by_path(before) if before else {}

    # Observed: what changed, and which modules those files are.
    changes: list[ChangedFile] = []

    for change in file_changes:
        module = old_module = note = None

        if change.is_submodule:
            note = "Git submodule; contents not analyzed"
        elif change.status == "deleted":
            _, note = _describe(change.path, scope, {}, {}, after)
            old_module = before_modules.get(change.path)
            if old_module is not None:
                note = None
        else:
            module, note = _describe(
                change.path, scope, after_modules, after_shadowed, after
            )
            if change.status == "renamed" and change.old_path:
                old_module = before_modules.get(change.old_path)

        changes.append(ChangedFile(
            status=change.status,
            path=change.path,
            old_path=change.old_path,
            similarity=change.similarity,
            module=module,
            old_module=old_module,
            note=note,
        ))

    changed_modules = {c.module for c in changes if c.module}
    removed_modules = (
        sorted(before.modules.keys() - after.modules.keys()) if before else []
    )

    # Predicted: dependents of changed modules in the working tree, and
    # dependents of removed modules in the base snapshot.
    affected: dict[str, AffectedModule] = {}

    for module, chain in trace_impact(after, changed_modules).items():
        affected[module] = AffectedModule(
            module, _module_path(after, scope, module),
            len(chain) - 1, chain, "working-tree",
        )

    if before and removed_modules:
        for module, chain in trace_impact(before, removed_modules).items():
            if module not in after.modules or module in changed_modules:
                continue
            distance = len(chain) - 1
            if module not in affected or distance < affected[module].distance:
                affected[module] = AffectedModule(
                    module, _module_path(after, scope, module),
                    distance, chain, "base",
                )

    ordered_affected = sorted(
        affected.values(), key=lambda entry: (entry.distance, entry.module)
    )

    renamed_to = {
        c.old_module: c.module
        for c in changes
        if c.status == "renamed" and c.old_module and c.module
    }
    broken: list[LikelyBrokenImport] = []

    for importer, missing in sorted(after.missing_imports.items()):
        for removed, line in missing:
            if removed in renamed_to:
                reason = (
                    f"{removed} was renamed to {renamed_to[removed]}; "
                    "this import still uses the old name"
                )
            elif any(m.startswith(removed + ".") for m in after.modules):
                reason = (
                    f"{scope.to_repo(before.modules[removed])} was removed, "
                    f"so {removed} is now a namespace package; names defined "
                    "in its initializer no longer exist"
                )
            else:
                reason = f"{removed} was deleted"

            broken.append(LikelyBrokenImport(
                importer, _module_path(after, scope, importer),
                line, removed, reason,
            ))

    suggested = [
        SuggestedTest(m, _module_path(after, scope, m), "test file changed")
        for m in sorted(changed_modules)
        if _is_test(after.modules[m])
    ]
    suggested.extend(
        SuggestedTest(entry.module, entry.path, "depends on changed code", entry.chain)
        for entry in ordered_affected
        if _is_test(entry.path)
    )

    # Diagnostics.
    errors = [
        Diagnostic(
            f"could not be analyzed: {message}",
            module=module,
            path=_module_path(after, scope, module),
        )
        for module, message in sorted(after.errors.items())
    ]
    # A base file that could not be reproduced faithfully may hide a
    # removed module or its dependents, so the report is incomplete.
    errors.extend(
        Diagnostic(f"base snapshot: {problem.message}", path=problem.path)
        for problem in snapshot_problems
        if scope.to_scan(problem.path) is not None
    )

    unmerged = [c.path for c in changes if c.status == "unmerged"]
    warnings.extend(
        Diagnostic(
            "unresolved merge conflict; the file on disk is analyzed "
            "and may contain conflict markers",
            path=path,
        )
        for path in unmerged
    )
    warnings.extend(
        Diagnostic(f"changed file is {c.note}", path=c.path)
        for c in changes
        if c.note and c.note.startswith("shadowed")
    )

    if before and removed_modules:
        warnings.extend(
            Diagnostic(
                f"base snapshot: could not be analyzed ({message}); "
                "dependents of removed modules may be incomplete",
                module=module,
            )
            for module, message in sorted(before.errors.items())
        )

    before_warnings = (
        {(m, w) for m, messages in before.warnings.items() for w in messages}
        if before else set()
    )
    relevant = changed_modules | affected.keys() | {b.module for b in broken}

    for module, messages in sorted(after.warnings.items()):
        for message in messages:
            introduced = (
                (module, message) not in before_warnings if before else None
            )
            if introduced or module in relevant:
                warnings.append(Diagnostic(
                    message,
                    module=module,
                    path=_module_path(after, scope, module),
                    introduced=introduced,
                ))

    # When the static estimate is not enough.
    reasons: list[str] = []

    if errors:
        reasons.append(
            f"analysis is incomplete: {len(errors)} module(s) could not "
            "be parsed or read"
        )

    if unmerged:
        reasons.append(f"unresolved merge conflicts: {_listed(unmerged)}")

    non_python = [
        c.path for c in changes
        if not c.path.endswith(".py")
        and not c.path.endswith(DOCUMENTATION_SUFFIXES)
        and not c.path.startswith("docs/")
    ]
    if non_python:
        reasons.append(
            f"non-Python files changed and were not analyzed: {_listed(non_python)}"
        )

    outside = [
        c.path for c in changes
        if c.path.endswith(".py") and scope.to_scan(c.path) is None
    ]
    if outside:
        reasons.append(
            "Python files outside the scanned path changed and were not "
            f"analyzed: {_listed(outside)}"
        )

    submodules = [c.path for c in changes if c.note and "submodule" in c.note]
    if submodules:
        reasons.append(f"Git submodules changed: {_listed(submodules)}")

    if after.dynamic_imports:
        reasons.append(
            f"{len(after.dynamic_imports)} module(s) use dynamic imports, "
            "which static analysis cannot follow: "
            f"{_listed(list(after.dynamic_imports))}"
        )

    return ChangeReport(
        repository=str(repository.root),
        scan_path=scope.scan_path,
        base=base_commit,
        include_untracked=include_untracked,
        allow_git_filters=allow_git_filters,
        changes=tuple(changes),
        changed_modules=tuple(sorted(changed_modules)),
        removed_modules=tuple(removed_modules),
        affected=tuple(ordered_affected),
        likely_broken_imports=tuple(broken),
        suggested_tests=tuple(suggested),
        full_suite_reasons=tuple(reasons),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
