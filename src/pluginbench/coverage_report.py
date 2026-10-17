"""Normalize coverage formats into independent, source-bound coordinate populations."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import json
from pathlib import Path, PurePosixPath
import re

from pluginbench.assessment_metrics import inspect_snapshot


class CoveragePopulationUnavailable(RuntimeError):
    """The report contains no measured sources or usable coordinate populations."""


def _integer(value: str) -> int:
    if not re.fullmatch(r"\d+", value):
        raise ValueError(f"Coverage count must be a nonnegative integer: {value!r}")
    return int(value)


def _source_path(value: str, files: set[str], scope: list[str], workdir: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute():
        try:
            path = path.relative_to(PurePosixPath(workdir))
        except ValueError as exc:
            raise ValueError(f"Coverage source escapes the measured repository: {value}") from exc
    if ".." in path.parts or path.as_posix() not in files:
        raise ValueError(f"Coverage source is absent from the frozen snapshot: {value}")
    relative = path.as_posix()
    if not any(fnmatch.fnmatchcase(relative, pattern) for pattern in scope):
        raise ValueError(f"Coverage source escapes declared scope: {relative}")
    return relative


@dataclass
class _Record:
    path: str
    line_count: int
    definitions: dict[str, list[int]] = field(default_factory=dict)
    hits: dict[str, list[int]] = field(default_factory=dict)
    branches: dict[str, int] = field(default_factory=dict)
    summaries: dict[str, int] = field(default_factory=dict)
    lines: dict[int, int] = field(default_factory=dict)

    def line(self, value: str) -> int:
        line = _integer(value)
        if not 1 <= line <= self.line_count:
            raise ValueError(f"LCOV coordinate escapes source line population: {self.path}:{line}")
        return line

    def function(self, key: str, value: str) -> None:
        fields = value.split(",", 1)
        target = self.definitions if key == "FN" else self.hits
        if len(fields) != 2 or not fields[1]:
            raise ValueError(f"Invalid or duplicate LCOV function: {value}")
        coordinate = self.line(fields[0]) if key == "FN" else _integer(fields[0])
        values = target.setdefault(fields[1], [])
        if key == "FN" and coordinate in values:
            raise ValueError(f"Duplicate LCOV function coordinate: {value}")
        values.append(coordinate)

    def function_coverage(self) -> dict[str, int] | None:
        result = {}
        for name, lines in self.definitions.items():
            covered = {count > 0 for count in self.hits[name]}
            # FNDA carries no source line: mixed hits cannot be paired safely.
            if len(covered) > 1:
                return None
            result.update({f"{line}:{name}": int(True in covered) for line in lines})
        return result

    def branch(self, value: str) -> None:
        fields = value.split(",")
        if len(fields) != 4:
            raise ValueError(f"Invalid LCOV branch: {value}")
        coordinate = ":".join(
            [str(self.line(fields[0])), str(_integer(fields[1])), str(_integer(fields[2]))]
        )
        if coordinate in self.branches:
            raise ValueError(f"Duplicate LCOV branch: {value}")
        self.branches[coordinate] = 0 if fields[3] == "-" else _integer(fields[3])

    def line_hits(self, value: str) -> None:
        fields = value.split(",")
        if len(fields) not in {2, 3}:
            raise ValueError(f"Invalid LCOV line: {value}")
        line = self.line(fields[0])
        if line in self.lines:
            raise ValueError(f"Duplicate LCOV line: {value}")
        self.lines[line] = _integer(fields[1])

    def add(self, key: str, value: str) -> None:
        if key in {"FN", "FNDA"}:
            self.function(key, value)
        elif key == "BRDA":
            self.branch(value)
        elif key in {"FNF", "FNH", "BRF", "BRH", "LF", "LH"}:
            if key in self.summaries:
                raise ValueError(f"Duplicate LCOV summary: {key}")
            self.summaries[key] = _integer(value)
        elif key == "DA":
            self.line_hits(value)
        elif key != "VER":
            raise ValueError(f"Unsupported LCOV record: {key}")

    def validate(self) -> None:
        if self.definitions.keys() != self.hits.keys() or any(
            len(lines) != len(self.hits[name]) for name, lines in self.definitions.items()
        ):
            raise ValueError(f"LCOV function definitions and hit counts differ: {self.path}")
        observed = {
            "FNF": sum(len(lines) for lines in self.definitions.values()),
            "FNH": sum(count > 0 for counts in self.hits.values() for count in counts),
            "BRF": len(self.branches),
            "BRH": sum(count > 0 for count in self.branches.values()),
            "LF": len(self.lines),
            "LH": sum(count > 0 for count in self.lines.values()),
        }
        if any(value != observed[key] for key, value in self.summaries.items()):
            raise ValueError(f"LCOV summaries disagree with coordinate populations: {self.path}")


def _record(
    rows: list[str], snapshot: Path, files: set[str], scope: list[str], workdir: str
) -> _Record:
    if not rows or not rows[0].startswith("SF:"):
        raise ValueError("LCOV record must begin with a source path")
    path = _source_path(rows[0][3:], files, scope, workdir)
    result = _Record(path, len((snapshot / path).read_text().splitlines()))
    for raw in rows[1:]:
        key, separator, value = raw.partition(":")
        if not separator:
            raise ValueError(f"Malformed LCOV record: {raw}")
        result.add(key, value)
    result.validate()
    return result


def _records(
    report: Path, snapshot: Path, files: set[str], scope: list[str], workdir: str
) -> list[_Record]:
    records = []
    seen: set[str] = set()
    rows: list[str] = []
    for raw in report.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("TN:"):
            continue
        if raw != "end_of_record":
            rows.append(raw)
            continue
        record = _record(rows, snapshot, files, scope, workdir)
        if record.path in seen:
            raise ValueError(f"Coverage source is duplicated: {record.path}")
        seen.add(record.path)
        records.append(record)
        rows = []
    if rows:
        raise ValueError("Coverage source record was not terminated")
    if not records:
        raise CoveragePopulationUnavailable("LCOV report contains no measured sources")
    return records


def parse_lcov(
    report: Path,
    *,
    snapshot: Path,
    source_scope: list[str],
    test_scope: list[str],
    workdir: str,
) -> dict[str, object]:
    """Keep available populations independently; missing kinds stay unavailable."""
    inventory = inspect_snapshot(snapshot)["files"]
    assert isinstance(inventory, dict)
    records = _records(report, snapshot, set(inventory), source_scope, workdir)
    result = _metadata("lcov", records, source_scope, test_scope)
    ambiguous = sorted(record.path for record in records if record.function_coverage() is None)
    if ambiguous:
        result["unavailable_populations"] = {
            "functions": {
                "files": ambiguous,
                "reason": "Repeated LCOV function names have mixed hits; FNDA has no source line to identify which function was covered",
            }
        }
    for kind, summaries in (
        ("functions", {"FNF", "FNH"}),
        ("branches", {"BRF", "BRH"}),
        ("lines", {"LF", "LH"}),
    ):
        measured = [
            record
            for record in records
            if (summaries <= record.summaries.keys() or (kind == "lines" and record.lines))
            and not (kind == "functions" and record.path in ambiguous)
        ]
        if not measured:
            continue
        total: set[str] = set()
        covered: set[str] = set()
        for record in measured:
            if kind == "functions":
                hits = record.function_coverage()
                assert hits is not None
            else:
                hits = (
                    record.branches
                    if kind == "branches"
                    else {str(line): count for line, count in record.lines.items()}
                )
            total.update(f"{record.path}:{coordinate}" for coordinate in hits)
            covered.update(
                f"{record.path}:{coordinate}" for coordinate, count in hits.items() if count
            )
        result[kind] = _population(total, covered, [record.path for record in measured], kind)
    if not any(kind in result for kind in ("lines", "branches", "functions")):
        raise CoveragePopulationUnavailable("LCOV contains no explicit coverage populations")
    return result


def _population(
    total: set[str], covered: set[str], files: list[str], kind: str
) -> dict[str, object]:
    return {
        "total": sorted(total),
        "covered": sorted(covered),
        "files_measured": sorted(files),
        "unit": {
            "lines": "line",
            "branches": "branch",
            "functions": "function",
            "statements": "statement",
        }[kind],
    }


def _metadata(
    format: str, records: list[_Record], source_scope: list[str], test_scope: list[str]
) -> dict[str, object]:
    return {
        "files_measured": sorted(record.path for record in records),
        "source_scope": source_scope,
        "test_scope": test_scope,
        "format": format,
    }


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate coverage JSON key: {key}")
        result[key] = value
    return result


def _json_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Coverage count must be a nonnegative integer: {value!r}")
    return value


def _python_lines(value: object, record: _Record) -> set[int]:
    if not isinstance(value, list):
        raise ValueError("Python coverage lines must be a list")
    lines = [record.line(str(_json_count(line))) for line in value]
    if len(lines) != len(set(lines)):
        raise ValueError("Duplicate Python coverage line")
    return set(lines)


def _python_branches(value: object, record: _Record) -> set[str]:
    if not isinstance(value, list):
        raise ValueError("Python coverage branches must be a list")
    branches: set[str] = set()
    for arc in value:
        if not isinstance(arc, list) or len(arc) != 2:
            raise ValueError("Python coverage branch must contain two line coordinates")
        source = record.line(str(_json_count(arc[0])))
        destination = arc[1]
        if isinstance(destination, bool) or not isinstance(destination, int) or destination == 0:
            raise ValueError("Invalid Python coverage branch destination")
        record.line(str(abs(destination)))
        coordinate = f"{source}:{destination}"
        if coordinate in branches:
            raise ValueError("Duplicate Python coverage branch")
        branches.add(coordinate)
    return branches


def _python_file(
    value: object, record: _Record, branch_coverage: bool
) -> dict[str, tuple[set[str], set[str]]]:
    data = _json_object(value, "Python coverage file")
    summary = _json_object(data.get("summary"), "Python coverage summary")
    executed = _python_lines(data.get("executed_lines"), record)
    missing = _python_lines(data.get("missing_lines"), record)
    excluded = _python_lines(data.get("excluded_lines"), record)
    if "statement_lines" in data:
        statements = _python_lines(data["statement_lines"], record)
        if missing != statements - executed or executed & missing:
            raise ValueError(
                "Python statement coordinates do not partition into covered and missing"
            )
        executed &= statements
    if executed & missing or (executed | missing) & excluded:
        raise ValueError("Python coverage line populations overlap")
    expected = {
        "num_statements": len(executed | missing),
        "covered_lines": len(executed),
        "missing_lines": len(missing),
        "excluded_lines": len(excluded),
    }
    result = {
        "lines": ({str(line) for line in executed | missing}, {str(line) for line in executed})
    }
    if branch_coverage:
        covered = _python_branches(data.get("executed_branches"), record)
        uncovered = _python_branches(data.get("missing_branches"), record)
        if covered & uncovered:
            raise ValueError("Python coverage branch populations overlap")
        result["branches"] = (covered | uncovered, covered)
        expected.update(
            num_branches=len(covered | uncovered),
            covered_branches=len(covered),
            missing_branches=len(uncovered),
        )
    elif "executed_branches" in data or "missing_branches" in data:
        raise ValueError("Python branch population contradicts report metadata")
    if any(_json_count(summary.get(key)) != count for key, count in expected.items()):
        raise ValueError("Python coverage summaries disagree with coordinate populations")
    return result


def parse_python_coverage(
    report: Path, *, snapshot: Path, source_scope: list[str], test_scope: list[str], workdir: str
) -> dict[str, object]:
    """Read coverage.py JSON formats 2/3; excluded lines are outside the denominator."""
    data = _json_object(json.loads(report.read_text(), object_pairs_hook=_json_pairs), "coverage")
    meta = _json_object(data.get("meta"), "Python coverage metadata")
    if _json_count(meta.get("format")) not in {2, 3}:
        raise ValueError("Unsupported coverage.py JSON format")
    if not isinstance(meta.get("branch_coverage"), bool) or not isinstance(
        meta.get("version"), str
    ):
        raise ValueError("Python coverage metadata lacks branch mode or tool version")
    branch_coverage = meta["branch_coverage"]
    assert isinstance(branch_coverage, bool)
    statement_source = meta.get("statement_lines_source")
    if statement_source not in (None, "coverage.Coverage.analysis2"):
        raise ValueError("Unsupported Python statement-coordinate source")
    inventory = inspect_snapshot(snapshot)["files"]
    assert isinstance(inventory, dict)
    raw_files = _json_object(data.get("files"), "Python coverage files")
    if not raw_files:
        raise CoveragePopulationUnavailable("Python report contains no measured sources")
    records: list[_Record] = []
    populations: dict[str, tuple[set[str], set[str]]] = {}
    for raw_path, raw_file in raw_files.items():
        if ("statement_lines" in _json_object(raw_file, "Python coverage file")) != (
            statement_source is not None
        ):
            raise ValueError("Python statement-coordinate source differs from file metadata")
        path = _source_path(raw_path, set(inventory), source_scope, workdir)
        if not path.endswith(".py"):
            raise ValueError(f"Python coverage source is not Python: {path}")
        if path in {record.path for record in records}:
            raise ValueError(f"Python coverage source is duplicated: {path}")
        record = _Record(path, len((snapshot / path).read_text().splitlines()))
        records.append(record)
        for kind, (total, covered) in _python_file(raw_file, record, branch_coverage).items():
            aggregate = populations.setdefault(kind, (set(), set()))
            aggregate[0].update(f"{path}:{coordinate}" for coordinate in total)
            aggregate[1].update(f"{path}:{coordinate}" for coordinate in covered)
    result = _metadata("coverage-py-json", records, source_scope, test_scope)
    result["tool_version"] = meta["version"]
    for kind, (total, covered) in populations.items():
        result[kind] = _population(total, covered, [record.path for record in records], kind)
    return result


def _go_modules(snapshot: Path, files: set[str]) -> dict[str, list[PurePosixPath]]:
    modules: dict[str, list[PurePosixPath]] = {}
    for name in files:
        if PurePosixPath(name).name != "go.mod":
            continue
        declarations = re.findall(
            r'^\s*module\s+(?:"([^"\n]+)"|([^\s]+))\s*(?://[^\n]*)?$',
            (snapshot / name).read_text(),
            re.M,
        )
        if len(declarations) != 1:
            raise ValueError(f"Cannot establish frozen Go module identity: {name}")
        module = declarations[0][0] or declarations[0][1]
        modules.setdefault(module, []).append(PurePosixPath(name).parent)
    return modules


def _go_source(
    raw_path: str,
    *,
    files: set[str],
    modules: dict[str, list[PurePosixPath]],
    source_scope: list[str],
    workdir: str,
) -> str:
    if raw_path in files or raw_path.startswith(("/", "./", "../")):
        return _source_path(raw_path, files, source_scope, workdir)
    matches = [module for module in modules if raw_path.startswith(module + "/")]
    if not matches:
        raise ValueError(f"Go source cannot be mapped to a frozen module: {raw_path}")
    module = max(matches, key=len)
    directories = modules[module]
    if len(directories) != 1:
        raise ValueError(f"Go source cannot be mapped to one frozen module: {raw_path}")
    path = (directories[0] / raw_path[len(module) + 1 :]).as_posix()
    return _source_path(path, files, source_scope, workdir)


def parse_go_coverage(
    report: Path, *, snapshot: Path, source_scope: list[str], test_scope: list[str], workdir: str
) -> dict[str, object]:
    """Keep basic-block coordinates and numStmt weights; Go measures statements."""
    rows = report.read_text().splitlines()
    if not rows or rows[0] not in {"mode: set", "mode: count", "mode: atomic"}:
        raise ValueError("Invalid Go coverprofile mode")
    inventory = inspect_snapshot(snapshot)["files"]
    assert isinstance(inventory, dict)
    files = set(inventory)
    modules = _go_modules(snapshot, files)
    source_lines: dict[str, list[str]] = {}
    records: dict[str, _Record] = {}
    weights: dict[str, int] = {}
    covered: set[str] = set()
    for row in rows[1:]:
        match = re.fullmatch(r"(.+):([0-9]+)\.([0-9]+),([0-9]+)\.([0-9]+) ([0-9]+) ([0-9]+)", row)
        if not match:
            raise ValueError(f"Malformed Go coverprofile block: {row}")
        raw_path, *values = match.groups()
        path = _go_source(
            raw_path, files=files, modules=modules, source_scope=source_scope, workdir=workdir
        )
        if not path.endswith(".go"):
            raise ValueError(f"Go coverage source is not Go: {path}")
        if path not in records:
            source_lines[path] = (snapshot / path).read_text().splitlines()
            records[path] = _Record(path, len(source_lines[path]))
        lines = source_lines[path]
        record = records[path]
        start, start_col, end, end_col, statements, hits = map(_integer, values)
        record.line(str(start))
        record.line(str(end))
        if not (
            1 <= start_col <= len(lines[start - 1].encode()) + 1
            and 1 <= end_col <= len(lines[end - 1].encode()) + 1
            and (start, start_col) < (end, end_col)
        ):
            raise ValueError(f"Go block escapes source span: {row}")
        if rows[0] == "mode: set" and hits not in {0, 1}:
            raise ValueError("Go set-mode hit count must be zero or one")
        coordinate = f"{path}:{start}.{start_col},{end}.{end_col}"
        if coordinate in weights:
            raise ValueError(f"Duplicate Go coverage block: {coordinate}")
        weights[coordinate] = statements
        if hits:
            covered.add(coordinate)
    if not records:
        raise CoveragePopulationUnavailable("Go report contains no measured source blocks")
    result = _metadata("go-coverprofile", list(records.values()), source_scope, test_scope)
    result["mode"] = rows[0][6:]
    population = _population(set(weights), covered, list(records), "statements")
    population["weights"] = weights
    result["statements"] = population
    return result


def parse_coverage(
    format: str,
    report: Path,
    *,
    snapshot: Path,
    source_scope: list[str],
    test_scope: list[str],
    workdir: str,
) -> dict[str, object]:
    parsers = {
        "lcov": parse_lcov,
        "coverage-py-json": parse_python_coverage,
        "go-coverprofile": parse_go_coverage,
    }
    if format not in parsers:
        raise ValueError(f"Unsupported coverage format: {format}")
    return parsers[format](
        report, snapshot=snapshot, source_scope=source_scope, test_scope=test_scope, workdir=workdir
    )
