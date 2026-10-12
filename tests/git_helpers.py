"""Temporary, configuration-isolated Git repositories for tests."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GIT_AVAILABLE = shutil.which("git") is not None

# Variables that would point git at some other repository or index.
_REPOSITORY_OVERRIDES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
)


@unittest.skipUnless(GIT_AVAILABLE, "git is not installed")
class GitRepositoryTestCase(unittest.TestCase):
    """
    Each test gets a fresh repository in a temporary directory.

    The user's global and system Git configuration, hooks and HOME are
    hidden, so the tests behave the same on every machine.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()

        home = self.tmp / "home"
        home.mkdir()

        environment = mock.patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            # Never discover a repository above the temporary directory.
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "GIT_AUTHOR_NAME": "WhatIf Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "WhatIf Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)

        for name in _REPOSITORY_OVERRIDES:
            os.environ.pop(name, None)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def write(self, files: dict[str, str]) -> None:
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()
