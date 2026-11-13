from __future__ import annotations

import json
import os
import shutil
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
    before = treatment_inventory(resolved, "codex")
    frozen = freeze_treatment(resolved, tmp_path / "frozen", "codex")
    assert frozen.skills[0] != skill
    assert (frozen.skills[0] / "scripts/check.sh").stat().st_mode & 0o777 == 0o755
    assert (frozen.skills[0] / "references/context.md").read_text() == (
        "The actual referenced context."
    )
    assert (frozen.plugins[0] / "assets/example.bin").read_bytes() == b"\x00\xff\x01"
    assert not (frozen.plugins[0] / ".git").exists()
    assert (frozen.plugins[0] / ".gitignore").exists()
    assert treatment_inventory(frozen, "codex") == before
    script.chmod(0o644)
    assert treatment_inventory(resolved, "codex") != before


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
    info = treatment_inventory(resolved, "codex")["plugins"][0]
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
    assert treatment_inventory(resolved, "codex")["plugins"][0]["hooks"] == [hooks]
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
    assert treatment_inventory(resolved, "codex")["plugins"][0]["hooks"] == [hooks]


def _claude_plugin(root: Path, **fields: object) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "repairs", **fields}))
    _skill(root / "skills/repair")
    return root


def _claude(treatment: Treatment, base: Path) -> Treatment:
    return resolve_treatment(treatment, base, "claude")


def test_codex_only_plugin_is_refused_for_claude(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin")
    with pytest.raises(ValueError, match="plugin JSON"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_superpowers_shaped_claude_plugin_admits_and_freezes(tmp_path: Path) -> None:
    plugin = _claude_plugin(
        tmp_path / "plugin", version="7.0.2", description="Skills.",
        author={"name": "Author"}, homepage="https://example.com",
        repository="https://example.com/repo", license="MIT", keywords=["skills"],
    )
    (plugin / "hooks").mkdir()
    hooks = {"description": "Session start.", "hooks": {"SessionStart": [{
        "matcher": "startup|resume", "hooks": [{
            "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-start",
            "shell": "bash", "powershell": "hooks/session-start.ps1", "async": False,
        }],
    }]}}
    (plugin / "hooks/hooks.json").write_text(json.dumps(hooks))
    (plugin / "bin").mkdir()
    (plugin / "bin/tool").write_text("#!/bin/sh\n")
    resolved = _claude(Treatment(plugins=[plugin]), tmp_path)
    info = treatment_inventory(resolved, "claude")["plugins"][0]
    assert info["manifest_path"] == ".claude-plugin/plugin.json"
    assert info["skills"] == [{"name": "repair", "path": "skills/repair"}]
    assert info["hooks"] == [hooks]
    assert info["default_components"] == ["bin"]
    frozen = freeze_treatment(resolved, tmp_path / "frozen", "claude")
    assert treatment_inventory(frozen, "claude")["plugins"][0]["sha256"] == info["sha256"]


def test_workflow_shaped_dual_manifest_plugin_reads_the_manifest_of_its_harness(
    tmp_path: Path,
) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", skills="./skills", displayName="Repairs")
    (plugin / "skills/repair/SKILL.md").write_text(
        "---\nname: repair\ndescription: Repair it.\ndisable-model-invocation: true\n"
        "metadata:\n  short-description: Repair\n---\nBody.\n"
    )
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "repairs-codex"}))
    resolved = _claude(Treatment(plugins=[plugin]), tmp_path)
    claude = treatment_inventory(resolved, "claude")["plugins"][0]
    assert claude["manifest_path"] == ".claude-plugin/plugin.json"
    assert claude["skills"] == [{"name": "repair", "path": "skills/repair"}]
    codex = treatment_inventory(_resolve(Treatment(plugins=[plugin]), tmp_path), "codex")
    assert codex["plugins"][0]["manifest_path"] == ".codex-plugin/plugin.json"
    assert "default_components" not in codex["plugins"][0]


def test_claude_refuses_a_portable_manifest_it_would_not_read(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", agents=["./x/reviewer.md"])
    (plugin / "x").mkdir()
    (plugin / "x/reviewer.md").write_text("Review.")
    (plugin / "plugin.json").write_text(json.dumps({"name": "repairs"}))
    with pytest.raises(ValueError, match="portable plugin.json"):
        _claude(Treatment(plugins=[plugin]), tmp_path)
    (plugin / ".claude-plugin/plugin.json").unlink()
    with pytest.raises(ValueError, match="portable plugin.json"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_claude_refuses_hook_file_modules(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {}, "modules": ["./mod.ts"]}))
    with pytest.raises(ValueError, match="hook-file keys"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


# CLI 2.1.322 ran a SessionStart hook for each refused matcher below: it reads a matcher
# outside names, `|` and `*` as a regular expression.
@pytest.mark.parametrize(
    ("matcher", "admitted"),
    [
        ("", True), ("*", True), ("startup", True), ("startup|clear|compact", True),
        ("Bash", True), ("start.*", False), ("^startup$", False), (".*", False),
        ("(startup)", False), (" startup", False), ("star*", False), ("Edit|Write*", False),
    ],
)
def test_claude_admits_only_hook_matchers_it_can_check_without_a_regex(
    tmp_path: Path, matcher: str, admitted: bool
) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": matcher, "hooks": [{"type": "command", "command": "true"}]},
    ]}}))
    if admitted:
        _claude(Treatment(plugins=[plugin]), tmp_path)
    else:
        with pytest.raises(ValueError, match="Claude hook matcher"):
            _claude(Treatment(plugins=[plugin]), tmp_path)


def test_claude_refuses_a_root_skill(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    shutil.rmtree(plugin / "skills")
    (plugin / "SKILL.md").write_text("---\nname: repairs\ndescription: R.\nmodel: haiku\n---\n")
    with pytest.raises(ValueError, match="default components"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_claude_explicit_default_hook_file_is_checked_once(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", hooks="./hooks/hooks.json")
    (plugin / "hooks").mkdir()
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}
    (plugin / "hooks/hooks.json").write_text(json.dumps(hooks))
    resolved = _claude(Treatment(plugins=[plugin]), tmp_path)
    assert treatment_inventory(resolved, "claude")["plugins"][0]["hooks"] == [hooks]


@pytest.mark.parametrize("path", [
    "commands/review.md", "workflows/ship.md", "output-styles/terse.md",
    "monitors/monitors.json", ".lsp.json", "settings.json", ".mcp.json",
])
def test_claude_default_components_are_refused(tmp_path: Path, path: str) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / path).parent.mkdir(exist_ok=True)
    # Flat .mcp.json servers have no mcpServers key; presence alone must refuse.
    (plugin / path).write_text(json.dumps({"server": {"command": "serve"}}))
    with pytest.raises(ValueError, match="default components"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


AGENT = "---\nname: reviewer\ndescription: Reviews the change.\n---\nReview it.\n"


def test_claude_plugin_agents_admit_and_join_only_the_claude_inventory(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / "agents").mkdir()
    (plugin / "agents/review.md").write_text(AGENT)
    claude = treatment_inventory(_claude(Treatment(plugins=[plugin]), tmp_path), "claude")
    # The frontmatter name, not the file name, is the agent's name.
    assert claude["plugins"][0]["agents"] == [{"name": "reviewer", "path": "agents/review.md"}]
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "repairs"}))
    codex = treatment_inventory(_resolve(Treatment(plugins=[plugin]), tmp_path), "codex")
    assert "agents" not in codex["plugins"][0]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"agents/review.md": AGENT.replace("---\nR", "model: sonnet\n---\nR")},
         r"Claude agent frontmatter \['model'\]"),
        ({"agents/review.md": AGENT.replace("---\nR", "tools: Read\n---\nR")},
         r"Claude agent frontmatter \['tools'\]"),
        ({"agents/nested/review.md": AGENT}, "only .md files directly in it"),
        ({"agents/review.md": AGENT, "agents/notes.txt": "x"}, "only .md files directly in it"),
        ({"agents/review.md": AGENT, "agents/again.md": AGENT}, "duplicate Claude plugin agent"),
        ({"agents/review.md": AGENT.replace("Reviews the change.", "''")},
         "agent requires a description"),
        ({"agents/review.md": "Review it.\n"}, "agent requires YAML frontmatter"),
        ({"agents/.keep": ""}, "only .md files directly in it"),
    ],
)
def test_claude_plugin_agents_outside_the_admitted_shape_are_refused(
    tmp_path: Path, files: dict[str, str], message: str
) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    for name, text in files.items():
        (plugin / name).parent.mkdir(parents=True, exist_ok=True)
        (plugin / name).write_text(text)
    with pytest.raises(ValueError, match=message):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_an_empty_claude_plugin_agents_directory_is_refused(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / "agents").mkdir()
    with pytest.raises(ValueError, match="agents/ must be a directory of agent files"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_a_claude_manifest_agents_field_stays_refused(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", agents="./agents/")
    (plugin / "agents").mkdir()
    (plugin / "agents/review.md").write_text(AGENT)
    with pytest.raises(ValueError, match="unsupported declared plugin component agents"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


@pytest.mark.parametrize("field", ["userConfig", "lspServers", "outputStyles", "monitors"])
def test_claude_only_manifest_fields_are_refused(tmp_path: Path, field: str) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", **{field: {"key": {}}})
    with pytest.raises(ValueError, match="manifest fields"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


@pytest.mark.parametrize("line", ["hooks: {}", "model: haiku", "context: fork", "effort: low"])
def test_claude_skill_frontmatter_outside_the_allowlist_is_refused(
    tmp_path: Path, line: str,
) -> None:
    skill = _skill(tmp_path / "skill")
    (skill / "SKILL.md").write_text(f"---\nname: repair\ndescription: Repair.\n{line}\n---\n")
    _resolve(Treatment(skills=[skill]), tmp_path)
    with pytest.raises(ValueError, match="Claude skill frontmatter"):
        _claude(Treatment(skills=[skill]), tmp_path)
    plugin = _claude_plugin(tmp_path / "plugin")
    (plugin / "skills/repair/SKILL.md").write_text((skill / "SKILL.md").read_text())
    with pytest.raises(ValueError, match="Claude skill frontmatter"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_claude_checks_default_skills_and_hooks_beside_declared_ones(tmp_path: Path) -> None:
    plugin = _claude_plugin(tmp_path / "plugin", skills="./extra", hooks={})
    _skill(plugin / "extra/other", "other")
    resolved = _claude(Treatment(plugins=[plugin]), tmp_path)
    assert [skill["name"] for skill in treatment_inventory(resolved, "claude")["plugins"][0][
        "skills"
    ]] == ["repair", "other"]
    (plugin / "skills/repair/SKILL.md").write_text(
        "---\nname: repair\ndescription: Repair.\nmodel: haiku\n---\n"
    )
    with pytest.raises(ValueError, match="Claude skill frontmatter"):
        _claude(Treatment(plugins=[plugin]), tmp_path)
    _skill(tmp_path / "clean/repair")
    (plugin / "skills/repair/SKILL.md").write_text(
        (tmp_path / "clean/repair/SKILL.md").read_text()
    )
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "prompt", "prompt": "Check the work.",
    }]}]}}))
    with pytest.raises(ValueError, match="only command"):
        _claude(Treatment(plugins=[plugin]), tmp_path)


def test_codex_admission_ignores_claude_default_components(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin", displayName="Repairs")
    with pytest.raises(ValueError, match="manifest fields"):
        _resolve(Treatment(plugins=[plugin]), tmp_path)
    plugin = _plugin(tmp_path / "other")
    for path in [
        "agents/reviewer.md", "commands/review.md", "workflows/ship.md", "bin/tool",
        "themes/dark.json",
    ]:
        (plugin / path).parent.mkdir(exist_ok=True)
        (plugin / path).write_text("x")
    (plugin / "settings.json").write_text("{}")
    (plugin / "plugin.json").write_text(json.dumps({"name": "repairs"}))
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {}, "modules": ["./m.ts"]}))
    resolved = _resolve(Treatment(plugins=[plugin]), tmp_path)
    info = treatment_inventory(resolved, "codex")["plugins"][0]
    assert {key: info[key] for key in info if key not in {"directory", "sha256", "entries"}} == {
        "name": "repairs", "manifest_path": "plugin.json", "manifest": {"name": "repairs"},
        "settings": {"name": "repairs"}, "skills": [], "hooks": [],
    }


def test_freeze_rechecks_inputs_and_destination_links(tmp_path: Path) -> None:
    path = tmp_path / "guidance.md"
    path.write_text("Instructions.")
    resolved = _resolve(Treatment(guidance=[path]), tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "other.md")
    with pytest.raises(ValueError, match="symlink"):
        freeze_treatment(resolved, tmp_path / "frozen", "codex")
    assert not (tmp_path / "frozen").exists()
    path.unlink()
    path.write_text("Instructions.")
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        freeze_treatment(resolved, tmp_path / "alias/snapshot", "codex")
    assert not (tmp_path / "real/snapshot").exists()
    skill = _skill(tmp_path / "skill")
    with pytest.raises(ValueError, match="outside its input"):
        freeze_treatment(
            _resolve(Treatment(skills=[skill]), tmp_path), skill / "frozen", "codex"
        )


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
