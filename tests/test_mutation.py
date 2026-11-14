"""Mutation bug-catch rate: site finding, sampling, run classification and report binding."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pluginbench import components, harbor_backend, mutation
from pluginbench.assessment_metrics import compare_snapshots, evaluate_evidence
from pluginbench.collectors import Collector, collect_measurements
from pluginbench.mutation import Run, Site

BEFORE = 'def grade(score, bonus):\n    return "C"\n'
# Changed lines 2-4. Line 2 holds relational, boolean and condition sites; line 4 equality and
# condition sites. One mutant per line: the relational one on line 2, the equality one on line 4.
AFTER = (
    "def grade(score, bonus):\n"
    "    if score >= 90 and bonus:\n"
    '        return "A"\n'
    '    return "B" if score == 80 else "C"\n'
)
FIRST, SECOND = "score > 90", "score != 80"


def _sites(source: str) -> list[tuple[int, str, str, str]]:
    return [
        (site.line, site.operator, site.original, site.replacement)
        for site in mutation.python_sites("a.py", source.encode())
    ]


def test_python_sites_cover_each_operator_and_condition_once() -> None:
    source = (
        "if a < b <= c and d or e:\n"
        "    pass\n"
        "elif x is not None:\n"
        "    pass\n"
        "while y is z: pass\n"
        "v = p if q == r else s\n"
        "w = m >= n != o > t\n"
    )
    assert sorted(_sites(source)) == sorted(
        [
            (1, "condition_negation", "a < b <= c and d or e", "not (a < b <= c and d or e)"),
            (1, "boolean_connective", "or", "and"),
            (1, "boolean_connective", "and", "or"),
            (1, "relational_boundary", "<", "<="),
            (1, "relational_boundary", "<=", "<"),
            (3, "condition_negation", "x is not None", "not (x is not None)"),
            (3, "equality_negation", "is not", "is"),
            (5, "condition_negation", "y is z", "not (y is z)"),
            (5, "equality_negation", "is", "is not"),
            (6, "condition_negation", "q == r", "not (q == r)"),
            (6, "equality_negation", "==", "!="),
            (7, "relational_boundary", ">=", ">"),
            (7, "equality_negation", "!=", "=="),
            (7, "relational_boundary", ">", ">="),
        ]
    )


def test_python_sites_skip_other_operators_and_find_tokens_inside_parentheses() -> None:
    source = (
        "assert a in b and c not in d\n"
        "values = [x for x in items if x]\n"
        "ok = (left) < (right)\n"
        "s = 'é'; t = u == v  # é < not an operator\n"
    )
    sites = mutation.python_sites("a.py", source.encode())
    assert [(site.line, site.original) for site in sites] == [(1, "and"), (3, "<"), (4, "==")]
    # Columns and spans are UTF-8 bytes: 'é' is two bytes, so `==` is at byte column 17.
    equality = sites[2]
    assert (equality.column, equality.start - source.encode().index(b"s = '")) == (17, 16)


def test_python_mutants_apply_as_byte_edits_that_still_parse() -> None:
    source = "if (a and\n        b):\n    x = 1 if é == 1 else 2\n"
    data = source.encode()
    for site in mutation.python_sites("a.py", data):
        assert data[site.start : site.end].decode() == site.original
        ast.parse(data[: site.start] + site.replacement.encode() + data[site.end :])


def _site(path: str, line: int, operator: str, column: int = 1) -> Site:
    return Site(path, line, column, operator, 0, 1, "<", "<=")


def test_sample_takes_one_mutant_per_line_round_robin_over_operators() -> None:
    candidates = [
        _site("a.py", line, operator, column)
        for line in range(1, 9)
        for column, operator in enumerate(mutation.OPERATORS, 1)
    ]
    chosen = mutation.sample(candidates, "sha256:digest")
    assert len(chosen) == 8 and len({site.line for site in chosen}) == 8
    # Each round offers the operators in their fixed order; lines run out after two rounds.
    assert [site.operator for site in chosen[:4]] == list(mutation.OPERATORS)
    first = min(
        (site for site in candidates if site.operator == mutation.OPERATORS[0]),
        key=lambda site: mutation.sample_key(site, "sha256:digest"),
    )
    assert chosen[0] == first
    assert mutation.sample(list(reversed(candidates)), "sha256:digest") == chosen
    assert mutation.sample(candidates, "sha256:other") != chosen


def test_sample_stops_at_ten_and_takes_every_line_when_fewer() -> None:
    many = [_site("a.py", line, "relational_boundary") for line in range(1, 30)]
    assert len(mutation.sample(many, "sha256:x")) == mutation.SAMPLE_SIZE == 10
    few = [_site("a.py", 1, "relational_boundary"), _site("a.py", 2, "boolean_connective", 3)]
    assert sorted(site.line for site in mutation.sample(few, "sha256:x")) == [1, 2]


@pytest.mark.parametrize(
    ("stdout", "exit_code", "timed_out", "status"),
    [
        ("tests\nPB-SUITE-EXIT 1\n", 0, False, 1),
        ("no marker\n", 2, False, 2),
        ("PB-SUITE-EXIT 0\nPB-SUITE-EXIT 1\n", 1, False, None),
        ("", None, False, None),
        ("PB-SUITE-EXIT 0\n", None, True, None),
    ],
)
def test_suite_status_reads_the_marker_then_the_exit(
    stdout: str, exit_code: int | None, timed_out: bool, status: int | None
) -> None:
    assert mutation.suite_status(Run(exit_code, timed_out, stdout, 1.0)) == status


def test_tsc_errors_ignore_columns() -> None:
    first = mutation.tsc_errors("src/a.ts(3,5): error TS2322: Type 'x' is wrong.\n")
    shifted = mutation.tsc_errors("src/a.ts(3,9): error TS2322: Type 'x' is wrong.\n")
    assert first == shifted and len(first) == 1
    assert mutation.tsc_errors("error TS5083: Cannot read file.\nFound 1 error.\n") != set()


# --- Collection with a fake executor -----------------------------------------------------


def _tree(path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    return path


def _config(**changes: object) -> dict[str, object]:
    return {
        "language": "python",
        "coverage": {"command": ["suite"], "report_path": "cov.json", "timeout_seconds": 100},
        "native_task_identity": {},
        "generated_files": [],
        "site_finders": {},
        "source_only_reference_paths": [],
        "observer_version": components.VERSIONS["mutation_observer"],
        **changes,
    }


@dataclass(frozen=True)
class View:
    """What a fake suite run sees: the tree's files, its command and earlier identical runs."""

    files: dict[str, str]
    command: list[str]
    mutated: bool
    seen: int

    @property
    def source(self) -> str:
        return self.files["src/grade.py"]


# A run's (status, timed out, extra stdout).
Behaviour = Callable[[View], tuple[int | None, bool, str]]


def _files(tree: Path) -> dict[str, str]:
    return {
        path.relative_to(tree).as_posix(): path.read_text()
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


class Fake:
    """Answers each run from the tree it is given; records the commands and timeouts it saw."""

    def __init__(self, behaviour: Behaviour, before: Path, after: Path) -> None:
        self.behaviour = behaviour
        self.versions = (_files(before), _files(after))
        self.calls: list[tuple[list[str], dict[str, str], int]] = []

    def __call__(self, tree: Path, command: list[str], timeout: int, destination: Path) -> Run:
        files = _files(tree)
        # A source-only tree mixes the two snapshots; only a mutant has text from neither.
        mutated = any(
            all(version.get(path) != text for version in self.versions)
            for path, text in files.items()
        )
        seen = sum(1 for call in self.calls if call[1] == files and call[0] == command)
        self.calls.append((command, files, timeout))
        status, timed_out, extra = self.behaviour(View(files, command, mutated, seen))
        marker = "" if status is None else f"PB-SUITE-EXIT {status}\n"
        return Run(None if timed_out else status, timed_out, extra + marker, 1.0)


def _passing(rule: Callable[[View], int]) -> Behaviour:
    """Unmutated trees pass; `rule` decides each mutant's suite status."""
    return lambda view: ((rule(view) if view.mutated else 0), False, "")


def _measure(
    tmp_path: Path,
    behaviour: Behaviour,
    *,
    after: dict[str, str] | None = None,
    config: dict[str, object] | None = None,
) -> tuple[dict[str, object], Fake, Path]:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE, "tests/test_grade.py": "x\n"})
    files = {"src/grade.py": AFTER, "tests/test_grade.py": "x\n", **(after or {})}
    submitted = _tree(tmp_path / "after", files)
    destination = tmp_path / "evidence"
    destination.mkdir()
    fake = Fake(behaviour, before, submitted)
    report = mutation.collect_mutation(
        submitted, before, config or _config(), destination, execute=fake
    )
    return json.loads(json.dumps(report)), fake, destination


def _metrics(report: dict[str, object], destination: Path) -> dict[str, object]:
    return mutation.mutation_metrics(report, destination)


def _kills_first(view: View) -> int:
    return 1 if FIRST in view.source else 0


def test_kills_are_confirmed_by_one_rerun_and_survivors_run_once(tmp_path: Path) -> None:
    report, fake, destination = _measure(tmp_path, _passing(_kills_first))
    metrics = _metrics(report, destination)
    assert [site["original"] for site in report["selected"]] == [">=", "=="]  # type: ignore[index]
    assert metrics["status"] == "available"
    assert (metrics["killed"], metrics["survived"], metrics["valid"]) == (1, 1, 2)
    assert metrics["bug_catch_percent"] == 50.0
    assert metrics["candidate_count"] == 5 and metrics["candidate_lines"] == [
        "src/grade.py:2",
        "src/grade.py:4",
    ]
    # Two baseline runs, the kill and its confirmation, then the survivor once; mutants get
    # three times the slower baseline plus a minute.
    assert [call[2] for call in fake.calls] == [100, 100, 63, 63, 63]
    # No test file changed, so the share is a measured zero with no source-only runs.
    assert metrics["test_change_kill_share"] == {
        "status": "available",
        "changed_test_files": [],
        "kills_needing_test_changes": 0,
        "percent": 0.0,
    }


def test_a_kill_that_does_not_reproduce_is_flaky_and_excluded(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path, _passing(lambda view: _kills_first(view) if view.seen == 0 else 0)
    )
    metrics = _metrics(report, destination)
    assert (metrics["flaky"], metrics["killed"], metrics["survived"]) == (1, 0, 1)
    assert metrics["bug_catch_percent"] == 0.0


def test_a_timeout_counts_as_a_kill_without_a_rerun(tmp_path: Path) -> None:
    report, fake, destination = _measure(
        tmp_path,
        lambda view: (None, True, "") if view.mutated and FIRST in view.source else (0, False, ""),
    )
    metrics = _metrics(report, destination)
    assert (metrics["killed"], metrics["timed_out"], metrics["survived"]) == (1, 1, 1)
    assert len(fake.calls) == 4


@pytest.mark.parametrize(
    "second",
    [(1, False, ""), (None, True, ""), (None, False, "")],
    ids=["red", "timed-out", "no-status"],
)
def test_a_red_or_unstable_baseline_makes_the_attempt_unavailable(
    tmp_path: Path, second: tuple[int | None, bool, str]
) -> None:
    report, fake, destination = _measure(
        tmp_path, lambda view: second if view.seen == 1 else (0, False, "")
    )
    metrics = _metrics(report, destination)
    assert (metrics["status"], metrics["reason"]) == ("unavailable", "baseline red or unstable")
    assert metrics["bug_catch_percent"] is None and len(fake.calls) == 2


def test_no_candidate_sites_runs_nothing(tmp_path: Path) -> None:
    report, fake, destination = _measure(
        tmp_path, _passing(lambda view: 0), after={"src/grade.py": BEFORE + "x = 1\n"}
    )
    metrics = _metrics(report, destination)
    assert metrics["status"] == "unavailable" and not fake.calls
    assert metrics["reason"] == "no candidate sites on changed production lines"


def test_a_run_without_a_suite_status_leaves_the_attempt_incomplete(tmp_path: Path) -> None:
    # Two markers in one run name no single status.
    report, _, destination = _measure(
        tmp_path,
        lambda view: (0, False, "PB-SUITE-EXIT 1\n" if SECOND in view.source else ""),
    )
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete" and metrics["errors"] == 1
    assert metrics["bug_catch_percent"] is None


def test_generated_and_unparsable_files_give_no_sites(tmp_path: Path) -> None:
    report, fake, destination = _measure(
        tmp_path,
        _passing(lambda view: 0),
        after={"src/broken.py": "def (:\n", "src/table.go": "package x\n"},
        config=_config(generated_files=["src/grade.py"]),
    )
    metrics = _metrics(report, destination)
    assert metrics["candidate_count"] == 0 and not fake.calls
    assert set(_object(metrics["unparsed_files"])) == {"src/broken.py", "src/table.go"}
    assert metrics["status"] == "incomplete" and "src/broken.py" in str(metrics["reason"])


def test_changed_test_files_give_no_sites(tmp_path: Path) -> None:
    test = "if a < b:\n    pass\n"
    assert mutation.python_sites("tests/test_grade.py", test.encode())
    report, fake, destination = _measure(
        tmp_path,
        _passing(lambda view: 0),
        after={"tests/test_grade.py": test, "src/grade.py": BEFORE + "x\n"},
    )
    metrics = _metrics(report, destination)
    assert report["changed_production_lines"] == ["src/grade.py:3"]
    assert metrics["candidate_lines"] == [] and metrics["status"] == "unavailable"
    assert not fake.calls


def test_go_mutants_that_do_not_build_are_invalid_and_not_replaced(tmp_path: Path) -> None:
    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.command == mutation.GO_CHECK:
            return _kills_first(view), False, ""
        return int(view.mutated), False, ""

    report, fake, destination = _measure(tmp_path, behaviour, config=_config(language="go"))
    metrics = _metrics(report, destination)
    assert (metrics["invalid"], metrics["killed"], metrics["valid"]) == (1, 1, 1)
    checks = [call[0] == mutation.GO_CHECK for call in fake.calls]
    assert checks == [False, False, True, True, False, False]


def test_typescript_mutants_are_invalid_only_for_new_tsc_errors(tmp_path: Path) -> None:
    known = "src/grade.py(2,5): error TS2322: Type 'x' is wrong.\n"

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.command != mutation.TSC_CHECK:
            return int(view.mutated), False, ""
        if not view.mutated:
            return 2, False, known
        # The first mutant shifts the known error's column; the second adds an error.
        added = "" if FIRST in view.source else "src/grade.py(4,1): error TS1005: ';' expected.\n"
        return 2, False, known.replace("(2,5)", "(2,9)") + added

    report, _, destination = _measure(tmp_path, behaviour, config=_config(language="typescript"))
    metrics = _metrics(report, destination)
    assert (metrics["killed"], metrics["invalid"]) == (1, 1)


def test_test_change_kill_share_counts_kills_the_source_only_suite_misses(
    tmp_path: Path,
) -> None:
    # Only the agent's changed test catches the first mutant; both suites catch the second.
    def rule(view: View) -> int:
        return int(view.files["tests/test_grade.py"] == "changed\n" or SECOND in view.source)

    report, fake, destination = _measure(
        tmp_path, _passing(rule), after={"tests/test_grade.py": "changed\n"}
    )
    metrics = _metrics(report, destination)
    assert metrics["test_change_kill_share"] == {
        "status": "available",
        "changed_test_files": ["tests/test_grade.py"],
        "kills_needing_test_changes": 1,
        "percent": 50.0,
    }
    # The source-only suite runs the starting test file: its baseline twice, then once for the
    # mutant it misses and twice for the kill it confirms.
    starting = [call for call in fake.calls if call[1]["tests/test_grade.py"] == "x\n"]
    assert len(starting) == 2 + 1 + 2


def test_a_new_test_file_is_left_out_of_the_source_only_suite(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path,
        _passing(lambda view: int("tests/test_new.py" in view.files or SECOND in view.source)),
        after={"tests/test_new.py": "def test(): pass\n"},
    )
    metrics = _metrics(report, destination)
    assert _object(metrics["test_change_kill_share"])["kills_needing_test_changes"] == 1
    labels = sorted({path.name.split("-", 1)[1] for path in (destination / "runs").iterdir()})
    assert labels == [
        "baseline",
        "mutant-0",
        "mutant-0-source-only",
        "mutant-1",
        "mutant-1-source-only",
        "source-only-baseline",
    ]


def test_a_mutant_the_source_only_filter_removes_leaves_the_share_incomplete(
    tmp_path: Path,
) -> None:
    # conftest.py counts as production for sites, but the source-only grade leaves it out.
    report, _, destination = _measure(
        tmp_path,
        _passing(lambda view: 1),
        after={"conftest.py": "ready = level < 3\n", "src/grade.py": BEFORE + "x = a < b\n"},
    )
    metrics = _metrics(report, destination)
    assert metrics["status"] == "available"
    share = _object(metrics["test_change_kill_share"])
    assert share["status"] == "incomplete" and "not placed" in str(share["reason"])


def test_a_red_source_only_baseline_or_no_reference_leaves_the_share_unavailable(
    tmp_path: Path,
) -> None:
    red, _, red_destination = _measure(
        tmp_path / "red",
        lambda view: (int(view.mutated or "tests/test_new.py" not in view.files), False, ""),
        after={"tests/test_new.py": "x\n"},
    )
    metrics = _metrics(red, red_destination)
    assert metrics["status"] == "available"
    assert metrics["test_change_kill_share"] == {
        "status": "unavailable",
        "reason": "source-only baseline red or unstable",
    }
    plain, _, plain_destination = _measure(
        tmp_path / "plain",
        _passing(lambda view: 1),
        config=_config(source_only_reference_paths=None),
    )
    share = _object(_metrics(plain, plain_destination)["test_change_kill_share"])
    assert share["status"] == "unavailable"


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


# --- Binding and tampering ---------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        lambda report, directory: report["selected"].reverse(),
        lambda report, directory: report["mutants"][0]["runs"].pop(),
        lambda report, directory: report["mutants"][1]["runs"][0].update(suite_status=1),
        lambda report, directory: report["candidates"].append(
            {**report["candidates"][0], "line": 1, "start": 0, "end": 1}
        ),
        lambda report, directory: (
            directory / report["baseline"][0]["receipts"] / "stdout.txt"
        ).write_text("PB-SUITE-EXIT 0\n\n"),
    ],
    ids=["sample-order", "missing-rerun", "status", "candidate-off-diff", "saved-output"],
)
def test_tampered_reports_are_refused(
    tmp_path: Path, tamper: Callable[[dict[str, object], Path], object]
) -> None:
    report, _, destination = _measure(tmp_path, _passing(_kills_first))
    tamper(report, destination)
    with pytest.raises(ValueError):
        _metrics(report, destination)


def test_collected_evidence_binds_its_report_and_the_attempt_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": AFTER})

    def native(**kwargs: object) -> dict[str, object]:
        snapshot = kwargs["snapshot"]
        assert isinstance(snapshot, Path)
        failing = (snapshot / "src/grade.py").read_text() != AFTER
        return {"exit_code": int(failing), "timed_out": False, "stdout": "ran\n"}

    monkeypatch.setattr(harbor_backend, "run_native_command", native)
    config = tmp_path / "mutation.json"
    config.write_text(json.dumps(_config()))
    collector = Collector(kind="mutation", config=config, scope=["*"], tool_version="mutation-v1")
    evidence = tmp_path / "evidence"
    paths = collect_measurements(after, evidence, [collector], before=before)
    refs = {"mutation": str(paths["mutation"].resolve())}
    measured = evaluate_evidence(after, refs, evidence)["mutation"]
    assert measured["status"] == "available"  # type: ignore[index]
    change = compare_snapshots(before, after)
    coverage = {
        "metrics": {
            "coordinates": {
                "line": {
                    "total": ["src/grade.py:2", "src/grade.py:4"],
                    "covered": ["src/grade.py:2"],
                }
            }
        }
    }
    compared = mutation.compare_mutation(measured, coverage, change)  # type: ignore[arg-type]
    assert (compared["status"], compared["bug_catch_percent"]) == ("available", 100.0)
    assert compared["execution_split"] == {
        "status": "available",
        "executed": {"killed": 1, "survived": 0},
        "unexecuted": {"killed": 1, "survived": 0},
        "not_in_population": {"killed": 0, "survived": 0},
    }
    other = _tree(tmp_path / "other", {"src/grade.py": BEFORE + "\n"})
    with pytest.raises(ValueError, match="starting source"):
        mutation.compare_mutation(measured, None, compare_snapshots(other, after))  # type: ignore[arg-type]
    report = paths["mutation"].parent / "report.json"
    report.write_text(report.read_text() + "\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        evaluate_evidence(after, refs, evidence)


def test_a_profile_from_another_observer_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(components.ObserverError, match="mutation observer version 0"):
        mutation.collect_mutation(
            tmp_path, tmp_path, _config(observer_version=0), tmp_path, execute=None
        )


# --- Pinned Go and Oxc site finders ------------------------------------------------------


def test_go_sites_from_the_pinned_go_parser(tmp_path: Path) -> None:
    if shutil.which("go") is None:
        pytest.skip("Go is not installed")
    from pluginbench.language_analysis import prepare_go_observer

    binary = prepare_go_observer(
        tmp_path / "cache", mutation.GO_OBSERVER, "pluginbench-go-mutation"
    )
    root = _tree(
        tmp_path / "src",
        {
            "a.go": 'package a\n\nfunc f(a, b int, s string) bool {\n\tif x := a; x < b && s == "é" {\n\t\treturn a >= b || !(a != b)\n\t}\n\tfor a > b {\n\t}\n\treturn false\n}\n',
            "broken.go": "package\n",
        },
    )
    finders = {
        "go": {
            "path": str(binary),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    }
    sites, unparsed = mutation.find_sites(root, ["a.go", "broken.go"], finders)
    assert set(unparsed) == {"broken.go"}
    assert sorted((site.line, site.operator, site.original) for site in sites) == [
        (4, "boolean_connective", "&&"),
        (4, "condition_negation", 'x < b && s == "é"'),
        (4, "equality_negation", "=="),
        (4, "relational_boundary", "<"),
        (5, "boolean_connective", "||"),
        (5, "equality_negation", "!="),
        (5, "relational_boundary", ">="),
        (7, "relational_boundary", ">"),
    ]


def test_javascript_and_typescript_sites_from_the_pinned_oxc_parser(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    from pluginbench.measurement_profiles import prepare_observer

    finders = {"oxc": prepare_observer("oxc-parser-0.126.0", tmp_path / "cache")}
    root = _tree(
        tmp_path / "src",
        {
            "a.ts": 'const s = "é"; if ((a) /* < */ < b && c) { x = y === z ? 1 : 2 }\n'
            "do { k() } while (k !== 1)\nwhile (!done || k >= 3) step();\nconst t = <T,>(v: T) => v;\n",
            "b.js": "const ok = a != b;\n",
            "c.ts": "const = ;\n",
        },
    )
    sites, unparsed = mutation.find_sites(root, ["a.ts", "b.js", "c.ts"], finders)
    assert set(unparsed) == {"c.ts"}
    assert sorted((site.path, site.line, site.operator, site.original) for site in sites) == [
        ("a.ts", 1, "boolean_connective", "&&"),
        ("a.ts", 1, "condition_negation", "(a) /* < */ < b && c"),
        ("a.ts", 1, "condition_negation", "y === z"),
        ("a.ts", 1, "equality_negation", "==="),
        ("a.ts", 1, "relational_boundary", "<"),
        ("a.ts", 2, "equality_negation", "!=="),
        ("a.ts", 3, "boolean_connective", "||"),
        ("a.ts", 3, "condition_negation", "!done || k >= 3"),
        ("a.ts", 3, "relational_boundary", ">="),
        ("b.js", 1, "equality_negation", "!="),
    ]
    condition = next(site for site in sites if site.original == "y === z")
    assert condition.replacement == "!(y === z)"
