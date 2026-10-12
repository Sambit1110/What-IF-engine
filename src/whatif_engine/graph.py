"""Build a static dependency graph from Python source files."""

import ast
import os
from collections import deque
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from whatif_engine.analyzer import IGNORED_DIRS, NotRegularFile, read_source

# Files that mark a directory as a project root, i.e. a sys.path entry
# rather than an importable package, even if it has an __init__.py.
PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")


@dataclass(frozen=True)
class DependencyGraph:
    # Module name -> project-relative source path
    modules: dict[str, str]

    # Importing module -> local modules it depends on
    dependencies: dict[str, tuple[str, ...]]

    # Module name -> parsing or reading error
    errors: dict[str, str]

    # Module name -> non-fatal findings, such as shadowed files or
    # imports Python itself would reject
    warnings: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Module name -> nearest package whose __init__.py Python executes
    # before the module. These edges are included in `dependencies`.
    parent_packages: dict[str, str] = field(default_factory=dict)

    # Project-relative path -> module name it loses to (see warnings)
    shadowed: dict[str, str] = field(default_factory=dict)

    # Importing module -> dependency -> line numbers of the import
    # statements behind that edge. Parent-package edges have none.
    import_lines: dict[str, dict[str, tuple[int, ...]]] = field(
        default_factory=dict
    )

    # Importing module -> (removed module, line) for imports that only
    # resolve to a module listed in `removed_modules`
    missing_imports: dict[str, tuple[tuple[str, int], ...]] = field(
        default_factory=dict
    )

    # Modules calling importlib.import_module or __import__, whose
    # targets static analysis cannot see
    dynamic_imports: tuple[str, ...] = ()


def _enclosing_packages(
    root: Path,
    project_root: Path | None = None,
) -> list[str]:
    """
    Names of the packages that contain the scan root, outermost first.

    Scanning src/pkg/sub, where each level has an __init__.py, gives
    ["pkg", "sub"], so its modules are named pkg.sub.*. The walk stops at
    a project root, whose own __init__.py is never executed when the
    project is run from there.
    """
    names: list[str] = []
    current = root

    while (
        current != project_root
        and (current / "__init__.py").is_file()
        and not any((current / marker).exists() for marker in PROJECT_MARKERS)
        and current.parent != current
    ):
        names.insert(0, current.name)
        current = current.parent

    return names


def _is_package(name: str, modules: dict[str, str]) -> bool:
    return modules.get(name, "").rpartition("/")[2] == "__init__.py"


def _nearest_parent_package(name: str, modules: dict[str, str]) -> str | None:
    """
    The closest enclosing package with an __init__.py. Directories
    without one (namespace packages) execute nothing, so they are skipped.
    """
    parts = name.split(".")

    for end in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:end])
        if _is_package(candidate, modules):
            return candidate

    return None


def _module_name(
    root: Path,
    path: Path,
    enclosing: list[str],
) -> str | None:
    """Convert a source path to a project module name."""
    parts = list(path.relative_to(root).with_suffix("").parts)

    if path.name == "__init__.py":
        parts = parts[:-1]

    if enclosing:
        parts = enclosing + parts
    # Support the common src/ package layout.
    elif parts and parts[0] == "src":
        parts = parts[1:]

    return ".".join(parts) or None


def _precedence(root: Path, path: Path) -> tuple[int, int]:
    """
    Rank files that map to the same module name; lower wins.

    Within one directory Python imports a package before a same-named
    module. Across the project root and src/, the winner depends on
    sys.path order, so the root is assumed to come first, as it does for
    `PYTHONPATH=src python -m ...` run from the project root.
    """
    in_src = path.relative_to(root).parts[0] == "src"
    is_module = path.name != "__init__.py"
    return (int(in_src), int(is_module))


def _shadow_warning(root: Path, chosen: Path, other: Path) -> str:
    chosen_path = chosen.relative_to(root).as_posix()
    other_path = other.relative_to(root).as_posix()

    if _precedence(root, chosen)[0] == _precedence(root, other)[0]:
        return (
            f"{other_path} is shadowed by {chosen_path}; "
            "Python imports the package, not the module"
        )

    return (
        f"{other_path} has the same module name as {chosen_path}; "
        "which one Python imports depends on sys.path order. "
        f"Analyzed {chosen_path} only"
    )


def _resolve_local(name: str, modules: dict[str, str]) -> str | None:
    """Resolve the longest matching local module or package."""
    parts = name.split(".")

    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in modules:
            return candidate

    return None


def _namespaces(modules: Iterable[str]) -> set[str]:
    """
    Every proper dotted prefix of a module name. Directories without an
    __init__.py are still importable as namespace packages (PEP 420).
    """
    prefixes: set[str] = set()

    for name in modules:
        parts = name.split(".")
        prefixes.update(".".join(parts[:end]) for end in range(1, len(parts)))

    return prefixes


def _qualify(
    name: str,
    importable: Collection[str],
    search_prefixes: list[str],
) -> str | None:
    """
    Rewrite an absolute import as seen from the first search location
    that defines its top-level name, mimicking sys.path order.

    importable holds module names and namespace-package names. An empty
    prefix means the project's own import roots.
    """
    head = name.partition(".")[0]

    for prefix in search_prefixes:
        if not prefix:
            if head in importable:
                return name
        elif f"{prefix}.{head}" in importable:
            return f"{prefix}.{name}"

    return None


def _with_parent_packages(name: str, modules: dict[str, str]) -> list[str]:
    """Importing a.b.c also executes a/__init__.py and a/b/__init__.py."""
    parts = name.split(".")
    found = [
        ".".join(parts[:end])
        for end in range(1, len(parts))
        if _is_package(".".join(parts[:end]), modules)
    ]

    return found + [name]


def _is_dynamic_import(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False

    function = node.func
    if isinstance(function, ast.Name):
        return function.id in ("__import__", "import_module")
    if isinstance(function, ast.Attribute):
        return function.attr == "import_module"
    return False


def _import_references(
    node: ast.Import | ast.ImportFrom,
    package_parts: list[str],
) -> tuple[list[tuple[str, bool]], str | None]:
    """
    Module names an import statement may load, each flagged True when it
    is absolute (and so subject to sys.path search), plus a warning when
    the statement is a relative import Python would reject.
    """
    if isinstance(node, ast.Import):
        return [(alias.name, True) for alias in node.names], None

    if node.level:
        keep = len(package_parts) - (node.level - 1)

        # Python raises ImportError when a relative import climbs above
        # the top-level package, including any relative import from a
        # top-level module.
        if keep < 1:
            target = "." * node.level + (node.module or "")
            return [], (
                f"line {node.lineno}: relative import '{target}' goes "
                "beyond the top-level package; Python would raise ImportError"
            )

        base = ".".join(
            part for part in (".".join(package_parts[:keep]), node.module or "")
            if part
        )
        absolute = False
    elif node.module:
        base = node.module
        absolute = True
    else:
        return [], None

    # `from package import name` may load the submodule package.name.
    references = [(base, absolute)]
    references.extend(
        (f"{base}.{alias.name}", absolute)
        for alias in node.names
        if alias.name != "*"
    )
    return references, None


def build_dependency_graph(
    root: str | Path,
    *,
    include: Collection[str] | None = None,
    project_root: str | Path | None = None,
    removed_modules: Collection[str] = (),
) -> DependencyGraph:
    """
    Build import relationships without executing project code.

    include: only analyze these root-relative POSIX paths.
    project_root: treat this directory as the project root when looking
        for packages that enclose `root`.
    removed_modules: module names that no longer exist; imports that
        only resolve to one of them are reported in `missing_imports`.
    """
    root = Path(root).resolve()

    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    paths = []

    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(
            name for name in directories
            if name not in IGNORED_DIRS
        )

        for filename in sorted(filenames):
            path = Path(current) / filename
            if filename.endswith(".py") and (
                include is None
                or path.relative_to(root).as_posix() in include
            ):
                paths.append(path)

    paths.sort(key=lambda p: p.relative_to(root).as_posix())

    if project_root is not None:
        project_root = Path(project_root).resolve()

    enclosing = _enclosing_packages(root, project_root)
    candidates: dict[str, list[Path]] = {}

    for path in paths:
        name = _module_name(root, path, enclosing)
        if name is not None:
            candidates.setdefault(name, []).append(path)

    module_paths: dict[str, Path] = {}
    warnings: dict[str, list[str]] = {}
    shadowed_paths: dict[str, str] = {}

    for name, found in sorted(candidates.items()):
        chosen, *shadowed = sorted(
            found,
            key=lambda p: (_precedence(root, p), p.relative_to(root).as_posix()),
        )
        module_paths[name] = chosen

        for other in shadowed:
            shadowed_paths[other.relative_to(root).as_posix()] = name
            warnings.setdefault(name, []).append(
                _shadow_warning(root, chosen, other)
            )

    modules = {
        name: path.relative_to(root).as_posix()
        for name, path in sorted(module_paths.items())
    }

    parent_packages = {
        name: parent
        for name in modules
        if (parent := _nearest_parent_package(name, modules)) is not None
    }

    namespaces = _namespaces(modules)
    importable = modules.keys() | namespaces

    removed = frozenset(removed_modules) - modules.keys()
    # Resolution universe in which removed modules still exist, used
    # only to recognize imports that now point nowhere.
    with_removed = {**modules, **{name: "" for name in removed}}
    importable_with_removed = importable | removed | _namespaces(removed)

    dependencies: dict[str, tuple[str, ...]] = {}
    import_lines: dict[str, dict[str, tuple[int, ...]]] = {}
    missing_imports: dict[str, tuple[tuple[str, int], ...]] = {}
    dynamic_imports: list[str] = []
    errors: dict[str, str] = {}

    for name, path in sorted(module_paths.items()):
        # Known from the file layout alone, even if parsing fails.
        implicit = (
            (parent_packages[name],) if name in parent_packages else ()
        )

        try:
            source = read_source(path)
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            errors[name] = f"line {exc.lineno}: {exc.msg}"
            dependencies[name] = implicit
            continue
        except NotRegularFile as exc:
            errors[name] = str(exc)
            dependencies[name] = implicit
            continue
        except (OSError, UnicodeError) as exc:
            errors[name] = f"Could not read file: {exc}"
            dependencies[name] = implicit
            continue

        if path.name == "__init__.py":
            package_parts = name.split(".")
        else:
            parent = name.rpartition(".")[0]
            package_parts = parent.split(".") if parent else []

        # Where absolute imports are searched, in sys.path order.
        search_prefixes = [""]

        # A script or test file in a plain (non-package) directory
        # sees its siblings as top-level modules: running it directly
        # or via `unittest discover -s <dir>` puts that directory first
        # on sys.path.
        if (
            path.name != "__init__.py"
            and package_parts
            and not (path.parent / "__init__.py").is_file()
        ):
            search_prefixes.insert(0, ".".join(package_parts))

        # When the scan root is itself a package, its directory may be
        # on sys.path as well, so `import b` can mean <package>.b.
        if enclosing:
            search_prefixes.append(".".join(enclosing))

        lines: dict[str, set[int]] = {}
        missing: set[tuple[str, int]] = set()
        invalid_imports: list[tuple[int, str]] = []

        def lookup(
            reference: str,
            absolute: bool,
            known: dict[str, str],
            known_importable: Collection[str],
        ) -> tuple[str | None, str | None]:
            qualified = (
                _qualify(reference, known_importable, search_prefixes)
                if absolute else reference
            )
            if qualified is None:
                return None, None
            return qualified, _resolve_local(qualified, known)

        def add_reference(reference: str, absolute: bool, line: int) -> None:
            qualified, local = lookup(reference, absolute, modules, importable)

            if local is not None:
                for dependency in _with_parent_packages(local, modules):
                    lines.setdefault(dependency, set()).add(line)

            # An exact module or an existing namespace package still
            # imports. Otherwise, check whether the import used to name
            # a module that has since been removed.
            if (
                removed
                and (local is None or local != qualified)
                and qualified not in namespaces
            ):
                _, previous = lookup(
                    reference, absolute, with_removed, importable_with_removed
                )
                if previous in removed:
                    missing.add((previous, line))

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                references, invalid = _import_references(node, package_parts)

                if invalid is not None:
                    invalid_imports.append((node.lineno, invalid))

                for reference, absolute in references:
                    add_reference(reference, absolute, node.lineno)

            elif _is_dynamic_import(node) and name not in dynamic_imports:
                dynamic_imports.append(name)

        lines.pop(name, None)
        dependencies[name] = tuple(sorted(lines.keys() | set(implicit)))

        if lines:
            import_lines[name] = {
                dependency: tuple(sorted(numbers))
                for dependency, numbers in sorted(lines.items())
            }

        if missing:
            missing_imports[name] = tuple(sorted(missing, key=lambda m: (m[1], m[0])))

        for _, message in sorted(invalid_imports):
            warnings.setdefault(name, []).append(message)

    return DependencyGraph(
        modules=modules,
        dependencies=dependencies,
        errors=errors,
        warnings={
            name: tuple(messages)
            for name, messages in sorted(warnings.items())
        },
        parent_packages=parent_packages,
        shadowed=dict(sorted(shadowed_paths.items())),
        import_lines=import_lines,
        missing_imports=missing_imports,
        dynamic_imports=tuple(sorted(dynamic_imports)),
    )


def trace_impact(
    graph: DependencyGraph,
    targets: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """
    Shortest dependency chain from each module that depends on any of
    targets, e.g. ("tests.test_api", "pkg.api", "pkg.core") means
    tests.test_api depends on pkg.api, which depends on pkg.core.

    Targets themselves are excluded; unknown targets are ignored.
    Ties are broken alphabetically, so results are deterministic.
    """
    reverse: dict[str, set[str]] = {name: set() for name in graph.modules}

    for importer, dependencies in graph.dependencies.items():
        for dependency in dependencies:
            if dependency in reverse:
                reverse[dependency].add(importer)

    seeds = sorted(set(targets) & graph.modules.keys())
    next_hop: dict[str, str | None] = {seed: None for seed in seeds}
    queue = deque(seeds)

    while queue:
        current = queue.popleft()

        for dependent in sorted(reverse[current]):
            if dependent not in next_hop:
                next_hop[dependent] = current
                queue.append(dependent)

    chains: dict[str, tuple[str, ...]] = {}

    for module in next_hop:
        if next_hop[module] is None:
            continue

        chain = [module]
        while (hop := next_hop[chain[-1]]) is not None:
            chain.append(hop)
        chains[module] = tuple(chain)

    return dict(sorted(
        chains.items(),
        key=lambda item: (len(item[1]), item[0]),
    ))


def calculate_impact(
    graph: DependencyGraph,
    target: str,
) -> dict[str, int]:
    """
    Find modules that directly or indirectly depend on target.

    The returned integer is the dependency distance:
    1 = direct dependent, 2 = dependent of a direct dependent, etc.
    """
    if target not in graph.modules:
        raise ValueError(f"Unknown project module: {target}")

    reverse: dict[str, set[str]] = {
        name: set() for name in graph.modules
    }

    for importer, dependencies in graph.dependencies.items():
        for dependency in dependencies:
            if dependency in reverse:
                reverse[dependency].add(importer)

    distances = {target: 0}
    queue = deque([target])

    while queue:
        current = queue.popleft()

        for dependent in sorted(reverse[current]):
            if dependent not in distances:
                distances[dependent] = distances[current] + 1
                queue.append(dependent)

    distances.pop(target)

    return dict(sorted(
        distances.items(),
        key=lambda item: (item[1], item[0]),
    ))
