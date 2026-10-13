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
    StrictStr,
    field_validator,
    model_validator,
)

Digest = str
MetricKind = Literal["fallow", "coverage"]


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
    id: Literal["handoff"]
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
    stages: list[StageInput] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def bound_before(self) -> AttemptInput:
        if self.before is None and self.before_evidence:
            raise ValueError("before evidence requires a source snapshot")
        return self


class AssessmentInput(InputModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    records: list[AttemptInput] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_records(self) -> AssessmentInput:
        if len({record.id for record in self.records}) != len(self.records):
            raise ValueError("assessment record IDs must be unique")
        return self


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
    provenance: dict[str, object]
    before_evidence: dict[str, str] | None = None
