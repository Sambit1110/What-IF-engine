"""Build a static dependency graph from Python source files."""

import ast
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from whatif_engine.analyzer import IGNORED_DIRS


@dataclass(frozen=True)
class DependencyGraph:
    # Module name -> project-relative source path
    modules: dict[str, str]

    # Importing module -> local modules it depends on
    dependencies: dict[str, tuple[str, ...]]

    # Module name -> parsing or reading error
    errors: dict[str, str]


def _module_name(root: Path, path: Path) -> str | None:
    """Convert a source path to a project module name."""
    parts = list(path.relative_to(root).with_suffix("").parts)

    if path.name == "__init__.py":
        parts = parts[:-1]

    # Support the common src/ package layout.
    if parts and parts[0] == "src":
        parts = parts[1:]

    return ".".join(parts) or None


def _resolve_local(name: str, modules: dict[str, str]) -> str | None:
    """Resolve the longest matching local module or package."""
    parts = name.split(".")

    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in modules:
            return candidate

    return None


def build_dependency_graph(root: str | Path) -> DependencyGraph:
    """Build import relationships without executing project code."""
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
            if filename.endswith(".py"):
                paths.append(Path(current) / filename)

    paths.sort(key=lambda p: p.relative_to(root).as_posix())

    module_paths: dict[str, Path] = {}

    for path in paths:
        name = _module_name(root, path)
        if name is not None and name not in module_paths:
            module_paths[name] = path

    modules = {
        name: path.relative_to(root).as_posix()
        for name, path in sorted(module_paths.items())
    }

    dependencies: dict[str, tuple[str, ...]] = {}
    errors: dict[str, str] = {}

    for name, path in sorted(module_paths.items()):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            errors[name] = f"line {exc.lineno}: {exc.msg}"
            dependencies[name] = ()
            continue
        except (OSError, UnicodeError) as exc:
            errors[name] = f"Could not read file: {exc}"
            dependencies[name] = ()
            continue

        if path.name == "__init__.py":
            package_parts = name.split(".")
        else:
            parent = name.rpartition(".")[0]
            package_parts = parent.split(".") if parent else []

        local_dependencies: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = _resolve_local(alias.name, modules)
                    if local is not None:
                        local_dependencies.add(local)

            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(package_parts) - (node.level - 1)

                    if keep < 0:
                        base = None
                    else:
                        prefix = ".".join(package_parts[:keep])
                        base = ".".join(
                            part for part in (prefix, node.module or "")
                            if part
                        ) or None
                else:
                    base = node.module

                if base:
                    local = _resolve_local(base, modules)
                    if local is not None:
                        local_dependencies.add(local)

                # Also resolve submodules imported from a package:
                # from package import helper
                for alias in node.names:
                    if alias.name == "*" or not base:
                        continue

                    candidate = f"{base}.{alias.name}"
                    local = _resolve_local(candidate, modules)

                    if local is not None:
                        local_dependencies.add(local)

        local_dependencies.discard(name)
        dependencies[name] = tuple(sorted(local_dependencies))

    return DependencyGraph(
        modules=modules,
        dependencies=dependencies,
        errors=errors,
    )


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
