"""Whether a candidate suite may freeze: every required gate of every task has passed.

Qualification makes no model call and cannot start paid work. This module imports only the
standard library, pydantic and `benchmark`, and runs only its own rules, so no launcher,
ledger or operation is reachable from it (tests/test_suite_qualification.py checks the
import boundary).

Each task has one receipt per gate. A receipt carries the raw facts a producer observed and
the task pins those facts were produced against. Freeze never trusts a stored verdict: a
rule computes each verdict from the facts, and a receipt whose pins do not match the task
is stale. Dispatch readiness (credentials, CLI pins, images, host state) is a separate,
later check (B3c); a frozen suite authorizes no paid call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pluginbench.benchmark import Count, Digest, Suite, SuiteTask


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# Task-level measures, as plan-v5-delta §8.2 item 8 fixes them: required measures gate a
# task; diagnostic ones are reported and never gate it. Pass, tokens, iterations, cost and
# time are proven by the correctness gate and by each harness's certification (B3c).
REQUIRED_MEASURES = (
    "changed_line_coverage",
    "mutation_catch_rate",
    "complexity_added",
    "production_lines_added",
    "test_lines_added",
    "duplicated_test_lines",
    "review_findings",
)
DIAGNOSTIC_MEASURES = ("dead_code", "decision_line_coverage", "test_change_kill_share")

# The pins each gate's evidence depends on, and so binds. No gate binds the mutation
# timeout: the measured mutation time sets it, and the mutation-time rule compares the
# recorded time with the task's current limit.
_CONTENT = ("content_sha256",)
_CODED = ("content_sha256", "coding_contract_sha256")
_MEASURED = ("content_sha256", "observer_profile_sha256", "observer_contract_sha256")
_ALL = (
    "content_sha256", "coding_contract_sha256", "observer_profile_sha256",
    "observer_contract_sha256",
)


class Gate(_Value):
    name: str
    binds: tuple[str, ...]
    required: bool = True


GATES = (
    Gate(name="clear_contract", binds=_CONTENT),
    Gate(name="correctness", binds=_CODED),
    Gate(name="visible_tests", binds=_CONTENT),
    Gate(name="changed_line_coverage", binds=_MEASURED),
    Gate(name="mutation", binds=_MEASURED),
    Gate(name="mutation_time", binds=_MEASURED),
    *(
        Gate(name=f"measure/{measure}", binds=_MEASURED)
        for measure in REQUIRED_MEASURES
        if measure not in {"changed_line_coverage", "mutation_catch_rate"}
    ),
    *(
        Gate(name=f"measure/{measure}", binds=_MEASURED, required=False)
        for measure in DIAGNOSTIC_MEASURES
    ),
    Gate(name="stability", binds=_ALL),
    Gate(name="resources", binds=_ALL),
    Gate(name="provenance_licence", binds=_CONTENT),
)
_GATES = {gate.name: gate for gate in GATES}


class Policy(_Value):
    """A suite release's population floors and per-repository cap."""

    min_tasks: Count
    min_repositories: Count
    max_tasks_per_repository: Count


class Receipt(_Value):
    """One gate's evidence for one task, as its producer recorded it."""

    task: Digest  # The task's content_sha256.
    gate: str
    pins: dict[str, Digest]
    producer: str = Field(min_length=1)
    facts: dict[str, Any]


class Verdict(_Value):
    status: Literal["pass", "fail", "unavailable"]
    reason: str | None = None


def _not_implemented(receipt: Receipt, task: SuiteTask) -> Verdict:
    return Verdict(status="unavailable", reason=f"{receipt.gate} has no qualification rule yet")


def _reward(verifier: object) -> bool | None:
    """A native verifier's binary reward, as harbor_backend.binary_native_reward reads it."""
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    if isinstance(rewards, dict) and ("reward" in rewards or len(rewards) == 1):
        reward = rewards["reward"] if "reward" in rewards else next(iter(rewards.values()))
        if type(reward) in {int, float} and reward in (0, 1):
            return bool(reward)
    return None


def _reference(agent: str, row: object, expected: bool) -> Verdict:
    """One reference agent's row, as `harbor_backend.run_reference_agents` records it, with
    the source-only grade of its reconstructed submission."""
    if not isinstance(row, dict):
        return Verdict(status="unavailable", reason=f"{agent}: no result")
    if row.get("status") != "complete":
        return Verdict(status="unavailable",
                       reason=f"{agent}: {row.get('reason') or 'trial did not complete'}")
    cleanup, reconstruction = row.get("cleanup"), row.get("reconstruction")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
        return Verdict(status="unavailable", reason=f"{agent}: trial cleanup incomplete")
    # The producer records an explicit null for a clean trial; an absent key proves nothing.
    if "exception_info" not in row or row["exception_info"] is not None:
        return Verdict(status="unavailable", reason=f"{agent}: trial failed or unrecorded")
    if not isinstance(reconstruction, dict) or reconstruction.get("status") != "complete":
        return Verdict(status="unavailable", reason=f"{agent}: graded patch not reconstructed")
    size = row.get("patch_bytes")
    if type(size) is not int or size < 0:
        return Verdict(status="unavailable", reason=f"{agent}: patch size not recorded")
    if (size > 0) is not expected:
        return Verdict(status="fail", reason=(
            "the reference solution's graded patch is empty" if expected
            else "the empty submission produced a graded patch"))
    reward = _reward(row.get("verifier_result"))
    if reward is None:
        return Verdict(status="unavailable", reason=f"{agent}: verifier reward is not binary")
    grade = row.get("source_only")
    if not isinstance(grade, dict) or grade.get("status") != "complete":
        return Verdict(status="unavailable", reason=f"{agent}: no source-only grade")
    # A grade with a fail reason (the verifier crashed, or the submission is unsupported)
    # says nothing about the tests: only the verifier's own pass or fail counts.
    passed = grade.get("passed")
    if type(passed) is not bool or "fail_reason" not in grade or grade["fail_reason"] is not None:
        return Verdict(status="unavailable", reason=f"{agent}: source-only grade is not a test result")
    # The grade is of this trial's reconstructed patch, not another submission's.
    if (grade.get("export_digest") != row.get("export_digest")
            or grade.get("native_patch_sha256") != reconstruction.get("patch_sha256")
            or not isinstance(row.get("export_digest"), str)
            or not isinstance(reconstruction.get("patch_sha256"), str)):
        return Verdict(status="unavailable", reason=f"{agent}: source-only grade is of another submission")
    for source, value in (("verifier reward", reward), ("source-only grade", passed)):
        if value is not expected:
            return Verdict(status="fail", reason=(
                f"{agent}: {source} is {'pass' if value else 'fail'}; expected "
                f"{'pass' if expected else 'fail'}"))
    return Verdict(status="pass")


def _correctness(receipt: Receipt, task: SuiteTask) -> Verdict:
    """The reference fix passes and an empty submission fails, by the hidden verifier and by
    the source-only grade of each reconstructed submission."""
    for agent, expected in (("oracle", True), ("nop", False)):
        verdict = _reference(agent, receipt.facts.get(agent), expected)
        if verdict.status != "pass":
            return verdict
    return Verdict(status="pass")


# Each gate's rule. B4 and B5 replace the stubs as their producers land. Private and fixed:
# a caller cannot hand freeze code to run.
Rule = Callable[[Receipt, SuiteTask], Verdict]
_RULES: dict[str, Rule] = {gate.name: _not_implemented for gate in GATES}
_RULES["correctness"] = _correctness


class Finding(_Value):
    task: str | None
    gate: str | None
    reason: str


class Freeze(_Value):
    """The frozen suite, or None with every reason it cannot freeze."""

    suite: Suite | None
    blockers: tuple[Finding, ...]
    # Diagnostic gates that did not pass: reported, never blocking.
    diagnostics: tuple[Finding, ...]
    tasks: int
    repositories: int


def _pins(suite: Suite, task: SuiteTask) -> dict[str, str]:
    return {
        "content_sha256": task.content_sha256,
        "coding_contract_sha256": task.coding_contract_sha256,
        "observer_profile_sha256": task.observer_profile_sha256,
        "observer_contract_sha256": suite.observer_contract_sha256,
    }


def freeze(suite: Suite, receipts: Iterable[Receipt], policy: Policy) -> Freeze:
    """Freeze `suite` only when every task's required gates pass and its population meets the
    policy. Nothing is dropped: a task that fails keeps the whole suite from freezing."""
    suite = Suite.model_validate(suite.model_dump())  # One validated population.
    blockers: list[Finding] = []
    diagnostics: list[Finding] = []
    tasks = {task.content_sha256: task for task in suite.tasks}
    found: dict[tuple[str, str], list[Receipt]] = {}
    for receipt in receipts:
        found.setdefault((receipt.task, receipt.gate), []).append(receipt)
    for task_id, gate_name in found:
        if task_id not in tasks:
            blockers.append(Finding(task=task_id, gate=gate_name,
                                    reason="receipt for a task not in the suite"))
        elif gate_name not in _GATES:
            blockers.append(Finding(task=task_id, gate=gate_name,
                                    reason="receipt for an unknown gate"))
    for task_id, task in tasks.items():
        pins = _pins(suite, task)
        for gate in GATES:
            reasons = _gate_reasons(found.get((task_id, gate.name), []), gate, task, pins)
            bucket = blockers if gate.required else diagnostics
            bucket.extend(Finding(task=task_id, gate=gate.name, reason=reason) for reason in reasons)
    clusters = Counter(task.repository_cluster for task in suite.tasks)
    if len(suite.tasks) < policy.min_tasks:
        blockers.append(Finding(task=None, gate=None, reason=(
            f"{len(suite.tasks)} tasks; the policy needs {policy.min_tasks}")))
    if len(clusters) < policy.min_repositories:
        blockers.append(Finding(task=None, gate=None, reason=(
            f"{len(clusters)} repositories; the policy needs {policy.min_repositories}")))
    for cluster, count in sorted(clusters.items()):
        if count > policy.max_tasks_per_repository:
            blockers.append(Finding(task=None, gate=None, reason=(
                f"{count} tasks from {cluster}; the policy allows {policy.max_tasks_per_repository}")))
    return Freeze(
        suite=None if blockers else suite,
        blockers=tuple(blockers),
        diagnostics=tuple(diagnostics),
        tasks=len(suite.tasks),
        repositories=len(clusters),
    )


def _gate_reasons(
    receipts: list[Receipt], gate: Gate, task: SuiteTask, pins: dict[str, str]
) -> list[str]:
    """Why one task's gate has not passed; empty when it has."""
    if not receipts:
        return ["no receipt"]
    if len(receipts) > 1:
        return [f"{len(receipts)} receipts; expected one"]
    [receipt] = receipts
    expected = {name: pins[name] for name in gate.binds}
    if receipt.pins != expected:
        problems = []
        if missing := sorted(set(expected) - set(receipt.pins)):
            problems.append(f"missing pins {', '.join(missing)}")
        if unexpected := sorted(set(receipt.pins) - set(expected)):
            problems.append(f"unexpected pins {', '.join(unexpected)}")
        if other := sorted(n for n in set(expected) & set(receipt.pins)
                           if receipt.pins[n] != expected[n]):
            problems.append(f"produced against other {', '.join(other)}")
        return ["stale: " + "; ".join(problems)]
    verdict = _RULES[gate.name](receipt, task)
    return [] if verdict.status == "pass" else [f"{verdict.status}: {verdict.reason}"]
