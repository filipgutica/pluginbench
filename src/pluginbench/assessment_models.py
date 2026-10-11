"""The offline assessment input contract; paths are relative to its owning document."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Digest = str
MetricKind = Literal["fallow", "coverage", "mutation"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ArtifactRef(InputModel):
    path: Path
    sha256: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: Path) -> Path:
        if value.is_absolute():
            raise ValueError("assessment paths must be relative to the manifest")
        return value


class Work(InputModel):
    elapsed_seconds: float | None = Field(default=None, strict=True, ge=0)
    tokens: dict[str, object] | None = None
    dollars: float | None = Field(default=None, strict=True, ge=0)
    token_format: str | None = None


class Runtime(InputModel):
    """Declared harness metadata; requested effort is never observed evidence."""

    harness: StrictStr | None = None
    harness_version: StrictStr | None = None
    requested_reasoning_effort: StrictStr | None = None
    reasoning_effort: StrictStr | None = None


class CorrectnessReceipt(InputModel):
    schema_version: Literal[1]
    snapshot_digest: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    checks: dict[str, StrictBool | None]
    command: list[str] = Field(min_length=1)
    tool: str = Field(min_length=1)
    tool_version: str | None = None


class StageInput(InputModel):
    id: str = Field(min_length=1)
    snapshot: ArtifactRef | None = None
    correctness: ArtifactRef | None = None
    evidence: dict[MetricKind, ArtifactRef] | None = None
    work: Work | None = None


class AttemptInput(InputModel):
    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    attempt: int = Field(default=1, strict=True, ge=1)
    arm: str | None = None
    model: str | None = None
    runtime: Runtime | None = None
    before: ArtifactRef | None = None
    before_evidence: dict[MetricKind, ArtifactRef] | None = None
    stages: list[StageInput] = Field(min_length=1)
    correction_ledger: ArtifactRef | None = None

    @model_validator(mode="after")
    def stage_order(self) -> AttemptInput:
        if self.before is None and self.before_evidence:
            raise ValueError("before evidence requires a source snapshot")
        ids = [stage.id for stage in self.stages]
        if ids[0] != "handoff" or len(set(ids)) != len(ids) or "before" in ids:
            raise ValueError(
                "stages must have unique IDs beginning with handoff; before is reserved"
            )
        return self


class AssessmentInput(InputModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    standards: Path | None = None
    records: list[AttemptInput] = Field(min_length=1)

    @field_validator("standards")
    @classmethod
    def relative_standards(cls, value: Path | None) -> Path | None:
        if value is not None and value.is_absolute():
            raise ValueError("standards path must be relative to the manifest")
        return value

    @model_validator(mode="after")
    def unique_records(self) -> AssessmentInput:
        if len({record.id for record in self.records}) != len(self.records):
            raise ValueError("assessment record IDs must be unique")
        return self


class Standards(InputModel):
    schema_version: Literal[1]
    allowed_paths: list[str] | None = None
    required_correctness_checks: list[str] = Field(
        default_factory=lambda: ["behavior"], min_length=1
    )
    required_metrics: list[MetricKind] = Field(default_factory=list)
    metrics: dict[MetricKind, dict[str, StrictInt | StrictFloat]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct_requirements(self) -> Standards:
        for values in (self.required_correctness_checks, self.required_metrics):
            if len(set(values)) != len(values) or any(not value.strip() for value in values):
                raise ValueError("required checks and metrics must be nonempty unique names")
        return self


class Correction(InputModel):
    issue_id: str = Field(min_length=1)
    round: int = Field(strict=True, ge=1)
    from_stage: str
    to_stage: str | None = None
    category: Literal["correctness", "scope", "maintainability", "tests"]
    external_verification: ArtifactRef | None = None


class SelfCleanup(InputModel):
    id: str = Field(min_length=1)
    from_stage: str
    to_stage: str


class ReviewRound(InputModel):
    round: int = Field(strict=True, ge=1)
    stage: str = Field(min_length=1)
    issue_ids: list[str]
    external_verification: ArtifactRef | None = None

    @field_validator("issue_ids")
    @classmethod
    def distinct_issues(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not item.strip() for item in value):
            raise ValueError("review issue IDs must be nonempty unique names")
        return value


class CorrectionLedger(InputModel):
    schema_version: Literal[1]
    stage_digests: dict[str, str]
    corrections: list[Correction]
    review_rounds: list[ReviewRound] | None = None
    self_cleanup: list[SelfCleanup] = Field(default_factory=list)
    completeness: ArtifactRef | None = None


class LedgerCompleteness(InputModel):
    """Recorded review completeness within a declared scope, not defect absence."""

    schema_version: Literal[1]
    stage_digests: dict[str, str]
    reviewer: str = Field(min_length=1)
    independent: StrictBool
    complete: StrictBool
    reviewed_stages: list[str] | None = Field(
        default=None,
        description="complete applies only to these stages; None preserves unspecified legacy scope. All stage_digests remain identity bindings.",
    )


class CorrectionVerification(InputModel):
    schema_version: Literal[1]
    issue_id: str
    round: int = Field(strict=True, ge=1)
    from_stage: str
    to_stage: str | None = None
    category: Literal["correctness", "scope", "maintainability", "tests"]
    stage_digests: dict[str, str]
    reviewer: str = Field(min_length=1)
    independent: StrictBool


class ReviewVerification(InputModel):
    schema_version: Literal[1]
    round: int = Field(strict=True, ge=1)
    stage: str
    issue_ids: list[str]
    stage_digests: dict[str, str]
    reviewer: str = Field(min_length=1)
    independent: StrictBool


@dataclass(frozen=True)
class Snapshot:
    root: Path
    digest: str


@dataclass(frozen=True)
class Stage:
    id: str
    snapshot: Snapshot | None
    correctness: CorrectnessReceipt | None
    evidence: dict[str, str] | None
    work: Work | None


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str
    attempt: int
    arm: str | None
    model: str | None
    before: Snapshot | None
    stages: tuple[Stage, ...]
    ledger: CorrectionLedger | None
    ledger_base: Path
    provenance: dict[str, object]
    before_evidence: dict[str, str] | None = None
