"""Actual OS filesystem boundaries, without invoking a model."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

import pytest

from pluginbench import isolation


def test_owned_job_cannot_read_shared_files_or_follow_an_escape(tmp_path: Path) -> None:
    own, sibling = tmp_path / "own", tmp_path / "sibling"
    own.mkdir()
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("secret")
    (own / "escape").symlink_to(sibling, target_is_directory=True)
    script = own / "check.py"
    script.write_text("""import json, os
from pathlib import Path
result = {}
for path in (Path("../sibling/secret.txt"), Path("escape/secret.txt"), Path(os.environ["PERSONAL_GUIDANCE"])):
    for operation in ("read", "write"):
        try:
            path.read_text() if operation == "read" else path.write_text("changed")
            result[str(path) + operation] = True
        except OSError:
            result[str(path) + operation] = False
Path("owned.txt").write_text("allowed")
Path(os.environ["TMPDIR"], "scratch.txt").write_text("allowed")
print(json.dumps(result))
""")
    guidance = tmp_path / "AGENTS.md"
    guidance.write_text("private guidance")
    result = isolation.run(
        [sys.executable, str(script)],
        cwd=own,
        env={"PATH": os.environ["PATH"], "PERSONAL_GUIDANCE": str(guidance)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not any(json.loads(result.stdout).values())
    assert secret.read_text() == "secret"
    assert guidance.read_text() == "private guidance"
    assert (own / "owned.txt").read_text() == "allowed"


def test_trusted_asset_is_read_only_and_not_a_sibling_permission(tmp_path: Path) -> None:
    own, trusted = tmp_path / "own", tmp_path / "trusted"
    own.mkdir()
    trusted.mkdir()
    allowed = trusted / "selected.json"
    allowed.write_text("{}")
    (trusted / "other.json").write_text("secret")
    script = own / "check.py"
    script.write_text("""from pathlib import Path
import sys
p=Path(sys.argv[1]); assert p.read_text() == "{}"
for target in (p, p.parent/"other.json"):
    try:
        target.write_text("wrong") if target == p else target.read_text()
    except OSError:
        continue
    raise AssertionError("access escaped selected asset")
""")
    result = isolation.run(
        [sys.executable, str(script), str(allowed)],
        cwd=own,
        env={"PATH": os.environ["PATH"]},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert allowed.read_text() == "{}"


def test_missing_backend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="requires"):
        isolation.runtime_identity()


def test_runtime_identity_tracks_replacement_of_selected_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("first binary")
    monkeypatch.setattr(shutil, "which", lambda name: str(executable) if name == "codex" else None)
    before = isolation.runtime_identity()
    replacement = tmp_path / "replacement"
    replacement.write_text("other binary")
    replacement.replace(executable)
    assert isolation.runtime_identity() != before


@pytest.mark.parametrize("tool_directory", ["bin", "."])
def test_standalone_tool_does_not_grant_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_directory: str
) -> None:
    private = tmp_path / "private-user"
    directory = private / tool_directory
    directory.mkdir(parents=True)
    tool = directory / "node"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    secret = private / "AGENTS.md"
    secret.write_text("private guidance")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    script = f"""from pathlib import Path
try:
    Path({str(secret)!r}).read_text()
except OSError:
    pass
else:
    raise AssertionError("standalone tool granted private files")
"""
    result = isolation.run(
        [sys.executable, "-c", script], cwd=job, env={"PATH": os.environ["PATH"]}, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert tool in isolation.tool_roots()
