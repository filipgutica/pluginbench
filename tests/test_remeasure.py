import json
import subprocess
import sys
from pathlib import Path

import pytest

from pluginbench import remeasure, study
from pluginbench.assessment_metrics import evaluate_evidence
from pluginbench.assessment_models import AssessmentInput
from pluginbench.collectors import Collector, collect_measurements


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _native(root: Path, *, distinct_handoffs: bool = False) -> Path:
    before = root / "frozen/before"
    before.mkdir(parents=True)
    (before / "value.js").write_text("export const value = 0;\n")
    records: list[dict[str, object]] = []
    executions: dict[str, dict[str, dict[str, str]]] = {}
    verifiers: dict[str, dict[str, dict[str, str]]] = {}
    for attempt_id, arm in (("a", "baseline"), ("b", "candidate")):
        stages: list[dict[str, object]] = []
        executions[attempt_id], verifiers[attempt_id] = {}, {}
        for stage_id, value in (("handoff", 2 if distinct_handoffs and arm == "candidate" else 1),):
            directory = root / "attempts" / attempt_id / stage_id
            snapshot = directory / "snapshot"
            snapshot.mkdir(parents=True)
            (snapshot / "value.js").write_text(f"export const value = {value};\n")
            reference = study._reference(root, snapshot, snapshot=True)
            _write(
                directory / "correctness.json",
                {
                    "schema_version": 1,
                    "snapshot_digest": reference["sha256"],
                    "checks": {"behavior": True},
                    "command": ["verify"],
                    "tool": "fixture",
                },
            )
            _write(
                directory / "execution.json",
                {
                    "successful_native_completion": True,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "startup": [],
                },
            )
            _write(directory / "verifier-result.json", {"pass": True})
            executions[attempt_id][stage_id] = study._reference(root, directory / "execution.json")
            verifiers[attempt_id][stage_id] = study._reference(
                root, directory / "verifier-result.json"
            )
            stages.append(
                {
                    "id": stage_id,
                    "snapshot": reference,
                    "correctness": study._reference(root, directory / "correctness.json"),
                    "work": {"tokens": {"input_tokens": 10, "output_tokens": 5}},
                }
            )
        records.append(
            {
                "id": attempt_id,
                "task_id": "task",
                "arm": arm,
                "model": "gpt-6.1-sol",
                "attempt": 1,
                "runtime": {"harness": "codex", "reasoning_effort": "medium"},
                "before": study._reference(root, before, snapshot=True),
                "stages": stages,
            }
        )
    _write(
        root / "assessment-input.json",
        {"schema_version": 1, "name": "Native test", "records": records},
    )
    _write(
        root / "plan.json",
        {
            "entries": [
                {key: record[key] for key in ("id", "task_id", "arm", "attempt")}
                for record in records
            ]
        },
    )
    _write(root / "runtime-preflight.json", {"harness_version": "test"})
    _write(
        root / "run.json",
        {
            "schema_version": 1,
            "status": "complete",
            "actual_calls": 2,
            "attempt_states": {"a": "complete", "b": "complete"},
            "assessment_input": study._reference(root, root / "assessment-input.json"),
            "plan": study._reference(root, root / "plan.json"),
            "runtime_preflight": study._reference(root, root / "runtime-preflight.json"),
            "executions": executions,
            "verifier_results": verifiers,
        },
    )
    _write(root / "report/assessment-report.json", {"prior": "published evidence"})
    _write(root / "frozen-inventory.json", study._inventory(root / "frozen"))
    return root


def _profile(root: Path, *, broken: bool = False) -> dict[str, list[Collector]]:
    config = root / "profile.json"
    _write(config, {"test_scope": ["tests/**"]})
    script = root / "coverage.py"
    if broken:
        (root / "missing_neighbor.py").write_text("value = 1\n")
    script.write_text(
        ("import missing_neighbor\n" if broken else "")
        + "import json,pathlib,sys\n"
        + "report={'files_measured':['value.js'], 'branches':{'total':['value.js:1:0'],'covered':['value.js:1:0']}, 'functions':{'total':['value.js:1:0'],'covered':['value.js:1:0']}}\n"
        + "pathlib.Path(sys.argv[2]).write_text(json.dumps(report))\n"
    )
    return {
        "task": [
            Collector(
                kind="coverage",
                config=config,
                scope=["value.js"],
                command=[sys.executable, str(script), "{snapshot}", "{output}"],
                tool_version="fixture-1",
            )
        ]
    }


def _cached_baseline_origin(root: Path) -> None:
    record = json.loads((root / "assessment-input.json").read_text())["records"][0]
    record["id"] = "previous-a"
    for stage in record["stages"]:
        for key in ("snapshot", "correctness"):
            stage[key]["path"] = stage[key]["path"].replace("attempts/a/", "attempts/previous-a/")
    historical = root / "baseline-evidence/a/native-starts.json"
    _write(historical, {"started": True})
    origin = {
        "status": "reused",
        "cache_key": "cached-cohort",
        "context": {"fixture_digest": record["before"]["sha256"]},
        "origin_run": "/previous/run/no-longer-present",
        "origin_attempt": "previous-a",
        "original_record": record,
        "original_task_digest": "original-task",
        "historical_evidence": [study._reference(root, historical)],
    }
    path = root / "baseline-evidence/a/origin.json"
    _write(path, origin)
    run = json.loads((root / "run.json").read_text())
    run["baseline_origin"] = {"a": {**origin, "evidence": study._reference(root, path)}}
    _write(root / "run.json", run)


def test_remeasurement_deduplicates_repeats_and_preserves_native_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _native(tmp_path / "run")
    _cached_baseline_origin(root)
    previous = study._inventory(root)
    calls: list[Path] = []

    def counting(
        snapshot: Path,
        evidence_dir: Path,
        collectors: list[Collector],
        *,
        before: Path | None = None,
    ) -> dict[str, Path]:
        calls.append(snapshot)
        return collect_measurements(snapshot, evidence_dir, collectors, before=before)

    monkeypatch.setattr(remeasure, "collect_measurements", counting)
    sidecar = remeasure.propose_remeasurement(
        root, profiles=_profile(tmp_path), revision="metrics-v2"
    )
    protocol = json.loads(sidecar.read_text())
    assert protocol["status"] == "complete"
    assert protocol["unique_inputs"] == 2 and len(calls) == 4
    assert len(protocol["observations"]) == 4
    proposed_path = root / protocol["proposed_assessment"]["path"]
    proposed = AssessmentInput.model_validate_json(proposed_path.read_text())
    original = AssessmentInput.model_validate_json((root / "assessment-input.json").read_text())
    for old, new in zip(original.records, proposed.records, strict=True):
        assert old.id == new.id and old.runtime == new.runtime and old.model == new.model
        assert old.before is not None and new.before is not None
        assert (root / old.before.path).resolve() == (
            proposed_path.parent / new.before.path
        ).resolve()
        for before_stage, after_stage in zip(old.stages, new.stages, strict=True):
            assert before_stage.id == after_stage.id and before_stage.work == after_stage.work
            assert before_stage.correctness is not None and after_stage.correctness is not None
            assert before_stage.snapshot is not None and after_stage.snapshot is not None
            assert after_stage.evidence is not None
            assert (root / before_stage.correctness.path).resolve() == (
                proposed_path.parent / after_stage.correctness.path
            ).resolve()
            assert before_stage.snapshot.sha256 == after_stage.snapshot.sha256
            assert after_stage.evidence["coverage"]
    assert {
        key: value
        for key, value in study._inventory(root).items()
        if not key.startswith("observer-revisions/")
    } == previous
    assert (sidecar.parent / "previous/report/assessment-report.json").read_bytes() == (
        root / "report/assessment-report.json"
    ).read_bytes()
    for observation in protocol["observations"]:
        assert len(observation["repeats"]) == 2
        for repeat in observation["repeats"]:
            study._bound_document(root, repeat["coverage"])


@pytest.mark.parametrize(
    "fault",
    [
        "running",
        "pending",
        "frozen",
        "snapshot",
        "execution",
        "origin_file",
        "origin_inline",
        "origin_extra_reference",
        "historical_file",
        "historical_escape",
        "historical_link",
    ],
)
def test_remeasurement_rejects_invalid_native_state_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = _native(tmp_path / "run")
    _cached_baseline_origin(root)
    run = json.loads((root / "run.json").read_text())
    if fault == "running":
        run["status"] = "running"
    elif fault == "pending":
        run["attempt_states"]["a"] = "pending"
    elif fault == "frozen":
        (root / "frozen/before/value.js").write_text("changed")
    elif fault == "snapshot":
        (root / "attempts/a/handoff/snapshot/value.js").write_text("changed")
    elif fault == "execution":
        (root / "attempts/a/handoff/execution.json").write_text("{}")
    elif fault == "origin_file":
        (root / "baseline-evidence/a/origin.json").write_text("{}")
    elif fault == "origin_inline":
        run["baseline_origin"]["a"]["original_record"]["model"] = "changed"
    elif fault == "origin_extra_reference":
        extra = root / "baseline-evidence/a/extra.json"
        _write(extra, {"bound": True})
        origin = run["baseline_origin"]["a"]
        origin["context"]["provenance"] = {"receipt": study._reference(root, extra)}
        _write(extra, {"bound": False})
        origin_path = root / "baseline-evidence/a/origin.json"
        _write(origin_path, {key: value for key, value in origin.items() if key != "evidence"})
        origin["evidence"] = study._reference(root, origin_path)
    elif fault == "historical_file":
        (root / "baseline-evidence/a/native-starts.json").write_text("{}")
    elif fault in {"historical_escape", "historical_link"}:
        outside = tmp_path / "outside.json"
        _write(outside, {"started": True})
        origin = run["baseline_origin"]["a"]
        reference = origin["historical_evidence"][0]
        if fault == "historical_escape":
            reference["path"] = "../outside.json"
        else:
            (root / "historical-link.json").symlink_to(outside)
            reference["path"] = "historical-link.json"
        origin_path = root / "baseline-evidence/a/origin.json"
        _write(origin_path, {key: value for key, value in origin.items() if key != "evidence"})
        origin["evidence"] = study._reference(root, origin_path)
    _write(root / "run.json", run)
    monkeypatch.setattr(
        remeasure,
        "collect_measurements",
        lambda *args, **kwargs: pytest.fail("collector must not run"),
    )
    with pytest.raises(ValueError):
        remeasure.propose_remeasurement(root, profiles=_profile(tmp_path), revision="bad")
    assert not (root / "observer-revisions/bad").exists()


def test_metric_normalization_preserves_multiplicity_but_ignores_set_order() -> None:
    first: dict[str, object] = {
        "snapshot_digest": "source",
        "coverage": {"metrics": {"identities": ["a", "b", "a"]}, "provenance": {"config": "bound"}},
    }
    second = json.loads(json.dumps(first))
    second["coverage"]["metrics"]["identities"] = ["b", "a", "a"]
    assert remeasure._normalized(first) == remeasure._normalized(second)
    second["coverage"]["metrics"]["identities"] = ["b", "a"]
    assert remeasure._normalized(first) != remeasure._normalized(second)


@pytest.mark.parametrize("claim", ["matching", "absent", "different", "unconfigured", "missing"])
def test_coverage_clock_claim_is_bound_and_only_retained_when_declared(
    tmp_path: Path, claim: str
) -> None:
    from pluginbench.assessment_metrics import EvidenceBinding, bind_evidence

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "value.js").write_text("export const value = 1;\n")
    clock = {
        "policy": "stepped-date-v1",
        "epoch_ms": 0,
        "step_ms": 1,
        "installation": "node-require-before-mocha",
        "implementation_sha256": "a" * 64,
    }
    config = {} if claim in {"absent", "unconfigured"} else {"clock": clock}
    report: dict[str, object] = {
        "files_measured": ["value.js"],
        "branches": {"total": [], "covered": []},
        "functions": {"total": [], "covered": []},
    }
    if claim not in {"absent", "missing"}:
        report["clock"] = {**clock, "step_ms": 2} if claim == "different" else clock
    config_path, report_path, envelope_path = [
        tmp_path / name for name in ("config.json", "report.json", "envelope.json")
    ]
    _write(config_path, config)
    _write(report_path, report)
    _write(
        envelope_path,
        bind_evidence(
            EvidenceBinding(snapshot, report_path, config_path, "fixture", "1", ("value.js",))
        ),
    )
    if claim in {"different", "unconfigured", "missing"}:
        with pytest.raises(ValueError, match="clock differs"):
            evaluate_evidence(snapshot, {"coverage": str(envelope_path)}, tmp_path)
    else:
        result = evaluate_evidence(snapshot, {"coverage": str(envelope_path)}, tmp_path)
        coverage = result["coverage"]
        assert isinstance(coverage, dict) and isinstance(coverage["metrics"], dict)
        if claim == "matching":
            assert coverage["metrics"]["clock"] == clock
        else:
            assert "clock" not in coverage["metrics"]


def test_profile_loader_resolves_declared_paths(tmp_path: Path) -> None:
    definitions = _profile(tmp_path)
    _write(tmp_path / "syntax.mjs", {})
    profile = definitions["task"][0].model_dump(mode="json")
    profile["config"] = "profile.json"
    profile["command"] = [sys.executable, "coverage.py", "{snapshot}", "{output}"]
    file = tmp_path / "profiles.json"
    _write(
        file,
        {
            "task": [profile],
            "static": [
                {
                    "kind": "fallow",
                    "config": "profile.json",
                    "syntax_command": ["node", "syntax.mjs", "{inputs}"],
                }
            ],
        },
    )
    loaded = remeasure.load_profiles(file)
    assert loaded["task"][0].config == tmp_path / "profile.json"
    command = loaded["task"][0].command
    syntax_command = loaded["static"][0].syntax_command
    assert command is not None and syntax_command is not None
    assert command[1] == str(tmp_path / "coverage.py")
    assert command[-1] == "{output}"
    assert syntax_command[1] == str(tmp_path / "syntax.mjs")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "profile",
        "receipt",
        "missing-repeat",
        "same-repeat",
        "native-metadata",
        "reports",
        "source",
        "nested-path",
    ],
)
def test_finished_remeasurement_validator_is_read_only_and_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    root = _native(tmp_path / "run")
    sidecar = remeasure.propose_remeasurement(
        root, profiles=_profile(tmp_path), revision="validated"
    )
    protocol = json.loads(sidecar.read_text())
    directory = sidecar.parent
    if fault == "profile":
        (directory / "profiles/0/0/config.json").write_text("{}")
    elif fault == "receipt":
        (directory / "measurements/0/1/coverage/report.json").write_text("{}")
    elif fault == "missing-repeat":
        protocol["observations"][0]["repeats"].pop()
        _write(sidecar, protocol)
    elif fault == "same-repeat":
        protocol["observations"][0]["repeats"][1] = protocol["observations"][0]["repeats"][0]
        _write(sidecar, protocol)
    elif fault == "native-metadata":
        proposed_path = root / protocol["proposed_assessment"]["path"]
        proposed = json.loads(proposed_path.read_text())
        proposed["records"][0]["model"] = "different-model"
        _write(proposed_path, proposed)
        protocol["proposed_assessment"] = study._reference(root, proposed_path)
        _write(sidecar, protocol)
    elif fault == "reports":
        (directory / "previous/report/assessment-report.json").write_text("{}")
    elif fault == "source":
        (root / "attempts/a/handoff/snapshot/value.js").write_text("changed")
    elif fault == "nested-path":
        import shutil

        nested = directory / "nested"
        shutil.copytree(directory, nested, ignore=shutil.ignore_patterns("nested"))
        directory = nested
    # Regenerated native report output is deliberately outside preserved inputs.
    _write(sidecar.parent / "report-native/assessment-report.json", {"new rendering": True})
    previous = study._inventory(root)
    monkeypatch.setattr(
        remeasure,
        "collect_measurements",
        lambda *args, **kwargs: pytest.fail("validation must never execute collectors"),
    )
    if fault is None:
        assert (
            remeasure.validate_remeasurement(root, directory)
            == root / protocol["proposed_assessment"]["path"]
        )
    else:
        with pytest.raises(ValueError):
            remeasure.validate_remeasurement(root, directory)
    assert study._inventory(root) == previous


def test_profile_loader_preserves_symlinked_virtualenv_executable(tmp_path: Path) -> None:
    executable = tmp_path / "venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    _write(tmp_path / "config.json", {})
    _write(tmp_path / "collector.py", {})
    profile = tmp_path / "profiles.json"
    _write(
        profile,
        {
            "task": [
                {
                    "kind": "coverage",
                    "config": "config.json",
                    "command": ["venv/bin/python", "collector.py", "{snapshot}", "{output}"],
                    "tool_version": "fixture",
                }
            ]
        },
    )
    command = remeasure.load_profiles(profile)["task"][0].command
    assert command is not None
    assert command[0] == str(executable)
    assert command[0] != str(executable.resolve())
    assert command[1] == str(tmp_path / "collector.py")


def test_command_resolution_leaves_path_commands_literal(tmp_path: Path) -> None:
    from pluginbench.collectors import resolve_collector_command

    (tmp_path / "node").write_text("local file is not a PATH executable")
    assert resolve_collector_command(tmp_path, ["node", "{snapshot}"]) == ["node", "{snapshot}"]


def test_remeasurement_rejects_symlinked_revision_parent_before_any_outside_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _native(tmp_path / "run")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("unrelated")
    (root / "observer-revisions").symlink_to(outside, target_is_directory=True)
    previous = study._inventory(outside)
    monkeypatch.setattr(
        remeasure,
        "collect_measurements",
        lambda *args, **kwargs: pytest.fail("collector must not run"),
    )
    with pytest.raises(ValueError, match="revision parent"):
        remeasure.propose_remeasurement(root, profiles=_profile(tmp_path), revision="escape")
    assert study._inventory(outside) == previous
    assert not (outside / "escape").exists()


def test_unavailable_submission_retains_assignment_and_does_not_block_other_measurements(
    tmp_path: Path,
) -> None:
    root = _native(tmp_path / "run")
    manifest_path = root / "assessment-input.json"
    manifest = AssessmentInput.model_validate_json(manifest_path.read_text())
    unavailable = manifest.records[0].stages[0]
    unavailable.snapshot = None
    unavailable.correctness = None
    _write(manifest_path, manifest.model_dump(mode="json"))
    run = json.loads((root / "run.json").read_text())
    run["assessment_input"] = study._reference(root, manifest_path)
    _write(root / "run.json", run)
    sidecar = remeasure.propose_remeasurement(
        root, profiles=_profile(tmp_path), revision="available"
    )
    state = json.loads(sidecar.read_text())
    assert state["status"] == "complete"
    assert len(state["observations"]) == 3
    assert all(
        (row["attempt_id"], row["stage"]) != ("a", "handoff") for row in state["observations"]
    )
    proposed_path = root / state["proposed_assessment"]["path"]
    proposed = AssessmentInput.model_validate_json(proposed_path.read_text())
    assert len(proposed.records) == 2
    assert proposed.records[0].stages[0].snapshot is None
    assert proposed.records[0].stages[0].evidence is None
    assert proposed.records[1].stages[0].evidence
    assert remeasure.validate_remeasurement(root, sidecar.parent) == proposed_path


def test_partial_proposal_finishes_later_inputs_and_validates(tmp_path: Path) -> None:
    root = _native(tmp_path / "run")
    revision = remeasure.propose_remeasurement(
        root, profiles=_profile(tmp_path, broken=True), revision="partial-ready"
    )
    state = json.loads(revision.read_text())
    assert state["status"] == "complete"
    assert state["unique_inputs"] == 2
    assert len(list(revision.parent.glob("measurements/*/*/coverage/failure.json"))) == 4
    assert all(
        row["outcomes"]["coverage"]["status"] == "unavailable" for row in state["observations"]
    )
    proposed = remeasure.validate_remeasurement(root, revision.parent)
    rows = json.loads(proposed.read_text())["records"]
    assert len(rows) == 2
    assert all("coverage" not in (row["before_evidence"] or {}) for row in rows)
    assert all(
        "coverage" not in (stage["evidence"] or {}) for row in rows for stage in row["stages"]
    )


@pytest.mark.parametrize("second", ["failed", "different"])
def test_partial_repeat_disagreement_never_promotes_a_passing_repeat(
    tmp_path: Path,
    second: str,
) -> None:
    root = _native(tmp_path / "run")
    profiles = _profile(tmp_path)
    script = tmp_path / "coverage.py"
    text = script.read_text()
    before_write = "pathlib.Path(sys.argv[2]).write_text"
    condition = "if pathlib.Path(sys.argv[2]).parent.parent.name == '1':\n"
    action = (
        " raise SystemExit(7)\n" if second == "failed" else " report['branches']['covered']=[]\n"
    )
    script.write_text(text.replace(before_write, condition + action + before_write))
    revision = remeasure.propose_remeasurement(root, profiles=profiles, revision="disagreement")
    state = json.loads(revision.read_text())
    assert all(
        row["outcomes"]["coverage"]["status"] == "nonrepeatable" for row in state["observations"]
    )
    assert all(
        row["outcomes"]["coverage"]["repeat_states"][0] == "available"
        for row in state["observations"]
    )
    proposed = json.loads(remeasure.validate_remeasurement(root, revision.parent).read_text())
    assert all(not record["before_evidence"] for record in proposed["records"])
    assert all(
        not stage["evidence"] for record in proposed["records"] for stage in record["stages"]
    )


def test_partial_coverage_failure_preserves_matching_fallow_and_later_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pluginbench.collectors as collectors

    root = _native(tmp_path / "run")
    profiles = _profile(tmp_path, broken=True)
    cfg = tmp_path / "fallow.json"
    _write(cfg, {"production": True})
    profiles["task"].insert(0, Collector(kind="fallow", config=cfg, scope=["*.js"]))
    original = collectors._execute

    def execute(
        argv: list[str],
        cwd: Path,
        timeout: int,
        *,
        output: Path | None = None,
        config: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] != "fallow":
            return original(argv, cwd, timeout, output=output, config=config)
        report: dict[str, object] = {
            "kind": "combined",
            "version": "3.18.0",
            "check": {},
            "dupes": {},
            "health": {"findings": []},
        }
        if argv[-2:] == ["list", "--files"]:
            report = {"files": ["value.js"], "file_count": 1}
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(collectors, "_execute", execute)
    revision = remeasure.propose_remeasurement(root, profiles=profiles, revision="families")
    state = json.loads(revision.read_text())
    assert state["unique_inputs"] == 2
    assert all(row["outcomes"]["fallow"]["status"] == "available" for row in state["observations"])
    proposed = json.loads(remeasure.validate_remeasurement(root, revision.parent).read_text())
    assert all(set(record["before_evidence"]) == {"fallow"} for record in proposed["records"])
    assert all(
        set(stage["evidence"]) == {"fallow"}
        for record in proposed["records"]
        for stage in record["stages"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "before",
        "profile",
        "repeat",
        "execution",
        "outcome",
        "same-repeat",
        "missing-repeat",
    ],
)
def test_partial_validator_rejects_resealed_failure_binding_tampering(
    tmp_path: Path, fault: str
) -> None:
    root = _native(tmp_path / "run")
    revision = remeasure.propose_remeasurement(
        root, profiles=_profile(tmp_path, broken=True), revision="tampered"
    )
    state = json.loads(revision.read_text())
    path = revision.parent / "measurements/0/0/coverage/failure.json"
    failure = json.loads(path.read_text())
    if fault == "source":
        failure["snapshot_digest"] = "sha256:" + "0" * 64
    elif fault == "before":
        failure["before_digest"] = "sha256:" + "0" * 64
    elif fault == "profile":
        failure["collector"]["scope"] = ["other/**"]
    elif fault == "repeat":
        failure["repeat_index"] = 1
    elif fault == "execution":
        (path.parent / "execution.json").write_text("[]")
    elif fault == "outcome":
        state["observations"][0]["outcomes"]["coverage"]["status"] = "available"
    if fault == "same-repeat":
        state["observations"][0]["repeats"][1] = state["observations"][0]["repeats"][0]
    elif fault == "missing-repeat":
        state["observations"][0]["repeats"].pop()
    _write(path, failure)
    for observation in state["observations"]:
        for repeat in observation["repeats"]:
            for kind, ref in repeat.items():
                repeat[kind] = study._reference(root, root / ref["path"])
    state["measurement_inventory"] = study._inventory(revision.parent / "measurements")
    _write(revision, state)
    with pytest.raises(ValueError):
        remeasure.validate_remeasurement(root, revision.parent)


def test_failed_replacement_removes_supplied_stale_success(tmp_path: Path) -> None:
    root = _native(tmp_path / "run")
    profile = _profile(tmp_path)
    manifest_path = root / "assessment-input.json"
    manifest = json.loads(manifest_path.read_text())
    for record in manifest["records"]:
        for stage in record["stages"]:
            paths = collect_measurements(
                root / stage["snapshot"]["path"],
                root / "old" / record["id"] / stage["id"],
                profile["task"],
            )
            stage["evidence"] = {"coverage": study._reference(root, paths["coverage"])}
    _write(manifest_path, manifest)
    run = json.loads((root / "run.json").read_text())
    run["assessment_input"] = study._reference(root, manifest_path)
    _write(root / "run.json", run)
    broken = _profile(tmp_path, broken=True)
    revision = remeasure.propose_remeasurement(root, profiles=broken, revision="replacement")
    proposed = json.loads(remeasure.validate_remeasurement(root, revision.parent).read_text())
    assert all(
        not stage["evidence"] for record in proposed["records"] for stage in record["stages"]
    )
    assert all(
        "coverage" in stage["evidence"]
        for record in manifest["records"]
        for stage in record["stages"]
    )


def test_trusted_adapter_failure_validator_rejects_resealed_integrity_exit(tmp_path: Path) -> None:
    root = _native(tmp_path / "run")
    profiles = _profile(tmp_path)
    collector = profiles["task"][0]
    collector.failure_protocol = "coverage-exit-v1"
    collector.command = [sys.executable, "-c", "raise SystemExit(3)"]
    revision = remeasure.propose_remeasurement(root, profiles=profiles, revision="trusted")
    assert remeasure.validate_remeasurement(root, revision.parent).is_file()
    state = json.loads(revision.read_text())
    receipt = revision.parent / "measurements/0/0/coverage/execution.json"
    executions = json.loads(receipt.read_text())
    executions[0]["exit_code"] = 2
    _write(receipt, executions)
    failure_path = receipt.with_name("failure.json")
    failure = json.loads(failure_path.read_text())
    failure["execution"]["sha256"] = study._digest(receipt)
    _write(failure_path, failure)
    for observation in state["observations"]:
        for repeat in observation["repeats"]:
            if repeat["coverage"]["path"] == str(failure_path.relative_to(root)):
                repeat["coverage"] = study._reference(root, failure_path)
    state["measurement_inventory"] = study._inventory(revision.parent / "measurements")
    _write(revision, state)
    with pytest.raises(ValueError, match="classified unavailable exit"):
        remeasure.validate_remeasurement(root, revision.parent)


def test_parallel_remeasurement_deduplicates_fixed_inputs_with_serial_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    root = _native(tmp_path / "run")
    previous = study._inventory(root)
    gate, lock = Barrier(2), Lock()
    released = [Event() for _ in range(2)]
    released[1].set()
    active: set[int] = set()
    maximum = 0
    calls: list[tuple[int, int]] = []
    finished: list[int] = []

    def collecting(
        snapshot: Path,
        evidence_dir: Path,
        collectors: list[Collector],
        *,
        before: Path | None = None,
    ) -> dict[str, Path]:
        nonlocal maximum
        index, repeat = int(evidence_dir.parent.name), int(evidence_dir.name)
        with lock:
            assert index not in active
            active.add(index)
            maximum = max(maximum, len(active))
            calls.append((index, repeat))
        if repeat == 0:
            gate.wait(timeout=5)
        assert released[index].wait(timeout=5)
        result = collect_measurements(snapshot, evidence_dir, collectors, before=before)
        with lock:
            active.remove(index)
            if repeat == 1:
                finished.append(index)
        if repeat == 1 and index:
            released[index - 1].set()
        return result

    monkeypatch.setattr(remeasure, "collect_measurements", collecting)
    revision = remeasure.propose_remeasurement(
        root,
        profiles=_profile(tmp_path),
        revision="parallel",
        jobs=3,
    )
    state = json.loads(revision.read_text())
    assert maximum == 2 and not active and finished == [1, 0]
    assert state["jobs"] == 3 and state["unique_inputs"] == 2
    assert sorted(calls) == [(index, repeat) for index in range(2) for repeat in range(2)]
    assert all([repeat for i, repeat in calls if i == index] == [0, 1] for index in range(2))
    proposed = json.loads(remeasure.validate_remeasurement(root, revision.parent).read_text())
    assert [record["id"] for record in proposed["records"]] == ["a", "b"]
    for row in state["observations"]:
        # Duplicate before/handoff contexts retain the first seeded input index.
        index = {"before": 0, "handoff": 1}[row["stage"]]
        assert [Path(repeat["coverage"]["path"]).parts[-4:-2] for repeat in row["repeats"]] == [
            (str(index), "0"),
            (str(index), "1"),
        ]
    assert all(study._inventory(root)[name] == value for name, value in previous.items())


def test_parallel_partial_collection_finishes_other_inputs_without_promoting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier

    root = _native(tmp_path / "run")
    profiles = _profile(tmp_path)
    script = tmp_path / "coverage.py"
    script.write_text(
        script.read_text().replace(
            "pathlib.Path(sys.argv[2]).write_text",
            "if pathlib.Path(sys.argv[2]).parts[-4] == '1': raise SystemExit(7)\n"
            "pathlib.Path(sys.argv[2]).write_text",
        )
    )
    gate = Barrier(2)

    def collecting(
        snapshot: Path,
        evidence_dir: Path,
        collectors: list[Collector],
        *,
        before: Path | None = None,
    ) -> dict[str, Path]:
        if evidence_dir.name == "0":
            gate.wait(timeout=5)
        return collect_measurements(snapshot, evidence_dir, collectors, before=before)

    monkeypatch.setattr(remeasure, "collect_measurements", collecting)
    revision = remeasure.propose_remeasurement(
        root,
        profiles=profiles,
        revision="partial-parallel",
        jobs=3,
    )
    state = json.loads(revision.read_text())
    assert state["status"] == "complete" and state["unique_inputs"] == 2
    assert all(
        row["outcomes"]["coverage"]["status"]
        == ("unavailable" if row["stage"] == "handoff" else "available")
        for row in state["observations"]
    )
    proposed = json.loads(remeasure.validate_remeasurement(root, revision.parent).read_text())
    assert all(row["before_evidence"]["coverage"] for row in proposed["records"])
    assert all(not row["stages"][0]["evidence"] for row in proposed["records"])


@pytest.mark.parametrize("jobs", [True, "2", 2.5, 0, 17])
def test_remeasurement_invalid_jobs_has_no_filesystem_effects(tmp_path: Path, jobs: int) -> None:
    # Exercise the runtime boundary without a valid study or profile to prove ordering.
    with pytest.raises(ValueError, match="integer from 1 to 16"):
        remeasure.propose_remeasurement(
            tmp_path / "missing", profiles={}, revision="invalid", jobs=jobs
        )
    assert list(tmp_path.iterdir()) == []


def test_parallel_observer_fatal_failure_drains_active_receipts_without_new_repeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    from pluginbench import parallel

    root = _native(tmp_path / "run", distinct_handoffs=True)
    preserved = study._inventory(root)
    gate, lock = Barrier(2), Lock()
    cancellation = Event()
    monkeypatch.setattr(parallel, "Event", lambda: cancellation)
    calls: list[tuple[int, int]] = []

    def collecting(
        snapshot: Path,
        evidence_dir: Path,
        collectors: list[Collector],
        *,
        before: Path | None = None,
    ) -> dict[str, Path]:
        index, repeat = int(evidence_dir.parent.name), int(evidence_dir.name)
        with lock:
            calls.append((index, repeat))
        gate.wait(timeout=5)
        if index == 0:
            raise ValueError("fatal observer integrity failure")
        assert cancellation.wait(timeout=5)
        return collect_measurements(snapshot, evidence_dir, collectors, before=before)

    monkeypatch.setattr(remeasure, "collect_measurements", collecting)
    with pytest.raises(ValueError, match="fatal observer integrity failure"):
        remeasure.propose_remeasurement(
            root,
            profiles=_profile(tmp_path),
            revision="fatal-parallel",
            jobs=2,
        )
    directory = root / "observer-revisions/fatal-parallel"
    state = json.loads((directory / "revision.json").read_text())
    assert state["status"] == "failed" and sorted(calls) == [(0, 0), (1, 0)]
    assert state["input_states"] == {"0": "failed", "1": "interrupted", "2": "pending"}
    assert (directory / "measurements/1/0/coverage/envelope.json").is_file()
    assert not (directory / "assessment-input.json").exists()
    assert not list(directory.glob("measurements/*/1"))
    assert all(study._inventory(root)[name] == value for name, value in preserved.items())


def test_reference_patch_applies_to_a_copy_or_reports_why(tmp_path: Path) -> None:
    before = tmp_path / "before"
    before.mkdir()
    (before / "a.txt").write_text("one\n")
    patch = tmp_path / "solution.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
    destination = tmp_path / "reference"
    assert remeasure._apply_reference(before, patch, destination) is None
    assert (destination / "a.txt").read_text() == "two\n"
    assert (before / "a.txt").read_text() == "one\n"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-missing\n+two\n")
    reason = remeasure._apply_reference(before, patch, tmp_path / "other")
    assert reason is not None and reason.startswith("reference patch does not apply")
    assert not (tmp_path / "other").exists()


def test_reference_record_compares_like_a_submission_and_requires_repeatability(
    tmp_path: Path,
) -> None:
    from pluginbench.assessment_metrics import compare_evidence, compare_snapshots
    from test_assessment_metrics import _bound_fallow, _function

    source = "function a() {\n  return 1;\n}\n"
    before, left = _bound_fallow(
        tmp_path / "before", {"src/index.ts": source}, [_function("a", 1, 3, 2)]
    )
    after, right = _bound_fallow(
        tmp_path / "after",
        {"src/index.ts": source + "function b() {\n  return 2;\n}\n"},
        [_function("a", 1, 3, 2), _function("b", 4, 3, 7)],
    )
    change = compare_snapshots(before, after)
    record = remeasure._reference_record([right, right], change)
    assert record["status"] == "available" and "comparison" not in record
    code_added = compare_evidence(left, right, change=change)["fallow"]["code_added"]
    assert code_added["net_cognitive"] == 7 and code_added["functions_added"] == 1
    _, other = _bound_fallow(
        tmp_path / "other",
        {"src/index.ts": source + "function b() {\n  return 2;\n}\n"},
        [_function("a", 1, 3, 2), _function("b", 4, 3, 9)],
    )
    assert remeasure._reference_record([right, other], change)["status"] == "unavailable"


def test_reference_comparison_reextracts_stored_raw_evidence(tmp_path: Path) -> None:
    from pluginbench.assessment_metrics import compare_snapshots
    from test_assessment_metrics import _bound_fallow, _function

    root = tmp_path / "evidence"
    source = "function a() {\n  return 1;\n}\n"
    before, _ = _bound_fallow(
        root / "ref/before", {"src/index.ts": source}, [_function("a", 1, 3, 2)]
    )
    after, _ = _bound_fallow(
        root / "ref/after",
        {"src/index.ts": source + "function b() {\n  return 2;\n}\n"},
        [_function("a", 1, 3, 2), _function("b", 4, 3, 7)],
    )
    # Same location builder the producer uses, so key drift breaks this test.
    reference = {
        "status": "available",
        "change": compare_snapshots(before, after),
        **remeasure.reference_locations(
            root,
            before=(before, "ref/before", {"fallow": "evidence.json"}),
            after=(after, "ref/after", {"fallow": "evidence.json"}),
        ),
    }
    code_added = remeasure.reference_comparison(root, reference)["fallow"]["code_added"]
    assert code_added["net_cognitive"] == 7 and code_added["functions_added"] == 1
    (after / "src/index.ts").write_text("changed\n")
    with pytest.raises(ValueError, match="reference snapshot changed"):
        remeasure.reference_comparison(root, reference)
