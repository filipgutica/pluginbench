import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Literal

import pytest

from pluginbench.assessment_metrics import evaluate_evidence, inspect_snapshot
from pluginbench.collectors import Collector, _execute, collect_measurements, freeze_collectors
from pluginbench.task_data.deepswe.measurements import ROOT, observer_cache_root


def source(root: Path) -> Path:
    snapshot = root / "source"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src/used.ts").write_text("export const value = () => 42;\n")
    return snapshot


def config(root: Path, value: object) -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(value))
    return path


def _syntax_command() -> list[str]:
    name = "oxc-parser-0.126.0"
    lock_digest = hashlib.sha256((ROOT / "observers" / name / "package-lock.json").read_bytes())
    cache = observer_cache_root() / name / lock_digest.hexdigest()
    if not (cache / "node_modules/oxc-parser").is_dir():
        pytest.skip("pinned Oxc observer cache unavailable")
    return [
        "node",
        str(Path(__file__).parents[1] / "src/pluginbench/fallow_parse.mjs"),
        str(cache),
        "{snapshot}",
        "{inputs}",
        "{output}",
    ]


def test_custom_collector_cannot_change_archived_source(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    script = tmp_path / "mutate.py"
    script.write_text(
        "import pathlib,sys\npathlib.Path(sys.argv[1], 'src/used.ts').write_text('changed')\npathlib.Path(sys.argv[2]).write_text('{}')\n"
    )
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=[sys.executable, str(script), "{snapshot}", "{output}"],
        tool_version="test",
    )
    with pytest.raises(ValueError, match="modified measured source"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert (snapshot / "src/used.ts").read_text() == "export const value = () => 42;\n"


def test_collector_failure_is_unavailable_not_zero(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=[sys.executable, "-c", "raise SystemExit(7)"],
        tool_version="test",
    )
    assert collect_measurements(snapshot, tmp_path / "evidence", [collector]) == {}
    assert "exited 7" in (tmp_path / "evidence/coverage/failure.json").read_text()


def test_collector_scripts_and_configs_are_frozen(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text("original")
    collector = Collector(
        kind="mutation",
        config=config(tmp_path, {}),
        command=[sys.executable, str(script)],
        tool_version="test",
    )
    frozen = freeze_collectors([collector], tmp_path / "frozen")[0]
    script.write_text("changed")
    assert frozen.command is not None
    assert Path(frozen.command[1]).read_text() == "original"
    collector.config.write_text('{"extends": ["elsewhere"]}')
    with pytest.raises(ValueError, match="resolved JSON"):
        freeze_collectors([collector], tmp_path / "bad-frozen")


def test_python_collector_import_does_not_mutate_frozen_scripts(tmp_path: Path) -> None:
    script = tmp_path / "collector.py"
    helper = tmp_path / "helper.py"
    helper.write_text("value = 42\n")
    script.write_text(
        "import importlib.util,sys\n"
        "spec = importlib.util.spec_from_file_location('helper', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(module.value)\n"
    )
    collector = Collector(
        kind="mutation",
        config=config(tmp_path, {}),
        command=[sys.executable, str(script), str(helper)],
        tool_version="test",
    )
    frozen_root = tmp_path / "frozen"
    frozen = freeze_collectors([collector], frozen_root)[0]
    before = {
        path.relative_to(frozen_root): path.read_bytes()
        for path in frozen_root.rglob("*")
        if path.is_file()
    }
    assert frozen.command is not None
    result = _execute(frozen.command, tmp_path, 10)
    assert result.returncode == 0 and result.stdout.strip() == "42"
    after = {
        path.relative_to(frozen_root): path.read_bytes()
        for path in frozen_root.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_node_coverage_keeps_unimported_source_unknown(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    (snapshot / "src/unloaded.ts").write_text("export const neverLoaded = () => false;\n")
    (snapshot / "package.json").write_text('{"type":"module"}')
    (snapshot / "test").mkdir()
    (snapshot / "test/value.test.ts").write_text(
        "import {test} from 'node:test';\nimport {strict as assert} from 'node:assert';\nimport {value} from '../src/used.ts';\ntest('value', () => assert.equal(value(), 42));\n"
    )
    collector = Collector(
        kind="coverage", config=config(tmp_path, {"test_globs": ["test/*.test.ts"]})
    )
    result = collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert "coverage" in result, (
        (tmp_path / "evidence/coverage/failure.json").read_text() if not result else ""
    )
    evidence = evaluate_evidence(
        snapshot,
        {"coverage": str(result["coverage"])},
        {"coverage": {"min_function_percent": 90}},
        tmp_path,
    )
    coverage = evidence["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["unknown_standards"] == ["min_function_percent"]
    metrics = coverage["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["function_percent"] == 100
    assert metrics["unmeasured_source_files"] == ["src/unloaded.ts"]
    assert metrics["population_complete_for_declared_scope"] is False


@pytest.mark.skipif(shutil.which("fallow") is None, reason="Fallow unavailable")
@pytest.mark.parametrize("with_syntax", [False, True])
def test_fallow_calibration_counts_planted_dead_code_and_type_only_files(
    tmp_path: Path, with_syntax: bool
) -> None:
    snapshot = source(tmp_path)
    (snapshot / "src/types.ts").write_text("export interface Value { value: number }\n")
    (snapshot / "src/used.ts").write_text(
        "import type {Value} from './types';\nexport const value = (): Value => ({value: 42});\nconst unused = () => 9;\n"
    )
    collector = Collector(
        kind="fallow",
        syntax_command=_syntax_command() if with_syntax else None,
        config=config(
            tmp_path,
            {
                "entry": ["src/used.ts"],
                "production": True,
                "health": {"maxCyclomatic": 0, "maxCognitive": 0, "maxUnitSize": 0},
            },
        ),
    )
    result = collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert "fallow" in result
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, {}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict)
    metrics = fallow["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["complexity_complete_for_declared_scope"] is with_syntax
    assert metrics["dead_code_complete_for_declared_scope"] is with_syntax
    # Fallow counts unused exports/files, not arbitrary unexported local variables.
    assert metrics["dead_code"] == 0
    (snapshot / "src/dead.ts").write_text("export const unused = () => 9;\n")
    result = collect_measurements(snapshot, tmp_path / "dead-evidence", [collector])
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, {}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict) and isinstance(fallow["metrics"], dict)
    assert fallow["metrics"]["dead_code"] > 0


@pytest.mark.skipif(shutil.which("fallow") is None, reason="Fallow unavailable")
def test_fallow_zero_entry_graph_does_not_prove_clean_scope(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    collector = Collector(
        kind="fallow",
        config=config(
            tmp_path,
            {"entry": [], "health": {"maxCognitive": 0, "maxCyclomatic": 0, "maxUnitSize": 0}},
        ),
    )
    result = collect_measurements(snapshot, tmp_path / "evidence", [collector])
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, {}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict) and isinstance(fallow["metrics"], dict)
    assert fallow["metrics"]["dead_code_complete_for_declared_scope"] is False


@pytest.mark.skipif(shutil.which("fallow") is None, reason="Fallow unavailable")
@pytest.mark.parametrize("with_syntax", [False, True])
def test_same_line_anonymous_functions_have_distinct_column_identities(
    tmp_path: Path, with_syntax: bool
) -> None:
    snapshot = source(tmp_path)
    (snapshot / "src/used.ts").write_text(
        "export const value = () => [1].map(() => [2].map(() => 42));\n"
    )
    collector = Collector(
        kind="fallow",
        syntax_command=_syntax_command() if with_syntax else None,
        config=config(
            tmp_path,
            {
                "entry": ["src/used.ts"],
                "production": True,
                "health": {"maxCognitive": 0, "maxCyclomatic": 0, "maxUnitSize": 0},
            },
        ),
    )
    result = collect_measurements(snapshot, tmp_path / "evidence", [collector])
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, {}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict) and isinstance(fallow["metrics"], dict)
    assert fallow["metrics"]["function_count_reported"] == 3
    assert fallow["metrics"]["complexity_complete_for_declared_scope"] is with_syntax


@pytest.mark.parametrize("kind", ["coverage", "mutation"])
def test_syntax_command_is_only_for_fallow(
    tmp_path: Path, kind: Literal["coverage", "mutation"]
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="optional Fallow parser"):
        Collector(
            kind=kind,
            config=config(tmp_path, {}),
            command=["tool"],
            tool_version="1",
            syntax_command=["node"],
        )


def test_fallow_syntax_validator_is_frozen_and_keeps_builtin_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import pluginbench.collectors as collectors

    snapshot = source(tmp_path)
    script = tmp_path / "parse.mjs"
    script.write_text("original validator")
    collector = Collector(
        kind="fallow",
        config=config(tmp_path, {"production": True}),
        syntax_command=["node", str(script), "{snapshot}", "{inputs}", "{output}"],
    )
    frozen = freeze_collectors([collector], tmp_path / "frozen")[0]
    script.write_text("changed validator")
    assert frozen.syntax_command is not None
    assert Path(frozen.syntax_command[1]).read_text() == "original validator"
    calls = []

    def execute(argv: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "node":
            assert json.loads(Path(argv[-2]).read_text()) == {
                "files": ["src/used.ts"],
                "file_count": 1,
            }
            Path(argv[-1]).write_text(json.dumps({"files": [], "version": "0.126.0"}))
            return subprocess.CompletedProcess(argv, 0, "", "")
        report: dict[str, object] = {
            "kind": "combined",
            "version": "3.18.0",
            "check": {},
            "dupes": {},
            "health": {"findings": []},
        }
        if argv[-2:] == ["list", "--files"]:
            report = {"files": ["src/used.ts"], "file_count": 1}
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(collectors, "_execute", execute)
    result = collect_measurements(snapshot, tmp_path / "evidence", [frozen])
    assert "fallow" in result
    assert [call[0] for call in calls] == ["fallow", "fallow", "node"]
    report = json.loads((tmp_path / "evidence/fallow/report.json").read_text())
    proof = report["syntax_validation"]
    assert proof["producer_version"] == "3.18.0"
    assert len(proof["producer_execution"]) == 2
    assert proof["validator_files"]["1"].startswith("sha256:")
    assert json.loads(result["fallow"].read_text())["tool_version"] == "3.18.0"


@pytest.mark.parametrize(
    "source_bytes, valid",
    [(b"export const value = 1;\n", True), (b"export const value = ;\n", False), (b"\xff", False)],
)
def test_pinned_oxc_observer_reports_parse_and_utf8_errors(
    tmp_path: Path, source_bytes: bytes, valid: bool
) -> None:
    snapshot = source(tmp_path)
    (snapshot / "src/used.ts").write_bytes(source_bytes)
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"files": ["src/used.ts"], "file_count": 1}))
    output = tmp_path / "syntax.json"
    execution = _execute(
        [
            arg.replace("{snapshot}", str(snapshot))
            .replace("{inputs}", str(inputs))
            .replace("{output}", str(output))
            for arg in _syntax_command()
        ],
        tmp_path,
        30,
    )
    assert execution.returncode == 0, execution.stderr
    receipt = json.loads(output.read_text())
    assert receipt["files"][0]["status"] == ("valid" if valid else "invalid")
    assert bool(receipt["files"][0]["errors"]) is not valid
    assert receipt["lock_digest"].startswith("sha256:")
    assert receipt["dependency_hashes"]


@pytest.mark.parametrize("target", ["snapshot", "before"])
def test_nonzero_collector_source_mutation_is_fatal(tmp_path: Path, target: str) -> None:
    snapshot = source(tmp_path)
    before = tmp_path / "before"
    shutil.copytree(snapshot, before)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        tool_version="test",
        command=[
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1], 'src/used.ts').write_text('changed'); sys.exit(7)",
            "{" + target + "}",
            "{before}",
        ],
    )
    with pytest.raises(ValueError, match="modified"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector], before=before)
    receipt = json.loads((tmp_path / "evidence/coverage/execution.json").read_text())
    assert receipt[0]["exit_code"] == 7


def test_successful_invalid_collector_report_is_fatal(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        tool_version="test",
        command=[
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{}')",
            "{output}",
        ],
    )
    with pytest.raises(ValueError):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])


def test_nonzero_collector_profile_mutation_is_fatal(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    cfg = config(tmp_path, {})
    collector = Collector(
        kind="coverage",
        config=cfg,
        tool_version="test",
        command=[
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{\"changed\":true}'); sys.exit(7)",
            str(cfg),
        ],
    )
    with pytest.raises(ValueError, match="modified its frozen profile"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])


def test_timeout_preserves_bound_execution_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import pluginbench.collectors as collectors

    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage", config=config(tmp_path, {}), command=["coverage-tool"], tool_version="test"
    )

    def timeout(argv: list[str], root: Path, seconds: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            argv, seconds, output="partial output", stderr="native error"
        )

    monkeypatch.setattr(collectors, "_execute", timeout)
    assert collect_measurements(snapshot, tmp_path / "evidence", [collector]) == {}
    receipt = json.loads((tmp_path / "evidence/coverage/execution.json").read_text())[0]
    assert receipt["exit_code"] is None
    assert receipt["stdout"] == "partial output" and receipt["stderr"] == "native error"
    failure = json.loads((tmp_path / "evidence/coverage/failure.json").read_text())
    assert failure["snapshot_digest"] == inspect_snapshot(snapshot)["digest"]
    assert failure["execution"]["sha256"].startswith("sha256:")


@pytest.mark.parametrize("exit_code", [2, 7])
def test_trusted_adapter_unclassified_inner_failure_is_fatal(
    tmp_path: Path, exit_code: int
) -> None:
    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=[sys.executable, "-c", f"raise SystemExit({exit_code})"],
        tool_version="test",
        failure_protocol="coverage-exit-v1",
    )
    with pytest.raises(ValueError, match="trusted coverage adapter"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert not (tmp_path / "evidence/coverage/failure.json").exists()


def test_trusted_adapter_classified_runtime_failure_remains_unknown(tmp_path: Path) -> None:
    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=[sys.executable, "-c", "raise SystemExit(3)"],
        tool_version="test",
        failure_protocol="coverage-exit-v1",
    )
    frozen = freeze_collectors([collector], tmp_path / "frozen")[0]
    assert frozen.failure_protocol == "coverage-exit-v1"
    assert collect_measurements(snapshot, tmp_path / "evidence", [frozen]) == {}
    failure = json.loads((tmp_path / "evidence/coverage/failure.json").read_text())
    assert failure["collector"]["failure_protocol"] == "coverage-exit-v1"
    assert (
        json.loads((tmp_path / "evidence/coverage/execution.json").read_text())[0]["exit_code"] == 3
    )


def test_trusted_adapter_outer_timeout_is_unclassified_and_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import pluginbench.collectors as collectors

    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=["coverage-tool"],
        tool_version="test",
        failure_protocol="coverage-exit-v1",
    )

    def timeout(argv: list[str], root: Path, seconds: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, seconds)

    monkeypatch.setattr(collectors, "_execute", timeout)
    with pytest.raises(ValueError, match="trusted coverage adapter failure was not classified"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert not (tmp_path / "evidence/coverage/failure.json").exists()


def test_prepared_coverage_profile_declares_and_freezes_failure_contract(tmp_path: Path) -> None:
    from pluginbench.task_data.deepswe import measurements
    from pluginbench.remeasure import load_profiles
    from test_real_coding_measurements import prepared_task

    prepared = tmp_path / "prepared"
    task = "ofetch-per-origin-circuit-breaker"
    verifier, payload = prepared_task(measurements, prepared, task)
    collectors = measurements.prepare_measurements(
        prepared=prepared,
        task=task,
        payload={key: value for key, value in payload.items()},
        verifier=verifier,
        selection=["coverage"],
        observer_cache=tmp_path / "cache",
    )
    assert collectors[0].failure_protocol == "coverage-exit-v1"
    assert load_profiles(prepared / "measurements.json")[task] == collectors
    assert (
        freeze_collectors(collectors, tmp_path / "frozen")[0].failure_protocol == "coverage-exit-v1"
    )
