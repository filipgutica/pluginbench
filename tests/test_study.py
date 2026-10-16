from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from pluginbench import codex, study, isolation, harbor_backend, submissions
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.datasets import native_identity
from native_support import task_entry, execution as native_execution, synthetic_contract


def _recipe(tmp_path: Path) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    tasks = [task_entry(suite, name) for name in ("first", "second")]
    (suite / "tasks.yaml").write_text(yaml.safe_dump({"schema_version": 1, "tasks": tasks}))
    (tmp_path / "guidance.md").write_text("Reuse the existing helper.\nKeep the change small.\n")
    path = tmp_path / "recipe.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Offline smoke study",
                "tasks": "suite/tasks.yaml",
                "guidance": {"candidate": ["guidance.md"]},
                "harness_version": "1.2.3",
            }
        )
    )
    return path


def _offline_harbor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Native preparation/admission is tested separately; retain real reconstruction here.
    monkeypatch.setattr(submissions, "reviewed_contract", synthetic_contract)
    identity = {"resources": {"cpus": 1, "memory_mb": 2048}, "offline": True}
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: identity)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})


@pytest.fixture(autouse=True)
def offline_harbor(monkeypatch: pytest.MonkeyPatch) -> None:
    _offline_harbor(monkeypatch)


def _fake_runtime(
    monkeypatch: pytest.MonkeyPatch, *, fail_call: int | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    _offline_harbor(monkeypatch)
    monkeypatch.setattr(
        harbor_backend, "preflight", lambda **kwargs: {"harness_version": "codex-cli test"}
    )

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        area = kwargs["area"]
        assert not list((area / "workspace").rglob("guidance.md"))
        if len(calls) != fail_call:
            (area / "workspace/src/answer.py").write_text("answer = 1\n")
        result = native_execution(
            area,
            kwargs["protocol"]["native_task_identity"],
            reward=0 if len(calls) == fail_call else 1,
        )
        (area / "artifacts/prompt.txt").write_text(kwargs["prompt"])
        if len(calls) == fail_call:
            result["usage"] = None
        result["startup"] = [{"type": "session_meta", "payload": {"cli_version": "observed-1"}}]
        return result

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    return calls


def test_full_study_keeps_all_twelve_attempts_and_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    calls = _fake_runtime(monkeypatch, fail_call=1)
    root = study.run_study(recipe, tmp_path / "results")
    manifest = json.loads((root / "assessment-input.json").read_text())
    assert len(calls) == len(manifest["records"]) == 12
    assert len({record["id"] for record in manifest["records"]}) == 12
    for task in ("first", "second"):
        for arm in ("baseline", "candidate"):
            assert (
                sum(
                    record["task_id"] == task and record["arm"] == arm
                    for record in manifest["records"]
                )
                == 3
            )
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 12
    assert report["attempts"][0]["stages"][0]["correctness"]["status"] == "fail"
    assert report["attempts"][0]["stages"][0]["work"]["tokens"] is None
    assert not any(
        "guidance.md" in str(path)
        for call in calls
        for path in (call["area"] / "workspace").glob("**/*")
    )
    for call in calls:
        assert "Set the answer to one for" not in call["prompt"]
        assert call["prompt"] == ""
        selected = call["protocol"]["treatment"]
        if call["entry"]["arm"] == "baseline":
            assert selected == {"guidance": [], "skills": [], "plugins": []}
        else:
            assert len(selected["guidance"]) == 1
            assert Path(selected["guidance"][0]).read_text() == "Reuse the existing helper.\nKeep the change small.\n"
    assert not (root / "frozen/auth.json").exists()


def test_harbor_submissions_use_trusted_grading_and_keep_missing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recipe(tmp_path)
    document = yaml.safe_load(path.read_text())
    document.update(backend="harbor", harness_version="1.2.3", repeats=1)
    document["guidance"]["second-package"] = ["guidance.md"]
    path.write_text(yaml.safe_dump(document))
    runtime = {"protocol": "offline-harbor-fixture"}
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: runtime)
    monkeypatch.setattr(harbor_backend, "assert_runtime", lambda expected: None)
    monkeypatch.setattr(harbor_backend, "preflight", lambda **kwargs: {"paid_calls": 0})
    monkeypatch.setattr(
        harbor_backend, "invoke_native", lambda **kwargs: pytest.fail("coding must use Harbor")
    )
    calls = []

    def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        assert kwargs["protocol"]["harness_version"] == "1.2.3"
        workspace = kwargs["area"] / "workspace"
        assert (workspace / "src/answer.py").read_text() == "answer = 0\n"
        if len(calls) != 1:
            (workspace / "src/answer.py").write_text("answer = 1\n")
        execution: dict[str, Any] = {
            **native_execution(
                kwargs["area"],
                kwargs["protocol"]["native_task_identity"],
                reward=0 if len(calls) == 1 else 1,
            ),
            "submission_collected": len(calls) != 1,
            "successful_native_completion": len(calls) > 2,
            "usage": None,
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
            "launch_harness_version": "1.2.3",
        }
        if len(calls) == 1:
            execution.pop("submission_export_digest", None)
        if len(calls) == 3:
            execution.update(
                submission_collected=False,
                submission_export_digest=inspect_snapshot(workspace)["digest"],
            )
            codex.write_json(kwargs["area"] / "artifacts/execution.json", execution)
            raise ValueError("stopped export completed but cleanup failed")
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = study.run_study(study.load_study(path), tmp_path / "results")
    report = json.loads((root / "report/assessment-report.json").read_text())
    stages = [attempt["stages"][0] for attempt in report["attempts"]]
    assert len(calls) == len(stages) == 6
    assert stages[0]["snapshot_digest"] is None
    assert stages[0]["correctness"]["status"] == "unknown"
    assert stages[1]["correctness"]["status"] == "fail"
    assert stages[2]["snapshot_digest"] is None
    assert stages[2]["correctness"]["status"] == "unknown"
    ungraded = list(root.glob("attempts/*/handoff/ungraded-submission/src/answer.py"))
    assert len(ungraded) == 1 and ungraded[0].read_text() == "answer = 1\n"
    assert all(stage["correctness"]["status"] == "pass" for stage in stages[3:])
    assert all(stage["work"]["tokens"] is None for stage in stages)
    assert json.loads((root / "run.json").read_text())["actual_calls"] == 6


def test_runtime_drift_stops_new_calls_and_preserves_completed_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recipe(tmp_path)
    recipe = study.load_study(path)
    calls = _fake_runtime(monkeypatch)
    native = harbor_backend.invoke_native
    runtime = harbor_backend
    identity = {"resources": {"cpus": 1, "memory_mb": 2048}, "offline": True}
    monkeypatch.setattr(runtime, "runtime_identity", lambda **kwargs: identity)

    def changed_runtime(**kwargs: Any) -> dict[str, Any]:
        result = native(**kwargs)
        result["submission_collected"] = True
        monkeypatch.setattr(
            runtime, "runtime_identity", lambda **kwargs: {**identity, "os_release": "changed"}
        )
        return result

    monkeypatch.setattr(runtime, "invoke_native", changed_runtime)
    root = tmp_path / "results"
    with pytest.raises(isolation.RuntimeDriftError, match="runtime changed"):
        study.run_study(recipe, root)
    run = json.loads((root / "run.json").read_text())
    assert len(calls) == run["actual_calls"] == 1
    assert run["status"] == "interrupted"
    assert list(root.glob("attempts/*/handoff/snapshot/src/answer.py"))
    manifest = json.loads((root / "assessment-input.json").read_text())
    assert len(manifest["records"]) == 12
    assert (root / "report/assessment-report.json").is_file()


@pytest.mark.parametrize("directory_link", [False, True])
def test_artifact_links_are_retained_without_reading_external_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_link: bool
) -> None:
    path = _recipe(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["repeats"] = 1
    path.write_text(yaml.safe_dump(document))
    private = tmp_path / "operator-private"
    private.mkdir()
    secret = private / "private.txt"
    secret.write_text("operator content must never enter submission evidence")
    calls = _fake_runtime(monkeypatch)
    native = harbor_backend.invoke_native

    def linked_artifacts(**kwargs: Any) -> dict[str, Any]:
        execution = native(**kwargs)
        logs = kwargs["area"] / "artifacts/harbor/trial/agent"
        logs.mkdir(parents=True)
        (logs / "not-native-log").symlink_to(private if directory_link else secret)
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", linked_artifacts)
    root = study.run_study(study.load_study(path), tmp_path / "results")
    assert len(calls) == 4
    for file in root.rglob("*"):
        if file.is_file() and not file.is_symlink():
            assert secret.read_bytes() not in file.read_bytes()
    archives = list(root.glob("attempts/*/handoff/artifacts/unsupported-artifacts.tar"))
    assert len(archives) == 4
    with tarfile.open(archives[0]) as archive:
        link = archive.getmember("artifacts/harbor/trial/agent/not-native-log")
        assert link.issym()
        assert link.linkname == str(private if directory_link else secret)
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert all(
        attempt["stages"][0]["snapshot_digest"] is None
        and attempt["stages"][0]["correctness"]["status"] == "unknown"
        for attempt in report["attempts"]
    )


def test_plan_is_fixed_balanced_and_single_attempt(tmp_path: Path) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    first = study.plan_study(recipe)
    assert first == study.plan_study(recipe)
    assert first["initial_calls"] == 12
    assert first["delivery"] == "native-instructions-skills-plugins-v1"


def test_report_derives_each_attempt_harness_version_from_bound_session_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    calls = _fake_runtime(monkeypatch)
    invoke = harbor_backend.invoke_native

    def changing_cli(**kwargs: Any) -> dict[str, Any]:
        execution = invoke(**kwargs)
        execution["startup"] = [
            {"type": "session_meta", "payload": {"cli_version": f"observed-{len(calls)}"}}
        ]
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", changing_cli)
    root = study.run_study(recipe, tmp_path / "results")
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert [row["provenance"]["harness_version"] for row in report["attempts"]] == [
        f"observed-{index}" for index in range(1, 13)
    ]
    assert {row["provenance"]["assigned_harness_version"] for row in report["attempts"]} == {
        "1.2.3"
    }


def test_frozen_guidance_tampering_is_rejected_on_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    _fake_runtime(monkeypatch)
    root = study.run_study(recipe, tmp_path / "results")
    next((root / "frozen/treatments").rglob("*.md")).write_text("changed")
    with pytest.raises(ValueError, match="frozen study inputs changed"):
        study.report_study(root)


@pytest.mark.parametrize("cause", ["timeout", "provider"])
def test_native_failure_is_retained_even_if_workspace_checks_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    calls = _fake_runtime(monkeypatch)
    invoke = harbor_backend.invoke_native
    stream_errors = (
        [
            {
                "type": "turn.failed",
                "error": {
                    "message": "Selected model is at capacity. Please try a different model."
                },
            }
        ]
        if cause == "provider"
        else []
    )
    partial_usage = [
        {
            "status": "incomplete usage lower bound",
            "tokens": {"input_tokens": 9, "output_tokens": 2},
        }
    ]

    def incomplete(**kwargs: Any) -> dict[str, Any]:
        execution = invoke(**kwargs)
        if len(calls) == 1:
            execution.update(
                successful_native_completion=False,
                timed_out=cause == "timeout",
                stream_errors=stream_errors,
                partial_usage=partial_usage,
                error={"exception_type": "ApiOverloadedError"} if cause == "provider" else None,
                usage=None,
                actual_primary_models=[],
                runtime_effort_evidence=[],
            )
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", incomplete)
    root = study.run_study(recipe, tmp_path / "results")
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 12
    failed = report["attempts"][0]["stages"][0]
    assert failed["correctness"]["checks"]["behavior"] is True
    assert failed["correctness"]["checks"]["native_completion"] is False
    assert failed["correctness"]["status"] == "fail"
    assert failed["work"]["tokens"] is None
    execution = failed["execution_provenance"]
    assert execution["stream_errors"] == stream_errors
    assert execution["partial_usage"] == partial_usage
    assert execution["successful_native_completion"] is False
    assert execution["timed_out"] is (cause == "timeout")
    provenance = report["attempts"][0]["provenance"]
    assert provenance["assigned_model"] == "gpt-6.1-sol"
    assert provenance["assigned_reasoning_effort"] == "medium"
    assert report["attempts"][0]["model"] is None
    assert provenance["reasoning_effort"] is None


@pytest.mark.parametrize(
    "filename", ["AGENTS.md", "AGENTS.override.md", ".claude/settings.json", ".git/config"]
)
def test_fixture_contamination_is_rejected(tmp_path: Path, filename: str) -> None:
    path = _recipe(tmp_path)
    contaminated = tmp_path / "suite/fixtures/first" / filename
    contaminated.parent.mkdir(parents=True, exist_ok=True)
    contaminated.write_text("custom instruction")
    with pytest.raises(ValueError, match="custom guidance|native source changed"):
        study.load_study(path)


def test_directory_cannot_be_silently_flattened_into_guidance(tmp_path: Path) -> None:
    path = _recipe(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("Useful instruction.")
    value = yaml.safe_load(path.read_text())
    value["guidance"] = {"plugin": ["bundle"]}
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match="guidance must select regular"):
        study.load_study(path)


@pytest.mark.parametrize("mutation", [None, "directory-mode", "empty-directory", "special-file-mode", "pre-freeze"])
def test_native_plugin_freezing_preserves_package_and_leaves_task_prompt_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None,
) -> None:
    path = _recipe(tmp_path)
    plugin = tmp_path / "plugin"
    skill = plugin / "skills/one"
    (skill / "references").mkdir(parents=True)
    (skill / "empty-resource").mkdir()
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
        "name": "fixture", "skills": "./skills", "hooks": {},
    }))
    (skill / "SKILL.md").write_text("---\nname: one\ndescription: Native skill.\n---\nRead references/help.md.\n")
    (skill / "references/help.md").write_text("Reference")
    (skill / "run.sh").write_text("#!/bin/sh\necho proof\n")
    (skill / "run.sh").chmod(0o755)
    value = yaml.safe_load(path.read_text())
    value.pop("guidance")
    value["treatments"] = {"plugin": {"plugins": ["plugin"]}}
    value["repeats"] = 1
    path.write_text(yaml.safe_dump(value))
    calls = _fake_runtime(monkeypatch)
    resolved = study.load_study(path)
    if mutation == "pre-freeze":
        (skill / "empty-resource").rmdir()
        with pytest.raises(ValueError, match="frozen.*treatment.*changed"):
            study.run_study(resolved, tmp_path / "results")
        assert not calls
        return
    root = study.run_study(resolved, tmp_path / "results")
    assert len(calls) == 4
    assert all(call["prompt"] == "" for call in calls)
    selected = next(call["protocol"]["treatment"] for call in calls if call["entry"]["arm"] == "plugin")
    frozen = Path(selected["plugins"][0])
    assert frozen.is_relative_to(root / "frozen/treatments")
    assert (frozen / "skills/one/references/help.md").read_text() == "Reference"
    assert (frozen / "skills/one/run.sh").stat().st_mode & 0o777 == 0o755
    assert (frozen / ".codex-plugin/plugin.json").read_bytes() == (plugin / ".codex-plugin/plugin.json").read_bytes()
    if mutation == "directory-mode":
        frozen.chmod(0o700 if frozen.stat().st_mode & 0o777 != 0o700 else 0o755)
    elif mutation == "empty-directory":
        (frozen / "skills/one/empty-resource").rmdir()
    elif mutation == "special-file-mode":
        (frozen / "skills/one/run.sh").chmod(0o4755)
    if mutation:
        with pytest.raises(ValueError, match="frozen.*treatment.*changed"):
            study.report_study(root)


def test_guidance_file_symlink_is_rejected_before_resolution(tmp_path: Path) -> None:
    path = _recipe(tmp_path)
    alias = tmp_path / "linked.md"
    alias.symlink_to(tmp_path / "guidance.md")
    value = yaml.safe_load(path.read_text())
    value["guidance"] = {"candidate": ["linked.md"]}
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError, match="symlink"):
        study.load_study(path)


@pytest.mark.parametrize(
    "reward, auxiliary, expected",
    [
        (0, False, False),
        (1, False, True),
        (0.5, False, None),
        (0, True, False),
        (1, True, True),
        (0.5, True, None),
        (True, True, None),
    ],
)
def test_native_grade_requires_binary_reward_and_bound_measured_source(
    tmp_path: Path, reward: float, auxiliary: bool, expected: bool | None
) -> None:
    task = study.load_study(_recipe(tmp_path)).tasks[0]
    area = tmp_path / "area"
    import shutil

    shutil.copytree(task.fixture, area / "workspace")
    (area / "workspace/src/answer.py").write_text("answer = 1\n")
    result = native_execution(area, native_identity(task))
    rewards = {"reward": reward}
    result["native_verifier_result"] = {"rewards": rewards}
    if auxiliary:
        rewards.update(f2p=1.0, p2p=1.0)
    bound = study._native_grade_binding(
        snapshot=area / "workspace", task=task, artifacts=area / "artifacts", execution=result
    )
    assert bound["pass"] is expected
    assert bound["graded_source_digest"] == bound["measured_source_digest"]
    (area / "workspace/src/uncommitted.py").write_text("extra = 1\n")
    with pytest.raises(ValueError, match="canonical graded source changed"):
        study._native_grade_binding(
            snapshot=area / "workspace", task=task, artifacts=area / "artifacts", execution=result
        )


def test_native_graded_patch_tampering_is_rejected(tmp_path: Path) -> None:
    import shutil

    task = study.load_study(_recipe(tmp_path)).tasks[0]
    area = tmp_path / "area"
    shutil.copytree(task.fixture, area / "workspace")
    (area / "workspace/src/answer.py").write_text("answer = 1\n")
    result = native_execution(area, native_identity(task))
    (area / "artifacts/harbor/trial/artifacts/logs/artifacts/model.patch").write_text(
        "foreign patch"
    )
    with pytest.raises(ValueError, match="graded patch changed"):
        study._native_grade_binding(
            snapshot=area / "workspace", task=task, artifacts=area / "artifacts", execution=result
        )


@pytest.mark.parametrize("fault", ["escape", "mode", "multi-reward"])
def test_native_source_binding_keeps_unsupported_grade_unknown(tmp_path: Path, fault: str) -> None:
    import shutil

    task = study.load_study(_recipe(tmp_path)).tasks[0]
    area = tmp_path / "area"
    shutil.copytree(task.fixture, area / "workspace")
    (area / "workspace/src/answer.py").write_text("answer = 1\n")
    result = native_execution(area, native_identity(task))
    if fault == "escape":
        result["native_artifact_hashes"] = {"../model.patch": "a" * 64}
        with pytest.raises(ValueError, match="graded patch hash unavailable"):
            study._native_grade_binding(
                snapshot=area / "workspace",
                task=task,
                artifacts=area / "artifacts",
                execution=result,
            )
        return
    if fault == "mode":
        (area / "workspace/src/answer.py").chmod(0o755)
        with pytest.raises(ValueError, match="canonical graded source changed"):
            study._native_grade_binding(
                snapshot=area / "workspace",
                task=task,
                artifacts=area / "artifacts",
                execution=result,
            )
        return
    else:
        result["native_verifier_result"] = {"rewards": {"a": 1, "b": 1}}
    bound = study._native_grade_binding(
        snapshot=area / "workspace", task=task, artifacts=area / "artifacts", execution=result
    )
    assert bound["pass"] is None
    assert bound["native_verifier_result"] == result["native_verifier_result"]


@pytest.mark.parametrize("failure", ["symlink", "patch", "export"])
def test_unsupported_submission_is_retained_and_remaining_attempts_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    calls = _fake_runtime(monkeypatch)
    invoke = harbor_backend.invoke_native
    outside = tmp_path / "outside.txt"
    outside.write_text("Must not be read as submitted source")

    def unsupported(**kwargs: Any) -> dict[str, Any]:
        execution = invoke(**kwargs)
        if len(calls) == 1:
            artifacts = kwargs["area"] / "artifacts"
            if failure == "symlink":
                (kwargs["area"] / "workspace/src/link.py").symlink_to(outside)
            elif failure == "patch":
                patch = artifacts / "harbor/trial/artifacts/logs/artifacts/model.patch"
                patch.write_text("invalid patch\n")
                execution["native_artifact_hashes"] = harbor_backend._native_artifact_hashes(
                    artifacts / "harbor/trial/artifacts"
                )
            else:
                receipt = artifacts / "submission-source.json"
                exported = json.loads(receipt.read_text())
                exported["source_metadata_digest"] = "unproven export metadata"
                receipt.write_text(json.dumps(exported))
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", unsupported)
    root = study.run_study(recipe, tmp_path / "results")
    assert len(calls) == 12
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 12
    stage = report["attempts"][0]["stages"][0]
    assert stage["snapshot_digest"] is None
    target = root / "attempts" / report["attempts"][0]["id"] / "handoff"
    if failure == "symlink":
        assert stage["submission_error"]["symlinks"] == {"src/link.py": str(outside)}
        assert (target / "unsupported-submission/src/link.py").is_symlink()
        assert "outside.txt" not in stage["submission_error"]["error"]
        return
    source = target / "diagnostic-source/src/answer.py"
    patch = target / "artifacts/harbor/trial/artifacts/logs/artifacts/model.patch"
    for artifact in (source, patch):
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"changed\n")
        with pytest.raises(ValueError, match="unsupported submission diagnostics changed"):
            study.report_study(root)
        artifact.write_bytes(original)
        mode = artifact.stat().st_mode
        artifact.chmod(0o755)
        with pytest.raises(ValueError, match="unsupported submission diagnostics changed"):
            study.report_study(root)
        artifact.chmod(mode)
    source.unlink()
    with pytest.raises(ValueError, match="unsupported submission diagnostics changed"):
        study.report_study(root)
    assert len(calls) == 12


def test_execution_receipt_tamper_is_rejected_on_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe = study.load_study(_recipe(tmp_path))
    _fake_runtime(monkeypatch)
    root = study.run_study(recipe, tmp_path / "results")
    next((root / "attempts").glob("*/handoff/execution.json")).write_text("{}")
    with pytest.raises(ValueError, match="run artifact changed after the run"):
        study.report_study(root)


def test_duplicate_collector_kinds_are_rejected_before_running(tmp_path: Path) -> None:
    path = _recipe(tmp_path)
    recipe = yaml.safe_load(path.read_text())
    recipe["collectors"] = [
        {"kind": "fallow", "config": "config.json"},
        {"kind": "fallow", "config": "another-config.json"},
    ]
    path.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValueError, match="each collector kind may appear only once"):
        study.load_study(path)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("backend lost")])
def test_interrupted_study_is_reportable_and_retains_every_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    resolved = study.load_study(_recipe(tmp_path))
    calls = _fake_runtime(monkeypatch)
    original = harbor_backend.invoke_native

    def interrupted(**kwargs: Any) -> dict[str, Any]:
        if calls:
            raise failure
        return original(**kwargs)

    monkeypatch.setattr(harbor_backend, "invoke_native", interrupted)
    root = tmp_path / "run"
    with pytest.raises(type(failure)):
        study.run_study(resolved, root)
    run = json.loads((root / "run.json").read_text())
    assert run["status"] == "interrupted"
    assert run["completed_attempts"] == 1
    assert run["actual_calls"] == 2
    assert list(run["attempt_states"].values()).count("pending") == 10
    study.report_study(root)
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 12
    assert report["attempts"][0]["stages"][0]["correctness"]["status"] == "pass"
    assert all(
        attempt["stages"][0]["snapshot_digest"] is None for attempt in report["attempts"][1:]
    )
    assert report["attempts"][1]["stages"][0]["execution_provenance"]["artifact"]
    from pluginbench.assessment_charts import chart_data

    rows = chart_data(report)["observations"]
    assert isinstance(rows, list)
    observations = [row for row in rows if row["metric"] == "Input tokens"]
    assert len(observations) == 12
    assert {row["attempt_id"] for row in observations} == {
        attempt["id"] for attempt in report["attempts"]
    }
    assert [row["correctness"] for row in observations].count("pass") == 1
    assert [row["correctness"] for row in observations].count("unknown") == 11
    assert observations[0]["value"] == 10
    assert all(row["value"] is None and row["status"] == "unknown" for row in observations[1:])
    for task_id in ("first", "second"):
        for arm in ("baseline", "candidate"):
            assert sum(row["task"] == task_id and row["arm"] == arm for row in observations) == 3
            assert sorted(
                attempt["attempt"]
                for attempt in report["attempts"]
                if attempt["task_id"] == task_id and attempt["arm"] == arm
            ) == [1, 2, 3]
    assert all(
        attempt["provenance"]["assigned_reasoning_effort"] == "medium"
        for attempt in report["attempts"]
    )
    assert observations[0]["reasoning_effort"] == "medium"
    assert all(row["reasoning_effort"] == row["model"] == "unknown" for row in observations[1:])


def test_preflight_failure_is_reportable_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = study.load_study(_recipe(tmp_path))

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("native CLI unavailable")

    monkeypatch.setattr(harbor_backend, "preflight", unavailable)
    root = tmp_path / "run"
    with pytest.raises(ValueError, match="native CLI unavailable"):
        study.run_study(resolved, root)
    run = json.loads((root / "run.json").read_text())
    assert run["completed_attempts"] == run["actual_calls"] == 0
    assert set(run["attempt_states"].values()) == {"pending"}
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(report["attempts"]) == 12
    assert all(row["provenance"]["assigned_harness_version"] is None for row in report["attempts"])
    from pluginbench.assessment_charts import chart_data

    charts = chart_data(report)
    rows = charts["observations"]
    assert isinstance(rows, list)
    observations = [row for row in rows if row["metric"] == "Input tokens"]
    assert charts["assigned"] == len(observations) == 12
    assert {row["attempt_id"] for row in observations} == set(run["attempt_states"])
    assert all(
        row["value"] is None and row["status"] == row["correctness"] == "unknown"
        for row in observations
    )
    assert all(
        row["reasoning_effort"] == row["model"] == row["harness_version"] == "unknown"
        for row in observations
    )
    assert all(
        attempt["provenance"]["assigned_model"] == "gpt-6.1-sol"
        and attempt["provenance"]["assigned_reasoning_effort"] == "medium"
        for attempt in report["attempts"]
    )


def test_task_provenance_is_frozen_and_bound_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recipe(tmp_path)
    catalog = tmp_path / "suite/tasks.yaml"
    data = yaml.safe_load(catalog.read_text())
    data["tasks"][0]["provenance"].update(
        {
            "repository": "https://example.com/package",
            "base_commit": "a" * 40,
            "preparation": "source-only snapshot",
        }
    )
    catalog.write_text(yaml.safe_dump(data))
    _fake_runtime(monkeypatch)
    root = study.run_study(study.load_study(path), tmp_path / "run")
    frozen = json.loads((root / "frozen/tasks.json").read_text())
    assert {
        key: value
        for key, value in frozen["tasks"][0]["provenance"].items()
        if key != "native_task_identity"
    } == {
        key: value
        for key, value in data["tasks"][0]["provenance"].items()
        if key != "native_task_identity"
    }
    report = json.loads((root / "report/assessment-report.json").read_text())
    first = next(attempt for attempt in report["attempts"] if attempt["task_id"] == "first")
    assert first["provenance"]["task"] == frozen["tasks"][0]["provenance"]


def test_submission_keeps_patch_files_and_retains_generated_export_only_as_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recipe(tmp_path)
    _fake_runtime(monkeypatch)
    original = harbor_backend.invoke_native

    def with_dependencies(**kwargs: Any) -> dict[str, Any]:
        workspace = kwargs["area"] / "workspace"
        (workspace / "dist").mkdir()
        (workspace / "dist/retained.js").write_text("export const answer = 1;\n")
        result = original(**kwargs)
        (workspace / "dist/transient.js").write_text("generated after patch collection\n")
        artifacts = kwargs["area"] / "artifacts"
        export = json.loads((artifacts / "submission-source.json").read_text())
        export["snapshot_digest"] = inspect_snapshot(workspace)["digest"]
        export["source_metadata_digest"] = harbor_backend.native_task_assets(workspace)
        (artifacts / "submission-source.json").write_text(json.dumps(export))
        runtime = json.loads((artifacts / "runtime-isolation.json").read_text())
        runtime["export"] = export
        (artifacts / "runtime-isolation.json").write_text(json.dumps(runtime))
        result["submission_export_digest"] = export["snapshot_digest"]
        (workspace / "node_modules/.bin").mkdir(parents=True)
        (workspace / "node_modules/.bin/tool").symlink_to("../package/tool")
        (workspace / ".git").mkdir()
        (workspace / ".git/object").write_bytes(b"temporary git data")
        return result

    monkeypatch.setattr(harbor_backend, "invoke_native", with_dependencies)
    root = study.run_study(study.load_study(path), tmp_path / "run")
    record = json.loads((root / "assessment-input.json").read_text())["records"][0]
    snapshot = root / record["stages"][0]["snapshot"]["path"]
    assert not (snapshot / "node_modules").exists()
    assert not (snapshot / ".git").exists()
    assert (snapshot / "src/answer.py").read_text() == "answer = 1\n"
    assert (snapshot / "dist/retained.js").read_text() == "export const answer = 1;\n"
    assert not (snapshot / "dist/transient.js").exists()
    diagnostic = snapshot.parent / "diagnostic-source"
    assert (diagnostic / "dist/transient.js").read_text() == "generated after patch collection\n"
    assert inspect_snapshot(diagnostic)["digest"] != inspect_snapshot(snapshot)["digest"]


def test_parallel_coding_overlaps_keeps_seeded_order_and_bound_native_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    recipe_path = _recipe(tmp_path)
    document = yaml.safe_load(recipe_path.read_text())
    document.update(repeats=1, jobs=4)
    recipe_path.write_text(yaml.safe_dump(document))
    resolved = study.load_study(recipe_path)
    plan = study.plan_study(resolved)
    order = {entry["id"]: index for index, entry in enumerate(plan["entries"])}
    gate, lock = Barrier(4), Lock()
    released = [Event() for _ in range(4)]
    released[3].set()
    completed: list[int] = []
    active = maximum = 0
    monkeypatch.setattr(
        harbor_backend, "preflight", lambda **kwargs: {"harness_version": "offline fixture"}
    )

    def invoke(*, area: Path, entry: dict[str, object], **kwargs: object) -> dict[str, object]:
        nonlocal active, maximum
        attempt_id = entry["id"]
        assert isinstance(attempt_id, str)
        index = order[attempt_id]
        with lock:
            active += 1
            maximum = max(maximum, active)
        gate.wait(timeout=5)
        assert released[index].wait(timeout=5)
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        with lock:
            completed.append(index)
            active -= 1
        if index:
            released[index - 1].set()
        return {
            "submission_collected": True,
            "successful_native_completion": True,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
        }

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = study.run_study(resolved, tmp_path / "parallel-results")
    run = json.loads((root / "run.json").read_text())
    records = json.loads((root / "assessment-input.json").read_text())["records"]
    assert maximum == 4 and completed == [3, 2, 1, 0]
    assert [record["id"] for record in records] == list(order)
    assert run["jobs"] == run["actual_calls"] == run["completed_attempts"] == 4
    assert set(run["invocation_starts"]) == set(order)
    for attempt_id, starts in run["invocation_starts"].items():
        assert set(starts) == {"handoff"}
        receipt = study._bound_document(root, starts["handoff"])
        assert receipt["attempt_id"] == attempt_id and receipt["stage"] == "handoff"
    assert all(record["stages"][0]["work"]["tokens"]["output_tokens"] == 5 for record in records)

    document["jobs"] = 1
    recipe_path.write_text(yaml.safe_dump(document))
    calls = _fake_runtime(monkeypatch)
    serial = study.run_study(study.load_study(recipe_path), tmp_path / "serial-results")
    serial_records = json.loads((serial / "assessment-input.json").read_text())["records"]
    assert len(calls) == 4
    assert [row["id"] for row in serial_records] == [row["id"] for row in records]
    assert [row["stages"][0]["work"]["tokens"] for row in serial_records] == [
        row["stages"][0]["work"]["tokens"] for row in records
    ]


def test_parallel_preparation_failure_has_no_native_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_path = _recipe(tmp_path)
    document = yaml.safe_load(recipe_path.read_text())
    document.update(repeats=1, jobs=2)
    recipe_path.write_text(yaml.safe_dump(document))
    calls = _fake_runtime(monkeypatch)

    def fail_preparation(*args: object, **kwargs: object) -> object:
        raise RuntimeError("preparation failed before invocation")

    monkeypatch.setattr(study, "_collect", fail_preparation)
    root = tmp_path / "failed-results"
    with pytest.raises(RuntimeError, match="preparation failed before invocation"):
        study.run_study(study.load_study(recipe_path), root)
    run = json.loads((root / "run.json").read_text())
    assert calls == [] and run["actual_calls"] == 0
    assert run["invocation_starts"] == {}
    assert run["status"] == "interrupted" and len(run["attempt_states"]) == 4


def test_parallel_interrupt_drains_active_cost_and_stops_queued_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    from pluginbench import parallel

    recipe_path = _recipe(tmp_path)
    document = yaml.safe_load(recipe_path.read_text())
    document.update(repeats=1, jobs=2)
    recipe_path.write_text(yaml.safe_dump(document))
    gate, lock = Barrier(2), Lock()
    calls: list[str] = []
    monkeypatch.setattr(
        harbor_backend, "preflight", lambda **kwargs: {"harness_version": "offline fixture"}
    )
    cancellation = Event()
    monkeypatch.setattr(parallel, "Event", lambda: cancellation)

    def invoke(*, area: Path, entry: dict[str, object], **kwargs: object) -> dict[str, object]:
        attempt_id = entry["id"]
        assert isinstance(attempt_id, str)
        with lock:
            ordinal = len(calls)
            calls.append(attempt_id)
        gate.wait(timeout=5)
        usage = {"input_tokens": 10, "output_tokens": 5}
        if ordinal == 0:
            (area / "artifacts").mkdir()
            (area / "artifacts/execution.json").write_text(json.dumps({"usage": usage}))
            raise KeyboardInterrupt("offline concurrent interrupt")
        assert cancellation.wait(timeout=5)
        return {
            "submission_collected": True,
            "successful_native_completion": True,
            "usage": usage,
            "actual_primary_models": ["gpt-6.1-sol"],
            "runtime_effort_evidence": ["medium"],
        }

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    with pytest.raises(KeyboardInterrupt, match="offline concurrent interrupt"):
        study.run_study(study.load_study(recipe_path), tmp_path / "results")
    run = json.loads((tmp_path / "results/run.json").read_text())
    records = json.loads((tmp_path / "results/assessment-input.json").read_text())["records"]
    assert len(calls) == run["actual_calls"] == 2
    assert sum(state == "pending" for state in run["attempt_states"].values()) == 2
    assert all(len(record["stages"]) == 1 for record in records)
    observed = [record for record in records if record["id"] in calls]
    assert all(record["stages"][0]["work"]["tokens"]["output_tokens"] == 5 for record in observed)
