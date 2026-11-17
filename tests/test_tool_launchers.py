"""Adversarial task files must not supply the provisioned coverage implementation."""

from __future__ import annotations

import json
import base64
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
        command, env = python_command(stage / tool.name, "run-module", "task_main")
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
    # Launcher checks exercise both operations; native integration owns the fresh-container proof.
    runner = root / "test-report.cjs"
    runner.write_text(
        "const {spawnSync}=require('node:child_process'),fs=require('node:fs');"
        "const first=spawnSync(process.execPath,[process.argv[2],...process.argv.slice(3)],{stdio:'inherit'});"
        "if(first.status!==0)process.exit(first.status||1);"
        "if(fs.existsSync('coverage/lcov.info'))process.exit(0);"
        "const report=spawnSync(process.execPath,[process.argv[2],'report'],{stdio:'inherit'});"
        "process.exit(report.status||0);"
    )
    return ["node", str(runner), str(root / "launch-c8.cjs"), *arguments]


@pytest.mark.parametrize("config", [".c8rc.json", ".nycrc.json", "package.json"])
def test_task_c8_config_cannot_execute_custom_reporter(tmp_path: Path, config: str) -> None:
    marker = tmp_path / "reporter-ran"
    tool = tools.ProvisionedTool(name="c8", version="10.1.3")
    with tools.staged_tool(tool, source_files=["main.js"]) as stage:
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


def python_fixture(app: Path) -> None:
    (app / "src").mkdir(parents=True)
    (app / "src/subject.py").write_text("def unused():\n    return 42\n\nvalue = 1\n")
    (app / "runner.py").write_text("import src.subject\n")


def python_measure(root: Path, app: Path, executable: str = sys.executable) -> list[int]:
    for operation in [("run", "runner.py"), ("json",)]:
        command, env = python_command(root, *operation)
        command[0] = executable
        result = subprocess.run(command, cwd=app, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    report = json.loads((app / "coverage.json").read_text())
    missing: list[int] = report["files"]["src/subject.py"]["missing_lines"]
    return missing


REPORTER_PATCH = """import coverage.jsonreport as reporter
original = reporter.JsonReporter.report
def forge(self, morfs, outfile):
    import io, json
    buffer = io.StringIO()
    result = original(self, morfs, buffer)
    data = json.loads(buffer.getvalue())
    for record in data['files'].values():
        record['executed_lines'] += record['missing_lines']
        record['missing_lines'] = []
    json.dump(data, outfile)
    return result
reporter.JsonReporter.report = forge
def coverage_init(reg, options): pass
"""


@pytest.mark.parametrize(("filename", "config"), [
    (".coveragerc", "[run]\nplugins = evil\n"),
    ("setup.cfg", "[coverage:run]\nplugins = evil\n"),
    ("tox.ini", "[coverage:run]\nplugins = evil\n"),
    ("pyproject.toml", '[tool.coverage.run]\nplugins = ["evil"]\n'),
])
def test_task_python_config_cannot_replace_report(tmp_path: Path, filename: str, config: str) -> None:
    app = tmp_path / "app"
    python_fixture(app)
    (app / filename).write_text(config)
    (app / "evil.py").write_text(REPORTER_PATCH)
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12")) as stage:
        report = python_measure(stage / "coverage.py", app)
    assert report == [2]


def test_task_parallel_config_cannot_select_stale_database(tmp_path: Path) -> None:
    app = tmp_path / "app"
    python_fixture(app)
    (app / ".coveragerc").write_text("[run]\ndata_file = chosen.sqlite\nparallel = True\n")
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12")) as stage:
        root = stage / "coverage.py"
        seed = ("import sys; sys.path.insert(0, sys.argv[1]); from coverage import CoverageData; "
                "from pathlib import Path; data=CoverageData(basename='chosen.sqlite'); "
                "data.add_lines({str(Path('src/subject.py').resolve()): {1,2,4}}); data.write()")
        subprocess.run([sys.executable, "-I", "-S", "-c", seed, str(root)], cwd=app, check=True)
        report = python_measure(root, app)
    assert report == [2]


def test_reporting_does_not_run_image_pth_or_task_sitecustomize(tmp_path: Path) -> None:
    app = tmp_path / "app"
    python_fixture(app)
    (app / "sitecustomize.py").write_text(REPORTER_PATCH)
    venv = tmp_path / "image-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = next((venv / "lib").glob("python*/site-packages"))
    (site / "editable-task.pth").write_text(f"import sys; sys.path.insert(0, {str(app)!r})\n")
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12")) as stage:
        report = python_measure(stage / "coverage.py", app, str(venv / "bin/python"))
    assert report == [2]


@pytest.mark.parametrize("damage", ["additional", "symlink", "missing"])
def test_raw_transfer_refuses_non_data_files(tmp_path: Path, damage: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / ".coverage").write_bytes(b"opaque collection data")
    if damage == "additional":
        (data / ".coverage.stale").write_bytes(b"stale database")
    elif damage == "symlink":
        (data / ".coverage").unlink()
        (data / ".coverage").symlink_to(tmp_path / "outside")
    else:
        (data / ".coverage").unlink()
    with pytest.raises(ValueError, match="raw data"):
        with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12"), data=data):
            pytest.fail("untrusted non-data crossed the reporting boundary")


def test_python_exclusion_hints_do_not_remove_unexecuted_lines(tmp_path: Path) -> None:
    python_fixture(tmp_path)
    (tmp_path / "src/subject.py").write_text("def unused():\n    return 42  # pragma: no cover\n\nvalue = 1\n")
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12")) as stage:
        assert python_measure(stage / "coverage.py", tmp_path) == [2]


@pytest.mark.parametrize("hint", ["c8 ignore next", "v8 ignore next", "node:coverage ignore next"])
def test_c8_exclusion_hints_do_not_cover_unexecuted_lines(tmp_path: Path, hint: str) -> None:
    (tmp_path / "main.js").write_text(
        f"function unused() {{\n  /* {hint} */ return 42;\n}}\nconsole.log(1);\n"
    )
    with tools.staged_tool(tools.ProvisionedTool(name="c8", version="10.1.3"), source_files=["main.js"]) as stage:
        result = subprocess.run(c8_command(stage / "c8", "--all", "--", "node", "main.js"),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "DA:2,0" in (tmp_path / "coverage/lcov.info").read_text()


@pytest.mark.parametrize("comment", [
    "//# sourceMappingURL={url}",
    "/*# sourceMappingURL={url} */ //# sourceMappingURL=x",
    "/*# sourceMappingURL={url} */ /*# sourceMappingURL=x */",
    "/*# sourceMappingURL={inline} */ //# sourceMappingURL=x",
])
def test_c8_maps_cannot_credit_unloaded_source(tmp_path: Path, comment: str) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/big.js").write_text("".join(f"export const v{i} = {i};\n" for i in range(20)))
    mapping = json.dumps({"version": 3, "sources": ["src/big.js"], "names": [], "mappings": "AAAA"})
    inline = "data:application/json;base64," + base64.b64encode(mapping.encode()).decode()
    (tmp_path / "main.js").write_text("console.log(1);\n" + comment.format(url="main.js.map", inline=inline) + "\n")
    (tmp_path / "main.js.map").write_text(mapping)
    with tools.staged_tool(tools.ProvisionedTool(name="c8", version="10.1.3"), source_files=["src/big.js"]) as stage:
        result = subprocess.run(c8_command(stage / "c8", "--all", "--", "node", "main.js"),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = (tmp_path / "coverage/lcov.info").read_text()
    assert "SF:src/big.js" in report
    assert [line for line in report.splitlines() if line.startswith("DA:")] == [f"DA:{i},0" for i in range(1, 21)]


def test_provisioned_c8_refuses_remapping_opt_in() -> None:
    with pytest.raises(ValueError, match="source_maps"):
        tools.ProvisionedTool.model_validate({"name": "c8", "version": "10.1.3", "source_maps": True})


def test_c8_source_population_has_no_filename_or_extension_exclusions(tmp_path: Path) -> None:
    from pluginbench.coverage_report import parse_lcov

    names = ["src/rollup.config.js", "src/logic-test.js", "src/upper.JS", *[
        f"src/impl{suffix}" for suffix in [".cjs", ".mjs", ".jsx", ".ts", ".cts", ".mts", ".tsx"]
    ]]
    excluded = ["src/types.d.ts", "src/types.d.mts", "src/types.d.cts", "src/tool.py", "src/x.vue",
                "src/core.test.js", "src/view.test.tsx", "src/__tests__/a.ts"]
    (tmp_path / "src").mkdir()
    for name in [*names, *excluded]:
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("export const value = 1;\n")
    (tmp_path / "main.cjs").write_text("console.log(1)\n")
    with tools.staged_tool(tools.ProvisionedTool(name="c8", version="10.1.3"), source_files=names + excluded) as stage:
        result = subprocess.run(c8_command(stage / "c8", "--all", "--", "node", "main.cjs"),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = parse_lcov(tmp_path / "coverage/lcov.info", snapshot=tmp_path, source_scope=["src/**"],
                        test_scope=["tests/**"], workdir=str(tmp_path))
    lines = report["lines"]
    assert isinstance(lines, dict)
    assert set(lines["total"]) == {f"{name}:1" for name in names}
    assert lines["covered"] == []


def test_c8_refuses_scope_containing_only_tests(tmp_path: Path) -> None:
    (tmp_path / "b.test.js").write_text("const value = 1;\n")
    with tools.staged_tool(tools.ProvisionedTool(name="c8", version="10.1.3"), source_files=["b.test.js"]) as stage:
        result = subprocess.run(c8_command(stage / "c8", "--all", "--", "node", "-e", "1"),
                                cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "PluginBench c8: no files in the profile source scope" in result.stderr


@pytest.mark.parametrize("name", ["a[b].py", "a*b.py", "a?b.py", "a]b.py"])
def test_python_source_scope_matches_literal_filenames(tmp_path: Path, name: str) -> None:
    for filename in [name, "ab.py", "axb.py"]:
        (tmp_path / filename).write_text("value = 1\n")
    (tmp_path / "runner.py").write_text(f"import runpy\nrunpy.run_path({name!r})\nrunpy.run_path('ab.py')\n")
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12"), source_files=[name]) as stage:
        for operation in [("run", "runner.py"), ("json",)]:
            command, env = python_command(stage / "coverage.py", *operation)
            result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "coverage.json").read_text())
    assert set(report["files"]) == {name}
    assert report["files"][name]["executed_lines"] == [1]


def test_python_unloaded_namespace_package_files_remain_in_population(tmp_path: Path) -> None:
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/__init__.py").write_text("")
    (tmp_path / "src/pkg/used.py").write_text("value = 1\n")
    (tmp_path / "src/pkg/unused.py").write_text("value = 2\n")
    (tmp_path / "runner.py").write_text("import src.pkg.used\n")
    with tools.staged_tool(tools.ProvisionedTool(name="coverage.py", version="7.6.12")) as stage:
        for operation in [("run", "runner.py"), ("json",)]:
            command, env = python_command(stage / "coverage.py", *operation)
            subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    report = json.loads((tmp_path / "coverage.json").read_text())
    assert report["files"]["src/pkg/unused.py"]["missing_lines"] == [1]
