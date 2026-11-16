"""One binary grade per attempt: the C1 coding and grade classification, grade contract 1.

A completed attempt passes or fails. Its only grade is the source-only verifier grade
(`source_only.py`); the native reward, test overlap, an empty patch, a timeout, an agent error
and a descendant on another model are attributes beside it, never other outcomes. What cannot
be graded is an infrastructure error: never a result, never in a denominator, and retried.

- Coding stage (`study._execute_stage`): a session that failed before its first model turn,
  a treatment that was not delivered, a lost worker or container, a missing export and broken
  evidence are infrastructure; a timeout or an agent error after the first turn is graded.
- Grade stage (`source_only.record_source_only_grades`): a reward that is not 0 or 1 is
  regraded once; a submission the patch policy refuses, or one whose verifier still crashes,
  fails only when the task's nop and oracle requalify healthy, and is infrastructure otherwise.
  Missing or changed evidence and host errors are infrastructure whatever the task's health.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pluginbench import codex_events

# Bump when what passes, fails or is infrastructure changes. Runs whose plan.json has no
# `grade_contract` were graded before it existed: they read as `legacy`, never regraded.
GRADE_CONTRACT = 1
VERIFIER_CRASHED = "verifier_crashed_on_submission"
UNSUPPORTED_SUBMISSION = "unsupported_submission"
# Written in an attempt's stage directory when its coding ended in an infrastructure error.
INFRASTRUCTURE_ERROR = "infrastructure-error.json"

Outcome = Literal["pass", "fail", "infra", "legacy"]

# The C1 tables, row by row: the evidence and what it becomes. `test_grade.py` holds one fixture
# per row and fails on any producer of an ungraded state that no row maps.
CODING_ROWS = {
    "timeout_after_first_turn": "graded; attribute timed_out; never retried",
    "agent_error_after_first_turn": "graded; attribute agent_error; never retried",
    "failed_before_first_turn": "infrastructure; the run stops and a resume codes it again",
    "provider_stop": "infrastructure; the run stops and a resume codes it again",
    "treatment_not_delivered": "infrastructure; retried",
    "worker_or_container_loss": "infrastructure; retried",
    "harbor_trial_error": "infrastructure unless an agent or verifier error; retried",
    "builtin_mismatch": "not graded; the run stops as before",
    "descendant_on_another_model": "graded; attribute mixed_model",
}
GRADE_ROWS = {
    "reward_not_binary": f"regrade once; healthy nop/oracle -> fail {VERIFIER_CRASHED}; "
    "else infrastructure",
    "quoted_path": "graded: paths come from git apply --numstat -z, matched by section order",
    "policy_violation": f"healthy nop/oracle -> fail {UNSUPPORTED_SUBMISSION}:<kind>; "
    "else infrastructure",
    "evidence_or_host_error": "infrastructure, whatever the task's health",
}


def failed_before_first_turn(execution: Mapping[str, Any], harness: object) -> str | None:
    """Why a coding session never reached a model turn, for either harness; else None."""
    if harness == "claude":
        return _claude_failed_before_first_turn(execution)
    return codex_events.failed_before_first_turn(dict(execution))


def _claude_failed_before_first_turn(execution: Mapping[str, Any]) -> str | None:
    """Why a Claude coding session never reached a model turn, else None.

    Claude names no such failure, unlike Codex's `turn.failed`, and its startup model stands in
    for an assistant's. So a session with no timeout, usage, tool call or message counts as one.
    A timeout is the agent's attempt, as for Codex. This reads the parsed execution only, so it
    lives here rather than in the hashed Claude launcher.
    """
    if (
        execution.get("timed_out") is True
        or execution.get("usage") is not None
        or any(execution.get(key) for key in ("command_items", "other_tools", "agent_messages"))
    ):
        return None
    for error in execution.get("stream_errors") or []:
        if isinstance(error, dict) and isinstance(error.get("result"), str) and error["result"]:
            return str(error["result"])
    return "the Claude session ended before any model turn"


# Harbor exception types a graded attempt may carry: the agent's own timeout, exit or refusal,
# and a verifier that produced no reward, which the grade stage regrades. Any other Harbor
# exception (environment start, agent setup, account or model errors) is infrastructure.
AGENT_HARBOR_ERRORS = frozenset(
    {"AgentTimeoutError", "NonZeroAgentExitCodeError", "AgentSafetyRefusalError"}
)
VERIFIER_HARBOR_ERRORS = frozenset(
    {
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
    }
)


def coding_infrastructure(execution: Mapping[str, Any]) -> str | None:
    """Why a returned coding session is infrastructure whatever it submitted, else None.

    The caller finds a worker that raised, artifacts it could not copy, and a submission that
    was not exported, which is a policy violation when the hash-bound patch is one.
    """
    if error := execution.get("treatment_error"):
        return f"the treatment was not delivered: {error}"
    harbor = execution.get("error")
    if isinstance(harbor, Mapping) and harbor.get("exception_type") not in (
        AGENT_HARBOR_ERRORS | VERIFIER_HARBOR_ERRORS
    ):
        kind, message = harbor.get("exception_type"), harbor.get("exception_message")
        return f"Harbor trial failed: {kind}: {message}"
    return None


def _differs(observed: object, root: set[object]) -> bool:
    return isinstance(observed, list) and any(value not in root for value in observed)


def attributes(execution: Mapping[str, Any]) -> dict[str, bool]:
    """The coding attributes a graded attempt carries; none of them changes its grade.

    Infrastructure causes never reach a grade, so `agent_error` is a session that worked and
    did not finish successfully, a timeout aside. `mixed_model` is a descendant on another
    model or reasoning effort than the root, or a root stream with more than one model.
    """
    timed_out = execution.get("timed_out") is True
    models = execution.get("actual_primary_models")
    efforts = execution.get("runtime_effort_evidence")
    root_models = set(models) if isinstance(models, list) else set()
    root_efforts = set(efforts) if isinstance(efforts, list) else set()
    return {
        "timed_out": timed_out,
        "agent_error": not timed_out and execution.get("successful_native_completion") is not True,
        "mixed_model": len(root_models) > 1
        or _differs(execution.get("actual_descendant_models"), root_models)
        or _differs(execution.get("actual_descendant_efforts"), root_efforts),
    }


def outcome(grading: Mapping[str, Any] | None) -> tuple[Outcome, str | None] | None:
    """`(outcome, reason)` of one attempt's grade record; None when it has none yet.

    The single reader of a grade: status, the report, compare and judge all call it.
    """
    if grading is None:
        return None
    if grading.get("grade_contract") != GRADE_CONTRACT:
        return "legacy", "graded before grade contract 1"
    if grading.get("status") == "infra":
        return "infra", str(grading.get("reason"))
    if grading.get("passed") is True:
        return "pass", None
    return "fail", grading.get("fail_reason")
