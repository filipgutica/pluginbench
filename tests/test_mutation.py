"""Mutation bug-catch rate: site finding, sampling, run classification and report binding."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import native_support
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


def test_sample_stops_at_m_and_takes_every_line_when_fewer() -> None:
    many = [_site("a.py", line, "relational_boundary") for line in range(1, 30)]
    assert len(mutation.sample(many, "sha256:x")) == mutation.SAMPLE_SIZE == 20
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
    assert mutation.suite_status(Run(exit_code, timed_out, stdout)) == status


def test_csstree_takes_its_suite_status_from_the_process_exit() -> None:
    assert "https://github.com/csstree/csstree" in mutation.PROCESS_EXIT_REPOSITORIES
    assert mutation.suite_status(Run(2, False, "no marker\n"), process_exit=True) == 2
    assert mutation.suite_status(Run(None, False, ""), process_exit=True) is None


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


# A prepared task: the installed coding image on top of its base image. Mutation measures in
# the base image, at two CPUs and 2,048 MiB.
BASE_IMAGE = {"image_id": "sha256:base", "workdir": "/app"}
IMAGE = {
    "image_identity": {"image_id": "sha256:installed"},
    "toolchain_identity": {"image_id": "sha256:installed", "base_image_identity": BASE_IMAGE},
    "isolation": {"resources": {"cpus": 1, "memory_mb": 4096}},
    "resources": {"cpus": 1, "memory_mb": 4096},
}
RESOURCES = {"cpus": 2, "memory_mb": 2048}


class Clock:
    """A monotonic clock each thread advances on its own timeline, as parallel lanes do.

    A lane's first reading starts at the latest time any thread reached; the thread that made
    the clock always reads that latest time, as it does after joining its lanes.
    """

    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.latest = 0
        self.local = threading.local()
        self.lock = threading.Lock()

    def __call__(self) -> int:
        with self.lock:
            if threading.get_ident() == self.owner or not hasattr(self.local, "ns"):
                self.local.ns = self.latest
            return int(self.local.ns)

    def advance(self, ms: int) -> None:
        self()
        with self.lock:
            self.local.ns += ms * 1_000_000
            self.latest = max(self.latest, self.local.ns)


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
    # The sample index of the mutant this run measures, from its receipt directory's name.
    site: int | None = None

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
                "image_id": BASE_IMAGE["image_id"],
                "resources": RESOURCES,
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


# How long a fake run takes in milliseconds, given what it sees and its timeout.
Duration = Callable[[View, int], int]


def _second_or_timeout(view: View, timeout: int) -> int:
    return 1000


class Fake:
    """Answers each run from the tree it is given; records the commands and timeouts it saw.

    Each run advances `clock` by its duration, one second unless `duration` says otherwise. A
    run that needs longer than its timeout times out at it, unless `late` is set: the real
    launcher rounds a timeout up to whole seconds and times only its exec, so it can return an
    exit after the engine's limit. Lanes call it from several threads, so it counts the calls
    running at once.
    """

    def __init__(
        self, behaviour: Behaviour, before: Path, after: Path, clock: Clock | None = None,
        duration: Duration | None = None, late: bool = False,
    ) -> None:
        self.late = late
        self.behaviour = behaviour
        self.versions = (_files(before), _files(after))
        self.calls: list[tuple[list[str], dict[str, str], int]] = []
        self.clock = clock or Clock()
        self.duration = duration or _second_or_timeout
        self.lock = threading.Lock()
        self.active = self.most_active = 0

    def __call__(self, tree: Path, command: list[str], timeout: int, destination: Path) -> Run:
        files = _files(tree)
        # A source-only tree mixes the two snapshots; only a mutant has text from neither.
        mutated = any(
            all(version.get(path) != text for version in self.versions)
            for path, text in files.items()
        )
        with self.lock:
            seen = sum(1 for call in self.calls if call[1] == files and call[0] == command)
            self.calls.append((command, files, timeout))
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        named = destination.parent.name
        site = int(named.removeprefix("site-")) if named.startswith("site-") else None
        view = View(files, command, mutated, seen, site)
        try:
            try:
                status, timed_out, extra = self.behaviour(view)
            except mutation.InfrastructureFailure as error:
                # The launcher writes its receipt, with the error, whatever happens.
                (destination / "execution.json").write_text(json.dumps({"error": str(error)}))
                raise
            took = self.duration(view, timeout)
            timed_out = timed_out or (took > timeout and not self.late)
            self.clock.advance(timeout if timed_out else took)
        finally:
            with self.lock:
                self.active -= 1
        marker = "" if status is None else f"PB-SUITE-EXIT {status}\n"
        run = Run(None if timed_out else status, timed_out, extra + marker)
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
    limits: mutation.Limits | None = None,
    duration: Duration | None = None,
    late: bool = False,
) -> tuple[dict[str, object], Fake, Path]:
    before = _tree(tmp_path / "before", {name: BEFORE, "tests/test_grade.py": "x\n"})
    files = {name: AFTER, "tests/test_grade.py": "x\n", **(after or {})}
    submitted = _tree(tmp_path / "after", files)
    destination = tmp_path / "evidence"
    destination.mkdir()
    fake = Fake(behaviour, before, submitted, duration=duration, late=late)
    report = mutation.collect_mutation(
        submitted, before, config or _config(), destination, execute=fake, limits=limits,
        clock=fake.clock,
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
    # Two baseline runs at the profile's 100 s, then, in lanes, each mutant's first run at its
    # 3B cap, B being one second, and the kill's confirmation at what is left of its 6B.
    assert sorted(call[2] for call in fake.calls) == [3000, 3000, 5000, 100_000, 100_000]
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


def test_a_kill_whose_confirmation_the_deadline_cuts_is_unconfirmed_never_a_kill(
    tmp_path: Path,
) -> None:
    report, _, destination = _measure(
        tmp_path,
        lambda view: (
            (None, True, "")
            if view.mutated and FIRST in view.source and view.seen
            else ((_kills_first(view) if view.mutated else 0), False, "")
        ),
    )
    metrics = _metrics(report, destination)
    assert (metrics["killed"], metrics["timed_out"], metrics["unconfirmed"]) == (0, 0, 1)
    assert (metrics["survived"], metrics["valid"], metrics["bug_catch_percent"]) == (1, 1, 0.0)
    assert _object(_list(metrics["mutants"])[0])["outcome"] == "unconfirmed"


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
        execute(tmp_path, ["suite"], 10_000, tmp_path / "run")
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

    clock = Clock()

    def native(**kwargs: object) -> dict[str, object]:
        clock.advance(1000)
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
        _receipt(destination, snapshot, command, Run(0, False, "PB-SUITE-EXIT 0\n"))
        return dict(json.loads((destination / "execution.json").read_text()))

    monkeypatch.setattr(harbor_backend, "run_native_command", native)
    destination = tmp_path / "evidence"
    destination.mkdir()
    execute = mutation.native_executor({}, "cov.json")
    if kind == "drift":
        with pytest.raises(isolation.RuntimeDriftError):
            mutation.collect_mutation(
                after, before, _config(), destination, execute=execute, clock=clock
            )
        return
    report = mutation.collect_mutation(
        after, before, _config(), destination, execute=execute, clock=clock
    )
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
    # An incomplete attempt's counts are partial, under their own key, never pooled ones.
    assert metrics["status"] == "incomplete" and metrics["errors"] is None
    assert _object(metrics["partial_counts"])["errors"] == 1
    assert metrics["killed"] is None and metrics["valid"] is None
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
    partial = _object(metrics["partial_counts"])
    assert (partial["errors"], partial["invalid"], metrics["status"]) == (2, 0, "incomplete")


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
    # Serial runs are numbered; each mutant's runs are named by its site, whichever lane ran it.
    runs = sorted(
        path.relative_to(destination / "runs").as_posix()
        for path in (destination / "runs").glob("**/execution.json")
    )
    assert runs == [
        "000-baseline/execution.json",
        "001-baseline/execution.json",
        "002-source-only-baseline/execution.json",
        "003-source-only-baseline/execution.json",
        "site-000/00-mutant/execution.json",
        "site-000/01-mutant/execution.json",
        "site-001/00-mutant/execution.json",
        "site-001/01-mutant/execution.json",
        "source-only-site-000/00-mutant/execution.json",
        "source-only-site-001/00-mutant/execution.json",
        "source-only-site-001/01-mutant/execution.json",
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
        lambda report, directory: report["mutants"][0]["runs"][0].update(timeout_ms=1),
        # The confirmation claims a start that leaves it no time: its clocks no longer add up.
        lambda report, directory: report["mutants"][0]["runs"][1].update(
            started_ms=report["mutants"][0]["dispatched_ms"] + 6000
        ),
        lambda report, directory: report["mutants"][0]["runs"][1].update(bound="command"),
        lambda report, directory: report["mutants"][0].update(
            dispatched_ms=report["mutants"][0]["dispatched_ms"] + 1
        ),
        lambda report, directory: _edit_receipt(
            directory, report["baseline"][0], resources={"cpus": 4, "memory_mb": 2048}
        ),
        # The run used the installed coding image, not the task's base image.
        lambda report, directory: _edit_receipt(
            directory, report["baseline"][0], image_id="sha256:installed"
        ),
        lambda report, directory: report.update(sample_size=1),
        lambda report, directory: report["mutants"][1].update(settled_ms=None),
        lambda report, directory: report.update(required_settled_ms=None),
        lambda report, directory: report["clocks"].update(baseline_ms=0),
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
        "confirmation-start",
        "confirmation-bound",
        "dispatch-time",
        "receipt-resources",
        "installed-image",
        "sample-size",
        "unsettled",
        "required-phase",
        "baseline-clock",
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

    # Each container run takes a second on the clock the collector reads.
    clock = Clock()
    monkeypatch.setattr(mutation, "time", SimpleNamespace(monotonic_ns=clock))

    def native(**kwargs: object) -> dict[str, object]:
        clock.advance(1000)
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
            destination, snapshot, command, Run(failing, False, f"PB-SUITE-EXIT {failing}\n")
        )
        return dict(json.loads((destination / "execution.json").read_text()))

    monkeypatch.setattr(harbor_backend, "run_native_command", native)
    capacity: list[tuple[int, int]] = []
    monkeypatch.setattr(
        harbor_backend, "assert_capacity",
        lambda inputs, *, jobs: capacity.append((len(inputs), jobs)),
    )
    config = tmp_path / "mutation.json"
    config.write_text(json.dumps(_config()))
    collector = Collector(kind="mutation", config=config, scope=["*"], tool_version=mutation.VERSION)
    evidence = tmp_path / "evidence"
    paths = collect_measurements(after, evidence, [collector], before=before)
    refs = {"mutation": str(paths["mutation"].resolve())}
    measured = evaluate_evidence(after, refs, evidence)["mutation"]
    assert measured["status"] == "available"  # type: ignore[index]
    # A submission collector may run beside another attempt's, so it keeps one lane and needs
    # no capacity beyond its one container's.
    assert capacity == []
    assert json.loads((paths["mutation"].parent / "report.json").read_text())["lanes"] == 1
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
        ("continuation line", "available", (1, 1, 100.0)),
        ("no line in population", "unavailable", None),
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
    # Ten mutants leave two of twelve candidate lines unsampled.
    limits = mutation.Limits(sample_size=10) if case == "unsampled lines" else None
    report, _, destination = _measure(
        tmp_path, behaviour, after={"src/grade.py": source}, limits=limits
    )
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
    # A site on a continuation line has no coverage coordinate: it leaves the denominator.
    if case == "continuation line":
        coverage["coordinates"] = {
            "line": {"total": ["src/grade.py:2", "src/grade.py:3"], "covered": ["src/grade.py:2"]}
        }
    if case == "no line in population":
        coverage["coordinates"] = {"line": {"total": ["src/grade.py:3"], "covered": []}}
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
    if case in {"continuation line", "no line in population"}:
        assert decision["not_in_population"] == (1 if case == "continuation line" else 2)


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


# --- Version 2: clocks, lanes and version 1 reports ----------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "ms"),
    [(0, 0, 0), (0, 1, 1), (0, 999_999, 1), (0, 1_000_000, 1), (0, 1_000_001, 2), (7, 2_000_007, 2)],
)
def test_elapsed_time_is_whole_milliseconds_rounded_up(start: int, end: int, ms: int) -> None:
    assert mutation.elapsed_ms(start, end) == ms


@pytest.mark.parametrize(
    ("started", "cap", "deadline", "attempt", "expected"),
    [
        # The command's own cap binds only while it is strictly the least time left.
        (0, 3000, 3001, None, ("command", 3000)),
        (0, 3000, 3000, None, ("site", 3000)),
        (1, 3000, 3000, None, ("site", 2999)),
        # The attempt wins every tie.
        (0, 3000, 3000, 3000, ("attempt", 3000)),
        (0, 3000, 3001, 3001, ("command", 3000)),
        (0, None, 6000, 6000, ("attempt", 6000)),
        (0, None, 6000, 6001, ("site", 6000)),
        # No time left: the call never starts.
        (6000, None, 6000, None, ("site", 0)),
        (6001, None, 6000, 9000, ("site", -1)),
        (0, 100_000, None, None, ("command", 100_000)),
    ],
)
def test_a_call_gets_the_least_time_left_and_ties_go_to_the_attempt_then_the_site(
    started: int,
    cap: int | None,
    deadline: int | None,
    attempt: int | None,
    expected: tuple[str, int],
) -> None:
    assert mutation.call_limit(
        started_ms=started, cap_ms=cap, deadline_ms=deadline, attempt_ms=attempt
    ) == expected


def _fake_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Go-style build check for Python files, so a test can spend a mutant's time on it."""
    monkeypatch.setattr(mutation, "_check_command", lambda path: mutation.GO_CHECK)


def _one_lane(**limits: int | None) -> mutation.Limits:
    return mutation.Limits(lanes=1, **limits)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("check_ms", "outcome", "first_timeout"),
    [
        # B is one second: the first run's cap is 3,000 ms, the site's deadline 6,000 ms.
        (2999, "timeout_kill", 3000),  # 3,001 ms left: the run stops at its own cap.
        (3000, "unconfirmed", 3000),  # 3,000 ms left: a tie, so the site cut it.
        (3001, "unconfirmed", 2999),
        (6000, "unconfirmed", None),  # No time left: the first run never starts.
    ],
)
def test_a_first_run_timeout_is_a_kill_only_at_its_own_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_ms: int, outcome: str,
    first_timeout: int | None,
) -> None:
    _fake_check(monkeypatch)

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.command == mutation.GO_CHECK:
            return 0, False, ""
        if view.mutated and FIRST in view.source:
            return None, True, ""
        return (1 if view.mutated else 0), False, ""

    report, fake, destination = _measure(
        tmp_path, behaviour, limits=_one_lane(),
        duration=lambda view, timeout: (
            check_ms if view.command == mutation.GO_CHECK and FIRST in view.source else 1000
        ),
    )
    metrics = _metrics(report, destination)
    first = _object(_list(metrics["mutants"])[0])
    assert first["outcome"] == outcome
    runs = _list(_object(_list(report["mutants"])[0])["runs"])
    assert [_object(run).get("timeout_ms") for run in runs] == [first_timeout or 6000 - check_ms]
    assert (metrics["timed_out"], metrics["unconfirmed"]) == (
        (1, 0) if outcome == "timeout_kill" else (0, 1)
    )
    # A first run cut by the site is never counted: only the second mutant's kill remains.
    assert metrics["valid"] == (2 if outcome == "timeout_kill" else 1)
    assert len([call for call in fake.calls if call[0] == ["suite"]]) == (
        2 + 2 + (0 if first_timeout is None else 1)
    )


@pytest.mark.parametrize(
    ("first_ms", "confirmation", "outcome"),
    [
        (3000, (1, False), "killed"),  # 3,000 ms left, and the rerun reproduces the kill.
        (3000, (None, True), "unconfirmed"),  # The rerun reaches the site deadline.
        (2999, (None, True), "unconfirmed"),
        (3000, (0, False), "flaky"),
    ],
)
def test_a_confirmation_the_site_deadline_cuts_is_unconfirmed_never_a_kill(
    tmp_path: Path, first_ms: int, confirmation: tuple[int | None, bool], outcome: str
) -> None:
    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.mutated and FIRST in view.source:
            return (1, False, "") if view.seen == 0 else (*confirmation, "")
        return 0, False, ""

    def duration(view: View, timeout: int) -> int:
        # The kill's first run ends at `first_ms` with a status; its rerun takes all it has.
        if view.mutated and FIRST in view.source:
            return first_ms if view.seen == 0 else timeout
        return 1000

    report, _, destination = _measure(
        tmp_path, behaviour, limits=_one_lane(), duration=duration
    )
    metrics = _metrics(report, destination)
    assert _object(_list(metrics["mutants"])[0])["outcome"] == outcome
    runs = _list(_object(_list(report["mutants"])[0])["runs"])
    assert _object(runs[1])["timeout_ms"] == 6000 - first_ms
    assert metrics["killed"] == (1 if outcome == "killed" else 0)


def test_baselines_get_their_cap_and_b_is_the_slower_rounded_up(tmp_path: Path) -> None:
    durations = iter([800, 1200])

    def duration(view: View, timeout: int) -> int:
        return next(durations) if not view.mutated else 1000

    report, fake, destination = _measure(
        tmp_path, _passing(_kills_first), limits=_one_lane(baseline_ms=4000), duration=duration
    )
    assert [call[2] for call in fake.calls[:2]] == [4000, 4000]
    # B = 1,200 ms: each first run is capped at 3,600 ms.
    assert {call[2] for call in fake.calls[2:] if call[2] != 7200 - 1000} == {3600}
    assert _metrics(report, destination)["status"] == "available"


def test_a_baseline_that_reaches_its_cap_is_red(tmp_path: Path) -> None:
    report, _, destination = _measure(
        tmp_path,
        lambda view: (None, True, "") if not view.mutated else (0, False, ""),
        limits=_one_lane(baseline_ms=4000),
    )
    metrics = _metrics(report, destination)
    assert metrics["status"] == "unavailable" and "baseline red" in str(metrics["reason"])


def _lines(count: int) -> str:
    """A changed file with one relational site on each of `count` lines."""
    return "".join(f"value_{index} = flag < {index}\n" for index in range(count))


def test_four_lanes_never_become_five(tmp_path: Path) -> None:
    barrier = threading.Barrier(4, timeout=10)
    waited = threading.Lock()
    gates = [0]

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.mutated:
            with waited:
                gates[0] += 1
                wait = gates[0] <= 4
            if wait:
                barrier.wait()  # Only four lanes running at once get past this.
            return 0, False, ""
        return 0, False, ""

    report, fake, destination = _measure(
        tmp_path, behaviour, after={"src/grade.py": _lines(12)}, limits=mutation.Limits(lanes=4)
    )
    assert fake.most_active == 4 and report["lanes"] == 4
    metrics = _metrics(report, destination)
    assert (metrics["survived"], metrics["selected"]) == (12, 12)


def test_results_follow_the_sample_whatever_order_lanes_finish_in(tmp_path: Path) -> None:
    others = threading.Semaphore(0)
    order: list[int] = []

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.site is not None:
            if view.site == 0:
                for _ in range(3):
                    assert others.acquire(timeout=10)  # The first site settles last.
            order.append(view.site)
            if view.site != 0:
                others.release()
        return 0, False, ""

    report, _, destination = _measure(
        tmp_path, behaviour, after={"src/grade.py": _lines(4)}, limits=mutation.Limits(lanes=4)
    )
    assert order[-1] == 0 and sorted(order) == [0, 1, 2, 3]
    assert [_object(item)["site"] for item in _list(report["mutants"])] == [0, 1, 2, 3]
    metrics = _metrics(report, destination)
    selected = _list(report["selected"])
    assert [
        (_object(row)["site"], _object(row)["line"]) for row in _list(metrics["mutants"])
    ] == [(index, _object(selected[index])["line"]) for index in range(4)]
    assert _object(metrics["sites"]) == {
        "settled": [0, 1, 2, 3], "interrupted": [], "unstarted": []
    }


def test_the_attempt_limit_expiring_is_infrastructure_with_its_partial_counts_apart(
    tmp_path: Path,
) -> None:
    # Two baselines take 2,000 ms; one lane then measures sites a second each, until 5,500 ms
    # cuts the fourth site's run: an infrastructure failure, never an observation.
    report, _, destination = _measure(
        tmp_path, _passing(lambda view: 0), after={"src/grade.py": _lines(8)},
        limits=_one_lane(attempt_ms=5500),
    )
    failure = _object(report["infrastructure_failure"])
    assert failure["cause"] == "attempt_deadline" and failure["receipts"] is None
    assert report["required_settled_ms"] is None
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete"
    assert str(metrics["reason"]).startswith("infrastructure failure: the attempt's 5500 ms")
    assert (metrics["killed"], metrics["valid"], metrics["survived"]) == (None, None, None)
    assert _object(metrics["partial_counts"])["survived"] == 3
    assert _object(metrics["sites"]) == {
        "settled": [0, 1, 2], "interrupted": [3], "unstarted": [4, 5, 6, 7]
    }
    # The cut run kept its record: the attempt's limit bound it.
    cut = _object(_list(_object(_list(report["mutants"])[3])["runs"])[0])
    assert (cut["bound"], cut["timeout_ms"], cut["timed_out"]) == ("attempt", 500, True)
    # Partial outcomes publish no execution split either.
    change = compare_snapshots(tmp_path / "before", tmp_path / "after")
    coverage: dict[str, object] = {
        "metrics": {"coordinates": {"line": {"total": ["src/grade.py:1"], "covered": []}}}
    }
    compared = mutation.compare_mutation({"metrics": metrics}, coverage, change)
    assert compared["status"] == "incomplete" and compared["killed"] is None
    assert _object(compared["execution_split"])["status"] == "incomplete"
    assert _object(compared["partial_counts"])["survived"] == 3
    # A forged expiry needs the limit it names.
    _object(report["clocks"])["attempt_ms"] = None
    with pytest.raises(ValueError):
        _metrics(report, destination)


def test_the_attempt_limit_refuses_a_site_once_it_has_passed(tmp_path: Path) -> None:
    report, fake, destination = _measure(
        tmp_path, _passing(lambda view: 0), after={"src/grade.py": _lines(4)},
        limits=_one_lane(attempt_ms=4000),
    )
    # Baselines end at 2,000 ms, two sites at 4,000 ms: the third is never dispatched.
    assert len(fake.calls) == 4
    metrics = _metrics(report, destination)
    assert _object(metrics["sites"]) == {"settled": [0, 1], "interrupted": [], "unstarted": [2, 3]}
    assert _object(report["infrastructure_failure"])["cause"] == "attempt_deadline"


def test_required_mutants_run_first_and_a_later_expiry_cuts_only_the_diagnostic(
    tmp_path: Path,
) -> None:
    # Both mutants are killed by the full suite; the source-only baseline starts after both
    # settle, and the limit expires during it.
    report, fake, destination = _measure(
        tmp_path,
        _passing(lambda view: 1),
        after={"tests/test_grade.py": "y\n"},
        limits=_one_lane(attempt_ms=6500),
    )
    labels = [_object(call)["label"] for call in _list(_object(report["source_only"])["baseline"])]
    assert labels == ["source-only-baseline"]
    suite = [call for call in fake.calls if "tests/test_grade.py" in call[1]]
    assert suite[-1][1]["tests/test_grade.py"] == "x\n"  # The last run was diagnostic.
    assert report["infrastructure_failure"] is None and report["required_settled_ms"] == 6000
    metrics = _metrics(report, destination)
    assert (metrics["status"], metrics["killed"], metrics["valid"]) == ("available", 2, 2)
    share = _object(metrics["test_change_kill_share"])
    assert share["status"] == "incomplete"


def test_a_version_1_report_still_validates() -> None:
    fixture = Path(__file__).parent / "fixtures" / "mutation-v1"
    report = json.loads((fixture / "evidence" / "report.json").read_text())
    config = json.loads((fixture / "profile.json").read_text())
    assert report["version"] == mutation.V1 and config["observer_version"] == 1
    metrics = mutation.mutation_metrics(
        report, fixture / "evidence", config=config, snapshot=fixture / "after"
    )
    assert metrics == json.loads((fixture / "metrics.json").read_text())
    # The version 1 rules still apply: ten mutants, `3 x slowest + 60 s` timeouts.
    tampered = json.loads(json.dumps(report))
    tampered["mutants"][0]["runs"][0]["timeout_seconds"] = 64
    with pytest.raises(ValueError, match="launcher receipt"):
        mutation.mutation_metrics(
            tampered, fixture / "evidence", config=config, snapshot=fixture / "after"
        )


# --- Version 2: a launcher that returns after the engine's limit ---------------------------


def test_a_late_green_baseline_under_the_attempt_limit_is_infrastructure(tmp_path: Path) -> None:
    # The launcher takes 750 ms for a 500 ms attempt and still reports a green exit.
    report, _, destination = _measure(
        tmp_path, _passing(lambda view: 0), limits=_one_lane(attempt_ms=500),
        duration=lambda view, timeout: 750, late=True,
    )
    first = _object(_list(report["baseline"])[0])
    assert (first["timed_out"], first["suite_status"], first["elapsed_ms"]) == (False, 0, 750)
    assert _object(report["infrastructure_failure"])["cause"] == "attempt_deadline"
    metrics = _metrics(report, destination)
    assert metrics["status"] == "incomplete" and metrics["killed"] is None


@pytest.mark.parametrize(
    ("first_ms", "outcome"),
    [
        (3000, "survived"),  # A green exit exactly at its 3B cap is on time.
        (3001, "timeout_kill"),  # One millisecond late: stopped at its own cap.
    ],
)
def test_a_late_first_run_is_cut_by_its_own_cap_whatever_it_exits_with(
    tmp_path: Path, first_ms: int, outcome: str
) -> None:
    report, _, destination = _measure(
        tmp_path, _passing(lambda view: 0), limits=_one_lane(), late=True,
        duration=lambda view, timeout: first_ms if view.mutated and FIRST in view.source else 1000,
    )
    metrics = _metrics(report, destination)
    assert _object(_list(metrics["mutants"])[0])["outcome"] == outcome
    assert metrics["status"] == "available"


@pytest.mark.parametrize(
    ("first_ms", "outcome"),
    [(3000, "survived"), (3001, "unconfirmed")],
)
def test_a_late_run_where_the_site_and_its_cap_tie_is_cut_by_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_ms: int, outcome: str
) -> None:
    # The check takes 3,000 ms of the site's 6,000, so the first run's 3,000 ms cap ties the
    # site's remaining time: a late green belongs to the site, never to the command.
    _fake_check(monkeypatch)

    def duration(view: View, timeout: int) -> int:
        if view.mutated and FIRST in view.source:
            return 3000 if view.command == mutation.GO_CHECK else first_ms
        return 1000

    report, _, destination = _measure(
        tmp_path, lambda view: (0, False, ""), limits=_one_lane(), late=True, duration=duration,
    )
    run = _object(_list(_object(_list(report["mutants"])[0])["runs"])[0])
    assert (run["bound"], run["timeout_ms"]) == ("site", 3000)
    metrics = _metrics(report, destination)
    assert _object(_list(metrics["mutants"])[0])["outcome"] == outcome


@pytest.mark.parametrize(("took", "overran"), [(3500, False), (3600, True)])
def test_a_last_return_past_the_attempt_limit_is_infrastructure_whatever_bound_its_call(
    tmp_path: Path, took: int, overran: bool
) -> None:
    # Site 1, the last, starts its first run at 3,000 ms: its 3,000 ms cap ends at 6,000 ms,
    # before the attempt's 6,500 ms limit, so the cap bounds it. It returns late, at 3,000 +
    # `took` ms: past its cap, so a timeout kill, and at 6,600 ms past the attempt's limit too.
    report, _, destination = _measure(
        tmp_path, _passing(lambda view: 0), limits=_one_lane(attempt_ms=6500), late=True,
        duration=lambda view, timeout: took if view.site == 1 else 1000,
    )
    run = _object(_list(_object(_list(report["mutants"])[1])["runs"])[0])
    assert (run["bound"], run["timeout_ms"], run["elapsed_ms"]) == ("command", 3000, took)
    metrics = _metrics(report, destination)
    if not overran:
        assert report["infrastructure_failure"] is None and report["required_settled_ms"] == 6500
        assert metrics["status"] == "available" and metrics["timed_out"] == 1
        return
    failure = _object(report["infrastructure_failure"])
    assert failure["cause"] == "attempt_deadline" and report["required_settled_ms"] is None
    assert metrics["status"] == "incomplete" and metrics["killed"] is None
    assert _object(metrics["partial_counts"])["timed_out"] == 1
    # The same evidence sealed as uninterrupted is refused.
    report.update(infrastructure_failure=None, required_settled_ms=6600)
    with pytest.raises(ValueError, match="overran its limit"):
        _metrics(report, destination)


@pytest.mark.parametrize(("took", "overran"), [(3500, False), (3600, True)])
def test_a_late_diagnostic_return_past_the_attempt_limit_leaves_only_the_share_incomplete(
    tmp_path: Path, took: int, overran: bool
) -> None:
    # The full suite kills both mutants and seals at 6,000 ms. The source-only baselines take
    # 6,000-8,000 ms; it misses the first mutant (8,000-9,000 ms). The second's first run starts
    # at 9,000 ms under its own 3,000 ms cap, before the 12,500 ms attempt limit, and returns
    # late at 9,000 + `took` ms.
    def rule(view: View) -> int:
        return int(view.files["tests/test_grade.py"] == "changed\n")

    def duration(view: View, timeout: int) -> int:
        source_only = view.mutated and view.files["tests/test_grade.py"] == "x\n"
        return took if source_only and SECOND in view.source else 1000

    report, _, destination = _measure(
        tmp_path, _passing(rule), after={"tests/test_grade.py": "changed\n"},
        limits=_one_lane(attempt_ms=12_500), late=True, duration=duration,
    )
    assert report["infrastructure_failure"] is None and report["required_settled_ms"] == 6000
    late = [
        _object(run) for row in _list(report["mutants"])
        for run in _list(_object(_object(row)["source_only"])["runs"])
        if _object(run)["elapsed_ms"] == took
    ]
    assert [(run["bound"], run["timeout_ms"]) for run in late] == [("command", 3000)]
    metrics = _metrics(report, destination)
    assert (metrics["status"], metrics["killed"], metrics["valid"]) == ("available", 2, 2)
    source = _object(report["source_only"])
    share = _object(metrics["test_change_kill_share"])
    if not overran:
        assert "interrupted" not in source
        assert (share["status"], share["percent"]) == ("available", 50.0)
        return
    assert _object(source["interrupted"])["cause"] == "attempt_deadline"
    # Every kill has a source-only outcome, yet the diagnostic was cut.
    assert all("source_only" in _object(row) for row in _list(metrics["mutants"]))
    assert share == {"status": "incomplete", "reason": "the source-only diagnostic was cut"}
    # The same diagnostic recorded as uninterrupted is refused.
    del source["interrupted"]
    with pytest.raises(ValueError, match="diagnostic overran the attempt limit"):
        _metrics(report, destination)


@pytest.mark.parametrize(("discovery_ms", "overran"), [(100, False), (101, True)])
def test_discovery_past_the_attempt_limit_is_infrastructure_with_nothing_sampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery_ms: int, overran: bool
) -> None:
    # A changed Python line with no eligible site; discovery takes `discovery_ms` of 100 ms.
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": BEFORE + "x = 1\n"})
    destination = tmp_path / "evidence"
    destination.mkdir()
    fake = Fake(_passing(lambda view: 0), before, after)
    find = mutation.find_sites

    def slow(*args: Any, **kwargs: Any) -> Any:
        fake.clock.advance(discovery_ms)
        return find(*args, **kwargs)

    monkeypatch.setattr(mutation, "find_sites", slow)
    report = mutation.collect_mutation(
        after, before, _config(), destination, execute=fake,
        limits=_one_lane(attempt_ms=100), clock=fake.clock,
    )
    assert report["selected"] == [] and not fake.calls
    metrics = _metrics(json.loads(json.dumps(report)), destination)
    if not overran:
        assert report["infrastructure_failure"] is None
        assert metrics["status"] == "unavailable"
        return
    assert _object(report["infrastructure_failure"])["cause"] == "attempt_deadline"
    assert metrics["status"] == "incomplete" and metrics["killed"] is None


def test_the_measurement_runtime_passes_the_runtime_check_of_a_saved_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shaped as harbor_backend.runtime_identity saves it, enforcement fields included.
    resources: dict[str, object] = {
        "cpus": 1, "memory_mb": 1024, "cpu_enforcement": "limit", "memory_enforcement": "limit"
    }
    saved = {**native_support.runtime_identity(), "resources": resources}
    asked: list[tuple[int, int]] = []

    def current(*, cpus: int, memory_mb: int, harness: str) -> dict[str, object]:
        asked.append((cpus, memory_mb))
        return {**saved, "resources": {**resources, "cpus": cpus, "memory_mb": memory_mb}}

    monkeypatch.setattr(harbor_backend, "runtime_identity", current)
    identity = {
        "toolchain_identity": {"base_image_identity": {"reference": "base"}},
        "image_identity": {"reference": "task"},
        "isolation": saved,
        "resources": resources,
    }

    measured = mutation.measurement_identity(identity)
    runtime = measured["isolation"]
    assert isinstance(runtime, dict)
    harbor_backend.assert_runtime(runtime, launcher=False, harbor=False)

    assert asked == [(mutation.MEASUREMENT_CPUS, mutation.MEASUREMENT_MEMORY_MB)]
    assert measured["resources"] == runtime["resources"] == {
        "cpus": mutation.MEASUREMENT_CPUS,
        "memory_mb": mutation.MEASUREMENT_MEMORY_MB,
        "cpu_enforcement": "limit",
        "memory_enforcement": "limit",
    }
