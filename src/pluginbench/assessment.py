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
    CorrectnessReceipt,
    Snapshot,
    Stage,
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


def _load_manifest(path: Path) -> tuple[str, list[Attempt]]:
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
        records.append(
            Attempt(
                entry.id,
                entry.task_id,
                entry.attempt,
                entry.arm,
                entry.model,
                _snapshot(base, entry.before) if entry.before else None,
                tuple(stages),
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
    return manifest.name, records


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
    base: Path,
    before_metrics: dict[str, object],
) -> dict[str, object]:
    correctness = _correctness(stage.correctness, ["behavior"])
    if stage.snapshot is None:
        return {
            "id": stage.id,
            "snapshot_digest": None,
            "snapshot_metadata": None,
            "correctness": correctness,
            "work": stage.work.model_dump() if stage.work else None,
            "metrics": None,
            "change": None,
        }
    change = compare_snapshots(record.before.root, stage.snapshot.root) if record.before else None
    metrics = evaluate_evidence(
        stage.snapshot.root,
        stage.evidence,
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
    return {
        "id": stage.id,
        "snapshot_digest": stage.snapshot.digest,
        "snapshot_metadata": _snapshot_metadata(stage.snapshot),
        "correctness": correctness,
        "work": stage.work.model_dump() if stage.work else None,
        "change": change,
        "metrics": metrics,
        "code_size": _code_size(record, stage, before_metrics, metrics, change),
        "test_code_size": _test_code_size(record, stage, before_metrics, metrics, change),
        "metric_comparison": compare_evidence(before_metrics, metrics, change=change),
    }


def build_assessment(input_path: Path) -> dict[str, Any]:
    """Read a native study manifest; never invoke auth or tools."""
    source = input_path.expanduser().resolve()
    name, records = _load_manifest(source)
    attempts = []
    for record in records:
        base = source.parent
        before_metrics = (
            evaluate_evidence(
                record.before.root,
                record.before_evidence,
                base,
            )
            if record.before
            else {}
        )
        stages = [_stage(record, stage, base, before_metrics) for stage in record.stages]
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
        "attempts": attempts,
        "limitations": [
            "Every attempt is retained; independent repeats are not correction stages.",
            "Bound receipts are supplied evidence, not new tool execution or proof of causality.",
            "No composite score or semantic test-redundancy claim is inferred.",
            "Missing metric, usage, or correctness evidence remains unavailable.",
        ],
    }


def write_assessment(report: dict[str, Any], out_dir: Path) -> Path:
    """Persist the canonical assessment consumed by the public report renderer."""
    out_dir = out_dir.expanduser().resolve()
    if any(out_dir.is_relative_to(Path(root)) for root in report["snapshot_roots"]):
        raise ValueError("assessment output must be outside every input snapshot")
    json_path = out_dir / "assessment-report.json"
    if json_path.is_symlink():
        raise ValueError("assessment output cannot overwrite a symlink")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    return json_path
