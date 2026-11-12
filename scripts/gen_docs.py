"""Generate reference pages from config models and report metric definitions.

Run from the repository root with PYTHONPATH=src python scripts/gen_docs.py.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path
from typing import get_args, get_origin

from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo

from pluginbench.assessment_charts import METRIC_GUIDE
from pluginbench.compare import Gates, TOKEN_METRICS
from pluginbench.evaluation import Evaluation

ROOT = Path(__file__).resolve().parents[1]

# These models live in pinned launcher files, whose source bytes must stay unchanged.
# Prefer Field.description when present; validate these keys against the live models.
PINNED_DESCRIPTIONS = {
    "Treatment": {
        "guidance": "Selects UTF-8 .md or .txt instruction files relative to the config file.",
        "skills": "Selects standalone skill roots or collections relative to the config file.",
        "plugins": "Selects complete native plugin directories relative to the config file.",
    },
    "NativeCapabilities": {
        "subagents": "Enables native agents for vanilla and every treatment.",
        "max_threads": "Limits concurrent native children to between one and sixteen.",
    },
}


def nested_models(annotation: object, path: str) -> Iterator[tuple[str, type[BaseModel]]]:
    """Follow optional models and model values inside lists and named mappings."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield path, annotation
        return
    origin = get_origin(annotation)
    suffix = "[]" if origin is list else ".<name>" if origin is dict else ""
    for argument in get_args(annotation):
        yield from nested_models(argument, path + suffix)


def config_fields(
    model: type[BaseModel], prefix: str = ""
) -> Iterator[tuple[str, type[BaseModel], str, FieldInfo]]:
    """Visit every field, including optional models and named treatment/profile values."""
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        yield path, model, name, field
        for child_path, child in nested_models(field.annotation, path):
            yield from config_fields(child, child_path)


def description(model: type[BaseModel], name: str, field: FieldInfo) -> str:
    return field.description or PINNED_DESCRIPTIONS.get(model.__name__, {}).get(name, "")


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def config_reference() -> str:
    lines = [
        "# Configuration reference",
        "",
        "Regenerate with `PYTHONPATH=src python scripts/gen_docs.py` from the repository root.",
        "",
        "Use YAML or JSON; paths are relative to the config file.",
        "Unknown fields are rejected. `<name>` denotes a mapping key; `[]` denotes a list item.",
        "Required applies within the containing object, including optional objects when supplied.",
        "Defaults are JSON values; `—` means no default. Validators also enforce combinations of fields.",
        "See [running evaluations](running-evaluations.md) for valid combinations and execution requirements.",
        "The no-guidance arm is `vanilla` in configuration, gates, and public report files.",
        "See [report format](metrics.md#report-files) for schema versions and legacy report compatibility.",
        "",
    ]
    for heading, model in (("Evaluation file", Evaluation), ("Gates file", Gates)):
        lines += [
            f"## {heading}",
            "",
            "| Key path | Type | Default | Required | Description |",
            "| --- | --- | --- | --- | --- |",
        ]
        for path, owner, name, field in config_fields(model):
            annotation = re.sub(
                r"(?:pluginbench\.\w+\.|typing\.|pathlib\.)",
                "",
                inspect.formatannotation(field.annotation),
            )
            default = (
                "—"
                if field.is_required()
                else TypeAdapter(object)
                .dump_json(field.get_default(call_default_factory=True))
                .decode()
            )
            values = (
                f"`{path}`",
                f"`{annotation}`",
                f"`{default}`",
                "yes" if field.is_required() else "no",
                description(owner, name, field),
            )
            lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        lines.append("")
    return "\n".join(lines)


def metrics_reference() -> str:
    lines = [
        "# Metrics glossary",
        "",
        "Regenerate with `PYTHONPATH=src python scripts/gen_docs.py` from the repository root.",
        "",
        "Metric names below are the exact strings accepted in a gate's `metrics` list.",
        "Definitions and units come from `METRIC_GUIDE` in `src/pluginbench/assessment_charts.py`.",
        "Point gates choose their direction; the code defines no universal better direction for each metric.",
        "Paired token bounds require both Input tokens and Output tokens, with `direction: lower`.",
        "Higher coverage or lower cost alone does not establish a better solution.",
        "Correctness gates use `correctness: passes` or `correctness: unknown` instead of metric names.",
        "See [running evaluations](running-evaluations.md#compare-runs) for gate statistics and bounds.",
        "",
        "| Metric | What it measures | Unit | Gates-file name | Direction |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, (title, unit, explanation) in METRIC_GUIDE.items():
        direction = "Lower for paired token bounds" if name in TOKEN_METRICS else "Gate chooses"
        values = (title, explanation, unit, f"`{name}`", direction)
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines += [
        "",
        "## Report files",
        "",
        "`report/results.json`, `report/results.csv`, and `report/report.html` name the no-guidance arm `vanilla`.",
        "CSV column names are unchanged; the `arm` column now contains `vanilla` instead of `baseline`.",
        "The JSON format is `schema_version: 2`. Version 1 called this arm `baseline`.",
        "`compare`, `status`, and `judge prepare` accept both report versions without rewriting them.",
        "",
        "| Version 1 | Version 2 | Location |",
        "| --- | --- | --- |",
        "| `baseline` | `vanilla` | `arm` and `guidance` values; `completeness_matrix[].cells` keys |",
        "| `baseline_median` | `vanilla_median` | `comparisons[]` |",
        "| `baseline_assigned` | `vanilla_assigned` | `comparisons[]` |",
        "| `baseline_measured` | `vanilla_measured` | `medians[]`, `raw_medians[]`, `comparisons[]` |",
        "| `baseline_value` | `vanilla_value` | `aggregates[]` |",
        "| `baseline_origin` | `vanilla_origin` | `attempts[]` |",
        "| `reused_baseline_attempts` | `reused_vanilla_attempts` | Top-level field |",
        "",
        "Arm values change in attempts, observations, medians, comparisons, aggregates, reference comparisons and aggregates,",
        "passing medians and aggregates, review findings, cost rows, and measurement completeness rows.",
        "HTML uses the same public data, labels, and vanilla-first ordering.",
        "Opaque attempt IDs, evidence paths, provenance, and comparison identities keep their stored values.",
        "Evidence files, `inputs/study.json`, run identities, and cache keys still use `baseline` internally.",
        "Existing runs and vanilla caches can resume and reuse without migration.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    for name, contents in (
        ("config-reference.md", config_reference()),
        ("metrics.md", metrics_reference()),
    ):
        (ROOT / "docs" / name).write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
