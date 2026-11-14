"""Normalize coverage formats into independent, source-bound coordinate populations."""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import base64
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import re

from pluginbench.assessment_metrics import coverage_sources, inspect_snapshot


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
    counts = report.with_name(report.name + ".instrumented.txt")
    if counts.is_file():
        result["lcov_instrumented"] = _lcov_instrumented(
            counts,
            files=set(inventory),
            measured={record.path for record in records},
            source_scope=source_scope,
        )
    per_test = _lcov_per_test(
        report.with_name(report.name + ".per-test.json"),
        records=records,
        inventory=inventory,
        source_scope=source_scope,
        workdir=workdir,
    )
    if per_test:
        result["per_test_numbits"] = per_test
    return result


def _lcov_instrumented(
    path: Path, *, files: set[str], measured: set[str], source_scope: list[str]
) -> dict[str, object]:
    """Name the scoped files the report omits because their instrumenter found nothing in them.

    Jest drops a file from its report when istanbul maps no statement, function or branch to it,
    as for a module of TypeScript types. A file the sidecar gives no count stays expected.
    """
    counts = _declared_counts(path, files, "instrumented counts")
    if declared_empty := sorted(file for file in measured if counts.get(file) == 0):
        raise ValueError(f"instrumented counts are 0 for measured files: {declared_empty}")
    return {
        "instrumented_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "files_without_statements": sorted(
            file
            for file in coverage_sources(files, source_scope) - measured
            if counts.get(file) == 0
        ),
    }


def _lcov_per_test(
    sidecar: Path,
    *,
    records: list[_Record],
    inventory: dict[str, object],
    source_scope: list[str],
    workdir: str,
) -> dict[str, dict[str, str]]:
    """Validate file-to-context numbits against LCOV's executed line population."""
    if not sidecar.exists():
        return {}
    data = _json_object(json.loads(sidecar.read_text(), object_pairs_hook=_json_pairs), "sidecar")
    raw_files = _json_object(data.get("per_test_numbits"), "LCOV per-test numbits")
    measured = {record.path: record for record in records}
    seen: set[str] = set()
    per_test: dict[str, dict[str, str]] = {}
    for raw_path, contexts in raw_files.items():
        path = _source_path(raw_path, set(inventory), source_scope, workdir)
        if path not in measured:
            raise ValueError(f"LCOV per-test coverage names an unmeasured source: {path}")
        if path in seen:
            raise ValueError(f"LCOV per-test source is duplicated: {path}")
        seen.add(path)
        covered = {line for line, count in measured[path].lines.items() if count}
        for name, encoded in _json_object(contexts, "LCOV per-test contexts").items():
            if not isinstance(encoded, str):
                raise ValueError("LCOV per-test numbits must be base64 strings")
            lines = numbits_to_lines(encoded)
            if not lines <= covered:
                raise ValueError("LCOV per-test coverage names an unexecuted or unmeasured line")
            if name:
                per_test.setdefault(name, {})[path] = lines_to_numbits(lines)
    return dict(sorted(per_test.items()))


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
    raw_executed = data.get("executed_lines")
    if isinstance(raw_executed, list):
        # coverage.py reports line 0, the module entry, as executed for a file without statements.
        raw_executed = [line for line in raw_executed if line != 0]
    executed = _python_lines(raw_executed, record)
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
    """Read coverage.py JSON formats 2/3, and 7.2.x's unnumbered format 2.

    Excluded lines are outside the denominator.
    """
    data = _json_object(json.loads(report.read_text(), object_pairs_hook=_json_pairs), "coverage")
    meta = _json_object(data.get("meta"), "Python coverage metadata")
    version = meta.get("version")
    if "format" not in meta and isinstance(version, str) and re.fullmatch(r"7\.2\.[0-9]+", version):
        # coverage.py numbered its JSON in 7.4.1 (commit 576cb3e): "Version 1" had no format
        # number, and format 2 only added meta.format. At 7.2.0 and 7.2.7, jsonreport.py,
        # results.py, numbits.py, analysis2 and contexts_by_lineno match 7.4.1 apart from it,
        # quote style and one import path, so every field read below means the same.
        json_format = 2
    else:
        json_format = _json_count(meta.get("format"))
    if json_format not in {2, 3}:
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
    executed_by_path: dict[str, tuple[set[str], set[str], int]] = {}
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
        file_populations = _python_file(raw_file, record, branch_coverage)
        for kind, (total, covered) in file_populations.items():
            aggregate = populations.setdefault(kind, (set(), set()))
            aggregate[0].update(f"{path}:{coordinate}" for coordinate in total)
            aggregate[1].update(f"{path}:{coordinate}" for coordinate in covered)
        executed_by_path[path] = (*file_populations["lines"], record.line_count)
    result = _metadata("coverage-py-json", records, source_scope, test_scope)
    result["tool_version"] = meta["version"]
    for kind, (total, covered) in populations.items():
        result[kind] = _population(total, covered, [record.path for record in records], kind)
    per_test = _python_per_test(
        data.get("per_test_numbits"), inventory, source_scope, workdir, executed_by_path
    )
    if per_test:  # contexts without test names leave per-test coverage unknown
        result["per_test_numbits"] = per_test
    return result


def lines_to_numbits(lines: Iterable[int]) -> str:
    """coverage.py numbits layout (bit n = line n), base64-encoded."""
    values = sorted(set(lines))
    bits = bytearray(values[-1] // 8 + 1 if values else 0)
    for line in values:
        bits[line // 8] |= 1 << (line % 8)
    return base64.b64encode(bytes(bits)).decode()


def numbits_to_lines(value: str) -> set[int]:
    bits = base64.b64decode(value, validate=True)
    return {
        index * 8 + bit for index, byte in enumerate(bits) for bit in range(8) if byte >> bit & 1
    }


def _python_per_test(
    value: object,
    inventory: dict[str, object],
    source_scope: list[str],
    workdir: str,
    executed_by_path: dict[str, tuple[set[str], set[str], int]],
) -> dict[str, dict[str, str]]:
    """Per-test executed lines from dynamic contexts, kept as compact numbits per file."""
    if value is None:
        return {}
    per_test: dict[str, dict[str, str]] = {}
    for raw_path, contexts in _json_object(value, "Python per-test numbits").items():
        path = _source_path(raw_path, set(inventory), source_scope, workdir)
        if path not in executed_by_path:
            raise ValueError(f"Python per-test coverage names an unmeasured source: {path}")
        total, covered, line_count = executed_by_path[path]
        executed = {int(line) for line in covered}
        missing = {int(line) for line in total} - executed
        for name, encoded in _json_object(contexts, "Python per-test contexts").items():
            if not isinstance(encoded, str):
                raise ValueError("Python per-test numbits must be base64 strings")
            lines = numbits_to_lines(encoded) - {0}  # coverage.py's module-entry line
            # Traced non-statement lines (decorators, docstrings) stay outside the population;
            # a statement the report calls missing, or a line outside the file, is an error.
            if lines & missing or any(not 1 <= line <= line_count for line in lines):
                raise ValueError("Python per-test coverage names an unexecuted line")
            statements = lines & executed
            if name and statements:
                per_test.setdefault(name, {})[path] = lines_to_numbits(statements)
    return dict(sorted(per_test.items()))


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
    return _source_path(_go_module_path(raw_path, modules), files, source_scope, workdir)


def _go_module_path(raw_path: str, modules: dict[str, list[PurePosixPath]]) -> str:
    """Map a coverprofile's `<module>/<path>` name to its repository path."""
    matches = [module for module in modules if raw_path.startswith(module + "/")]
    if not matches:
        raise ValueError(f"Go source cannot be mapped to a frozen module: {raw_path}")
    module = max(matches, key=len)
    directories = modules[module]
    if len(directories) != 1:
        raise ValueError(f"Go source cannot be mapped to one frozen module: {raw_path}")
    return (directories[0] / raw_path[len(module) + 1 :]).as_posix()


# A `//line` comment must start its line; a `/*line` one may sit anywhere. Either names a
# file, then a line and optionally a column. Text that only quotes one also matches, so a
# second file that does makes the target ambiguous and refused.
_GO_LINE_DIRECTIVE = re.compile(
    r"^//line (.+?):[0-9]+(?::[0-9]+)?\r?$|/\*line (.+?):[0-9]+(?::[0-9]+)?\*/", re.M
)


def _go_line_carrier(
    raw_path: str, *, files: set[str], modules: dict[str, list[PurePosixPath]], snapshot: Path
) -> str | None:
    """The Go file whose `//line` directives name the file a coverprofile row names, if any.

    Go's cover tool names a function's blocks `<package path>/<base name>` of the file the
    function starts in once `//line` directives apply, and gives each block the line numbers
    and columns those directives set. When that name is no frozen Go file, the blocks belong
    to the one non-test Go file in the package whose directive names it. A row naming a frozen
    Go file, as cgo's directives make, is not this case.
    """
    if raw_path in files or raw_path.startswith(("/", "./", "../")):
        return None
    try:
        path = PurePosixPath(_go_module_path(raw_path, modules))
    except ValueError:
        return None
    if path.as_posix() in files and path.suffix == ".go":
        return None
    carriers = sorted(
        name
        for name in files
        if PurePosixPath(name).parent == path.parent
        and name.endswith(".go")
        and not name.endswith("_test.go")
        and any(
            PurePosixPath(match.group(1) or match.group(2)).name == path.name
            for match in _GO_LINE_DIRECTIVE.finditer((snapshot / name).read_text())
        )
    )
    if len(carriers) > 1:
        raise ValueError(f"Go //line target {raw_path} is named by more than one file: {carriers}")
    return carriers[0] if carriers else None


def _declared_counts(path: Path, files: set[str], label: str) -> dict[str, int]:
    """Read the count a coverage toolchain declared for each file, one `<count> <path>` line each.

    The count is what the toolchain's instrumenter found to measure in the file, so 0 means the
    toolchain itself found nothing to measure. A file without a line has no declared count.
    """
    counts: dict[str, int] = {}
    # Only "\n" ends a line: a path may hold any other character `splitlines` would split on,
    # and reading bytes keeps text mode from turning "\r" into one.
    lines = path.read_bytes().decode().split("\n")
    if lines[-1] == "":
        lines.pop()  # what follows the last line's newline
    for line in lines:
        match = re.fullmatch(r"(0|[1-9][0-9]*) (\S.*)", line)
        if match is None:
            raise ValueError(f"{label} line is malformed: {line!r}")
        count, file = int(match.group(1)), match.group(2)
        if file not in files:
            raise ValueError(f"{label} file is absent from the frozen snapshot: {file}")
        if file in counts:
            raise ValueError(f"{label} lists a file more than once: {file}")
        counts[file] = count
    return counts


def _go_build(
    listing: Path,
    blocks: Path,
    *,
    files: set[str],
    modules: dict[str, list[PurePosixPath]],
    source_scope: list[str],
    workdir: str,
    measured: set[str],
    carrier_rows: dict[str, int],
) -> dict[str, object]:
    """Split scoped sources by the `go list -deps -test -json` output the coverage command wrote.

    Packages outside the workdir, such as the standard library, and vendored packages are
    skipped. Files that no listed package compiles are recorded with a reason, never as empty
    records. Compiled files the report omits stay expected unless `go tool cover` declared them
    zero blocks.
    """
    text = listing.read_text()
    decoder = json.JSONDecoder(object_pairs_hook=_json_pairs)
    roots = {directory for directories in modules.values() for directory in directories}
    vendors = {root / "vendor" for root in roots}

    def vendored(path: PurePosixPath) -> bool:
        return any(path.is_relative_to(vendor) for vendor in vendors)

    compiled: set[str] = set()
    ignored: dict[str, str] = {}
    listed: dict[str, str] = {}
    test_mains = 0
    index, spaces = 0, re.compile(r"\s*")
    while True:
        # `go list -json` writes a stream of objects, not one JSON document.
        space = spaces.match(text, index)
        assert space is not None
        if (index := space.end()) == len(text):
            break
        value, index = decoder.raw_decode(text, index)
        package = _json_object(value, "go list package")
        directory, name = package.get("Dir"), package.get("ImportPath")
        if not isinstance(name, str):
            raise ValueError("go list package requires ImportPath")
        # `-deps` adds packages outside the repository, and `-e` ones that failed to load
        # without a Dir. Their files are not ours to measure.
        if not isinstance(directory, str) or not PurePosixPath(directory).is_relative_to(
            workdir
        ):
            continue
        relative = PurePosixPath(directory).relative_to(workdir)
        # A module's vendor/ holds its dependencies' code, not the repository's.
        if vendored(relative):
            continue
        fields: dict[str, list[str]] = {}
        for field_name in ("GoFiles", "CgoFiles", "IgnoredGoFiles"):
            items = package.get(field_name, [])
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValueError(f"go list {field_name} must hold file names")
            fields[field_name] = items
        # `-test` adds each tested package's synthesized `<package>.test` main. Its one file is
        # the generated test main in the build cache, by absolute path; a real package lists
        # file names, so a package directory merely named `x.test` is not skipped.
        if (
            name.endswith(".test")
            and " [" not in name
            and fields["GoFiles"]
            and all(PurePosixPath(item).is_absolute() for item in fields["GoFiles"])
        ):
            test_mains += 1
            continue
        # `-test` also lists test variants, `<package> [<tested>.test]`: the package's files
        # again with its `_test.go` files, or a package only tests import. The set drops the
        # repeats and the scope drops `_test.go`; reasons name the package as first listed.
        name = name.split(" [", 1)[0]
        listed.setdefault(relative.as_posix(), name)
        for field_name, items in fields.items():
            for item in items:
                path = (relative / item).as_posix()
                if path not in files:
                    raise ValueError(f"go list file is absent from the frozen snapshot: {path}")
                if field_name == "IgnoredGoFiles":
                    ignored.setdefault(path, f"excluded from package {name} by build constraints")
                else:
                    compiled.add(path)
    # Without `-test`, packages only tests import are missing and their files would read as
    # out of scope. A list from `-test` names a test main for any tested package.
    if not test_mains and any(
        file.endswith("_test.go") and PurePosixPath(file).parent.as_posix() in listed
        for file in files
    ):
        raise ValueError("go list must be run with -deps -test: it lists no test main")
    expected = coverage_sources(files, source_scope)

    def reason(path: str) -> str:
        if vendored(PurePosixPath(path)):
            return "vendored dependency"
        if path in ignored:
            return ignored[path]
        parent = PurePosixPath(path).parent
        if parent.as_posix() in listed:
            return f"not among the files go list compiles for package {listed[parent.as_posix()]}"
        module = next(
            (str(p) for p in (parent, *parent.parents) if p in roots and str(p) != "."), None
        )
        if module is not None and not any(
            directory == module or directory.startswith(module + "/") for directory in listed
        ):
            return f"in nested module {module}, which go list did not list"
        return "in no package that go list -deps -test listed"

    # The block count is the `N` of `Count [N]uint32` in the file `go tool cover` instruments.
    counts = _declared_counts(blocks, files, "go cover blocks") if blocks.is_file() else {}
    if declared_empty := sorted(path for path in measured if counts.get(path) == 0):
        raise ValueError(f"go cover declared no blocks for measured files: {declared_empty}")
    # Rows a //line target names skip the duplicate check, so a profile that repeats a section
    # would count them twice; Go writes at most one row per declared block.
    if overcounted := sorted(p for p, n in carrier_rows.items() if n > counts.get(p, n)):
        raise ValueError(f"Go profile has more blocks than go cover declared for: {overcounted}")
    in_scope = compiled & expected
    return {
        "go_list_sha256": hashlib.sha256(listing.read_bytes()).hexdigest(),
        "go_blocks_sha256": (
            hashlib.sha256(blocks.read_bytes()).hexdigest() if blocks.is_file() else None
        ),
        "compiled_files": sorted(in_scope),
        "not_compiled_files": {path: reason(path) for path in sorted(expected - compiled)},
        "files_without_statements": sorted(
            path for path in in_scope - measured if counts.get(path) == 0
        ),
    }


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
    carriers: dict[str, str | None] = {}
    row_counts: dict[str, int] = {}
    carrier_paths: set[str] = set()  # files a //line target names
    for row in rows[1:]:
        match = re.fullmatch(r"(.+):([0-9]+)\.([0-9]+),([0-9]+)\.([0-9]+) ([0-9]+) ([0-9]+)", row)
        if not match:
            raise ValueError(f"Malformed Go coverprofile block: {row}")
        raw_path, *values = match.groups()
        if raw_path not in carriers:
            carriers[raw_path] = _go_line_carrier(
                raw_path, files=files, modules=modules, snapshot=snapshot
            )
        carrier = carriers[raw_path]
        path = _go_source(
            carrier or raw_path,
            files=files,
            modules=modules,
            source_scope=source_scope,
            workdir=workdir,
        )
        if not path.endswith(".go"):
            raise ValueError(f"Go coverage source is not Go: {path}")
        if path not in records:
            source_lines[path] = (snapshot / path).read_text().splitlines()
            records[path] = _Record(path, len(source_lines[path]))
        lines = source_lines[path]
        record = records[path]
        start, start_col, end, end_col, statements, hits = map(_integer, values)
        if rows[0] == "mode: set" and hits not in {0, 1}:
            raise ValueError("Go set-mode hit count must be zero or one")
        row_counts[path] = row_counts.get(path, 0) + 1
        if carrier is not None:
            carrier_paths.add(path)
            # The block's lines are the //line target's, and two targets' lines can coincide,
            # so it is keyed by the target and numbered by order in the profile when repeated.
            target = PurePosixPath(raw_path).name
            key = f"{path}:{target}:{start}.{start_col},{end}.{end_col}"
            coordinate, repeat = key, 1
            while coordinate in weights:
                repeat += 1
                coordinate = f"{key}#{repeat}"
            weights[coordinate] = statements
            if hits:
                covered.add(coordinate)
            continue
        record.line(str(start))
        record.line(str(end))
        if not (
            1 <= start_col <= len(lines[start - 1].encode()) + 1
            and 1 <= end_col <= len(lines[end - 1].encode()) + 1
            # Only a block without statements, such as an empty `case`, may have no width.
            and (start, start_col) <= (end, end_col)
            and ((start, start_col) != (end, end_col) or statements == 0)
        ):
            raise ValueError(f"Go block escapes source span: {row}")
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
    listing = report.with_name(report.name + ".go-list.json")
    if listing.is_file():
        result["go_build"] = _go_build(
            listing,
            report.with_name(report.name + ".go-blocks.txt"),
            files=files,
            modules=modules,
            source_scope=source_scope,
            workdir=workdir,
            measured=set(records),
            carrier_rows={path: row_counts[path] for path in carrier_paths},
        )
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
