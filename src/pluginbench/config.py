"""Compatibility types for native local task catalogs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatasetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal["local"] = "local"
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: Path
    task_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dataset(self) -> DatasetConfig:
        if self.version.lower() in {"latest", "head", "main", "master"}:
            raise ValueError("dataset.version must be an immutable release or revision")
        if any(not task_id.strip() for task_id in self.task_ids):
            raise ValueError("dataset.task_ids must not contain empty IDs")
        if any(any(character in task_id for character in "*?[") for task_id in self.task_ids):
            raise ValueError("dataset.task_ids must be exact IDs, not glob patterns")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("dataset.task_ids must be unique")
        return self
