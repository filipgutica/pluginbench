"""Assess immutable implementation stages without executing models or analysis tools."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from pluginbench.assessment_metrics import (
    compare_evidence,
    compare_snapshots,
    evaluate_evidence,
    inspect_snapshot,
    scoped_code_size,
    scoped_test_size,
)
from pluginbench.assessment_models import (
    ArtifactRef,
    AssessmentInput,
    Attempt,
    Correction,
    CorrectionLedger,
    CorrectionVerification,
    CorrectnessReceipt,
    LedgerCompleteness,
    ReviewVerification,
    Snapshot,
    Stage,
    Standards,
)
from pluginbench.code_diff import _is_test_path


def _document(path: Path) -> object:
    return yaml.safe_load(path.read_text())


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("assessment evidence must be an object")
    return {key: item for key, item in value.items()}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("assessment paths must be a string list")
    return [item for item in value]


def _artifact(base: Path, reference: ArtifactRef) -> Path:
    path = base / reference.path
    if path.is_symlink():
        raise ValueError(f"assessment artifact is a symlink: {path}")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != reference.sha256:
        raise ValueError(f"assessment artifact digest mismatch: {path}")
    return path.resolve()


def _snapshot(base: Path, reference: ArtifactRef) -> Snapshot:
    path = base / reference.path
    if inspect_snapshot(path)["digest"] != reference.sha256:
        raise ValueError(f"snapshot digest mismatch: {path}")
    return Snapshot(path.resolve(), reference.sha256)


def _snapshot_metadata(snapshot: Snapshot) -> dict[str, object]:
    metadata = inspect_snapshot(snapshot.root)
    if metadata["digest"] != snapshot.digest:
        raise ValueError(f"snapshot changed during assessment: {snapshot.root}")
    return metadata


def _load_manifest(path: Path) -> tuple[str, list[Attempt], Path | None]:
    manifest = AssessmentInput.model_validate(_document(path))
    base = path.parent
    records = []
    for entry in manifest.records:
        stages = []
        for item in entry.stages:
            snapshot = _snapshot(base, item.snapshot) if item.snapshot is not None else None
            correctness = None
            if item.correctness is not None:
                correctness = CorrectnessReceipt.model_validate(
                    _document(_artifact(base, item.correctness))
                )
                if snapshot is None or correctness.snapshot_digest != snapshot.digest:
                    raise ValueError(
                        f"correctness is bound to another snapshot: {entry.id}/{item.id}"
                    )
            evidence: dict[str, str] | None = (
                {kind: str(_artifact(base, ref)) for kind, ref in item.evidence.items()}
                if item.evidence is not None
                else None
            )
            stages.append(Stage(item.id, snapshot, correctness, evidence, item.work))
        ledger_path = _artifact(base, entry.correction_ledger) if entry.correction_ledger else None
        ledger = CorrectionLedger.model_validate(_document(ledger_path)) if ledger_path else None
        records.append(
            Attempt(
                entry.id,
                entry.task_id,
                entry.attempt,
                entry.arm,
                entry.model,
                _snapshot(base, entry.before) if entry.before else None,
                tuple(stages),
                ledger,
                ledger_path.parent if ledger_path else base,
                {
                    "input": "manifest",
                    "manifest": str(path),
                    **(entry.runtime.model_dump() if entry.runtime else {}),
                },
                (
                    {kind: str(_artifact(base, ref)) for kind, ref in entry.before_evidence.items()}
                    if entry.before_evidence is not None
                    else None
                ),
            )
        )
    standards = base / manifest.standards if manifest.standards else None
    return manifest.name, records, standards


def _correctness(receipt: CorrectnessReceipt | None, required: list[str]) -> dict[str, object]:
    if receipt is None:
        return {"status": "unknown", "checks": {}, "reason": "no bound correctness receipt"}
    checks = receipt.checks
    missing = sorted(
        {name for name in required if checks.get(name) is None}
        | {name for name, value in checks.items() if value is None}
    )
    status = (
        "fail"
        if any(value is False for value in checks.values())
        else "unknown"
        if missing
        else "pass"
    )
    return {
        "status": status,
        "checks": checks,
        "missing_required_checks": missing,
        "command": receipt.command,
        "tool": receipt.tool,
        "tool_version": receipt.tool_version,
        "snapshot_digest": receipt.snapshot_digest,
    }


def _review_history(record: Attempt) -> dict[str, object]:
    ledger = record.ledger
    if ledger is None or ledger.review_rounds is None:
        return {"external_rounds": None, "reviewed_stages": None, "unverified_rounds": []}
    rounds = ledger.review_rounds
    order = {stage.id: index for index, stage in enumerate(record.stages)}
    if [review.round for review in rounds] != list(range(1, len(rounds) + 1)):
        raise ValueError(f"review rounds must be contiguous and ordered: {record.id}")
    previous_stage = -1
    reviewed_stages = set()
    unverified_rounds = []
    for review in rounds:
        if review.stage not in ledger.stage_digests or order[review.stage] < previous_stage:
            raise ValueError(f"review rounds must reference ordered stages: {record.id}")
        previous_stage = order[review.stage]
        if review.external_verification is None:
            unverified_rounds.append(review.round)
            continue
        proof = ReviewVerification.model_validate(
            _document(_artifact(record.ledger_base, review.external_verification))
        )
        if (
            proof.round != review.round
            or proof.stage != review.stage
            or proof.issue_ids != review.issue_ids
            or proof.stage_digests != {review.stage: ledger.stage_digests[review.stage]}
        ):
            raise ValueError(f"external review verification mismatch: {record.id}/{review.round}")
        if not proof.independent:
            unverified_rounds.append(review.round)
            continue
        expected_issues = {
            issue.issue_id
            for issue in ledger.corrections
            if order[issue.from_stage] <= order[review.stage]
            and (issue.to_stage is None or order[review.stage] < order[issue.to_stage])
        }
        if set(review.issue_ids) != expected_issues:
            raise ValueError(f"review findings disagree with recorded issues: {record.id}")
        reviewed_stages.add(review.stage)
    for issue in ledger.corrections:
        if issue.round > len(rounds):
            raise ValueError(f"correction references an absent review round: {record.id}")
        origin = rounds[issue.round - 1]
        if origin.stage != issue.from_stage or issue.issue_id not in origin.issue_ids:
            raise ValueError(f"correction disagrees with its origin review: {record.id}")
    return {
        "external_rounds": len(rounds) - len(unverified_rounds),
        "reviewed_stages": sorted(reviewed_stages, key=order.__getitem__),
        "unverified_rounds": unverified_rounds,
    }


def _burden(record: Attempt) -> dict[str, object]:
    if record.ledger is None:
        return {
            "status": "unavailable",
            "verified_issue_count": None,
            "verified_correction_events": None,
            "external_rounds": None,
            "recorded_self_cleanup": None,
            "reason": "no correction ledger; independent attempts are not corrections",
        }
    ledger = record.ledger
    digests = {stage.id: stage.snapshot.digest for stage in record.stages if stage.snapshot}
    if ledger.stage_digests != digests:
        raise ValueError(f"correction ledger stage digest mismatch: {record.id}")
    order = {stage.id: index for index, stage in enumerate(record.stages)}

    def ordered(first: str, last: str) -> None:
        if first not in order or last not in order or order[first] >= order[last]:
            raise ValueError(f"correction must reference ordered stages: {record.id}")

    verified: list[Correction] = []
    reviewers: dict[tuple[str, int], str] = {}
    unverified = []
    seen = set()
    for issue in ledger.corrections:
        if issue.from_stage not in digests or (
            issue.to_stage is not None and issue.to_stage not in digests
        ):
            raise ValueError(f"correction references an absent stage: {record.id}")
        if issue.to_stage is not None:
            ordered(issue.from_stage, issue.to_stage)
        key = (issue.issue_id, issue.round)
        if key in seen:
            raise ValueError(f"duplicate correction issue/round: {record.id}")
        seen.add(key)
        if issue.external_verification is None:
            unverified.append(issue.issue_id)
            continue
        proof = CorrectionVerification.model_validate(
            _document(_artifact(record.ledger_base, issue.external_verification))
        )
        fields = ("issue_id", "round", "from_stage", "to_stage", "category")
        expected = {
            name: digests[name] for name in (issue.from_stage, issue.to_stage) if name is not None
        }
        if (
            any(getattr(proof, name) != getattr(issue, name) for name in fields)
            or proof.stage_digests != expected
        ):
            raise ValueError(
                f"external correction verification mismatch: {record.id}/{issue.issue_id}"
            )
        if proof.independent:
            verified.append(issue)
            reviewers[key] = proof.reviewer
        else:
            unverified.append(issue.issue_id)
    for cleanup in ledger.self_cleanup:
        ordered(cleanup.from_stage, cleanup.to_stage)
    reviews = _review_history(record)
    review_scope = reviews["reviewed_stages"]
    completed_stages: set[str] = set()
    if ledger.completeness is not None:
        proof_complete = LedgerCompleteness.model_validate(
            _document(_artifact(record.ledger_base, ledger.completeness))
        )
        if proof_complete.stage_digests != digests:
            raise ValueError(f"ledger completeness digest mismatch: {record.id}")
        if proof_complete.reviewed_stages is not None:
            review_scope = proof_complete.reviewed_stages
            if not set(review_scope) <= set(digests) or (
                reviews["reviewed_stages"] is not None
                and not set(review_scope) <= set(_strings(reviews["reviewed_stages"]))
            ):
                raise ValueError(f"ledger completeness review scope mismatch: {record.id}")
        if (
            proof_complete.independent
            and proof_complete.complete
            and not unverified
            and not reviews["unverified_rounds"]
        ):
            completed_stages = set(digests if review_scope is None else _strings(review_scope))
    complete = completed_stages == set(digests)
    outstanding: dict[str, list[str] | None] = {
        stage.id: sorted(
            {
                issue.issue_id
                for issue in verified
                if order[issue.from_stage] <= order[stage.id]
                and (issue.to_stage is None or order[stage.id] < order[issue.to_stage])
            }
        )
        for stage in record.stages
    }
    for stage_id, known in outstanding.items():
        if stage_id not in completed_stages and not known:
            outstanding[stage_id] = None
    lower_bounds = not complete or bool(unverified) or bool(reviews["unverified_rounds"])
    return {
        "status": "incomplete" if lower_bounds else "available",
        "verified_issue_count": len({issue.issue_id for issue in verified}),
        "verified_correction_events": sum(issue.to_stage is not None for issue in verified),
        **reviews,
        "recorded_self_cleanup": len(ledger.self_cleanup),
        "outstanding_by_stage": outstanding,
        "verified_issues": [
            {**issue.model_dump(mode="json"), "reviewer": reviewers[issue.issue_id, issue.round]}
            for issue in verified
        ],
        "unverified_issue_ids": unverified,
        "completeness_verified": complete,
        "completeness_by_stage": {
            stage.id: stage.id in completed_stages for stage in record.stages
        },
        "counts_are_lower_bounds": lower_bounds,
        "method": "Explicit ledger and independent verification receipts; no reverted edits inferred",
    }


def _code_size(
    record: Attempt,
    stage: Stage,
    before_metrics: dict[str, object],
    metrics: dict[str, object],
    change: dict[str, object] | None,
) -> dict[str, object]:
    kinds = {
        kind
        for kind in ("fallow", "coverage")
        if kind in (record.before_evidence or {}) or kind in (stage.evidence or {})
    }
    if not kinds:
        return {"status": "not configured", "reason": "no declared source measurement scope"}
    scopes = []
    for kind in sorted(kinds):
        for measured in (before_metrics, metrics):
            scope = _mapping(_mapping(measured.get(kind, {})).get("provenance", {})).get("scope")
            if (
                not isinstance(scope, list)
                or not scope
                or any(not isinstance(pattern, str) or not pattern for pattern in scope)
            ):
                return {"status": "unavailable", "reason": f"missing bound {kind} source scope"}
            scopes.append(sorted({str(pattern).removeprefix("./") for pattern in scope}))
    if any(scope != scopes[0] for scope in scopes):
        return {"status": "unavailable", "reason": "bound source measurement scopes disagree"}
    if record.before is None or stage.snapshot is None or change is None:
        return {"status": "unavailable", "reason": "source-size comparison needs both snapshots"}
    try:
        return scoped_code_size(
            before=record.before.root, after=stage.snapshot.root, scope=scopes[0], change=change
        )
    except ValueError as error:
        return {"status": "unavailable", "reason": str(error)}


def _test_code_size(
    record: Attempt,
    stage: Stage,
    before_metrics: dict[str, object],
    metrics: dict[str, object],
    change: dict[str, object] | None,
) -> dict[str, object]:
    if "coverage" not in (record.before_evidence or {}) and "coverage" not in (
        stage.evidence or {}
    ):
        return {"status": "not configured", "reason": "no bound visible-test scope"}
    scopes = []
    for measured in (before_metrics, metrics):
        scope = _mapping(_mapping(measured.get("coverage", {})).get("metrics") or {}).get(
            "test_scope"
        )
        if (
            not isinstance(scope, list)
            or not scope
            or any(not isinstance(pattern, str) or not pattern for pattern in scope)
        ):
            return {"status": "unavailable", "reason": "missing bound visible-test scope"}
        scopes.append(sorted({str(pattern).removeprefix("./") for pattern in scope}))
    if scopes[0] != scopes[1]:
        return {"status": "unavailable", "reason": "bound visible-test scopes disagree"}
    if record.before is None or stage.snapshot is None or change is None:
        return {"status": "unavailable", "reason": "test-size comparison needs both snapshots"}
    try:
        return scoped_test_size(
            before=record.before.root, after=stage.snapshot.root, scope=scopes[0], change=change
        )
    except ValueError as error:
        return {"status": "unavailable", "reason": str(error)}


def _stage(
    record: Attempt,
    stage: Stage,
    standards: Standards | None,
    base: Path,
    before_metrics: dict[str, object],
) -> dict[str, object]:
    required = standards.required_correctness_checks if standards else ["behavior"]
    correctness = _correctness(stage.correctness, required)
    if stage.snapshot is None:
        return {
            "id": stage.id,
            "snapshot_digest": None,
            "snapshot_metadata": None,
            "correctness": correctness,
            "standards": {"status": "unknown", "reason": "submission snapshot unavailable"},
            "selected_checks_pass": None,
            "work": stage.work.model_dump() if stage.work else None,
            "metrics": None,
            "change": None,
        }
    change = compare_snapshots(record.before.root, stage.snapshot.root) if record.before else None
    metrics = evaluate_evidence(
        stage.snapshot.root,
        stage.evidence,
        {kind: limits for kind, limits in standards.metrics.items()} if standards else {},
        base,
    )
    contribution = _mapping(
        _mapping(_mapping(metrics.get("coverage", {})).get("metrics") or {}).get(
            "test_contribution", {}
        )
    )
    if contribution.get("status") == "available" and (
        change is None or contribution.get("before_digest") != change["before_digest"]
    ):
        raise ValueError(
            "test-contribution selection does not bind to this attempt's before snapshot"
        )
    if contribution.get("status") == "available":
        assert change is not None
        coverage = _mapping(_mapping(metrics["coverage"])["metrics"])
        scope = _strings(coverage["test_scope"])
        excluded = set(_strings(coverage["excluded_test_files"]))
        present = set(_mapping(inspect_snapshot(stage.snapshot.root)["files"]))
        selected = {
            name
            for name in _strings(change["changed_paths"])
            if name in present
            and name not in excluded
            and _is_test_path(name)
            and any(fnmatch.fnmatchcase(name, pattern) for pattern in scope)
        }
        if selected != set(_strings(contribution["changed_test_files"])):
            raise ValueError("test-contribution changed-file selection differs from the bound diff")
        if coverage.get("selection_policy") == "fixed-reference-plus-all-changed-test-files-v1":
            required_tests = set(_strings(coverage["reference_test_files"]))
            if not required_tests or not required_tests <= present - excluded:
                raise ValueError("required coverage reference files missing or excluded")
            if set(_strings(coverage["selected_test_files"])) != required_tests | selected:
                raise ValueError(
                    "coverage task corpus differs from its fixed reference and bound diff"
                )
    violations: list[str] = []
    unknown: list[str] = []
    if standards is None:
        unknown.append("no standards supplied")
    else:
        if standards.allowed_paths is not None and change is None:
            unknown.append("allowed paths cannot be checked without a before snapshot")
        elif standards.allowed_paths is not None and change is not None:
            violations.extend(
                f"out of scope: {name}"
                for name in _strings(change["changed_paths"])
                if not any(
                    fnmatch.fnmatchcase(name, pattern) for pattern in standards.allowed_paths
                )
            )
        kinds = set(standards.required_metrics) | {
            kind for kind, limits in standards.metrics.items() if limits
        }
        for kind in sorted(kinds):
            metric = _mapping(metrics[kind])
            if metric["status"] != "available":
                unknown.append(f"required {kind} evidence {metric['status']}")
            unknown.extend(
                f"{kind}: {value}" for value in _strings(metric.get("unknown_thresholds", []))
            )
            violations.extend(
                f"{kind}: {value}" for value in _strings(metric.get("violations", []))
            )
    status = "fail" if violations else "unknown" if unknown else "pass"
    accepted = (
        False
        if correctness["status"] == "fail" or status == "fail"
        else (True if correctness["status"] == status == "pass" else None)
    )
    return {
        "id": stage.id,
        "snapshot_digest": stage.snapshot.digest,
        "snapshot_metadata": _snapshot_metadata(stage.snapshot),
        "correctness": correctness,
        "standards": {"status": status, "violations": violations, "unavailable": unknown},
        "selected_checks_pass": accepted,
        "work": stage.work.model_dump() if stage.work else None,
        "change": change,
        "metrics": metrics,
        "code_size": _code_size(record, stage, before_metrics, metrics, change),
        "test_code_size": _test_code_size(record, stage, before_metrics, metrics, change),
        "metric_comparison": compare_evidence(before_metrics, metrics, change=change),
    }


def build_assessment(input_path: Path, standards_path: Path | None = None) -> dict[str, Any]:
    """Read a native study manifest; never invoke auth or tools."""
    source = input_path.expanduser().resolve()
    name, records, default_standards = _load_manifest(source)
    chosen = standards_path.expanduser().resolve() if standards_path else default_standards
    standards = Standards.model_validate(_document(chosen)) if chosen else None
    attempts = []
    for record in records:
        base = source if source.is_dir() else source.parent
        before_metrics = (
            evaluate_evidence(
                record.before.root,
                record.before_evidence,
                {kind: limits for kind, limits in standards.metrics.items()} if standards else {},
                base,
            )
            if record.before
            else {}
        )
        stages = [_stage(record, stage, standards, base, before_metrics) for stage in record.stages]
        burden = _burden(record)
        outstanding = _mapping(burden.get("outstanding_by_stage", {}))
        for stage in stages:
            known = outstanding.get(str(stage["id"]))
            issues = _strings(known) if known is not None else None
            stage["outstanding_verified_issues"] = issues
            reviewed = burden.get("reviewed_stages") is None or str(stage["id"]) in _strings(
                burden["reviewed_stages"]
            )
            complete = _mapping(burden.get("completeness_by_stage", {})).get(str(stage["id"]))
            if stage["selected_checks_pass"] is False or (issues and reviewed):
                stage["accepted"] = False
            elif complete is True and reviewed:
                stage["accepted"] = stage["selected_checks_pass"]
            else:
                stage["accepted"] = None
        first_acceptance = next(
            (index for index, stage in enumerate(stages) if stage["accepted"] is True), None
        )
        work_to_acceptance = None
        if (
            first_acceptance is not None
            and burden["status"] == "available"
            and all(
                _mapping(stage["correctness"])["status"] != "unknown"
                and _mapping(stage["standards"])["status"] != "unknown"
                for stage in stages[: first_acceptance + 1]
            )
        ):
            elapsed = [
                stage.work.elapsed_seconds if stage.work else None
                for stage in record.stages[: first_acceptance + 1]
            ]
            if all(value is not None for value in elapsed):
                work_to_acceptance = {
                    "stage": stages[first_acceptance]["id"],
                    "elapsed_seconds": sum(value for value in elapsed if value is not None),
                }
        attempts.append(
            {
                "id": record.id,
                "task_id": record.task_id,
                "attempt": record.attempt,
                "arm": record.arm,
                "model": record.model,
                "before_digest": record.before.digest if record.before else None,
                "before_snapshot_metadata": _snapshot_metadata(record.before)
                if record.before
                else None,
                "before_metrics": before_metrics,
                "configured_metrics": sorted(
                    set(record.before_evidence or {})
                    | {kind for stage in record.stages for kind in (stage.evidence or {})}
                ),
                "stages": stages,
                "handoff": stages[0]["id"],
                "final": stages[-1]["id"],
                "correction_burden": burden,
                "work_to_acceptance": work_to_acceptance,
                "provenance": record.provenance,
            }
        )
    return {
        "schema_version": 1,
        "name": name,
        "input": str(source),
        "snapshot_roots": sorted(
            {
                str(snapshot.root)
                for record in records
                for snapshot in (record.before, *(stage.snapshot for stage in record.stages))
                if snapshot is not None
            }
        ),
        "standards": standards.model_dump(mode="json") if standards else None,
        "attempts": attempts,
        "limitations": [
            "Every attempt is retained; independent repeats are not correction stages.",
            "Bound receipts are supplied evidence, not new tool execution or proof of causality.",
            "No composite score or semantic test-redundancy claim is inferred.",
            "Missing ledger, metric, usage, or correctness evidence remains unavailable.",
        ],
    }


_DISPLAY_METRICS = {
    "fallow": (
        ("max_cognitive_reported", "cognitive"),
        ("max_cyclomatic_reported", "cyclomatic"),
        ("max_unit_size_reported", "unit size"),
        ("dead_code", "dead"),
        ("clone_groups", "clones"),
        ("cycles", "cycles"),
        ("boundary_violations", "boundaries"),
    ),
    "coverage": (("branch_percent", "branch %"), ("function_percent", "function %")),
    "mutation": (("score_percent", "mutation %"),),
}


def _fallow_change_summary(comparison: dict[str, object]) -> str:
    changed = _mapping(comparison.get("changed_functions", {}))
    new = _mapping(comparison.get("new_file_functions", {}))
    dead = _mapping(comparison.get("dead_findings", {}))

    def population(value: object) -> str:
        metrics = _mapping(value)
        values = [
            metrics.get(key)
            for key in (
                "function_count_reported",
                "max_cognitive_reported",
                "max_cyclomatic_reported",
                "max_unit_size_reported",
            )
        ]
        return "/".join(str(item) if item is not None else "unknown" for item in values)

    introduced, resolved = (dead.get(key) for key in ("introduced_count", "resolved_count"))
    return (
        f"changed functions count/max cognitive/cyclomatic/lines {population(changed.get('before', {}))} → {population(changed.get('after', {}))}"
        + (" (reported only)" if changed.get("status") == "incomplete" else "")
        + f"; new-file functions {population(new.get('after', {}))}"
        + (" (reported only)" if new.get("status") == "incomplete" else "")
        + f"; dead introduced/resolved {introduced if introduced is not None else 'unknown'}/{resolved if resolved is not None else 'unknown'}"
    )


def _metric_summary(
    kind: str, measurement: dict[str, object], comparison: dict[str, object]
) -> str:
    if measurement.get("metrics") is None:
        return f"{kind}: unknown"
    metrics = _mapping(measurement["metrics"])
    deltas = _mapping(comparison["deltas"]) if comparison["status"] == "available" else {}
    values = []
    for key, label in _DISPLAY_METRICS[kind]:
        value, delta = metrics.get(key), deltas.get(key)
        rendered = f"{value:g}" if isinstance(value, (int, float)) else "unknown"
        change = f"{delta:+g}" if isinstance(delta, (int, float)) else "unknown"
        values.append(f"{label} {rendered} (Δ {change})")
    prefix = f"{kind} incomplete; reported " if measurement["status"] == "incomplete" else ""
    summary = prefix + ", ".join(values)
    return summary + "; " + _fallow_change_summary(comparison) if kind == "fallow" else summary


def write_assessment(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    from pluginbench.assessment_charts import chart_artifacts, chart_paths

    for snapshot in _strings(report["snapshot_roots"]):
        if out_dir.resolve().is_relative_to(Path(snapshot).resolve()):
            raise ValueError("assessment output must be outside every input snapshot")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = out_dir / "assessment-report.json", out_dir / "assessment-report.md"
    html_path, csv_path = chart_paths(out_dir)
    if any(path.is_symlink() for path in (json_path, markdown_path, html_path, csv_path)):
        raise ValueError("assessment output files must not be symlinks")
    chart_html, chart_csv = chart_artifacts(report)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    kinds = [
        kind
        for kind in _DISPLAY_METRICS
        if any(
            stage["metrics"] and stage["metrics"][kind]["status"] != "unavailable"
            for attempt in report["attempts"]
            for stage in attempt["stages"]
        )
    ]
    review = report.get("independent_review")
    selected_column = " Selected review findings |" if isinstance(review, dict) else ""
    lines = [
        f"# Standards assessment: {report['name']}",
        "",
        f"{len(report['attempts'])} recorded attempts. Unknown evidence is not a pass. Full identities and receipts are in JSON.",
        "",
        f"| Task / arm / model | Attempt/stage | Correctness | Standards | Accepted | Source LOC +/- | Test LOC +/- | Metrics |{selected_column} Recorded ledger issues/resolutions/rounds |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        + (" --- |" if selected_column else ""),
    ]
    progress = report.get("study_progress")
    if isinstance(progress, dict):
        lines[2:2] = [
            f"Study {progress['status']}: {progress['completed_attempts']}/{progress['assigned_attempts']} attempts finished; {progress['native_invocations']} native invocation requests (startup may fail before a model call).",
            "",
        ]
    if isinstance(review, dict):
        lines[2:2] = [
            f"Independent AI review of {review.get('stage', 'handoff')} submissions {review['status']}: {review['native_invocation_requests']} judge requests with {review['requested_model']}/{review['requested_reasoning_effort']}. Selected review findings apply only to the reviewed stage; recorded ledger counts retain prior review and correction history. Judge usage is separate in JSON; these findings are judgments, not deterministic defect counts.",
            "",
        ]
    for attempt in report["attempts"]:
        for stage in attempt["stages"]:
            change = stage["change"]
            code_size = stage.get("code_size", {})
            if code_size.get("status") == "available":
                source_loc = f"+{code_size['source_lines_added']}/-{code_size['source_lines_removed']} under {', '.join(code_size['scope'])}"
            elif code_size.get("status") == "unavailable":
                source_loc = f"unknown: {code_size['reason']}"
            else:
                source_loc = (
                    f"+{change['source_lines_added']}/-{change['source_lines_removed']} extension-classified, including generated files"
                    if change
                    else "unknown"
                )
            test_size = stage.get("test_code_size", {})
            test_loc = (
                f"+{test_size['test_lines_added']}/-{test_size['test_lines_removed']} under {', '.join(test_size['scope'])}"
                if test_size.get("status") == "available"
                else f"unknown: {test_size['reason']}"
                if test_size.get("status") == "unavailable"
                else f"+{change['test_lines_added']}/-{change['test_lines_removed']} extension-classified, including generated files"
                if change
                else "unknown"
            )
            measured = []
            for kind in kinds:
                measurement: dict[str, object] = (
                    _mapping(stage["metrics"][kind])
                    if stage["metrics"]
                    else {"status": "unavailable"}
                )
                comparison = stage.get("metric_comparison", {}).get(kind, {"status": "unavailable"})
                measured.append(_metric_summary(kind, measurement, _mapping(comparison)))
            burden = attempt["correction_burden"]
            counts = (
                "unknown"
                if burden["status"] == "unavailable"
                else (
                    "/".join(
                        str(burden[key]) if burden[key] is not None else "unknown"
                        for key in (
                            "verified_issue_count",
                            "verified_correction_events",
                            "external_rounds",
                        )
                    )
                    + (" lower bound" if burden["status"] == "incomplete" else "")
                )
            )
            label = attempt["id"][:8] if len(attempt["id"]) == 32 else attempt["id"]
            values = [
                f"{attempt['task_id']} / {attempt['arm'] or 'unknown'} / {attempt['model'] or 'unknown'}",
                f"{label}/{stage['id']} ({attempt['provenance'].get('attempt_status', 'unknown')})",
                stage["correctness"]["status"],
                stage["standards"]["status"],
                "unknown" if stage["accepted"] is None else str(stage["accepted"]).lower(),
                source_loc,
                test_loc,
                "; ".join(measured) or "—",
            ]
            if isinstance(review, dict):
                selected = attempt.get("independent_review", {})
                selected_stage = selected.get(
                    "stage_id", attempt.get(review.get("stage", "handoff"))
                )
                values.append(
                    "not selected"
                    if selected_stage is not None and selected_stage != stage["id"]
                    else selected.get("finding_count", "unknown")
                    if selected.get("status") == "reviewed"
                    and selected.get("finding_count") is not None
                    else "unknown"
                )
            values.append(counts)
            lines.append(
                "| "
                + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values)
                + " |"
            )
    lines.extend(
        [
            "",
            "LOC is measured against before; metric deltas require matching tool/config/scope and fixed denominators.",
            "Changed-function counts and raw maxima are separate before/after hunk-overlap populations, not matched functions. New-file functions are reported separately. Dead introduced/resolved findings require compatible complete static graphs and declaration identities; neither proves semantic deadness.",
            "",
        ]
    )
    lines.extend(report["limitations"])
    absent = [kind for kind in _DISPLAY_METRICS if kind not in kinds]
    if absent:
        lines.append(f"Unavailable throughout this study: {', '.join(absent)}.")
    markdown_path.write_text("\n".join(lines) + "\n")
    html_path.write_text(chart_html)
    csv_path.write_text(chart_csv)
    return json_path, markdown_path
