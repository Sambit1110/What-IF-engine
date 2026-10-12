import tempfile
import unittest
from pathlib import Path

from whatif_engine.graph import (
    build_dependency_graph,
    calculate_impact,
)


class TestDependencyGraph(unittest.TestCase):
    def test_direct_and_transitive_impact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            files = {
                "src/pkg/__init__.py": "",
                "src/pkg/core.py": "VALUE = 1\n",
                "src/pkg/service.py": "from . import core\n",
                "src/pkg/api.py": "from .service import run\n",
                "tests/test_api.py": "from pkg.api import endpoint\n",
            }

            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            graph = build_dependency_graph(root)

            self.assertIn("pkg.core", graph.modules)
            self.assertIn("pkg.core", graph.dependencies["pkg.service"])
            self.assertIn("pkg.service", graph.dependencies["pkg.api"])

            impact = calculate_impact(graph, "pkg.core")

            self.assertEqual(impact["pkg.service"], 1)
            self.assertEqual(impact["pkg.api"], 2)
            self.assertEqual(impact["tests.test_api"], 3)

    def test_unknown_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )

            graph = build_dependency_graph(root)

            with self.assertRaises(ValueError):
                calculate_impact(graph, "missing_module")

    def test_syntax_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "broken.py").write_text(
                "def broken(:\n", encoding="utf-8"
            )

            graph = build_dependency_graph(root)

            self.assertIn("broken", graph.errors)


if __name__ == "__main__":
    unittest.main()
