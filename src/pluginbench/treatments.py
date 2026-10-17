"""Resolve and freeze native instruction files, skill trees, and plugin packages.

This validates package contents, not whether a particular runtime can load them.
Git internals are excluded from snapshots. Ambient dotenv files are rejected;
only explicitly named example/sample/template dotenv files are allowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Treatment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guidance: list[Path] = Field(default_factory=list)
    skills: list[Path] = Field(default_factory=list)
    plugins: list[Path] = Field(default_factory=list)


def _checked_path(path: Path, base: Path) -> Path:
    """Check components before normalization, so symlink/../ cannot hide a link."""
    value = path.expanduser()
    if not value.is_absolute():
        value = base.absolute() / value
    if ".git" in value.parts:
        raise ValueError(f"treatment must not select Git internals: {value}")
    current = Path(value.anchor)
    for part in value.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ValueError(f"treatment path is missing or unreadable: {current}") from error
        if stat.S_ISLNK(mode):
            raise ValueError(f"treatment path must not contain a symlink: {current}")
    return current


def _tree(root: Path) -> list[Path]:
    _checked_path(root, Path.cwd())
    paths = [root]
    if not root.is_dir():
        raise ValueError(f"treatment package must be a directory: {root}")
    def unreadable(error: OSError) -> None:
        raise ValueError(f"treatment contains an unreadable directory: {error.filename}") from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        parent = Path(directory)
        for name in sorted([*dirs, *files]):
            path = parent / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"treatment contains a symlink: {path}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"treatment contains a nonregular asset: {path}")
            if name == ".git":
                continue
            if (name == ".env" or name.startswith(".env.")) and name not in {
                ".env.example", ".env.sample", ".env.template"
            }:
                raise ValueError(f"treatment contains an ambient dotenv file: {path}")
            paths.append(path)
        dirs[:] = sorted(name for name in dirs if name != ".git")
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _guidance(path: Path, base: Path) -> Path:
    path = _checked_path(path, base)
    if not stat.S_ISREG(path.stat().st_mode) or path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError(f"guidance must select regular .md/.txt files: {path}")
    try:
        path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise ValueError(f"guidance must be UTF-8: {path}") from error
    return path


def _skill_name(root: Path) -> str:
    skill = root / "SKILL.md"
    try:
        text = skill.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"skill requires a UTF-8 SKILL.md: {root}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"skill requires YAML frontmatter: {skill}")
    try:
        metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid skill frontmatter: {skill}") from error
    if not isinstance(metadata, dict):
        raise ValueError(f"skill frontmatter must be an object: {skill}")
    name = metadata.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(f"skill requires a kebab-case name: {skill}")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill requires a description: {skill}")
    _skill_dependencies(metadata, skill)
    agent_file = root / "agents/openai.yaml"
    if agent_file.exists():
        try:
            agent = yaml.safe_load(agent_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise ValueError(f"invalid skill agent metadata: {agent_file}") from error
        if not isinstance(agent, dict):
            raise ValueError(f"skill agent metadata must be an object: {agent_file}")
        _skill_dependencies(agent, agent_file)
    if (root / "SKILL.json").exists():
        _skill_dependencies(_json_object(root / "SKILL.json"), root / "SKILL.json")
    return name


def _skill_dependencies(metadata: dict[str, Any], source: Path) -> None:
    dependencies = metadata.get("dependencies", {})
    if (not isinstance(dependencies, dict)
            or any(dependencies.values())):
        raise ValueError(f"unsupported declared skill dependencies: {source}")


def _skill_roots(path: Path) -> list[Path]:
    _tree(path)
    roots = [path] if (path / "SKILL.md").exists() else [
        child for child in sorted(path.iterdir())
        if child.is_dir() and child.name != ".git" and (child / "SKILL.md").exists()
    ]
    if not roots:
        raise ValueError(f"skill directory contains no direct SKILL.md roots: {path}")
    names = [_skill_name(root) for root in roots]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate skill names in collection: {path}")
    return roots


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid or missing plugin JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"plugin JSON must be an object: {path}")
    return value


def _component(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.startswith("./"):
        raise ValueError(f"plugin component path must start with ./ inside {root}: {value}")
    relative = Path(value)
    if ".." in relative.parts:
        raise ValueError(f"plugin component path escapes its package: {value}")
    path = _checked_path(relative, root)
    if ".git" in path.relative_to(root).parts:
        raise ValueError(f"plugin component must not refer to excluded Git metadata: {value}")
    return path


def _hooks(root: Path, value: object) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    configs = []
    for entry in values:
        config = _json_object(_component(root, entry)) if isinstance(entry, str) else entry
        if not isinstance(config, dict):
            raise ValueError(f"plugin hooks must be JSON paths or objects: {root}")
        if config and "hooks" not in config:
            raise ValueError(f"plugin hooks require the full hook-file object with hooks: {root}")
        events = config.get("hooks", {})
        if not isinstance(events, dict):
            raise ValueError(f"plugin hook events must be an object: {root}")
        for groups in events.values():
            if not isinstance(groups, list):
                raise ValueError(f"plugin hook event must contain a list: {root}")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError(f"invalid plugin hook group: {root}")
                for hook in group["hooks"]:
                    if (not isinstance(hook, dict) or hook.get("type") != "command"
                            or not isinstance(hook.get("command"), str)
                            or not hook["command"].strip()):
                        raise ValueError(f"only command plugin hooks are supported: {root}")
                    if hook.get("async") or hook.get("asyncRewake"):
                        raise ValueError(f"asynchronous plugin hooks are unsupported: {root}")
        if any(events.values()):
            configs.append(config)
    return configs


_METADATA = {
    "$schema", "name", "version", "description", "author", "homepage", "repository",
    "license", "keywords", "interface", "extensions", "onboardingSkill",
}
_COMPONENTS = {"skills", "hooks", "mcpServers", "apps", "agents", "commands", "dependencies"}


def _plugin(root: Path, harness: str | None = None) -> dict[str, Any]:
    _tree(root)
    portable = root / "plugin.json"
    codex = root / ".codex-plugin/plugin.json"
    claude = root / ".claude-plugin/plugin.json"
    manifest_path = portable if portable.exists() else (
        claude if harness == "claude" else codex
    )
    if harness is None and not manifest_path.exists() and claude.exists():
        manifest_path = claude
    manifest = _json_object(manifest_path)
    name = manifest.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(f"plugin requires a kebab-case name: {manifest_path}")
    unknown = set(manifest) - _METADATA - _COMPONENTS
    if unknown:
        raise ValueError(f"unsupported plugin manifest fields: {sorted(unknown)}")
    settings = manifest
    if manifest_path == portable:
        extensions = manifest.get("extensions", {})
        if not isinstance(extensions, dict):
            raise ValueError(f"plugin extensions must be an object: {manifest_path}")
        unknown_extensions = set(extensions) - {"com.openai"}
        if unknown_extensions:
            raise ValueError(f"unsupported plugin extensions: {sorted(unknown_extensions)}")
        if "com.openai" in extensions:
            settings = extensions["com.openai"]
            if not isinstance(settings, dict):
                raise ValueError(f"plugin com.openai extension must be an object: {manifest_path}")
        elif codex.exists() and harness != "claude":
            settings = _json_object(codex)
    unknown = set(settings) - _METADATA - _COMPONENTS
    if unknown:
        raise ValueError(f"unsupported plugin settings: {sorted(unknown)}")
    onboarding = [settings["onboardingSkill"]] if "onboardingSkill" in settings else []
    if manifest_path != portable and "extensions" in settings:
        extensions = settings["extensions"]
        if not isinstance(extensions, dict) or set(extensions) - {"com.openai"}:
            raise ValueError(f"unsupported plugin compatibility extensions: {manifest_path}")
        extension = extensions.get("com.openai", {})
        if not isinstance(extension, dict) or set(extension) - {"onboardingSkill"}:
            raise ValueError(f"unsupported plugin compatibility extension fields: {manifest_path}")
        if "onboardingSkill" in extension:
            onboarding.append(extension["onboardingSkill"])
    for document in (manifest, settings):
        for field in ("mcpServers", "apps", "agents", "commands", "dependencies"):
            if document.get(field):
                raise ValueError(f"unsupported declared plugin component {field}: {root}")
    # Default MCP files are active components even without a manifest declaration.
    for filename in ("mcp.json", ".mcp.json", ".app.json"):
        if (root / filename).exists():
            config = _json_object(root / filename)
            if config.get("mcpServers") or config.get("apps"):
                raise ValueError(f"unsupported plugin MCP/app configuration: {root / filename}")
    skill_paths = settings.get("skills")
    if skill_paths is None:
        roots = _skill_roots(root / "skills") if (root / "skills").exists() else []
    else:
        paths = skill_paths if isinstance(skill_paths, list) else [skill_paths]
        roots = [skill for value in paths for skill in _skill_roots(_component(root, value))]
    names = [_skill_name(skill) for skill in roots]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate plugin skill names: {root}")
    if "hooks" in settings:
        hooks = _hooks(root, settings["hooks"])
    elif (root / "hooks/hooks.json").exists():
        hooks = _hooks(root, "./hooks/hooks.json")
    else:
        hooks = []
    for value in onboarding:
        path = _component(root, value)
        if path.name != "SKILL.md" or path.parent not in roots:
            raise ValueError(f"plugin onboardingSkill must reference a packaged skill: {path}")
    interface = settings.get("interface", {})
    if not isinstance(interface, dict):
        raise ValueError(f"plugin interface must be an object: {root}")
    for field in ("composerIcon", "logo", "screenshots"):
        if field in interface:
            values = interface[field] if isinstance(interface[field], list) else [interface[field]]
            for value in values:
                if not _component(root, value).is_file():
                    raise ValueError(f"plugin interface asset must be a file: {value}")
    return {
        "name": name,
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest": manifest,
        "settings": settings,
        "skills": [{"name": _skill_name(skill), "path": skill.relative_to(root).as_posix()}
                   for skill in roots],
        "hooks": hooks,
    }


def resolve_treatment(treatment: Treatment, base: Path, harness: str) -> Treatment:
    """Validate selected inputs and return absolute files and actual skill roots."""
    if harness not in {"codex", "claude"}:
        raise ValueError(f"unsupported treatment harness: {harness}")
    if harness == "claude" and treatment.plugins:
        raise ValueError("native plugin qualification currently supports only Codex")
    guidance = [_guidance(path, base) for path in treatment.guidance]
    if len(set(guidance)) != len(guidance):
        raise ValueError("duplicate guidance files in treatment")
    skills = [root for path in treatment.skills
              for root in _skill_roots(_checked_path(path, base))]
    names = [_skill_name(root) for root in skills]
    if len(set(names)) != len(names):
        raise ValueError("duplicate skill names in treatment")
    if len({root.name for root in skills}) != len(skills):
        raise ValueError("duplicate skill directory basenames in treatment")
    plugins = [_checked_path(path, base) for path in treatment.plugins]
    names = [_plugin(root, harness)["name"] for root in plugins]
    if len(set(names)) != len(names):
        raise ValueError("duplicate plugin identity in treatment")
    return Treatment(guidance=guidance, skills=skills, plugins=plugins)


def _entries(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in _tree(root):
        mode = stat.S_IMODE(path.stat().st_mode)
        entry: dict[str, Any] = {
            "path": path.relative_to(root).as_posix(),
            "kind": "directory" if path.is_dir() else "file", "mode": mode,
        }
        if path.is_file():
            content = path.read_bytes()
            entry.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
        entries.append(entry)
    return entries


def _package_inventory(root: Path) -> dict[str, Any]:
    entries = _entries(root)
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return {"directory": root.name, "sha256": digest, "entries": entries}


def treatment_inventory(treatment: Treatment) -> dict[str, Any]:
    """Portable content/mode identities, independent of installation paths."""
    guidance = [_guidance(path, Path.cwd()) for path in treatment.guidance]
    return {
        "guidance": [{"name": path.name, "mode": stat.S_IMODE(path.stat().st_mode),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "size": path.stat().st_size} for path in guidance],
        "skills": [{**_package_inventory(root), "name": _skill_name(root)}
                   for root in treatment.skills],
        "plugins": [{**_package_inventory(root), **_plugin(root)} for root in treatment.plugins],
    }


def freeze_treatment(treatment: Treatment, destination: Path) -> Treatment:
    """Copy complete packages into deterministic locations without Git internals."""
    # Inventory first: reject invalid packages before writing a partial snapshot.
    before = treatment_inventory(treatment)
    for path in [destination, *destination.parents]:
        if path.exists() or path.is_symlink():
            _checked_path(path, Path.cwd())
    normalized = Path(os.path.abspath(destination))
    if any(normalized.is_relative_to(root.absolute()) for root in [
        *treatment.skills, *treatment.plugins,
    ]):
        raise ValueError("treatment snapshot destination must be outside its input packages")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"treatment snapshot destination must be a directory: {destination}")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"treatment snapshot destination must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    frozen: dict[str, list[Path]] = {"guidance": [], "skills": [], "plugins": []}
    for kind in frozen:
        for index, source in enumerate(getattr(treatment, kind)):
            target = destination / kind / f"{index:03d}" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "guidance":
                shutil.copy2(source, target)
            else:
                shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
            frozen[kind].append(target.absolute())
    result = Treatment(**frozen)
    if treatment_inventory(result) != before:
        raise ValueError("treatment changed while its snapshot was being frozen")
    return result
