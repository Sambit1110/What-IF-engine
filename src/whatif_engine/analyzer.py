"""Basic, read-only Python repository scanner."""

import ast
import os
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "build", "dist",
}


@dataclass(frozen=True)
class SourceFile:
    path: str
    imports: tuple[str, ...]
    syntax_error: str | None = None


def scan_project(root: str | Path) -> list[SourceFile]:
    """Find Python files and extract imports without executing the code."""
    root = Path(root).resolve()

    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    findings = []

    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(
            name for name in directories
            if name not in IGNORED_DIRS
        )

        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue

            path = Path(current) / filename
            relative_path = path.relative_to(root).as_posix()

            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative_path)
            except SyntaxError as exc:
                error = f"line {exc.lineno}: {exc.msg}"
                findings.append(
                    SourceFile(relative_path, (), error)
                )
                continue
            except (OSError, UnicodeError) as exc:
                findings.append(
                    SourceFile(
                        relative_path, (),
                        f"Could not read file: {exc}",
                    )
                )
                continue

            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    if module:
                        imports.append(module)

            findings.append(
                SourceFile(
                    relative_path,
                    tuple(dict.fromkeys(imports)),
                )
            )

    return findings
