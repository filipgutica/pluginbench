import json
from pathlib import Path

import pytest

from pluginbench.reporting import build_report, render_markdown
from pluginbench.results import (
    ArmName,
    ArmResult,
    TaskResult,
    Usage,
    canonical_fingerprint,
    load_arm,
    save_arm,
)


def _arm(arm: ArmName, scores: dict[str, float | None], *, cost: float | None) -> ArmResult:
    tasks = {
        task: TaskResult(
            task_id=task,
            attempts=1,
            score=score,
            passed=score is not None and score >= 1,
            duration_seconds=5,
            infrastructure_errors=[],
            usage=Usage(input_tokens=10, cached_tokens=2, output_tokens=5, cost_usd=cost),
        )
        for task, score in scores.items()
    }
    return ArmResult.from_tasks(
        arm=arm,
        compatibility_fingerprint=canonical_fingerprint({}),
        compatibility_payload={},
        tasks=tasks,
    )


def test_paired_report_calculates_lift_wins_and_overhead() -> None:
    baseline = _arm("baseline", {"a": 0, "b": 1}, cost=0.01)
    treatment = _arm("treatment", {"a": 1, "b": 1}, cost=0.02)

    report = build_report(
        baseline,
        treatment,
        minimum_pass_rate_lift_pp=0,
        maximum_cost_overhead_pct=200,
        maximum_incremental_cost_per_additional_success_usd=None,
    )

    assert report["comparison"]["treatment_wins"] == 1
    assert report["comparison"]["baseline_wins"] == 0
    assert report["comparison"]["unchanged_tasks"] == 1
    assert report["comparison"]["score_lift"] == 0.5
    assert report["comparison"]["cost_overhead_usd"] == 0.02
    assert report["decision"]["status"] == "justified"


def test_baseline_free_report_has_no_comparison_claims() -> None:
    treatment = _arm("treatment", {"a": 1}, cost=None)

    report = build_report(None, treatment)
    markdown = render_markdown(report)

    assert "comparison" not in report
    assert "decision" not in report
    assert "Skill lift" not in markdown
    assert "Unavailable" in markdown


def test_comparison_lift_uses_only_paired_scoreable_tasks() -> None:
    baseline = _arm("baseline", {"a": None, "b": 0}, cost=0.01)
    treatment = _arm("treatment", {"a": 1, "b": 0}, cost=0.01)

    report = build_report(baseline, treatment)

    assert report["comparison"]["paired_tasks"] == 1
    assert report["comparison"]["pass_rate_lift_pp"] == 0
    assert report["decision"]["status"] == "not_configured"


def test_pass_rate_threshold_alone_configures_the_decision() -> None:
    baseline = _arm("baseline", {"a": 0, "b": 0}, cost=0.01)
    treatment = _arm("treatment", {"a": 1, "b": 0}, cost=0.01)

    report = build_report(baseline, treatment, minimum_pass_rate_lift_pp=40)

    assert report["decision"]["status"] == "justified"
    assert report["decision"]["gates"]["minimum_pass_rate_lift_pp"]


def test_comparison_rejects_tampered_compatibility_payload() -> None:
    baseline = _arm("baseline", {"a": 0}, cost=0.01)
    treatment = _arm("treatment", {"a": 1}, cost=0.02)
    baseline.compatibility_payload = {"model": "tampered"}

    with pytest.raises(ValueError, match="baseline compatibility fingerprint is invalid"):
        build_report(baseline, treatment)


def test_comparison_rejects_treatment_arm_in_baseline_position() -> None:
    not_a_baseline = _arm("treatment", {"a": 0}, cost=0.01)
    treatment = _arm("treatment", {"a": 1}, cost=0.02)

    with pytest.raises(ValueError, match="baseline result must have arm=baseline"):
        build_report(not_a_baseline, treatment)


def test_saved_arm_rejects_tampered_derived_aggregates(tmp_path: Path) -> None:
    arm = _arm("baseline", {"a": 1}, cost=0.01)
    path = tmp_path / "arm.json"
    save_arm(path, arm)
    payload = json.loads(path.read_text())
    payload["tasks_passed"] = 0
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="arm aggregates do not match"):
        load_arm(path)
