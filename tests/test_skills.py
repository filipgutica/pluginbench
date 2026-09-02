from pathlib import Path
import stat

import pytest

from pluginbench.skills import inspect_skill_bundle


def test_inspects_bundle_and_digest_changes_with_content(tmp_path: Path) -> None:
    skill = tmp_path / "bundle" / "review"
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text("---\nname: review\ndescription: Review code.\n---\nreview once\n")

    first = inspect_skill_bundle(tmp_path / "bundle")
    skill_file.write_text("---\nname: review\ndescription: Review code.\n---\nreview twice\n")
    second = inspect_skill_bundle(tmp_path / "bundle")

    assert first.skill_names == ("review",)
    assert first.digest.startswith("sha256:")
    assert first.digest != second.digest


def test_bundle_digest_tracks_executable_modes_and_empty_directories(tmp_path: Path) -> None:
    skill = tmp_path / "bundle" / "review"
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text("---\nname: review\ndescription: Review code.\n---\nreview\n")
    helper = skill / "helper.sh"
    helper.write_text("#!/bin/sh\n")

    original = inspect_skill_bundle(tmp_path / "bundle").digest
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    executable = inspect_skill_bundle(tmp_path / "bundle").digest
    (skill / "empty").mkdir()
    with_empty_directory = inspect_skill_bundle(tmp_path / "bundle").digest

    assert original != executable
    assert executable != with_empty_directory


def test_rejects_directory_without_skill(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SKILL.md"):
        inspect_skill_bundle(tmp_path)


def test_rejects_skill_without_required_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "review"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Review carefully.\n")

    with pytest.raises(ValueError, match="frontmatter"):
        inspect_skill_bundle(skill)


def test_rejects_symlink_that_escapes_bundle(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    skill = tmp_path / "bundle" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: Review code.\n---\nreview\n")
    (skill / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        inspect_skill_bundle(tmp_path / "bundle")


def test_rejects_nested_bundle_layout(tmp_path: Path) -> None:
    nested = tmp_path / "bundle" / "group" / "review"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: review\ndescription: Review code.\n---\nreview\n")

    with pytest.raises(ValueError, match="immediate child"):
        inspect_skill_bundle(tmp_path / "bundle")
