from pathlib import Path

import pytest

from conftest import write_evaluation_config, write_swebench_config

from pluginbench.config import ConfigOverrides, load_config


def test_loads_promptfoo_config_and_resolves_local_paths(tmp_path: Path) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")

    config = load_config(config_path)

    assert config.dataset.adapter == "local"
    assert config.dataset.source == (tmp_path / "tasks.yaml").resolve()
    assert config.agent.model == "gpt-5.6-luna"
    assert config.agent.auth_file == (tmp_path / "auth.json").resolve()
    assert config.tooling.promptfoo_version == "0.122.2"


def test_applies_important_cli_overrides(tmp_path: Path) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")

    config = load_config(
        config_path,
        ConfigOverrides(
            task_ids=("task-b",),
            model="gpt-5.6-terra",
            reasoning="max",
            attempts=2,
            concurrency=1,
            max_tasks=1,
            skip_baseline=True,
        ),
    )

    assert config.dataset.task_ids == ["task-b"]
    assert config.agent.model == "gpt-5.6-terra"
    assert config.agent.reasoning == "max"
    assert config.execution.attempts == 2
    assert config.execution.concurrency == 1
    assert config.execution.max_tasks == 1
    assert config.execution.baseline.mode == "skip"


def test_rejects_moving_dataset_version(tmp_path: Path) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    config_path.write_text(config_path.read_text().replace('version: "1"', "version: latest"))

    with pytest.raises(ValueError, match="immutable"):
        load_config(config_path)


def test_resolves_swebench_snapshot_and_harness_paths(tmp_path: Path) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")

    config = load_config(config_path)

    assert config.dataset.source == (tmp_path / "swebench.json").resolve()
    assert config.dataset.harness is not None
    assert config.dataset.harness.executable == (tmp_path / "bin" / "swebench").resolve()
    assert config.dataset.harness.version == "5.0.2"


def test_swebench_requires_a_native_harness(tmp_path: Path) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    config_path.write_text(
        config_path.read_text().replace(
            "  harness:\n    executable: bin/swebench\n    version: 5.0.2\n"
            "    image_platform: linux/amd64\n",
            "",
        )
    )

    with pytest.raises(ValueError, match="dataset.harness is required"):
        load_config(config_path)


def test_swebench_requires_one_trial_per_isolated_container(tmp_path: Path) -> None:
    config_path = write_swebench_config(tmp_path / "eval.yaml")
    config_path.write_text(config_path.read_text().replace("batch_size: 1", "batch_size: 2"))

    with pytest.raises(ValueError, match="batch_size=1 and concurrency=1"):
        load_config(config_path)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("  source: tasks.yaml\n", "", "dataset.source"),
        ("  source: tasks.yaml\n", "  source: null\n", "dataset.source"),
        ("  path: skills\n", "", "skill"),
        ("  auth_file: auth.json\n", "  auth_file: null\n", "agent.auth_file"),
    ],
)
def test_missing_or_null_required_paths_are_validation_errors(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    config_path.write_text(config_path.read_text().replace(old, new))

    with pytest.raises(ValueError, match=expected):
        load_config(config_path)


def test_skill_override_rejects_a_non_mapping_skill_section(tmp_path: Path) -> None:
    config_path = write_evaluation_config(tmp_path / "eval.yaml")
    config_path.write_text(config_path.read_text().replace("skill:\n  path: skills", "skill: []"))

    with pytest.raises(ValueError, match="configuration sections must be YAML mappings"):
        load_config(config_path, ConfigOverrides(skill=tmp_path / "skills"))
