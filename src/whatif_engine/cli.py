"""Command-line interface for WhatIf Engine."""

import argparse
from pathlib import Path

from whatif_engine.analyzer import scan_project


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="whatif",
        description="Inspect Python source files without executing them.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan a project for Python files and imports.",
    )
    scan_parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project directory (default: current directory).",
    )

    args = parser.parse_args()

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
