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
    assert any(b.gate == "mutation" and "no qualification rule yet" in b.reason
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
    def row(patch: int, reward: int) -> dict[str, Any]:
        return {"status": "complete", "cleanup": {"status": "complete"}, "exception_info": None,
                "reconstruction": {"status": "complete"}, "patch_bytes": patch,
                "verifier_result": {"rewards": {"reward": reward}},
                "source_only": {"status": "complete", "passed": bool(reward)}}

    rows = {"oracle": row(120, 1), "nop": row(0, 0)}
    for path, value in changes.items():
        agent, key = path.split("__")
        rows[agent][key] = value
    return rows


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({}, "pass"),
        ({"oracle__patch_bytes": 0}, "fail"),
        ({"nop__patch_bytes": 40}, "fail"),
        ({"nop__verifier_result": {"rewards": {"reward": 1}}}, "fail"),
        ({"oracle__verifier_result": {"rewards": {"reward": 0}}}, "fail"),
        ({"oracle__source_only": {"status": "complete", "passed": False}}, "fail"),
        ({"nop__source_only": {"status": "complete", "passed": True}}, "fail"),
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
