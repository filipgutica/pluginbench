"""Provisioning fails closed and makes both vendored tools usable offline."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pluginbench import collectors, components, harbor_backend as harbor, measurement_profiles
from pluginbench import measurement_tools as tools, mutation
from pluginbench.assessment_metrics import inspect_snapshot


def profile(name: str, version: str) -> measurement_profiles.CoverageSettings:
    return measurement_profiles.CoverageSettings.model_validate({
        "command": ["true"], "report_path": "coverage.json", "tool_version": version,
        "provisioned_tool": {"name": name, "version": version},
    })


@pytest.mark.parametrize(("name", "version"), [("unknown", "1.0"), ("c8", "latest")])
def test_unknown_or_unpinned_tools_refuse(name: str, version: str) -> None:
    with pytest.raises(ValueError, match="unsupported provisioned tool"):
        profile(name, version)


@pytest.mark.parametrize("damage", ["missing", "hash"])
def test_artifact_refuses_before_runtime_or_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    tool = profile("coverage.py", "7.6.12").provisioned_tool
    assert tool is not None
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    if damage == "hash":
        (artifacts / "coverage-7.6.12-py3-none-any.whl").write_bytes(b"tampered")
    monkeypatch.setattr(tools, "ARTIFACTS", artifacts)
    monkeypatch.setattr(harbor, "assert_runtime", lambda *a, **k: pytest.fail("runtime ran"))
    monkeypatch.setattr(harbor, "_docker", lambda *a, **k: pytest.fail("Docker ran"))
    with pytest.raises(ValueError, match="missing artifact" if damage == "missing" else "SHA-256"):
        harbor.run_native_command(
            snapshot=tmp_path / "unused", task_identity={}, destination=tmp_path / "out",
            command=["true"], report_path="coverage.json", timeout=10, provisioned_tool=tool,
        )


def test_tool_version_hash_and_provisioner_bind_profile_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = tmp_path / "native"
    native.mkdir()
    (native / "task.toml").write_text('[metadata]\nlanguage = "python"\n')
    source = tmp_path / "source"
    source.mkdir()

    def freeze(directory: str) -> dict[str, object]:
        settings = measurement_profiles.MeasurementSettings(coverage=profile("coverage.py", version))
        [collector] = measurement_profiles.prepare_profiles(
            source, tmp_path / directory, ["coverage"], settings, {"task_dir": str(native)},
        )
        frozen = json.loads(collector.config.read_text())["coverage"]["provisioned_tool"]
        assert frozen["name"] == "coverage.py" and frozen["version"] == version
        assert frozen["sha256"] == tools.CATALOG[("coverage.py", version)][1]
        assert frozen["provisioner_version"] == components.VERSIONS["measurement_tools"]
        return collectors.collector_identity(collector)

    version = "7.6.12"
    original = freeze("one")
    # A second pinned release can use the same fixture bytes; version still changes identity.
    monkeypatch.setitem(tools.CATALOG, ("coverage.py", "7.6.13"), tools.CATALOG[("coverage.py", version)])
    version = "7.6.13"
    second = freeze("two")
    assert second["config_digest"] != original["config_digest"]
    artifact = tmp_path / "replacement.whl"
    artifact.write_bytes(b"replacement bytes")
    for name in tools.SUPPORT_FILES["coverage.py"]:
        shutil.copyfile(tools.ARTIFACTS / name, tmp_path / name)
    monkeypatch.setattr(tools, "ARTIFACTS", tmp_path)
    monkeypatch.setitem(tools.CATALOG, ("coverage.py", version), (
        artifact.name, hashlib.sha256(artifact.read_bytes()).hexdigest(),
    ))
    assert freeze("three")["config_digest"] != second["config_digest"]


@pytest.mark.parametrize(("name", "version"), [("coverage.py", "7.6.12"), ("c8", "10.1.3")])
def test_native_coverage_uses_vendored_tool_outside_task_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, version: str,
) -> None:
    """A filesystem-backed fake Docker runs the actual vendored coverage command."""
    if name == "c8" and shutil.which("node") is None:
        pytest.skip("Node is required for c8")
    source = tmp_path / "source"
    source.mkdir()
    suffix = "py" if name == "coverage.py" else "js"
    (source / f"main.{suffix}").write_text("print(1)\n" if suffix == "py" else "console.log(1)\n")
    (source / f"unloaded.{suffix}").write_text("value = 2\n" if suffix == "py" else "const value = 2\n")
    (source / ".git").mkdir()
    (source / ".git/HEAD").write_text("original git state\n")
    container = tmp_path / "container"
    app = container / "app"
    shutil.copytree(source, app)
    identity: dict[str, object] = {
        "isolation": {"resources": {"cpus": 1, "memory_mb": 256},
                      "docker_engine": {"endpoint": "unix:///frozen.sock"}, "docker_cli": {}},
        "image_identity": {"image_id": "sha256:frozen"}, "workdir": "/app",
    }
    monkeypatch.setattr(harbor, "assert_runtime", lambda expected, **kwargs: expected)
    monkeypatch.setattr(harbor, "_frozen_executable", lambda *a: {"resolved_path": "docker"})
    copies: list[str] = []
    preload_marker = tmp_path / "node-preload-ran"
    if suffix == "js":
        (source / "preload.cjs").write_text(
            f"require('fs').writeFileSync({json.dumps(str(preload_marker))}, 'injected');"
        )
        shutil.copyfile(source / "preload.cjs", app / "preload.cjs")

    def container_env(arguments: tuple[str, ...] | list[str]) -> dict[str, str]:
        env = dict(os.environ)
        if suffix == "js":
            env.update(NODE_OPTIONS="--require=" + str(app / "preload.cjs"),
                       NODE_PATH=str(app), EXPERIMENTAL_MONOCART="1")
            for index, argument in enumerate(arguments):
                if argument == "--env":
                    name, value = arguments[index + 1].split("=", 1)
                    env[name] = value
        return env

    def docker(*args: str, **kwargs: object) -> str:
        if args[0] == "create":
            assert args[args.index("--network") + 1] == "none"
            return "container"
        if "--pluginbench-verify" in args:
            command = list(args[args.index("container") + 1:])
            command = [arg.replace("/opt/pluginbench-tools", str(container / "opt/pluginbench-tools"))
                       for arg in command]
            if command[0] == "python":
                command[0] = sys.executable
            result = subprocess.run(command, cwd=app, env=container_env(args),
                                    capture_output=True, text=True)
            assert not preload_marker.exists(), "task NODE_OPTIONS executed before c8 startup"
            assert result.returncode == 0, result.stderr
            return result.stdout
        if args[0] == "cp":
            target = args[2].split(":", 1)[1]
            copies.append(target)
            shutil.copytree(args[1].removesuffix("/."), container / target.lstrip("/"), dirs_exist_ok=True)
        return ""

    monkeypatch.setattr(harbor, "_docker", docker)

    def export(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        shutil.copytree(app, destination, ignore=shutil.ignore_patterns("__pycache__"))
        return {"snapshot_digest": inspect_snapshot(destination)["digest"]}

    monkeypatch.setattr(harbor, "export_native_source", export)

    def execute(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = argv[argv.index("container") + 1:]
        command = [arg.replace("/opt/pluginbench-tools", str(container / "opt/pluginbench-tools"))
                   for arg in command]
        result = subprocess.run(command, cwd=app, env=container_env(argv),
                                capture_output=True, text=True, timeout=30)
        assert not preload_marker.exists(), "task NODE_OPTIONS executed before c8 startup"
        return result

    monkeypatch.setattr(harbor, "run_command", execute)
    monkeypatch.setattr(harbor, "cleanup", lambda **kwargs: {"status": "complete"})
    if suffix == "py":
        (source / ".coveragerc").write_text("[run]\nrelative_files = True\nsource = .\n")
        shutil.copyfile(source / ".coveragerc", app / ".coveragerc")
        command = ["bash", "-c", f"{sys.executable} -I -S /opt/pluginbench-tools/coverage.py/launch-coverage.py run main.py && "
                   f"{sys.executable} -I -S /opt/pluginbench-tools/coverage.py/launch-coverage.py json -o coverage.json"]
        report_path = "coverage.json"
        report_format = "coverage-py-json"
    else:
        command = ["node", "/opt/pluginbench-tools/c8/launch-c8.cjs", "--all",
                   "--reporter=lcovonly", "--reports-dir=coverage", "--exclude=preload.cjs", "--", "node", "main.js"]
        report_path = "coverage/lcov.info"
        report_format = "lcov"
    frozen = tmp_path / "profile.json"
    coverage = profile(name, version).model_copy(update={"command": command, "report_path": report_path})
    frozen.write_text(json.dumps({"native_task_identity": identity, "test_scope": ["test_*"],
                                 "coverage": coverage.model_dump(mode="json")}))
    collector = collectors.Collector.model_validate({
        "kind": "coverage", "config": frozen, "scope": [f"*.{suffix}"], "command": command,
        "tool_version": version, "report_format": report_format, "report_path": report_path,
    })
    before = inspect_snapshot(source)["digest"]
    report, observed_version = collectors._native_coverage(source, collector, tmp_path / "out")
    assert observed_version == version
    assert report and (tmp_path / "out/report.raw").is_file()
    lines = report["lines"]
    assert isinstance(lines, dict)
    assert f"main.{suffix}:1" in lines["covered"]
    assert f"unloaded.{suffix}:1" in lines["total"]
    assert f"unloaded.{suffix}:1" not in lines["covered"]
    receipt = json.loads((tmp_path / "out/execution.json").read_text())
    assert coverage.provisioned_tool is not None
    provisioned = receipt["provisioned_tool"]
    origin = provisioned.pop("runtime_origin")
    assert provisioned == coverage.provisioned_tool.model_dump(mode="json")
    assert str(container / "opt/pluginbench-tools" / name) in origin["module"]
    assert "/opt/pluginbench-tools/" in copies
    assert inspect_snapshot(source)["digest"] == before
    assert (app / ".git/HEAD").read_text() == "original git state\n"
    assert not (app / "opt").exists()


@pytest.mark.parametrize("workdir", ["/", "/opt", "/opt/pluginbench-tools/task"])
def test_tool_path_cannot_overlap_task_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir: str,
) -> None:
    monkeypatch.setattr(harbor, "_docker", lambda *a, **k: pytest.fail("Docker ran"))
    with pytest.raises(ValueError, match="outside the task workdir"):
        harbor.run_native_command(
            snapshot=tmp_path, task_identity={"workdir": workdir}, destination=tmp_path / "out",
            command=["true"], report_path="coverage.json", timeout=10,
            provisioned_tool=profile("c8", "10.1.3").provisioned_tool,
        )


def test_saved_tool_pins_refuse_drift() -> None:
    tool = profile("c8", "10.1.3").provisioned_tool
    assert tool is not None
    frozen = tool.model_dump()
    with pytest.raises(ValueError, match="SHA-256"):
        tools.read_tool({**frozen, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="measurement_tools version"):
        tools.read_tool({**frozen, "provisioner_version": 0})
    with pytest.raises(ValueError, match="tool_version must match"):
        measurement_profiles.CoverageSettings.model_validate({
            "command": ["true"], "report_path": "coverage.json", "tool_version": "1",
            "provisioned_tool": frozen,
        })


def test_mutation_forwards_the_same_frozen_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = profile("c8", "10.1.3").provisioned_tool
    assert tool is not None

    def native(**kwargs: object) -> dict[str, object]:
        assert kwargs["provisioned_tool"] == tool
        return {"exit_code": 0}

    monkeypatch.setattr(harbor, "run_native_command", native)
    execute = mutation.native_executor({}, "coverage.json", provisioned_tool=tool.model_dump())
    assert execute(tmp_path, ["suite"], 10, tmp_path / "run").exit_code == 0
