"""Reference mutation receipts: the producer, and its rules agreeing with the engine."""

from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from test_mutation import AFTER, BEFORE, FIRST, Fake, View, _config, _lines, _passing, _tree

from pluginbench import harbor_backend, mutation, reference_mutation, suite_qualification
from pluginbench.benchmark import SuiteTask
from pluginbench.reference_mutation import Contract, measure_reference
from pluginbench.suite_qualification import Receipt

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
