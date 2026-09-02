from pathlib import Path
from typing import Any

import pytest

from conftest import load_evaluation_config

from pluginbench.config import ConfigOverrides, PluginbenchConfig
from pluginbench.datasets import load_tasks
from pluginbench.experiment import compatibility_payload, validate_baseline_compatibility
from pluginbench.results import ArmResult, canonical_fingerprint


def _payload(tmp_path: Path, **overrides: str) -> tuple[PluginbenchConfig, dict[str, Any]]:
    config = load_evaluation_config(
        tmp_path,
        ConfigOverrides(model=overrides.get("model")),
    )
    return config, compatibility_payload(
        config,
        load_tasks(config.dataset),
        promptfoo_version=overrides.get("promptfoo_version", "0.122.2"),
    )


def test_fingerprint_is_stable_and_excludes_skill_content(tmp_path: Path) -> None:
    config, first = _payload(tmp_path)
    config.skill.path.joinpath("review", "SKILL.md").write_text("changed treatment\n")
    second = compatibility_payload(
        config,
        load_tasks(config.dataset),
        promptfoo_version="0.122.2",
    )

    assert canonical_fingerprint(first) == canonical_fingerprint(second)


def test_fingerprint_includes_promptfoo_version(tmp_path: Path) -> None:
    _, first = _payload(tmp_path, promptfoo_version="0.122.2")
    _, second = _payload(tmp_path, promptfoo_version="0.123.0")

    assert canonical_fingerprint(first) != canonical_fingerprint(second)


def test_baseline_mismatch_names_changed_field(tmp_path: Path) -> None:
    _, payload = _payload(tmp_path)
    baseline = ArmResult.from_tasks(
        arm="baseline",
        compatibility_fingerprint=canonical_fingerprint(payload),
        compatibility_payload=payload,
        tasks={},
    )
    _, changed_payload = _payload(tmp_path, model="gpt-5.6-terra")

    with pytest.raises(ValueError, match="agent.model"):
        validate_baseline_compatibility(baseline, expected_payload=changed_payload)


def test_rejects_partial_baseline_before_reuse(tmp_path: Path) -> None:
    _, payload = _payload(tmp_path)
    baseline = ArmResult.from_tasks(
        arm="baseline",
        compatibility_fingerprint=canonical_fingerprint(payload),
        compatibility_payload=payload,
        tasks={},
    )

    with pytest.raises(ValueError, match="task set"):
        validate_baseline_compatibility(baseline, expected_payload=payload)


def test_rejects_treatment_arm_as_reused_baseline(tmp_path: Path) -> None:
    _, payload = _payload(tmp_path)
    treatment = ArmResult.from_tasks(
        arm="treatment",
        compatibility_fingerprint=canonical_fingerprint(payload),
        compatibility_payload=payload,
        tasks={},
    )

    with pytest.raises(ValueError, match="reuse requires a baseline arm"):
        validate_baseline_compatibility(treatment, expected_payload=payload)
