from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pluginbench import benchmark, evaluation, suite_qualification
from pluginbench.benchmark import ReviewerRule, Suite, SuiteTask
from pluginbench.suite_qualification import GATES, Policy, Receipt, Verdict, freeze

# The gates and pins plan-v5-delta §2, §8.2 item 8 and the B3 design v2 require, written
# out here rather than read from GATES, so a gate that disappears or binds less fails.
CONTENT = ["content_sha256"]
CODED = ["coding_contract_sha256", "content_sha256"]
MEASURED = ["content_sha256", "observer_contract_sha256", "observer_profile_sha256"]
ALL = sorted({*CODED, *MEASURED})
REQUIRED = {
    "clear_contract": CONTENT,
    "correctness": CODED,
    "visible_tests": CONTENT,
    "changed_line_coverage": MEASURED,
    "mutation": MEASURED,
    "mutation_time": MEASURED,
    "measure/complexity_added": MEASURED,
    "measure/production_lines_added": MEASURED,
    "measure/test_lines_added": MEASURED,
    "measure/duplicated_test_lines": MEASURED,
    "measure/review_findings": MEASURED,
    "stability": ALL,
    "resources": ALL,
    "provenance_licence": CONTENT,
}
DIAGNOSTIC = {
    "measure/dead_code": MEASURED,
    "measure/decision_line_coverage": MEASURED,
    "measure/test_change_kill_share": MEASURED,
}


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _task(name: str, cluster: str) -> SuiteTask:
    return SuiteTask(
        content_sha256=_digest(f"content {name}"),
        repository_cluster=cluster,
        coding_contract_sha256=_digest("coding"),
        observer_profile_sha256=_digest(f"profile {name}"),
        mutation_timeout_seconds=600,
    )


REVIEWER = ReviewerRule(
    coding_harness="claude", reviewer_harness="codex", reviewer_model_id="gpt-6.2-sol",
    reviewer_effort="high", reviewer_cli_version="0.167.0", protocol_sha256=_digest("protocol"),
)
SUITE = Suite(
    tasks=(_task("a", "repo-a"), _task("b", "repo-b"), _task("c", "repo-c")),
    attempts=3,
    seed=7,
    observer_contract_sha256=_digest("observers"),
    reviewers=(REVIEWER,),
)
POLICY = Policy(min_tasks=3, min_repositories=3, max_tasks_per_repository=2)


def _with_tasks(*tasks: SuiteTask) -> Suite:
    return Suite.model_validate({**SUITE.model_dump(), "tasks": [t.model_dump() for t in tasks]})


@pytest.fixture
def passing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every gate's rule passes; a test replaces the one it exercises."""
    rules = {name: (lambda receipt, task: Verdict(status="pass")) for name in {*REQUIRED, *DIAGNOSTIC}}
    monkeypatch.setattr(suite_qualification, "_RULES", rules)
    return rules


def _receipt(task: SuiteTask, gate: str, **facts: Any) -> Receipt:
    pins = {
        "content_sha256": task.content_sha256,
        "coding_contract_sha256": task.coding_contract_sha256,
        "observer_profile_sha256": task.observer_profile_sha256,
        "observer_contract_sha256": SUITE.observer_contract_sha256,
    }
    binds = {**REQUIRED, **DIAGNOSTIC}[gate]
    return Receipt(task=task.content_sha256, gate=gate, pins={name: pins[name] for name in binds},
                   producer="test", facts=facts)


def _all(suite: Suite = SUITE) -> list[Receipt]:
    return [_receipt(task, gate) for task in suite.tasks for gate in {*REQUIRED, *DIAGNOSTIC}]


def test_the_gate_table_is_the_required_and_diagnostic_contract() -> None:
    assert {g.name: sorted(g.binds) for g in GATES if g.required} == REQUIRED
    assert {g.name: sorted(g.binds) for g in GATES if not g.required} == DIAGNOSTIC


def test_a_suite_whose_every_gate_passes_freezes(passing: dict[str, Any]) -> None:
    result = freeze(SUITE, _all(), POLICY)

    assert result.suite == SUITE and result.blockers == () and result.diagnostics == ()
    assert (result.tasks, result.repositories) == (3, 3)


@pytest.mark.parametrize("gate", sorted({*REQUIRED, *DIAGNOSTIC}))
def test_one_failed_gate_on_one_task_refuses_freeze_unless_it_is_diagnostic(
    gate: str, passing: dict[str, Any]
) -> None:
    failed = SUITE.tasks[1]
    passing[gate] = lambda receipt, task: Verdict(
        status="pass" if task != failed else "fail", reason="probe contradicted it")

    result = freeze(SUITE, _all(), POLICY)

    finding = {"task": failed.content_sha256, "gate": gate, "reason": "fail: probe contradicted it"}
    # A diagnostic failure is reported and the suite still freezes; nothing else is dropped.
    assert (result.suite is None) is (gate in REQUIRED)
    found = result.blockers if gate in REQUIRED else result.diagnostics
    assert [item.model_dump() for item in found] == [finding]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing", "no receipt"),
        ("duplicate", "2 receipts; expected one"),
        ("stale", "stale: produced against other coding_contract_sha256"),
        ("extra-pin", "stale: unexpected pins observer_profile_sha256"),
        ("foreign", "receipt for a task not in the suite"),
        ("unknown-gate", "receipt for an unknown gate"),
    ],
)
def test_a_missing_duplicate_stale_or_foreign_receipt_refuses_freeze(
    change: str, reason: str, passing: dict[str, Any]
) -> None:
    receipts = _all()
    correctness = next(r for r in receipts if r.gate == "correctness")
    if change == "missing":
        receipts.remove(correctness)
    elif change == "duplicate":
        receipts.append(correctness)
    elif change in {"stale", "extra-pin"}:
        pins = ({**correctness.pins, "coding_contract_sha256": _digest("old")} if change == "stale"
                else {**correctness.pins, "observer_profile_sha256": _digest("profile a")})
        receipts[receipts.index(correctness)] = correctness.model_copy(update={"pins": pins})
    elif change == "foreign":
        receipts.append(_receipt(_task("z", "repo-z"), "correctness"))
    else:
        receipts.append(correctness.model_copy(update={"gate": "measure/tokens"}))

    result = freeze(SUITE, receipts, POLICY)

    assert result.suite is None and [b.reason for b in result.blockers] == [reason]


def test_a_mutation_timeout_set_from_measured_time_leaves_receipts_current(
    passing: dict[str, Any]
) -> None:
    retimed = _with_tasks(*(t.model_copy(update={"mutation_timeout_seconds": 900})
                            for t in SUITE.tasks))

    assert freeze(retimed, _all(), POLICY).suite == retimed


def test_a_profile_fix_makes_only_measurement_receipts_stale(passing: dict[str, Any]) -> None:
    fixed = SUITE.tasks[0].model_copy(update={"observer_profile_sha256": _digest("fixed")})

    stale = {b.gate for b in freeze(_with_tasks(fixed, *SUITE.tasks[1:]), _all(), POLICY).blockers}

    assert stale == {gate for gate, pins in REQUIRED.items() if "observer_profile_sha256" in pins}


def test_the_production_rules_refuse_every_suite_today() -> None:
    result = freeze(SUITE, _all(), POLICY)

    assert result.suite is None
    assert any(b.gate == "stability" and "no qualification rule yet" in b.reason
               for b in result.blockers)
    # The mutation rules exist now; empty facts prove nothing.
    assert any(b.gate == "mutation" and b.reason.startswith("unavailable: malformed")
               for b in result.blockers)


@pytest.mark.parametrize(
    ("clusters", "reason"),
    [
        (["repo-a", "repo-b", "repo-b"], "2 repositories; the policy needs 3"),
        (["repo-a", "repo-a", "repo-a"], "3 tasks from repo-a; the policy allows 2"),
    ],
)
def test_population_floors_and_the_repository_cap_refuse_freeze(
    clusters: list[str], reason: str, passing: dict[str, Any]
) -> None:
    suite = _with_tasks(*(t.model_copy(update={"repository_cluster": c})
                          for t, c in zip(SUITE.tasks, clusters, strict=True)))

    result = freeze(suite, _all(suite), POLICY)

    assert result.suite is None and reason in [b.reason for b in result.blockers]


def test_fewer_tasks_than_the_floor_refuses_freeze(passing: dict[str, Any]) -> None:
    policy = POLICY.model_copy(update={"min_tasks": 4})

    assert "3 tasks; the policy needs 4" in [b.reason for b in freeze(SUITE, _all(), policy).blockers]


def test_an_unvalidated_duplicate_task_is_refused() -> None:
    doubled = SUITE.model_copy(update={"tasks": (*SUITE.tasks, SUITE.tasks[0])})

    with pytest.raises(ValueError, match="lists one task twice"):
        freeze(doubled, _all(), POLICY)


def _reference(**changes: Any) -> dict[str, Any]:
    """Reference-agent rows as harbor_backend.run_reference_agents records them, with the
    source-only grade of each reconstructed submission."""
    def row(agent: str, patch: int, reward: int) -> dict[str, Any]:
        export, patch_sha = _digest(f"export {agent}"), _digest(f"patch {patch}")
        return {"status": "complete", "cleanup": {"status": "complete"}, "exception_info": None,
                "trial": f"{agent}-trial", "export_digest": export,
                "reconstruction": {"status": "complete", "patch_sha256": patch_sha},
                "patch_bytes": patch, "verifier_result": {"rewards": {"reward": reward}},
                "source_only": {"status": "complete", "passed": bool(reward), "fail_reason": None,
                                "trial": f"{agent}-trial", "export_digest": export, "native_patch_sha256": patch_sha}}

    rows = {"oracle": row("oracle", 120, 1), "nop": row("nop", 0, 0)}
    for path, value in changes.items():
        agent, key, *field = path.split("__")
        if value is _ABSENT:
            del rows[agent][key]
        elif field:
            rows[agent][key] = {**rows[agent][key], field[0]: value}
        else:
            rows[agent][key] = value
    return rows


_ABSENT = object()


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({}, "pass"),
        ({"oracle__patch_bytes": 0}, "fail"),
        ({"nop__patch_bytes": 40}, "fail"),
        ({"nop__verifier_result": {"rewards": {"reward": 1}}}, "fail"),
        ({"oracle__verifier_result": {"rewards": {"reward": 0}}}, "fail"),
        ({"oracle__source_only__passed": False}, "fail"),
        ({"nop__source_only__passed": True}, "fail"),
        # A crashed or unsupported verifier is no test result, even when it reads as a fail.
        ({"nop__source_only__fail_reason": "verifier_crashed_on_submission"}, "unavailable"),
        ({"nop__source_only__fail_reason": "unsupported_submission"}, "unavailable"),
        ({"oracle__source_only__native_patch_sha256": "f" * 64}, "unavailable"),
        ({"nop__source_only__export_digest": "f" * 64}, "unavailable"),
        # Another trial over the same source: equal digests, another trial's grade.
        ({"nop__source_only__trial": "earlier-nop-trial"}, "unavailable"),
        ({"nop__trial": _ABSENT}, "unavailable"),
        ({"oracle__exception_info": _ABSENT}, "unavailable"),
        ({"oracle__patch_bytes": "120"}, "unavailable"),
        ({"oracle__reconstruction": {"status": "skipped"}}, "unavailable"),
        ({"nop__source_only": None}, "unavailable"),
        ({"oracle__verifier_result": {"rewards": {"reward": 0.5}}}, "unavailable"),
        ({"nop__status": "failed"}, "unavailable"),
        ({"nop__cleanup": {"status": "failed"}}, "unavailable"),
        ({"oracle__exception_info": {"type": "Timeout"}}, "unavailable"),
        ({"oracle__status": "not_configured", "oracle__reason": "no solve.sh"}, "unavailable"),
    ],
)
def test_correctness_is_computed_from_reference_agent_facts(
    changes: dict[str, Any], status: str
) -> None:
    task = SUITE.tasks[0]
    rows = _reference(**changes)

    verdict = suite_qualification._RULES["correctness"](_receipt(task, "correctness", **rows), task)

    assert verdict.status == status
    # Never laxer than `pluginbench check`: rows it reports broken never pass here.
    checked = evaluation._reference_agent_rows(rows)
    if any(row["state"] != "ready" for row in checked.values()):
        assert verdict.status != "pass"


# Modules qualification may import, anywhere in its source or in benchmark's: nothing that
# could launch a coding or review agent, record a paid call or start an operation.
ALLOWED_IMPORTS = {
    "__future__", "collections", "collections.abc", "typing", "hashlib", "json", "pydantic",
    "pluginbench.benchmark",
}


@pytest.mark.parametrize("module", [suite_qualification, benchmark])
def test_qualification_imports_nothing_that_reaches_paid_work(module: Any) -> None:
    tree = ast.parse(Path(module.__file__).read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert imported <= ALLOWED_IMPORTS


def test_importing_qualification_loads_no_other_pluginbench_module() -> None:
    code = (
        "import json, sys; import pluginbench.suite_qualification as q; print(json.dumps("
        "[q.__file__, sorted(m for m in sys.modules if m.startswith('pluginbench'))]))"
    )
    path, loaded = json.loads(subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout)

    assert path == suite_qualification.__file__  # The same source this test run imports.
    assert loaded == ["pluginbench", "pluginbench.benchmark", "pluginbench.suite_qualification"]


# --- Reference mutation rules ------------------------------------------------------------
#
# Synthetic receipts as `reference_mutation.measure_reference` records them: B is one second
# (baselines of 1,000 ms), so a mutant's first run is capped at 3,000 ms and its runs share
# 6,000 ms from dispatch. Sites run one after another from 2,000 ms.


def _ran(started: int, elapsed: int, status: int | None = 0, timed_out: bool = False) -> dict[str, Any]:
    return {"started_ms": started, "dispatched": True, "elapsed_ms": elapsed,
            "exit_code": None if timed_out else 0, "timed_out": timed_out,
            "suite_status": None if timed_out else status}


# Each outcome's runs from dispatch at `t`, and how long the mutant takes.
_OUTCOMES: dict[str, tuple[Any, int]] = {
    "survived": (lambda t: [_ran(t, 1000)], 1000),
    "killed": (lambda t: [_ran(t, 1000, 1), _ran(t + 1000, 1000, 1)], 2000),
    "timeout_kill": (lambda t: [_ran(t, 3000, timed_out=True)], 3000),
    "flaky": (lambda t: [_ran(t, 1000, 1), _ran(t + 1000, 1000, 0)], 2000),
    "unconfirmed": (lambda t: [_ran(t, 1000, 1), _ran(t + 1000, 5000, timed_out=True)], 6000),
    "error": (lambda t: [_ran(t, 1000, None)], 1000),
}


def _repeat(run: str, outcomes: tuple[str, ...] = ("killed", "survived"), **changes: Any) -> dict[str, Any]:
    sites, t = [], 2000
    for index, state in enumerate(outcomes):
        runs, took = _OUTCOMES[state]
        sites.append({"site": index, "dispatched_ms": t, "settled_ms": t + took, "check": None,
                      "runs": runs(t)})
        t += took
    return {
        "run_id": run, "report_sha256": _digest(f"report {run}"),
        "before_digest": "sha256:before", "after_digest": "sha256:after",
        "image_id": "sha256:base", "resources": {"cpus": 2, "memory_mb": 2048},
        "timing_boundary": "executor_call", "candidate_count": 5,
        "selected": [f"src/a.py:{index + 1}:5:relational_boundary" for index in range(len(outcomes))],
        "baseline_ms": 100_000, "attempt_ms": None,
        "baseline": [_ran(0, 1000), _ran(1000, 1000)], "check_baseline": None,
        "sites": sites, "required_settled_ms": t, "infrastructure_failure": None,
        "unparsed_files": [], "unsupported_files": [],
        **changes,
    }


def _contract(**changes: Any) -> dict[str, Any]:
    return {"sample_size": 20, "lanes": 4, "resources": {"cpus": 2, "memory_mb": 2048},
            "baseline_factor": 4, "site_factor": 6, "first_run_factor": 3,
            "headroom_percent": 150, **changes}


def _mutation_facts(*repeats: dict[str, Any], **contract: Any) -> dict[str, Any]:
    return {"contract": _contract(**contract),
            "repeats": list(repeats) or [_repeat(f"run-{i}") for i in range(3)]}


def _rule(gate: str, facts: dict[str, Any], task: SuiteTask = SUITE.tasks[0]) -> Verdict:
    return suite_qualification._RULES[gate](_receipt(task, gate, **facts), task)


def _forged(facts: dict[str, Any]) -> dict[str, Any]:
    facts["repeats"][1]["sites"][0]["outcome"] = "killed"
    return facts


def _three(**changes: Any) -> list[dict[str, Any]]:
    return [_repeat(f"run-{i}", **changes) for i in range(3)]


@pytest.mark.parametrize(
    ("facts", "status", "reason"),
    [
        (_mutation_facts(), "pass", None),
        # 0/n with n > 0 is a measured zero, and passes.
        (_mutation_facts(*_three(outcomes=("survived", "survived"))), "pass", None),
        (_mutation_facts(*_three(outcomes=("timeout_kill", "survived"))), "pass", None),
        # A verdict written into the facts is refused, never read.
        (_forged(_mutation_facts()), "unavailable", "malformed"),
        ({**_mutation_facts(), "passed": True}, "unavailable", "malformed"),
        (_mutation_facts(_repeat("a"), _repeat("b")), "unavailable", "2 reference repeats"),
        (_mutation_facts(_repeat("a"), _repeat("a"), _repeat("b")), "unavailable", "reused"),
        (_mutation_facts(_repeat("a"), _repeat("b"), _repeat("c", after_digest="sha256:x")),
         "unavailable", "different inputs"),
        (_mutation_facts(_repeat("a"), _repeat("b"),
                         _repeat("c", resources={"cpus": 4, "memory_mb": 2048})),
         "unavailable", "other resources"),
        (_mutation_facts(_repeat("a"), _repeat("b"),
                         _repeat("c", infrastructure_failure="attempt_deadline")),
         "unavailable", "interrupted"),
        # No mutant runs after a baseline that is not green.
        (_mutation_facts(*_three(baseline=[_ran(0, 1000), _ran(1000, 1000, 1)], sites=[],
                                 required_settled_ms=None)),
         "fail", "baseline is red"),
        (_mutation_facts(*_three(baseline=[_ran(0, 1000), _ran(1000, 100_000, timed_out=True)],
                                 sites=[], required_settled_ms=None)),
         "unavailable", "no status"),
        (_mutation_facts(*_three(outcomes=())), "fail", "no mutation site"),
        (_mutation_facts(*_three(selected=[f"src/a.py:{i}:5:x" for i in range(3)])),
         "unavailable", "not accounted for"),
        (_mutation_facts(*_three(outcomes=("unconfirmed", "survived"))),
         "unavailable", "unconfirmed"),
        (_mutation_facts(*_three(outcomes=("error", "survived"))), "unavailable", "error"),
        (_mutation_facts(*_three(required_settled_ms=None)), "unavailable", "required phase"),
        (_mutation_facts(_repeat("a"), _repeat("b"), _repeat("c", outcomes=("survived", "survived"))),
         "fail", "different outcomes"),
        (_mutation_facts(_repeat("a"), _repeat("b"), _repeat(
            "c", selected=["src/a.py:9:5:x", "src/a.py:2:5:relational_boundary"])),
         "fail", "selected different sites"),
        (_mutation_facts(*_three(outcomes=("flaky", "flaky"))), "fail", "no valid mutant"),
    ],
)
def test_the_mutation_rule_recomputes_outcomes_from_raw_runs(
    facts: dict[str, Any], status: str, reason: str | None
) -> None:
    verdict = _rule("mutation", facts)
    assert verdict.status == status
    assert reason is None or reason in str(verdict.reason)


def test_a_run_whose_start_contradicts_its_clocks_proves_nothing() -> None:
    facts = _mutation_facts()
    # The confirmation claims it never started, with 5,000 ms of its site's 6,000 left.
    facts["repeats"][0]["sites"][0]["runs"][1] = {"started_ms": 3000, "dispatched": False}
    assert _rule("mutation", facts).status == "unavailable"
    # A confirmation that started with no time left cannot have run.
    facts = _mutation_facts()
    facts["repeats"][0]["sites"][0]["runs"][1]["started_ms"] = 8000
    assert _rule("mutation", facts).status == "unavailable"


def test_a_tsc_mutant_needs_a_tsc_baseline_that_ran() -> None:
    check = {**_ran(2000, 0), "kind": "tsc", "diagnostics": 0, "new_diagnostics": 0}
    repeats = _three()
    for repeat in repeats:
        repeat["sites"][1]["check"] = {**check, "started_ms": repeat["sites"][1]["dispatched_ms"]}
    assert _rule("mutation", _mutation_facts(*repeats)).status == "unavailable"
    for repeat in repeats:
        repeat["check_baseline"] = {**_ran(2000, 0), "kind": "tsc", "diagnostics": 0}
    assert _rule("mutation", _mutation_facts(*repeats)).status == "pass"
    # A mutant that adds a tsc error is invalid; the other still makes the population valid.
    for repeat in repeats:
        repeat["sites"][1]["check"]["new_diagnostics"] = 1
        repeat["sites"][1]["check"]["diagnostics"] = 1
        repeat["sites"][1]["check"]["exit_code"] = 2
        repeat["sites"][1]["runs"] = []
    assert _rule("mutation", _mutation_facts(*repeats)).status == "pass"


def _timed(*settled: int, sample_size: int = 2, headroom: int = 200) -> dict[str, Any]:
    """Three repeats whose last site settles at each `settled`, at least 5,000 ms, when its
    run has ended; with M = n the projection is the measured time."""
    repeats = []
    for index, end in enumerate(settled):
        repeat = _repeat(f"run-{index}")
        repeat["sites"][1]["settled_ms"] = end
        repeat["required_settled_ms"] = end
        repeats.append(repeat)
    return _mutation_facts(*repeats, sample_size=sample_size, headroom_percent=headroom)


def _limited(seconds: int) -> SuiteTask:
    return SUITE.tasks[0].model_copy(update={"mutation_timeout_seconds": seconds})


@pytest.mark.parametrize(
    ("facts", "seconds", "status"),
    [
        # 5,000 ms x 200% = 10 s: equality passes, one millisecond more fails.
        (_timed(5000, 5000, 5000), 10, "pass"),
        (_timed(5000, 5001, 5000), 10, "fail"),
        (_timed(5000, 5000, 5000), 9, "fail"),
        # Two of M = 20 sites in four lanes: one wave measured, five projected.
        (_timed(5000, 5000, 5000, sample_size=20, headroom=100), 17, "pass"),
        (_timed(5000, 5000, 5000, sample_size=20, headroom=100), 16, "fail"),
    ],
)
def test_mutation_time_projects_the_full_sample_and_fits_it_with_headroom(
    facts: dict[str, Any], seconds: int, status: str
) -> None:
    assert _rule("mutation_time", facts, _limited(seconds)).status == status


def test_the_projection_scales_waves_of_lanes() -> None:
    project = suite_qualification.projected_ms
    assert project(setup_ms=2000, settled_ms=5000, sites=20, sample_size=20, lanes=4) == 5000
    assert project(setup_ms=2000, settled_ms=5000, sites=2, sample_size=20, lanes=4) == 17000
    # Ten sites are three waves of four; twenty are five.
    assert project(setup_ms=2000, settled_ms=5000, sites=10, sample_size=20, lanes=4) == 7000
    # Rounded up to the next whole millisecond.
    assert project(setup_ms=0, settled_ms=1001, sites=12, sample_size=20, lanes=4) == 1669


@pytest.mark.parametrize(
    "change",
    [
        lambda facts: facts["repeats"][2].update(infrastructure_failure="executor"),
        lambda facts: facts["repeats"][1].update(required_settled_ms=None),
        lambda facts: facts["repeats"][0]["sites"][1].update(settled_ms=None),
        lambda facts: facts["repeats"][0].update(sites=facts["repeats"][0]["sites"][:1]),
        lambda facts: facts["repeats"].pop(),
    ],
    ids=["censored", "required-open", "unsettled", "site-missing", "two-repeats"],
)
def test_a_missing_or_censored_timing_is_unavailable(change: Any) -> None:
    facts = _timed(5000, 5000, 5000)
    change(facts)
    assert _rule("mutation_time", facts, _limited(1000)).status == "unavailable"


def test_a_lowered_limit_changes_only_the_time_verdict_and_a_new_profile_stales_both() -> None:
    task = SUITE.tasks[0]
    facts = _timed(5000, 5000, 5000)
    receipts = [_receipt(task, gate, **facts) for gate in ("mutation", "mutation_time")]
    pins = suite_qualification._pins(SUITE, task)

    def reasons(subject: SuiteTask) -> dict[str, list[str]]:
        current = suite_qualification._pins(SUITE, subject)
        return {r.gate: suite_qualification._gate_reasons(
            [r], suite_qualification._GATES[r.gate], subject, current) for r in receipts}

    assert reasons(_limited(10)) == {"mutation": [], "mutation_time": []}
    lowered = reasons(_limited(9))
    assert lowered["mutation"] == [] and lowered["mutation_time"][0].startswith("fail: projected")
    assert suite_qualification._pins(SUITE, _limited(9)) == pins
    profile = task.model_copy(update={"observer_profile_sha256": _digest("other profile")})
    assert all(found[0].startswith("stale: produced against other observer_profile_sha256")
               for found in reasons(profile).values())


def _both(facts: dict[str, Any]) -> tuple[str, str]:
    task = _limited(1000)
    return (_rule("mutation", facts, task).status, _rule("mutation_time", facts, task).status)


def _overlapping() -> list[dict[str, Any]]:
    """Two sites with one start: valid in two lanes, impossible in one."""
    sites: list[dict[str, Any]] = _repeat("a")["sites"]
    sites[1] = {**sites[1], "dispatched_ms": 2000, "settled_ms": 3000,
                "runs": [_ran(2000, 1000)]}
    return sites


def _tampered(change: Any) -> dict[str, Any]:
    facts = _mutation_facts()
    for repeat in facts["repeats"]:
        change(repeat)
    return facts


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        # A 1 ms attempt limit that 1,000 ms baselines and every site would have overrun.
        (_tampered(lambda r: r.update(attempt_ms=1)), "contradicts the time it had left"),
        # The last site's run is cut by a 4,999 ms limit, yet the site is marked settled.
        (_tampered(lambda r: r.update(attempt_ms=4999)), "settlement differs from its runs"),
        # Its own 3,000 ms cap bounds the last site's first run, which returns 500 ms late at
        # 7,500 ms: a timeout kill, and past the 7,200 ms attempt limit, yet nothing was cut.
        (_tampered(lambda r: (r["sites"][1]["runs"][0].update(elapsed_ms=3500),
                              r["sites"][1].update(settled_ms=7500),
                              r.update(required_settled_ms=7500, attempt_ms=7200))),
         "overran its attempt limit"),
        # A red baseline its own cap bounded, returning at 2,000 ms past a 1,999 ms limit.
        (_mutation_facts(*_three(baseline=[_ran(0, 1000), _ran(1000, 1000, 1)], sites=[],
                                 required_settled_ms=None, attempt_ms=1999)),
         "overran its attempt limit"),
        (_tampered(lambda r: r["sites"][0]["runs"][0].update(exit_code=None)),
         "suite status with no exit code"),
        (_tampered(lambda r: r["sites"][0]["runs"][1].update(exit_code=None, timed_out=True)),
         "timed out records an exit"),
        (_tampered(lambda r: [site.update(settled_ms=0) for site in r["sites"]]),
         "settled before its runs ended"),
        (_tampered(lambda r: r["sites"][0].update(dispatched_ms=1500)), "before the baselines"),
        (_tampered(lambda r: r["baseline"][1].update(started_ms=500)), "baseline runs overlap"),
        (_tampered(lambda r: r["sites"][0]["runs"][1].update(started_ms=2500)), "out of order"),
        (_tampered(lambda r: r.update(required_settled_ms=4000)), "before its sites settled"),
        # A failed first run with no confirmation has no outcome yet, so it cannot be settled.
        (_tampered(lambda r: r["sites"][0].update(runs=r["sites"][0]["runs"][:1])),
         "settlement differs from its runs"),
        # With one lane, site 1 runs inside site 0's span.
        (_mutation_facts(*_three(sites=_overlapping()), lanes=1), "lanes hold"),
        (_tampered(lambda r: r.update(candidate_count=1)), "more sites than it found"),
        (_tampered(lambda r: r.update(candidate_count=0)), "more sites than it found"),
        (_tampered(lambda r: r.update(timing_boundary="exec")), "timed exec, not executor_call"),
        # Discovery that left a changed file unparsed, or in a language with no finder.
        (_tampered(lambda r: r.update(unparsed_files=["src/broken.py"])), "incomplete"),
        (_tampered(lambda r: r.update(unsupported_files=["src/lib.rs"])), "incomplete"),
    ],
)
def test_both_reference_rules_refuse_clocks_or_exits_that_contradict_each_other(
    facts: dict[str, Any], reason: str
) -> None:
    for gate in ("mutation", "mutation_time"):
        verdict = _rule(gate, facts, _limited(1000))
        assert verdict.status == "unavailable" and reason in str(verdict.reason), gate


def test_sites_that_overlap_within_the_lanes_pass() -> None:
    assert _rule("mutation", _mutation_facts(*_three(sites=_overlapping()), lanes=2)).status == (
        "pass"
    )


def test_an_attempt_limit_the_repeat_kept_to_passes_both_rules() -> None:
    # The required phase ends at 5,000 ms: a limit of exactly 5,000 ms holds it.
    assert _both(_tampered(lambda r: r.update(attempt_ms=5000))) == ("pass", "pass")


def test_a_late_exit_is_cut_by_the_limit_it_overran() -> None:
    # A green first run 1 ms past its 3,000 ms cap is a timeout kill, not a survivor; one
    # exactly at the cap is on time.
    late = _tampered(lambda r: r["sites"][1]["runs"][0].update(elapsed_ms=3001))
    for repeat in late["repeats"]:
        repeat["sites"][1]["settled_ms"] = repeat["required_settled_ms"] = 7001
    assert _rule("mutation", late).status == "pass"
    outcomes = suite_qualification._repeat_outcomes(
        suite_qualification._MutationFacts.model_validate(late).repeats[0],
        suite_qualification._MutationContract.model_validate(_contract()),
    )
    assert outcomes == ("killed", "timeout_kill")
    on_time = _tampered(lambda r: r["sites"][1]["runs"][0].update(elapsed_ms=3000))
    for repeat in on_time["repeats"]:
        repeat["sites"][1]["settled_ms"] = repeat["required_settled_ms"] = 7000
    assert suite_qualification._repeat_outcomes(
        suite_qualification._MutationFacts.model_validate(on_time).repeats[0],
        suite_qualification._MutationContract.model_validate(_contract()),
    ) == ("killed", "survived")
    # A late green baseline under the attempt limit, with no interruption recorded, is refused.
    baseline = _tampered(lambda r: r.update(attempt_ms=100_000))
    for repeat in baseline["repeats"]:
        repeat["baseline"][0]["elapsed_ms"] = 1000
        repeat["attempt_ms"] = 999
    assert _both(baseline) == ("unavailable", "unavailable")


def _faster(facts: dict[str, Any]) -> dict[str, Any]:
    """The same repeats, run IDs and report digests, with the last site's run 1 ms shorter."""
    faster: dict[str, Any] = json.loads(json.dumps(facts))
    for repeat in faster["repeats"]:
        repeat["sites"][1]["runs"][0]["elapsed_ms"] -= 1
    return faster


@pytest.mark.parametrize(
    "time_facts",
    [
        lambda batch: _mutation_facts(*[_repeat(f"other-{i}") for i in range(3)]),
        _faster,
        lambda batch: {**batch, "contract": _contract(headroom_percent=100)},
    ],
    ids=["other-repeats", "same-ids-faster-calls", "other-contract"],
)
def test_freeze_refuses_time_and_outcome_verdicts_from_different_facts(
    passing: dict[str, Any], time_facts: Any
) -> None:
    task = SUITE.tasks[0]
    batch = _mutation_facts()
    receipts = [r for r in _all() if not (r.task == task.content_sha256 and r.gate in {
        "mutation", "mutation_time"})]
    same = [_receipt(task, "mutation", **batch), _receipt(task, "mutation_time", **batch)]
    assert freeze(SUITE, receipts + same, POLICY).suite == SUITE
    other = time_facts(batch)
    # The time facts pass the real rule on their own (`passing` stubs `_RULES`); only the
    # pairing refuses them.
    alone = _receipt(task, "mutation_time", **other)
    assert suite_qualification._mutation_time(alone, _limited(1000)).status == "pass"
    mixed = [_receipt(task, "mutation", **batch), _receipt(task, "mutation_time", **other)]
    result = freeze(SUITE, receipts + mixed, POLICY)
    assert result.suite is None
    assert [(b.gate, b.reason) for b in result.blockers] == [
        ("mutation_time", "unavailable: its facts differ from its mutation receipt's")
    ]
