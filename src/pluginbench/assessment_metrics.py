"""Offline, source-bound measurements of immutable submitted snapshots."""

from __future__ import annotations

import fnmatch
import hashlib
import math
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pluginbench.code_diff import (
    _SOURCE_SUFFIXES,
    _is_source_path,
    _is_test_path,
    _marker_path,
    analyze_patch,
)

EXCLUDED_NAMES = frozenset({".git", ".agents", ".claude", "node_modules", "__pycache__"})


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return {key: item for key, item in value.items()}


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string list")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate identifiers")
    return [item for item in value]


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _load(path: Path) -> dict[str, object]:
    return _object(json.loads(path.read_text(encoding="utf-8")), str(path))


def inspect_snapshot(root: Path) -> dict[str, object]:
    """Hash every included file; reject links instead of following them."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"snapshot must be a directory, not a link: {root}")
    files: dict[str, str] = {}
    excluded: list[str] = []
    source_lines = test_lines = 0

    def visit(directory: Path) -> None:
        nonlocal source_lines, test_lines
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f"snapshot contains a symlink: {relative}")
            if path.name in EXCLUDED_NAMES:
                excluded.append(relative)
            elif path.is_dir():
                visit(path)
            elif path.is_file():
                data = path.read_bytes()
                files[relative] = _digest(data)
                try:
                    lines = len(data.decode("utf-8").splitlines())
                except UnicodeDecodeError:
                    continue
                if _is_test_path(relative):
                    test_lines += lines
                elif _is_source_path(relative):
                    source_lines += lines
            else:
                raise ValueError(f"snapshot contains a nonregular file: {relative}")

    visit(root)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "digest": _digest(encoded),
        "files": files,
        "excluded_paths": excluded,
        "exclusion_names": sorted(EXCLUDED_NAMES),
        "source_lines": source_lines,
        "test_lines": test_lines,
    }


def _changed_line_ranges(patch: str) -> dict[str, object]:
    """Record zero-context Git hunk ranges, including zero-width edit anchors."""
    ranges: dict[str, dict[str, list[list[int]]]] = {}
    old_path = new_path = None
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            old_path = new_path = None
            in_hunk = False
        elif not in_hunk and line.startswith("--- "):
            old_path = _marker_path(line[4:])
        elif not in_hunk and line.startswith("+++ "):
            new_path = _marker_path(line[4:])
        elif line.startswith("@@ "):
            in_hunk = True
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match is None:
                raise ValueError("invalid snapshot diff hunk")
            for path, side, start, count in (
                (old_path, "before", match[1], match[2]),
                (new_path, "after", match[3], match[4]),
            ):
                if path is not None:
                    ranges.setdefault(path, {"before": [], "after": []})[side].append(
                        [int(start), int(count) if count is not None else 1]
                    )
    return dict(ranges)


def compare_snapshots(before: Path, after: Path) -> dict[str, object]:
    """Use the existing patch analyzer, with a canonical Git diff of filtered copies."""
    snapshots = [inspect_snapshot(before), inspect_snapshot(after)]
    with tempfile.TemporaryDirectory(prefix="pluginbench-assess-") as directory:
        temporary = Path(directory)
        for root, snapshot, name in zip((before, after), snapshots, ("a", "b"), strict=True):
            target = temporary / name
            target.mkdir()
            for relative in _object(snapshot["files"], "snapshot files"):
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, destination)
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-index",
                "--no-prefix",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--unified=0",
                "--inter-hunk-context=0",
                "--",
                "a",
                "b",
            ],
            cwd=temporary,
            capture_output=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise ValueError(f"snapshot diff failed: {result.stderr.decode(errors='replace')}")
        patch = result.stdout.decode(errors="surrogateescape")
        metrics: dict[str, object] = dict(analyze_patch(patch))
        metrics["changed_line_ranges"] = _changed_line_ranges(patch)
    for root, original in zip((before, after), snapshots, strict=True):
        if inspect_snapshot(root)["digest"] != original["digest"]:
            raise ValueError("snapshot changed during analysis")
    metrics.update(
        before_digest=snapshots[0]["digest"],
        after_digest=snapshots[1]["digest"],
        before_source_lines=snapshots[0]["source_lines"],
        after_source_lines=snapshots[1]["source_lines"],
        before_test_lines=snapshots[0]["test_lines"],
        after_test_lines=snapshots[1]["test_lines"],
        new_paths=sorted(
            set(_object(snapshots[1]["files"], "files"))
            - set(_object(snapshots[0]["files"], "files"))
        ),
    )
    return metrics


def _scoped_lines(
    *,
    before: Path,
    after: Path,
    scope: list[str],
    change: dict[str, object],
    kind: Literal["source", "test"],
) -> dict[str, object]:
    """Measure explicit line scope directly, reusing an already bound source diff."""
    if (
        not isinstance(scope, list)
        or not scope
        or any(not isinstance(pattern, str) or not pattern for pattern in scope)
    ):
        raise ValueError("code-size scope must be an explicit nonempty string list")
    normalized = sorted({pattern.removeprefix("./") for pattern in scope})
    if any(
        not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts or "\\" in pattern
        for pattern in normalized
    ):
        raise ValueError("code-size scope requires relative source patterns")
    snapshots = [inspect_snapshot(before), inspect_snapshot(after)]
    for side, snapshot in zip(("before", "after"), snapshots, strict=True):
        if change.get(f"{side}_digest") != snapshot["digest"]:
            raise ValueError("source diff does not bind to the inspected snapshot")
    ranges = _object(change.get("changed_line_ranges"), "changed-line ranges")
    inventories = [_object(snapshot["files"], "snapshot files") for snapshot in snapshots]
    populations: list[dict[str, int]] = []
    classify = _is_source_path if kind == "source" else _is_test_path
    for root, files in zip((before, after), inventories, strict=True):
        lines: dict[str, int] = {}
        for path in sorted(files):
            if not classify(path) or not any(
                fnmatch.fnmatchcase(path, pattern) for pattern in normalized
            ):
                continue
            try:
                data = (root / path).read_bytes()
            except OSError as error:
                raise ValueError("snapshot changed during code-size analysis") from error
            if _digest(data) != files[path]:
                raise ValueError("snapshot changed during code-size analysis")
            try:
                lines[path] = len(data.decode("utf-8").splitlines())
            except UnicodeDecodeError as error:
                raise ValueError(f"scoped source must be UTF-8 text: {path}") from error
        populations.append(lines)
    added = removed = 0
    before_files, after_files = inventories
    for path in sorted(set(populations[0]) | set(populations[1])):
        if before_files.get(path) != after_files.get(path) and path not in ranges:
            raise ValueError(f"bound source diff omits changed scoped path: {path}")
        row = _object(ranges.get(path, {"before": [], "after": []}), "file ranges")
        if set(row) != {"before", "after"}:
            raise ValueError("changed-line ranges require before and after")
        counts = []
        for side, population in zip(("before", "after"), populations, strict=True):
            items = row[side]
            if not isinstance(items, list):
                raise ValueError("changed-line ranges must be a list")
            count_total = 0
            previous_end = 0
            for item in items:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError("changed-line range requires start and count")
                start, count = (_integer(value, "changed-line range") for value in item)
                if (count and start <= previous_end) or start + max(count - 1, 0) > population.get(
                    path, 0
                ):
                    raise ValueError("changed-line range escapes scoped text or overlaps")
                if count:
                    previous_end = start + count - 1
                count_total += count
            counts.append(count_total)
        removed += counts[0]
        added += counts[1]
    for root, original in zip((before, after), snapshots, strict=True):
        if inspect_snapshot(root)["digest"] != original["digest"]:
            raise ValueError("snapshot changed during code-size analysis")
    return {
        "status": "available",
        "method": f"scoped-{kind}-lines-v1",
        "scope": normalized,
        "before_digest": snapshots[0]["digest"],
        "after_digest": snapshots[1]["digest"],
        "before_paths": sorted(populations[0]),
        "after_paths": sorted(populations[1]),
        f"before_{kind}_lines": sum(populations[0].values()),
        f"after_{kind}_lines": sum(populations[1].values()),
        f"{kind}_lines_added": added,
        f"{kind}_lines_removed": removed,
        "counting_semantics": f"UTF-8 splitlines totals from snapshot inventory paths matched by fnmatchcase; shared {kind} classification; additions/removals sum supplied zero-context Git hunk counts in scope; no analyzer file-discovery or generated-directory filtering",
    }


def scoped_code_size(
    *, before: Path, after: Path, scope: list[str], change: dict[str, object]
) -> dict[str, object]:
    return _scoped_lines(before=before, after=after, scope=scope, change=change, kind="source")


def scoped_test_size(
    *, before: Path, after: Path, scope: list[str], change: dict[str, object]
) -> dict[str, object]:
    return _scoped_lines(before=before, after=after, scope=scope, change=change, kind="test")


def _source_path(path: str, files: set[str], scope: list[str]) -> None:
    if path not in files or not any(fnmatch.fnmatchcase(path, pattern) for pattern in scope):
        raise ValueError(f"measurement path escapes source-bound scope: {path}")


def _coordinates(
    value: object, label: str, files: set[str], scope: list[str]
) -> tuple[set[str], set[str]]:
    row = _object(value, label)
    if set(row) != {"total", "covered"}:
        raise ValueError(f"{label} requires total and covered coordinate lists")
    total = set(_strings(row["total"], f"{label}.total"))
    covered = set(_strings(row["covered"], f"{label}.covered"))
    for coordinate in total:
        if ":" not in coordinate:
            raise ValueError("coverage coordinates require relative-path:coordinate")
        _source_path(coordinate.split(":", 1)[0], files, scope)
    if not covered <= total:
        raise ValueError(f"{label} covered coordinates escape the fixed denominator")
    return total, covered


def _percent(numerator: int, denominator: int) -> float | None:
    return 100 * numerator / denominator if denominator else None


def _marginal(value: object, measured: set[str], label: str) -> list[str] | None:
    if value is None:
        return None
    rows = _object(value, label)
    per_test = {name: set(_strings(items, f"{label}.{name}")) for name, items in rows.items()}
    if any(not items <= measured for items in per_test.values()):
        raise ValueError(f"{label} includes unmeasured coordinates")
    return sorted(
        name
        for name, items in per_test.items()
        if not items - set().union(*(other for key, other in per_test.items() if key != name))
    )


def _test_contribution(
    report: dict[str, object],
    files: set[str],
    branches: set[str],
    covered: set[str],
    functions: set[str],
    covered_functions: set[str],
) -> dict[str, object]:
    if report.get("contribution") is None:
        return {"status": "unavailable", "reason": "no declared test-contribution collection"}
    row = _object(report["contribution"], "test contribution")
    if row.get("method") != "leave-one-out-branches-v1":
        raise ValueError("unsupported test-contribution method")
    test_scope = [
        name.removeprefix("./") for name in _strings(report.get("test_scope"), "test scope")
    ]
    if not test_scope or any(
        Path(pattern).is_absolute() or ".." in Path(pattern).parts or "\\" in pattern
        for pattern in test_scope
    ):
        raise ValueError("test scope requires nonempty relative patterns")
    excluded = {
        name.removeprefix("./")
        for name in _strings(report.get("excluded_test_files", []), "excluded test files")
    }
    selected = (
        set(_strings(report["selected_test_files"], "selected test files"))
        if report.get("selected_test_files") is not None
        else None
    )
    reference = set(_strings(row.get("reference_ids"), "reference tests"))
    statuses = _object(row.get("reference_statuses"), "reference test statuses")
    if set(statuses) != reference or any(
        status not in {"passed", "pending", "failed"} for status in statuses.values()
    ):
        raise ValueError("test-contribution statuses must cover the declared reference population")
    for test_id in reference:
        path, delimiter, title = test_id.partition("::")
        if (
            not delimiter
            or not title
            or path not in files
            or path in excluded
            or (selected is not None and path not in selected)
            or not _is_test_path(path)
            or not any(fnmatch.fnmatchcase(path, pattern) for pattern in test_scope)
        ):
            raise ValueError("test-contribution identity escapes its bound test scope")
    runnable = {test_id for test_id, status in statuses.items() if status != "pending"}
    executed = set(_strings(row.get("executed_ids"), "executed reference tests"))
    passing = set(_strings(row.get("passing_ids"), "passing reference tests"))
    if (
        not runnable
        or executed != runnable
        or passing != runnable
        or any(statuses[test_id] != "passed" for test_id in runnable)
    ):
        raise ValueError("test contribution requires a completely executed passing reference suite")
    candidates = set(_strings(row.get("candidate_ids"), "candidate tests"))
    if (
        not candidates <= runnable
        or row.get("selection") != "all runnable cases in added/modified visible test files"
    ):
        raise ValueError("candidate tests require a declared runnable selection")
    changed = set(_strings(row.get("changed_test_files"), "changed visible test files"))
    expected = {test_id for test_id in runnable if test_id.partition("::")[0] in changed}
    if candidates != expected:
        raise ValueError("test-contribution candidate population omits selected runnable cases")
    runs = row.get("removal_runs")
    if not isinstance(runs, list):
        raise ValueError("test contribution requires removal runs")
    observed: set[str] = set()
    zero: set[str] = set()
    semantic_runs: dict[str, object] = {}
    for value in runs:
        removal = _object(value, "test removal run")
        excluded_id = removal.get("excluded_id")
        if (
            not isinstance(excluded_id, str)
            or excluded_id not in candidates
            or excluded_id in observed
        ):
            raise ValueError("test-removal runs must cover each candidate exactly once")
        observed.add(excluded_id)
        expected_remaining = runnable - {excluded_id}
        if (
            _integer(removal.get("exit_code"), "removal exit code") != 0
            or set(_strings(removal.get("executed_ids"), "remaining executed tests"))
            != expected_remaining
            or set(_strings(removal.get("passing_ids"), "remaining passing tests"))
            != expected_remaining
        ):
            raise ValueError("test-removal run must pass every remaining reference test")
        remaining_statuses = _object(removal.get("test_statuses"), "remaining test statuses")
        if excluded_id in remaining_statuses:
            if remaining_statuses.pop(excluded_id) != "pending":
                raise ValueError("excluded test must be absent or pending in native statuses")
        if remaining_statuses != {
            test_id: status for test_id, status in statuses.items() if test_id != excluded_id
        }:
            raise ValueError("test-removal native statuses differ from remaining reference tests")
        remaining = set(_strings(removal.get("covered_branches"), "remaining branch coverage"))
        lost = set(_strings(removal.get("unique_branches"), "removed branch contribution"))
        if not remaining <= branches or lost != covered - remaining:
            raise ValueError(
                "test-removal coverage escapes or disagrees with the bound branch population"
            )
        if remaining == covered:
            zero.add(excluded_id)
        semantic_runs[excluded_id] = {
            "test_statuses": remaining_statuses,
            "executed_ids": sorted(expected_remaining),
            "passing_ids": sorted(expected_remaining),
            "covered_branches": sorted(remaining),
            "unique_branches": sorted(lost),
        }
    if (
        observed != candidates
        or set(_strings(row.get("zero_marginal_ids"), "zero marginal tests")) != zero
    ):
        raise ValueError("test-contribution results must cover the complete candidate population")
    before_digest = report.get("before_digest")
    if (
        not isinstance(before_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", before_digest) is None
    ):
        raise ValueError("test contribution requires its bound before-snapshot digest")
    semantic = {
        "method": row["method"],
        "branches": {"total": sorted(branches), "covered": sorted(covered)},
        "functions": {"total": sorted(functions), "covered": sorted(covered_functions)},
        "reference_statuses": statuses,
        "candidate_ids": sorted(candidates),
        "changed_test_files": sorted(changed),
        "removal_runs": semantic_runs,
    }
    return {
        "status": "available",
        "method": row["method"],
        "label": "zero marginal branch contribution",
        "reference_count": len(runnable),
        "pending_count": len(reference - runnable),
        "candidate_count": len(candidates),
        "tested_count": len(observed),
        "zero_marginal_count": len(zero),
        "candidate_ids": sorted(candidates),
        "zero_marginal_ids": sorted(zero),
        "reference_ids": sorted(reference),
        "selection": row["selection"],
        "changed_test_files": sorted(changed),
        "before_digest": before_digest,
        "semantic_digest": _digest(
            json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
        ),
        "limitations": "Removing one case leaves the covered branch set unchanged in this declared suite. Equal coverage does not prove equivalent assertions or unnecessary tests; candidates cannot necessarily be removed together.",
    }


def _coverage(report: dict[str, object], files: set[str], scope: list[str]) -> dict[str, object]:
    test_scope = (
        [name.removeprefix("./") for name in _strings(report["test_scope"], "test scope")]
        if report.get("test_scope") is not None
        else None
    )
    excluded_tests = [
        name.removeprefix("./")
        for name in _strings(report.get("excluded_test_files", []), "excluded test files")
    ]
    branches, covered_branches = _coordinates(report.get("branches"), "branches", files, scope)
    functions, covered_functions = _coordinates(report.get("functions"), "functions", files, scope)
    measured_value = report.get("files_measured")
    measured = (
        set(_strings(measured_value, "files_measured")) if measured_value is not None else set()
    )
    for path in measured:
        _source_path(path, files, scope)
    expected = {
        path
        for path in files
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)
        and Path(path).suffix.lower() in _SOURCE_SUFFIXES
        and not path.endswith((".d.ts", ".d.mts", ".d.cts"))
    }
    return {
        "branch_total": len(branches),
        "branch_covered": len(covered_branches),
        "branch_percent": _percent(len(covered_branches), len(branches)),
        "function_total": len(functions),
        "function_covered": len(covered_functions),
        "function_percent": _percent(len(covered_functions), len(functions)),
        "population_complete_for_declared_scope": bool(expected) and expected <= measured,
        "files_measured": sorted(measured),
        "unmeasured_source_files": sorted(expected - measured),
        "branch_denominator_digest": _digest(json.dumps(sorted(branches)).encode()),
        "function_denominator_digest": _digest(json.dumps(sorted(functions)).encode()),
        "per_test_detection": "unique contribution among supplied tests only; not proof of redundancy",
        "per_test_count": len(_object(report["per_test"], "per_test"))
        if report.get("per_test") is not None
        else None,
        "zero_unique_branch_contribution_tests": _marginal(
            report.get("per_test"), covered_branches, "per_test"
        ),
        "test_scope": test_scope,
        "excluded_test_files": excluded_tests,
        "selected_test_files": report.get("selected_test_files"),
        "reference_test_files": report.get("reference_test_files"),
        "selection_policy": report.get("selection_policy"),
        **({"clock": report["clock"]} if "clock" in report else {}),
        "test_contribution": _test_contribution(
            report, files, branches, covered_branches, functions, covered_functions
        ),
    }


def _mutation(report: dict[str, object], files: set[str], scope: list[str]) -> dict[str, object]:
    mutant_rows = _object(report.get("mutants"), "mutants")
    mutants: dict[str, object] = {}
    source_identity: dict[str, str] = {}
    for identifier, value in mutant_rows.items():
        row = _object(value, f"mutant {identifier}")
        if set(row) != {"status", "path"} or not isinstance(row["path"], str):
            raise ValueError("mutants require status and path")
        _source_path(row["path"], files, scope)
        mutants[identifier] = row["status"]
        source_identity[identifier] = row["path"]
    allowed = {"killed", "survived", "no_coverage", "timeout", "compile_error", "runtime_error"}
    if any(not isinstance(status, str) or status not in allowed for status in mutants.values()):
        raise ValueError("mutation report contains an unsupported mutant status")
    detected = {key for key, status in mutants.items() if status in {"killed", "timeout"}}
    undetected = {key for key, status in mutants.items() if status in {"survived", "no_coverage"}}
    killed = {key for key, status in mutants.items() if status == "killed"}
    return {
        "mutant_total": len(mutants),
        "detected": len(detected),
        "undetected": len(undetected),
        "invalid": len(mutants) - len(detected) - len(undetected),
        "score_percent": _percent(len(detected), len(detected) + len(undetected)),
        "mutant_set_digest": _digest(json.dumps(sorted(mutants)).encode()),
        "eligible_mutant_set_digest": _digest(json.dumps(sorted(detected | undetected)).encode()),
        "mutant_source_digest": _digest(
            json.dumps(source_identity, sort_keys=True, separators=(",", ":")).encode()
        ),
        "per_test_detection": "unique contribution among supplied tests only; not proof of redundancy",
        "per_test_count": len(_object(report["per_test"], "per_test"))
        if report.get("per_test") is not None
        else None,
        "zero_unique_mutant_kill_tests": _marginal(report.get("per_test"), killed, "per_test"),
    }


_DEAD_FIELDS = (
    "unused_files",
    "unused_exports",
    "unused_types",
    "unused_dependencies",
    "unused_dev_dependencies",
    "unused_optional_dependencies",
    "unused_enum_members",
    "unused_class_members",
    "unused_catalog_entries",
    "unused_dependency_overrides",
)

_DEAD_IDENTITY_FIELDS = {
    "unused_files": ("path",),
    "unused_exports": ("path", "export_name"),
    "unused_types": ("path", "export_name"),
    "unused_dependencies": ("path", "package_name"),
    "unused_dev_dependencies": ("path", "package_name"),
    "unused_optional_dependencies": ("path", "package_name"),
    "unused_enum_members": ("path", "parent_name", "member_name", "kind"),
    "unused_class_members": ("path", "parent_name", "member_name", "kind"),
    "unused_catalog_entries": ("path", "catalog_name", "entry_name"),
    "unused_dependency_overrides": ("path", "raw_key"),
}


def _declaration_context(root: Path, row: dict[str, object]) -> str | None:
    line = row.get("line")
    if type(line) is not int or line < 1:
        return None
    path = row["path"]
    assert isinstance(path, str)
    try:
        source = (root / path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = source.splitlines(keepends=True)
    if line > len(lines):
        return None
    context = lines[line - 1].strip()
    start = row.get("span_start")
    if start is not None:
        offset = len("".join(lines[: line - 1]).encode("utf-8"))
        if type(start) is not int or not offset <= start < offset + len(
            lines[line - 1].encode("utf-8")
        ):
            return None
    return _digest(context.encode("utf-8")) if context else None


def _dead_identities(
    check: dict[str, object], files: set[str], root: Path
) -> list[dict[str, str]] | None:
    """Retain bound declaration context for pair-aware collision disambiguation."""
    findings = []
    for category, fields in _DEAD_IDENTITY_FIELDS.items():
        items = check.get(category)
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                return None
            row = _object(item, category)
            if any(not isinstance(row.get(key), str) or not row[key] for key in fields):
                return None
            if row["path"] not in files:
                raise ValueError(f"dead finding path escapes bound snapshot: {row['path']}")
            identity = {"category": category, **{key: str(row[key]) for key in fields}}
            context = _declaration_context(root, row)
            if context is not None:
                identity["declaration_context"] = context
            findings.append(identity)
    return findings


def _list_count(report: dict[str, object], key: str) -> int | None:
    value = report.get(key)
    return len(value) if isinstance(value, list) else None


_OXC_VERSION = "0.126.0"
_OXC_COMMIT = "836fe9d51c11981162461216f37a067fc14441a7"


def _validated_fallow_inputs(
    report: dict[str, object], config: dict[str, object], root: Path, inputs: set[str]
) -> bool:
    """Pinned producer execution plus strict primary parses closes silent read/parse omissions."""
    validation = report.get("syntax_validation")
    if not isinstance(validation, dict) or report.get("version") != "3.18.0":
        return False
    if any(
        validation.get(key) != expected
        for key, expected in {
            "schema_version": 1,
            "parser": "oxc-parser",
            "version": _OXC_VERSION,
            "commit": _OXC_COMMIT,
            "producer_version": "3.18.0",
            "parser_options": "fallow-3.18.0-default-primary",
            "package_integrity": "sha512-FktCvLby/mOHyuijZt22+nOt10dS24gGUZE3XwIbUg7Kf4+rer3/5T7RgwzazlNuVsCjPloZ3p8E+4ONT3A8Kw==",
        }.items()
    ):
        return False
    # This audited producer path shares discovery with dupes. Other modes need
    # their own observer proof, especially per-analysis production overrides.
    if set(config) - {"$schema", "entry", "production", "ignorePatterns", "health", "duplicates"}:
        return False
    if config.get("production") is not True:
        return False
    for section in ("health", "duplicates"):
        value = config.get(section, {})
        if not isinstance(value, dict) or "production" in value:
            return False
    duplicates = config.get("duplicates", {})
    assert isinstance(duplicates, dict)
    # Default duplicate options are the audited eligibility path. Do not claim
    # completeness for unexamined nested limits, near-clone modes or overrides.
    if duplicates:
        return False
    executions = validation.get("producer_execution")
    validator_execution = validation.get("validator_execution")
    if (
        not isinstance(executions, list)
        or len(executions) != 2
        or not isinstance(validator_execution, dict)
        or validator_execution.get("exit_code") != 0
    ):
        return False
    for index, execution in enumerate(executions):
        if not isinstance(execution, dict) or execution.get("exit_code") not in (0, 1):
            return False
        argv = execution.get("command")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return False
        # Exact built-in command shape excludes diff/workspace/top truncation.
        if (
            len(argv) != 10 + index * 2
            or argv[0] != "fallow"
            or argv[1] != "--root"
            or argv[3] != "--config"
        ):
            return False
        if argv[5:10] != ["--format", "json", "--quiet", "--explain", "--no-cache"]:
            return False
        if index and argv[-2:] != ["list", "--files"]:
            return False
    if not isinstance(validation.get("lock_digest"), str) or not str(
        validation["lock_digest"]
    ).startswith("sha256:"):
        return False
    binding = validation.get("binding")
    if (
        not isinstance(binding, dict)
        or len(binding) != 1
        or any(
            not isinstance(value, dict)
            or value.get("version") != _OXC_VERSION
            or not value.get("integrity")
            for value in binding.values()
        )
    ):
        return False
    for key in ("dependency_hashes", "validator_files"):
        hashes = validation.get(key)
        if (
            not isinstance(hashes, dict)
            or not hashes
            or any(
                not isinstance(value, str) or not value.startswith("sha256:")
                for value in hashes.values()
            )
        ):
            return False
    rows = validation.get("files")
    if not isinstance(rows, list) or len(rows) != len(inputs):
        return False
    observed = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("path") not in inputs:
            return False
        path = row["path"]
        assert isinstance(path, str)
        if path in observed or row.get("status") != "valid" or row.get("errors") != []:
            return False
        try:
            data = (root / path).read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if row.get("source_digest") != _digest(data):
            return False
        observed.add(path)
    return observed == inputs


def _clone_identities(dupes: dict[str, object], files: set[str], root: Path) -> list[str] | None:
    groups = dupes.get("clone_groups")
    if not isinstance(groups, list):
        return None
    identities = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("instances"), list):
            return None
        members = []
        for instance in group["instances"]:
            if not isinstance(instance, dict):
                return None
            path, fragment = instance.get("file"), instance.get("fragment")
            if (
                not isinstance(path, str)
                or path not in files
                or not isinstance(fragment, str)
                or not fragment
            ):
                return None
            try:
                source = (root / path).read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            if fragment not in source:
                return None
            members.append((path, _digest(fragment.encode("utf-8"))))
        if len(members) < 2:
            return None
        identities.append(_digest(json.dumps(sorted(members), separators=(",", ":")).encode()))
    return identities if len(identities) == len(set(identities)) else None


def _fallow(
    report: dict[str, object],
    boundary_rules: bool,
    files: set[str],
    scope: list[str],
    root: Path,
    config: dict[str, object],
    config_digest: str,
) -> dict[str, object]:
    if report.get("kind") != "combined":
        raise ValueError("Fallow evidence must be a combined report")
    check = _object(report.get("check"), "fallow.check")
    dupes = _object(report.get("dupes"), "fallow.dupes")
    health = _object(report.get("health"), "fallow.health")
    findings = health.get("findings")
    if not isinstance(findings, list):
        raise ValueError("fallow.health.findings must be a list")
    functions: list[dict[str, object]] = []
    for item in findings:
        row = _object(item, "function")
        if not isinstance(row.get("path"), str) or not isinstance(row.get("name"), str):
            raise ValueError("Fallow functions require path and name")
        path = row["path"]
        assert isinstance(path, str)
        _source_path(path, files, scope)
        _integer(row.get("line"), "function.line")
        if row.get("col") is not None:
            _integer(row["col"], "function.col")
        functions.append(
            {
                key: row.get(key)
                for key in ("path", "name", "line", "col", "cognitive", "cyclomatic", "line_count")
            }
        )
        for key in ("cognitive", "cyclomatic", "line_count"):
            _integer(row.get(key), f"function.{key}")
    summary_value = health.get("summary", {})
    summary = _object(summary_value, "fallow.health.summary")
    scoped_files = {
        path for path in files if any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)
    }
    supported_suffixes = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"}
    scoped_source_files = {
        path for path in scoped_files if Path(path).suffix.lower() in supported_suffixes
    }
    unsupported_source_files = {
        path
        for path in scoped_files
        if Path(path).suffix.lower() in _SOURCE_SUFFIXES
        and Path(path).suffix.lower() not in supported_suffixes
    }
    identities = {(row["path"], row["name"], row["line"], row["col"]) for row in functions}
    complete_complexity = (
        bool(scoped_source_files)
        and not unsupported_source_files
        and all(type(summary.get(key)) is int for key in ("functions_analyzed", "files_analyzed"))
        and summary.get("functions_analyzed") == len(functions)
        and len(identities) == len(functions)
        and summary.get("files_analyzed") == len(scoped_source_files)
    )
    duplicate_stats = _object(dupes.get("stats", {}), "fallow.dupes.stats")
    dead_counts = {key: _list_count(check, key) for key in _DEAD_FIELDS}
    values = list(dead_counts.values())
    total_dead = sum(value for value in values if value is not None) if None not in values else None
    boundaries = ("boundary_violations", "boundary_coverage_violations", "boundary_call_violations")
    boundary_counts = [_list_count(check, key) for key in boundaries]
    boundary_total = (
        sum(value for value in boundary_counts if value is not None)
        if None not in boundary_counts and boundary_rules
        else None
    )
    # A separate discovery receipt from the same explicit config closes the file population.
    discovery = report.get("discovery")
    discovered = discovery.get("files") if isinstance(discovery, dict) else None
    entries = check.get("entry_points")
    rooted = (
        isinstance(entries, dict) and type(entries.get("total")) is int and entries["total"] > 0
    )
    complete_graph = (
        bool(scoped_source_files)
        and not unsupported_source_files
        and isinstance(discovered, list)
        and all(isinstance(path, str) for path in discovered)
        and isinstance(discovery, dict)
        and discovery.get("file_count") == len(discovered)
        and len(discovered) == len(set(discovered))
        and set(discovered) == scoped_source_files
        and check.get("unresolved_imports") == []
        and rooted
    )
    validation = report.get("syntax_validation")
    syntax_valid = (
        isinstance(validation, dict)
        and validation.get("config_digest") == config_digest
        and isinstance(discovered, list)
        and all(isinstance(path, str) for path in discovered)
        and set(discovered) == scoped_source_files
        and isinstance(discovery, dict)
        and discovery.get("file_count") == len(discovered)
        and len(discovered) == len(set(discovered))
        and _validated_fallow_inputs(report, config, root, scoped_source_files)
    )
    if not syntax_valid:
        complete_complexity = complete_graph = False
    eligible = duplicate_stats.get("total_files")
    groups = dupes.get("clone_groups")
    complete_clones = (
        bool(scoped_source_files)
        and not unsupported_source_files
        and syntax_valid
        and type(eligible) is int
        and 0 <= eligible <= len(scoped_source_files)
        and isinstance(groups, list)
        and duplicate_stats.get("clone_groups") == len(groups)
    )
    return {
        **_function_population(functions),
        "complexity_complete_for_declared_scope": complete_complexity,
        "complexity_detection": "complete for declared source scope"
        if complete_complexity
        else "reported functions only; configured thresholds and overrides can omit functions",
        "scoped_supported_files": sorted(scoped_source_files),
        "scoped_unsupported_files": sorted(unsupported_source_files),
        "dead_code_complete_for_declared_scope": complete_graph,
        "cycles_complete_for_declared_scope": complete_graph,
        "boundary_violations_complete_for_declared_scope": complete_graph and boundary_rules,
        "graph_detection": "matching discovery population, declared entries, no unresolved imports; static findings only"
        if complete_graph
        else "reported findings only; scoped graph population or import resolution incomplete",
        "clone_groups_complete_for_declared_scope": complete_clones,
        "clone_detection": "complete configured Fallow analysis; eligibility count producer-reported"
        if complete_clones
        else "reported findings only; scoped clone-file population is unsupported or unmeasured",
        "dead_code": total_dead,
        "dead_code_categories": dead_counts,
        "dead_finding_identities": _dead_identities(check, files, root),
        "clone_input_file_count": len(scoped_source_files),
        "clone_eligible_stream_count": eligible,
        "clone_finding_identities": _clone_identities(dupes, files, root),
        "clone_groups": _list_count(dupes, "clone_groups"),
        "cycles": _list_count(check, "circular_dependencies"),
        "boundary_violations": boundary_total,
        "boundary_rules_declared": boundary_rules,
        "unresolved_imports": _list_count(check, "unresolved_imports"),
    }


_THRESHOLDS = {
    "fallow": {
        "max_cognitive": "cognitive",
        "max_cyclomatic": "cyclomatic",
        "max_unit_size": "line_count",
        "max_dead_code": "dead_code",
        "max_clone_groups": "clone_groups",
        "max_cycles": "cycles",
        "max_boundary_violations": "boundary_violations",
    },
    "coverage": {
        "min_branch_percent": "branch_percent",
        "min_function_percent": "function_percent",
    },
    "mutation": {"min_score_percent": "score_percent"},
}


def _validate_thresholds(kind: str, standards: dict[str, object]) -> None:
    for key, limit in standards.items():
        if key not in _THRESHOLDS[kind]:
            raise ValueError(f"unsupported {kind} threshold: {key}")
        threshold = _number(limit, key)
        if threshold < 0 or (key.startswith("min_") and threshold > 100):
            raise ValueError(f"invalid threshold: {key}")
        if kind == "fallow":
            _integer(limit, key)
            if key in {"max_cyclomatic", "max_unit_size"} and threshold < 1:
                raise ValueError(f"invalid threshold: {key}")


def _violations(
    kind: str, metrics: dict[str, object], standards: dict[str, object]
) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    unknown: list[str] = []
    for key, limit in standards.items():
        threshold = _number(limit, key)
        metric = _THRESHOLDS[kind][key]
        if kind == "fallow" and metric in {"cognitive", "cyclomatic", "line_count"}:
            rows = metrics["functions"]
            assert isinstance(rows, list)
            exceeded = any(
                _number(_object(row, "function")[metric], metric) > threshold for row in rows
            )
            if exceeded:
                violations.append(key)
            elif not metrics.get("complexity_complete_for_declared_scope"):
                unknown.append(key)
        else:
            value = metrics[metric]
            if value is None:
                unknown.append(key)
            elif (
                _number(value, metric) < threshold
                if key.startswith("min_")
                else _number(value, metric) > threshold
            ):
                violations.append(key)
            elif kind == "fallow" and (
                metrics.get("unresolved_imports") != 0
                or not metrics.get(f"{metric}_complete_for_declared_scope")
            ):
                unknown.append(key)
            elif kind == "coverage" and not metrics.get("population_complete_for_declared_scope"):
                unknown.append(key)
    return violations, unknown


def evaluate_evidence(
    snapshot_root: Path,
    evidence_refs: dict[str, str] | None,
    standards: dict[str, object],
    base_dir: Path,
) -> dict[str, object]:
    """Import bound reports, without running analyzers or mutating submitted source."""
    snapshot = inspect_snapshot(snapshot_root)
    refs = evidence_refs or {}
    files = set(_object(snapshot["files"], "snapshot files"))
    if set(refs) - set(_THRESHOLDS) or set(standards) - set(_THRESHOLDS):
        raise ValueError("unsupported evidence kind")
    result: dict[str, object] = {"snapshot_digest": snapshot["digest"]}
    for kind in _THRESHOLDS:
        limits = _object(standards.get(kind, {}), f"standards.{kind}")
        _validate_thresholds(kind, limits)
        if kind not in refs:
            result[kind] = {
                "status": "unavailable",
                "reason": "no source-bound report",
                "metrics": None,
                "violations": [],
                "unknown_standards": sorted(limits),
            }
            continue
        envelope_path = base_dir / refs[kind]
        envelope = _load(envelope_path)
        required = {
            "schema_version",
            "snapshot_digest",
            "tool",
            "tool_version",
            "config",
            "config_digest",
            "scope",
            "report",
            "report_digest",
        }
        if required - set(envelope) or set(envelope) - required - {"boundary_rules_declared"}:
            raise ValueError(f"invalid {kind} evidence envelope fields")
        if (
            type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != 1
            or envelope["snapshot_digest"] != snapshot["digest"]
        ):
            raise ValueError(f"{kind} evidence does not bind to this snapshot")
        for key in ("tool", "tool_version", "config", "config_digest", "report", "report_digest"):
            if not isinstance(envelope[key], str) or not envelope[key]:
                raise ValueError(f"{kind}.{key} must be a nonempty string")
        scope = _strings(envelope["scope"], f"{kind}.scope")
        if not scope:
            raise ValueError(f"{kind} analysis scope must be explicit")
        config_name = envelope["config"]
        assert isinstance(config_name, str)
        config_path = envelope_path.parent / config_name
        if _digest(config_path.read_bytes()) != envelope["config_digest"]:
            raise ValueError(f"{kind} config digest mismatch")
        config = _load(config_path)
        if config.get("extends"):
            raise ValueError("evidence requires fully resolved JSON configuration without extends")
        report_name = envelope["report"]
        assert isinstance(report_name, str)
        report_path = envelope_path.parent / report_name
        if _digest(report_path.read_bytes()) != envelope["report_digest"]:
            raise ValueError(f"{kind} report digest mismatch")
        report = _load(report_path)
        boundary_rules = envelope.get("boundary_rules_declared", False)
        if not isinstance(boundary_rules, bool):
            raise ValueError("boundary_rules_declared must be boolean")
        if kind == "fallow" and boundary_rules:
            boundaries = _object(config.get("boundaries", {}), "config.boundaries")
            calls = _object(boundaries.get("calls", {}), "config.boundaries.calls")
            coverage = _object(boundaries.get("coverage", {}), "config.boundaries.coverage")
            boundary_rules = bool(
                boundaries.get("preset")
                or boundaries.get("rules")
                or calls.get("forbidden")
                or coverage.get("requireAllFiles")
            )
        if kind == "fallow":
            if report.get("version") != envelope["tool_version"] or envelope["tool"] != "fallow":
                raise ValueError("Fallow report version/tool disagrees with evidence envelope")
            metrics = _fallow(
                report,
                boundary_rules,
                files,
                scope,
                snapshot_root,
                config,
                str(envelope["config_digest"]),
            )
        elif kind == "coverage":
            if report.get("clock") != config.get("clock"):
                raise ValueError("coverage clock differs from its bound configuration")
            if (
                report.get("contribution") is not None
                and report.get("source_digest") != snapshot["digest"]
            ):
                raise ValueError("test-contribution report does not bind to this source snapshot")
            if report.get("source_scope") is not None and sorted(
                _strings(report["source_scope"], "coverage source scope")
            ) != sorted(scope):
                raise ValueError("coverage source scope differs from its bound envelope")
            if report.get("contribution") is not None:
                for key, default in (("test_scope", None), ("excluded_test_files", [])):
                    declared = _strings(report.get(key, default), f"coverage {key}")
                    configured = _strings(config.get(key, default), f"coverage config {key}")
                    if sorted(name.removeprefix("./") for name in declared) != sorted(
                        name.removeprefix("./") for name in configured
                    ):
                        raise ValueError(f"coverage {key} differs from its bound configuration")
                if config.get("reference_test_files") is not None:
                    required_tests = _strings(
                        config["reference_test_files"], "required reference tests"
                    )
                    if report.get(
                        "selection_policy"
                    ) != "fixed-reference-plus-all-changed-test-files-v1" or sorted(
                        _strings(report.get("reference_test_files"), "reported reference tests")
                    ) != sorted(required_tests):
                        raise ValueError(
                            "coverage reference selection differs from its bound configuration"
                        )
            metrics = _coverage(report, files, scope)
        else:
            metrics = _mutation(report, files, scope)
        violations, unknown = _violations(kind, metrics, limits)
        provenance = {
            key: envelope[key] for key in ("tool", "tool_version", "config_digest", "scope")
        }
        if kind == "fallow" and isinstance(report.get("syntax_validation"), dict):
            validation = _object(report["syntax_validation"], "syntax validation")
            provenance["syntax_observer"] = {
                key: validation.get(key)
                for key in (
                    "parser",
                    "version",
                    "commit",
                    "parser_options",
                    "lock_digest",
                    "dependency_hashes",
                    "validator_files",
                )
            }
        result[kind] = {
            "status": "incomplete" if unknown else "available",
            "provenance": provenance,
            "metrics": metrics,
            "violations": violations,
            "unknown_standards": unknown,
            "evidence_basis": "externally supplied source-bound report; execution not independently attested",
        }
    return result


@dataclass(frozen=True)
class EvidenceBinding:
    snapshot: Path
    report: Path
    config: Path
    tool: str
    tool_version: str
    scope: tuple[str, ...]
    boundary_rules_declared: bool = False


def bind_evidence(binding: EvidenceBinding) -> dict[str, object]:
    """Build a receipt for existing local reports; this does not attest tool execution."""
    config = _load(binding.config)
    if config.get("extends"):
        raise ValueError("evidence requires fully resolved JSON configuration without extends")
    if not binding.tool or not binding.tool_version or not binding.scope:
        raise ValueError("tool, version and source scope must be explicit")
    return {
        "schema_version": 1,
        "snapshot_digest": inspect_snapshot(binding.snapshot)["digest"],
        "tool": binding.tool,
        "tool_version": binding.tool_version,
        "config": str(binding.config.resolve()),
        "config_digest": _digest(binding.config.read_bytes()),
        "scope": list(binding.scope),
        "report": str(binding.report.resolve()),
        "report_digest": _digest(binding.report.read_bytes()),
        "boundary_rules_declared": binding.boundary_rules_declared,
    }


def _function_population(functions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "functions": functions,
        "function_count_reported": len(functions),
        "complexity_distribution": {
            key: sorted(_integer(row[key], key) for row in functions)
            for key in ("cognitive", "cyclomatic", "line_count")
        },
        **{
            f"max_{name}_reported": max(
                (_integer(row[field], field) for row in functions), default=None
            )
            for name, field in (
                ("cognitive", "cognitive"),
                ("cyclomatic", "cyclomatic"),
                ("unit_size", "line_count"),
            )
        },
    }


def _overlaps_change(row: dict[str, object], ranges: object) -> bool:
    if not isinstance(ranges, list):
        return False
    first = _integer(row["line"], "function.line")
    last = first + _integer(row["line_count"], "function.line_count") - 1
    for item in ranges:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("changed-line range requires start and count")
        start, count = (_integer(value, "changed-line range") for value in item)
        # A zero-width edit is after start, so insertions before/after a function
        # do not turn an unchanged, shifted declaration into a changed function.
        if (count and first <= start + count - 1 and start <= last) or (
            count == 0 and first <= start < last
        ):
            return True
    return False


def _function_changes(
    left: dict[str, object], right: dict[str, object], change: dict[str, object] | None
) -> dict[str, object]:
    unavailable = {"status": "unavailable", "reason": "bound source diff or function rows missing"}
    if change is None or not all(isinstance(row.get("functions"), list) for row in (left, right)):
        return {"changed_functions": unavailable, "new_file_functions": unavailable}
    ranges = _object(change.get("changed_line_ranges"), "changed-line ranges")
    populations = []
    complete = all(
        row.get("complexity_complete_for_declared_scope") is True for row in (left, right)
    )
    for side, metrics in (("before", left), ("after", right)):
        raw = metrics["functions"]
        assert isinstance(raw, list)
        functions = [_object(row, "function") for row in raw]
        populations.append(
            {
                **_function_population(
                    [
                        row
                        for row in functions
                        if _overlaps_change(
                            row, _object(ranges.get(str(row["path"]), {}), "file ranges").get(side)
                        )
                    ]
                ),
                "complete_for_declared_scope": metrics.get("complexity_complete_for_declared_scope")
                is True,
            }
        )
    raw_after = right["functions"]
    assert isinstance(raw_after, list)
    new_paths = set(_strings(change.get("new_paths"), "new paths"))
    new_functions = [
        _object(row, "function")
        for row in raw_after
        if _object(row, "function")["path"] in new_paths
    ]
    status = "available" if complete else "incomplete"
    return {
        "changed_functions": {
            "status": status,
            "before": populations[0],
            "after": populations[1],
            "method": "reported function line..line+line_count-1 overlaps a zero-context Git hunk on that snapshot; zero-width edits strictly inside the span count; includes tests when scoped",
            "limitations": "line-level overlap, not AST identity or semantic change; enclosing/nested spans may both count, same-line changes may overcount, moves/renames count as changes; before/after populations are not matched functions",
        },
        "new_file_functions": {
            "status": "available"
            if right.get("complexity_complete_for_declared_scope") is True
            else "incomplete",
            "after": {
                **_function_population(new_functions),
                "complete_for_declared_scope": right.get("complexity_complete_for_declared_scope")
                is True,
            },
            "paths": sorted(new_paths),
            "method": "reported functions in paths absent from before; no new-function classification inside existing files",
        },
    }


def _dead_changes(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    identities = [row.get("dead_finding_identities") for row in (left, right)]
    if not all(
        row.get("dead_code_complete_for_declared_scope") is True for row in (left, right)
    ) or not all(isinstance(value, list) for value in identities):
        return {
            "status": "unavailable",
            "reason": "complete graph populations and unambiguous declaration identities required on both snapshots",
            "introduced_count": None,
            "resolved_count": None,
        }
    bases = []
    for items in identities:
        assert isinstance(items, list)
        bases.append(
            [
                {
                    key: value
                    for key, value in _object(row, "dead identity").items()
                    if key != "declaration_context"
                }
                for row in items
            ]
        )
    collisions = {
        identity
        for group in bases
        for identity, count in Counter(json.dumps(row, sort_keys=True) for row in group).items()
        if count > 1
    }
    sets = []
    for items, group in zip(identities, bases, strict=True):
        assert isinstance(items, list)
        matched = {}
        for row, base in zip(items, group, strict=True):
            identity = dict(base)
            if json.dumps(base, sort_keys=True) in collisions:
                context = _object(row, "dead identity").get("declaration_context")
                if not isinstance(context, str):
                    return {
                        "status": "unavailable",
                        "reason": "colliding declarations lack bound unique context",
                        "introduced_count": None,
                        "resolved_count": None,
                    }
                identity["declaration_context"] = context
            encoded = json.dumps(identity, sort_keys=True)
            if encoded in matched:
                return {
                    "status": "unavailable",
                    "reason": "colliding declarations lack bound unique context",
                    "introduced_count": None,
                    "resolved_count": None,
                }
            matched[encoded] = identity
        sets.append(matched)
    introduced = [sets[1][key] for key in sorted(sets[1].keys() - sets[0].keys())]
    resolved = [sets[0][key] for key in sorted(sets[0].keys() - sets[1].keys())]
    return {
        "status": "available",
        "introduced": introduced,
        "resolved": resolved,
        "introduced_count": len(introduced),
        "resolved_count": len(resolved),
        "before_count": len(sets[0]),
        "after_count": len(sets[1]),
        "method": "category plus declaration path/symbol fields; colliding identities in either snapshot additionally use unique bound declaration-line context, excluding coordinates/actions; matching tool/version/config/scope and complete static graph populations required",
        "limitations": "static finding changes only, not semantic deadness; moves/renames and changed colliding declaration-line content change identity; identical collision contexts remain unknown; externally used APIs and runtime-only references are outside this evidence",
    }


def _clone_changes(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    before, after = left.get("clone_finding_identities"), right.get("clone_finding_identities")
    if (
        not left.get("clone_groups_complete_for_declared_scope")
        or not right.get("clone_groups_complete_for_declared_scope")
        or not isinstance(before, list)
        or not isinstance(after, list)
    ):
        return {
            "status": "unavailable",
            "introduced_count": None,
            "resolved_count": None,
            "reason": "complete configured analysis or unique source-bound clone identities missing",
        }
    return {
        "status": "available",
        "introduced_count": len(set(after) - set(before)),
        "resolved_count": len(set(before) - set(after)),
        "before_count": len(before),
        "after_count": len(after),
        "method": "exact reported clone-group path/content membership, independent of line shifts",
        "limitations": [
            "Fragment or membership edits change group identity; these are configured static findings, not semantic redundancy."
        ],
    }


def compare_evidence(
    before: dict[str, object], after: dict[str, object], *, change: dict[str, object] | None = None
) -> dict[str, object]:
    """Compare measurements only with compatible collection and denominator provenance."""
    result: dict[str, object] = {}
    for kind in _THRESHOLDS:
        left = _object(before.get(kind, {}), f"before.{kind}")
        right = _object(after.get(kind, {}), f"after.{kind}")
        if not isinstance(left.get("metrics"), dict) or not isinstance(right.get("metrics"), dict):
            result[kind] = {
                "status": "unavailable",
                "reason": "before or after evidence missing",
                "deltas": None,
            }
            continue
        left_provenance = _object(left.get("provenance"), "before provenance")
        right_provenance = _object(right.get("provenance"), "after provenance")
        if left_provenance != right_provenance:
            result[kind] = {
                "status": "incomplete",
                "reason": "tool/version/config/scope differ",
                "deltas": None,
            }
            continue
        left_metrics = _object(left["metrics"], "before metrics")
        right_metrics = _object(right["metrics"], "after metrics")
        denominator_keys = (
            ("branch_denominator_digest", "function_denominator_digest")
            if kind == "coverage"
            else ("mutant_set_digest", "eligible_mutant_set_digest", "mutant_source_digest")
            if kind == "mutation"
            else ()
        )
        if any(
            not isinstance(left_metrics.get(key), str)
            or not isinstance(right_metrics.get(key), str)
            or left_metrics.get(key) != right_metrics.get(key)
            for key in denominator_keys
        ):
            result[kind] = {
                "status": "incomplete",
                "reason": "fixed denominator or mutant source identity differs or is unbound",
                "deltas": None,
            }
            continue
        deltas: dict[str, float] = {}
        for key, left_value in left_metrics.items():
            right_value = right_metrics.get(key)
            if (
                isinstance(left_value, (int, float))
                and not isinstance(left_value, bool)
                and isinstance(right_value, (int, float))
                and not isinstance(right_value, bool)
            ):
                deltas[key] = float(right_value) - float(left_value)
        result[kind] = {
            "status": "available",
            "deltas": deltas,
            "interpretation": "net changes in compatible reported metrics; not identity-grounded newly introduced findings",
        }
        if kind == "fallow":
            if change is not None and (
                change.get("before_digest") != before.get("snapshot_digest")
                or change.get("after_digest") != after.get("snapshot_digest")
            ):
                raise ValueError("changed-function diff does not bind to evidence snapshots")
            comparison = _object(result[kind], "fallow comparison")
            comparison.update(_function_changes(left_metrics, right_metrics, change))
            comparison["dead_findings"] = _dead_changes(left_metrics, right_metrics)
            comparison["clone_findings"] = _clone_changes(left_metrics, right_metrics)
            result[kind] = comparison
    return result
