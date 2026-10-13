# WhatIf Engine

WhatIf Engine investigates the potential impact of software changes.

It reads Python source with the `ast` parser and never imports or executes
the code it analyzes.

## Requirements

| | Supported | Tested locally | In CI |
|---|---|---|---|
| Python | 3.11 or newer (`requires-python = ">=3.11"`) | 3.14.8 | 3.11, 3.12, 3.13, 3.14 on Ubuntu; 3.14 on macOS |
| Operating system | Linux and macOS | macOS | Ubuntu and macOS |
| Git, for `changes` only | 2.40.0 or newer | 2.54.0 | the runner's Git |

- **CI:** the workflow in `.github/workflows/ci.yml` runs on every push
  and pull request. It has not run yet, because the repository has not
  been pushed anywhere.
- **Windows** is untested and not supported for now. The test suite
  relies on POSIX shell scripts, named pipes and symlinks.
- **Python 3.10** reached end of life on 2026-10-01, and the test suite
  uses `tomllib` (3.11+), so 3.11 is the minimum.
- Git 2.40.0 added `git check-attr --source`, which `changes` uses to
  read a commit's filter attributes. Older Git is refused up front, with
  exit code 2 and a message naming the version found. `scan`, `graph`
  and `impact` do not use Git.
- There are no runtime dependencies.

## Installation

Install into a virtual environment, never into the system Python:

    python3 -m venv .venv
    .venv/bin/python -m pip install -e .

This installs the `whatif` command into `.venv/bin`.
- `-e` makes an editable install: the environment uses the source in
  `src/`, so code changes take effect without reinstalling.
- `.venv/bin/python -m pip install .` makes a regular install instead.

Building needs one build-time package, `flit_core` (`>=3.12,<5`; small,
with no dependencies). pip downloads it into a temporary build
environment; it is not installed into `.venv`. Installing therefore
needs access to PyPI, or a local copy of `flit_core`.

## Commands

With the environment activated (`source .venv/bin/activate`), or by
calling `.venv/bin/whatif` directly:

    whatif scan [PATH]
    whatif graph [PATH]
    whatif impact MODULE [PATH]
    whatif changes [PATH] [--base REF] [--merge-base] [--no-untracked] [--allow-git-filters] [--json]

`PATH` defaults to the current directory, and `whatif` works from any
directory.
- `python -m whatif_engine ...` is equivalent to `whatif ...`.
- Without installing, run `PYTHONPATH=src python -m whatif_engine ...`
  from the repository root.

- `scan` lists every Python file and its imports exactly as written,
  and reports files that cannot be parsed.
- `graph` resolves those imports into dependencies between the project's
  own modules. External packages are left out.
- `impact` lists the modules that depend on `MODULE` directly
  (distance 1) or through other modules (distance 2 and above),
  for example `impact whatif_engine.analyzer`.
- `changes` asks Git what changed, then predicts which modules and tests
  are affected. See [Change analysis](#change-analysis).

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Analysis completed without errors. Warnings may still be printed. |
| 1 | Some files could not be parsed or read. For `impact` and `changes`, the result may be incomplete, because an unparsed file could depend on what changed. |
| 2 | The analysis did not run: usage or Git error, or refused for safety. Examples: unknown command or module; `PATH` is not a directory; not inside a Git repository; invalid `--base`; `git` not installed, older than 2.40.0, or timed out; a Git filter would run without `--allow-git-filters`. The message goes to stderr. `changes` prints nothing on stdout in this case. `impact` may already have printed graph warnings on stdout. |

### Errors and warnings

- **Errors** are files that could not be analyzed. Their dependencies are
  unknown. This includes any `*.py` path that is not a regular file,
  such as a named pipe, device, socket or directory. Those are reported
  as `not a regular file (<kind>); not read` and never opened for
  reading in a way that could block. A symlink to a regular file is
  read normally.
- **Warnings** point out spots where the tool had to make a choice or saw
  code Python would reject:
  - a file shadowed by another file with the same module name
  - a relative import that climbs above the top-level package

## Change analysis

    whatif changes [PATH] [--base REF] [--merge-base] [--no-untracked] [--allow-git-filters] [--json]

### What is compared

| Invocation | Compares the working tree with |
|------------|--------------------------------|
| `changes` | `HEAD`: your uncommitted work, staged or not |
| `changes --base REF` | the commit `REF` names, e.g. `main` or `v1.2` |
| `changes --base REF --merge-base` | the merge base of `HEAD` and `REF`: only what this branch changed since it split from `REF`, plus uncommitted work |

- **Working tree** means the files on disk. Staged edits count because
  they are on disk too. If you staged an edit and then reverted it on
  disk, the file on disk is what gets analyzed.
- **Untracked files** that Git does not ignore are included by default.
  `--no-untracked` leaves them out of the analysis entirely, not just
  out of the change list.
- **Ignored files** are never reported or analyzed. Here the graph
  covers only files Git can see; the `graph` command scans every `.py`
  file on disk.
- `--merge-base` requires `--base`.
- `REF` must name a commit. Refs that start with `-` or contain
  whitespace or control characters are rejected before Git sees them.
  Git then verifies the ref with `rev-parse --verify --end-of-options`.

### What it reports

Every report keeps three kinds of statement apart.

- **Observed** facts, from Git and the source:
  - changed files, with status `added`, `modified`, `deleted`,
    `renamed`, `copied`, `type-changed`, `unmerged` or `untracked`
  - the module each changed file is
  - modules that existed at the base but not in the working tree
- **Predicted** outcomes, from static analysis:
  - affected modules, with distance and the shortest dependency chain
    to a changed or removed module
  - likely broken imports: import statements, with line numbers, that
    still name a deleted or renamed module
  - suggested tests: changed test files, then test files that depend on
    changed code. Test files are `test_*.py` and `*_test.py`. This is a
    static estimate, not an exhaustive list.
  - whether to run the full test suite, and why
- **Unknown** impact: changed files that were not analyzed, such as
  non-Python files, files outside `PATH` and Git submodules. Each one
  says why.
- **Errors and warnings:**
  - Errors mean the analysis is incomplete, and set exit code 1.
  - Warnings flag conflicts and shadowed files. A warning marked
    `introduced` (shown as `[new]`) does not occur at the base.

Running the full suite is recommended when any of these hold:
- the analysis is incomplete
- a merge conflict is unresolved
- a non-Python, non-documentation file changed (`*.md`, `*.rst` and
  `docs/` count as documentation)
- a Python file outside `PATH` changed
- a submodule changed
- any module uses dynamic imports

### Deletions and renames

When a Python file was added, deleted, renamed, copied or changed type,
WhatIf also analyzes the base commit:
- It writes the base commit's Python and project-marker files into a
  temporary directory, reading the committed blobs straight from Git's
  object database. `git archive` is not used, so `export-ignore` cannot
  hide a tracked file and `export-subst` cannot substitute content.
- It builds the graph from there, so modules that disappeared still
  have dependents.
- Those dependents are reported as `(base snapshot)` in text output and
  `"graph": "base"` in JSON.

The snapshot never guesses. These base files are left out, reported as
errors, and make the report incomplete (exit 1):
- a file checked out through a Git filter with a `smudge` or `process`
  command (for example Git LFS). Its committed blob is not the source
  on disk, and that command is never run.
- a symlink whose target leaves the repository, is missing, is a
  directory, loops, or takes more than 8 links to resolve
- a path that collides with another on disk, such as `pkg/A.py` and
  `pkg/a.py` on a case-insensitive file system

A symlink to a regular file inside the same commit is written as a copy
of its target, never as a symlink. Paths containing `..`, `.` or empty
components, or absolute paths, are refused outright. Files are created
exclusively, never overwritten, so no path can escape or replace
another inside the temporary directory.

An import counts as likely broken only if the name no longer resolves to
anything:
- If you delete `pkg/__init__.py`, `pkg` becomes a namespace package.
  `from pkg import submodule` still works and is not flagged.
  `from pkg import NAME` is flagged when `NAME` was defined in the
  deleted initializer.

### Output formats

- **Text** (default) is for people.
- **`--json`** prints a single JSON document on stdout:
  - The format has `"schema_version": 1`.
  - Nothing else goes to stdout.
  - A failed run (exit 2) prints nothing on stdout and an error on stderr.

Top-level JSON keys:

| Key | Content |
|-----|---------|
| `schema_version`, `tool` | format version, tool name and version |
| `repository`, `scan_path` | repository root, and `PATH` relative to it (`.` for the root) |
| `base` | `ref`, the full `commit` compared against, `merge_base_with` (or `null`) |
| `include_untracked`, `allow_git_filters`, `complete` | options in effect; `complete` is `false` when there are errors |
| `observed` | `changes`, `changed_modules`, `removed_modules` |
| `predicted` | `note`, `affected_modules` (with `distance`, `relationship`, `chain`, `graph`), `likely_broken_imports`, `suggested_tests`, `run_full_suite` (`recommended`, `reasons`) |
| `unknown` | changes that were not analyzed, each with a `reason` |
| `errors`, `warnings` | `message`, `module`, `path`; warnings also carry `introduced` (`true`, `false`, or `null` when the base was not analyzed) |

All paths in the report are relative to the repository root.

### Safety

- Repository code is parsed, never imported or run.
- Git is run with an argument list, never through a shell, and with a
  30-second timeout per command.
- `.git/index` is never written or locked:
  - The comparison runs against a temporary copy of the index
    (`GIT_INDEX_FILE`), because `git diff` can refresh the index's stat
    cache even with optional locks disabled.
  - `GIT_OPTIONAL_LOCKS=0` is set as well.
- A repository-configured `core.fsmonitor` command is never started.
- Output is read with `-z`, `--no-color` and `--no-relative`, so user
  settings and unusual file names cannot change how it is parsed.

#### Git filters

To compare working-tree files with the base, Git may run a filter's
`clean` or `process` command. Such commands come from Git
configuration, such as `filter.lfs.process`, and are applied through
the `filter` attribute. Before comparing, WhatIf checks for them:
1. It reads every configured `filter.<driver>.clean` and
   `filter.<driver>.process` from all of Git's configuration sources.
2. It asks `git check-attr` which tracked files use one of those
   drivers, with attributes taken from the working tree, as the
   comparison itself would.
3. If any file does, `changes` refuses to run. It exits 2 and prints
   the drivers and example files on stderr, in both text and JSON mode.

A filter that is configured but used by no tracked file does not block
the analysis. Neither does a `smudge`-only filter, since comparing files
never runs `smudge`. If the configuration cannot be read, the analysis
is refused rather than assumed safe.

`--allow-git-filters` lets Git run those commands. Use it only for
repositories and Git configuration you trust. The report then says
`Git filters: allowed`, sets `"allow_git_filters": true` and warns about
each driver that may have run.

Submodule working trees are never inspected, because their own filters
would run. New submodule commits are still reported; uncommitted
changes inside a submodule are not.

## How module names are resolved

- The project root and `src/` are both treated as import roots, so
  `src/pkg/core.py` is the module `pkg.core`.
- If `PATH` is itself a package, meaning it contains `__init__.py`, the
  enclosing package names are kept. Scanning `src/whatif_engine` still
  yields `whatif_engine.cli`.
- A directory containing `pyproject.toml`, `setup.py`, `setup.cfg` or
  `.git` is treated as a project root, never as a package, even if it
  has an `__init__.py`. Its `src/` stays an import root, and its own
  `__init__.py` is ignored, because Python never runs it when the project
  is run from there.
- A package wins over a same-named module in the same directory, as in
  Python (`pkg/__init__.py` over `pkg.py`).
- If the same module exists at the project root and under `src/`, the
  root copy is analyzed and a warning is printed. Python's real choice
  depends on `sys.path` order.
- A file in a plain directory, one without `__init__.py` such as
  `tests/`, can import its neighbours by bare name (`import helpers`),
  as it can under `unittest discover -s tests`.
- Importing `a.b.c` also records dependencies on `a` and `a.b`, because
  Python runs their `__init__.py` files first.
- Every module depends on its nearest parent package, even with no
  imports, because Python runs that package's `__init__.py` before the
  module. `graph` labels these edges `(parent package initializer)`.
  Changing `pkg/__init__.py` therefore affects `pkg.leaf` at distance 1
  and `pkg.sub.leaf` at distance 2, through `pkg.sub`. Directories
  without `__init__.py` are skipped when finding the nearest parent.

## Limitations

This is static analysis, not proof of runtime behavior.

- Dynamic imports (`importlib.import_module`, `__import__`) are not
  resolved. Imports inside functions or `if` blocks are counted as if
  they always run.
- `from pkg import name` links to the module `pkg.name` only if that
  file exists. Otherwise it links to `pkg`.
- Only the project root and `src/` are import roots. Other layouts, such
  as `lib/` or monorepos, and imports written as `from src.pkg import ...`
  are not resolved.
- Bare-name imports between neighbouring files assume their directory is
  on `sys.path`. In namespace packages this can report a dependency
  Python would not make.
- Not supported: a repository whose root is itself meant to be imported
  as a package, with code that imports `reponame.module`. If the root
  has a project marker, such imports are not resolved. Scan the
  repository from a directory without markers instead.
- Project roots are recognized by the marker files above. That is a
  convention, not a Python rule: Python's real behavior depends on how
  `sys.path` is set when the code runs.

Change analysis has further limits:

- Only Python imports are followed. Changes to configuration, data,
  templates or dependencies (`pyproject.toml`, `requirements.txt`) are
  reported as unknown, and the full suite is recommended.
- Dynamic imports (`importlib.import_module`, `__import__`) are detected
  but not followed. If any module uses them, the full suite is
  recommended.
- Tests that reach code without importing it are not suggested. That
  includes subprocesses, fixtures, plugins, data files and pytest
  `conftest.py` fixtures.
- The filter check covers `clean` and `process` commands, which are
  the commands Git documents for comparing file contents. Git also
  reads other settings that can name commands, such as credential
  helpers and `core.sshCommand`. None of the read-only commands used
  here should need them, but WhatIf relies on Git for that.
- Uncommitted changes inside Git submodules are not detected.
- A repository with no commits has no `HEAD` to compare against, so it
  is rejected.
- In the working tree, a symlinked `.py` file is analyzed through its
  target, even outside the repository, as Python would import it. Only
  base-snapshot symlinks are restricted to the commit.
- Paths quoted inside graph warning messages are relative to `PATH`.
  Paths in the report's fields are relative to the repository root.

## Development and testing

After the editable install, run the test suite from the repository root.
It uses only the standard library's `unittest`:

    .venv/bin/python -m unittest discover -s tests -v

Without an install, `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`
runs the same suite. The packaging tests that need an installed
`whatif-engine` or `whatif` command are then skipped, each with the
reason.

- **Git tests** create temporary repositories with `git` from `PATH`.
  `HOME` and the global and system Git configuration are hidden from
  them. They are skipped if `git` is not installed.
- **Packaging tests** check the metadata in `pyproject.toml`, the
  license, the registered `whatif` entry point and the installed
  version. They also run every command through the installed `whatif`
  from a temporary directory outside the repository, without
  `PYTHONPATH`. They also check the CI workflow's safety settings.

### Continuous integration

`.github/workflows/ci.yml` runs on Python 3.11–3.14 on Ubuntu and on
Python 3.14 on macOS. Each job:
1. makes a regular (non-editable) install into a fresh virtual
   environment, so packaging mistakes surface
2. runs the full test suite
3. runs `whatif --help`, `scan`, `graph`, `impact` and `changes --json`
   from outside the repository

The workflow's token is read-only, and checkout credentials are not
kept. It uses only the official `actions/checkout` and
`actions/setup-python`, each pinned to a full commit SHA. Installing
downloads the build-time `flit_core` from PyPI, as a local install does.

## License

WhatIf Engine is released under the MIT License. See [LICENSE](LICENSE).
