from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from pluginbench.treatments import (
    Treatment,
    freeze_treatment,
    resolve_treatment,
    treatment_inventory,
)


def _skill(root: Path, name: str = "repair") -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Repair the existing behavior.\n---\nRead references.\n"
    )
    return root


def _plugin(root: Path, **fields: object) -> Path:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "repairs", **fields}))
    return root


def _resolve(treatment: Treatment, base: Path) -> Treatment:
    return resolve_treatment(treatment, base, "codex")


def test_guidance_stays_ordered_files_and_does_not_flatten_skills(tmp_path: Path) -> None:
    (tmp_path / "core.md").write_text("Core instructions.")
    (tmp_path / "quality.txt").write_text("Quality instructions.")
    skill = _skill(tmp_path / "skills/fix")
    resolved = _resolve(Treatment(
        guidance=[Path("quality.txt"), Path("core.md")], skills=[Path("skills")]
    ), tmp_path)
    assert resolved.guidance == [tmp_path / "quality.txt", tmp_path / "core.md"]
    assert resolved.skills == [skill]
    assert resolved.plugins == []
    with pytest.raises(ValueError, match="regular .md/.txt"):
        _resolve(Treatment(guidance=[Path("skills")]), tmp_path)
    with pytest.raises(ValidationError, match="Extra inputs"):
        Treatment.model_validate({"prompt": "append these instructions"})


def test_freeze_retains_resources_modes_and_package_identity(tmp_path: Path) -> None:
    skill = _skill(tmp_path / "skills/repair")
    (skill / "references").mkdir()
    (skill / "references/context.md").write_text("The actual referenced context.")
    (skill / "scripts").mkdir()
    script = skill / "scripts/check.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    plugin = _plugin(tmp_path / "plugin", skills="./skills", hooks={})
    _skill(plugin / "skills/diagnose", "diagnose")
    (plugin / "assets").mkdir()
    (plugin / "assets/example.bin").write_bytes(b"\x00\xff\x01")
    (plugin / ".git").mkdir()
    (plugin / ".git/config").write_text("do not snapshot local git credentials")
    (plugin / ".gitignore").write_text("cache/\n")
    resolved = _resolve(Treatment(skills=[skill], plugins=[plugin]), tmp_path)
    before = treatment_inventory(resolved)
    frozen = freeze_treatment(resolved, tmp_path / "frozen")
    assert frozen.skills[0] != skill
    assert (frozen.skills[0] / "scripts/check.sh").stat().st_mode & 0o777 == 0o755
    assert (frozen.skills[0] / "references/context.md").read_text() == (
        "The actual referenced context."
    )
    assert (frozen.plugins[0] / "assets/example.bin").read_bytes() == b"\x00\xff\x01"
    assert not (frozen.plugins[0] / ".git").exists()
    assert (frozen.plugins[0] / ".gitignore").exists()
    assert treatment_inventory(frozen) == before
    script.chmod(0o644)
    assert treatment_inventory(resolved) != before


@pytest.mark.parametrize("selection", ["guidance", "skills", "plugins"])
def test_resolution_rejects_symlink_input_ancestors(tmp_path: Path, selection: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "core.md").write_text("Core instructions.")
    _skill(source / "skill")
    _plugin(source / "plugin")
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    selected = {"guidance": "core.md", "skills": "skill", "plugins": "plugin"}[selection]
    with pytest.raises(ValueError, match="symlink"):
        _resolve(Treatment.model_validate({selection: [alias / selected]}), tmp_path)


def test_resolution_rejects_nested_links_and_nonregular_assets(tmp_path: Path) -> None:
    skill = _skill(tmp_path / "skill")
    (skill / "outside.md").symlink_to(tmp_path / "missing.md")
    with pytest.raises(ValueError, match="symlink"):
        _resolve(Treatment(skills=[skill]), tmp_path)
    (skill / "outside.md").unlink()
    os.mkfifo(skill / "pipe")
    with pytest.raises(ValueError, match="nonregular"):
        _resolve(Treatment(skills=[skill]), tmp_path)


def test_duplicate_skill_frontmatter_and_plugin_names_fail_closed(tmp_path: Path) -> None:
    first = _skill(tmp_path / "first", "same")
    second = _skill(tmp_path / "second", "same")
    with pytest.raises(ValueError, match="duplicate skill names"):
        _resolve(Treatment(skills=[first, second]), tmp_path)
    first_plugin = _plugin(tmp_path / "first-plugin")
    second_plugin = _plugin(tmp_path / "second-plugin")
    with pytest.raises(ValueError, match="duplicate plugin identity"):
        _resolve(Treatment(plugins=[first_plugin, second_plugin]), tmp_path)


def test_duplicate_upload_basenames_are_rejected_even_when_skill_names_differ(
    tmp_path: Path,
) -> None:
    first = _skill(tmp_path / "first/skill", "first")
    second = _skill(tmp_path / "second/skill", "second")
    with pytest.raises(ValueError, match="directory basenames"):
        _resolve(Treatment(skills=[first, second]), tmp_path)


@pytest.mark.parametrize("filename", ["agents/openai.yaml", "SKILL.json"])
def test_skill_ui_metadata_is_allowed_but_external_dependencies_are_not(
    tmp_path: Path, filename: str,
) -> None:
    skill = _skill(tmp_path / "skill")
    metadata = skill / filename
    metadata.parent.mkdir(exist_ok=True)
    metadata.write_text(json.dumps({"interface": {"display_name": "Repair"}}))
    _resolve(Treatment(skills=[skill]), tmp_path)
    metadata.write_text(json.dumps({"dependencies": {
        "tools": [{"type": "mcp", "value": "external-service"}],
    }}))
    with pytest.raises(ValueError, match="skill dependencies"):
        _resolve(Treatment(skills=[skill]), tmp_path)


@pytest.mark.parametrize("field,value", [
    ("skills", "./missing"),
    ("skills", "./../outside"),
    ("hooks", "./missing.json"),
    ("apps", "./.app.json"),
    ("dependencies", ["unavailable-plugin"]),
])
def test_invalid_declared_components_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    plugin = _plugin(tmp_path / "plugin", **{field: value})
    with pytest.raises(ValueError):
        _resolve(Treatment(plugins=[plugin]), tmp_path)


def test_missing_manifest_is_not_treated_as_a_skill_collection(tmp_path: Path) -> None:
    plugin = _skill(tmp_path / "plugin/skills/repair")
    with pytest.raises(ValueError, match="plugin JSON"):
        _resolve(Treatment(plugins=[plugin.parent.parent]), tmp_path)


def test_empty_hooks_disable_fallback_and_portable_overlay_replaces_compatibility(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path / "plugin", hooks="./missing.json")
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text("invalid fallback which must not be parsed")
    (plugin / "plugin.json").write_text(json.dumps({
        "name": "portable", "version": "1.0.0",
        "extensions": {"com.openai": {"hooks": {}}},
    }))
    resolved = _resolve(Treatment(plugins=[plugin]), tmp_path)
    info = treatment_inventory(resolved)["plugins"][0]
    assert info["name"] == "portable"
    assert info["manifest_path"] == "plugin.json"
    assert info["hooks"] == []
    assert info["settings"] == {"hooks": {}}


def test_hook_configuration_is_preserved_and_model_hooks_are_ineligible(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin", hooks="./hooks/start.json")
    (plugin / "hooks").mkdir()
    command_hook: dict[str, str | bool] = {
        "type": "command", "command": "python3 ${PLUGIN_ROOT}/scripts/start.py",
    }
    hooks = {"hooks": {"SessionStart": [{"hooks": [command_hook]}]}}
    (plugin / "hooks/start.json").write_text(json.dumps(hooks))
    resolved = _resolve(Treatment(plugins=[plugin]), tmp_path)
    assert treatment_inventory(resolved)["plugins"][0]["hooks"] == [hooks]
    hooks["hooks"]["SessionStart"][0]["hooks"][0]["type"] = "prompt"
    (plugin / "hooks/start.json").write_text(json.dumps(hooks))
    with pytest.raises(ValueError, match="only command"):
        _resolve(Treatment(plugins=[plugin]), tmp_path)
    hooks["hooks"]["SessionStart"][0]["hooks"][0].update(type="command", **{"async": True})
    (plugin / "hooks/start.json").write_text(json.dumps(hooks))
    with pytest.raises(ValueError, match="asynchronous"):
        _resolve(Treatment(plugins=[plugin]), tmp_path)


@pytest.mark.parametrize("as_file", [False, True])
def test_unwrapped_hook_event_maps_are_rejected_instead_of_silently_ignored(
    tmp_path: Path, as_file: bool,
) -> None:
    events = {"SessionStart": [{"hooks": [{
        "type": "command", "command": "echo native-hook",
    }]}]}
    plugin = _plugin(tmp_path / "plugin", hooks="./hooks.json" if as_file else events)
    if as_file:
        (plugin / "hooks.json").write_text(json.dumps(events))
    with pytest.raises(ValueError, match="full hook-file object"):
        _resolve(Treatment(plugins=[plugin]), tmp_path)


def test_inline_hooks_use_the_native_hook_file_wrapper(tmp_path: Path) -> None:
    hooks = {"hooks": {"SessionStart": [{"hooks": [{
        "type": "command", "command": "echo native-hook",
    }]}]}}
    plugin = _plugin(tmp_path / "plugin", hooks=hooks)
    resolved = _resolve(Treatment(plugins=[plugin]), tmp_path)
    assert treatment_inventory(resolved)["plugins"][0]["hooks"] == [hooks]


def test_claude_plugin_cannot_be_admitted_without_native_qualification(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin")
    with pytest.raises(ValueError, match="only Codex"):
        resolve_treatment(Treatment(plugins=[plugin]), tmp_path, "claude")


def test_freeze_rechecks_inputs_and_destination_links(tmp_path: Path) -> None:
    path = tmp_path / "guidance.md"
    path.write_text("Instructions.")
    resolved = _resolve(Treatment(guidance=[path]), tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "other.md")
    with pytest.raises(ValueError, match="symlink"):
        freeze_treatment(resolved, tmp_path / "frozen")
    assert not (tmp_path / "frozen").exists()
    path.unlink()
    path.write_text("Instructions.")
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        freeze_treatment(resolved, tmp_path / "alias/snapshot")
    assert not (tmp_path / "real/snapshot").exists()
    skill = _skill(tmp_path / "skill")
    with pytest.raises(ValueError, match="outside its input"):
        freeze_treatment(_resolve(Treatment(skills=[skill]), tmp_path), skill / "frozen")


def test_dotenv_is_rejected_but_examples_are_preserved(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin")
    (plugin / ".env.example").write_text("EXAMPLE=\n")
    _resolve(Treatment(plugins=[plugin]), tmp_path)
    (plugin / ".env.local").write_text("SECRET=value\n")
    with pytest.raises(ValueError, match="dotenv"):
        _resolve(Treatment(plugins=[plugin]), tmp_path)


def test_guidance_encoding_and_missing_skills_fail_before_freezing(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        _resolve(Treatment(guidance=[invalid]), tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no direct SKILL.md"):
        _resolve(Treatment(skills=[empty]), tmp_path)
