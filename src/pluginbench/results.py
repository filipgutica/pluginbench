from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ArmName = Literal["baseline", "treatment"]


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Usage(ResultModel):
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class TaskResult(ResultModel):
    task_id: str
    attempts: int
    score: float | None
    passed: bool
    duration_seconds: float | None
    infrastructure_errors: list[str] = Field(default_factory=list)
    skill_calls: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


def _sum_complete_int(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_complete_float(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


class ArmResult(ResultModel):
    schema_version: Literal[1] = 1
    arm: ArmName
    compatibility_fingerprint: str
    compatibility_payload: dict[str, Any] = Field(default_factory=dict)
    tasks: dict[str, TaskResult]
    task_checksums: dict[str, str] = Field(default_factory=dict)
    promptfoo_version: str | None = None
    agent_versions: list[str] = Field(default_factory=list)
    model_identifiers: list[str] = Field(default_factory=list)
    skill_digest: str | None = None
    skill_source_path: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    tasks_attempted: int
    tasks_passed: int
    infrastructure_errors: dict[str, int]
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    known_cost_usd: float | None

    @model_validator(mode="after")
    def validate_derived_aggregates(self) -> ArmResult:
        errors = Counter(
            error for task in self.tasks.values() for error in task.infrastructure_errors
        )
        costs = [task.usage.cost_usd for task in self.tasks.values()]
        expected = {
            "tasks_attempted": len(self.tasks),
            "tasks_passed": sum(task.passed for task in self.tasks.values()),
            "infrastructure_errors": dict(sorted(errors.items())),
            "input_tokens": _sum_complete_int(
                [task.usage.input_tokens for task in self.tasks.values()]
            ),
            "cached_tokens": _sum_complete_int(
                [task.usage.cached_tokens for task in self.tasks.values()]
            ),
            "output_tokens": _sum_complete_int(
                [task.usage.output_tokens for task in self.tasks.values()]
            ),
            "cost_usd": _sum_complete_float(costs),
            "known_cost_usd": (
                sum(cost for cost in costs if cost is not None)
                if any(cost is not None for cost in costs)
                else None
            ),
        }
        mismatches = [name for name, value in expected.items() if getattr(self, name) != value]
        if mismatches:
            raise ValueError("arm aggregates do not match task results: " + ", ".join(mismatches))
        return self

    @classmethod
    def from_tasks(
        cls,
        *,
        arm: ArmName,
        compatibility_fingerprint: str,
        tasks: dict[str, TaskResult],
        **provenance: Any,
    ) -> ArmResult:
        errors = Counter(error for task in tasks.values() for error in task.infrastructure_errors)
        costs = [task.usage.cost_usd for task in tasks.values()]
        return cls(
            arm=arm,
            compatibility_fingerprint=compatibility_fingerprint,
            tasks=tasks,
            tasks_attempted=len(tasks),
            tasks_passed=sum(task.passed for task in tasks.values()),
            infrastructure_errors=dict(sorted(errors.items())),
            input_tokens=_sum_complete_int([task.usage.input_tokens for task in tasks.values()]),
            cached_tokens=_sum_complete_int([task.usage.cached_tokens for task in tasks.values()]),
            output_tokens=_sum_complete_int([task.usage.output_tokens for task in tasks.values()]),
            cost_usd=_sum_complete_float(costs),
            known_cost_usd=(
                sum(cost for cost in costs if cost is not None)
                if any(cost is not None for cost in costs)
                else None
            ),
            **provenance,
        )


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def fingerprint_differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences: list[str] = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                differences.append(child)
            else:
                differences.extend(fingerprint_differences(expected[key], actual[key], child))
        return differences
    if expected != actual:
        return [path or "<root>"]
    return []


def load_arm(path: Path) -> ArmResult:
    arm_path = path / "arm.json" if path.is_dir() else path
    return ArmResult.model_validate_json(arm_path.read_text())


def save_arm(path: Path, arm: ArmResult) -> None:
    path.write_text(arm.model_dump_json(indent=2) + "\n")
