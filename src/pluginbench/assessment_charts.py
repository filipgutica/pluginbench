"""Offline views of canonical, source-bound assessment observations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass
from importlib.resources import files
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
# Panels marked diagnostic stay in results JSON/CSV but out of report charts and summaries.
DIAGNOSTIC_PANELS = frozenset({"Whole-suite coverage"})
METRICS = (
    ("Tokens", "Input tokens", "work", "input_tokens"),
    ("Tokens", "Cached input tokens", "work", "cached_input_tokens"),
    ("Tokens", "Uncached input tokens", "work", "uncached_input_tokens"),
    ("Tokens", "Output tokens", "work", "output_tokens"),
    ("Tokens", "Reasoning output tokens", "work", "reasoning_output_tokens"),
    ("Time", "Coding time", "work", "elapsed_seconds"),
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
    ("Code added", "Net cognitive complexity added", "fallow_change", "code_added.net_cognitive"),
    ("Code added", "Net cyclomatic complexity added", "fallow_change", "code_added.net_cyclomatic"),
    ("Code added", "Functions added", "fallow_change", "code_added.functions_added"),
    ("Code added", "Source files added", "fallow_change", "code_added.source_files_added"),
    ("Code added", "Complexity hotspots added", "fallow_change", "code_added.hotspots_added"),
    (
        "Code added",
        "Introduced dead findings",
        "fallow_change",
        "dead_findings.introduced_count",
    ),
    (
        "Code added",
        "Introduced clone groups",
        "fallow_change",
        "clone_findings.introduced_count",
    ),
    ("LOC", "Source lines added", "loc_change", "source_lines_added"),
    ("LOC", "Source lines removed", "loc_change", "source_lines_removed"),
    ("LOC", "Source lines net", "loc_change", "source_lines_net"),
    ("LOC", "Test lines added", "loc_change", "test_lines_added"),
    ("LOC", "Test lines removed", "loc_change", "test_lines_removed"),
    ("Tests added", "Changed-line coverage %", "coverage_change", "changed_lines.line_percent"),
    ("Tests added", "Changed-branch coverage %", "coverage_change", "changed_lines.branch_percent"),
    (
        "Tests added",
        "Redundant added tests",
        "coverage_change",
        "changed_lines.redundant_added_tests",
    ),
    (
        "Tests added",
        "Duplicated added test lines",
        "change",
        "test_duplication.duplicated_added_test_lines",
    ),
    ("Whole-suite coverage", "Line coverage %", "coverage", "line_percent"),
    ("Whole-suite coverage", "Statement coverage %", "coverage", "statement_percent"),
    ("Whole-suite coverage", "Branch coverage %", "coverage", "branch_percent"),
    ("Whole-suite coverage", "Function coverage %", "coverage", "function_percent"),
    (
        "Tests with zero unique contribution",
        "Branch contribution",
        "coverage",
        "zero_unique_branch_contribution_tests",
    ),
)

# Display copy stays separate from the metric identities used in evidence and CSVs.
# Each entry supplies a title, axis unit, and a brief explanation beside the plot.
METRIC_GUIDE = {
    "Line coverage %": (
        "Line coverage",
        "% covered",
        "Executable source lines reached by the selected tests. Unsupported units remain unknown; coverage alone does not establish useful assertions.",
    ),
    "Statement coverage %": (
        "Statement coverage",
        "% covered",
        "Source statements reached by the selected tests. Go counts statements within measured blocks; this is not branch or function coverage.",
    ),
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
    "Introduced dead findings": (
        "New unused-code warnings",
        "warnings",
        "Unused-code warnings present after the task that were absent from the starting scan. "
        "Public APIs and runtime calls can produce false positives. Zero means no new warnings.",
    ),
    "Introduced clone groups": (
        "New duplicate-code groups",
        "groups",
        "Groups of matching code blocks detected after the task but absent from the starting scan. "
        "Lower means fewer new groups; this counts groups, not duplicated lines.",
    ),
    "Branch contribution": (
        "Tests with no unique branch coverage",
        "tests",
        "Supplied tests whose covered branches are also covered by other supplied tests. "
        "Shared coverage does not mean shared assertions or unnecessary tests.",
    ),
}

METRIC_GUIDE.update(
    {
        "Cached input tokens": (
            "Cached input tokens",
            "tokens",
            "Input tokens served from the provider's prompt cache. Usually priced far below fresh input.",
        ),
        "Uncached input tokens": (
            "Uncached input tokens",
            "tokens",
            "Input tokens that were not served from cache: input minus cached input. The better input-cost signal.",
        ),
        "Reasoning output tokens": (
            "Reasoning output tokens",
            "tokens",
            "Output tokens spent on hidden reasoning. A subset of output tokens, not added to them.",
        ),
        "Source lines net": (
            "Source lines net",
            "lines",
            "Source lines added minus source lines removed. Negative means the submission shrank the code.",
        ),
        "Coding time": (
            "Coding time",
            "seconds",
            "Elapsed time of the coding run, including subagents. Shared machine load can affect it.",
        ),
        "Net cognitive complexity added": (
            "Cognitive complexity added",
            "score",
            "Branching-and-nesting the agent added to source code: changed functions after the edit "
            "minus the same functions before. New functions count in full; deleted ones subtract.",
        ),
        "Net cyclomatic complexity added": (
            "Cyclomatic complexity added",
            "paths",
            "Independent paths the agent added to source code, measured like cognitive complexity "
            "added. Editing an already-complex function counts only its growth.",
        ),
        "Functions added": (
            "Functions added",
            "functions",
            "Source functions after the edit whose path and name did not exist before. Renames count as added.",
        ),
        "Source files added": (
            "Source files added",
            "files",
            "New non-test source files. More files is not automatically worse; compare with the reference.",
        ),
        "Complexity hotspots added": (
            "Complexity hotspots added",
            "functions",
            "New or grown source functions whose cognitive score is above 15, a common review threshold.",
        ),
        "Changed-line coverage %": (
            "Changed-line coverage",
            "% covered",
            "Share of the agent's added executable source lines that its submitted tests run. "
            "Running a line does not prove its behavior is checked.",
        ),
        "Changed-branch coverage %": (
            "Changed-branch coverage",
            "% covered",
            "Share of branches on the agent's added source lines that its submitted tests take.",
        ),
        "Redundant added tests": (
            "Redundant added tests",
            "tests",
            "Added tests whose covered added lines are all covered by other tests. "
            "Candidates only: equal coverage does not mean equal assertions.",
        ),
        "Duplicated added test lines": (
            "Duplicated added test lines",
            "lines",
            "Added test lines inside a six-line block that repeats elsewhere in the added tests.",
        ),
    }
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


def _net_lines(size: dict[str, object]) -> float | None:
    added, removed = (
        _number(size.get("source_lines_added")),
        _number(size.get("source_lines_removed")),
    )
    return added - removed if added is not None and removed is not None else None


def _signed(value: object) -> float | None:
    """Like _number, for measures that can fall below zero, such as complexity removed."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    return None


def execution_outcome(provenance: object) -> dict[str, object]:
    """Explain bound native completion independently of source/task grading."""
    evidence = _map(provenance)
    if evidence.get("successful_native_completion") is True:
        return {"status": "complete", "reason": None}
    if evidence.get("timed_out") is True:
        return {"status": "timed_out", "reason": "Native execution timed out."}
    exit_code = evidence.get("exit_code")
    if evidence.get("successful_native_completion") is not False and not (
        type(exit_code) is int and exit_code != 0
    ):
        return {"status": "unknown", "reason": None}
    error = _map(evidence.get("error"))
    error_type = error.get("exception_type")
    cause = None
    stream_errors = evidence.get("stream_errors")
    if isinstance(stream_errors, list):
        for event in reversed(stream_errors):
            row = _map(event)
            message = _map(row.get("error")).get("message", row.get("message"))
            if isinstance(message, str) and message.strip():
                cause = message.strip()
                break
    if cause is None:
        message = error.get("message")
        if not message and not error_type:
            message = error.get("exception_message")
        if not message and isinstance(evidence.get("error"), str):
            message = evidence["error"]
        if isinstance(message, str) and message.strip():
            cause = message.strip().splitlines()[0]
    label = "Native execution failed"
    if isinstance(error_type, str) and error_type:
        label += " (" + error_type + ")"
    if cause:
        reason = label + ": " + cause
    elif type(exit_code) is int and exit_code != 0:
        reason = label + f" (exit {exit_code})."
    else:
        reason = label + "."
    return {"status": "failed", "reason": reason}


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
    static_kind = (
        "analysis"
        if "analysis" in _map(stage.get("metrics"))
        and _map(_map(stage.get("metrics")).get("analysis")).get("status") == "available"
        else "fallow"
    )
    work = _map(stage.get("work"))
    native_outcome = execution_outcome(stage.get("execution_provenance"))
    evidence = _map(
        _map(stage.get("metrics")).get(
            {
                "fallow": static_kind,
                "fallow_change": static_kind,
                "coverage_change": "coverage",
            }.get(kind, kind)
        )
    )
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
                if row.get("id") == review.get("stage_id")
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
            "review_stage": review.get("stage"),
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
                "defects or human review effort"
                if value is not None
                else "valid fixed-criteria review, runtime identity, or explicit finding counts unavailable"
            )
    elif kind == "work":
        tokens = _map(work.get("tokens"))
        if key == "uncached_input_tokens":
            total, cached = (
                _number(tokens.get(name)) for name in ("input_tokens", "cached_input_tokens")
            )
            value = total - cached if total is not None and cached is not None else None
        elif key == "elapsed_seconds":
            value = _number(work.get("elapsed_seconds"))
        else:
            value = _number(tokens.get(key))
        provenance = {"token_format": work.get("token_format")}
        eligible = isinstance(work.get("token_format"), str) and bool(work["token_format"])
        identity.update(provenance)
        reason = "native tokens at handoff; cached input and reasoning output subsets are not added"
    elif kind == "loc_change":
        classification = "source" if key.startswith("source_") else "test"
        code_size = _map(stage.get("code_size" if classification == "source" else "test_code_size"))
        if code_size.get("status") == "available":
            value = (
                _net_lines(code_size) if key == "source_lines_net" else _number(code_size.get(key))
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
            change_size = _map(stage.get("change"))
            value = (
                _net_lines(change_size)
                if key == "source_lines_net"
                else _number(change_size.get(key))
            )
            provenance = {
                "exclusion_names": _map(stage.get("snapshot_metadata")).get("exclusion_names"),
                "classification": "extension-classified LOC, including generated files",
            }
            eligible = provenance["exclusion_names"] is not None
            identity.update(provenance)
            reason = "extension-classified LOC, including generated files; shared source/test classification"
    elif kind == "change":
        section, field = key.split(".")
        detail = _map(_map(stage.get("change")).get(section))
        value = _number(detail.get(field))
        provenance = {"method": detail.get("method")}
        eligible = bool(detail)
        identity.update(provenance)
        reason = str(detail.get("method") or "bound source diff unavailable")
    elif kind in {"fallow_change", "coverage_change"}:
        comparison = _map(
            _map(stage.get("metric_comparison")).get(
                "coverage" if kind == "coverage_change" else static_kind
            )
        )
        path = key.split(".")
        detail = _map(comparison.get(path[0]))
        population_metrics = _map(detail.get(path[1])) if len(path) == 3 else detail
        value = (_signed if path[-1].startswith("net_") else _number)(
            population_metrics.get(path[-1])
        )
        # Changed-line results stand on their own; whole-suite coverage is incomplete whenever lines
        # change. Code-added results still require their static comparison to be available.
        eligible = (
            detail.get("status") == "available"
            and (kind == "coverage_change" or comparison.get("status") == "available")
        ) and all(
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
        if kind == "coverage_change" and key.endswith("_percent"):
            # Changed-line rates keep their own covered/total counts beside the percentage.
            covered_key, total_key = (
                ("added_lines_covered", "added_executable_lines")
                if key.endswith("line_percent")
                else ("branch_covered", "branch_total")
            )
            provenance.update(covered=detail.get(covered_key), total=detail.get(total_key))
        reason = str(
            detail.get("method")
            or detail.get("reason")
            or comparison.get("reason")
            or "bound before/after comparison unavailable"
        )
        if detail.get("status") == "incomplete":
            reason += "; population incomplete"
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
            if static_kind == "analysis":
                reason = (
                    str(metrics.get("dead_code_detection"))
                    if key == "dead_code"
                    else "function scores under the named language analyzer; unlike analyzer definitions are not interchangeable"
                )
                if key != "dead_code" and metrics.get("language") == "python":
                    reason += "; " + str(_map(metrics.get("settings")).get("python_rca_population"))
        if kind == "coverage":
            unit = key.partition("_")[0] if key.endswith("_percent") else "branch"
            denominator = f"{unit}_denominator_digest"
            provenance = {**provenance, denominator: metrics.get(denominator)}
            eligible = eligible and isinstance(metrics.get(denominator), str)
            complete = metrics.get(
                f"{unit}_population_complete", metrics.get("population_complete_for_declared_scope")
            )
            eligible = eligible and complete is True
            if metrics.get(denominator) is None:
                reason = f"{unit} coverage is not supplied by {metrics.get('format') or 'this report format'}"
            elif complete is not True:
                reason = (
                    "reported coverage only; declared source population completeness is unproven"
                )
            else:
                reason = "coverage of each submission's source; populations may differ; descriptive per-submission rates, no fixed-denominator gain claim"
            plural = {
                "line": "lines",
                "statement": "statements",
                "branch": "branches",
                "function": "functions",
            }
            limit = _map(_map(metrics.get("unavailable_populations")).get(plural[unit]))
            if limit.get("reason") and (metrics.get(denominator) is None or complete is not True):
                reason = str(limit["reason"])
            if metrics.get(f"{unit}_total") == 0 and complete is True:
                reason = f"measured {unit} population is empty; percentage is undefined"
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
                    "covered": metrics.get(f"{unit}_covered"),
                    "total": metrics.get(f"{unit}_total"),
                }
            )
        if key.startswith("zero_unique_"):
            names = metrics.get(key)
            value = (
                float(len(names))
                if isinstance(names, list) and population is not None and population > 0
                else None
            )
            eligible = False
            reason = "among supplied tests only; full-suite population completeness is not attested; shown as raw counts, not semantic redundancy"
        else:
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
    provenance.update(
        execution_status=native_outcome["status"], execution_reason=native_outcome["reason"]
    )
    if value is None:
        if kind == "work" and isinstance(provenance.get("execution_reason"), str):
            reason = (
                str(provenance["execution_reason"])
                + "; final token usage unavailable; unknown is not zero"
            )
            partial = _map(stage.get("execution_provenance")).get("partial_usage")
            if isinstance(partial, list) and partial:
                reason += "; partial usage receipts are lower bounds, not totals"
            status = "unknown"
        else:
            status, reason = (
                "unknown",
                reason + "; unknown is not zero"
                if kind
                in {
                    "fallow_change",
                    "coverage_change",
                    "change",
                    "loc",
                    "loc_change",
                    "review_findings",
                }
                or (kind == "coverage" and metrics)
                else _text(evidence.get("reason"))
                if evidence.get("reason")
                else "measurement unavailable; unknown is not zero",
            )
    else:
        status = "measured" if compatibility else "not comparable"
    change = _map(stage.get("change"))
    line_changes: dict[str, float | None] = {}
    change_provenance: dict[str, object] = {}
    for classification, size_field, section, default_method in (
        ("source", "code_size", "source", "extension-classified LOC, including generated files"),
        (
            "test",
            "test_code_size",
            "tests",
            "extension-classified test LOC, including generated files",
        ),
    ):
        size = _map(stage.get(size_field))
        size_status = size.get("status")
        measured_change = (
            size if size_status == "available" else {} if size_status == "unavailable" else change
        )
        for action in ("added", "removed"):
            field = f"{classification}_lines_{action}"
            line_changes[field] = _number(measured_change.get(field))
        change_provenance[section] = {
            "status": size.get("status", "not configured"),
            "scope": size.get("scope"),
            "method": size.get("method")
            if size_status == "unavailable"
            else size.get("method", default_method),
            "reason": size.get("reason"),
        }
    change_provenance["raw_extension_classified_counts"] = {
        name: change.get(name)
        for name in (
            "source_lines_added",
            "source_lines_removed",
            "test_lines_added",
            "test_lines_removed",
        )
    }
    return Observation(
        attempt_id=_text(attempt.get("id")),
        task=_text(attempt.get("task_id")),
        arm=_text(attempt.get("arm")),
        model=_text(attempt.get("model")),
        host=host,
        harness=settings["harness"],
        harness_version=settings["harness_version"],
        reasoning_effort=settings["reasoning_effort"],
        requested_reasoning_effort=settings["requested_reasoning_effort"],
        runtime_source=runtime_source,
        role=role,
        stage=_text(stage.get("id")),
        panel=panel,
        metric=label,
        value=value,
        correctness=_text(_map(stage.get("correctness")).get("status")),
        status=status,
        reason=reason,
        compatibility=compatibility,
        provenance=_chart_provenance(provenance),
        snapshot_digest=_text(stage.get("snapshot_digest")),
        before_digest=_text(attempt.get("before_digest")),
        per_test_count=population,
        elapsed_seconds=_number(work.get("elapsed_seconds")),
        source_lines_added=line_changes["source_lines_added"],
        source_lines_removed=line_changes["source_lines_removed"],
        test_lines_added=line_changes["test_lines_added"],
        test_lines_removed=line_changes["test_lines_removed"],
        change_loc_provenance=change_provenance,
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
    attempts = _rows(report.get("attempts"))
    for attempt in attempts:
        stages = _rows(attempt.get("stages"))
        if not stages:
            raise ValueError("canonical attempt requires a handoff stage")
        if len(stages) != 1 or stages[0].get("id") != "handoff":
            raise ValueError("canonical attempt requires exactly one handoff submission")
        stage = stages[0]
        role = "handoff"
        observations.extend(
            _observation(attempt, stage, role, spec)
            for spec in METRICS
            if spec[2] not in {"review_work", "review_findings"} or report.get("independent_review")
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
        raw_groups.setdefault((row.role, identity, row.arm), []).append(row)
        if row.compatibility and row.arm != "unknown":
            groups.setdefault((row.role, row.compatibility, row.arm), []).append(row)
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
        "assigned": len(attempts),
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
            and any(kind in configured for kind in ("coverage", "fallow", "analysis"))
        )
        or any(
            _map(evidence.get(kind)).get("metrics")
            for evidence in (
                _map(attempt.get("before_metrics")),
                *(_map(stage.get("metrics")) for stage in _rows(attempt.get("stages"))),
            )
            for kind in ("coverage", "fallow", "analysis")
        )
    }
    rows = []
    for attempt in attempts:
        if attempt.get("task_id") not in measured_tasks:
            continue
        stages = _rows(attempt.get("stages"))
        stage = stages[0]
        evidence = _map(stage.get("metrics"))
        static_kind = (
            "analysis" if _map(evidence.get("analysis")).get("status") == "available" else "fallow"
        )
        static = _map(_map(evidence.get(static_kind)).get("metrics"))
        coverage = _map(_map(evidence.get("coverage")).get("metrics"))
        comparison = _map(_map(stage.get("metric_comparison")).get(static_kind))
        work = stage.get("work")
        tokens = _map(_map(work).get("tokens"))
        coverage_units = [
            unit
            for unit in ("line", "statement", "branch", "function")
            if any(
                coverage.get(f"{unit}_{key}") is not None
                for key in ("total", "covered", "percent", "denominator_digest")
            )
        ]
        captured = {
            "Input/output tokens": all(
                _number(tokens.get(key)) is not None for key in ("input_tokens", "output_tokens")
            ),
            "Source/test lines added/removed": all(
                _map(stage.get(key)).get("status") == "available"
                and all(
                    _number(_map(stage.get(key)).get(f"{kind}_lines_{side}")) is not None
                    for side in ("added", "removed")
                )
                for key, kind in (("code_size", "source"), ("test_code_size", "test"))
            ),
            "Complexity population": static.get("complexity_complete_for_declared_scope") is True
            and _number(static.get("function_count_reported")) is not None,
            "Coverage covered/total counts": bool(coverage_units)
            and all(
                coverage.get(
                    f"{unit}_population_complete",
                    coverage.get("population_complete_for_declared_scope"),
                )
                is True
                and all(
                    _number(coverage.get(f"{unit}_{key}")) is not None
                    for key in ("total", "covered")
                )
                for unit in coverage_units
            ),
            "Introduced dead findings": _map(comparison.get("dead_findings")).get("status")
            == "available"
            and _number(_map(comparison.get("dead_findings")).get("introduced_count")) is not None,
            "Introduced clone groups": _map(comparison.get("clone_findings")).get("status")
            == "available"
            and _number(_map(comparison.get("clone_findings")).get("introduced_count")) is not None,
        }
        rows.append(
            {
                "attempt_id": attempt["id"],
                "task": attempt["task_id"],
                "arm": attempt.get("arm"),
                "role": "handoff",
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
