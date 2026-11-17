"""Adversarial task files must not supply the provisioned coverage implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pluginbench import measurement_tools as tools


def python_command(root: Path, *arguments: str) -> tuple[list[str], dict[str, str]]:
    launcher = root / "launch-coverage.py"
    if launcher.exists():
        return [sys.executable, "-I", "-S", str(launcher), *arguments], dict(os.environ)
    # Exercise the documented pre-fix launch so red evidence observes task code executing.
    return [sys.executable, "-S", "-m", "coverage", *arguments], {
        **os.environ, "PYTHONPATH": str(root),
    }


def test_task_coverage_module_cannot_shadow_pinned_distribution(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    marker = tmp_path / "shadow-ran"
    (app / "coverage.py").write_text(f"open({str(marker)!r}, 'w').write('task tool ran')\n")
    (app / "helper.py").write_text("value = 42\n")
    (app / "task_main.py").write_text(
        "import helper\nfrom pathlib import Path\n"
        f"Path({str(tmp_path / 'measured')!r}).write_text(str(helper.value))\n"
    )
    tool = tools.ProvisionedTool(name="coverage.py", version="7.6.12")
    with tools.staged_tool(tool) as stage:
        command, env = python_command(stage / tool.name, "run", "-m", "task_main")
        result = subprocess.run(command, cwd=app, env=env, capture_output=True, text=True, timeout=30)
    assert not marker.exists(), "task-local coverage.py executed instead of the pinned tool"
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "measured").read_text() == "42"


def test_coverage_dependency_symlink_refuses_before_task_code_executes(tmp_path: Path) -> None:
    marker = tmp_path / "dependency-ran"
    evil = tmp_path / "evil.py"
    evil.write_text(f"open({str(marker)!r}, 'w').write('dependency ran')\n")
    tool = tools.ProvisionedTool(name="coverage.py", version="7.6.12")
    with tools.staged_tool(tool) as stage:
        root = stage / tool.name
        (root / "coverage/misc.py").unlink()
        (root / "coverage/misc.py").symlink_to(evil)
        command, env = python_command(root, "--version")
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert not marker.exists(), "an out-of-distribution coverage dependency executed"
    assert result.returncode != 0
    assert "outside staged distribution" in result.stderr


def c8_command(root: Path, *arguments: str) -> list[str]:
    launcher = root / "launch-c8.cjs"
    if launcher.exists():
        return ["node", str(launcher), *arguments]
    return ["node", str(root / "node_modules/c8/bin/c8.js"), *arguments]


@pytest.mark.parametrize("config", [".c8rc.json", ".nycrc.json", "package.json"])
def test_task_c8_config_cannot_execute_custom_reporter(tmp_path: Path, config: str) -> None:
    marker = tmp_path / "reporter-ran"
    tool = tools.ProvisionedTool(name="c8", version="10.1.3")
    with tools.staged_tool(tool) as stage:
        root = stage / tool.name
        reporter = tmp_path / "reporter.cjs"
        reporter.write_text(
            f"require('fs').writeFileSync({json.dumps(str(marker))}, 'task reporter ran');\n"
            f"module.exports = require({json.dumps(str(root / 'node_modules/istanbul-reports/lib/lcovonly'))});\n"
        )
        settings = {"reporter": ["lcovonly", str(reporter)]}
        (tmp_path / config).write_text(json.dumps({"c8": settings} if config == "package.json" else settings))
        (tmp_path / "main.js").write_text("console.log(42)\n")
        result = subprocess.run(c8_command(root, "--all", "--", "node", "main.js"),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert not marker.exists(), "task configuration executed a require-able reporter"
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "coverage/lcov.info").is_file()


@pytest.mark.parametrize("options", [["--reporter=./evil.cjs"], ["-r", "./evil.cjs"],
                                     ["--config=./evil.json"], ["--experimental-monocart"]])
def test_c8_launcher_refuses_reporter_or_config_overrides(tmp_path: Path, options: list[str]) -> None:
    tool = tools.ProvisionedTool(name="c8", version="10.1.3")
    marker = tmp_path / "override-reporter-ran"
    measured = tmp_path / "instrumentee-ran"
    with tools.staged_tool(tool) as stage:
        reporter = tmp_path / "evil.cjs"
        reporter.write_text(
            f"require('fs').writeFileSync({json.dumps(str(marker))}, 'reporter ran');"
            f"module.exports=require({json.dumps(str(stage / tool.name / 'node_modules/istanbul-reports/lib/lcovonly'))});"
        )
        config = tmp_path / "evil.json"
        config.write_text(json.dumps({"reporter": [str(reporter)]}))
        options = [value.replace("./evil.cjs", str(reporter)).replace("./evil.json", str(config))
                   for value in options]
        task = f"require('fs').writeFileSync({json.dumps(str(measured))}, 'ran')"
        result = subprocess.run(c8_command(stage / tool.name, *options, "--", "node", "-e", task),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert not marker.exists(), "an override executed task reporter code"
    assert not measured.exists(), "an invalid reporter/config executed the measured program"
    assert result.returncode != 0
    assert "PluginBench c8" in result.stderr


def test_unused_tool_catalog_does_not_invalidate_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    from pluginbench import components, remeasure

    original = remeasure._implementation()
    source_sha256 = components.source_sha256
    monkeypatch.setattr(components, "source_sha256", lambda name: (
        "changed unrelated catalog" if name == "measurement_tools.py" else source_sha256(name)
    ))
    assert remeasure._implementation() == original


def test_resume_binds_only_the_selected_distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pluginbench import collectors, components, remeasure

    tool = tools.ProvisionedTool(name="coverage.py", version="7.6.12")
    config = tmp_path / "coverage.json"
    config.write_text(json.dumps({"coverage": {"provisioned_tool": tool.model_dump()}}))
    collector = collectors.Collector(
        kind="coverage", config=config, command=["true"], tool_version="7.6.12",
        report_format="coverage-py-json", report_path="coverage.json",
    )
    selected = {"task": [collector]}
    original = remeasure._implementation(selected)
    assert original["measurement_tools"] == {"coverage.py@7.6.12": tool.model_dump()}
    monkeypatch.setitem(tools.CATALOG, ("c8", "10.1.3"), ("missing-c8.tar.gz", "other hash"))
    assert remeasure._implementation(selected) == original
    monkeypatch.setitem(components.VERSIONS, "measurement_tools", tool.provisioner_version + 1)
    with pytest.raises(ValueError, match="measurement_tools version"):
        remeasure._implementation(selected)


def test_c8_bundle_keeps_maintained_eastasianwidth_notice() -> None:
    import tarfile

    filename, _ = tools.CATALOG[("c8", "10.1.3")]
    with tarfile.open(tools.ARTIFACTS / filename) as bundle:
        for source, target in [("eastasianwidth-LICENSE", "MIT-LICENSE.txt"),
                               ("eastasianwidth-NOTICE", "LICENSE-NOTES.md")]:
            content = bundle.extractfile("node_modules/eastasianwidth/" + target)
            assert content is not None
            assert content.read() == (tools.ARTIFACTS / source).read_bytes()
    notice = (tools.ARTIFACTS / "eastasianwidth-NOTICE").read_text()
    assert "0.2.0" in notice and "b89f04d44dc786885615e94cd6e2ba1ef7866fa4" in notice


def test_custom_adapter_metadata_is_not_interpreted_as_native_tool_settings(tmp_path: Path) -> None:
    from pluginbench import collectors, remeasure

    config = tmp_path / "adapter.json"
    config.write_text('{"coverage": "adapter-owned metadata"}')
    custom = collectors.Collector(kind="coverage", config=config, command=["true"], tool_version="1")
    assert remeasure._implementation({"task": [custom]}) == remeasure._implementation()


def test_measured_program_keeps_its_own_unused_optional_dependency(tmp_path: Path) -> None:
    if sys.version_info < (3, 11):
        pytest.skip("coverage uses stdlib tomllib starting with Python 3.11")
    app = tmp_path / "app"
    app.mkdir()
    dependencies = tmp_path / "task-dependencies"
    dependencies.mkdir()
    (dependencies / "tomli.py").write_text("VALUE = 'task dependency'\n")
    (app / "main.py").write_text("import tomli\nfrom pathlib import Path\nPath('result').write_text(tomli.VALUE)\n")
    tool = tools.ProvisionedTool(name="coverage.py", version="7.6.12")
    with tools.staged_tool(tool) as stage:
        command, env = python_command(stage / tool.name, "run", "main.py")
        env["PYTHONPATH"] = str(dependencies)
        result = subprocess.run(command, cwd=app, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (app / "result").read_text() == "task dependency"
