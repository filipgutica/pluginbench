"""Git diff metrics and source/test file classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cjs",
    ".cpp",
    ".cs",
    ".cts",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".mjs",
    ".mts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}


_DEPENDENCY_FILES = {
    "cargo.lock",
    "cargo.toml",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "gradle.lockfile",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pom.xml",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "yarn.lock",
}


@dataclass(frozen=True)
class FileChange:
    old_path: str | None
    new_path: str | None
    added: int
    removed: int
    binary: bool

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        if path is None:
            raise ValueError("diff file has no usable path")
        return path


def _marker_path(value: str) -> str | None:
    path = value.split("\t", 1)[0]
    if path.startswith('"'):
        if not path.endswith('"'):
            raise ValueError(f"cannot parse diff path: {value}")
        # Git's C/octal escapes represent path bytes, not Unicode code points.
        decoded = path[1:-1].encode().decode("unicode_escape")
        path = decoded.encode("latin-1").decode("utf-8", errors="surrogateescape")
    if not path or path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def _file_changes(patch: str) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    old_path: str | None = None
    new_path: str | None = None
    added = 0
    removed = 0
    binary = False
    in_hunk = False
    active = False

    def flush() -> None:
        nonlocal active, old_path, new_path, added, removed, binary, in_hunk
        if active:
            changes.append(FileChange(old_path, new_path, added, removed, binary))
        active = False
        old_path = None
        new_path = None
        added = 0
        removed = 0
        binary = False
        in_hunk = False

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            header = re.fullmatch(
                r'diff --git ("(?:\\.|[^"\\])*"|[ab]/.*?) ("(?:\\.|[^"\\])*"|[ab]/.*)', line
            )
            if header is None:
                raise ValueError(f"invalid diff header: {line}")
            old_path = _marker_path(header[1])
            new_path = _marker_path(header[2])
            active = True
        elif active and not in_hunk and line.startswith("--- "):
            old_path = _marker_path(line[4:])
        elif active and not in_hunk and line.startswith("+++ "):
            new_path = _marker_path(line[4:])
        elif active and line.startswith("@@"):
            in_hunk = True
        elif active and (line == "GIT binary patch" or line.startswith("Binary files ")):
            binary = True
            in_hunk = False
        elif active and in_hunk and line.startswith("+"):
            added += 1
        elif active and in_hunk and line.startswith("-"):
            removed += 1
    flush()
    if patch.strip() and not changes:
        raise ValueError("expected a Git diff with 'diff --git' headers; unsupported patch format")
    return tuple(changes)


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        any(part.lower() in {"test", "tests", "testing", "spec", "specs"} for part in parts)
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _is_source_path(path: str) -> bool:
    return not _is_test_path(path) and Path(path).suffix.lower() in _SOURCE_SUFFIXES


def _is_dependency_manifest(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        name in _DEPENDENCY_FILES
        or (name.startswith("requirements") and name.endswith(".txt"))
        or name.startswith("build.gradle")
    )


def analyze_patch(patch: str) -> dict[str, Any]:
    """Return transparent line and path metrics for one saved Git patch."""
    changes = _file_changes(patch)
    source = [change for change in changes if _is_source_path(change.path)]
    tests = [change for change in changes if _is_test_path(change.path)]
    manifests = sorted(change.path for change in changes if _is_dependency_manifest(change.path))
    lines_added = sum(change.added for change in changes)
    lines_removed = sum(change.removed for change in changes)
    source_added = sum(change.added for change in source)
    source_removed = sum(change.removed for change in source)
    test_added = sum(change.added for change in tests)
    test_removed = sum(change.removed for change in tests)
    return {
        "changed_paths": sorted(change.path for change in changes),
        "files_changed": len(changes),
        "source_files_changed": len(source),
        "test_files_changed": len(tests),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "churn": lines_added + lines_removed,
        "source_lines_added": source_added,
        "source_lines_removed": source_removed,
        "source_churn": source_added + source_removed,
        "test_lines_added": test_added,
        "test_lines_removed": test_removed,
        "test_churn": test_added + test_removed,
        "binary_patch": any(change.binary for change in changes),
        "empty_patch": not patch.strip(),
        "dependency_manifest_changes": manifests,
    }
