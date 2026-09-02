from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pluginbench.review_models import (
    FindingCategory,
    ReviewFinding,
    ReviewResponse,
    ReviewVerdict,
    load_review_config,
)


def _config(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "study": {"name": "saved-swebench-review", "randomization_seed": "seed-1"},
        "sources": {
            "baseline": {"arm": str(tmp_path / "baseline" / "arm.json")},
            "full": {"arm": str(tmp_path / "full" / "arm.json")},
            "core": {"arm": str(tmp_path / "core" / "arm.json")},
            "compatibility_fingerprint": "sha256:" + "a" * 64,
        },
        "dataset": {
            "name": "princeton-nlp/SWE-bench_Verified",
            "version": "revision-1",
            "source": str(tmp_path / "tasks.json"),
        },
        "reviewer": {
            "provider": "openai:codex-sdk",
            "model": "gpt-5.6-luna",
            "reasoning": "high",
            "attempts": 2,
            "timeout_seconds": 900,
            "concurrency": 1,
            "auth": {"mode": "subscription", "codex_home": str(tmp_path / "codex")},
        },
        "tooling": {
            "promptfoo_version": "0.122.2",
            "container_image": "pluginbench/promptfoo:0.122.2",
        },
        "calibration": {
            "enabled": True,
            "oracle_result": str(tmp_path / "gold.json"),
            "evidence_manifest": str(tmp_path / "gold-evidence.json"),
        },
        "limits": {"max_tokens": 2_000_000, "max_cost_usd": 20.0},
        "estimates": {"tokens_per_review": 30_000, "cost_per_review_usd": 0.25},
        "output": {"runs_dir": str(tmp_path / "runs")},
    }


def test_load_review_config_is_strict_and_resolves_paths(tmp_path: Path) -> None:
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(_config(tmp_path)), encoding="utf-8")

    config = load_review_config(path)

    assert config.sources.baseline.arm == (tmp_path / "baseline" / "arm.json").resolve()
    assert config.reviewer.attempts == 2

    data = _config(tmp_path)
    data["unexpected"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_review_config(path)


def test_review_response_requires_actionable_findings() -> None:
    finding = ReviewFinding(
        category=FindingCategory.CORRECTNESS,
        severity="major",
        file="src/example.py",
        line=12,
        evidence="The returned value drops the validated identifier.",
        required_action="Return the validated identifier from this branch.",
        confidence=0.9,
    )

    response = ReviewResponse(verdict=ReviewVerdict.CHANGES_REQUIRED, findings=[finding])
    assert response.findings == [finding]

    with pytest.raises(ValidationError):
        ReviewResponse(verdict=ReviewVerdict.CHANGES_REQUIRED, findings=[])
    with pytest.raises(ValidationError):
        ReviewResponse(verdict=ReviewVerdict.ACCEPT, findings=[finding])
    with pytest.raises(ValidationError):
        ReviewFinding(
            category="style",  # type: ignore[arg-type]
            severity="minor",
            file="src/example.py",
            line=1,
            evidence="Preference only.",
            required_action="Rename it.",
            confidence=0.5,
        )
