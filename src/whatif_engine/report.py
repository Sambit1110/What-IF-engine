"""Human-readable and JSON renderings of a ChangeReport."""

import json
from typing import Any

from whatif_engine import __version__
from whatif_engine.changes import ChangeReport

JSON_SCHEMA_VERSION = 1

PREDICTION_NOTE = (
    "static estimate from import analysis; not proof of runtime behavior "
    "and not an exhaustive list of affected tests"
)


def _relationship(distance: int) -> str:
    return "direct" if distance == 1 else "indirect"


def report_to_dict(report: ChangeReport) -> dict[str, Any]:
    """The report as JSON-compatible data (schema version 1)."""
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": {"name": "whatif-engine", "version": __version__},
        "repository": report.repository,
        "scan_path": report.scan_path,
        "base": {
            "ref": report.base.ref,
            "commit": report.base.commit,
            "merge_base_with": report.base.merge_base_with,
        },
        "include_untracked": report.include_untracked,
        "allow_git_filters": report.allow_git_filters,
        "complete": report.complete,
        "observed": {
            "changes": [
                {
                    "status": change.status,
                    "path": change.path,
                    "old_path": change.old_path,
                    "similarity": change.similarity,
                    "module": change.module,
                    "old_module": change.old_module,
                }
                for change in report.changes
            ],
            "changed_modules": list(report.changed_modules),
            "removed_modules": list(report.removed_modules),
        },
        "predicted": {
            "note": PREDICTION_NOTE,
            "affected_modules": [
                {
                    "module": entry.module,
                    "path": entry.path,
                    "distance": entry.distance,
                    "relationship": _relationship(entry.distance),
                    "chain": list(entry.chain),
                    "graph": entry.graph,
                }
                for entry in report.affected
            ],
            "likely_broken_imports": [
                {
                    "module": item.module,
                    "path": item.path,
                    "line": item.line,
                    "removed_module": item.removed_module,
                    "reason": item.reason,
                }
                for item in report.likely_broken_imports
            ],
            "suggested_tests": [
                {
                    "module": test.module,
                    "path": test.path,
                    "reason": test.reason,
                    "chain": list(test.chain),
                }
                for test in report.suggested_tests
            ],
            "run_full_suite": {
                "recommended": bool(report.full_suite_reasons),
                "reasons": list(report.full_suite_reasons),
            },
        },
        "unknown": [
            {"path": change.path, "status": change.status, "reason": change.note}
            for change in report.unknown
        ],
        "errors": [
            {"message": d.message, "module": d.module, "path": d.path}
            for d in report.errors
        ],
        "warnings": [
            {
                "message": d.message,
                "module": d.module,
                "path": d.path,
                "introduced": d.introduced,
            }
            for d in report.warnings
        ],
    }


def format_json(report: ChangeReport) -> str:
    return json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n"


def format_text(report: ChangeReport) -> str:
    lines: list[str] = []
    add = lines.append

    add("")
    add("WhatIf Engine — change impact")
    add("")
    scope = "" if report.scan_path == "." else f" (scanned: {report.scan_path})"
    add(f"Repository: {report.repository}{scope}")

    commit = report.base.commit[:12]
    if report.base.merge_base_with:
        add(f"Base: merge base of HEAD and {report.base.merge_base_with} ({commit})")
    else:
        add(f"Base: {report.base.ref} ({commit})")

    untracked = "included" if report.include_untracked else "excluded"
    add(f"Compared: working tree, including staged edits; untracked files {untracked}")
    if report.allow_git_filters:
        add("Git filters: allowed (configured filter commands may have run)")
    add("")

    if not report.changes:
        add("No changes against the base.")
        return "\n".join(lines) + "\n"

    add(f"Observed changes (from Git): {len(report.changes)}")
    for change in report.changes:
        if change.old_path:
            path = f"{change.old_path} -> {change.path}"
            if change.similarity is not None:
                path += f" ({change.similarity}% similar)"
        else:
            path = change.path

        if change.old_module and change.module:
            modules = f"  [{change.old_module} -> {change.module}]"
        elif change.module:
            modules = f"  [{change.module}]"
        elif change.old_module:
            modules = f"  [was {change.old_module}]"
        else:
            modules = ""

        note = f" — {change.note}" if change.note else ""
        add(f"  {change.status:<11} {path}{modules}{note}")

    add("")
    add(f"Predicted impact (static estimate): {len(report.affected)} module(s)")
    if not report.affected:
        add("  No dependents found.")
    for entry in report.affected:
        source = " (base snapshot)" if entry.graph == "base" else ""
        add(
            f"  {entry.module} — {_relationship(entry.distance)}, "
            f"distance {entry.distance}{source}"
        )
        add(f"      {' -> '.join(entry.chain)}")

    if report.likely_broken_imports:
        add("")
        add("Likely broken imports (predicted):")
        for item in report.likely_broken_imports:
            add(f"  {item.path}:{item.line}  {item.module} — {item.reason}")

    add("")
    add("Suggested tests (static estimate, not exhaustive):")
    if not report.suggested_tests:
        add("  None found through imports.")
    for test in report.suggested_tests:
        add(f"  {test.path} — {test.reason}")
        if test.chain:
            add(f"      {' -> '.join(test.chain)}")

    add("")
    if report.full_suite_reasons:
        add("Run the full test suite: recommended")
        for reason in report.full_suite_reasons:
            add(f"  - {reason}")
    else:
        add("Run the full test suite: not required by this analysis")

    if report.errors:
        add("")
        add("Errors (analysis incomplete):")
        for diagnostic in report.errors:
            where = (
                f"{diagnostic.module} ({diagnostic.path})"
                if diagnostic.module else diagnostic.path
            )
            add(f"  {where}: {diagnostic.message}")

    if report.warnings:
        add("")
        add("Warnings:")
        for diagnostic in report.warnings:
            marker = "[new] " if diagnostic.introduced else ""
            where = diagnostic.module or diagnostic.path
            add(f"  {marker}{where}: {diagnostic.message}")

    add("")
    add("Note: predictions come from static import analysis, not from")
    add("running code. Verify with tests before relying on them.")

    return "\n".join(lines) + "\n"
