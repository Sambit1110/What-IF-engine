import os
import tempfile
import unittest
from pathlib import Path

from fifo_helpers import FIFO_SUPPORTED, call_with_timeout, make_fifo
from whatif_engine.graph import (
    build_dependency_graph,
    calculate_impact,
    trace_impact,
)


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


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
            self.assertEqual(graph.warnings, {})


class TestRelativeImports(unittest.TestCase):
    def test_import_beyond_top_level_package_is_not_resolved(self):
        # Python raises: attempted relative import beyond top-level package
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/svc.py": "VALUE = 1\nfrom ..utils import x\n",
                "utils.py": "",
            })

            graph = build_dependency_graph(root)

            # Only the implicit parent-package edge; never `utils`.
            self.assertEqual(graph.dependencies["pkg.svc"], ("pkg",))
            self.assertEqual(len(graph.warnings["pkg.svc"]), 1)
            self.assertIn("line 2", graph.warnings["pkg.svc"][0])
            self.assertIn("..utils", graph.warnings["pkg.svc"][0])
            self.assertEqual(graph.errors, {})

    def test_relative_import_in_top_level_module_is_not_resolved(self):
        # Python raises: attempted relative import with no known parent package
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "top.py": "from . import utils\n",
                "utils.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["top"], ())
            self.assertIn("top", graph.warnings)

    def test_valid_two_level_relative_import_is_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/core.py": "X = 1\n",
                "pkg/sub/__init__.py": "",
                "pkg/sub/mod.py": "from ..core import X\n",
            })

            graph = build_dependency_graph(root)

            self.assertIn("pkg.core", graph.dependencies["pkg.sub.mod"])
            self.assertEqual(graph.warnings, {})


class TestModuleNameCollisions(unittest.TestCase):
    def test_package_takes_precedence_over_same_named_module(self):
        # Python's path finder imports pkg/__init__.py, not pkg.py,
        # when both exist in the same directory.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg.py": "",
                "pkg/__init__.py": "from . import helper\n",
                "pkg/helper.py": "",
                "app.py": "import pkg\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.modules["pkg"], "pkg/__init__.py")
            self.assertIn("pkg.helper", graph.dependencies["pkg"])
            self.assertIn("app", calculate_impact(graph, "pkg.helper"))

            self.assertEqual(len(graph.warnings["pkg"]), 1)
            self.assertIn("pkg.py", graph.warnings["pkg"][0])
            self.assertEqual(graph.errors, {})

    def test_duplicate_module_across_import_roots_is_reported(self):
        # Which file wins depends on sys.path order, so the analysis
        # keeps a deterministic choice but must not hide the other file.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/a.py": "",
                "src/pkg/__init__.py": "",
                "src/pkg/a.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.modules["pkg.a"], "pkg/a.py")
            self.assertIn("src/pkg/a.py", graph.warnings["pkg.a"][0])
            self.assertIn("src/pkg/__init__.py", graph.warnings["pkg"][0])
            self.assertEqual(graph.errors, {})


class TestSiblingImports(unittest.TestCase):
    # `unittest discover -s tests` and running a script both put the
    # file's own directory first on sys.path.

    def test_test_helper_imported_by_bare_name_is_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "tests/helpers.py": "",
                "tests/test_a.py": "import helpers\n",
                "tests/test_b.py": "from helpers import make\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(
                graph.dependencies["tests.test_a"], ("tests.helpers",)
            )
            self.assertEqual(
                graph.dependencies["tests.test_b"], ("tests.helpers",)
            )
            self.assertEqual(
                list(calculate_impact(graph, "tests.helpers")),
                ["tests.test_a", "tests.test_b"],
            )

    def test_sibling_module_takes_precedence_over_top_level_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "helpers.py": "",
                "tests/helpers.py": "",
                "tests/test_a.py": "import helpers\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(
                graph.dependencies["tests.test_a"], ("tests.helpers",)
            )

    def test_package_modules_do_not_resolve_bare_sibling_names(self):
        # Python 3 has no implicit relative imports inside packages.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/a.py": "import b\n",
                "pkg/b.py": "",
                "b.py": "",
            })

            graph = build_dependency_graph(root)

            # `import b` is the top-level b, never pkg.b; pkg itself is
            # the implicit parent-package initializer.
            self.assertEqual(graph.dependencies["pkg.a"], ("b", "pkg"))


class TestPackageScanRoot(unittest.TestCase):
    def test_scan_root_that_is_a_package_keeps_full_module_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_files(project, {
                "src/pkg/__init__.py": "",
                "src/pkg/core.py": "X = 1\n",
                "src/pkg/service.py": "from pkg.core import X\n",
                "src/pkg/api.py": "from .service import run\n",
            })

            graph = build_dependency_graph(project / "src" / "pkg")

            self.assertEqual(graph.modules["pkg"], "__init__.py")
            self.assertEqual(graph.modules["pkg.core"], "core.py")
            self.assertIn("pkg.core", graph.dependencies["pkg.service"])
            self.assertIn("pkg.service", graph.dependencies["pkg.api"])
            self.assertEqual(graph.warnings, {})

    def test_nested_scan_root_includes_every_enclosing_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_files(project, {
                "pkg/__init__.py": "",
                "pkg/other.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/mod.py": "from .. import other\nfrom . import peer\n",
                "pkg/sub/peer.py": "",
            })

            graph = build_dependency_graph(project / "pkg" / "sub")

            self.assertEqual(
                set(graph.modules), {"pkg.sub", "pkg.sub.mod", "pkg.sub.peer"}
            )
            self.assertIn("pkg.sub.peer", graph.dependencies["pkg.sub.mod"])
            # `from .. import other` is valid Python; its target is
            # simply outside the scanned directory.
            self.assertEqual(graph.warnings, {})

    def test_imports_written_from_inside_the_package_still_resolve(self):
        # If the package directory itself is on sys.path, `import b`
        # works too, so both spellings are treated as local.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pkg"
            write_files(root, {
                "__init__.py": "",
                "a.py": "import b\n",
                "b.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertIn("pkg.b", graph.dependencies["pkg.a"])

    def test_project_root_with_init_file_keeps_src_import_root(self):
        # With PYTHONPATH=src run from the project root, a stray root
        # __init__.py never executes: the root is an import root, not a
        # package. Project markers identify it as such.
        for marker in ("pyproject.toml", "setup.py", "setup.cfg", ".git"):
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_files(root, {
                        "__init__.py": "",
                        "src/pkg/__init__.py": "",
                        "src/pkg/core.py": "X = 1\n",
                        "src/pkg/api.py": "from pkg.core import X\n",
                        "tests/test_api.py": "from pkg.api import run\n",
                    })
                    if marker == ".git":
                        (root / ".git").mkdir()
                    else:
                        (root / marker).write_text("", encoding="utf-8")

                    graph = build_dependency_graph(root)

                    self.assertEqual(
                        graph.modules["pkg.core"], "src/pkg/core.py"
                    )
                    self.assertIn("pkg.core", graph.dependencies["pkg.api"])
                    self.assertIn(
                        "tests.test_api",
                        calculate_impact(graph, "pkg.core"),
                    )
                    self.assertEqual(graph.warnings, {})


class TestParentPackageDependencies(unittest.TestCase):
    # Importing pkg.sub.leaf executes pkg/__init__.py and
    # pkg/sub/__init__.py first.

    def test_import_of_submodule_depends_on_parent_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/leaf.py": "",
                "plain.py": "import pkg.sub.leaf\n",
                "from_form.py": "from pkg.sub import leaf\n",
            })

            graph = build_dependency_graph(root)

            expected = ("pkg", "pkg.sub", "pkg.sub.leaf")
            self.assertEqual(graph.dependencies["plain"], expected)
            self.assertEqual(graph.dependencies["from_form"], expected)
            self.assertEqual(calculate_impact(graph, "pkg")["plain"], 1)


class TestImplicitParentInitialization(unittest.TestCase):
    # Python executes a submodule's parent package __init__.py before
    # the submodule itself, even when the submodule imports nothing.

    def test_initializer_change_affects_submodule_without_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/leaf.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["pkg.leaf"], ("pkg",))
            self.assertEqual(graph.dependencies["pkg"], ())
            self.assertEqual(graph.parent_packages, {"pkg.leaf": "pkg"})
            self.assertEqual(calculate_impact(graph, "pkg"), {"pkg.leaf": 1})

    def test_nested_import_accounts_for_parent_initializers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/leaf.py": "",
                "app.py": "import pkg.sub.leaf\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["pkg.sub.leaf"], ("pkg.sub",))
            self.assertEqual(graph.dependencies["pkg.sub"], ("pkg",))

            # app names pkg.sub.leaf in an import, so all three
            # initializers run on its behalf (distance 1). pkg.sub.leaf
            # only reaches pkg through pkg.sub (distance 2).
            self.assertEqual(
                calculate_impact(graph, "pkg"),
                {"app": 1, "pkg.sub": 1, "pkg.sub.leaf": 2},
            )
            self.assertEqual(
                calculate_impact(graph, "pkg.sub"),
                {"app": 1, "pkg.sub.leaf": 1},
            )

    def test_no_self_or_duplicate_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "from . import leaf\n",
                "pkg/leaf.py": "import pkg\nfrom . import helper\n",
                "pkg/helper.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["pkg"], ("pkg.leaf",))
            self.assertEqual(
                graph.dependencies["pkg.leaf"], ("pkg", "pkg.helper")
            )
            for name, dependencies in graph.dependencies.items():
                self.assertNotIn(name, dependencies)
                self.assertEqual(len(dependencies), len(set(dependencies)))

            # The pkg <-> pkg.leaf cycle must not break distances.
            self.assertEqual(
                calculate_impact(graph, "pkg.helper"),
                {"pkg.leaf": 1, "pkg": 2},
            )

    def test_plain_directory_uses_nearest_package_initializer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/ns/mod.py": "",
                "tests/test_a.py": "",
            })

            graph = build_dependency_graph(root)

            # pkg/ns has no __init__.py, so pkg is the nearest
            # initializer; tests/ is not a package at all.
            self.assertEqual(graph.dependencies["pkg.ns.mod"], ("pkg",))
            self.assertEqual(graph.dependencies["tests.test_a"], ())
            self.assertNotIn("tests.test_a", graph.parent_packages)

    def test_module_named_like_a_directory_is_not_an_initializer(self):
        # pkg.py is a plain module; Python cannot import pkg.sub from
        # it, so it must not be treated as pkg/sub.py's parent package.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg.py": "",
                "pkg/sub.py": "",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["pkg.sub"], ())
            self.assertEqual(graph.parent_packages, {})

    def test_unparseable_submodule_still_depends_on_its_initializer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/broken.py": "def broken(:\n",
            })

            graph = build_dependency_graph(root)

            self.assertIn("pkg.broken", graph.errors)
            self.assertEqual(graph.dependencies["pkg.broken"], ("pkg",))
            self.assertIn("pkg.broken", calculate_impact(graph, "pkg"))


@unittest.skipUnless(FIFO_SUPPORTED, "named pipes are not supported")
class TestNonRegularFiles(unittest.TestCase):
    def test_named_pipe_and_symlink_to_it_are_reported_not_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"app.py": "import pipe\nimport link\n"})
            make_fifo(root / "pipe.py")
            os.symlink("pipe.py", root / "link.py")

            graph = call_with_timeout(lambda: build_dependency_graph(root))

            for module in ("pipe", "link"):
                self.assertIn("not a regular file", graph.errors[module])
            # The modules still exist, so their importers stay visible.
            self.assertEqual(graph.dependencies["app"], ("link", "pipe"))


class TestNamespacePackages(unittest.TestCase):
    def test_imports_from_namespace_packages_resolve(self):
        # Regression: ns/ has no __init__.py, but Python still imports
        # ns.mod (PEP 420). Resolution must not require `ns` itself to be
        # a module.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "ns/mod.py": "",
                "app.py": "from ns import mod\nimport ns.mod\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dependencies["app"], ("ns.mod",))
            self.assertEqual(graph.import_lines["app"]["ns.mod"], (1, 2))


class TestGraphScope(unittest.TestCase):
    def test_include_restricts_analysis_to_listed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "a.py": "",
                "b.py": "import a\n",
                "generated.py": "import a\n",
            })

            graph = build_dependency_graph(root, include={"a.py", "b.py"})

            self.assertEqual(set(graph.modules), {"a", "b"})

    def test_project_root_bounds_the_enclosing_package_walk(self):
        # A Git snapshot has no .git marker, so the caller names the
        # project root explicitly.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "snapshot"
            write_files(root, {
                "__init__.py": "",
                "pkg/__init__.py": "",
                "pkg/m.py": "",
            })

            bounded = build_dependency_graph(root, project_root=root)
            unbounded = build_dependency_graph(root)

            self.assertEqual(set(bounded.modules), {"pkg", "pkg.m"})
            self.assertIn("snapshot.pkg.m", unbounded.modules)

    def test_shadowed_files_are_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"pkg.py": "", "pkg/__init__.py": ""})

            graph = build_dependency_graph(root)

            self.assertEqual(graph.shadowed, {"pkg.py": "pkg"})


class TestImportEvidence(unittest.TestCase):
    def test_import_lines_are_recorded_per_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "a.py": "",
                "pkg/__init__.py": "",
                "pkg/leaf.py": "\n\nimport a\nfrom a import x\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.import_lines["pkg.leaf"]["a"], (3, 4))
            # The parent-package edge comes from no import statement.
            self.assertNotIn("pkg", graph.import_lines["pkg.leaf"])

    def test_dynamic_imports_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "a.py": "import importlib\nimportlib.import_module('x')\n",
                "b.py": "__import__('y')\n",
                "c.py": "from importlib import import_module\nimport_module('z')\n",
                "d.py": "import json\n",
            })

            graph = build_dependency_graph(root)

            self.assertEqual(graph.dynamic_imports, ("a", "b", "c"))


class TestRemovedModuleImports(unittest.TestCase):
    def test_imports_of_a_removed_module_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/__init__.py": "",
                "pkg/api.py": (
                    "import pkg.old\n"
                    "from pkg import old\n"
                    "from pkg.old import thing\n"
                    "from pkg import attribute\n"
                    "from .old import other\n"
                ),
            })

            graph = build_dependency_graph(root, removed_modules={"pkg.old"})

            self.assertEqual(
                graph.missing_imports["pkg.api"],
                (("pkg.old", 1), ("pkg.old", 2), ("pkg.old", 3), ("pkg.old", 5)),
            )
            # A removed module never becomes a real dependency.
            self.assertEqual(graph.dependencies["pkg.api"], ("pkg",))

    def test_import_still_satisfied_elsewhere_is_not_missing(self):
        # With tests/helpers.py gone, `import helpers` from tests/ now
        # finds the top-level helpers.py instead.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "helpers.py": "",
                "tests/test_a.py": "import helpers\n",
            })

            graph = build_dependency_graph(
                root, removed_modules={"tests.helpers"}
            )

            self.assertEqual(graph.missing_imports, {})
            self.assertEqual(graph.dependencies["tests.test_a"], ("helpers",))

    def test_removed_initializer_breaks_only_names_defined_in_it(self):
        # Without pkg/__init__.py, pkg is a namespace package: submodule
        # imports still work, names defined in the initializer do not.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {
                "pkg/service.py": "",
                "app.py": "from pkg import service\nfrom pkg import SETTING\n",
            })

            graph = build_dependency_graph(root, removed_modules={"pkg"})

            self.assertEqual(graph.missing_imports, {"app": (("pkg", 2),)})

    def test_removed_sibling_helper_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_files(root, {"tests/test_a.py": "import helpers\n"})

            graph = build_dependency_graph(
                root, removed_modules={"tests.helpers"}
            )

            self.assertEqual(
                graph.missing_imports, {"tests.test_a": (("tests.helpers", 1),)}
            )


class TestTraceImpact(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_files(root, {
            "pkg/__init__.py": "",
            "pkg/core.py": "",
            "pkg/util.py": "",
            "pkg/service.py": "from . import core\n",
            "pkg/api.py": "from .service import run\nfrom . import util\n",
            "tests/test_api.py": "from pkg.api import endpoint\n",
        })
        self.graph = build_dependency_graph(root)

    def test_returns_shortest_chain_from_dependent_to_target(self):
        chains = trace_impact(self.graph, ["pkg.core"])

        self.assertEqual(chains["pkg.service"], ("pkg.service", "pkg.core"))
        self.assertEqual(
            chains["tests.test_api"],
            ("tests.test_api", "pkg.api", "pkg.service", "pkg.core"),
        )
        self.assertNotIn("pkg.core", chains)

    def test_multiple_targets_keep_the_closest_one(self):
        chains = trace_impact(self.graph, ["pkg.core", "pkg.util"])

        self.assertEqual(chains["pkg.api"], ("pkg.api", "pkg.util"))
        self.assertEqual(
            chains["tests.test_api"], ("tests.test_api", "pkg.api", "pkg.util")
        )

    def test_distances_match_calculate_impact(self):
        chains = trace_impact(self.graph, ["pkg"])
        distances = calculate_impact(self.graph, "pkg")

        self.assertEqual(
            {module: len(chain) - 1 for module, chain in chains.items()},
            distances,
        )


if __name__ == "__main__":
    unittest.main()
