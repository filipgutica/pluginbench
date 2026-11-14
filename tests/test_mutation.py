"""Mutation bug-catch rate: site finding, sampling, run classification and report binding."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pluginbench import components, harbor_backend, isolation, mutation
from pluginbench.assessment_metrics import (
    compare_evidence,
    compare_snapshots,
    evaluate_evidence,
    inspect_snapshot,
)
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


@pytest.mark.parametrize(
    "source",
    ["ok = (x is  # a note\n      not y)\n", "ok = x is \\\n    not y\n", "ok = x is  not y\n"],
    ids=["comment", "continuation", "spaces"],
)
def test_python_is_not_with_text_between_its_tokens_is_one_site(source: str) -> None:
    data = source.encode()
    [condition] = [
        site for site in mutation.python_sites("a.py", data) if site.operator == "equality_negation"
    ]
    mutated = data[: condition.start] + condition.replacement.encode() + data[condition.end :]
    # Only `not` goes: a comment stays a comment, and no line moves.
    assert mutated.count(b"\n") == data.count(b"\n")
    compare = ast.parse(mutated).body[0].value  # type: ignore[attr-defined]
    while not isinstance(compare, ast.Compare):
        compare = compare.value
    assert [type(op) for op in compare.ops] == [ast.Is]


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
        # Every task but csstree must print its marker: a bare process exit is no status.
        ("no marker\n", 2, False, None),
        ("PB-SUITE-EXIT 0\nPB-SUITE-EXIT 1\n", 1, False, None),
        # A marker from a run with no recorded exit is no status either.
        ("PB-SUITE-EXIT 0\n", None, False, None),
        ("", None, False, None),
        ("PB-SUITE-EXIT 0\n", None, True, None),
    ],
)
def test_suite_status_needs_exactly_one_marker_and_a_recorded_exit(
    stdout: str, exit_code: int | None, timed_out: bool, status: int | None
) -> None:
    assert mutation.suite_status(Run(exit_code, timed_out, stdout, 1.0)) == status


def test_csstree_takes_its_suite_status_from_the_process_exit() -> None:
    assert "https://github.com/csstree/csstree" in mutation.PROCESS_EXIT_REPOSITORIES
    assert mutation.suite_status(Run(2, False, "no marker\n", 1.0), process_exit=True) == 2
    assert mutation.suite_status(Run(None, False, "", 1.0), process_exit=True) is None


TSC_SITE = Site("src/a.ts", 3, 9, "relational_boundary", 0, 1, "<", "<=")


def test_tsc_errors_drop_columns_only_on_the_mutated_line_and_keep_counts() -> None:
    error = "src/a.ts({},{}): error TS2322: Type 'x' is wrong.\n"
    known = mutation.tsc_errors(error.format(3, 5) + error.format(7, 2), TSC_SITE)
    # A column shift on the mutated line is the same error.
    assert mutation.tsc_errors(error.format(3, 9) + error.format(7, 2), TSC_SITE) == known
    # Elsewhere, and as a second copy on the mutated line, the same text is a new error.
    for added in (error.format(7, 8), error.format(3, 1)):
        mutated = mutation.tsc_errors(error.format(3, 9) + error.format(7, 2) + added, TSC_SITE)
        assert mutated - known
    assert mutation.tsc_errors("error TS5083: Cannot read file.\nFound 1 error.\n")


# --- Collection with a fake executor -----------------------------------------------------


def _tree(path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    return path


IMAGE = {"image_identity": {"image_id": "sha256:image"}}


def _config(**changes: object) -> dict[str, object]:
    return {
        "language": "python",
        "coverage": {"command": ["suite"], "report_path": "cov.json", "timeout_seconds": 100},
        "native_task_identity": IMAGE,
        "generated_files": [],
        "suite_status": "marker",
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
        return "".join(text for path, text in self.files.items() if path.startswith("src/"))


# A run's (status, timed out, extra stdout).
Behaviour = Callable[[View], tuple[int | None, bool, str]]


def _receipt(destination: Path, tree: Path, command: list[str], run: Run) -> None:
    """The launcher's execution.json for one run, as `run_native_command` writes it."""
    (destination / "execution.json").write_text(
        json.dumps(
            {
                "command": command,
                "image_id": IMAGE["image_identity"]["image_id"],
                "before_digest": inspect_snapshot(tree)["digest"],
                "exit_code": run.exit_code,
                "timed_out": run.timed_out,
                "stdout": run.stdout,
            }
        )
    )


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
        try:
            status, timed_out, extra = self.behaviour(View(files, command, mutated, seen))
        except mutation.InfrastructureFailure as error:
            # The launcher writes its receipt, with the error, whatever happens.
            (destination / "execution.json").write_text(json.dumps({"error": str(error)}))
            raise
        marker = "" if status is None else f"PB-SUITE-EXIT {status}\n"
        run = Run(None if timed_out else status, timed_out, extra + marker, 1.0)
        _receipt(destination, tree, command, run)
        return run


def _passing(rule: Callable[[View], int]) -> Behaviour:
    """Unmutated trees pass; `rule` decides each mutant's suite status."""
    return lambda view: ((rule(view) if view.mutated else 0), False, "")


def _measure(
    tmp_path: Path,
    behaviour: Behaviour,
    *,
    after: dict[str, str] | None = None,
    config: dict[str, object] | None = None,
    name: str = "src/grade.py",
) -> tuple[dict[str, object], Fake, Path]:
    before = _tree(tmp_path / "before", {name: BEFORE, "tests/test_grade.py": "x\n"})
    files = {name: AFTER, "tests/test_grade.py": "x\n", **(after or {})}
    submitted = _tree(tmp_path / "after", files)
    destination = tmp_path / "evidence"
    destination.mkdir()
    fake = Fake(behaviour, before, submitted)
    report = mutation.collect_mutation(
        submitted, before, config or _config(), destination, execute=fake
    )
    return json.loads(json.dumps(report)), fake, destination


def _metrics(
    report: dict[str, object], destination: Path, config: dict[str, object] | None = None
) -> dict[str, object]:
    return mutation.mutation_metrics(
        report, destination, config=config or _config(), snapshot=destination.parent / "after"
    )


def _kills_first(view: View) -> int:
    return 1 if FIRST in view.source else 0


def test_kills_are_confirmed_by_one_rerun_and_survivors_run_once(tmp_path: Path) -> None:
    report, fake, destination = _measure(tmp_path, _passing(_kills_first))
    metrics = _metrics(report, destination)
    assert [_object(site)["original"] for site in _list(report["selected"])] == [">=", "=="]
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


def test_a_kill_whose_confirmation_times_out_is_reported_timed_out(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path,
        lambda view: (
            (None, True, "")
            if view.mutated and FIRST in view.source and view.seen
            else ((_kills_first(view) if view.mutated else 0), False, "")
        ),
    )
    metrics = _metrics(report, destination)
    assert (metrics["killed"], metrics["timed_out"], metrics["survived"]) == (1, 1, 1)
    assert _object(_list(metrics["mutants"])[0])["outcome"] == "timeout"


def test_the_share_is_a_measured_zero_without_test_changes_whatever_the_headline(
    tmp_path: Path,
) -> None:
    zero = {
        "status": "available",
        "changed_test_files": [],
        "kills_needing_test_changes": 0,
        "percent": 0.0,
    }
    none, _, none_destination = _measure(
        tmp_path / "none", _passing(lambda view: 0), after={"src/grade.py": BEFORE + "x = 1\n"}
    )
    red, _, red_destination = _measure(tmp_path / "red", lambda view: (1, False, ""))
    for report, destination, headline in (
        (none, none_destination, "no candidate sites"),
        (red, red_destination, "baseline red"),
    ):
        metrics = _metrics(report, destination)
        assert metrics["status"] == "unavailable" and headline in str(metrics["reason"])
        assert metrics["test_change_kill_share"] == zero


def test_an_unsupported_language_file_holds_no_sites_and_is_counted(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path, _passing(_kills_first), after={"src/lib.rs": "fn f(a: i32) -> bool { a < 1 }\n"}
    )
    metrics = _metrics(report, destination)
    assert (metrics["unsupported_files"], metrics["unsupported_file_count"]) == (["src/lib.rs"], 1)
    assert metrics["status"] == "available" and metrics["unparsed_files"] == {}


def test_an_infrastructure_failure_leaves_the_attempt_incomplete(tmp_path: Path) -> None:
    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.mutated and SECOND in view.source:
            raise mutation.InfrastructureFailure("docker exec failed: daemon gone")
        return (_kills_first(view) if view.mutated else 0), False, ""

    report, _, destination = _measure(tmp_path, behaviour)
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete"
    assert metrics["reason"] == "infrastructure failure: docker exec failed: daemon gone"
    assert metrics["bug_catch_percent"] is None
    assert _object(metrics["test_change_kill_share"])["percent"] == 0.0
    # Its record must match the failing run's receipt.
    _object(report["infrastructure_failure"])["reason"] = "docker exec failed: other"
    with pytest.raises(ValueError, match="infrastructure failure differs"):
        _metrics(report, destination)


def test_an_infrastructure_failure_in_the_baseline_is_not_a_red_baseline(tmp_path: Path) -> None:
    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.seen:
            raise mutation.InfrastructureFailure("docker create failed: no space")
        return 0, False, ""

    report, _, destination = _measure(tmp_path, behaviour)
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete" and "no space" in str(metrics["reason"])


def test_an_integrity_violation_fails_the_collection(tmp_path: Path) -> None:
    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.mutated:
            raise ValueError("Frozen measurement input changed during execution")
        return 0, False, ""

    with pytest.raises(ValueError, match="input changed"):
        _measure(tmp_path, behaviour)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ValueError("docker exec failed: daemon gone"), mutation.InfrastructureFailure),
        (subprocess.TimeoutExpired(["docker", "cp"], 300), mutation.InfrastructureFailure),
        (ValueError("Frozen measurement input changed during execution"), ValueError),
        (ValueError("Native measurement introduced production or test source: a.py"), ValueError),
    ],
    ids=["docker-command", "docker-timeout", "input-changed", "source-introduced"],
)
def test_the_native_executor_tells_infrastructure_failures_from_integrity_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, kind: type[Exception]
) -> None:
    def native(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        destination.mkdir(parents=True)
        (destination / "execution.json").write_text(json.dumps({"error": str(error)}))
        raise error

    monkeypatch.setattr(harbor_backend, "run_native_command", native)
    execute = mutation.native_executor({}, "cov.json")
    with pytest.raises(Exception) as raised:
        execute(tmp_path, ["suite"], 10, tmp_path / "run")
    assert type(raised.value) is kind


# `assert_runtime`'s one message for every failure to read the current Docker runtime.
_UNVERIFIED = "Cannot verify the frozen Harbor and Docker runtime"


def _drift(message: str, cause: BaseException | None) -> isolation.RuntimeDriftError:
    error = isolation.RuntimeDriftError(message)
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_drift(_UNVERIFIED, ValueError("docker info failed: daemon gone")), "infrastructure"),
        (_drift(_UNVERIFIED, subprocess.TimeoutExpired(["docker", "info"], 30)), "infrastructure"),
        (_drift(_UNVERIFIED, FileNotFoundError("docker")), "infrastructure"),
        # Docker answered, and what it reported is refused: these stay integrity failures.
        (_drift(_UNVERIFIED, ValueError("Harbor 0.1 is required; installed is 0.2")), "drift"),
        (_drift(_UNVERIFIED, ValueError("Docker did not report a complete daemon identity")), "drift"),
        (_drift(_UNVERIFIED, ValueError("Harbor execution currently requires Linux containers")), "drift"),
        (_drift("Harbor or Docker runtime changed since this run was prepared: x", None), "drift"),
        (_drift("Docker capacity no longer fits this run: 1 jobs", None), "drift"),
        (_drift("Cannot verify frozen Docker CLI", FileNotFoundError("docker")), "drift"),
        (_drift(_UNVERIFIED + ".", ValueError("docker info failed: daemon gone")), "drift"),
    ],
    ids=[
        "docker-info-failed",
        "docker-info-timeout",
        "docker-missing",
        "harbor-version",
        "daemon-identity",
        "platform",
        "runtime-changed",
        "capacity",
        "frozen-cli",
        "other-message",
    ],
)
def test_docker_lost_before_launch_leaves_the_attempt_incomplete_and_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: isolation.RuntimeDriftError,
    kind: str,
) -> None:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": AFTER})

    def native(**kwargs: object) -> dict[str, object]:
        snapshot, destination, command = (
            kwargs["snapshot"],
            kwargs["destination"],
            kwargs["command"],
        )
        assert isinstance(snapshot, Path) and isinstance(destination, Path)
        assert isinstance(command, list)
        # The launcher's runtime check fails on the first mutant, before it writes a receipt.
        if (snapshot / "src/grade.py").read_text() != AFTER:
            raise error
        _receipt(destination, snapshot, command, Run(0, False, "PB-SUITE-EXIT 0\n", 1))
        return dict(json.loads((destination / "execution.json").read_text()))

    monkeypatch.setattr(harbor_backend, "run_native_command", native)
    destination = tmp_path / "evidence"
    destination.mkdir()
    execute = mutation.native_executor({}, "cov.json")
    if kind == "drift":
        with pytest.raises(isolation.RuntimeDriftError):
            mutation.collect_mutation(after, before, _config(), destination, execute=execute)
        return
    report = mutation.collect_mutation(after, before, _config(), destination, execute=execute)
    metrics = mutation.mutation_metrics(report, destination, config=_config(), snapshot=after)
    assert metrics["status"] == "incomplete"
    assert str(metrics["reason"]).startswith(f"infrastructure failure: {_UNVERIFIED}: ")
    # The record must match what the executor saved for the run that never launched.
    _object(report["infrastructure_failure"])["reason"] = "other"
    with pytest.raises(ValueError, match="infrastructure failure differs"):
        mutation.mutation_metrics(report, destination, config=_config(), snapshot=after)


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


def test_a_parse_failure_outranks_a_red_baseline(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path,
        lambda view: (1, False, "") if view.seen == 1 else (0, False, ""),
        after={"src/broken.py": "def (:\n"},
    )
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete" and "src/broken.py" in str(metrics["reason"])


@pytest.mark.parametrize(
    "finder",
    [
        subprocess.TimeoutExpired(["finder"], 600),
        OSError("finder killed"),
        subprocess.CompletedProcess(["finder"], 2, "", "panic: crashed"),
        subprocess.CompletedProcess(["finder"], 0, "not json", ""),
        subprocess.CompletedProcess(["finder"], 0, '{"sites": []}', ""),
        subprocess.CompletedProcess(["finder"], 0, '{"sites": [{}], "failures": []}', ""),
        subprocess.CompletedProcess(["finder"], 0, '{"sites": [], "failures": [null]}', ""),
    ],
    ids=["timeout", "os-error", "crash", "not-json", "no-failure-list", "bad-site", "bad-failure"],
)
def test_a_site_finder_that_fails_leaves_its_files_unparsed_and_the_attempt_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finder: object
) -> None:
    binary = tmp_path / "go-finder"
    binary.write_bytes(b"finder")

    def run(*args: object, **kwargs: object) -> object:
        if isinstance(finder, BaseException):
            raise finder
        return finder

    monkeypatch.setattr(isolation, "run", run)
    tool = {"path": str(binary), "sha256": hashlib.sha256(b"finder").hexdigest()}
    report, _, destination = _measure(
        tmp_path,
        _passing(_kills_first),
        after={"src/table.go": "package x\n"},
        config=_config(site_finders={"go": tool}),
    )
    metrics = _metrics(report, destination, _config(site_finders={"go": tool}))
    unparsed = _object(metrics["unparsed_files"])
    assert set(unparsed) == {"src/table.go"}
    assert str(unparsed["src/table.go"]).startswith("go site finder: ")
    assert metrics["status"] == "incomplete" and "src/table.go" in str(metrics["reason"])


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


def _as_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Find sites in every changed file as Python, so Go and TypeScript files need no parser."""

    def find(
        root: Path, paths: list[str], finders: object, **kwargs: object
    ) -> tuple[list[Site], dict[str, str]]:
        sites = [
            site
            for path in sorted(paths)
            for site in mutation.python_sites(path, (root / path).read_bytes())
        ]
        return sites, {}

    monkeypatch.setattr(mutation, "find_sites", find)


def test_go_mutants_that_do_not_build_are_invalid_and_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_python(monkeypatch)

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.command == mutation.GO_CHECK:
            return _kills_first(view), False, ""
        return int(view.mutated), False, ""

    report, fake, destination = _measure(tmp_path, behaviour, name="src/grade.go")
    metrics = _metrics(report, destination)
    assert (metrics["invalid"], metrics["killed"], metrics["valid"]) == (1, 1, 1)
    checks = [call[0] == mutation.GO_CHECK for call in fake.calls]
    assert checks == [False, False, True, True, False, False]


def test_a_check_that_cannot_run_is_an_error_not_an_invalid_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bash exits 127 when the image has no `go`: the mutant's validity is unknown.
    _as_python(monkeypatch)
    report, _, destination = _measure(
        tmp_path,
        lambda view: (127, False, "") if view.command == mutation.GO_CHECK else (0, False, ""),
        name="src/grade.go",
    )
    metrics = _metrics(report, destination)
    assert (metrics["errors"], metrics["invalid"], metrics["status"]) == (2, 0, "incomplete")


def test_typescript_mutants_are_invalid_only_for_new_tsc_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_python(monkeypatch)
    known = "src/grade.ts(2,5): error TS2322: Type 'x' is wrong.\n"

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.command != mutation.TSC_CHECK:
            return int(view.mutated), False, ""
        if not view.mutated:
            return 2, False, known
        # The first mutant, on line 2, shifts the known error's column; the second, on line 4,
        # adds a second copy of it.
        if FIRST in view.source:
            return 2, False, known.replace("(2,5)", "(2,9)")
        return 2, False, known + known.replace("(2,5)", "(4,1)")

    report, _, destination = _measure(tmp_path, behaviour, name="src/grade.ts")
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


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _edit_receipt(directory: Path, run: dict[str, object], **changes: object) -> None:
    path = directory / str(run["receipts"]) / "execution.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


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
        lambda report, directory: _edit_receipt(directory, report["baseline"][0], command=["x"]),
        lambda report, directory: _edit_receipt(
            directory, report["baseline"][1], image_id="sha256:other"
        ),
        lambda report, directory: _edit_receipt(
            directory, report["mutants"][0]["runs"][0], exit_code=0
        ),
        lambda report, directory: _edit_receipt(
            directory, report["mutants"][0]["runs"][1], timed_out=True
        ),
        lambda report, directory: _edit_receipt(
            directory, report["mutants"][1]["runs"][0], stdout="PB-SUITE-EXIT 1\n"
        ),
        # A mutant run that measured the unmutated tree.
        lambda report, directory: _edit_receipt(
            directory,
            report["mutants"][0]["runs"][0],
            before_digest=report["after_digest"],
        ),
        lambda report, directory: report["baseline"][1].update(
            receipts=report["baseline"][0]["receipts"]
        ),
        # Another spelling of the first baseline run's receipts names the same directory.
        lambda report, directory: report["baseline"][1].update(
            receipts=report["baseline"][0]["receipts"] + "/."
        ),
        lambda report, directory: report["baseline"][1].update(
            receipts=report["baseline"][0]["receipts"].replace("/", "//")
        ),
        lambda report, directory: report["baseline"][1].update(
            receipts="runs/../" + report["baseline"][0]["receipts"]
        ),
        lambda report, directory: report["baseline"][1].update(
            receipts=str(directory / report["baseline"][0]["receipts"])
        ),
        lambda report, directory: (
            (directory / "runs/999-alias").symlink_to(
                directory / report["baseline"][0]["receipts"]
            ),
            report["baseline"][1].update(receipts="runs/999-alias"),
        ),
        lambda report, directory: report["mutants"][0].update(mutated_sha256="sha256:0"),
        lambda report, directory: report["mutants"][0]["runs"][0].update(timeout_seconds=1),
    ],
    ids=[
        "sample-order",
        "missing-rerun",
        "status",
        "candidate-off-diff",
        "saved-output",
        "receipt-command",
        "receipt-image",
        "receipt-exit",
        "receipt-timeout",
        "receipt-output",
        "receipt-input",
        "shared-receipts",
        "receipts-dot-alias",
        "receipts-double-slash-alias",
        "receipts-parent-alias",
        "receipts-absolute-alias",
        "receipts-symlink-alias",
        "mutant-bytes",
        "timeout-limit",
    ],
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
        snapshot, destination, command = (
            kwargs["snapshot"],
            kwargs["destination"],
            kwargs["command"],
        )
        assert isinstance(snapshot, Path) and isinstance(destination, Path)
        assert isinstance(command, list)
        failing = int((snapshot / "src/grade.py").read_text() != AFTER)
        destination.mkdir(parents=True, exist_ok=True)
        _receipt(
            destination, snapshot, command, Run(failing, False, f"PB-SUITE-EXIT {failing}\n", 1)
        )
        return dict(json.loads((destination / "execution.json").read_text()))

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


@pytest.mark.parametrize(
    ("case", "status", "counts"),
    [
        ("available", "available", (1, 2, 50.0)),
        ("zero covered", "available", (0, 2, 0.0)),
        ("no candidates", "unavailable", None),
        ("no coverage", "unavailable", None),
        ("no mutation", "unavailable", None),
        ("no lines", "unavailable", None),
        ("missing eligible file", "incomplete", None),
        ("mapped eligible file", "incomplete", None),
        ("unrelated missing file", "available", (1, 2, 50.0)),
        ("unparsed candidates", "incomplete", None),
        ("unavailable catch rate", "available", (1, 2, 50.0)),
        ("unsampled lines", "available", (1, 12, 8.333333)),
    ],
)
def test_decision_line_coverage_uses_all_unique_candidate_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    case: str, status: str, counts: tuple[int, int, float] | None,
) -> None:
    source = AFTER
    if case == "no candidates":
        source = "answer = 2\n"
    elif case == "unsampled lines":
        source = "\n".join(f"value_{i} = flag < {i}" for i in range(12)) + "\n"
    behaviour = _passing(_kills_first)
    if case == "unavailable catch rate":
        def behaviour(view: View) -> tuple[int | None, bool, str]:
            return 1, False, ""
    report, _, destination = _measure(tmp_path, behaviour, after={"src/grade.py": source})
    if case == "unparsed candidates":
        report["unparsed_files"] = {"src/other.py": "parse failed"}
    metrics = _metrics(report, destination)
    if case == "unavailable catch rate":
        assert metrics["status"] == "unavailable"
    # Five sites on two lines, or twelve lines before the ten-mutant sample.
    if case == "unsampled lines":
        assert metrics["selected"] == 10 and metrics["candidate_line_count"] == 12
    coverage: dict[str, object] = {
        "coordinates": {
            "line": {
                "total": ["src/grade.py:2", "src/grade.py:3", "src/grade.py:4"],
                "covered": [] if case == "zero covered" else ["src/grade.py:2", "src/grade.py:3"],
            }
        }
    }
    if case == "no lines":
        coverage["coordinates"] = {}
    if case in {"missing eligible file", "unrelated missing file"}:
        coverage["unmeasured_source_files"] = [
            "src/other.py" if case == "unrelated missing file" else "src/grade.py"
        ]
    if case == "mapped eligible file":
        coverage["unavailable_populations"] = {
            "lines": {"files": ["src/grade.py"], "reason": "line directives"}
        }
    if case == "unsampled lines":
        coverage["coordinates"] = {
            "line": {
                "total": [f"src/grade.py:{i}" for i in range(1, 13)],
                "covered": ["src/grade.py:12"],
            }
        }
    change = compare_snapshots(tmp_path / "before", tmp_path / "after")
    before: dict[str, object] = {"snapshot_digest": change["before_digest"]}
    after: dict[str, object] = {
        "snapshot_digest": change["after_digest"], "mutation": {"metrics": metrics},
    }
    if case != "no coverage":
        after["coverage"] = {"metrics": coverage}
    if case == "no mutation":
        del after["mutation"]
    monkeypatch.setattr(
        harbor_backend, "run_native_command",
        lambda **kwargs: pytest.fail("deriving decision-line coverage must not run containers"),
    )
    compared = _object(compare_evidence(before, after, change=change)["mutation"])
    decision = _object(compared["decision_line_coverage"]) if case != "no mutation" else compared
    assert decision["status"] == status
    if counts is None:
        assert decision.get("percent") is None
    else:
        assert (decision["covered"], decision["total"], decision["percent"]) == counts


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
