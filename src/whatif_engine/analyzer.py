"""Basic, read-only Python repository scanner."""

import ast
import os
import stat
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "build", "dist",
}


class NotRegularFile(OSError):
    """A source path that is a named pipe, device, directory, or socket."""


_FILE_KINDS = (
    (stat.S_ISFIFO, "named pipe"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
    (stat.S_ISSOCK, "socket"),
    (stat.S_ISDIR, "directory"),
)


def read_source(path: str | Path) -> str:
    """
    Read a UTF-8 source file, refusing anything but a regular file.

    The file is opened non-blocking and checked with fstat on that same
    descriptor before reading, so a named pipe or device is rejected
    instead of blocking or streaming forever, and the path cannot be
    swapped between the check and the read.
    """
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))

    with open(descriptor, encoding="utf-8") as handle:
        mode = os.fstat(handle.fileno()).st_mode

        if not stat.S_ISREG(mode):
            kind = next(
                (name for test, name in _FILE_KINDS if test(mode)),
                "special file",
            )
            raise NotRegularFile(f"not a regular file ({kind}); not read")

        return handle.read()


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
                source = read_source(path)
                tree = ast.parse(source, filename=relative_path)
            except SyntaxError as exc:
                error = f"line {exc.lineno}: {exc.msg}"
                findings.append(
                    SourceFile(relative_path, (), error)
                )
                continue
            except NotRegularFile as exc:
                findings.append(SourceFile(relative_path, (), str(exc)))
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
                    dots = "." * node.level
                    if node.module:
                        imports.append(dots + node.module)
                    else:
                        # `from . import x` names x, which may be a
                        # submodule or an attribute of the package.
                        imports.extend(
                            dots if alias.name == "*" else dots + alias.name
                            for alias in node.names
                        )

            findings.append(
                SourceFile(
                    relative_path,
                    tuple(dict.fromkeys(imports)),
                )
            )

    return findings
