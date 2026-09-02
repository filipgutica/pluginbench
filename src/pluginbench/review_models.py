from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewVerdict(StrEnum):
    ACCEPT = "accept"
    CHANGES_REQUIRED = "changes_required"


class FindingCategory(StrEnum):
    CORRECTNESS = "correctness"
    COMPATIBILITY = "compatibility"
    TESTING = "testing"
    MAINTAINABILITY = "maintainability"
    OVERENGINEERING = "overengineering"
    UNNECESSARY_SCOPE = "unnecessary_scope"
    API_CONTRACT = "api_contract"
    DEPENDENCY_CHURN = "dependency_churn"


class ReviewFinding(StrictModel):
    category: FindingCategory
    severity: Literal["blocker", "major", "minor"]
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    evidence: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ReviewResponse(StrictModel):
    verdict: ReviewVerdict
    findings: list[ReviewFinding]

    @model_validator(mode="after")
    def validate_verdict(self) -> ReviewResponse:
        if self.verdict == ReviewVerdict.ACCEPT and self.findings:
            raise ValueError("accept requires an empty findings list")
        if self.verdict == ReviewVerdict.CHANGES_REQUIRED and not self.findings:
            raise ValueError("changes_required requires at least one finding")
        return self


class ReviewUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ReviewAttemptResult(StrictModel):
    candidate_id: str
    attempt: int = Field(ge=1)
    response: ReviewResponse | None = None
    usage: ReviewUsage = Field(default_factory=ReviewUsage)
    duration_seconds: float | None = Field(default=None, ge=0)
    infrastructure_errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> ReviewAttemptResult:
        if self.response is None and not self.infrastructure_errors:
            raise ValueError("a missing response requires an infrastructure error")
        if self.response is not None and self.infrastructure_errors:
            raise ValueError("a valid response cannot also be an infrastructure failure")
        return self


class ReviewStudyConfig(StrictModel):
    name: str = Field(min_length=1)
    randomization_seed: str = Field(min_length=1)


class ReviewArmSource(StrictModel):
    arm: Path


class ReviewSourcesConfig(StrictModel):
    baseline: ReviewArmSource
    full: ReviewArmSource
    core: ReviewArmSource
    compatibility_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReviewDatasetConfig(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: Path
    task_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_dataset(self) -> ReviewDatasetConfig:
        if self.version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("dataset.version must be an immutable revision")
        if self.task_ids is not None:
            if not self.task_ids or any(not task_id.strip() for task_id in self.task_ids):
                raise ValueError("dataset.task_ids must contain exact non-empty IDs")
            if len(set(self.task_ids)) != len(self.task_ids):
                raise ValueError("dataset.task_ids must be unique")
        return self


class ReviewAuthConfig(StrictModel):
    mode: Literal["subscription"] = "subscription"
    codex_home: Path


class ReviewerConfig(StrictModel):
    provider: Literal["openai:codex-sdk"] = "openai:codex-sdk"
    codex_sdk_version: str | None = None
    model: str = Field(min_length=1)
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    attempts: int = Field(default=2, ge=2)
    timeout_seconds: float = Field(default=900, gt=0)
    concurrency: int = Field(default=1, ge=1)
    auth: ReviewAuthConfig


class ReviewToolingConfig(StrictModel):
    promptfoo_version: str = Field(min_length=1)
    container_image: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_version(self) -> ReviewToolingConfig:
        version = self.promptfoo_version
        if version.lower() in {"latest", "head", "main", "master"} or any(
            character in version for character in "*^~<>=| "
        ):
            raise ValueError("tooling.promptfoo_version must be exact")
        return self


class ReviewCalibrationConfig(StrictModel):
    enabled: bool = False
    oracle_result: Path | None = None
    evidence_manifest: Path | None = None

    @model_validator(mode="after")
    def validate_oracle(self) -> ReviewCalibrationConfig:
        if self.enabled and (self.oracle_result is None or self.evidence_manifest is None):
            raise ValueError(
                "calibration oracle_result and evidence_manifest are required when enabled"
            )
        if not self.enabled and (
            self.oracle_result is not None or self.evidence_manifest is not None
        ):
            raise ValueError("calibration evidence is only valid when calibration is enabled")
        return self


class GoldTaskEvidence(StrictModel):
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    patch_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class GoldCalibrationEvidence(StrictModel):
    schema_version: Literal[1]
    dataset_name: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    oracle_result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tasks: dict[str, GoldTaskEvidence] = Field(min_length=1)


class ReviewLimitsConfig(StrictModel):
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class ReviewEstimatesConfig(StrictModel):
    tokens_per_review: int | None = Field(default=None, ge=1)
    cost_per_review_usd: float | None = Field(default=None, ge=0)


class ReviewOutputConfig(StrictModel):
    runs_dir: Path = Path("runs/reviews")


class ReviewEvaluationConfig(StrictModel):
    schema_version: Literal[1]
    study: ReviewStudyConfig
    sources: ReviewSourcesConfig
    dataset: ReviewDatasetConfig
    reviewer: ReviewerConfig
    tooling: ReviewToolingConfig
    calibration: ReviewCalibrationConfig = Field(default_factory=ReviewCalibrationConfig)
    limits: ReviewLimitsConfig = Field(default_factory=ReviewLimitsConfig)
    estimates: ReviewEstimatesConfig = Field(default_factory=ReviewEstimatesConfig)
    output: ReviewOutputConfig = Field(default_factory=ReviewOutputConfig)


def _resolve(base: Path, value: str | Path) -> str:
    path = Path(value).expanduser()
    return str((base / path).resolve() if not path.is_absolute() else path.resolve())


def _resolve_paths(data: dict[str, Any], base: Path) -> None:
    sources = data.get("sources")
    if isinstance(sources, dict):
        for label in ("baseline", "full", "core"):
            source = sources.get(label)
            if isinstance(source, dict) and "arm" in source:
                source["arm"] = _resolve(base, source["arm"])
    dataset = data.get("dataset")
    if isinstance(dataset, dict) and "source" in dataset:
        dataset["source"] = _resolve(base, dataset["source"])
    reviewer = data.get("reviewer")
    if isinstance(reviewer, dict):
        auth = reviewer.get("auth")
        if isinstance(auth, dict) and "codex_home" in auth:
            auth["codex_home"] = _resolve(base, auth["codex_home"])
    calibration = data.get("calibration")
    if isinstance(calibration, dict):
        for key in ("oracle_result", "evidence_manifest"):
            if calibration.get(key) is not None:
                calibration[key] = _resolve(base, calibration[key])
    output = data.get("output")
    if isinstance(output, dict) and "runs_dir" in output:
        output["runs_dir"] = _resolve(base, output["runs_dir"])


def load_review_config(path: Path, *, task_ids: tuple[str, ...] = ()) -> ReviewEvaluationConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("review configuration must be a YAML mapping")
    _resolve_paths(raw, path.parent.resolve())
    if task_ids:
        raw.setdefault("dataset", {})["task_ids"] = list(task_ids)
    return ReviewEvaluationConfig.model_validate(raw)
