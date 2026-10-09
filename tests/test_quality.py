from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pluginbench.cli import app
from pluginbench.quality import analyze_patch, build_quality_report, write_quality_reports
from pluginbench.results import (
    ArmName,
    ArmResult,
    StartingPatchResult,
    TaskResult,
    Usage,
    canonical_fingerprint,
    load_arm,
    save_arm,
)


def _task_result(*, attempts: int, seeded: bool) -> TaskResult:
    starting_patch = None
    if seeded:
        starting_patch = StartingPatchResult(
            digest="sha256:starting",
            expected_score=0,
            repaired_attempts=attempts,
        )
    return TaskResult(
        task_id="task-a",
        attempts=attempts,
        score=1.0,
        passed=True,
        duration_seconds=1.0,
        usage=Usage(),
        starting_patch=starting_patch,
    )


def _write_arm(
    root: Path,
    label: ArmName,
    outcomes: dict[int, str],
    *,
    seeded: bool = False,
    fingerprint: str | None = None,
    checksum: str = "sha256:task",
    patch: str = "",
    agent_patch: str | None = None,
) -> Path:
    arm_dir = root / label
    arm_dir.mkdir(parents=True)
    payload = {"dataset": {"task_digests": {"task-a": checksum}}}
    arm = ArmResult.from_tasks(
        arm=label,
        compatibility_fingerprint=fingerprint or canonical_fingerprint(payload),
        tasks={"task-a": _task_result(attempts=len(outcomes), seeded=seeded)},
        task_checksums={"task-a": checksum},
        compatibility_payload=payload,
    )
    save_arm(arm_dir / "arm.json", arm)
    for attempt, outcome in outcomes.items():
        attempt_dir = arm_dir / "verifiers" / "task-a" / f"attempt-{attempt:02d}"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "predictions.jsonl").write_text(
            json.dumps({"instance_id": "task-a", "model_patch": patch}) + "\n"
        )
        (attempt_dir / "patch.diff").write_text(patch)
        if agent_patch is not None:
            (attempt_dir / "agent-change.diff").write_text(agent_patch)
        key = {
            "resolved": "resolved_ids",
            "unresolved": "unresolved_ids",
            "empty_patch": "empty_patch_ids",
            "error": "error_ids",
            "infrastructure_error": "infra_failure_ids",
        }[outcome]
        (attempt_dir / "swebench-summary.json").write_text(json.dumps({key: ["task-a"]}))
    return arm_dir


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _write_python_workspace(run: Path, label: str, final_source: str) -> str:
    workspace = run / label / "workspaces" / "task-a" / "attempt-01"
    workspace.mkdir(parents=True)
    _git("init", cwd=workspace)
    _git("config", "user.email", "test@example.com", cwd=workspace)
    _git("config", "user.name", "Test", cwd=workspace)
    source = workspace / "module.py"
    source.write_text("from math import sqrt\n\ndef existing(value):\n    return sqrt(value)\n")
    _git("add", "module.py", cwd=workspace)
    _git("-c", "commit.gpgsign=false", "commit", "-m", "base", cwd=workspace)
    base = _git("rev-parse", "HEAD", cwd=workspace)
    source.write_text(final_source)
    task_dir = run / "inputs" / "tasks" / "task-a"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(json.dumps({"benchmark": {"base_commit": base}}))
    patch = _git("diff", "--binary", base, cwd=workspace) + "\n"
    (run / label / "verifiers" / "task-a" / "attempt-01" / "patch.diff").write_text(patch)
    return patch


def test_aligns_every_attempt_and_only_analyzes_matched_successes(tmp_path: Path) -> None:
    patch = (
        "diff --git a/module.txt b/module.txt\n"
        "--- a/module.txt\n"
        "+++ b/module.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    baseline = _write_arm(
        tmp_path / "baseline-run", "baseline", {1: "resolved", 2: "unresolved"}, patch=patch
    )
    treatment = _write_arm(
        tmp_path / "treatment-run", "treatment", {1: "resolved", 2: "resolved"}, patch=patch
    )

    report = build_quality_report(baseline, treatment)

    alignment = report["attempt_alignment"]
    assert alignment["aligned_attempts"] == 2
    assert alignment["matched_success_alignments"] == 1
    assert alignment["baseline_resolved_attempts"] == 1
    assert alignment["treatment_resolved_attempts"] == 2
    assert alignment["excluded_alignment_count"] == 1
    assert alignment["matched_success_fraction"] == 0.5
    assert alignment["alignments"][1] == {
        "task_id": "task-a",
        "attempt": 2,
        "baseline_outcome": "unresolved",
        "treatment_outcome": "resolved",
        "included_in_quality_population": False,
    }
    assert "does not imply shared randomness" in alignment["method"]
    assert len(report["quality_population"]) == 1
    assert "median_paired" not in json.dumps(report["aggregates"])


def test_seeded_tasks_use_agent_change_patch(tmp_path: Path) -> None:
    whole_patch = (
        "diff --git a/module.txt b/module.txt\n--- a/module.txt\n+++ b/module.txt\n"
        "@@ -1 +1,3 @@\n-old\n+one\n+two\n+three\n"
    )
    agent_patch = (
        "diff --git a/module.txt b/module.txt\n--- a/module.txt\n+++ b/module.txt\n"
        "@@ -1 +1 @@\n-old\n+one\n"
    )
    baseline = _write_arm(
        tmp_path / "baseline-run",
        "baseline",
        {1: "resolved"},
        seeded=True,
        patch=whole_patch,
        agent_patch=agent_patch,
    )
    treatment = _write_arm(
        tmp_path / "treatment-run",
        "treatment",
        {1: "resolved"},
        seeded=True,
        patch=whole_patch,
        agent_patch=agent_patch,
    )

    report = build_quality_report(baseline, treatment)

    attempt = report["quality_population"][0]["baseline"]
    assert attempt["patch_file"] == "agent-change.diff"
    assert attempt["diff"]["lines_added"] == 1


def test_diff_metrics_separate_source_tests_and_manifests() -> None:
    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,3 @@\n-old\n+new\n+if ready:\n+    run()\n"
        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-old test\n+new test\n"
        "diff --git a/package.json b/package.json\n"
        '--- a/package.json\n+++ b/package.json\n@@ -1 +1 @@\n-{}\n+{"x": 1}\n'
    )

    metrics = analyze_patch(patch)

    assert metrics["changed_paths"] == ["package.json", "src/app.py", "tests/test_app.py"]
    assert metrics["files_changed"] == 3
    assert metrics["source_files_changed"] == 1
    assert metrics["test_files_changed"] == 1
    assert metrics["lines_added"] == 5
    assert metrics["lines_removed"] == 3
    assert metrics["churn"] == 8
    assert metrics["source_lines_added"] == 3
    assert metrics["source_lines_removed"] == 1
    assert metrics["source_churn"] == 4
    assert metrics["test_lines_added"] == 1
    assert metrics["test_lines_removed"] == 1
    assert metrics["test_churn"] == 2
    assert metrics["dependency_manifest_changes"] == ["package.json"]
    assert metrics["binary_patch"] is False
    assert metrics["empty_patch"] is False

    assert analyze_patch("")["empty_patch"] is True
    binary = analyze_patch(
        "diff --git a/logo.png b/logo.png\nnew file mode 100644\nGIT binary patch\nliteral 1\nA\n"
    )
    assert binary["binary_patch"] is True
    assert binary["files_changed"] == 1


@pytest.mark.parametrize("name", ["spaced module.py", "café.py"])
def test_git_generated_paths_are_decoded_without_shell_rules(tmp_path: Path, name: str) -> None:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    source = tmp_path / name
    source.write_text("value = 1\n")
    _git("add", name, cwd=tmp_path)
    _git("-c", "commit.gpgsign=false", "commit", "-m", "base", cwd=tmp_path)
    source.write_text("value = 2\n")

    metrics = analyze_patch(_git("diff", "--binary", "HEAD", cwd=tmp_path))

    assert metrics["changed_paths"] == [name]
    assert metrics["source_files_changed"] == 1
    assert metrics["lines_added"] == metrics["lines_removed"] == 1


def test_git_generated_hunk_payload_is_not_a_file_marker(tmp_path: Path) -> None:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    source = tmp_path / "counter.js"
    source.write_text("-- counter;\n")
    _git("add", source.name, cwd=tmp_path)
    _git("-c", "commit.gpgsign=false", "commit", "-m", "base", cwd=tmp_path)
    source.write_text("++ counter;\n")

    metrics = analyze_patch(_git("diff", "--binary", "HEAD", cwd=tmp_path))

    assert metrics["changed_paths"] == [source.name]
    assert metrics["lines_added"] == metrics["lines_removed"] == 1


def test_python_ast_metrics_compare_starting_tree_with_final_workspace(tmp_path: Path) -> None:
    baseline_run = tmp_path / "baseline-run"
    treatment_run = tmp_path / "treatment-run"
    baseline = _write_arm(baseline_run, "baseline", {1: "resolved"})
    treatment = _write_arm(treatment_run, "treatment", {1: "resolved"})
    final = (
        "from math import sqrt\n"
        "import os\n\n"
        "def existing(value):\n"
        "    if value:\n"
        "        return sqrt(value)\n"
        "    return 0\n\n"
        "def helper(value):\n"
        "    return existing(value)\n\n"
        "def helper_two(value):\n"
        "    return existing(value)\n\n"
        "result = helper(4) + helper(16) + helper_two(9)\n"
    )
    _write_python_workspace(baseline_run, "baseline", final)
    _write_python_workspace(treatment_run, "treatment", final)

    report = build_quality_report(baseline, treatment)

    ast_metrics = report["quality_population"][0]["baseline"]["python_ast"]
    assert ast_metrics["function_definitions"] == {"before": 1, "after": 3, "delta": 2}
    assert ast_metrics["class_definitions"] == {"before": 0, "after": 0, "delta": 0}
    assert ast_metrics["import_statements"] == {"before": 1, "after": 2, "delta": 1}
    assert ast_metrics["explicit_decision_points"]["delta"] == 1
    assert ast_metrics["existing_local_symbol_calls"]["delta"] == 2
    assert ast_metrics["new_single_use_helpers"] == ["module.py:helper_two"]
    assert ast_metrics["new_helper_call_counts"] == {
        "module.py:helper": 2,
        "module.py:helper_two": 1,
    }
    assert ast_metrics["new_helpers_called_at_least_twice_count"] == 1
    assert ast_metrics["new_top_level_public_symbols"] == [
        "module.py:helper",
        "module.py:helper_two",
    ]
    assert ast_metrics["new_top_level_public_symbol_count"] == 2
    assert ast_metrics["duplicate_new_function_body_count"] == 1


def test_repeated_qualified_definitions_count_actual_nodes(tmp_path: Path) -> None:
    baseline_run = tmp_path / "baseline-run"
    treatment_run = tmp_path / "treatment-run"
    baseline = _write_arm(baseline_run, "baseline", {1: "resolved"})
    treatment = _write_arm(treatment_run, "treatment", {1: "resolved"})
    final = (
        "def answer():\n    return 1\n\n"
        "def answer():\n    return 2\n\n"
        "class Container:\n    pass\n\n"
        "class Container:\n    pass\n"
    )
    _write_python_workspace(baseline_run, "baseline", final)
    _write_python_workspace(treatment_run, "treatment", final)

    metrics = build_quality_report(baseline, treatment)["quality_population"][0]["baseline"][
        "python_ast"
    ]

    assert metrics["function_definitions"] == {"before": 1, "after": 2, "delta": 1}
    assert metrics["class_definitions"] == {"before": 0, "after": 2, "delta": 2}


def test_rejects_stale_workspace_before_ast_analysis(tmp_path: Path) -> None:
    baseline_run = tmp_path / "baseline-run"
    treatment_run = tmp_path / "treatment-run"
    baseline = _write_arm(baseline_run, "baseline", {1: "resolved"})
    treatment = _write_arm(treatment_run, "treatment", {1: "resolved"})
    final = "def answer():\n    return 42\n"
    _write_python_workspace(baseline_run, "baseline", final)
    _write_python_workspace(treatment_run, "treatment", final)
    stale = baseline_run / "baseline" / "workspaces" / "task-a" / "attempt-01" / "module.py"
    stale.write_text("def answer():\n    return 0\n")

    with pytest.raises(ValueError, match="workspace no longer matches selected saved patch"):
        build_quality_report(baseline, treatment)


def test_parse_failure_marks_ast_unavailable_and_excludes_ast_aggregates(
    tmp_path: Path,
) -> None:
    baseline_run = tmp_path / "baseline-run"
    treatment_run = tmp_path / "treatment-run"
    baseline = _write_arm(baseline_run, "baseline", {1: "resolved"})
    treatment = _write_arm(treatment_run, "treatment", {1: "resolved"})
    invalid = "def broken(:\n    pass\n"
    _write_python_workspace(baseline_run, "baseline", "def answer():\n    return 42\n")
    _write_python_workspace(treatment_run, "treatment", invalid)

    report = build_quality_report(baseline, treatment)

    assert report["quality_population"][0]["baseline"]["python_ast"]["available"] is True
    metrics = report["quality_population"][0]["treatment"]["python_ast"]
    assert metrics["available"] is False
    assert metrics["parse_error_count"] == 1
    assert metrics["unavailable_reason"] == "production_python_parse_incomplete"
    assert "function_definition_delta" not in report["aggregates"]["distributions"]["baseline"]


def test_test_only_python_patch_is_excluded_from_ast_metrics(tmp_path: Path) -> None:
    patch = (
        "diff --git a/tests/test_module.py b/tests/test_module.py\n"
        "--- a/tests/test_module.py\n"
        "+++ b/tests/test_module.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n"
    )
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved"}, patch=patch)
    treatment = _write_arm(tmp_path / "treatment-run", "treatment", {1: "resolved"}, patch=patch)

    report = build_quality_report(baseline, treatment)

    metrics = report["quality_population"][0]["baseline"]["python_ast"]
    assert metrics == {
        "available": False,
        "unavailable_reason": "no_changed_production_python_files",
        "changed_python_files": 0,
        "analyzed_python_files": 0,
        "parse_error_count": 0,
        "parse_errors": [],
    }
    assert "function_definition_delta" not in report["aggregates"]["distributions"]["baseline"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"fingerprint": "sha256:other"}, "treatment compatibility fingerprint is invalid"),
        ({"checksum": "sha256:other"}, "task checksums differ"),
    ],
)
def test_rejects_incompatible_arms(tmp_path: Path, change: dict[str, str], message: str) -> None:
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved"})
    treatment = _write_arm(
        tmp_path / "treatment-run",
        "treatment",
        {1: "resolved"},
        fingerprint=change.get("fingerprint"),
    )
    if "checksum" in change:
        arm = load_arm(treatment / "arm.json")
        arm.task_checksums["task-a"] = change["checksum"]
        save_arm(treatment / "arm.json", arm)

    with pytest.raises(ValueError, match=message):
        build_quality_report(baseline, treatment)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("model_identifiers", "actual model identifiers differ"),
        ("compatibility_payload", "treatment compatibility fingerprint is invalid"),
    ],
)
def test_rejects_tampered_compatibility_evidence(tmp_path: Path, field: str, message: str) -> None:
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved"})
    treatment = _write_arm(tmp_path / "treatment-run", "treatment", {1: "resolved"})
    arm = load_arm(treatment / "arm.json")
    if field == "model_identifiers":
        arm.model_identifiers = ["different-model"]
    else:
        arm.compatibility_payload["model"] = "tampered"
    save_arm(treatment / "arm.json", arm)

    with pytest.raises(ValueError, match=message):
        build_quality_report(baseline, treatment)


def test_rejects_symmetric_missing_declared_verifier_attempts(tmp_path: Path) -> None:
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved", 2: "resolved"})
    treatment = _write_arm(tmp_path / "treatment-run", "treatment", {1: "resolved", 2: "resolved"})
    for arm in (baseline, treatment):
        attempt = arm / "verifiers" / "task-a" / "attempt-02"
        for artifact in attempt.iterdir():
            artifact.unlink()
        attempt.rmdir()

    with pytest.raises(ValueError, match="baseline verifier artifacts.*missing task-a/attempt-02"):
        build_quality_report(baseline, treatment)


def test_rejects_verifier_attempt_beyond_declared_count(tmp_path: Path) -> None:
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved"})
    treatment = _write_arm(tmp_path / "treatment-run", "treatment", {1: "resolved"})
    unexpected = baseline / "verifiers" / "task-a" / "attempt-02"
    unexpected.mkdir()
    (unexpected / "predictions.jsonl").write_text(
        json.dumps({"instance_id": "task-a", "model_patch": ""}) + "\n"
    )
    (unexpected / "patch.diff").write_text("")
    (unexpected / "swebench-summary.json").write_text(json.dumps({"resolved_ids": ["task-a"]}))

    with pytest.raises(
        ValueError, match="baseline verifier artifacts.*unexpected task-a/attempt-02"
    ):
        build_quality_report(baseline, treatment)


def test_writes_json_markdown_and_cli_accepts_run_directories(tmp_path: Path) -> None:
    baseline_run = tmp_path / "baseline-run"
    treatment_run = tmp_path / "treatment-run"
    _write_arm(baseline_run, "baseline", {1: "resolved"})
    _write_arm(treatment_run, "treatment", {1: "resolved"})
    batch = treatment_run / "treatment" / "batches" / "0001"
    batch.mkdir(parents=True)
    (batch / "promptfoo-results.json").write_text(
        json.dumps(
            {
                "results": {
                    "results": [
                        {
                            "vars": {
                                "trial_id": "task-a--attempt-01",
                                "task_id": "task-a",
                                "attempt": "1",
                            },
                            "latencyMs": 1500,
                            "cost": 0.02,
                            "response": {
                                "tokenUsage": {"prompt": 10, "cached": 3, "completion": 4}
                            },
                        }
                    ]
                }
            }
        )
    )
    output = tmp_path / "quality"

    result = CliRunner().invoke(
        app,
        ["quality", str(baseline_run), str(treatment_run), "--output", str(output)],
    )

    assert result.exit_code == 0, result.stdout
    assert (output / "quality-report.json").is_file()
    assert (output / "quality-report.md").is_file()
    data = json.loads((output / "quality-report.json").read_text())
    treatment = data["quality_population"][0]["treatment"]
    assert "first_generation" not in treatment
    markdown = (output / "quality-report.md").read_text()
    assert "# Successful-patch quality report" in markdown
    assert "Baseline resolved verifier attempts | 1" in markdown
    assert "Matched-success fraction | 1" in markdown
    assert "does not imply shared randomness" in markdown
    assert "Treatment - baseline median" in markdown
    assert "Correction turns and human review time are not measured" in markdown
    assert "normal PluginBench reports" in markdown
    assert "Reports:" in result.stdout

    json_path, markdown_path = write_quality_reports(output, data)
    assert json_path.name == "quality-report.json"
    assert markdown_path.name == "quality-report.md"


def test_rejects_mislabeled_arms(tmp_path: Path) -> None:
    baseline = _write_arm(tmp_path / "baseline-run", "baseline", {1: "resolved"})
    wrong_treatment = _write_arm(tmp_path / "wrong-treatment-run", "baseline", {1: "resolved"})

    with pytest.raises(ValueError, match="treatment input must contain arm=treatment"):
        build_quality_report(baseline, wrong_treatment)
