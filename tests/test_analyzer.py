import tempfile
import unittest
from pathlib import Path

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

    def test_rejects_a_missing_directory(self):
        with self.assertRaises(NotADirectoryError):
            scan_project("/this/path/should/not/exist/whatif")


if __name__ == "__main__":
    unittest.main()
