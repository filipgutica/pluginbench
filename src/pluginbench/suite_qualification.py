"""Whether a candidate suite may freeze: every required gate of every task has passed.

Qualification makes no model call and cannot start paid work. This module imports only the
standard library, pydantic and `benchmark`, so no launcher, ledger or operation is
reachable from it (tests/test_suite_qualification.py checks the import boundary).

Each task has one receipt per gate. A receipt carries the raw facts a producer observed and
the task pins those facts were produced against. Freeze never trusts a stored verdict: a
rule computes each verdict from the facts, and a receipt whose pins no longer match the
task is stale. Dispatch readiness (credentials, CLI pins, images, host state) is a
separate, later check (B3c); a frozen suite authorizes no paid call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench.benchmark import Count, Digest, Suite, SuiteTask


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

# Task-level measures a suite can require or keep as diagnostics (plan-v5-delta §8.2 item 8).
# Pass, tokens, iterations, cost and time are proven by the correctness gate and by each
# harness's certification (B3c), not per task.
MEASURES = (
    "changed_line_coverage",
    "mutation_catch_rate",
    "complexity_added",
    "production_lines_added",
    "test_lines_added",
    "duplicated_test_lines",
    "review_findings",
    "dead_code",
    "decision_line_coverage",
    "test_change_kill_share",
)

# The pins each gate's evidence depends on, and so binds. No gate binds the mutation
# timeout: the measured mutation time sets it.
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
    # The measure whose requiredness decides the gate's; None means always required.
    measure: str | None = None


GATES = (
    Gate(name="clear_contract", binds=_CONTENT),
    Gate(name="correctness", binds=_CODED),
    Gate(name="visible_tests", binds=_CONTENT),
    Gate(name="changed_line_coverage", binds=_MEASURED, measure="changed_line_coverage"),
    Gate(name="mutation", binds=_MEASURED, measure="mutation_catch_rate"),
    Gate(name="mutation_time", binds=_MEASURED, measure="mutation_catch_rate"),
    *(
        Gate(name=f"measure/{measure}", binds=_MEASURED, measure=measure)
        for measure in MEASURES
        if measure not in {"changed_line_coverage", "mutation_catch_rate"}
    ),
    Gate(name="stability", binds=_ALL),
    Gate(name="resources", binds=_ALL),
    Gate(name="provenance_licence", binds=_CONTENT),
)
_GATES = {gate.name: gate for gate in GATES}


class Policy(_Value):
    """What a suite release requires: which measures, and the population floors and cap."""

    required_measures: tuple[str, ...]
    diagnostic_measures: tuple[str, ...]
    min_tasks: Count
    min_repositories: Count
    max_tasks_per_repository: Count

    @model_validator(mode="after")
    def _known(self) -> Policy:
        listed = [*self.required_measures, *self.diagnostic_measures]
        if unknown := sorted(set(listed) - set(MEASURES)):
            raise ValueError(f"unknown measures: {', '.join(unknown)}")
        if len(set(listed)) != len(listed):
            raise ValueError("a measure is listed twice, or as both required and diagnostic")
        return self

    def role(self, gate: Gate) -> Literal["required", "diagnostic"] | None:
        """Whether freeze needs the gate to pass, only reports it, or skips it."""
        if gate.measure is None or gate.measure in self.required_measures:
            return "required"
        return "diagnostic" if gate.measure in self.diagnostic_measures else None


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


Rule = Callable[[Receipt, SuiteTask], Verdict]


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


def _correctness(receipt: Receipt, task: SuiteTask) -> Verdict:
    """The reference fix passes and an empty submission fails, from the oracle and nop rows
    `harbor_backend.run_reference_agents` records (the same rule `pluginbench check` uses)."""
    for agent, expected in (("oracle", True), ("nop", False)):
        row = receipt.facts.get(agent)
        if not isinstance(row, dict):
            return Verdict(status="unavailable", reason=f"no {agent} result")
        if row.get("status") == "not_configured":
            return Verdict(status="unavailable", reason=f"{agent}: {row.get('reason')}")
        if row.get("status") != "complete":
            return Verdict(status="unavailable",
                           reason=f"{agent}: {row.get('reason') or 'trial did not complete'}")
        cleanup = row.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("status") != "complete":
            return Verdict(status="unavailable", reason=f"{agent}: trial cleanup incomplete")
        if row.get("exception_info") is not None:
            return Verdict(status="unavailable", reason=f"{agent}: trial failed")
        if expected and not row.get("patch_bytes"):
            return Verdict(status="fail", reason="the reference solution's graded patch is empty")
        reward = _reward(row.get("verifier_result"))
        if reward is None:
            return Verdict(status="unavailable", reason=f"{agent}: reward is not binary")
        if reward is not expected:
            return Verdict(status="fail",
                           reason=f"{agent}: verifier reward is {int(reward)}; expected {int(expected)}")
    return Verdict(status="pass")


# Each gate's rule. B4 and B5 replace the stubs as their producers land.
RULES: Mapping[str, Rule] = {gate.name: _not_implemented for gate in GATES} | {
    "correctness": _correctness,
}


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


def freeze(
    suite: Suite, receipts: Iterable[Receipt], policy: Policy,
    rules: Mapping[str, Rule] = RULES,
) -> Freeze:
    """Freeze `suite` only when every task's required gates pass and its population meets the
    policy. Nothing is dropped: a task that fails keeps the whole suite from freezing."""
    blockers: list[Finding] = []
    diagnostics: list[Finding] = []
    tasks = {task.content_sha256: task for task in suite.tasks}
    found: dict[tuple[str, str], list[Receipt]] = {}
    for receipt in receipts:
        found.setdefault((receipt.task, receipt.gate), []).append(receipt)
    for task_id, gate_name in found:
        if task_id not in tasks:
            blockers.append(Finding(task=task_id, gate=gate_name, reason="receipt for a task not in the suite"))
        elif gate_name not in _GATES:
            blockers.append(Finding(task=task_id, gate=gate_name, reason="receipt for an unknown gate"))
    for task_id, task in tasks.items():
        pins = _pins(suite, task)
        for gate in GATES:
            role = policy.role(gate)
            if role is None:
                continue
            reasons = _gate_reasons(found.get((task_id, gate.name), []), gate, task, pins, rules)
            bucket = blockers if role == "required" else diagnostics
            bucket.extend(Finding(task=task_id, gate=gate.name, reason=reason) for reason in reasons)
    clusters = Counter(task.repository_cluster for task in suite.tasks)
    if len(suite.tasks) < policy.min_tasks:
        blockers.append(Finding(task=None, gate=None,
                                reason=f"{len(suite.tasks)} tasks; the policy needs {policy.min_tasks}"))
    if len(clusters) < policy.min_repositories:
        blockers.append(Finding(task=None, gate=None,
                                reason=f"{len(clusters)} repositories; the policy needs {policy.min_repositories}"))
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
    receipts: list[Receipt], gate: Gate, task: SuiteTask, pins: dict[str, str],
    rules: Mapping[str, Rule],
) -> list[str]:
    """Why one task's gate has not passed; empty when it has."""
    if not receipts:
        return ["no receipt"]
    if len(receipts) > 1:
        return [f"{len(receipts)} receipts; expected one"]
    [receipt] = receipts
    expected = {name: pins[name] for name in gate.binds}
    if receipt.pins != expected:
        stale = sorted(name for name in {*expected, *receipt.pins}
                       if receipt.pins.get(name) != expected.get(name))
        return [f"stale: produced against other {', '.join(stale)}"]
    verdict = rules[gate.name](receipt, task)
    return [] if verdict.status == "pass" else [f"{verdict.status}: {verdict.reason}"]
