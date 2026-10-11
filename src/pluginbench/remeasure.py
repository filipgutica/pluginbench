"""Propose repeatable offline observer revisions without replacing native study evidence."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from threading import Event
from pathlib import Path

from pluginbench import study
from pluginbench.parallel import run_jobs, validate_jobs
from pluginbench.assessment import _load_manifest, build_assessment
from pluginbench.assessment_metrics import evaluate_evidence, inspect_snapshot
from pluginbench.assessment_models import ArtifactRef, AssessmentInput, MetricKind
from pluginbench.collectors import (
    Collector,
    collect_measurements,
    collector_identity,
    freeze_collectors,
    resolve_collector_command,
)


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("remeasurement requires a JSON object")
    return dict(value)


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted(
            (_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
        )
    return value


def _normalized(measurement: dict[str, object]) -> dict[str, object]:
    # Evaluated metrics include counts, identities, denominators, populations and
    # limitations. Raw execution paths/timing are intentionally not compared.
    return {
        kind: {
            key: _canonical(_object(value).get(key))
            for key in ("status", "provenance", "metrics", "violations", "unknown_standards")
        }
        for kind, value in measurement.items()
        if kind != "snapshot_digest"
    }


@dataclass
class CollectedInput:
    repeats: list[dict[MetricKind, ArtifactRef]]
    outcomes: dict[str, object]


@dataclass(frozen=True)
class MeasurementInput:
    index: int
    key: tuple[str, str, str]
    stage: str
    snapshot: Path
    snapshot_digest: str
    before: Path
    before_digest: str


def _family_outcomes(
    normalized: list[dict[str, object]],
    repeats: list[dict[MetricKind, ArtifactRef]],
) -> dict[str, object]:
    outcomes: dict[str, object] = {}
    for kind in repeats[0]:
        states = [
            "available" if refs[kind].path.name == "envelope.json" else "unavailable"
            for refs in repeats
        ]
        matching = (
            states == ["available", "available"] and normalized[0][kind] == normalized[1][kind]
        )
        status = (
            "available"
            if matching
            else ("nonrepeatable" if "available" in states else "unavailable")
        )
        outcomes[kind] = {
            "status": status,
            "repeat_states": states,
            "reason": None
            if matching
            else (
                "collector unavailable in both repeats"
                if status == "unavailable"
                else "two valid matching measurements were not obtained"
            ),
        }
    return outcomes


def _validate_failure(
    *,
    path: Path,
    collector: Collector,
    snapshot_digest: str,
    before_digest: str,
    repeat: int,
) -> None:
    failure = _object(json.loads(path.read_text()))
    if set(failure) != {
        "schema_version",
        "status",
        "reason",
        "snapshot_digest",
        "before_digest",
        "collector",
        "execution",
        "repeat_index",
    }:
        raise ValueError("invalid collector failure receipt fields")
    if (
        type(failure["schema_version"]) is not int
        or failure["schema_version"] != 1
        or failure["status"] != "unavailable"
        or failure["snapshot_digest"] != snapshot_digest
        or failure["before_digest"] != before_digest
        or type(failure["repeat_index"]) is not int
        or failure["repeat_index"] != repeat
        or failure["collector"] != collector_identity(collector)
        or not isinstance(failure["reason"], str)
        or not failure["reason"]
    ):
        raise ValueError("collector failure differs from its bound source/profile/repeat")
    execution = _object(failure["execution"])
    if execution.get("path") != "execution.json":
        raise ValueError("collector failure must bind its own execution receipt")
    receipt = path.parent / "execution.json"
    if execution != {"path": "execution.json", "sha256": study._digest(receipt)}:
        raise ValueError("collector failure execution receipt changed")
    executions = json.loads(receipt.read_text())
    if not isinstance(executions, list):
        raise ValueError("collector failure execution receipt must be a list")
    if collector.failure_protocol is not None and (
        len(executions) != 1 or _object(executions[0]).get("exit_code") != 3
    ):
        raise ValueError("trusted coverage adapter failure lacks a classified unavailable exit")


def _verify_native_references(root: Path, value: object, seen: set[Path]) -> None:
    if isinstance(value, list):
        for item in value:
            _verify_native_references(root, item, seen)
    elif isinstance(value, dict):
        if set(value) == {"path", "sha256"}:
            path = study._bound_path(root, value)
            if path in seen:
                return
            seen.add(path)
            if path.suffix == ".json":
                document = json.loads(path.read_text())
                # Assessment refs belong to that manifest, and include directories
                # and locally rooted ledger proofs. Their loader checks them below.
                if isinstance(document, dict) and "name" in document and "records" in document:
                    return
                _verify_native_references(root, document, seen)
        else:
            for item in value.values():
                _verify_native_references(root, item, seen)


def _native_state(root: Path) -> tuple[dict[str, object], AssessmentInput, Path]:
    study._assert_frozen(root)
    run = _object(json.loads((root / "run.json").read_text()))
    states = _object(run.get("attempt_states"))
    if (
        run.get("status") != "complete"
        or not states
        or any(value != "complete" for value in states.values())
    ):
        raise ValueError("remeasurement requires every native attempt to be complete")
    original = study._bound_path(root, run["assessment_input"])
    manifest = AssessmentInput.model_validate_json(original.read_text())
    plan = study._bound_document(root, run["plan"])
    entries = {entry["id"]: entry for entry in plan["entries"]}
    if set(states) != {record.id for record in manifest.records} or set(entries) != set(states):
        raise ValueError("native attempt identities differ from their frozen plan")
    for record in manifest.records:
        if any(
            getattr(record, key) != entries[record.id][key] for key in ("task_id", "arm", "attempt")
        ):
            raise ValueError("native assignment differs from its frozen plan")
        if record.before is None:
            raise ValueError("remeasurement requires a saved starting snapshot")
        for stage in record.stages:
            executions = _object(_object(run.get("executions")).get(record.id))
            study._bound_document(root, executions[stage.id])
    study._bound_document(root, run["runtime_preflight"])
    if run.get("calibration"):
        study._bound_document(root, run["calibration"])
    if (root / "review").exists():
        state_path = root / "review/review-state.json"
        if run.get("review") is None or not state_path.is_file():
            raise ValueError("staged native review must finish before remeasurement")
        staged = _object(json.loads(state_path.read_text()))
        if staged.get("status") not in {"complete", "incomplete", "interrupted"}:
            raise ValueError("staged native review is active")
    if run.get("review"):
        review = study._bound_document(root, run["review"])
        if review.get("status") not in {"complete", "incomplete", "interrupted"}:
            raise ValueError("native review is active")
    _verify_native_references(root, run, set())
    # Validate old source, correctness, metric-envelope and ledger bindings too.
    _load_manifest(original)
    build_assessment(original)
    return run, manifest, original


def _preserved_inventory(root: Path, manifest: AssessmentInput, base: Path) -> dict[str, object]:
    files: dict[str, str] = {}
    snapshots: dict[str, str] = {}
    for name in ("frozen", "review", "observer-revision", "report"):
        directory = root / name
        if directory.exists():
            files.update(
                {f"{name}/{path}": digest for path, digest in study._inventory(directory).items()}
            )
    for name in ("run.json", "frozen-inventory.json"):
        files[name] = study._digest(root / name)
    for standards_path in [manifest.standards] if manifest.standards else []:
        path = (base / standards_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("standards must remain inside the native study")
        files[path.relative_to(root).as_posix()] = study._digest(path)
    for record in manifest.records:
        for reference in (record.before, *(stage.snapshot for stage in record.stages)):
            if reference is None:
                continue
            path = (base / reference.path).resolve()
            if not path.is_relative_to(root):
                raise ValueError("snapshot must remain inside the native study")
            snapshots[path.relative_to(root).as_posix()] = str(inspect_snapshot(path)["digest"])
        references = [record.correction_ledger, *(stage.correctness for stage in record.stages)]
        references += list((record.before_evidence or {}).values())
        for stage in record.stages:
            references += list((stage.evidence or {}).values())
        for reference in references:
            if reference is not None:
                path = (base / reference.path).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("native artifact must remain inside the native study")
                files[path.relative_to(root).as_posix()] = study._digest(path)
    # These native receipts are not necessarily under review/report directories.
    run = _object(json.loads((root / "run.json").read_text()))
    for group in ("executions", "verifier_results", "submission_errors", "invocation_starts"):
        for stages in _object(run.get(group, {})).values():
            for receipt_ref in _object(stages).values():
                path = study._bound_path(root, receipt_ref)
                files[path.relative_to(root).as_posix()] = study._digest(path)
    for key in (
        "assessment_input",
        "plan",
        "runtime_preflight",
        "calibration",
        "review",
        "observer_revision",
    ):
        if run.get(key):
            path = study._bound_path(root, run[key])
            files[path.relative_to(root).as_posix()] = study._digest(path)
    return {"files": files, "snapshots": snapshots}


def _rebased(manifest: AssessmentInput, base: Path, destination: Path) -> AssessmentInput:
    revised = manifest.model_copy(deep=True)

    def rebase(reference: ArtifactRef) -> ArtifactRef:
        return reference.model_copy(
            update={"path": Path(os.path.relpath((base / reference.path).resolve(), destination))}
        )

    if revised.standards:
        revised.standards = Path(os.path.relpath((base / revised.standards).resolve(), destination))
    for record in revised.records:
        record.before = rebase(record.before) if record.before else None
        record.correction_ledger = (
            rebase(record.correction_ledger) if record.correction_ledger else None
        )
        record.before_evidence = (
            {kind: rebase(ref) for kind, ref in record.before_evidence.items()}
            if record.before_evidence
            else None
        )
        for stage in record.stages:
            stage.snapshot = rebase(stage.snapshot) if stage.snapshot else None
            stage.correctness = rebase(stage.correctness) if stage.correctness else None
            stage.evidence = (
                {kind: rebase(ref) for kind, ref in stage.evidence.items()}
                if stage.evidence
                else None
            )
    return AssessmentInput.model_validate(revised.model_dump())


def load_profiles(path: Path) -> dict[str, list[Collector]]:
    """Load a JSON task-to-collector mapping with the study's path semantics."""
    source = path.resolve()
    document = _object(json.loads(source.read_text()))
    profiles = {}
    for task_id, definitions in document.items():
        if not task_id or not isinstance(definitions, list):
            raise ValueError("profiles must map task IDs to collector lists")
        collectors = [Collector.model_validate(value) for value in definitions]
        for collector in collectors:
            collector.config = study._resolve(source.parent, collector.config)
            for field in ("command", "syntax_command"):
                command = getattr(collector, field)
                if command:
                    setattr(collector, field, resolve_collector_command(source.parent, command))
        profiles[task_id] = collectors
    return profiles


def propose_remeasurement(
    root: Path,
    *,
    profiles: dict[str, list[Collector]],
    revision: str,
    progress: Callable[[str], None] | None = None,
    allow_partial: bool = False,
    jobs: int = 1,
) -> Path:
    """Write a separate proposal; exceptions retain diagnostics and never promote it."""
    validate_jobs(jobs)
    root = root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", revision):
        raise ValueError("revision must be a single named directory")
    parent = root / "observer-revisions"
    if (
        parent.is_symlink()
        or parent.resolve() != parent
        or (parent.exists() and not parent.is_dir())
    ):
        raise ValueError(
            "measurement revision parent must be a real directory inside its native study"
        )
    if not profiles or any(
        not collectors or len({item.kind for item in collectors}) != len(collectors)
        for collectors in profiles.values()
    ):
        raise ValueError("each task needs nonempty, uniquely named collectors")
    run, manifest, original = _native_state(root)
    if set(profiles) != {record.task_id for record in manifest.records}:
        raise ValueError("replacement profiles must cover every native task exactly")
    protected = _preserved_inventory(root, manifest, original.parent)
    directory = parent / revision
    directory.mkdir(parents=True, exist_ok=False)
    state: dict[str, object] = {
        "schema_version": 2 if allow_partial else 1,
        "status": "collecting",
        "jobs": jobs,
        "revision": revision,
        "source_run": study._reference(root, root / "run.json"),
        "source_assessment": run["assessment_input"],
        "preserved": protected,
    }
    try:
        previous = directory / "previous"
        previous.mkdir()
        shutil.copy2(root / "run.json", previous / "run.json")
        shutil.copy2(original, previous / "assessment-input.json")
        if (root / "report").is_dir():
            shutil.copytree(root / "report", previous / "report")
        frozen = {
            task: freeze_collectors(collectors, directory / "profiles" / str(index))
            for index, (task, collectors) in enumerate(sorted(profiles.items()))
        }
        profile_path = directory / "profiles.json"
        _json(
            profile_path,
            {
                task: [item.model_dump(mode="json") for item in collectors]
                for task, collectors in frozen.items()
            },
        )
        profile_inventory = study._inventory(directory / "profiles")
        state.update(
            frozen_profiles=study._reference(root, profile_path),
            profile_inventory=profile_inventory,
        )
        _json(directory / "revision.json", state)
        revised = _rebased(manifest, original.parent, directory)
        observations: dict[tuple[str, str, str], CollectedInput] = {}
        inputs: list[MeasurementInput] = []
        input_indices: dict[tuple[str, str, str], int] = {}
        cases: list[tuple[str, str, str, str, tuple[str, str, str]]] = []
        for record in manifest.records:
            assert record.before is not None
            for stage_id, snapshot_ref in [
                ("before", record.before),
                *(
                    (stage.id, stage.snapshot)
                    for stage in record.stages
                    if stage.snapshot is not None
                ),
            ]:
                assert snapshot_ref is not None
                uses_before = any(
                    "{before}" in argument
                    for collector in frozen[record.task_id]
                    for argument in collector.command or []
                )
                key = (
                    record.task_id,
                    snapshot_ref.sha256,
                    record.before.sha256 if uses_before or allow_partial else "",
                )
                if key not in input_indices:
                    index = len(inputs)
                    input_indices[key] = index
                    inputs.append(
                        MeasurementInput(
                            index=index,
                            key=key,
                            stage=stage_id,
                            snapshot=(original.parent / snapshot_ref.path).resolve(),
                            snapshot_digest=snapshot_ref.sha256,
                            before=(original.parent / record.before.path).resolve(),
                            before_digest=record.before.sha256,
                        )
                    )
                cases.append((record.id, stage_id, snapshot_ref.sha256, record.before.sha256, key))
        input_states = {str(item.index): "pending" for item in inputs}
        state["input_states"] = input_states

        def update_input(item: MeasurementInput, status: str, repeat: int | None = None) -> None:
            input_states[str(item.index)] = status
            if repeat is not None:
                state["current_input"] = {
                    "task_id": item.key[0],
                    "stage": item.stage,
                    "snapshot_digest": item.snapshot_digest,
                    "repeat": repeat + 1,
                    "index": item.index,
                }
                if progress:
                    progress(
                        f"{item.key[0]}/{item.stage}: collecting input {item.index + 1}, repeat {repeat + 1}/2"
                    )
            _json(directory / "revision.json", state)

        def accept(item: MeasurementInput, collected: CollectedInput) -> None:
            observations[item.key] = collected
            update_input(item, "complete")

        def collect_input(
            item: MeasurementInput,
            cancelled: Event,
            emit: Callable[[Callable[[], None]], None],
        ) -> None:
            repeats: list[dict[MetricKind, ArtifactRef]] = []
            normalized = []
            try:
                for repeat in range(2):
                    if cancelled.is_set():
                        emit(partial(update_input, item, "interrupted"))
                        return
                    evidence = directory / "measurements" / str(item.index) / str(repeat)
                    emit(partial(update_input, item, "running", repeat))
                    paths = collect_measurements(
                        item.snapshot, evidence, frozen[item.key[0]], before=item.before
                    )
                    if not allow_partial and set(paths) != {
                        collector.kind for collector in frozen[item.key[0]]
                    }:
                        raise ValueError(
                            f"partial observer collection: {item.key[0]}/{item.stage}, repeat {repeat + 1}"
                        )
                    result = evaluate_evidence(
                        item.snapshot,
                        {kind: str(path.resolve()) for kind, path in paths.items()},
                        {},
                        evidence.resolve(),
                    )
                    normalized.append(_normalized(result))
                    _json(evidence / "normalized.json", normalized[-1])
                    refs: dict[MetricKind, ArtifactRef] = {}
                    for collector in frozen[item.key[0]]:
                        path = paths.get(collector.kind)
                        if path is None:
                            path = evidence / collector.kind / "failure.json"
                            failure = _object(json.loads(path.read_text()))
                            failure["repeat_index"] = repeat
                            _json(path, failure)
                            _validate_failure(
                                path=path,
                                collector=collector,
                                snapshot_digest=item.snapshot_digest,
                                before_digest=item.before_digest,
                                repeat=repeat,
                            )
                        refs[collector.kind] = ArtifactRef.model_validate(
                            study._reference(directory, path)
                        )
                    repeats.append(refs)
                outcomes = _family_outcomes(normalized, repeats)
                if not allow_partial and any(
                    _object(value)["status"] != "available" for value in outcomes.values()
                ):
                    raise ValueError(
                        f"observer measurements are not repeatable: {item.key[0]}/{item.stage}"
                    )
                emit(partial(accept, item, CollectedInput(repeats=repeats, outcomes=outcomes)))
            except BaseException:
                emit(partial(update_input, item, "failed"))
                raise

        run_jobs(inputs, jobs=jobs, operation=collect_input)
        bindings = []
        proposed_records = {record.id: record for record in revised.records}
        for attempt_id, stage_id, snapshot_digest, before_digest, key in cases:
            collected = observations[key]
            repeats = collected.repeats
            bindings.append(
                {
                    "attempt_id": attempt_id,
                    "stage": stage_id,
                    "snapshot_digest": snapshot_digest,
                    "before_digest": before_digest,
                    **({"outcomes": collected.outcomes} if allow_partial else {}),
                    "repeats": [
                        {
                            kind: study._reference(root, directory / reference.path)
                            for kind, reference in refs.items()
                        }
                        for refs in repeats
                    ],
                }
            )
            proposed = proposed_records[attempt_id]
            supplied = {collector.kind for collector in frozen[key[0]]}
            available = {
                kind: ref
                for kind, ref in repeats[0].items()
                if _object(collected.outcomes[kind])["status"] == "available"
            }
            if stage_id == "before":
                proposed.before_evidence = {
                    **{
                        kind: ref
                        for kind, ref in (proposed.before_evidence or {}).items()
                        if kind not in supplied
                    },
                    **available,
                } or None
            else:
                stage = next(stage for stage in proposed.stages if stage.id == stage_id)
                stage.evidence = {
                    **{
                        kind: ref
                        for kind, ref in (stage.evidence or {}).items()
                        if kind not in supplied
                    },
                    **available,
                } or None
        if (
            _preserved_inventory(root, manifest, original.parent) != protected
            or study._inventory(directory / "profiles") != profile_inventory
        ):
            raise ValueError("native evidence or frozen observer profile changed during collection")
        _native_state(root)
        proposed_path = directory / "assessment-input.json"
        _json(proposed_path, revised.model_dump(mode="json"))
        assessment = build_assessment(proposed_path)
        report_path = directory / "assessment-report.json"
        _json(report_path, assessment)
        state.pop("current_input", None)
        state.update(
            status="complete",
            purpose="offline observer proposal; native run and reports unchanged",
            original_run=study._reference(root, previous / "run.json"),
            original_assessment=study._reference(root, original),
            saved_assessment=study._reference(root, previous / "assessment-input.json"),
            original_reports=study._inventory(previous / "report")
            if (previous / "report").is_dir()
            else {},
            preserved=protected,
            native_review=run.get("review"),
            frozen_profiles=study._reference(root, profile_path),
            profile_inventory=profile_inventory,
            measurement_inventory=study._inventory(directory / "measurements"),
            observations=bindings,
            proposed_assessment=study._reference(root, proposed_path),
            proposed_report=study._reference(root, report_path),
            normalization="evaluated metrics/counts/sets/populations and configuration/observer identity; raw temporary paths and execution timings excluded",
            repeats=2,
            unique_inputs=len(observations),
        )
        _json(directory / "revision.json", state)
        if progress:
            progress(
                f"Observer proposal complete: {len(observations)} unique inputs, two repeats per family"
            )
        return directory / "revision.json"
    except BaseException as exc:
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        _json(directory / "revision.json", state)
        raise


def validate_remeasurement(root: Path, directory: Path) -> Path:
    """Read-only validation of a finished proposal; never rerun collectors or models."""
    if directory.is_symlink():
        raise ValueError("measurement revision cannot be a symlink")
    root, directory = root.resolve(), directory.resolve()
    if directory.parent != root / "observer-revisions":
        raise ValueError("measurement revision must remain inside its native study")
    protocol = _object(json.loads((directory / "revision.json").read_text()))
    if (
        protocol.get("status") != "complete"
        or protocol.get("repeats") != 2
        or protocol.get("revision") != directory.name
    ):
        raise ValueError("measurement revision is incomplete")
    validate_jobs(protocol.get("jobs", 1))
    partial = protocol.get("schema_version") == 2
    if protocol.get("schema_version") not in (1, 2):
        raise ValueError("unsupported measurement revision protocol")
    run, original, original_path = _native_state(root)
    if _preserved_inventory(root, original, original_path.parent) != protocol.get("preserved"):
        raise ValueError("native evidence changed after observer collection")
    if study._bound_document(root, protocol["original_run"]) != run:
        raise ValueError("saved native run differs from the current study")
    if protocol.get("native_review") != run.get("review"):
        raise ValueError("native review differs from the observer proposal")
    if study._bound_path(root, protocol["original_assessment"]) != original_path:
        raise ValueError("original assessment differs from the observer proposal")
    saved = study._bound_path(root, protocol["saved_assessment"])
    if saved.read_bytes() != original_path.read_bytes():
        raise ValueError("saved original assessment differs")
    saved_reports = (
        study._inventory(directory / "previous/report")
        if (directory / "previous/report").is_dir()
        else {}
    )
    if saved_reports != protocol.get("original_reports"):
        raise ValueError("saved original reports changed")
    if study._inventory(directory / "profiles") != protocol.get("profile_inventory"):
        raise ValueError("frozen measurement profile changed")
    if study._inventory(directory / "measurements") != protocol.get("measurement_inventory"):
        raise ValueError("measurement artifacts changed")
    profiles = load_profiles(study._bound_path(root, protocol["frozen_profiles"]))
    if set(profiles) != {record.task_id for record in original.records}:
        raise ValueError("observer profiles differ from native task assignment")
    proposed_path = study._bound_path(root, protocol["proposed_assessment"])
    study._bound_path(root, protocol["proposed_report"])
    proposed = AssessmentInput.model_validate_json(proposed_path.read_text())
    expected = _rebased(original, original_path.parent, proposed_path.parent)
    if len(proposed.records) != len(expected.records):
        raise ValueError("observer proposal changed native records")
    without_metrics = proposed.model_copy(deep=True)
    for record, old in zip(without_metrics.records, expected.records, strict=True):
        supplied = {collector.kind for collector in profiles[old.task_id]}
        if {
            kind: ref
            for kind, ref in (record.before_evidence or {}).items()
            if kind not in supplied
        } != {
            kind: ref for kind, ref in (old.before_evidence or {}).items() if kind not in supplied
        }:
            raise ValueError("observer proposal changed unsupplied measurements")
        record.before_evidence = old.before_evidence
        if len(record.stages) != len(old.stages):
            raise ValueError("observer proposal changed native stage history")
        for stage, old_stage in zip(record.stages, old.stages, strict=True):
            if stage.snapshot is None and stage.evidence != old_stage.evidence:
                raise ValueError("unavailable submission cannot receive measurements")
            if {
                kind: ref for kind, ref in (stage.evidence or {}).items() if kind not in supplied
            } != {
                kind: ref
                for kind, ref in (old_stage.evidence or {}).items()
                if kind not in supplied
            }:
                raise ValueError("observer proposal changed unsupplied measurements")
            stage.evidence = old_stage.evidence
    if without_metrics.model_dump() != expected.model_dump():
        raise ValueError("observer proposal changed native source/correctness/work/ledger fields")
    observations = protocol.get("observations")
    if not isinstance(observations, list):
        raise ValueError("measurement observations missing")
    indexed = {}
    for value in observations:
        observation = _object(value)
        key = (observation.get("attempt_id"), observation.get("stage"))
        if key in indexed:
            raise ValueError("duplicate measurement observation")
        indexed[key] = observation
    expected_keys = {
        (record.id, stage)
        for record in original.records
        for stage in ["before", *(item.id for item in record.stages if item.snapshot is not None)]
    }
    if set(indexed) != expected_keys:
        raise ValueError("observer proposal omits a native stage")
    input_contexts: dict[str, tuple[str, str, str]] = {}
    context_inputs: dict[tuple[str, str, str], str] = {}
    for record in proposed.records:
        assert record.before is not None
        for stage_id, snapshot_ref, evidence in [
            ("before", record.before, record.before_evidence),
            *((stage.id, stage.snapshot, stage.evidence) for stage in record.stages),
        ]:
            if snapshot_ref is None:
                continue
            observation = indexed[(record.id, stage_id)]
            if (
                observation.get("snapshot_digest") != snapshot_ref.sha256
                or observation.get("before_digest") != record.before.sha256
            ):
                raise ValueError("measurement observation binds another source")
            repeats = observation.get("repeats")
            if not isinstance(repeats, list) or len(repeats) != 2:
                raise ValueError("measurement repeat missing")
            uses_before = any(
                "{before}" in argument
                for collector in profiles[record.task_id]
                for argument in collector.command or []
            )
            context = (
                record.task_id,
                snapshot_ref.sha256,
                record.before.sha256 if uses_before or partial else "",
            )
            input_index: str | None = None
            normalized = []
            bound_repeats: list[dict[MetricKind, ArtifactRef]] = []
            for index, values in enumerate(repeats):
                refs = _object(values)
                if set(refs) != {collector.kind for collector in profiles[record.task_id]}:
                    raise ValueError("partial measurement repeat")
                paths = {
                    kind: study._bound_path(root, reference) for kind, reference in refs.items()
                }
                valid_paths: dict[str, str] = {}
                bound_refs: dict[MetricKind, ArtifactRef] = {}
                for collector in profiles[record.task_id]:
                    path = paths[collector.kind]
                    if not path.is_relative_to(directory):
                        raise ValueError("repeat artifact escapes its measurement revision")
                    parts = path.relative_to(directory).parts
                    if (
                        len(parts) != 5
                        or parts[0] != "measurements"
                        or not parts[1].isdigit()
                        or parts[2:4] != (str(index), collector.kind)
                        or parts[4]
                        not in ({"envelope.json", "failure.json"} if partial else {"envelope.json"})
                    ):
                        raise ValueError("repeat artifact does not bind a distinct recorded repeat")
                    if input_index is None:
                        input_index = parts[1]
                    elif input_index != parts[1]:
                        raise ValueError("supplied families bind different input contexts")
                    if (parts[1] in input_contexts and input_contexts[parts[1]] != context) or (
                        context in context_inputs and context_inputs[context] != parts[1]
                    ):
                        raise ValueError(
                            "measurement input identity differs from deduplicated context"
                        )
                    input_contexts[parts[1]] = context
                    context_inputs[context] = parts[1]
                    # Both repeats and all supplied families must use one input directory.
                    first = study._bound_path(root, _object(repeats[0])[collector.kind])
                    if path.parent.parent.parent != first.parent.parent.parent:
                        raise ValueError("measurement repeats bind different input contexts")
                    bound_refs[collector.kind] = ArtifactRef.model_validate(
                        study._reference(directory, path)
                    )
                    if path.name == "failure.json":
                        _validate_failure(
                            path=path,
                            collector=collector,
                            snapshot_digest=snapshot_ref.sha256,
                            before_digest=record.before.sha256,
                            repeat=index,
                        )
                        continue
                    envelope = _object(json.loads(path.read_text()))
                    if (
                        envelope.get("config_digest") != study._digest(collector.config)
                        or envelope.get("scope") != collector.scope
                        or (
                            collector.tool_version is not None
                            and envelope.get("tool_version") != collector.tool_version
                        )
                    ):
                        raise ValueError("measurement differs from its frozen collector profile")
                    valid_paths[collector.kind] = str(path)
                bound_repeats.append(bound_refs)
                normalized.append(
                    _normalized(
                        evaluate_evidence(
                            (proposed_path.parent / snapshot_ref.path).resolve(),
                            valid_paths,
                            {},
                            directory,
                        )
                    )
                )
            outcomes = _family_outcomes(normalized, bound_repeats)
            if partial:
                if observation.get("outcomes") != outcomes:
                    raise ValueError("recorded family outcome differs from bound repeats")
            elif normalized[0] != normalized[1]:
                raise ValueError("observer measurements are not repeatable")
            for collector in profiles[record.task_id]:
                available = _object(outcomes[collector.kind])["status"] == "available"
                reference = (evidence or {}).get(collector.kind)
                if available:
                    first = study._bound_path(root, _object(repeats[0])[collector.kind])
                    if (
                        reference is None
                        or (proposed_path.parent / reference.path).resolve() != first
                        or reference.sha256 != study._digest(first)
                    ):
                        raise ValueError("proposed evidence is not the first bound measurement")
                elif reference is not None:
                    raise ValueError("unavailable family retained successful or stale evidence")
    if protocol.get("unique_inputs") != len(input_contexts):
        raise ValueError("measurement unique input count differs")
    build_assessment(proposed_path)
    return proposed_path
