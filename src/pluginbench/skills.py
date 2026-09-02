from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillBundle:
    source_path: Path
    skill_roots: tuple[Path, ...]
    skill_names: tuple[str, ...]
    digest: str


def _bundle_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*names, *filenames]:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise ValueError(f"skill bundle contains unsupported symlink: {candidate}")
        files.extend(directory_path / name for name in filenames)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _digest_bundle(root: Path, files: list[Path], directories: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in directories:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(b"directory")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(b"file")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _skill_name(root: Path) -> str:
    path = root / "SKILL.md"
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"skill lacks YAML frontmatter: {path}")
    parts = text.split("---\n", maxsplit=2)
    if len(parts) != 3:
        raise ValueError(f"skill has invalid YAML frontmatter: {path}")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError(f"skill frontmatter must be a mapping: {path}")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"skill frontmatter requires name: {path}")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"skill frontmatter requires description: {path}")
    return name


def inspect_skill_bundle(path: Path) -> SkillBundle:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"skill path is not a directory: {root}")

    files = _bundle_files(root)
    if (root / "SKILL.md").is_file():
        skill_roots = [root]
    else:
        child_directories = sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ),
            key=lambda child: child.name,
        )
        invalid = [child.name for child in child_directories if not (child / "SKILL.md").is_file()]
        if invalid:
            raise ValueError(
                "skill bundle has immediate child directories without SKILL.md: "
                + ", ".join(invalid)
            )
        skill_roots = child_directories
    if not skill_roots:
        raise ValueError(f"skill bundle contains no SKILL.md: {root}")

    names = [_skill_name(skill) for skill in skill_roots]
    if len(set(names)) != len(names):
        raise ValueError("skill bundle contains duplicate skill names")
    injected_files = [
        file for file in files if any(file.is_relative_to(skill) for skill in skill_roots)
    ]
    injected_directories = sorted(
        (
            path
            for skill in skill_roots
            for path in skill.rglob("*")
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )

    return SkillBundle(
        source_path=root,
        skill_roots=tuple(skill_roots),
        skill_names=tuple(names),
        digest=_digest_bundle(root, injected_files, [*skill_roots, *injected_directories]),
    )


def stage_skill_bundle(bundle: SkillBundle, workspace: Path) -> None:
    destination = workspace / ".agents" / "skills"
    destination.mkdir(parents=True, exist_ok=True)
    for skill_root in bundle.skill_roots:
        shutil.copytree(skill_root, destination / skill_root.name)
