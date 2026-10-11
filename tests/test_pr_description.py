from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


def module(name: str) -> ModuleType:
    source = Path(__file__).parents[1] / "benchmarks/pr-description" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"pr_description_{name}", source)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path]:
    verify = module("verify")
    (tmp_path / "context.md").write_text("captured source")
    monkeypatch.setattr(
        verify,
        "PAYLOAD",
        {
            "input_sha256": {"context.md": hashlib.sha256(b"captured source").hexdigest()},
            "preference_guide_sha256": "frozen-preference-identity",
        },
    )
    (tmp_path / "PR.md").write_text(module("calibrate").GOOD_DRAFT)
    return verify, tmp_path


def test_grounded_draft_passes_without_mandatory_word_limit(
    submission: tuple[ModuleType, Path],
) -> None:
    verify, root = submission
    result = verify.evaluate(root)
    assert result["pass"] is True
    assert result["metrics"]["words"] > 100
    assert result["preference_guide_sha256"] == "frozen-preference-identity"
    assert result["feedback"] == []


def test_documented_executable_cli_path_counts_as_a_feature_command(
    submission: tuple[ModuleType, Path],
) -> None:
    verify, root = submission
    output = root / "PR.md"
    output.write_text(output.read_text().replace("node dist/cli.js", "./dist/cli.js"))
    result = verify.evaluate(root)
    assert result["criteria"]["preference_compliance"]["feature_command"] is True


def test_inline_expected_output_parameter_is_not_an_unfilled_body_placeholder(
    submission: tuple[ModuleType, Path],
) -> None:
    verify, root = submission
    output = root / "PR.md"
    output.write_text(
        output.read_text() + "\nThe header shows `filter: <port>` for the selected port.\n"
    )
    assert verify.evaluate(root)["criteria"]["preference_compliance"]["no_placeholders"] is True


@pytest.mark.parametrize(
    "edit,criterion,group",
    [
        (
            lambda text: text.replace("## Changes", "## Implementation"),
            "required_headings",
            "preference_compliance",
        ),
        (
            lambda text: text + "\n`npm run smoke` passed.\n",
            "no_routine_ci_echo",
            "preference_compliance",
        ),
        (
            lambda text: text + "\nI ran the picker locally.\n",
            "no_claimed_local_execution",
            "factual_and_delivery",
        ),
    ],
)
def test_known_misses_fail_the_owning_criterion(
    submission: tuple[ModuleType, Path], edit: Callable[[str], str], criterion: str, group: str
) -> None:
    verify, root = submission
    output = root / "PR.md"
    output.write_text(edit(output.read_text()))
    result = verify.evaluate(root)
    assert result["pass"] is False
    assert result["criteria"][group][criterion] is False
    assert {row["criterion"] for row in result["feedback"]} == {
        name
        for checks in result["criteria"].values()
        for name, passed in checks.items()
        if not passed
    }


def test_format_rejection_tells_an_unguided_writer_the_required_format(
    submission: tuple[ModuleType, Path],
) -> None:
    verify, root = submission
    output = root / "PR.md"
    output.write_text(output.read_text().replace("## Changes", "## Implementation"))
    result = verify.evaluate(root)
    feedback = next(row for row in result["feedback"] if row["criterion"] == "required_headings")
    assert "Description, Changes, Verification" in feedback["required_correction"]
    assert "Implementation" in feedback["required_correction"]


def test_source_edits_and_extra_files_fail_delivery(submission: tuple[ModuleType, Path]) -> None:
    verify, root = submission
    (root / "context.md").write_text("changed")
    result = verify.evaluate(root)
    assert result["criteria"]["factual_and_delivery"]["captured_inputs_unchanged"] is False
    (root / "context.md").write_text("captured source")
    (root / "extra.md").write_text("unrequested")
    assert verify.evaluate(root)["pass"] is False


def test_output_symlink_cannot_substitute_for_delivered_description(
    submission: tuple[ModuleType, Path],
) -> None:
    verify, root = submission
    output = root / "PR.md"
    content = output.read_text()
    output.unlink()
    outside = root.parent / "outside.md"
    outside.write_text(content)
    output.symlink_to(outside)
    result = verify.evaluate(root)
    assert result["pass"] is False
    assert result["criteria"]["factual_and_delivery"]["output_present"] is False
