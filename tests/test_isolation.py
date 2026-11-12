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
    which = shutil.which
    # Only codex is replaced: on Linux the sandbox backend itself is found through `which`.
    monkeypatch.setattr(
        shutil, "which", lambda name: str(executable) if name == "codex" else which(name)
    )
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


def test_tool_roots_grant_the_codex_code_mode_host_beside_the_launched_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release" / "bin"
    release.mkdir(parents=True)
    for name in ("codex", "codex-code-mode-host"):
        (release / name).write_text("#!/bin/sh\n")
        (release / name).chmod(0o755)
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / "codex").symlink_to(release / "codex")
    (launcher / "codex-code-mode-host").symlink_to(release / "codex-code-mode-host")
    monkeypatch.setenv("PATH", str(launcher) + os.pathsep + os.environ["PATH"])
    roots = isolation.tool_roots()
    assert launcher / "codex-code-mode-host" in roots
    assert release / "codex-code-mode-host" in roots


def _codex_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, host: bool) -> Path:
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    names = ("codex", "codex-code-mode-host") if host else ("codex",)
    for name in names:
        (launcher / name).write_text("#!/bin/sh\n")
        (launcher / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(launcher) + os.pathsep + os.environ["PATH"])
    return launcher


def test_reviewer_probe_finds_the_code_mode_host_inside_the_sandbox_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _codex_launcher(tmp_path, monkeypatch, host=True)
    assert isolation.codex_code_mode_host() == launcher / "codex-code-mode-host"


def test_reviewer_probe_fails_when_codex_code_mode_host_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every pilot review failed: code mode could not start its host inside the sandbox.
    _codex_launcher(tmp_path, monkeypatch, host=False)
    with pytest.raises(ValueError, match="codex-code-mode-host"):
        isolation.codex_code_mode_host()


def test_reviewer_probe_fails_when_the_sandbox_would_not_grant_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex_launcher(tmp_path, monkeypatch, host=True)
    monkeypatch.setattr(isolation, "tool_roots", lambda: [])
    with pytest.raises(ValueError, match="sandbox"):
        isolation.codex_code_mode_host()


@pytest.mark.parametrize(
    "layout", ["standalone", "linuxbrew-cask", "linuxbrew-cellar", "arbitrary"]
)
@pytest.mark.parametrize("launch_by_name", [False, True])
def test_codex_symlink_launch_keeps_helpers_visible_without_granting_private_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, launch_by_name: bool
) -> None:
    home = tmp_path / "home"
    launcher = home / ".local/bin/codex"
    if layout == "standalone":
        package = home / ".codex/packages/standalone"
        release = package / "releases/0.167.1-x86_64-unknown-linux-musl"
        release.mkdir(parents=True)
        (package / "current").symlink_to(release.relative_to(package), target_is_directory=True)
        target = package / "current/bin/codex"
    elif layout.startswith("linuxbrew"):
        kind = "Caskroom" if layout == "linuxbrew-cask" else "Cellar"
        prefix = home / f"linuxbrew/.linuxbrew/{kind}"
        launcher = prefix.parent / "bin/codex"
        release = prefix / "codex/0.167.0"
        target = release / "bin/codex"
        monkeypatch.setattr(
            isolation,
            "PACKAGE_ROOTS",
            tuple(
                tmp_path / root.relative_to("/") for root in getattr(isolation, "PACKAGE_ROOTS", ())
            ),
            raising=False,
        )
    else:
        release = home / "private"
        target = release / "bin/codex"
    binary = release / "bin/codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    for name in ("codex", "codex-code-mode-host", "codex-exec-server"):
        path = binary.with_name(name)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(target)
    monkeypatch.setattr(Path, "home", lambda: home)
    which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: str(launcher) if name == "codex" else which(name)
    )
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    wrapped, _ = isolation.command(
        ["codex" if launch_by_name else str(launcher), "--version"], writable=[]
    )
    launched = Path(wrapped[wrapped.index("--") + 1])
    binds = [
        (Path(wrapped[index + 1]), Path(wrapped[index + 2]))
        for index, value in enumerate(wrapped)
        if value == "--ro-bind"
    ]

    def visible(path: Path) -> bool:
        return any(
            path == destination or (source.is_dir() and path.is_relative_to(destination))
            for source, destination in binds
        )

    assert launched.is_absolute()
    for name in ("codex-code-mode-host", "codex-exec-server"):
        assert visible(launched.with_name(name)), (launched, binds)
    assert isolation.codex_code_mode_host() == binary.with_name("codex-code-mode-host")
    assert not visible(home / "AGENTS.md")
    assert not visible(launcher.parent / "private-file")
    if layout != "arbitrary":
        assert (release, release) in binds
        assert not visible(release.parent / "other-version/private-file")
    else:
        assert not visible(release / "private-file")
        assert not visible(binary.parent / "private-file")
