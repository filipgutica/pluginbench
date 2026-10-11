"""Offline views of canonical, source-bound assessment observations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from statistics import median

from pluginbench import codex

REVIEW_SEVERITIES = {
    "P0": "Critical · immediate blocker",
    "P1": "High · fix before landing",
    "P2": "Medium · should fix",
    "P3": "Low · concrete improvement",
}
REVIEW_CATEGORIES = {
    "correctness": "Incorrect behavior or a missed requirement",
    "scope": "Unnecessary changes beyond the task",
    "maintainability": "Complexity, duplication, or unclear responsibilities",
    "tests": "Missing, misleading, or low-value tests",
}

# Panel, subtype, canonical evidence kind, canonical metric key.
METRICS = (
    ("Tokens", "Input tokens", "work", "input_tokens"),
    ("Tokens", "Output tokens", "work", "output_tokens"),
    ("Review tokens", "Judge input tokens", "review_work", "input_tokens"),
    ("Review tokens", "Judge output tokens", "review_work", "output_tokens"),
    ("Review findings", "Total review findings", "review_findings", "finding_count"),
    *(
        (
            "Review severity",
            f"{severity} review findings",
            "review_findings",
            f"severity.{severity}",
        )
        for severity in REVIEW_SEVERITIES
    ),
    *(
        (
            "Review categories",
            f"{category.title()} review findings",
            "review_findings",
            f"category.{category}",
        )
        for category in REVIEW_CATEGORIES
    ),
    ("Complexity", "Reported cognitive maximum", "fallow", "max_cognitive_reported"),
    ("Complexity", "Reported cyclomatic maximum", "fallow", "max_cyclomatic_reported"),
    *(
        (
            "Changed functions",
            f"{side.title()} changed-function {label}",
            "fallow_change",
            f"changed_functions.{side}.{key}",
        )
        for side in ("before", "after")
        for label, key in (
            ("count", "function_count_reported"),
            ("cognitive maximum", "max_cognitive_reported"),
            ("cyclomatic maximum", "max_cyclomatic_reported"),
            ("unit-size maximum", "max_unit_size_reported"),
        )
    ),
    *(
        (
            "New-file functions",
            f"New-file function {label}",
            "fallow_change",
            f"new_file_functions.after.{key}",
        )
        for label, key in (
            ("count", "function_count_reported"),
            ("cognitive maximum", "max_cognitive_reported"),
            ("cyclomatic maximum", "max_cyclomatic_reported"),
            ("unit-size maximum", "max_unit_size_reported"),
        )
    ),
    ("Coverage", "Branch coverage %", "coverage", "branch_percent"),
    ("Coverage", "Function coverage %", "coverage", "function_percent"),
    ("LOC", "Source LOC", "loc", "source_lines"),
    ("LOC", "Test LOC", "loc", "test_lines"),
    ("LOC", "Source lines added", "loc_change", "source_lines_added"),
    ("LOC", "Source lines removed", "loc_change", "source_lines_removed"),
    ("LOC", "Test lines added", "loc_change", "test_lines_added"),
    ("LOC", "Test lines removed", "loc_change", "test_lines_removed"),
    ("Reported dead findings", "Reported dead findings", "fallow", "dead_code"),
    (
        "Reported dead findings",
        "Introduced dead findings",
        "fallow_change",
        "dead_findings.introduced_count",
    ),
    (
        "Reported dead findings",
        "Resolved dead findings",
        "fallow_change",
        "dead_findings.resolved_count",
    ),
    (
        "Test overlap",
        "Zero marginal branch contribution cases",
        "coverage",
        "zero_marginal_count",
    ),
    ("Reported clone groups", "Reported clone groups", "fallow", "clone_groups"),
    (
        "Reported clone groups",
        "Introduced clone groups",
        "fallow_change",
        "clone_findings.introduced_count",
    ),
    (
        "Reported clone groups",
        "Resolved clone groups",
        "fallow_change",
        "clone_findings.resolved_count",
    ),
    (
        "Tests with zero unique contribution",
        "Branch contribution",
        "coverage",
        "zero_unique_branch_contribution_tests",
    ),
    (
        "Tests with zero unique contribution",
        "Mutant-kill contribution",
        "mutation",
        "zero_unique_mutant_kill_tests",
    ),
)

# Display copy stays separate from the metric identities used in evidence and CSVs.
# Each entry supplies a title, axis unit, and a brief explanation beside the plot.
METRIC_GUIDE = {
    "Input tokens": (
        "Input tokens",
        "tokens",
        "Tokens the implementation model read: the task, guidance, context, and tool results. "
        "Repeated context can be counted again on later turns; fewer tokens means less input used.",
    ),
    "Output tokens": (
        "Output tokens",
        "tokens",
        "Tokens the implementation model generated. Reasoning is included when the harness counts "
        "it in output; fewer tokens means less output used, not necessarily a better solution.",
    ),
    "Judge input tokens": (
        "Review input tokens",
        "tokens",
        "Tokens the independent reviewer read while judging the submitted code. "
        "These are review costs, separate from the coding model's input.",
    ),
    "Judge output tokens": (
        "Review output tokens",
        "tokens",
        "Tokens the independent reviewer generated while judging the submitted code. "
        "These are review costs, separate from the coding model's output.",
    ),
    "Branch coverage %": (
        "Branch coverage",
        "% covered",
        "Percentage of code branches exercised by the selected tests. "
        "Higher means a larger share ran; it does not prove their behavior was checked correctly.",
    ),
    "Function coverage %": (
        "Function coverage",
        "% covered",
        "Percentage of functions called by the selected tests. "
        "Higher means a larger share ran; it does not prove every path or result was checked.",
    ),
    "Source LOC": (
        "Total source lines",
        "lines",
        "Source lines in the measured code after the task, including comments and blank lines. "
        "This is the total size, not the number of lines the agent added.",
    ),
    "Test LOC": (
        "Total test lines",
        "lines",
        "Test lines in the measured code after the task, including comments and blank lines. "
        "More lines do not necessarily mean better tests.",
    ),
    **{
        f"{kind} lines {action}": (
            f"{kind} lines {action}",
            "lines",
            f"{kind} lines {action} by the agent's edits, including comments and blank lines. "
            "A rewritten line counts once as removed and once as added; these are not net changes.",
        )
        for kind in ("Source", "Test")
        for action in ("added", "removed")
    },
    "Reported dead findings": (
        "Total unused-code warnings",
        "warnings",
        "Unused-code warnings reported by the analyzer after the task, including inherited ones. "
        "Public APIs and runtime calls can produce false positives. Zero means no warnings detected.",
    ),
    "Introduced dead findings": (
        "New unused-code warnings",
        "warnings",
        "Unused-code warnings present after the task that were absent from the starting scan. "
        "Public APIs and runtime calls can produce false positives. Zero means no new warnings.",
    ),
    "Resolved dead findings": (
        "Unused-code warnings cleared",
        "warnings",
        "Warnings from the starting unused-code scan that are absent after the task. "
        "Higher means more warnings disappeared; the code may have been removed or made reachable.",
    ),
    "Reported clone groups": (
        "Total duplicate-code groups",
        "groups",
        "Groups of matching code blocks reported by the analyzer after the task, including inherited ones. "
        "This counts groups, not duplicated lines; some duplication may be intentional.",
    ),
    "Introduced clone groups": (
        "New duplicate-code groups",
        "groups",
        "Groups of matching code blocks detected after the task but absent from the starting scan. "
        "Lower means fewer new groups; this counts groups, not duplicated lines.",
    ),
    "Resolved clone groups": (
        "Duplicate-code groups cleared",
        "groups",
        "Duplicate-code groups from the starting scan that are absent after the task. "
        "Higher means more groups disappeared; it does not prove the remaining design is better.",
    ),
    "Zero marginal branch contribution cases": (
        "Test cases adding no branch coverage",
        "test cases",
        "Test cases from added or edited test files whose removal leaves the suite's covered branches unchanged. "
        "More means more coverage overlap; those tests may still check different behavior.",
    ),
    "Branch contribution": (
        "Tests with no unique branch coverage",
        "tests",
        "Supplied tests whose covered branches are also covered by other supplied tests. "
        "Shared coverage does not mean shared assertions or unnecessary tests.",
    ),
    "Mutant-kill contribution": (
        "Tests with no unique mutation detection",
        "tests",
        "Supplied tests that detect no deliberately changed code missed by the other supplied tests. "
        "This measures overlap in mutation detection, not whether a test is useful.",
    ),
}

_FUNCTION_GUIDE = {
    "count": (
        "Function count",
        "functions",
        "Number of reported functions",
        "This describes the measured code; fewer functions is not automatically better.",
    ),
    "cognitive maximum": (
        "Highest cognitive complexity",
        "score",
        "Highest branching-and-nesting score for any one function",
        "Lower suggests simpler control flow. The score starts at zero; it is not the complexity added.",
    ),
    "cyclomatic maximum": (
        "Highest cyclomatic complexity",
        "paths",
        "Most independent execution paths through any one function",
        "Higher means more paths to consider. A function with no branching starts at one.",
    ),
    "unit-size maximum": (
        "Longest function",
        "lines",
        "Most source lines occupied by any one function",
        "Lower means a shorter function; shortness alone does not establish good design.",
    ),
}
for _label, (_title, _unit, _meaning, _reading) in _FUNCTION_GUIDE.items():
    if _label in {"cognitive maximum", "cyclomatic maximum"}:
        METRIC_GUIDE[f"Reported {_label}"] = (
            _title,
            _unit,
            f"{_meaning} in the declared scan. {_reading}",
        )
    for _side in ("before", "after"):
        METRIC_GUIDE[f"{_side.title()} changed-function {_label}"] = (
            f"{_title} · {_side}",
            _unit,
            f"{_meaning} touched by changed lines, {_side} the agent's edits. {_reading}",
        )
    METRIC_GUIDE[f"New-file function {_label}"] = (
        f"{_title} in new files",
        _unit,
        f"{_meaning} in files added by the agent. {_reading}",
    )

METRIC_GUIDE["Total review findings"] = (
    "Review findings",
    "findings",
    "Actionable issues reported for each submission by an isolated AI reviewer using the same "
    "criteria for vanilla and guided code. Fewer means fewer reported issues, not proof of fewer defects.",
)
for _severity, _meaning in REVIEW_SEVERITIES.items():
    METRIC_GUIDE[f"{_severity} review findings"] = (
        f"{_severity} · {_meaning}",
        "findings",
        "Issues assigned this severity by the independent reviewer. "
        "The same rubric applies to both groups; severity is a judgment, not a measured defect count.",
    )
for _category, _meaning in REVIEW_CATEGORIES.items():
    METRIC_GUIDE[f"{_category.title()} review findings"] = (
        f"{_category.title()} findings",
        "findings",
        f"{_meaning}, as reported by the independent reviewer. Each issue has one category; "
        "fewer means fewer reported issues of this kind.",
    )


def _review_count(review: dict[str, object], key: str) -> int | None:
    """Retain explicit, internally consistent counts; absent findings are never zero."""
    issues = review.get("issues")
    total = review.get("finding_count")
    if (
        not isinstance(issues, list)
        or any(not isinstance(issue, dict) for issue in issues)
        or type(total) is not int
        or total != len(issues)
    ):
        return None
    if key == "finding_count":
        return total
    dimension, label = key.split(".")
    labels = REVIEW_SEVERITIES if dimension == "severity" else REVIEW_CATEGORIES
    counts = _map(review.get(f"{dimension}_counts"))
    if set(counts) != set(labels) or any(
        type(count) is not int or count < 0 for count in counts.values()
    ):
        return None
    expected = {name: 0 for name in labels}
    for issue in issues:
        name = issue.get(dimension)
        if name not in expected:
            return None
        expected[name] += 1
    return expected[label] if counts == expected else None


def _map(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return {}
    return dict(value)


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("canonical assessment requires object rows")
    return [_map(row) for row in value]


def _text(value: object) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return None


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _chart_provenance(provenance: dict[str, object]) -> dict[str, object]:
    """Keep chart bindings compact; canonical reports retain the full inventories."""
    compact = dict(provenance)
    syntax = compact.get("syntax_observer")
    if isinstance(syntax, dict):
        compact["syntax_observer"] = {
            key: syntax.get(key)
            for key in ("parser", "version", "commit", "parser_options", "lock_digest")
        } | {"binding_digest": _fingerprint(syntax)}
    for side in ("before", "after"):
        paths = compact.get(f"{side}_paths")
        if isinstance(paths, list):
            del compact[f"{side}_paths"]
            compact[f"{side}_file_count"] = len(paths)
            compact[f"{side}_inventory_digest"] = _fingerprint(paths)
    return compact


def _runtime(attempt: dict[str, object]) -> dict[str, str]:
    provenance = _map(attempt.get("provenance"))
    return {
        name: _text(provenance.get(name))
        for name in (
            "host",
            "harness",
            "harness_version",
            "reasoning_effort",
            "requested_reasoning_effort",
        )
    } | {
        "runtime_source": _text(provenance.get("runtime_source"))
        if provenance.get("runtime_source")
        else "native runtime evidence"
        if provenance.get("input") == "workflow-matrix"
        else "manifest declaration"
    }


def _assigned_runtime(attempt: dict[str, object]) -> dict[str, object]:
    """Use frozen assignment settings for counts, never for measured observations."""
    provenance = _map(attempt.get("provenance"))
    runtime = _runtime(attempt)
    model = _text(provenance.get("assigned_model"))
    effort = _text(
        provenance.get("assigned_reasoning_effort", runtime["requested_reasoning_effort"])
    )
    bound = model != "unknown" and effort != "unknown"
    return {
        **runtime,
        "model": model if bound else _text(attempt.get("model")),
        "reasoning_effort": effort if bound else runtime["reasoning_effort"],
        "requested_reasoning_effort": effort if bound else runtime["requested_reasoning_effort"],
        "harness_version": _text(provenance["assigned_harness_version"])
        if bound and "assigned_harness_version" in provenance
        else runtime["harness_version"],
        "assignment_bound": bound,
    }


def _total_work(stages: list[dict[str, object]]) -> dict[str, object]:
    work = [_map(stage.get("work")) for stage in stages]
    formats = [_text(row.get("token_format")) for row in work]
    matching = bool(work) and "unknown" not in formats and len(set(formats)) == 1
    tokens: dict[str, float | None] = {}
    for key in ("input_tokens", "output_tokens"):
        values = [_number(_map(row.get("tokens")).get(key)) for row in work]
        tokens[key] = (
            sum(value for value in values if value is not None)
            if matching and all(value is not None for value in values)
            else None
        )
    elapsed = [_number(row.get("elapsed_seconds")) for row in work]
    return {
        "tokens": tokens,
        "token_format": formats[0] if matching else None,
        "elapsed_seconds": sum(value for value in elapsed if value is not None)
        if all(value is not None for value in elapsed)
        else None,
    }


def _outcomes(attempts: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for attempt in attempts:
        identity = {
            "task": _text(attempt.get("task_id")),
            "arm": _text(attempt.get("arm")),
            "before_digest": _text(attempt.get("before_digest")),
            **_assigned_runtime(attempt),
        }
        groups.setdefault(json.dumps(identity, sort_keys=True), []).append(attempt)
    outcomes = []
    for identity_key, members in groups.items():
        row = json.loads(identity_key)
        row["assigned"] = len(members)
        states: dict[str, int] = {}
        for attempt in members:
            state = _text(_map(attempt.get("provenance")).get("attempt_status"))
            states[state] = states.get(state, 0) + 1
        row["attempt_states"] = states
        for role in ("handoff", "final"):
            counts = {"pass": 0, "fail": 0, "unknown": 0}
            for attempt in members:
                stage = next(
                    (
                        stage
                        for stage in _rows(attempt.get("stages"))
                        if stage.get("id") == attempt.get(role)
                    ),
                    {},
                )
                status = _text(_map(stage.get("correctness")).get("status"))
                counts[status if status in counts else "unknown"] += 1
            row[role] = counts
        burdens = [_map(attempt.get("correction_burden")) for attempt in members]
        for key in ("verified_issue_count", "external_rounds"):
            values = [_number(burden.get(key)) for burden in burdens]
            row[key] = (
                sum(value for value in values if value is not None)
                if all(burden.get("status") == "available" for burden in burdens)
                and all(value is not None for value in values)
                else None
            )
        reviews = [_map(attempt.get("independent_review")) for attempt in members]
        values = [_number(review.get("finding_count")) for review in reviews]
        row["selected_review_findings"] = (
            sum(value for value in values if value is not None)
            if all(review.get("status") == "reviewed" for review in reviews)
            and all(value is not None for value in values)
            else None
        )
        row["selected_review_stages"] = sorted(
            {_text(review.get("stage")) for review in reviews if review}
        )
        rounds = [_number(burden.get("external_rounds")) for burden in burdens]
        row["recorded_review_rounds"] = (
            sum(value for value in rounds if value is not None)
            if all(value is not None for value in rounds)
            else None
        )
        row["guidance_delivery"] = sorted(
            {_text(_map(attempt.get("provenance")).get("guidance_delivery")) for attempt in members}
        )
        actual_runtimes = []
        for attempt in members:
            actual = {"model": _text(attempt.get("model")), **_runtime(attempt)}
            unknown = any(
                actual[key] == "unknown"
                for key in ("model", "reasoning_effort", "harness", "harness_version")
            )
            mismatch = bool(row["assignment_bound"]) and any(
                actual[key] != "unknown" and row[key] != "unknown" and actual[key] != row[key]
                for key in ("model", "reasoning_effort", "harness_version")
            )
            actual_runtimes.append(
                {
                    "attempt_id": _text(attempt.get("id")),
                    **actual,
                    "unknown": unknown,
                    "mismatch": mismatch,
                }
            )
        row["actual_runtimes"] = actual_runtimes
        row["actual_runtime_unknown"] = sum(actual["unknown"] is True for actual in actual_runtimes)
        row["actual_runtime_mismatch"] = sum(
            actual["mismatch"] is True for actual in actual_runtimes
        )
        outcomes.append(row)
    return outcomes


@dataclass
class Observation:
    attempt_id: str
    task: str
    arm: str
    model: str
    host: str
    harness: str
    harness_version: str
    reasoning_effort: str
    requested_reasoning_effort: str
    runtime_source: str
    role: str
    stage: str
    panel: str
    metric: str
    value: float | None
    correctness: str
    status: str
    reason: str
    compatibility: str | None
    provenance: dict[str, object]
    snapshot_digest: str
    before_digest: str
    per_test_count: float | None
    elapsed_seconds: float | None
    source_lines_added: float | None
    source_lines_removed: float | None
    test_lines_added: float | None
    test_lines_removed: float | None
    change_loc_provenance: dict[str, object]


def _observation(
    attempt: dict[str, object], stage: dict[str, object], role: str, spec: tuple[str, str, str, str]
) -> Observation:
    panel, label, kind, key = spec
    work = _map(stage.get("work"))
    evidence = _map(_map(stage.get("metrics")).get("fallow" if kind == "fallow_change" else kind))
    metrics = _map(evidence.get("metrics"))
    provenance = _map(evidence.get("provenance"))
    settings = _runtime(attempt)
    host = settings["host"]
    runtime_source = settings["runtime_source"]
    population = _number(metrics.get("per_test_count"))
    reason = "no source-bound measurement"
    eligible = True
    identity: dict[str, object] = {
        "task": attempt.get("task_id"),
        "model": attempt.get("model"),
        "host": host,
        "before": attempt.get("before_digest"),
        "metric": key,
        **settings,
        "runtime_source": runtime_source,
    }
    if kind in {"review_work", "review_findings"}:
        review = _map(attempt.get("independent_review"))
        review_work = _map(review.get("work"))
        execution = _map(review.get("execution"))
        stage = next(
            (
                row
                for row in _rows(attempt.get("stages"))
                if row.get("id") == review.get("stage_id", attempt.get("handoff"))
            ),
            {},
        )
        work = review_work
        value = _number(_map(review_work.get("tokens")).get(key))
        provenance = {
            "token_format": review_work.get("token_format"),
            "judge_model": execution.get("actual_primary_models"),
            "judge_harness": {"codex": "codex-cli", "claude": "claude-cli"}.get(
                str(review.get("harness", review.get("requested_harness", "codex"))), "unknown"
            ),
            "judge_reasoning_effort": execution.get("runtime_effort_evidence"),
            "judge_harness_version": codex.observed_cli_version(execution.get("startup")),
            "review_stage": review.get("stage", "handoff"),
        }
        judge_effort = provenance["judge_reasoning_effort"]
        if codex.validated_claude_launch(
            execution,
            _text(review.get("requested_model")),
            _text(review.get("requested_reasoning_effort")),
        ):
            provenance["judge_harness"] = "claude-cli"
            provenance["judge_harness_version"] = _map(execution.get("launch")).get(
                "harness_version"
            )
            if judge_effort == []:
                judge_effort = [review.get("requested_reasoning_effort")]
                provenance["judge_comparison_reasoning_effort"] = judge_effort
                provenance["judge_effort_evidence_source"] = (
                    "requested launch setting; effective effort unverified"
                )
        judge_runtime_known = bool(
            provenance["judge_harness_version"]
            and provenance["judge_harness"] != "unknown"
            and provenance["review_stage"]
        ) and all(
            isinstance(value, list) and len(value) == 1 and _text(value[0]) != "unknown"
            for value in (provenance["judge_model"], judge_effort)
        )
        eligible = judge_runtime_known and bool(provenance["token_format"])
        identity.update(provenance)
        reason = f"separate independent judge usage for {provenance['review_stage']}; unreviewed or incomplete usage stays unknown"
        criteria = review.get("criteria_sha256")
        digest = criteria.removeprefix("sha256:") if isinstance(criteria, str) else ""
        criteria_valid = len(digest) == 64 and all(
            character in "0123456789abcdef" for character in digest
        )
        if criteria is not None:
            provenance["criteria_sha256"] = criteria
            identity["criteria_sha256"] = digest
            eligible = eligible and criteria_valid
        if kind == "review_findings":
            provenance.pop("token_format")
            identity.pop("token_format")
            eligible = (
                judge_runtime_known
                and review.get("status") == "reviewed"
                and bool(stage)
                and criteria_valid
                and _text(review.get("requested_model")) != "unknown"
                and _text(review.get("requested_reasoning_effort")) != "unknown"
                and provenance["judge_model"] == [review.get("requested_model")]
                and judge_effort == [review.get("requested_reasoning_effort")]
            )
            provenance["criteria_sha256"] = criteria
            identity.update(provenance)
            identity["criteria_sha256"] = digest
            value = _review_count(review, key) if eligible else None
            reason = (
                "independent AI judgment under fixed review criteria; counts are not deterministic "
                "defects, repair rounds, or human review churn"
                if value is not None
                else "valid fixed-criteria review, runtime identity, or explicit finding counts unavailable"
            )
    elif kind == "work":
        value = _number(_map(work.get("tokens")).get(key))
        provenance = {"token_format": work.get("token_format")}
        eligible = isinstance(work.get("token_format"), str) and bool(work["token_format"])
        identity.update(provenance)
        reason = (
            "total native tokens across all calls through final"
            if role == "final"
            else "first-call native tokens at handoff"
        ) + "; cached input and reasoning output subsets are not added"
    elif kind in {"loc", "loc_change"}:
        classification = "source" if key.startswith("source_") else "test"
        code_size = _map(stage.get("code_size" if classification == "source" else "test_code_size"))
        if code_size.get("status") == "available":
            value = _number(
                code_size.get(f"after_{classification}_lines" if kind == "loc" else key)
            )
            provenance = {
                field: code_size.get(field)
                for field in (
                    "method",
                    "scope",
                    "before_paths",
                    "after_paths",
                    "before_digest",
                    "after_digest",
                    "counting_semantics",
                )
            }
            identity.update({field: provenance[field] for field in ("method", "scope")})
            scope = code_size.get("scope")
            reason = (
                f"{classification} LOC under " + ", ".join(str(item) for item in scope)
                if isinstance(scope, list)
                else "source LOC with unknown scope"
            )
        elif code_size.get("status") == "unavailable":
            value = None
            eligible = False
            provenance = {"classification": f"scoped {classification} LOC unavailable"}
            reason = str(code_size.get("reason"))
        else:
            value = _number(
                _map(stage.get("snapshot_metadata" if kind == "loc" else "change")).get(key)
            )
            provenance = {
                "exclusion_names": _map(stage.get("snapshot_metadata")).get("exclusion_names"),
                "classification": "extension-classified LOC, including generated files",
            }
            eligible = provenance["exclusion_names"] is not None
            identity.update(provenance)
            reason = "extension-classified LOC, including generated files; shared source/test classification"
    elif kind == "fallow_change":
        comparison = _map(_map(stage.get("metric_comparison")).get("fallow"))
        path = key.split(".")
        detail = _map(comparison.get(path[0]))
        population_metrics = _map(detail.get(path[1])) if len(path) == 3 else detail
        value = _number(population_metrics.get(path[-1]))
        eligible = comparison.get("status") == detail.get("status") == "available" and all(
            provenance.get(field) is not None
            for field in ("tool", "tool_version", "config_digest", "scope")
        )
        identity.update(provenance)
        identity["method"] = detail.get("method")
        provenance = {
            **provenance,
            "method": detail.get("method"),
            "limitations": detail.get("limitations"),
            "complete_for_declared_scope": population_metrics.get("complete_for_declared_scope"),
            "reported_functions": population_metrics.get("function_count_reported"),
        }
        reason = str(
            detail.get("method")
            or detail.get("reason")
            or comparison.get("reason")
            or "bound before/after comparison unavailable"
        )
        if detail.get("status") == "incomplete":
            reason += "; reported functions only; population incomplete"
        if value is None and population_metrics.get("function_count_reported") == 0:
            reason += "; no reported functions in this population; maximum undefined"
        population = None
    else:
        value = _number(metrics.get(key))
        identity.update(provenance)
        eligible = all(
            provenance.get(field) is not None
            for field in ("tool", "tool_version", "config_digest", "scope")
        )
        scope = provenance.get("scope")
        if isinstance(scope, list):
            identity["scope"] = sorted(str(item) for item in scope)
        if kind == "fallow":
            complete_key = (
                "dead_code_complete_for_declared_scope"
                if key == "dead_code"
                else "clone_groups_complete_for_declared_scope"
                if key == "clone_groups"
                else "complexity_complete_for_declared_scope"
            )
            eligible = eligible and metrics.get(complete_key) is True
            provenance = {
                **provenance,
                "complete_for_declared_scope": metrics.get(complete_key),
                "reported_functions": metrics.get("function_count_reported"),
            }
            reason = (
                "reported findings only; scan population may be incomplete; zero is not absence proof"
                if key == "dead_code"
                else "reported clone groups from configured detection; this does not establish semantic redundancy"
                if key == "clone_groups"
                else "reported function maximum across declared scope, including tests when scoped; partial reporting is not a complete maximum"
            )
        if kind in {"coverage", "mutation"}:
            denominator = (
                "mutant_set_digest"
                if kind == "mutation"
                else (
                    "function_denominator_digest"
                    if key == "function_percent"
                    else "branch_denominator_digest"
                )
            )
            if kind != "coverage":
                identity[denominator] = metrics.get(denominator)
            provenance = {**provenance, denominator: metrics.get(denominator)}
            eligible = eligible and isinstance(metrics.get(denominator), str)
            reason = "fixed denominator and matching analyzer/config/scope required for comparison"
            if kind == "coverage":
                eligible = (
                    eligible and metrics.get("population_complete_for_declared_scope") is True
                )
                if metrics.get("population_complete_for_declared_scope") is not True:
                    reason = "reported coverage only; declared source population completeness is unproven"
                else:
                    reason = "coverage of each submission's source; populations may differ; descriptive per-submission rates, no fixed-denominator gain claim"
                provenance.update(
                    {
                        name: metrics.get(name)
                        for name in (
                            "test_scope",
                            "selected_test_files",
                            "reference_test_files",
                            "selection_policy",
                        )
                    }
                )
                provenance.update(
                    {
                        "covered": metrics.get(
                            "function_covered" if key == "function_percent" else "branch_covered"
                        ),
                        "total": metrics.get(
                            "function_total" if key == "function_percent" else "branch_total"
                        ),
                    }
                )
        if key == "zero_marginal_count":
            contribution = _map(metrics.get("test_contribution"))
            value = _number(contribution.get("zero_marginal_count"))
            population = _number(contribution.get("candidate_count"))
            eligible = eligible and contribution.get("status") == "available"
            test_scope = metrics.get("test_scope")
            valid_test_scope = (
                isinstance(test_scope, list)
                and bool(test_scope)
                and all(isinstance(name, str) and name for name in test_scope)
            )
            normalized_test_scope = (
                sorted({str(name).removeprefix("./") for name in test_scope})
                if valid_test_scope and isinstance(test_scope, list)
                else None
            )
            eligible = eligible and valid_test_scope
            identity["method"] = contribution.get("method")
            identity["test_scope"] = normalized_test_scope
            provenance.update(
                {
                    "method": contribution.get("method"),
                    "test_scope": normalized_test_scope,
                    "selection": contribution.get("selection"),
                    "reference_count": contribution.get("reference_count"),
                    "candidate_count": population,
                    "tested_count": contribution.get("tested_count"),
                    "limitations": contribution.get("limitations"),
                }
            )
            identity["selection"] = contribution.get("selection")
            reason = "fresh leave-one-out runs; zero marginal branch contribution is a test-overlap proxy, not a count of unnecessary tests"
        if key.startswith("zero_unique_"):
            names = metrics.get(key)
            value = (
                float(len(names))
                if isinstance(names, list) and population is not None and population > 0
                else None
            )
            eligible = False
            reason = "among supplied tests only; full-suite population completeness is not attested; shown as raw counts, not semantic redundancy"
        elif key != "zero_marginal_count":
            population = None
        if kind == "coverage" and metrics.get("clock") is not None:
            clock = _map(metrics["clock"])
            identity["clock"] = clock
            provenance["clock"] = clock
            reason += "; coverage under a controlled Date clock; native correctness uses its original clock"
    runtime_evidence = _map(attempt.get("provenance"))
    comparison_effort = _text(runtime_evidence.get("comparison_reasoning_effort"))
    if (
        host == "claude"
        and settings["harness"] == "claude-cli"
        and settings["reasoning_effort"] == "unknown"
        and comparison_effort != "unknown"
        and comparison_effort == settings["requested_reasoning_effort"]
        and _text(runtime_evidence.get("launch_harness_version")) != "unknown"
        and runtime_evidence.get("effort_evidence_source")
        == "requested launch setting; effective effort unverified"
    ):
        identity["reasoning_effort"] = comparison_effort
        identity["effort_comparison_basis"] = "validated native launch setting"
        provenance["reasoning_comparison"] = {
            "effective": "unknown",
            "requested": comparison_effort,
            "source": runtime_evidence["effort_evidence_source"],
        }
        reason += "; requested launch effort matched; effective Claude reasoning remains unverified"
    assignment = _assigned_runtime(attempt)
    if assignment["assignment_bound"] and any(
        _text(identity.get(field)) != _text(assignment[field])
        for field in ("model", "reasoning_effort", "harness_version")
        if assignment[field] != "unknown"
    ):
        eligible = False
        reason += "; observed runtime differs from frozen assignment"
    identity_known = all(
        _text(identity[field]) != "unknown"
        for field in (
            "task",
            "model",
            "before",
            "host",
            "harness",
            "harness_version",
            "reasoning_effort",
        )
    )
    compatibility = (
        _fingerprint(identity) if eligible and identity_known and value is not None else None
    )
    if value is None:
        status, reason = (
            "unknown",
            reason + "; unknown is not zero"
            if kind in {"fallow_change", "loc", "loc_change", "review_findings"}
            or key == "zero_marginal_count"
            else _text(evidence.get("reason"))
            if evidence.get("reason")
            else "measurement unavailable; unknown is not zero",
        )
    else:
        status = "measured" if compatibility else "not comparable"
    change = _map(stage.get("change"))
    code_size = _map(stage.get("code_size"))
    source_change = (
        code_size
        if code_size.get("status") == "available"
        else {}
        if code_size.get("status") == "unavailable"
        else change
    )
    test_size = _map(stage.get("test_code_size"))
    test_change = (
        test_size
        if test_size.get("status") == "available"
        else {}
        if test_size.get("status") == "unavailable"
        else change
    )
    change_provenance: dict[str, object] = {
        "source": {
            "status": code_size.get("status", "not configured"),
            "scope": code_size.get("scope"),
            "method": code_size.get("method")
            if code_size.get("status") == "unavailable"
            else code_size.get("method", "extension-classified LOC, including generated files"),
            "reason": code_size.get("reason"),
        },
        "tests": {
            "status": test_size.get("status", "not configured"),
            "scope": test_size.get("scope"),
            "method": test_size.get("method")
            if test_size.get("status") == "unavailable"
            else test_size.get(
                "method", "extension-classified test LOC, including generated files"
            ),
            "reason": test_size.get("reason"),
        },
        "raw_extension_classified_counts": {
            name: change.get(name)
            for name in (
                "source_lines_added",
                "source_lines_removed",
                "test_lines_added",
                "test_lines_removed",
            )
        },
    }
    return Observation(
        _text(attempt.get("id")),
        _text(attempt.get("task_id")),
        _text(attempt.get("arm")),
        _text(attempt.get("model")),
        host,
        settings["harness"],
        settings["harness_version"],
        settings["reasoning_effort"],
        settings["requested_reasoning_effort"],
        runtime_source,
        role,
        _text(stage.get("id")),
        panel,
        label,
        value,
        _text(_map(stage.get("correctness")).get("status")),
        status,
        reason,
        compatibility,
        _chart_provenance(provenance),
        _text(stage.get("snapshot_digest")),
        _text(attempt.get("before_digest")),
        population,
        _number(work.get("elapsed_seconds")),
        _number(source_change.get("source_lines_added")),
        _number(source_change.get("source_lines_removed")),
        _number(test_change.get("test_lines_added")),
        _number(test_change.get("test_lines_removed")),
        change_provenance,
    )


def _median_row(
    *, role: str, rows: list[Observation], baseline: list[Observation]
) -> dict[str, object]:
    row = rows[0]
    return {
        "role": role,
        "panel": row.panel,
        "metric": row.metric,
        "task": row.task,
        "model": row.model,
        "host": row.host,
        "before_digest": row.before_digest,
        "arm": row.arm,
        "compatibility": row.compatibility,
        "value": median(item.value for item in rows if item.value is not None),
        "measured": len(rows),
        "baseline_measured": len(baseline),
    }


def chart_data(report: dict[str, object]) -> dict[str, object]:
    """Extract observations without rescanning snapshots or invoking any tool."""
    if report.get("schema_version") != 1 or "snapshot_roots" not in report:
        raise ValueError("charts require a canonical version-1 assessment report")
    observations: list[Observation] = []
    distinct_final = False
    single_stage_attempts: list[str] = []
    attempts = _rows(report.get("attempts"))
    for attempt in attempts:
        stages = _rows(attempt.get("stages"))
        if not stages:
            raise ValueError("canonical attempt requires a handoff stage")
        distinct_final |= attempt.get("handoff") != attempt.get("final")
        if attempt.get("handoff") == attempt.get("final"):
            single_stage_attempts.append(_text(attempt.get("id")))
        for role, identifier in (
            ("handoff", attempt.get("handoff")),
            ("final", attempt.get("final")),
        ):
            if role == "final" and attempt.get("handoff") == attempt.get("final"):
                continue
            stage = next((row for row in stages if row.get("id") == identifier), None)
            if stage is None:
                raise ValueError("canonical stage identity is missing")
            if role == "final":
                final_index = stages.index(stage)
                stage = {**stage, "work": _total_work(stages[: final_index + 1])}
            observations.extend(
                _observation(attempt, stage, role, spec)
                for spec in METRICS
                if spec[2] not in {"review_work", "review_findings"}
                or report.get("independent_review")
            )
    groups: dict[tuple[str, str, str], list[Observation]] = {}
    raw_groups: dict[tuple[str, str, str], list[Observation]] = {}
    for row in observations:
        if row.value is None:
            continue
        identity = (
            row.compatibility
            if row.compatibility and row.arm != "unknown"
            else _fingerprint((row.task, row.metric, row.attempt_id))
        )
        roles = [row.role]
        if distinct_final and row.attempt_id in single_stage_attempts:
            roles.append("final")
        for role in roles:
            raw_groups.setdefault((role, identity, row.arm), []).append(row)
            if row.compatibility and row.arm != "unknown":
                groups.setdefault((role, row.compatibility, row.arm), []).append(row)
    for row in observations:
        if row.compatibility and not groups.get((row.role, row.compatibility, "baseline")):
            row.status = "not comparable"
            row.reason += "; no matching explicit baseline observation"
    medians = []
    for (role, identity, arm), rows in groups.items():
        baseline = groups.get((role, identity, "baseline"), [])
        if not baseline:
            continue
        medians.append(_median_row(role=role, rows=rows, baseline=baseline))
    raw_medians = [
        {
            **_median_row(
                role=role,
                rows=rows,
                baseline=groups.get((role, identity, "baseline"), []),
            ),
            "raw_profile_id": _fingerprint((role, identity, arm)),
            "attempt_ids": [row.attempt_id for row in rows],
        }
        for (role, identity, arm), rows in raw_groups.items()
    ]
    return {
        "name": report.get("name"),
        "study_progress": report.get("study_progress"),
        "independent_review": {
            key: value
            for key, value in _map(report.get("independent_review")).items()
            if key
            in {
                "status",
                "stage",
                "assigned",
                "requested_model",
                "requested_reasoning_effort",
                "reasoning_effort",
                "limitations",
                "cost_accounting",
                "criteria_sha256",
                "revision",
            }
        }
        if report.get("independent_review")
        else None,
        "review_findings": [
            {
                "attempt_id": attempt.get("id"),
                "task": attempt.get("task_id"),
                "arm": attempt.get("arm"),
                "status": review.get("status", "unknown"),
                "stage": review.get("stage"),
                "stage_id": review.get("stage_id"),
                "criteria_sha256": review.get("criteria_sha256"),
                "issues": review.get("issues"),
                "reason": review.get("reason"),
            }
            for attempt in attempts
            for review in [_map(attempt.get("independent_review"))]
            if report.get("independent_review")
        ],
        "limitations": report.get("limitations", []),
        "assigned": len(_rows(report.get("attempts"))),
        "distinct_final": distinct_final,
        "single_stage_attempts": single_stage_attempts,
        "outcomes": _outcomes(attempts),
        "metric_guide": {
            metric: {"title": title, "unit": unit, "explanation": explanation}
            for metric, (title, unit, explanation) in METRIC_GUIDE.items()
        },
        "observations": [asdict(row) for row in observations],
        "medians": medians,
        "raw_medians": raw_medians,
        "measurement_completeness": _measurement_completeness(attempts),
    }


def _measurement_completeness(attempts: list[dict[str, object]]) -> list[dict[str, object]]:
    """Count captured coding measurements separately from implementation correctness."""
    measured_tasks = {
        attempt.get("task_id")
        for attempt in attempts
        if (
            isinstance(configured := attempt.get("configured_metrics"), list)
            and any(kind in configured for kind in ("coverage", "fallow"))
        )
        or any(
            _map(evidence.get(kind)).get("metrics")
            for evidence in (
                _map(attempt.get("before_metrics")),
                *(_map(stage.get("metrics")) for stage in _rows(attempt.get("stages"))),
            )
            for kind in ("coverage", "fallow")
        )
    }
    rows = []
    for attempt in attempts:
        if attempt.get("task_id") not in measured_tasks:
            continue
        for role in ("handoff", "final"):
            stages = _rows(attempt.get("stages"))
            stage = next(row for row in stages if row["id"] == attempt.get(role))
            evidence = _map(stage.get("metrics"))
            static = _map(_map(evidence.get("fallow")).get("metrics"))
            coverage = _map(_map(evidence.get("coverage")).get("metrics"))
            overlap = _map(coverage.get("test_contribution"))
            comparison = _map(_map(stage.get("metric_comparison")).get("fallow"))
            work = (
                stage.get("work")
                if role == "handoff"
                else _total_work(stages[: stages.index(stage) + 1])
            )
            tokens = _map(_map(work).get("tokens"))
            captured = {
                "Input/output tokens": all(
                    _number(tokens.get(key)) is not None
                    for key in ("input_tokens", "output_tokens")
                ),
                "Source/test lines added/removed": all(
                    _map(stage.get(key)).get("status") == "available"
                    and all(
                        _number(_map(stage.get(key)).get(f"{kind}_lines_{side}")) is not None
                        for side in ("added", "removed")
                    )
                    for key, kind in (("code_size", "source"), ("test_code_size", "test"))
                ),
                "Complexity population": static.get("complexity_complete_for_declared_scope")
                is True
                and _number(static.get("function_count_reported")) is not None,
                "Coverage covered/total counts": coverage.get(
                    "population_complete_for_declared_scope"
                )
                is True
                and all(
                    _number(coverage.get(key)) is not None
                    for key in (
                        "branch_total",
                        "branch_covered",
                        "function_total",
                        "function_covered",
                    )
                ),
                "Introduced dead findings": _map(comparison.get("dead_findings")).get("status")
                == "available"
                and _number(_map(comparison.get("dead_findings")).get("introduced_count"))
                is not None,
                "Introduced clone groups": _map(comparison.get("clone_findings")).get("status")
                == "available"
                and _number(_map(comparison.get("clone_findings")).get("introduced_count"))
                is not None,
                "Test-overlap candidate counts": overlap.get("status") == "available"
                and _number(overlap.get("candidate_count")) is not None
                and overlap.get("tested_count") == overlap.get("candidate_count")
                and _number(overlap.get("zero_marginal_count")) is not None,
            }
            rows.append(
                {
                    "attempt_id": attempt["id"],
                    "task": attempt["task_id"],
                    "arm": attempt.get("arm"),
                    "role": role,
                    **_assigned_runtime(attempt),
                    "counts": captured,
                    "complete": all(captured.values()),
                }
            )
    return rows


def _csv_cell(value: object) -> str:
    text = (
        json.dumps(value, sort_keys=True)
        if isinstance(value, (dict, list))
        else str(value)
        if value is not None
        else ""
    )
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )


def chart_artifacts(report: dict[str, object]) -> tuple[str, str]:
    return chart_artifacts_from_data(chart_data(report))


def chart_artifacts_from_data(data: dict[str, object]) -> tuple[str, str]:
    """Render an already-selected dataset without regenerating its observations."""
    payload = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c")
    template = files("pluginbench").joinpath("assessment-charts.html").read_text()
    vendor = files("pluginbench").joinpath("vendor")
    library = vendor.joinpath("echarts.min.js").read_text()
    licenses = "\n\n".join(
        vendor.joinpath(name).read_text()
        for name in ("ECHARTS-LICENSE", "ECHARTS-NOTICE", "ZRENDER-LICENSE")
    )
    # Keep the standalone export licensed and safe inside an HTML script element.
    library = ("/*\n" + licenses.replace("*/", "* / ") + "\n*/\n" + library).replace(
        "</script", "<\\/script"
    )
    html = template.replace("__ECHARTS_SCRIPT__", library).replace("__ASSESSMENT_DATA__", payload)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    rows = _rows(data["observations"])
    fields = data.get("observation_fields", list(Observation.__dataclass_fields__))
    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
        raise ValueError("chart observation fields must be a list of strings")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([_csv_cell(row.get(key)) for key in fields])
    return html, stream.getvalue()


def chart_paths(out_dir: Path) -> tuple[Path, Path]:
    return out_dir / "assessment-charts.html", out_dir / "assessment-charts.csv"
