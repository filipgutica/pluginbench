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
from collections.abc import Callable, Iterable, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # The grade is of this trial's reconstructed patch. Two trials over the same source share
    # both digests, so the trial ID tells them apart.
    bindings = (("trial", row.get("trial")), ("export_digest", row.get("export_digest")),
                ("native_patch_sha256", reconstruction.get("patch_sha256")))
    if any(not isinstance(value, str) or not value or grade.get(key) != value
           for key, value in bindings):
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


# --- Reference mutation -------------------------------------------------------------------
#
# The `mutation` and `mutation_time` receipts carry the same facts: the mutation part of the
# common observer contract and three independent reference repeats, as
# `reference_mutation.measure_reference` records them from validated mutation reports. The
# rules re-derive every clock, outcome, count and the site partition from those raw readings;
# they never read a verdict. The producer, not the rule, checks the sample against the
# candidates, since sampling lives outside this module's import boundary.

REFERENCE_REPEATS = 3
_Whole = Annotated[int, Field(strict=True, ge=0)]


class _Call(_Value):
    """One run: when it started, and, if it started, how it ended."""

    started_ms: _Whole
    dispatched: bool
    elapsed_ms: _Whole | None = None
    exit_code: int | None = None
    timed_out: bool | None = None
    suite_status: int | None = None

    @model_validator(mode="after")
    def _ended(self) -> _Call:
        if self.dispatched != (self.elapsed_ms is not None and self.timed_out is not None):
            raise ValueError("a run has a result exactly when it started")
        return self


class _Check(_Call):
    """A mutant's build or type check. `diagnostics` counts tsc errors in its output and
    `new_diagnostics` those the unmutated tree does not have."""

    kind: Literal["go", "tsc"]
    diagnostics: _Whole | None = None
    new_diagnostics: _Whole | None = None


class _Site(_Value):
    site: _Whole
    dispatched_ms: _Whole
    settled_ms: _Whole | None
    check: _Check | None
    runs: tuple[_Call, ...]


class _Resources(_Value):
    cpus: Count
    memory_mb: Count


class _MutationContract(_Value):
    """The mutation part of the common observer contract, as the producer read it.

    Binding these values to `observer_contract_sha256` is the manifest reader's job (B3b).
    """

    sample_size: Count
    lanes: Count
    resources: _Resources
    baseline_factor: Count
    site_factor: Count
    first_run_factor: Count
    # The mutation time limit must hold the projected reference time times this percentage.
    headroom_percent: Count


class _Repeat(_Value):
    run_id: str = Field(min_length=1)
    report_sha256: Digest
    before_digest: str
    after_digest: str
    image_id: str = Field(min_length=1)
    resources: _Resources
    timing_boundary: str
    candidate_count: _Whole
    selected: tuple[str, ...]
    baseline_ms: Count
    attempt_ms: Count | None
    baseline: tuple[_Call, ...]
    check_baseline: _Check | None
    sites: tuple[_Site, ...]
    required_settled_ms: _Whole | None
    infrastructure_failure: Literal["executor", "attempt_deadline"] | None


class _MutationFacts(_Value):
    contract: _MutationContract
    repeats: tuple[_Repeat, ...]


def _limit(
    started: int, cap: int | None, deadline: int | None, attempt: int | None
) -> tuple[str, int]:
    """The clock that bounds a call, and the time it leaves; ties go to the attempt, then the
    site, then the command (mutation.call_limit)."""
    limits = [(name, value) for name, value in (
        ("attempt", None if attempt is None else attempt - started),
        ("site", None if deadline is None else deadline - started),
        ("command", cap),
    ) if value is not None]
    least = min(value for _, value in limits)
    return next(item for item in limits if item[1] == least)


# A run's end: the clock that stopped it or left it no time (None when it ran to an exit), and
# its suite status.
_End = tuple[str | None, int | None]


def _end(call: _Call, cap: int | None, deadline: int | None, attempt: int | None) -> _End:
    bound, left = _limit(call.started_ms, cap, deadline, attempt)
    if call.dispatched != (left > 0):
        raise ValueError("a run's start contradicts the time it had left")
    return (bound if not call.dispatched or call.timed_out else None), call.suite_status


def _settle(ends: Sequence[_End]) -> str | None:
    """A mutant's outcome from its runs' ends; None needs another run (mutation.site_outcome)."""
    if not ends:
        return None
    (cut, status) = ends[0]
    if cut is not None:
        return {"command": "timeout_kill", "site": "unconfirmed"}.get(cut, "interrupted")
    if status is None:
        return "error"
    if status == 0:
        return "survived"
    if len(ends) == 1:
        return None
    (cut, status) = ends[1]
    if cut is not None:
        return "interrupted" if cut == "attempt" else "unconfirmed"
    if status is None:
        return "error"
    return "flaky" if status == 0 else "killed"


def _ran(check: _Check) -> bool:
    """Whether a tsc baseline ran to a known result (mutation._tsc_ran)."""
    code = check.exit_code
    return (check.dispatched and not check.timed_out and code is not None
            and code not in (126, 127) and (code == 0 or bool(check.diagnostics)))


def _checked(check: _Check, end: _End) -> str:
    """valid, invalid, error, unconfirmed or interrupted for a check (mutation._check_state_v2)."""
    if end[0] is not None:
        return "interrupted" if end[0] == "attempt" else "unconfirmed"
    code = check.exit_code
    if check.timed_out or code is None or code in (126, 127):
        return "error"
    if check.kind == "go":
        return "valid" if code == 0 else "invalid"
    if check.diagnostics is None or check.new_diagnostics is None:
        raise ValueError("a tsc check records no diagnostic counts")
    if code and not check.diagnostics:
        return "error"
    return "invalid" if check.new_diagnostics else "valid"


def _site_outcome(site: _Site, slowest: int, attempt: int | None, contract: _MutationContract) -> str | None:
    """A reference mutant's outcome, from its raw runs and clocks."""
    deadline = site.dispatched_ms + contract.site_factor * slowest
    state: str | None
    if site.check is not None:
        state = _checked(site.check, _end(site.check, None, deadline, attempt))
        if state != "valid":
            if site.runs:
                raise ValueError("a mutant ran after its check settled it")
            return state
    ends = [
        _end(call, contract.first_run_factor * slowest if position == 0 else None, deadline,
             attempt)
        for position, call in enumerate(site.runs)
    ]
    state = _settle(ends)
    if any(_settle(ends[:count]) is not None for count in range(1, len(ends))):
        raise ValueError("a mutant's runs go on after its outcome is known")
    return state


def _facts(receipt: Receipt) -> _MutationFacts | Verdict:
    """The receipt's facts, or why they prove nothing: malformed, forged, reused or mixed."""
    try:
        facts = _MutationFacts.model_validate(receipt.facts)
    except ValueError as error:
        return Verdict(status="unavailable", reason=f"malformed reference mutation facts: {error}")
    repeats = facts.repeats
    if len(repeats) != REFERENCE_REPEATS:
        return Verdict(status="unavailable", reason=(
            f"{len(repeats)} reference repeats; expected {REFERENCE_REPEATS}"))
    # Independent repeats can write identical reports, so only their run IDs tell them apart.
    if len({r.run_id for r in repeats}) != len(repeats):
        return Verdict(status="unavailable", reason="a reference repeat is reused")
    if len({(r.before_digest, r.after_digest, r.image_id) for r in repeats}) != 1:
        return Verdict(status="unavailable", reason="reference repeats measured different inputs")
    if any(r.resources != facts.contract.resources for r in repeats):
        return Verdict(status="unavailable", reason="a reference repeat ran with other resources")
    return facts


def _repeat_outcomes(repeat: _Repeat, contract: _MutationContract) -> tuple[str, ...] | Verdict:
    """Every selected site's outcome in one complete repeat, or why the repeat proves nothing
    (unavailable) or disproves the reference (fail)."""
    if repeat.infrastructure_failure is not None:
        return Verdict(status="unavailable", reason=(
            f"repeat {repeat.run_id} was interrupted: {repeat.infrastructure_failure}"))
    statuses = [c.suite_status if c.dispatched and not c.timed_out else None
                for c in repeat.baseline]
    if len(statuses) != 2 or None in statuses:
        return Verdict(status="unavailable", reason=f"repeat {repeat.run_id}: baseline has no status")
    if any(status != 0 for status in statuses):
        return Verdict(status="fail", reason=f"repeat {repeat.run_id}: reference baseline is red")
    if not repeat.selected:
        return Verdict(status="fail", reason="the reference change has no mutation site")
    if len(repeat.selected) > contract.sample_size:
        return Verdict(status="unavailable", reason=f"repeat {repeat.run_id}: sample exceeds M")
    if [site.site for site in repeat.sites] != list(range(len(repeat.selected))):
        return Verdict(status="unavailable", reason=(
            f"repeat {repeat.run_id}: the sample is not accounted for"))
    tsc = any(site.check is not None and site.check.kind == "tsc" for site in repeat.sites)
    if tsc and (repeat.check_baseline is None or not _ran(repeat.check_baseline)):
        return Verdict(status="unavailable", reason=(
            f"repeat {repeat.run_id}: tsc did not run on the unmutated tree"))
    for call in repeat.baseline:
        _end(call, repeat.baseline_ms, None, repeat.attempt_ms)
    slowest = max(call.elapsed_ms or 0 for call in repeat.baseline)
    outcomes = []
    for site in repeat.sites:
        state = _site_outcome(site, slowest, repeat.attempt_ms, contract)
        if state in (None, "interrupted", "error", "unconfirmed") or site.settled_ms is None:
            return Verdict(status="unavailable", reason=(
                f"repeat {repeat.run_id}: site {site.site} is {state or 'unsettled'}"))
        assert state is not None
        outcomes.append(state)
    if repeat.required_settled_ms is None:
        return Verdict(status="unavailable", reason=f"repeat {repeat.run_id}: required phase open")
    return tuple(outcomes)


def _mutation(receipt: Receipt, task: SuiteTask) -> Verdict:
    """Three clean reference repeats, each with a green baseline, its whole sample settled and
    a nonempty valid population, and the same outcome at every site in all three."""
    facts = _facts(receipt)
    if isinstance(facts, Verdict):
        return facts
    if len({r.selected for r in facts.repeats}) != 1:
        return Verdict(status="fail", reason="reference repeats selected different sites")
    results = []
    for repeat in facts.repeats:
        try:
            result = _repeat_outcomes(repeat, facts.contract)
        except ValueError as error:
            return Verdict(status="unavailable", reason=f"repeat {repeat.run_id}: {error}")
        results.append(result)
    verdicts = [result for result in results if isinstance(result, Verdict)]
    if verdicts:
        return next((v for v in verdicts if v.status == "fail"), verdicts[0])
    outcomes = [result for result in results if not isinstance(result, Verdict)]
    if len(set(outcomes)) != 1:
        return Verdict(status="fail", reason="reference repeats gave different outcomes")
    valid = sum(state in ("killed", "timeout_kill", "survived") for state in outcomes[0])
    if not valid:
        return Verdict(status="fail", reason="the reference has no valid mutant")
    return Verdict(status="pass")


def projected_ms(*, setup_ms: int, settled_ms: int, sites: int, sample_size: int,
                 lanes: int) -> int:
    """A repeat's required time scaled to a full sample of M mutants in `lanes` lanes.

    The site phase runs in waves of `lanes`, so it is scaled by `ceil(M / L) / ceil(n / L)`,
    rounded up; with n = M it is the measured time.
    """
    waves = -(-sample_size // lanes)
    measured = -(-sites // lanes)
    return setup_ms + -(-(settled_ms - setup_ms) * waves // measured)


def _mutation_time(receipt: Receipt, task: SuiteTask) -> Verdict:
    """The slowest projected full-M reference time, times the contract's headroom, fits the
    task's mutation time limit. Equality passes; a censored repeat proves nothing."""
    facts = _facts(receipt)
    if isinstance(facts, Verdict):
        return facts
    contract = facts.contract
    projections = []
    for repeat in facts.repeats:
        if (repeat.infrastructure_failure is not None or repeat.required_settled_ms is None
                or not repeat.sites
                or [site.site for site in repeat.sites] != list(range(len(repeat.selected)))
                or any(site.settled_ms is None for site in repeat.sites)):
            return Verdict(status="unavailable", reason=(
                f"repeat {repeat.run_id}: the reference timing is censored or missing"))
        setup = min(site.dispatched_ms for site in repeat.sites)
        settled = max(site.settled_ms or 0 for site in repeat.sites)
        if settled < setup or repeat.required_settled_ms < settled:
            return Verdict(status="unavailable", reason=(
                f"repeat {repeat.run_id}: its clock readings are out of order"))
        projections.append(projected_ms(
            setup_ms=setup, settled_ms=settled, sites=len(repeat.sites),
            sample_size=contract.sample_size, lanes=contract.lanes))
    worst = max(projections)
    limit_ms = 1000 * task.mutation_timeout_seconds
    if worst * contract.headroom_percent > 100 * limit_ms:
        return Verdict(status="fail", reason=(
            f"projected reference time {worst} ms at {contract.headroom_percent}% exceeds the "
            f"{task.mutation_timeout_seconds} s mutation limit"))
    return Verdict(status="pass")


# Each gate's rule. B4 and B5 replace the stubs as their producers land. Private and fixed:
# a caller cannot hand freeze code to run.
Rule = Callable[[Receipt, SuiteTask], Verdict]
_RULES: dict[str, Rule] = {gate.name: _not_implemented for gate in GATES}
_RULES["correctness"] = _correctness
_RULES["mutation"] = _mutation
_RULES["mutation_time"] = _mutation_time


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
