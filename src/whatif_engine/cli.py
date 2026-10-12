"""Command-line interface for WhatIf Engine."""

import argparse
from pathlib import Path

from whatif_engine.analyzer import scan_project
from whatif_engine.graph import build_dependency_graph, calculate_impact


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="whatif",
        description="Analyze potential software change impact.",
    )
    commands = parser.add_subparsers(
        dest="command", required=True
    )

    scan_parser = commands.add_parser(
        "scan", help="Inspect Python files and imports."
    )
    scan_parser.add_argument("path", nargs="?", default=".")

    graph_parser = commands.add_parser(
        "graph", help="Display local Python dependency relationships."
    )
    graph_parser.add_argument("path", nargs="?", default=".")

    impact_parser = commands.add_parser(
        "impact", help="Find direct and indirect dependents of a module."
    )
    impact_parser.add_argument("target", help="Module name, e.g. whatif_engine.analyzer")
    impact_parser.add_argument("path", nargs="?", default=".")

    args = parser.parse_args()

    if args.command == "scan":
        try:
            findings = scan_project(Path(args.path))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"whatif: error: {exc}\n")

        errors = 0
        print(f"\nWhatIf Engine — scan: {Path(args.path).resolve()}\n")

        for finding in findings:
            print(f"  {finding.path}")

            if finding.syntax_error:
                errors += 1
                print(f"    ERROR: {finding.syntax_error}")
            elif finding.imports:
                print(f"    Imports: {', '.join(finding.imports)}")
            else:
                print("    Imports: (none)")

        print("\n--- Summary ---")
        print(f"Python files scanned: {len(findings)}")
        print(f"Files with errors:    {errors}")
        print("Note: imports are reported, not yet resolved into dependencies.")

        return 1 if errors else 0

    try:
        graph = build_dependency_graph(Path(args.path))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"whatif: error: {exc}\n")

    if graph.errors:
        print("Warnings: some modules could not be analyzed.")
        for module, error in sorted(graph.errors.items()):
            print(f"  {module}: {error}")
        print()

    if args.command == "graph":
        print("\nWhatIf Engine — dependency graph\n")

        for module, path in graph.modules.items():
            print(f"{module} ({path})")
            dependencies = graph.dependencies[module]

            if dependencies:
                for dependency in dependencies:
                    print(f"  -> {dependency}")
            else:
                print("  -> (no local dependencies)")

        edge_count = sum(
            len(deps) for deps in graph.dependencies.values()
        )
        print("\n--- Summary ---")
        print(f"Modules discovered: {len(graph.modules)}")
        print(f"Local dependency edges: {edge_count}")
        print(f"Modules with errors: {len(graph.errors)}")
        print("External packages and dynamic imports are not fully resolved.")

        return 1 if graph.errors else 0

    try:
        impact = calculate_impact(graph, args.target)
    except ValueError as exc:
        parser.exit(2, f"whatif: error: {exc}\n")

    print(f"\nWhatIf Engine — impact analysis: {args.target}\n")
    print(f"Potentially affected modules: {len(impact)}")

    for module, distance in impact.items():
        relationship = "direct" if distance == 1 else "indirect"
        print(
            f"  {module} — {relationship}, "
            f"dependency distance {distance}"
        )

    if not impact:
        print("  No local dependents found.")

    print("\nNote: this is static analysis, not proof of runtime impact.")
    print("Review warnings and test results before changing code.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
