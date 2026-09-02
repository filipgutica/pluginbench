from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExperimentConfig(StrictModel):
    name: str = Field(min_length=1)


class SWEbenchHarnessConfig(StrictModel):
    executable: Path
    version: str = Field(min_length=1)
    image_platform: str | None = None

    @model_validator(mode="after")
    def validate_version(self) -> SWEbenchHarnessConfig:
        if self.version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("dataset.harness.version must be exact")
        if any(character in self.version for character in "*^~<>=| "):
            raise ValueError("dataset.harness.version must be exact")
        return self


class StartingPatchConfig(StrictModel):
    path: Path
    expected_score: Literal[0, 1]


class DatasetConfig(StrictModel):
    adapter: Literal["local", "swe-bench", "live-swe-bench"] = "local"
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: Path | str
    task_ids: list[str] = Field(min_length=1)
    harness: SWEbenchHarnessConfig | None = None
    starting_patches: dict[str, StartingPatchConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dataset(self) -> DatasetConfig:
        if self.adapter == "local":
            self.source = Path(self.source)
            if self.harness is not None:
                raise ValueError("dataset.harness is only valid for swe-bench")
        elif self.adapter == "swe-bench":
            self.source = Path(self.source)
            if self.source.suffix not in {".json", ".jsonl"}:
                raise ValueError("swe-bench source must be a pinned local .json or .jsonl snapshot")
            if self.harness is None:
                raise ValueError("dataset.harness is required for swe-bench")
        else:
            self.source = str(self.source)
            if self.harness is not None:
                raise ValueError("dataset.harness is only valid for swe-bench")
        if self.starting_patches and self.adapter != "swe-bench":
            raise ValueError("dataset.starting_patches is only valid for swe-bench")
        unselected = sorted(self.starting_patches.keys() - set(self.task_ids))
        if unselected:
            raise ValueError(
                "dataset.starting_patches contains an unselected task: " + ", ".join(unselected)
            )
        if self.version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("dataset.version must be an immutable release or revision")
        if any(not task_id.strip() for task_id in self.task_ids):
            raise ValueError("dataset.task_ids must not contain empty IDs")
        if any(any(character in task_id for character in "*?[") for task_id in self.task_ids):
            raise ValueError("dataset.task_ids must be exact IDs, not glob patterns")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("dataset.task_ids must be unique")
        return self


class SkillConfig(StrictModel):
    path: Path


class AgentConfig(StrictModel):
    provider: Literal["openai:codex-sdk"] = "openai:codex-sdk"
    codex_sdk_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    sandbox_mode: Literal["read-only", "workspace-write"] = "workspace-write"
    approval_policy: Literal["never"] = "never"
    network_access_enabled: bool = False
    web_search_enabled: bool = False
    auth_file: Path

    @model_validator(mode="after")
    def validate_versions(self) -> AgentConfig:
        if self.codex_sdk_version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("agent.codex_sdk_version must be exact")
        if any(character in self.codex_sdk_version for character in "*^~<>=| "):
            raise ValueError("agent.codex_sdk_version must be exact")
        return self


class ToolingConfig(StrictModel):
    promptfoo_version: str = Field(min_length=1)
    container_image: str | None = None

    @model_validator(mode="after")
    def validate_version(self) -> ToolingConfig:
        if self.promptfoo_version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("tooling.promptfoo_version must be exact")
        if any(character in self.promptfoo_version for character in "*^~<>=| "):
            raise ValueError("tooling.promptfoo_version must be exact")
        return self


class BaselineConfig(StrictModel):
    mode: Literal["run", "reuse", "skip"] = "run"
    result: Path | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> BaselineConfig:
        if self.mode == "reuse" and self.result is None:
            raise ValueError("baseline.result is required when mode is reuse")
        if self.mode != "reuse" and self.result is not None:
            raise ValueError("baseline.result is only valid when mode is reuse")
        return self


class ExecutionConfig(StrictModel):
    baseline: BaselineConfig = Field(default_factory=BaselineConfig)
    attempts: int = Field(default=1, ge=1)
    concurrency: int = Field(default=1, ge=1)
    batch_size: int = Field(default=1, ge=1)
    max_tasks: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=600, gt=0)


class LimitsConfig(StrictModel):
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_total_cost_usd: float | None = Field(default=None, gt=0)


class EstimatesConfig(StrictModel):
    tokens_per_trial: int | None = Field(default=None, ge=1)
    cost_per_trial_usd: float | None = Field(default=None, ge=0)


class DecisionConfig(StrictModel):
    minimum_pass_rate_lift_pp: float = 0.0
    maximum_cost_overhead_pct: float | None = None
    maximum_incremental_cost_per_additional_success_usd: float | None = None


class OutputConfig(StrictModel):
    runs_dir: Path = Path("runs")


class PluginbenchConfig(StrictModel):
    schema_version: Literal[1]
    experiment: ExperimentConfig
    dataset: DatasetConfig
    skill: SkillConfig
    agent: AgentConfig
    tooling: ToolingConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    estimates: EstimatesConfig = Field(default_factory=EstimatesConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def validate_task_limit(self) -> PluginbenchConfig:
        if len(self.dataset.task_ids) > self.execution.max_tasks:
            raise ValueError(
                f"dataset has {len(self.dataset.task_ids)} tasks, exceeding max_tasks "
                f"{self.execution.max_tasks}"
            )
        if self.dataset.adapter == "swe-bench":
            if self.tooling.container_image is None:
                raise ValueError(
                    "tooling.container_image is required for isolated SWE-bench trials"
                )
            if self.execution.batch_size != 1 or self.execution.concurrency != 1:
                raise ValueError("SWE-bench requires batch_size=1 and concurrency=1")
        return self


@dataclass(frozen=True)
class ConfigOverrides:
    baseline_result: Path | None = None
    skip_baseline: bool = False
    dataset: str | None = None
    dataset_version: str | None = None
    task_ids: tuple[str, ...] = ()
    skill: Path | None = None
    model: str | None = None
    reasoning: str | None = None
    attempts: int | None = None
    concurrency: int | None = None
    batch_size: int | None = None
    max_tasks: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    output: Path | None = None


def _resolve_path(base: Path, value: str | Path) -> str:
    path = Path(value).expanduser()
    return str((base / path).resolve() if not path.is_absolute() else path.resolve())


def _resolve_mapping_path(mapping: dict[str, Any], key: str, base: Path) -> None:
    value = mapping.get(key)
    if isinstance(value, str | Path):
        mapping[key] = _resolve_path(base, value)


def _resolve_config_paths(data: dict[str, Any], base: Path) -> None:
    dataset = data.get("dataset")
    if isinstance(dataset, dict):
        adapter = dataset.get("adapter", "local")
        source = dataset.get("source")
        if adapter == "local" or (
            adapter == "swe-bench"
            and isinstance(source, str)
            and Path(source).suffix in {".json", ".jsonl"}
        ):
            _resolve_mapping_path(dataset, "source", base)
        harness = dataset.get("harness")
        if isinstance(harness, dict) and "executable" in harness:
            _resolve_mapping_path(harness, "executable", base)
        starting_patches = dataset.get("starting_patches")
        if isinstance(starting_patches, dict):
            for starting_patch in starting_patches.values():
                if isinstance(starting_patch, dict):
                    _resolve_mapping_path(starting_patch, "path", base)
    skill = data.get("skill")
    if isinstance(skill, dict):
        _resolve_mapping_path(skill, "path", base)
    agent = data.get("agent")
    if isinstance(agent, dict):
        _resolve_mapping_path(agent, "auth_file", base)
    execution = data.get("execution")
    if isinstance(execution, dict):
        baseline = execution.get("baseline")
        if isinstance(baseline, dict) and baseline.get("result") is not None:
            _resolve_mapping_path(baseline, "result", base)
    output = data.get("output")
    if isinstance(output, dict) and "runs_dir" in output:
        _resolve_mapping_path(output, "runs_dir", base)


def _apply_overrides(data: dict[str, Any], overrides: ConfigOverrides, base: Path) -> None:
    if overrides.baseline_result is not None and overrides.skip_baseline:
        raise ValueError("--baseline and --skip-baseline are mutually exclusive")
    dataset = data.setdefault("dataset", {})
    agent = data.setdefault("agent", {})
    execution = data.setdefault("execution", {})
    limits = data.setdefault("limits", {})
    output = data.setdefault("output", {})
    skill = data.setdefault("skill", {}) if overrides.skill is not None else data.get("skill")
    sections = (dataset, agent, execution, limits, output)
    if not all(isinstance(value, dict) for value in sections) or (
        skill is not None and not isinstance(skill, dict)
    ):
        raise ValueError("configuration sections must be YAML mappings")

    if overrides.dataset is not None:
        dataset["name"] = overrides.dataset
    if overrides.dataset_version is not None:
        dataset["version"] = overrides.dataset_version
    if overrides.task_ids:
        dataset["task_ids"] = list(overrides.task_ids)
    if overrides.skill is not None:
        assert isinstance(skill, dict)
        skill["path"] = _resolve_path(base, overrides.skill)
    if overrides.model is not None:
        agent["model"] = overrides.model
    if overrides.reasoning is not None:
        agent["reasoning"] = overrides.reasoning
    if overrides.attempts is not None:
        execution["attempts"] = overrides.attempts
    if overrides.concurrency is not None:
        execution["concurrency"] = overrides.concurrency
    if overrides.batch_size is not None:
        execution["batch_size"] = overrides.batch_size
    if overrides.max_tasks is not None:
        execution["max_tasks"] = overrides.max_tasks

    baseline = execution.setdefault("baseline", {})
    if not isinstance(baseline, dict):
        raise ValueError("execution.baseline must be a YAML mapping")
    if overrides.baseline_result is not None:
        baseline.update(mode="reuse", result=_resolve_path(base, overrides.baseline_result))
    elif overrides.skip_baseline:
        baseline.update(mode="skip", result=None)
    if overrides.max_tokens is not None:
        limits["max_total_tokens"] = overrides.max_tokens
    if overrides.max_cost_usd is not None:
        limits["max_total_cost_usd"] = overrides.max_cost_usd
    if overrides.output is not None:
        output["runs_dir"] = _resolve_path(base, overrides.output)


def load_config(path: Path, overrides: ConfigOverrides | None = None) -> PluginbenchConfig:
    config_path = path.expanduser().resolve()
    try:
        loaded = yaml.safe_load(config_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read configuration {config_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a YAML mapping")
    data: dict[str, Any] = loaded
    _resolve_config_paths(data, config_path.parent)
    _apply_overrides(data, overrides or ConfigOverrides(), config_path.parent)
    return PluginbenchConfig.model_validate(data)
