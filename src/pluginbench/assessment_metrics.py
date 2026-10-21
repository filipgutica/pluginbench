"""Offline, source-bound measurements of immutable submitted snapshots."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import math
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping
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


DUPLICATION_WINDOW = 6


def _test_duplication(patch: str) -> dict[str, object]:
    """Added test lines inside a run of consecutive added lines that repeats elsewhere in added tests."""
    hunks: list[list[str]] = []
    new_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            new_path = None
        elif line.startswith("+++ "):
            new_path = _marker_path(line[4:])
        elif line.startswith("@@ "):
            hunks.append([])
        elif (
            line.startswith("+")
            and new_path is not None
            and _is_test_path(new_path)
            and Path(new_path).suffix.lower() in _SOURCE_SUFFIXES
            and hunks
        ):
            text = " ".join(line[1:].split())
            if text:
                hunks[-1].append(text)
    windows: Counter[tuple[str, ...]] = Counter(
        tuple(hunk[index : index + DUPLICATION_WINDOW])
        for hunk in hunks
        for index in range(len(hunk) - DUPLICATION_WINDOW + 1)
    )
    duplicated = 0
    for hunk in hunks:
        marked = [False] * len(hunk)
        for index in range(len(hunk) - DUPLICATION_WINDOW + 1):
            if windows[tuple(hunk[index : index + DUPLICATION_WINDOW])] > 1:
                marked[index : index + DUPLICATION_WINDOW] = [True] * DUPLICATION_WINDOW
        duplicated += sum(marked)
    return {
        "window_lines": DUPLICATION_WINDOW,
        "added_test_lines": sum(len(hunk) for hunk in hunks),
        "duplicated_added_test_lines": duplicated,
        "method": "whitespace-normalized non-blank added lines in test source files (fixture and snapshot data excluded); a line is duplicated when it belongs to a run of consecutive added lines that appears more than once in added test code; copies of existing tests are not detected",
    }


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
        metrics["test_duplication"] = _test_duplication(patch)
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
                if kind == "test" and Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
                    continue
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
    if not {"total", "covered"} <= set(row) or set(row) - {
        "total",
        "covered",
        "unit",
        "weights",
        "files_measured",
    }:
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


def _coverage_limits(
    report: dict[str, object], files: set[str], scope: list[str]
) -> dict[str, object]:
    limits = _object(report.get("unavailable_populations", {}), "unavailable populations")
    for kind, value in limits.items():
        row = _object(value, "unavailable population")
        if kind not in {"lines", "statements", "branches", "functions"} or not isinstance(
            row.get("reason"), str
        ):
            raise ValueError("Unavailable coverage requires a known unit and reason")
        for path in _strings(row.get("files"), "unavailable coverage files"):
            _source_path(path, files, scope)
    return limits


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
    populations = {
        kind: _coordinates(report[kind], kind, files, scope)
        for kind in ("lines", "statements", "branches", "functions")
        if kind in report
    }
    if not populations:
        raise ValueError("coverage requires at least one explicit measured population")
    covered_branches = populations.get("branches", (set(), set()))[1]
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
        and _is_source_path(path)
        and Path(path).suffix.lower() in _SOURCE_SUFFIXES
        and not path.endswith((".d.ts", ".d.mts", ".d.cts"))
    }
    units: dict[str, object] = {}
    for kind, singular in (
        ("lines", "line"),
        ("statements", "statement"),
        ("branches", "branch"),
        ("functions", "function"),
    ):
        if kind not in populations:
            units.update(
                {
                    f"{singular}_{suffix}": None
                    for suffix in ("total", "covered", "percent", "denominator_digest")
                }
            )
            units[f"{singular}_population_complete"] = False
            continue
        total, covered = populations[kind]
        row = _object(report[kind], kind)
        if row.get("unit", singular) != singular:
            raise ValueError(f"coverage {kind} uses an incompatible unit")
        weights = _object(row.get("weights", {key: 1 for key in total}), f"{kind}.weights")
        if set(weights) != total:
            raise ValueError("coverage weights must cover the complete coordinate population")
        counts = {key: _integer(value, "coverage weight") for key, value in weights.items()}
        denominator = sum(counts.values())
        numerator = sum(counts[key] for key in covered)
        unit_files = set(
            _strings(row.get("files_measured", sorted(measured)), f"{kind}.files_measured")
        )
        for path in unit_files:
            _source_path(path, files, scope)
        units.update(
            {
                f"{singular}_total": denominator,
                f"{singular}_covered": numerator,
                f"{singular}_percent": _percent(numerator, denominator),
                f"{singular}_denominator_digest": _digest(
                    json.dumps(sorted(counts.items())).encode()
                ),
                f"{singular}_population_complete": bool(expected) and expected <= unit_files,
            }
        )
    lines_total = populations.get("lines", (set(), set()))[0]
    per_test_numbits = None
    if report.get("per_test_numbits") is not None:
        per_test_numbits = {
            name: {
                path: str(encoded)
                for path, encoded in _object(files, f"per_test_numbits.{name}").items()
            }
            for name, files in _object(report["per_test_numbits"], "per_test_numbits").items()
        }
        if any(
            not _numbit_coordinates(files) <= lines_total for files in per_test_numbits.values()
        ):
            raise ValueError("per_test_numbits includes unmeasured line coordinates")
    return {
        **units,
        # Coordinates support changed-line coverage; per-test lines support redundancy.
        "coordinates": {
            unit: {"total": sorted(populations[kind][0]), "covered": sorted(populations[kind][1])}
            for kind, unit in (("lines", "line"), ("branches", "branch"))
            if kind in populations
        },
        # Compact per-test lines (coverage.py numbits per file); decoded only where needed.
        "per_test_numbits": per_test_numbits,
        "available_units": [
            unit
            for kind, unit in (
                ("lines", "line"),
                ("statements", "statement"),
                ("branches", "branch"),
                ("functions", "function"),
            )
            if kind in populations
        ],
        "unavailable_populations": _coverage_limits(report, files, scope),
        "format": report.get("format"),
        "test_command_exit_code": report.get("test_command_exit_code"),
        "population_complete_for_declared_scope": bool(expected) and expected <= measured,
        "files_measured": sorted(measured),
        "unmeasured_source_files": sorted(expected - measured),
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


def _analysis_functions(
    report: dict[str, object], *, analyzed: set[str], root: Path, complete: dict[str, bool]
) -> list[dict[str, object]]:
    raw = report.get("functions")
    if not isinstance(raw, list):
        raise ValueError("analysis requires function rows")
    functions = []
    seen = set()
    for value in raw:
        row = _object(value, "analysis function")
        path = row.get("path")
        if (
            not isinstance(path, str)
            or path not in analyzed
            or not isinstance(row.get("name"), str)
        ):
            raise ValueError("analysis function escapes analyzed source")
        first = _integer(row.get("line"), "function line")
        last = _integer(row.get("end_line"), "function end line")
        if (
            not 1 <= first <= last <= len((root / path).read_text().splitlines())
            or row.get("line_count") != last - first + 1
        ):
            raise ValueError("analysis function span escapes source")
        identity = (path, first, row.get("col"), row["name"])
        if identity in seen:
            raise ValueError("duplicate analysis function")
        seen.add(identity)
        for key in ("cognitive", "cyclomatic"):
            if complete[key]:
                _integer(row.get(key), key)
            elif row.get(key) is not None:
                raise ValueError("unavailable analysis metric must not provide scores")
        functions.append(row)
    return functions


def _python_declarations(source: str) -> dict[tuple[int, str], list[dict[str, object]]]:
    """Bind declarations to lexical scope and structure, never occurrence ordinals."""
    declarations: dict[tuple[int, str], list[dict[str, object]]] = {}
    scopes: Counter[tuple[str, ...]] = Counter()

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        symbol = None
        role = None
        line = getattr(node, "lineno", None)
        outer_scope = scope
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            overload_signature = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                (isinstance(item, ast.Name) and item.id == "overload")
                or (isinstance(item, ast.Attribute) and item.attr == "overload")
                for item in node.decorator_list
            ):
                overload_signature = [
                    ast.dump(node.args),
                    ast.dump(node.returns) if node.returns else None,
                ]
            token = json.dumps(
                [
                    type(node).__name__,
                    node.name,
                    [ast.dump(item) for item in node.decorator_list],
                    overload_signature,
                ]
            )
            scope = (*scope, token)
            scopes[scope] += 1
            symbol, role = node.name, "class" if isinstance(node, ast.ClassDef) else "function"
            if node.decorator_list:
                line = node.decorator_list[0].lineno
        elif isinstance(node, ast.Lambda):
            scope = (*scope, "Lambda:" + _digest(ast.dump(node).encode()))
            scopes[scope] += 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            scope = (*scope, "unsupported:" + type(node).__name__)
        elif isinstance(node, ast.arg):
            symbol, role = node.arg, "parameter"
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            symbol, role = node.id, "variable"
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            symbol, role = node.attr, "attribute"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                declarations.setdefault(
                    (node.lineno, alias.asname or alias.name.split(".")[0]), []
                ).append({"declaration_scope": list(scope), "declaration_role": "import"})
        elif isinstance(node, ast.ExceptHandler) and node.name:
            symbol, role = node.name, "variable"
        if symbol is not None:
            assert isinstance(line, int)
            declarations.setdefault((line, symbol), []).append(
                {"declaration_scope": list(scope), "declaration_role": role}
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            arguments += [item for item in (node.args.vararg, node.args.kwarg) if item is not None]
            for argument in arguments:
                visit(argument, scope)
                if argument.annotation is not None:
                    visit(argument.annotation, outer_scope)
            for expression in [*node.args.defaults, *node.args.kw_defaults]:
                if expression is not None:
                    visit(expression, outer_scope)
            if isinstance(node, ast.Lambda):
                visit(node.body, scope)
            else:
                for decorator in node.decorator_list:
                    visit(decorator, outer_scope)
                if node.returns is not None:
                    visit(node.returns, outer_scope)
                for statement in node.body:
                    visit(statement, scope)
        elif isinstance(node, ast.ClassDef):
            for class_child in [*node.bases, *node.keywords, *node.decorator_list]:
                visit(class_child, outer_scope)
            for statement in node.body:
                visit(statement, scope)
        elif not isinstance(node, ast.arg):
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

    visit(ast.parse(source), ())
    lines = source.splitlines()
    contexts = Counter(
        json.dumps(
            [row["declaration_scope"], row["declaration_role"], symbol, lines[line - 1].strip()]
        )
        for (line, symbol), rows in declarations.items()
        for row in rows
    )
    bindings = Counter(
        json.dumps([row["declaration_scope"], row["declaration_role"], symbol])
        for (_, symbol), rows in declarations.items()
        for row in rows
    )
    for (line, symbol), rows in declarations.items():
        for row in rows:
            scope = row["declaration_scope"]
            assert isinstance(scope, list)
            row["declaration_context_required"] = (
                bindings[json.dumps([scope, row["declaration_role"], symbol])] > 1
            )
            row["declaration_bound"] = contexts[
                json.dumps([scope, row["declaration_role"], symbol, lines[line - 1].strip()])
            ] == 1 and not any(
                token.startswith("unsupported:") or scopes[tuple(scope[: index + 1])] > 1
                for index, token in enumerate(scope)
            )
    return declarations


def _analysis_candidates(
    report: dict[str, object], *, analyzed: set[str], root: Path
) -> tuple[list[object], list[dict[str, object]]]:
    candidates = report.get("unused_candidates")
    if not isinstance(candidates, list):
        raise ValueError("analysis requires unused candidate rows")
    identities = []
    python_sources: dict[str, dict[tuple[int, str], list[dict[str, object]]]] = {}
    for value in candidates:
        row = _object(value, "unused candidate")
        path = row.get("path")
        if not isinstance(path, str) or path not in analyzed:
            raise ValueError("unused candidate escapes analyzed source")
        line = _integer(row.get("line"), "candidate line")
        lines = (root / path).read_text().splitlines()
        if not 1 <= line <= len(lines):
            raise ValueError("unused candidate line escapes source")
        if not all(isinstance(row.get(key), str) and row[key] for key in ("category", "symbol")):
            raise ValueError("unused candidate requires rule and symbol")
        identity = {
            "category": row["category"],
            "path": path,
            "symbol": row["symbol"],
            "declaration_context": lines[line - 1].strip(),
        }
        if report.get("language") == "python":
            if path not in python_sources:
                try:
                    python_sources[path] = _python_declarations((root / path).read_text())
                except SyntaxError:
                    python_sources[path] = {}
            roles = {
                "vulture-unused-variable": {"variable", "parameter"},
                "vulture-unused-attribute": {"attribute"},
                "vulture-unused-import": {"import"},
                "vulture-unused-class": {"class"},
                "vulture-unused-function": {"function"},
                "vulture-unused-method": {"function"},
                "vulture-unused-property": {"function"},
            }.get(str(row["category"]), set())
            bindings = [
                item
                for item in python_sources[path].get((line, str(row["symbol"])), [])
                if item["declaration_role"] in roles
            ]
            identity.update(bindings[0] if len(bindings) == 1 else {"declaration_bound": False})
        identities.append(identity)
    return candidates, identities


def _analysis(
    report: dict[str, object],
    files: set[str],
    scope: list[str],
    root: Path,
    config: dict[str, object],
) -> dict[str, object]:
    if report.get("version") != "language-analysis-v1" or report.get("language") != config.get(
        "language"
    ):
        raise ValueError("language analysis version or language differs from frozen configuration")
    if report.get("tools") != config.get("tool_identities"):
        raise ValueError("analysis tools differ from frozen configuration")
    if report.get("settings") != config.get("settings") or report.get(
        "supported_suffixes"
    ) != config.get("supported_suffixes"):
        raise ValueError("analysis settings or source language differ from frozen configuration")
    suffixes = _strings(report.get("supported_suffixes"), "analysis suffixes")
    expected = {
        path
        for path in files
        if Path(path).suffix in suffixes
        and not _is_test_path(path)
        and any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)
    }
    analyzed = set(_strings(report.get("analyzed_paths"), "analyzed paths"))
    if analyzed != expected:
        raise ValueError("analysis paths differ from the declared source population")
    statuses = _object(report.get("metrics"), "analysis metrics")
    complete = {
        key: _object(statuses.get(key), key).get("status") == "available"
        for key in ("cognitive", "cyclomatic", "dead_code")
    }
    functions = _analysis_functions(report, analyzed=analyzed, root=root, complete=complete)
    candidates, identities = _analysis_candidates(report, analyzed=analyzed, root=root)
    return {
        **_function_population(functions),
        "complexity_complete_for_declared_scope": complete["cognitive"] and complete["cyclomatic"],
        "metric_statuses": statuses,
        "dead_code": len(candidates) if complete["dead_code"] else None,
        "dead_finding_identities": identities if complete["dead_code"] else None,
        "dead_code_complete_for_declared_scope": complete["dead_code"],
        "dead_code_detection": "unused candidates under the named analyzer and build settings; not semantic deadness or absence proof",
        "analyzed_paths": sorted(analyzed),
        "unused_candidates": candidates,
        "language": report["language"],
        "tools": report["tools"],
        "settings": report.get("settings"),
    }


def _fallow_production_excluded(path: str) -> bool:
    # Pinned Fallow 3.18.0 discover/walk.rs uses literal directory separators.
    parts = Path(path).parts
    if any(
        part in {"__tests__", "__mocks__", "__snapshots__", "__fixtures__", "test", "tests"}
        for part in parts[:-1]
    ):
        return True
    patterns = (
        "*.test.*",
        "*.spec.*",
        "*.e2e.*",
        "*.e2e-spec.*",
        "*.bench.*",
        "*.fixture.*",
        "*.stories.*",
        "*.story.*",
        ".*.js",
        ".*.ts",
        ".*.mjs",
        ".*.cjs",
    )
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in patterns) or (
        len(parts) == 1 and fnmatch.fnmatchcase(parts[-1], "*.config.*")
    )


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
        path
        for path in files
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)
        and not any(
            fnmatch.fnmatchcase(path, pattern)
            for pattern in _strings(config.get("ignorePatterns", []), "Fallow ignored paths")
        )
        and (config.get("production") is not True or not _fallow_production_excluded(path))
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


_EVIDENCE_KINDS = ("fallow", "analysis", "coverage")


def evaluate_evidence(
    snapshot_root: Path,
    evidence_refs: dict[str, str] | None,
    base_dir: Path,
) -> dict[str, object]:
    """Import bound reports, without running analyzers or mutating submitted source."""
    snapshot = inspect_snapshot(snapshot_root)
    refs = evidence_refs or {}
    files = set(_object(snapshot["files"], "snapshot files"))
    if set(refs) - set(_EVIDENCE_KINDS):
        raise ValueError("unsupported evidence kind")
    result: dict[str, object] = {"snapshot_digest": snapshot["digest"]}
    for kind in _EVIDENCE_KINDS:
        if kind not in refs:
            result[kind] = {
                "status": "unavailable",
                "reason": "no source-bound report",
                "metrics": None,
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
        elif kind == "analysis":
            if envelope["tool"] != "language-analysis" or envelope["tool_version"] != report.get(
                "version"
            ):
                raise ValueError("language analysis envelope disagrees with report")
            metrics = _analysis(report, files, scope, snapshot_root, config)
        elif kind == "coverage":
            if report.get("clock") != config.get("clock"):
                raise ValueError("coverage clock differs from its bound configuration")
            if report.get("source_scope") is not None and sorted(
                _strings(report["source_scope"], "coverage source scope")
            ) != sorted(scope):
                raise ValueError("coverage source scope differs from its bound envelope")
            metrics = _coverage(report, files, scope)
        provenance = {
            key: envelope[key] for key in ("tool", "tool_version", "config_digest", "scope")
        }
        if kind == "analysis":
            provenance.update(
                language=report.get("language"),
                analyzers=report.get("tools"),
                metric_settings=report.get("settings"),
            )
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
            "status": "available",
            "provenance": provenance,
            "metrics": metrics,
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
            key: sorted(_integer(row[key], key) for row in functions if row.get(key) is not None)
            for key in ("cognitive", "cyclomatic", "line_count")
        },
        **{
            f"max_{name}_reported": max(
                (_integer(row[field], field) for row in functions if row.get(field) is not None),
                default=None,
            )
            if all(row.get(field) is not None for row in functions)
            else None
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


HOTSPOT_COGNITIVE = 15


def _code_added(
    *,
    before_all: list[dict[str, object]],
    after_all: list[dict[str, object]],
    changed_paths: set[str],
    new_paths: list[str],
    complete: bool,
) -> dict[str, object]:
    """Net source complexity and structure an attempt added, independent of inherited code."""
    method = "all source functions in changed files after the edit minus the same files before; unchanged functions cancel, new functions count in full, deleted ones subtract; tests and Go closure rows inside a declaration (already in its score) excluded"
    limitations = "added and grown functions match on path and name with line numbers removed; nested functions, overloads, renames and Go methods sharing a name on different receivers in one file can misclassify them; net sums do not depend on matching"
    if not complete:
        return {
            "status": "incomplete",
            "complete_for_declared_scope": False,
            "reason": "complexity population incomplete on one side",
            "method": method,
            "limitations": limitations,
        }

    def enclosed(row: dict[str, object], rows: list[dict[str, object]]) -> bool:
        # Go closure rows inside another function are already counted in that function's score.
        if not str(row["name"]).startswith("closure@"):
            return False
        first = _integer(row["line"], "function line")
        return any(
            other is not row
            and other["path"] == row["path"]
            and _integer(other["line"], "function line")
            <= first
            < _integer(other["line"], "function line") + _integer(other["line_count"], "line count")
            and (other["line"], other["line_count"]) != (row["line"], row["line_count"])
            for other in rows
        )

    def source(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            row
            for row in rows
            if str(row["path"]) in changed_paths
            and not _is_test_path(str(row["path"]))
            and not enclosed(row, rows)
        ]

    def identity(row: dict[str, object]) -> tuple[str, str]:
        # Go method names embed their line ("method@47:Name"); moving a method must not rename it.
        return (str(row["path"]), re.sub(r"@\d+(?::\d+)?:?", "@", str(row["name"])))

    def total(rows: list[dict[str, object]], key: str) -> int:
        return sum(_integer(row[key], key) for row in rows)

    before_rows, after_rows = source(before_all), source(after_all)
    previous: dict[tuple[str, str], int] = {}
    for row in before_rows:
        previous[identity(row)] = max(
            previous.get(identity(row), -1), _integer(row["cognitive"], "cognitive")
        )
    analyzed_paths = {str(row["path"]) for row in after_rows}
    return {
        "status": "available",
        "net_cognitive": total(after_rows, "cognitive") - total(before_rows, "cognitive"),
        "net_cyclomatic": total(after_rows, "cyclomatic") - total(before_rows, "cyclomatic"),
        "functions_added": sum(identity(row) not in previous for row in after_rows),
        "source_files_added": sum(
            _is_source_path(path) and path in analyzed_paths for path in new_paths
        ),
        "hotspots_added": sum(
            _integer(row["cognitive"], "cognitive") > HOTSPOT_COGNITIVE
            and _integer(row["cognitive"], "cognitive") > previous.get(identity(row), -1)
            for row in after_rows
        ),
        "hotspot_threshold": HOTSPOT_COGNITIVE,
        "function_count_reported": len(after_rows),
        "complete_for_declared_scope": True,
        "method": method,
        "limitations": limitations,
    }


def _function_changes(
    left: dict[str, object], right: dict[str, object], change: dict[str, object] | None
) -> dict[str, object]:
    unavailable = {"status": "unavailable", "reason": "bound source diff or function rows missing"}
    if change is None or not all(isinstance(row.get("functions"), list) for row in (left, right)):
        return {
            "changed_functions": unavailable,
            "new_file_functions": unavailable,
            "code_added": unavailable,
        }
    ranges = _object(change.get("changed_line_ranges"), "changed-line ranges")
    populations = []
    complete = all(
        row.get("complexity_complete_for_declared_scope") is True for row in (left, right)
    )
    for side, metrics in (("before", left), ("after", right)):
        raw = metrics["functions"]
        assert isinstance(raw, list)
        functions = [_object(row, "function") for row in raw]
        changed = [
            row
            for row in functions
            if _overlaps_change(
                row, _object(ranges.get(str(row["path"]), {}), "file ranges").get(side)
            )
        ]
        populations.append(
            {
                **_function_population(changed),
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
    before_all = left["functions"]
    assert isinstance(before_all, list)
    return {
        "code_added": _code_added(
            before_all=[_object(row, "function") for row in before_all],
            after_all=[_object(row, "function") for row in raw_after],
            changed_paths=set(ranges),
            new_paths=sorted(new_paths),
            complete=complete,
        ),
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
        if any(_object(row, "dead identity").get("declaration_bound") is False for row in items):
            return {
                "status": "unavailable",
                "reason": "Python candidates lack unique source-bound lexical declarations",
                "introduced_count": None,
                "resolved_count": None,
            }
        bases.append(
            [
                {
                    key: value
                    for key, value in _object(row, "dead identity").items()
                    if key not in {"declaration_context", "declaration_context_required"}
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
    for items, group in zip(identities, bases, strict=True):
        assert isinstance(items, list)
        collisions.update(
            json.dumps(base, sort_keys=True)
            for row, base in zip(items, group, strict=True)
            if _object(row, "dead identity").get("declaration_context_required") is True
        )
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
        "method": "category plus declaration path/symbol fields; Python uses AST-bound lexical scopes, structural overload/lambda identities, and declaration roles; colliding identities in either snapshot or Python source additionally use unique bound declaration-line context, excluding coordinates/actions; matching tool/version/config/scope and complete static graph populations required",
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


def _coverage_unit_comparison(
    left: dict[str, object], right: dict[str, object], unit: str
) -> dict[str, object]:
    kind = {
        "line": "lines",
        "statement": "statements",
        "branch": "branches",
        "function": "functions",
    }[unit]
    reasons = []
    for side, metrics in (("before", left), ("after", right)):
        limits = _object(metrics.get("unavailable_populations", {}), "unavailable populations")
        limit = _object(limits.get(kind, {}), "coverage unit limit")
        complete = metrics.get(
            f"{unit}_population_complete", metrics.get("population_complete_for_declared_scope")
        )
        reason = None
        if complete is not True and limit.get("reason"):
            reason = limit["reason"]
        elif metrics.get(f"{unit}_denominator_digest") is None:
            reason = (
                f"{unit} population not supplied by {metrics.get('format') or 'this report format'}"
            )
        elif complete is not True:
            reason = f"{unit} declared source population is partial or completeness is unproven"
        if reason is not None:
            reasons.append(f"{side}: {reason}")
    key = f"{unit}_denominator_digest"
    if not reasons and (not isinstance(left.get(key), str) or left.get(key) != right.get(key)):
        reasons.append("fixed denominator differs or is unbound")
    return {
        "status": "unavailable" if reasons else "available",
        "reason": "; ".join(reasons) if reasons else None,
    }


def _numbit_coordinates(files: Mapping[str, object]) -> set[str]:
    from pluginbench.coverage_report import numbits_to_lines

    return {
        f"{path}:{line}"
        for path, encoded in files.items()
        for line in numbits_to_lines(str(encoded))
    }


def _added_source_lines(change: dict[str, object]) -> set[str]:
    ranges = _object(change.get("changed_line_ranges"), "changed-line ranges")
    lines: set[str] = set()
    for path, sides in ranges.items():
        if not _is_source_path(path):
            continue
        after = _object(sides, "file ranges").get("after") or []
        if not isinstance(after, list):
            raise ValueError("changed-line ranges require a list")
        for item in after:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("changed-line range requires start and count")
            start, count = (_integer(value, "changed-line range") for value in item)
            lines.update(f"{path}:{line}" for line in range(start, start + count))
    return lines


def _changed_line_coverage(
    left: dict[str, object], right: dict[str, object], change: dict[str, object] | None
) -> dict[str, object]:
    """Coverage of the attempt's own added source lines, and added tests that add no unique coverage."""
    coordinates = right.get("coordinates")
    if change is None or not isinstance(coordinates, dict) or "line" not in coordinates:
        return {"status": "unavailable", "reason": "bound source diff or line coordinates missing"}
    added = _added_source_lines(change)
    unmeasured = sorted(
        set(_strings(right.get("unmeasured_source_files", []), "unmeasured source files"))
        & {line.rsplit(":", 1)[0] for line in added}
    )
    if unmeasured:
        # Dropping an untested added file from the report would otherwise raise the rate.
        return {
            "status": "incomplete",
            "reason": "added source files missing from the coverage report: "
            + ", ".join(unmeasured),
        }
    result: dict[str, object] = {
        "status": "available",
        "method": "added source lines from the bound diff intersected with the submission's executable and executed coordinates; branches belong to the added line they start on",
        "limitations": "executing a line does not prove its behavior is asserted; redundant tests are candidates: equal coverage does not establish equal assertions",
    }
    for unit in ("line", "branch"):
        population = _object(coordinates.get(unit, {}), unit)
        if not population:
            result.update({f"{unit}_total": None, f"{unit}_covered": None, f"{unit}_percent": None})
            continue

        def on_added_line(coordinate: str) -> bool:
            path, _, rest = coordinate.partition(":")
            return f"{path}:{rest.split(':')[0]}" in added

        total = {item for item in _strings(population["total"], unit) if on_added_line(item)}
        covered = {item for item in _strings(population["covered"], unit) if item in total}
        result.update(
            {
                f"{unit}_total": len(total),
                f"{unit}_covered": len(covered),
                f"{unit}_percent": _percent(len(covered), len(total)),
            }
        )
    result["added_executable_lines"] = result.pop("line_total")
    result["added_lines_covered"] = result.pop("line_covered")
    before_tests, after_tests = left.get("per_test_numbits"), right.get("per_test_numbits")
    if not isinstance(before_tests, dict) or not isinstance(after_tests, dict):
        result.update(
            added_tests=None,
            redundant_added_tests=None,
            redundant_added_test_names=None,
            added_tests_without_added_lines=None,
            redundancy_reason="per-test line coverage was not recorded for both snapshots",
        )
        return result
    reached = {
        name: _numbit_coordinates(_object(files, f"per_test_numbits.{name}")) & added
        for name, files in after_tests.items()
    }
    new_tests = sorted(set(after_tests) - set(before_tests))
    reaching = [name for name in new_tests if reached[name]]
    # Flag the smallest tests first and drop each flagged test from the pool, so removing every
    # flagged test together keeps added-line coverage unchanged (one of two duplicates stays).
    remaining = dict(reached)
    redundant = []
    for name in sorted(reaching, key=lambda item: (len(reached[item]), item)):
        others = set().union(*(lines for other, lines in remaining.items() if other != name))
        if reached[name] <= others:
            redundant.append(name)
            del remaining[name]
    result.update(
        added_tests=len(new_tests),
        redundant_added_tests=len(redundant),
        redundant_added_test_names=sorted(redundant),
        added_tests_without_added_lines=len(new_tests) - len(reaching),
    )
    return result


def compare_evidence(
    before: dict[str, object], after: dict[str, object], *, change: dict[str, object] | None = None
) -> dict[str, object]:
    """Compare measurements only with compatible collection and denominator provenance."""
    result: dict[str, object] = {}
    if change is not None and (
        change.get("before_digest") != before.get("snapshot_digest")
        or change.get("after_digest") != after.get("snapshot_digest")
    ):
        raise ValueError("changed-line diff does not bind to evidence snapshots")
    for kind in _EVIDENCE_KINDS:
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
        deltas: dict[str, float] = {}
        coverage_units: dict[str, object] = {}
        if kind == "coverage":
            for unit in ("line", "statement", "branch", "function"):
                coverage_units[unit] = _coverage_unit_comparison(left_metrics, right_metrics, unit)
        for key, left_value in left_metrics.items():
            if kind == "coverage":
                unit = key.partition("_")[0]
                if (
                    unit not in coverage_units
                    or _object(coverage_units[unit], unit)["status"] != "available"
                ):
                    continue
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
        if kind == "coverage":
            result[kind] = {
                **_object(result[kind], kind),
                "units": coverage_units,
                "changed_lines": _changed_line_coverage(left_metrics, right_metrics, change),
            }
            if not any(
                _object(unit, "coverage unit")["status"] == "available"
                for unit in coverage_units.values()
            ):
                result[kind] = {
                    **_object(result[kind], kind),
                    "status": "incomplete",
                    "reason": "no compatible complete coverage populations; see per-unit reasons",
                    "deltas": None,
                }
        if kind in {"fallow", "analysis"}:
            comparison = _object(result[kind], "static analysis comparison")
            comparison.update(_function_changes(left_metrics, right_metrics, change))
            comparison["dead_findings"] = _dead_changes(left_metrics, right_metrics)
            if kind == "analysis":
                dead = _object(comparison["dead_findings"], "unused candidate comparison")
                dead["limitations"] = (
                    "candidate changes under the fixed named analyzer/build settings; not proof of semantic deadness or absence; renamed symbols/scopes, changed decorators/overload signatures/lambda expressions, and changed collision contexts can change identity; indistinguishable redefinitions, same-line declarations, and comprehension scopes remain unknown"
                )
                comparison["dead_findings"] = dead
            if kind == "fallow":
                comparison["clone_findings"] = _clone_changes(left_metrics, right_metrics)
            result[kind] = comparison
    return result
