import json
import shutil
import sys
from pathlib import Path

import pytest

from pluginbench.assessment_metrics import evaluate_evidence, inspect_snapshot
from pluginbench import isolation
from pluginbench.collectors import (
    observer_identity,
    Collector,
    _execute,
    collect_measurements,
    freeze_collectors,
)
from pluginbench.measurement_profiles import prepare_observer


def source(root: Path) -> Path:
    snapshot = root / "source"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src/used.ts").write_text("export const value = () => 42;\n")
    return snapshot


def config(root: Path, value: object) -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize(
    ("operator_owned", "selected"), [(False, True), (True, False), (True, True)]
)
def test_only_operator_json_can_grant_selected_read_paths(
    tmp_path: Path, operator_owned: bool, selected: bool
) -> None:
    job = tmp_path / "job"
    job.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private")
    settings = (tmp_path if operator_owned else job) / "settings.json"
    settings.write_text(json.dumps({"asset": str(secret)}))
    script = job / "probe.py"
    script.write_text("""import json, sys
from pathlib import Path
asset = Path(json.loads(Path(sys.argv[1]).read_text())['asset'])
try:
    asset.read_text()
    allowed = True
except OSError:
    allowed = False
assert allowed == (sys.argv[2] == 'True')
""")
    execution = _execute(
        [sys.executable, str(script), str(settings), str(operator_owned and selected)],
        job,
        10,
        config=settings if selected else None,
    )
    assert execution.returncode == 0, execution.stderr


def test_frozen_verifier_dependencies_are_read_only_and_exact(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    asset = dependencies / "value.txt"
    asset.write_text("42")
    neighbor = tmp_path / "private.txt"
    neighbor.write_text("private")
    script = tmp_path / "verifier.py"
    script.write_text(
        "import json\nfrom pathlib import Path\n"
        f"PAYLOAD = { {'dependencies': str(dependencies)}!r}\n"
        "asset = Path(PAYLOAD['dependencies']) / 'value.txt'\n"
        "result = {}\n"
        "try:\n    result['read'] = asset.read_text()\n"
        "except OSError:\n    result['read'] = None\n"
        "try:\n    asset.write_text('changed')\n    result['write'] = True\n"
        "except OSError:\n    result['write'] = False\n"
        f"try:\n    Path({str(neighbor)!r}).read_text()\n    result['neighbor'] = True\n"
        "except OSError:\n    result['neighbor'] = False\n"
        "print(json.dumps(result))\n"
    )
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=[sys.executable, str(script)],
        tool_version="test",
    )
    frozen = freeze_collectors([collector], tmp_path / "frozen")[0]
    assert frozen.command is not None
    result = _execute(frozen.command, job, 10, config=frozen.config)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"read": "42", "write": False, "neighbor": False}
    assert asset.read_text() == "42"


@pytest.mark.parametrize("kind", ["python", "json"])
@pytest.mark.parametrize(
    "route",
    [
        "submitted",
        "submitted-link",
        "outside-link",
        "round-trip",
        "declared-submitted-link",
        "declared-round-trip",
    ],
)
def test_submitted_metadata_and_aliases_cannot_grant_reads(
    tmp_path: Path, kind: str, route: str
) -> None:
    job = tmp_path / "job"
    job.mkdir()
    operator = tmp_path / "operator"
    operator.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private")
    asset = secret
    if route == "declared-submitted-link":
        asset = job / "asset-link"
        asset.symlink_to(secret)
    elif route == "declared-round-trip":
        (tmp_path / "bridge").symlink_to(job / "back", target_is_directory=True)
        (job / "back").symlink_to(tmp_path, target_is_directory=True)
        asset = tmp_path / "bridge" / secret.name
    metadata = operator / ("verifier.py" if kind == "python" else "settings.json")
    if kind == "python":
        metadata.write_text(
            "from pathlib import Path\n"
            f"PAYLOAD = { {'asset': str(asset)}!r}\n"
            "try:\n    Path(PAYLOAD['asset']).read_text()\n    allowed = True\n"
            "except OSError:\n    allowed = False\n"
            "print(allowed)\n"
        )
    else:
        metadata.write_text(json.dumps({"asset": str(asset)}))
    if route == "submitted":
        selected = job / metadata.name
        shutil.copy2(metadata, selected)
    elif route == "submitted-link":
        selected = job / metadata.name
        selected.symlink_to(metadata)
    elif route == "outside-link":
        selected = tmp_path / metadata.name
        selected.symlink_to(metadata)
    elif route == "round-trip":
        (tmp_path / "bridge").symlink_to(job / "back", target_is_directory=True)
        (job / "back").symlink_to(operator, target_is_directory=True)
        selected = tmp_path / "bridge" / metadata.name
    else:
        selected = metadata
    if kind == "python":
        argv = [sys.executable, str(selected)]
        settings = None
    else:
        script = job / "probe.py"
        script.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "asset = Path(json.loads(Path(sys.argv[1]).read_text())['asset'])\n"
            "try:\n    asset.read_text()\n    allowed = True\n"
            "except OSError:\n    allowed = False\n"
            "print(allowed)\n"
        )
        argv = [sys.executable, str(script), str(selected)]
        settings = selected
    result = _execute(argv, job, 10, config=settings)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_canonical_preparation_preserves_trusted_alias_reads(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    operator = tmp_path / "operator"
    operator.mkdir()
    alias = tmp_path / "trusted-alias"
    alias.symlink_to(operator, target_is_directory=True)
    asset = operator / "value.txt"
    asset.write_text("42")
    script = operator / "verifier.py"
    script.write_text(
        "from pathlib import Path\n"
        f"PAYLOAD = { {'asset': str((alias / asset.name).resolve())}!r}\n"
        "print(Path(PAYLOAD['asset']).read_text())\n"
    )
    result = _execute([sys.executable, str((alias / script.name).resolve())], job, 10)
    assert result.returncode == 0 and result.stdout.strip() == "42", result.stderr


def test_dynamic_payload_runs_once_without_granting_reads(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("private")
    script = tmp_path / "verifier.py"
    script.write_text(
        "import json\nfrom pathlib import Path\n"
        f"PAYLOAD = json.loads({json.dumps({'asset': str(secret)})!r})\n"
        "with Path('executions.txt').open('a') as log:\n    log.write('executed\\n')\n"
        "try:\n    Path(PAYLOAD['asset']).read_text()\n    allowed = True\n"
        "except OSError:\n    allowed = False\n"
        "print(allowed)\n"
    )
    result = _execute([sys.executable, str(script)], job, 10)
    assert result.returncode == 0 and result.stdout.strip() == "False", result.stderr
    assert (job / "executions.txt").read_text() == "executed\n"


def _syntax_command(cache: Path) -> list[str]:
    observer = prepare_observer("oxc-parser-0.126.0", cache)
    prepared = Path(observer["path"]).parent
    return [
        "node",
        str(Path(__file__).parents[1] / "src/pluginbench/fallow_parse.mjs"),
        str(prepared),
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
        kind="coverage",
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
        kind="coverage",
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


@pytest.mark.skipif(shutil.which("fallow") is None, reason="Fallow unavailable")
@pytest.mark.parametrize("with_syntax", [False, True])
def test_fallow_calibration_counts_planted_dead_code_and_type_only_files(
    tmp_path: Path, with_syntax: bool
) -> None:
    snapshot = source(tmp_path)
    (snapshot / "src/types.ts").write_text("export interface Value { value: number }\n")
    (snapshot / "vitest.config.ts").write_text("export const development = () => 1;\n")
    (snapshot / ".prettierrc.js").write_text("module.exports = {};\n")
    (snapshot / "src/app.config.ts").write_text("export const runtime = () => 0;\n")
    (snapshot / "src/used.ts").write_text(
        "import type {Value} from './types';\nimport {runtime} from './app.config';\nexport const value = (): Value => ({value: 42 + runtime()});\nconst unused = () => 9;\n"
    )
    collector = Collector(
        kind="fallow",
        scope=["*"],
        syntax_command=_syntax_command(tmp_path / "parser-cache") if with_syntax else None,
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
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict)
    metrics = fallow["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["complexity_complete_for_declared_scope"] is with_syntax
    assert metrics["dead_code_complete_for_declared_scope"] is with_syntax
    assert "src/app.config.ts" in {row["path"] for row in metrics["functions"]}
    # Fallow counts unused exports/files, not arbitrary unexported local variables.
    assert metrics["dead_code"] == 0
    (snapshot / "src/dead.ts").write_text("export const unused = () => 9;\n")
    result = collect_measurements(snapshot, tmp_path / "dead-evidence", [collector])
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, tmp_path)
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
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, tmp_path)
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
        syntax_command=_syntax_command(tmp_path / "parser-cache") if with_syntax else None,
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
    evidence = evaluate_evidence(snapshot, {"fallow": str(result["fallow"])}, tmp_path)
    fallow = evidence["fallow"]
    assert isinstance(fallow, dict) and isinstance(fallow["metrics"], dict)
    assert fallow["metrics"]["function_count_reported"] == 3
    assert fallow["metrics"]["complexity_complete_for_declared_scope"] is with_syntax


def test_syntax_command_is_only_for_fallow(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="optional Fallow parser"):
        Collector(
            kind="coverage",
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

    def execute(
        argv: list[str],
        root: Path,
        timeout: int,
        *,
        output: Path | None = None,
        config: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
            for arg in _syntax_command(tmp_path / "parser-cache")
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
def test_nonzero_collector_source_mutation_is_fatal(
    tmp_path: Path,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pluginbench.processes import run_command

    # Keep integrity validation independent of the new OS prevention boundary.
    monkeypatch.setattr(
        isolation,
        "run",
        lambda argv, **kw: run_command(
            argv,
            cwd=kw["cwd"],
            env=kw["env"],
            timeout=kw["timeout"],
        ),
    )
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


def test_nonzero_collector_profile_mutation_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pluginbench.processes import run_command

    monkeypatch.setattr(
        isolation,
        "run",
        lambda argv, **kw: run_command(
            argv,
            cwd=kw["cwd"],
            env=kw["env"],
            timeout=kw["timeout"],
        ),
    )
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

    def timeout(
        argv: list[str],
        root: Path,
        seconds: int,
        *,
        output: Path | None = None,
        config: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
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

    def timeout(
        argv: list[str],
        root: Path,
        seconds: int,
        *,
        output: Path | None = None,
        config: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, seconds)

    monkeypatch.setattr(collectors, "_execute", timeout)
    with pytest.raises(ValueError, match="trusted coverage adapter failure was not classified"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert not (tmp_path / "evidence/coverage/failure.json").exists()


@pytest.mark.parametrize("outcome", ["complete", "line-only", "failed", "malformed"])
def test_native_lcov_dispatch_preserves_runtime_unknown_and_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from pluginbench import harbor_backend

    snapshot = source(tmp_path)
    identity: dict[str, object] = {"task_dir": str(tmp_path / "native"), "workdir": "/app"}
    collector = Collector(
        kind="coverage",
        config=config(
            tmp_path,
            {
                "native_task_identity": identity,
                "test_scope": ["test/**"],
                "implementation": observer_identity("lcov"),
            },
        ),
        scope=["src/**"],
        command=["npm", "test"],
        report_format="lcov",
        report_path="coverage/lcov.info",
        tool_version="coverage-tool 1",
    )
    original = inspect_snapshot(snapshot)

    def execute(
        *,
        snapshot: Path,
        task_identity: dict[str, object],
        destination: Path,
        command: list[str],
        report_path: str,
        timeout: int,
    ) -> dict[str, object]:
        assert task_identity == identity and command == ["npm", "test"]
        assert report_path == "coverage/lcov.info"
        assert snapshot != tmp_path / "source"  # The archive is never the execution directory.
        report = "SF:/app/src/used.ts\nFN:1,value\nFNDA:1,value\nFNF:1\nFNH:1\nBRF:0\nBRH:0\nend_of_record\n"
        if outcome == "line-only":
            report = "SF:/app/src/used.ts\nDA:1,1\nend_of_record\n"
        elif outcome == "malformed":
            report = report.replace("FNF:1", "FNF:2")
        receipt: dict[str, object] = {
            "exit_code": 1 if outcome == "failed" else 0,
            "timed_out": False,
            "report_collected": outcome != "failed",
        }
        (destination / "execution.json").write_text(json.dumps(receipt))
        if outcome != "failed":
            (destination / "report.raw").write_text(report)
        return receipt

    monkeypatch.setattr(harbor_backend, "run_native_command", execute)
    evidence = tmp_path / "evidence"
    if outcome == "malformed":
        with pytest.raises(ValueError, match="summaries disagree"):
            collect_measurements(snapshot, evidence, [collector])
    else:
        result = collect_measurements(snapshot, evidence, [collector])
        if outcome in {"complete", "line-only"}:
            metrics = evaluate_evidence(snapshot, {"coverage": str(result["coverage"])}, evidence)
            coverage = metrics["coverage"]
            assert isinstance(coverage, dict)
            if outcome == "complete":
                assert coverage["metrics"]["function_total"] == 1
            else:
                assert coverage["metrics"]["line_total"] == 1
                assert coverage["metrics"]["function_total"] is None
                assert coverage["metrics"]["branch_total"] is None
        else:
            assert result == {}
            failure = json.loads((evidence / "coverage/failure.json").read_text())
            assert failure["status"] == "unavailable"
    assert inspect_snapshot(snapshot) == original


def test_native_coverage_command_arguments_are_not_host_script_inputs(tmp_path: Path) -> None:
    script = tmp_path / "project-script.py"
    script.write_text("print('project test')\n")
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {}),
        command=["python", str(script)],
        report_format="lcov",
        report_path="coverage.info",
        tool_version="1",
    )
    frozen = freeze_collectors([collector], tmp_path / "frozen")[0]
    assert frozen.command == collector.command
    assert list((tmp_path / "frozen/0").iterdir()) == [tmp_path / "frozen/0/config.json"]


def test_native_observer_drift_is_rejected_before_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import collectors

    snapshot = source(tmp_path)
    collector = Collector(
        kind="coverage",
        config=config(tmp_path, {"implementation": {"coverage_report.py": "old"}}),
        command=["pytest"],
        report_format="lcov",
        report_path="coverage/lcov.info",
        tool_version="1",
    )
    monkeypatch.setattr(
        collectors, "_collect", lambda *args: pytest.fail("must not execute a changed observer")
    )
    with pytest.raises(ValueError, match="implementation differs"):
        collect_measurements(snapshot, tmp_path / "evidence", [collector])
    assert inspect_snapshot(snapshot)["source_lines"] == 1
