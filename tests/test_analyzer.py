import tempfile
import unittest
from pathlib import Path

from fifo_helpers import FIFO_SUPPORTED, call_with_timeout, make_fifo
from whatif_engine.analyzer import scan_project


class TestScanProject(unittest.TestCase):
    def test_extracts_imports_and_reports_syntax_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            (root / "app.py").write_text(
                "import json\nfrom pathlib import Path\n",
                encoding="utf-8",
            )
            (root / "broken.py").write_text(
                "def broken(:\n",
                encoding="utf-8",
            )

            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "ignored.py").write_text(
                "import os\n", encoding="utf-8"
            )

            results = {
                item.path: item for item in scan_project(root)
            }

            self.assertEqual(set(results), {"app.py", "broken.py"})
            self.assertEqual(
                results["app.py"].imports,
                ("json", "pathlib"),
            )
            self.assertIsNotNone(results["broken.py"].syntax_error)

    def test_reports_names_imported_from_bare_relative_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pkg" / "mod.py").write_text(
                "from . import alpha, beta\n"
                "from .. import gamma\n"
                "from .sibling import thing\n"
                "from . import *\n",
                encoding="utf-8",
            )

            results = {
                item.path: item for item in scan_project(root)
            }

            self.assertEqual(
                results["pkg/mod.py"].imports,
                (".alpha", ".beta", "..gamma", ".sibling", "."),
            )

    @unittest.skipUnless(FIFO_SUPPORTED, "named pipes are not supported")
    def test_named_pipe_is_reported_and_never_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ok.py").write_text("import json\n", encoding="utf-8")
            make_fifo(root / "pipe.py")

            results = {
                item.path: item
                for item in call_with_timeout(lambda: scan_project(root))
            }

            self.assertIn("not a regular file", results["pipe.py"].syntax_error)
            self.assertEqual(results["pipe.py"].imports, ())
            self.assertEqual(results["ok.py"].imports, ("json",))

    def test_rejects_a_missing_directory(self):
        with self.assertRaises(NotADirectoryError):
            scan_project("/this/path/should/not/exist/whatif")


if __name__ == "__main__":
    unittest.main()
