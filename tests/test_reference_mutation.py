"""Reference mutation receipts: the producer, and its rules agreeing with the engine."""

from __future__ import annotations

import errno
import itertools
import json
import threading
from pathlib import Path
from typing import Any

from types import SimpleNamespace

import pytest
import typer
from pydantic import ValidationError
from test_mutation import (
    AFTER, BASE_IMAGE, BEFORE, FIRST, IMAGE, Fake, View, _config, _lines, _passing, _tree,
)
from typer.testing import CliRunner

from pluginbench import (
    cli,
    control_store,
    harbor_backend,
    invocation_runtime,
    mutation,
    operations,
    reference_mutation,
    run_lock,
    suite_qualification,
)
from pluginbench.benchmark import ReviewerRule, Suite, SuiteTask
from pluginbench.invocation_runtime import OperationRequest
from pluginbench.reference_mutation import Contract, measure_reference
from pluginbench.suite_qualification import Policy, Receipt

PINS = {
    "content_sha256": "a" * 64,
    "observer_profile_sha256": "b" * 64,
    "observer_contract_sha256": "c" * 64,
}
TASK = SuiteTask(
    content_sha256="a" * 64,
    repository_cluster="repo-a",
    coding_contract_sha256="d" * 64,
    observer_profile_sha256="b" * 64,
    mutation_timeout_seconds=600,
)


def _reference(
    tmp_path: Path, behaviour: Any = None, **options: Any
) -> tuple[Receipt, Receipt, Fake]:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": AFTER})
    fake = Fake(behaviour or _passing(lambda view: int(FIRST in view.source)), before, after)
    mutation_receipt, time_receipt = measure_reference(
        before=before, after=after, config=_config(), contract=Contract(headroom_percent=150),
        pins=PINS, destination=tmp_path / "reference", execute=fake, clock=fake.clock, **options,
    )
    return mutation_receipt, time_receipt, fake


def test_three_independent_repeats_give_receipts_both_rules_pass(tmp_path: Path) -> None:
    mutation_receipt, time_receipt, fake = _reference(tmp_path)
    assert (mutation_receipt.gate, time_receipt.gate) == ("mutation", "mutation_time")
    for receipt in (mutation_receipt, time_receipt):
        assert receipt.pins == PINS and receipt.task == PINS["content_sha256"]
        saved = tmp_path / "reference" / f"{receipt.gate}.receipt.json"
        assert Receipt.model_validate_json(saved.read_text()) == receipt
    repeats = mutation_receipt.facts["repeats"]
    assert len({repeat["run_id"] for repeat in repeats}) == 3
    assert all((tmp_path / "reference" / f"repeat-{i}" / "report.json").is_file() for i in range(3))
    # Each repeat measured the reference in the base image, with every mutant's raw runs.
    assert {repeat["image_id"] for repeat in repeats} == {"sha256:base"}
    assert [len(repeat["sites"]) for repeat in repeats] == [2, 2, 2]
    assert len(fake.calls) == 3 * (2 + 2 + 1)
    for receipt in (mutation_receipt, time_receipt):
        rule = suite_qualification._RULES[receipt.gate]
        assert rule(receipt, TASK).status == "pass"


def test_the_receipts_pin_exactly_content_profile_and_the_common_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pin exactly"):
        measure_reference(
            before=tmp_path, after=tmp_path, config=_config(),
            contract=Contract(headroom_percent=150),
            pins={**PINS, "coding_contract_sha256": "d" * 64}, destination=tmp_path / "out",
        )


def test_a_repeat_the_attempt_limit_cuts_is_censored_for_both_rules(tmp_path: Path) -> None:
    # Baselines take two seconds of the 3.5 s limit; the kill's confirmation is cut.
    mutation_receipt, time_receipt, _ = _reference(tmp_path, attempt_ms=3500)
    assert {r["infrastructure_failure"] for r in mutation_receipt.facts["repeats"]} == {
        "attempt_deadline"
    }
    for receipt in (mutation_receipt, time_receipt):
        verdict = suite_qualification._RULES[receipt.gate](receipt, TASK)
        assert verdict.status == "unavailable"


def test_a_slow_reference_fails_only_the_time_rule(tmp_path: Path) -> None:
    mutation_receipt, time_receipt, _ = _reference(tmp_path)
    # Baselines took 2 s; two of twenty mutants took one 2 s wave of four lanes. Five waves
    # project 2 s + 5 x 2 s = 12 s, which needs 18 s at 150%: equality passes.
    tight = TASK.model_copy(update={"mutation_timeout_seconds": 17})
    assert suite_qualification._RULES["mutation"](mutation_receipt, tight).status == "pass"
    verdict = suite_qualification._RULES["mutation_time"](time_receipt, tight)
    assert verdict.status == "fail" and "12000 ms" in str(verdict.reason)
    loose = TASK.model_copy(update={"mutation_timeout_seconds": 18})
    assert suite_qualification._RULES["mutation_time"](time_receipt, loose).status == "pass"


def test_the_producer_refuses_a_sample_the_contract_did_not_prescribe(tmp_path: Path) -> None:
    mutation_receipt, _, _ = _reference(tmp_path)
    report = json.loads((tmp_path / "reference" / "repeat-0" / "report.json").read_text())
    reference_mutation._check_sample(report, Contract(headroom_percent=150))
    with pytest.raises(ValueError, match="preregistered sample"):
        reference_mutation._check_sample(report, Contract(headroom_percent=150, sample_size=1))
    report["selected"].reverse()
    with pytest.raises(ValueError, match="preregistered sample"):
        reference_mutation._check_sample(report, Contract(headroom_percent=150))


def test_a_flaky_reference_differs_across_repeats_and_fails(tmp_path: Path) -> None:
    runs = {"count": 0}

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        # The kill reproduces in the first repeat only.
        if view.mutated and FIRST in view.source:
            runs["count"] += 1
            return (1 if runs["count"] <= 2 or view.seen % 2 == 0 else 0), False, ""
        return 0, False, ""

    mutation_receipt, _, _ = _reference(tmp_path, behaviour)
    verdict = suite_qualification._RULES["mutation"](mutation_receipt, TASK)
    assert verdict.status == "fail" and "different outcomes" in str(verdict.reason)


# --- The rules' classifier is the engine's --------------------------------------------------

# A run's end: ("ran", status) on time, ("late", (clock, status)) for an exit after its limit,
# or ("timeout" | "skipped", clock) for a run a clock stopped or left no time to start.
_ENDS: list[tuple[str, Any]] = [("ran", 0), ("ran", 1), ("ran", None)] + [
    (kind, clock) for kind in ("timeout", "skipped") for clock in ("command", "site", "attempt")
] + [("late", (clock, status)) for clock in ("command", "site", "attempt") for status in (0, 1)]


def _engine_call(end: tuple[str, Any]) -> dict[str, object]:
    kind, value = end
    if kind == "ran":
        return {"dispatched": True, "timed_out": False, "suite_status": value, "bound": "command",
                "elapsed_ms": 10, "timeout_ms": 10}
    if kind == "late":
        return {"dispatched": True, "timed_out": False, "suite_status": value[1],
                "bound": value[0], "elapsed_ms": 11, "timeout_ms": 10}
    return {"dispatched": kind == "timeout", "timed_out": kind == "timeout",
            "suite_status": None, "bound": value, "elapsed_ms": 10, "timeout_ms": 10}


def _rule_end(end: tuple[str, Any]) -> tuple[str | None, int | None]:
    kind, value = end
    if kind == "ran":
        return None, value
    return (value[0], value[1]) if kind == "late" else (value, None)


@pytest.mark.parametrize("length", [0, 1, 2])
def test_the_rule_settles_every_run_sequence_as_the_engine_does(length: int) -> None:
    for ends in itertools.product(_ENDS, repeat=length):
        expected = mutation.site_outcome([_engine_call(end) for end in ends])
        assert suite_qualification._settle([_rule_end(end) for end in ends]) == expected, ends


def test_the_rule_bounds_every_call_as_the_engine_does() -> None:
    values = [None, 0, 1, 2999, 3000, 3001, 6000]
    for started, cap, deadline, attempt in itertools.product(
        [0, 1, 3000], values, values, values
    ):
        if cap is None and deadline is None and attempt is None:
            continue
        assert suite_qualification._limit(started, cap, deadline, attempt) == mutation.call_limit(
            started_ms=started, cap_ms=cap, deadline_ms=deadline, attempt_ms=attempt
        )


def test_a_file_discovery_could_not_use_leaves_both_gates_unavailable(tmp_path: Path) -> None:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {
        "src/grade.py": AFTER, "src/broken.py": "def (:\n", "src/lib.rs": "fn f() {}\n",
    })
    fake = Fake(_passing(lambda view: int(FIRST in view.source)), before, after)
    receipts = measure_reference(
        before=before, after=after, config=_config(), contract=Contract(headroom_percent=150),
        pins=PINS, destination=tmp_path / "reference", execute=fake, clock=fake.clock,
    )
    repeat = receipts[0].facts["repeats"][0]
    assert (repeat["unparsed_files"], repeat["unsupported_files"]) == (
        ["src/broken.py"], ["src/lib.rs"]
    )
    for receipt in receipts:
        verdict = suite_qualification._RULES[receipt.gate](receipt, TASK)
        assert verdict.status == "unavailable" and "population is incomplete" in str(verdict.reason)


def test_only_the_reference_producer_runs_four_lanes_and_checks_docker_holds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": AFTER})
    fake = Fake(_passing(lambda view: int(FIRST in view.source)), before, after)
    capacity: list[tuple[int, int]] = []
    monkeypatch.setattr(
        harbor_backend, "assert_capacity",
        lambda inputs, *, jobs: capacity.append((len(inputs), jobs)),
    )
    monkeypatch.setattr(mutation, "native_executor", lambda identity, *args, **kwargs: fake)
    measure_reference(
        before=before, after=after, config=_config(), contract=Contract(headroom_percent=150),
        pins=PINS, destination=tmp_path / "reference", clock=fake.clock,
    )
    assert capacity == [(4, 4)] * 3
    reports = [
        json.loads((tmp_path / "reference" / f"repeat-{i}" / "report.json").read_text())
        for i in range(3)
    ]
    assert {report["lanes"] for report in reports} == {mutation.LANES} == {4}
    # Everyone else keeps one lane: a submission collect may run beside another.
    assert mutation.Limits().lanes == 1


def test_four_lanes_of_eight_sites_pass_both_rules(tmp_path: Path) -> None:
    # Each repeat's first four mutants wait for each other, so four sites run at once.
    barrier = threading.Barrier(4, timeout=10)
    lock = threading.Lock()
    mutants = [0]

    def behaviour(view: View) -> tuple[int | None, bool, str]:
        if view.mutated:
            with lock:
                wave, mutants[0] = mutants[0] % 8 < 4, mutants[0] + 1
            if wave:
                barrier.wait()
        return 0, False, ""

    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after = _tree(tmp_path / "after", {"src/grade.py": _lines(8)})
    fake = Fake(behaviour, before, after)
    receipts = measure_reference(
        before=before, after=after, config=_config(), contract=Contract(headroom_percent=150),
        pins=PINS, destination=tmp_path / "reference", execute=fake, clock=fake.clock,
    )
    for repeat in receipts[0].facts["repeats"]:
        dispatched = sorted(site["dispatched_ms"] for site in repeat["sites"])
        assert len(dispatched) == 8 and dispatched[:4] == [2000] * 4 and dispatched[4] > 2000
    for receipt in receipts:
        assert suite_qualification._RULES[receipt.gate](receipt, TASK).status == "pass"
    # The same evidence claimed for three lanes ran more sites at once than they hold.
    facts = receipts[0].facts
    three = receipts[0].model_copy(update={"facts": {
        **facts, "contract": {**facts["contract"], "lanes": 3}
    }})
    verdict = suite_qualification._RULES["mutation"](three, TASK)
    assert verdict.status == "unavailable" and "3 lanes hold" in str(verdict.reason)


def test_a_tsc_baseline_the_attempt_limit_left_no_time_keeps_its_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second baseline ends exactly at the 2,000 ms limit, on time: the tsc baseline that
    # follows has no time left, so it never starts and has no output to read.
    monkeypatch.setattr(mutation, "_check_command", lambda path: mutation.TSC_CHECK)
    mutation_receipt, time_receipt, _ = _reference(tmp_path, attempt_ms=2000)
    repeats = mutation_receipt.facts["repeats"]
    assert [r["infrastructure_failure"] for r in repeats] == ["attempt_deadline"] * 3
    assert [r["check_baseline"] for r in repeats] == [
        {"started_ms": 2000, "dispatched": False, "kind": "tsc"}
    ] * 3
    for receipt in (mutation_receipt, time_receipt):
        assert suite_qualification._RULES[receipt.gate](receipt, TASK).status == "unavailable"


# --- The command: one registered operation on the profile's frozen daemon --------------------

_RUNTIME: dict[str, object] = {
    "docker_cli": {}, "docker_engine": {"id": "daemon", "endpoint": "unix:///fake.sock"},
}


def _command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *options: str, after: str = AFTER,
    ended: BaseException | None = None, engine: object = _RUNTIME["docker_engine"],
    profile_changes: dict[str, object] | None = None, behaviour: Any = None,
    standalone: bool = True, answering: str = "daemon", stray: str | None = None,
) -> tuple[Any, list[OperationRequest], dict[str, Any]]:
    """Run `reference-mutation` on a prepared profile, each container a fake run on a fake
    clock. Returns the result, the operations it ran and what the executor saw. `ended` is
    raised once the handler has returned, as the owner raises an unverified cleanup or a
    deadline that fires as the handler finishes. `engine` and `profile_changes` alter the
    prepared profile; a repeated option overrides the default before it. Without
    `standalone`, a usage error is raised as itself rather than rendered for a terminal.
    `answering` is the engine ID Docker gives at the profile's socket. `stray` names a file
    each run leaves in its receipt directory, as a source copy the executor could not remove."""
    before = _tree(tmp_path / "before", {"src/grade.py": BEFORE})
    after_tree = _tree(tmp_path / "after", {"src/grade.py": after})
    behaviour = behaviour or _passing(lambda view: int(FIRST in view.source))
    fake = Fake(behaviour, before, after_tree)
    isolation = {"resources": {"cpus": 1, "memory_mb": 4096}, "docker_cli": {},
                 "docker_engine": engine}
    profile = tmp_path / "mutation.json"
    # As preparation saves it: the coverage settings whole (`CoverageSettings`).
    coverage = {"command": ["suite"], "report_path": "cov.json", "timeout_seconds": 100,
                "tool_version": "7.6", "format": "coverage-py-json"}
    profile.write_text(json.dumps(_config(**{
        "native_task_identity": {**IMAGE, "isolation": isolation}, "coverage": coverage,
        **(profile_changes or {}),
    })))
    requests: list[OperationRequest] = []
    seen: dict[str, Any] = {}
    in_place = invocation_runtime.run_operation  # `operation_support.run_in_place`

    def recording(request: OperationRequest, **kwargs: Any) -> Any:
        requests.append(request)
        value = in_place(request, **kwargs)
        if ended is not None:
            raise ended
        return value

    def executor(identity: dict[str, object], *args: Any, **kwargs: Any) -> mutation.Executor:
        lock = run_lock.held_execution()
        # The check `harbor_backend._docker` runs before every create, on the daemon this
        # identity names, and the label the create then carries.
        runtime = identity["isolation"]
        assert isinstance(runtime, dict)
        invocation = invocation_runtime.container_invocation(runtime)
        seen["create"] = invocation_runtime.with_label(("create", "image"), invocation)
        seen.update(
            invocation=invocation_runtime.current(), identity=identity,
            lock=None if lock is None else lock.record["kind"],
        )
        if stray is None:
            return fake

        def leaving(
            tree: Path, command: list[str], timeout: int, destination: Path
        ) -> mutation.Run:
            run = fake(tree, command, timeout, destination)
            (destination / stray).parent.mkdir(parents=True, exist_ok=True)
            (destination / stray).write_text("{}")
            return run

        return leaving

    monkeypatch.setattr(invocation_runtime, "run_operation", recording)
    monkeypatch.setattr(
        invocation_runtime, "_recovery_docker",
        lambda *arguments, **kwargs: json.dumps({"ID": answering, "ServerErrors": None}),
    )
    monkeypatch.setattr(mutation, "native_executor", executor)
    monkeypatch.setattr(mutation, "_check_capacity", lambda identity, lanes: None)
    # The engine's only clock reading: the fake's, so each run takes its fake duration.
    monkeypatch.setattr(mutation, "time", SimpleNamespace(monotonic_ns=fake.clock))
    result = CliRunner().invoke(cli.app, [
        "reference-mutation", str(profile), "--before", str(before), "--after", str(after_tree),
        "--headroom-percent", "150", "--output", str(tmp_path / "reference"), *options,
    ], standalone_mode=standalone)
    return result, requests, seen


def test_the_command_times_the_reference_in_one_operation_and_freeze_can_never_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, requests, seen = _command(tmp_path, monkeypatch, "--attempt-seconds", "600")
    assert result.exit_code == 0, result.output
    [request] = requests
    assert request.operation == "reference"
    # Its outer bound holds every repeat's whole attempt limit, then its cleanup.
    assert request.timeout == operations.reference_budget(600_000) > 3 * 600
    assert request.arguments["attempt_ms"] == 600_000
    assert request.arguments["contract"] == {"headroom_percent": 150, "sample_size": 20, "lanes": 4}
    assert "pins" not in request.arguments
    # Containers start on the daemon the profile froze, labelled with this invocation, while
    # the command holds the host.
    assert request.spec.docker == _RUNTIME
    assert seen["invocation"] == request.spec.id
    assert seen["create"] == (
        "create", "--label", f"{invocation_runtime.LABEL_KEY}={request.spec.id}", "image"
    )
    assert seen["lock"] == "preflight"
    assert seen["identity"]["image_identity"] == BASE_IMAGE
    output = tmp_path / "reference"
    published = json.loads((output / cli.PILOT_DIRECTORY / cli.PILOT_FILE).read_text())
    assert published["kind"] == "reference-mutation-pilot"
    assert len(published["facts"]["repeats"]) == 3
    assert sorted(p.name for p in (output / cli.PILOT_DIRECTORY).iterdir()) == [
        cli.PILOT_FILE, "repeat-0", "repeat-1", "repeat-2",
    ]
    assert sorted(p.name for p in output.iterdir()) == [cli.PILOT_DIRECTORY]
    # Pilot only: nothing it wrote is a receipt, so freeze has none of it to qualify on.
    assert not (output / "staging").exists() and not list(output.rglob("*.receipt.json"))
    for path in output.rglob("*.json"):
        with pytest.raises(ValidationError):
            Receipt.model_validate_json(path.read_text())
    suite = Suite(
        tasks=(TASK,), attempts=3, seed=1,
        observer_contract_sha256=PINS["observer_contract_sha256"],
        reviewers=(ReviewerRule(
            coding_harness="claude", reviewer_harness="codex", reviewer_model_id="model",
            reviewer_effort="high", reviewer_cli_version="1", protocol_sha256="e" * 64,
        ),),
    )
    frozen = suite_qualification.freeze(
        suite, [], Policy(min_tasks=1, min_repositories=1, max_tasks_per_repository=1)
    )
    assert {(f.gate, f.reason) for f in frozen.blockers if f.gate in reference_mutation.GATES} == {
        ("mutation", "no receipt"), ("mutation_time", "no receipt"),
    }
    lines = result.output.splitlines()
    assert [line.split(":")[0] for line in lines[:3]] == ["repeat 0", "repeat 1", "repeat 2"]
    assert all("baselines 1.0 s green, 1.0 s green;" in line for line in lines[:3])
    assert all(line.endswith("; complete") for line in lines[:3])
    assert "M = 20, 4 lanes. No model calls." in result.output
    assert "Not a qualification receipt." in result.output


@pytest.mark.parametrize(
    "ended",
    [control_store.CleanupBlocked("cleanup of invocation x was not verified"),
     invocation_runtime.OperationDeadlineExceeded("operation reference did not return")],
    ids=["cleanup unverified", "deadline"],
)
def test_repeats_that_ended_before_their_cleanup_was_proven_are_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ended: BaseException
) -> None:
    """The handler finished every repeat, then its operation failed: the staged evidence
    stays unpublished, and the output says why, durably."""
    result, _, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", ended=ended
    )
    assert result.exit_code == 2 and str(ended) in result.output
    output = tmp_path / "reference"
    assert not (output / cli.PILOT_DIRECTORY).exists()
    assert (output / "staging" / "repeat-2" / "report.json").is_file()
    outcome = json.loads((output / cli.PILOT_OUTCOME).read_text())
    assert outcome == {
        "published": False, "infrastructure": True,
        "error": f"{type(ended).__name__}: {ended}",
    }


def test_a_publication_that_fails_records_why_and_its_output_names_the_cleanup_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk fills as the manifest is written: nothing is published, the staged repeats
    stay, and outcome.json says why. Running again there is refused with the exact cleanup."""
    write = cli._write_json

    def full(path: Path, value: object) -> None:
        if path.name == cli.PILOT_FILE:
            raise OSError(errno.ENOSPC, "No space left on device")
        write(path, value)

    monkeypatch.setattr(cli, "_write_json", full)
    result, _, _ = _command(tmp_path, monkeypatch, "--attempt-seconds", "600")
    assert result.exit_code == 2 and "No space left on device" in result.output
    output = tmp_path / "reference"
    assert not (output / cli.PILOT_DIRECTORY).exists()
    assert (output / "staging" / "repeat-2" / "report.json").is_file()
    outcome = json.loads((output / cli.PILOT_OUTCOME).read_text())
    assert outcome["published"] is False and outcome["infrastructure"] is True
    assert "No space left on device" in outcome["error"]
    again, requests, _ = _command(tmp_path / "again", monkeypatch, "--attempt-seconds", "600",
                                  "--output", str(output))
    assert again.exit_code == 2 and requests == []
    assert f"first remove it with `rm -r {output}`" in again.output


def test_only_the_evidence_the_pilot_is_meant_to_output_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose source copy could not be removed leaves it, receipt-named file included:
    it stays in staging, and the published pilot holds only the expected files."""
    result, _, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", stray="after/mutation.receipt.json"
    )
    assert result.exit_code == 0, result.output
    published = tmp_path / "reference" / cli.PILOT_DIRECTORY
    files = sorted(path.relative_to(published) for path in published.rglob("*") if path.is_file())
    assert Path(cli.PILOT_FILE) in files and Path("repeat-0/report.json") in files
    assert {path.name for path in files} == {
        cli.PILOT_FILE, "report.json", "execution.json", "stdout.txt",
    }
    assert not list(published.rglob("*.receipt.json")) and not list(published.rglob("after"))
    # Each run's stray stays behind, unpublished.
    runs = sum(path.name == "execution.json" for path in files)
    left = tmp_path / "reference" / "staging"
    assert len(list(left.rglob("mutation.receipt.json"))) == runs > 0


def test_the_pilot_sets_m_lanes_and_the_limit_and_sees_a_censored_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Twelve sites at M = 12 and one lane: one second per run, so the 13 s limit cuts every
    # repeat before its last mutants settle.
    result, [request], _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "13", "--sample-size", "12", "--lanes", "1",
        after=_lines(12),
    )
    assert result.exit_code == 0, result.output
    assert request.arguments["contract"] == {"headroom_percent": 150, "sample_size": 12, "lanes": 1}
    assert request.timeout == operations.reference_budget(13_000)
    first = result.output.splitlines()[0]
    assert first.startswith("repeat 0: 12 of ")
    assert "baselines 1.0 s green, 1.0 s green" in first and "first mutant 2.0 s" in first
    # The M = 10 prefix is reported apart from the whole sample.
    assert "first 10 settled 12.0 s" in first
    assert "all settled unsettled" in first and first.endswith("censored (attempt_deadline)")


def test_a_red_baseline_is_shown_and_its_repeat_is_incomplete_never_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unmutated suite fails: no mutant can be measured against it.
    result, _, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600",
        behaviour=lambda view: (0 if view.mutated else 1, False, ""),
    )
    assert result.exit_code == 0, result.output
    first = result.output.splitlines()[0]
    assert "red (suite status 1)" in first and "required work unsettled" in first
    assert first.endswith("; incomplete")


@pytest.mark.parametrize(
    ("option", "value"), [("--attempt-seconds", "0"), ("--lanes", "0"), ("--lanes", "5")]
)
def test_an_out_of_range_option_is_refused_before_the_host_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    """Above four lanes the contract's measurement capacity no longer holds. The usage error
    is checked as raised: rendered, its wording depends on the terminal's width and colour."""
    taken: list[object] = []
    monkeypatch.setattr(cli, "_execution", lambda *args: taken.append(args))
    result, requests, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", option, value, standalone=False
    )
    refused = result.exception
    assert isinstance(refused, typer.BadParameter) and refused.param is not None
    assert option in refused.param.opts
    assert taken == [] and requests == []


@pytest.mark.parametrize(
    ("engine", "message"),
    [({"endpoint": "unix:///fake.sock"}, "records no engine ID"),
     ({"id": "", "endpoint": "unix:///fake.sock"}, "records no engine ID"),
     ({"id": "daemon", "endpoint": "tcp://10.0.0.2:2375"}, "not a local Unix socket"),
     (None, "records no engine ID")],
)
def test_a_daemon_cleanup_could_not_check_is_refused_before_the_host_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: object, message: str
) -> None:
    """Cleanup asks the recorded engine by its ID at a Unix socket: without them a measured
    container could never be proven gone, and the host would stay fenced."""
    taken: list[object] = []
    monkeypatch.setattr(cli, "_execution", lambda *args: taken.append(args))
    result, requests, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", engine=engine
    )
    assert result.exit_code == 2 and message in result.output
    assert taken == [] and requests == []


def test_the_worker_gets_the_coverage_settings_with_the_defaults_they_were_validated_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coverage = {"command": ["suite"], "report_path": "cov.json", "tool_version": "7.6"}
    result, [request], _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600",
        profile_changes={"coverage": coverage},
    )
    assert result.exit_code == 0, result.output
    sent = request.arguments["config"]["coverage"]
    assert sent["timeout_seconds"] == 600 and sent["format"] == "lcov"
    # The baseline cap the repeats ran under is that default.
    published = json.loads(
        (tmp_path / "reference" / cli.PILOT_DIRECTORY / cli.PILOT_FILE).read_text()
    )
    assert {r["baseline_ms"] for r in published["facts"]["repeats"]} == {600_000}


def test_a_profile_whose_engine_was_replaced_is_refused_before_the_host_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[object] = []
    monkeypatch.setattr(cli, "_execution", lambda *args: taken.append(args))
    result, requests, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", answering="other"
    )
    assert result.exit_code == 2 and "another Docker engine, other, answers" in result.output
    assert taken == [] and requests == []


@pytest.mark.parametrize("case", ["output in before", "output in after", "missing before",
                                  "after is a file", "malformed coverage"])
def test_trees_an_output_inside_them_and_coverage_are_checked_before_the_host_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    taken: list[object] = []
    monkeypatch.setattr(cli, "_execution", lambda *args: taken.append(args))
    options: list[str] = []
    changes: dict[str, object] = {}
    if case.startswith("output in"):
        tree = tmp_path / case.removeprefix("output in ")
        options = ["--output", str(tree / "pilot")]
        message = "which it would change"
    elif case == "missing before":
        options = ["--before", str(tmp_path / "absent")]
        message = "is not a directory"
    elif case == "after is a file":
        (tmp_path / "file").write_text("")
        options = ["--after", str(tmp_path / "file")]
        message = "is not a directory"
    else:
        changes = {"coverage": {"report_path": "cov.json", "timeout_seconds": 100}}
        message = "for CoverageSettings"
    result, requests, _ = _command(
        tmp_path, monkeypatch, "--attempt-seconds", "600", *options, profile_changes=changes
    )
    assert result.exit_code == 2 and message in result.output
    assert taken == [] and requests == []
    assert not list(tmp_path.rglob("pilot"))


def test_an_existing_output_is_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "reference").mkdir()
    result, requests, _ = _command(tmp_path, monkeypatch, "--attempt-seconds", "600")
    assert result.exit_code == 2 and "already exists" in result.output
    assert requests == []
