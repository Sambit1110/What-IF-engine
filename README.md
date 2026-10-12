# WhatIf Engine

WhatIf Engine investigates the potential impact of software changes.

It reads Python source with the `ast` parser and never imports or executes
the code it analyzes.

## Requirements

- Python 3.10 or newer, for the syntax the code uses. Only Python 3.14.8
  has been tested. There are no third-party dependencies.

## Commands

Activate the project's virtual environment, then run from the project root:

    PYTHONPATH=src python -m whatif_engine scan [PATH]
    PYTHONPATH=src python -m whatif_engine graph [PATH]
    PYTHONPATH=src python -m whatif_engine impact MODULE [PATH]

`PATH` defaults to the current directory.

- `scan` lists every Python file and its imports exactly as written,
  and reports files that cannot be parsed.
- `graph` resolves those imports into dependencies between the project's
  own modules. External packages are left out.
- `impact` lists the modules that depend on `MODULE` directly
  (distance 1) or through other modules (distance 2 and above),
  for example `impact whatif_engine.analyzer`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Analysis completed without errors. Warnings may still be printed. |
| 1 | Some files could not be parsed or read. For `impact`, the result may be incomplete, because an unparsed file could depend on the target. |
| 2 | Usage error: unknown command or module, or `PATH` is not a directory. The message goes to stderr. `impact` may already have printed graph warnings on stdout. |

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
- Git-based change detection is not implemented yet.

## Tests

    PYTHONPATH=src python -m unittest discover -s tests -v
