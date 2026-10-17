"""Observer qualification and evidence-boundary regressions, without model calls."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pluginbench.language_analysis import (
    VERSION,
    AnalysisUnavailable,
    RCASpace,
    _complexity,
    _go_unused,
    _path,
    _rca_functions,
    collect_language_analysis,
    prepare_analysis_config,
    prepare_go_observer,
    probe_language_tools,
)

SAMPLES = {
    "python": (
        "sample.py",
        "def plain(value):\n    return value\n\ndef nested(values):\n"
        "    total = 0\n    for value in values:\n        if value > 0:\n"
        "            total += value\n    return total\n\ndef abandoned():\n"
        "    return 99\n\nprint(plain(nested([1])))\n",
    ),
    "go": (
        "sample.go",
        "package sample\nfunc Plain(v int) int { return v }\n"
        "func Nested(values []int) int {\n total := 0\n"
        " for _, value := range values {\n  if value > 0 { total += value }\n"
        " }\n return total\n}\nfunc abandoned() int { return 99 }\n",
    ),
    "rust": (
        "sample.rs",
        "pub fn plain(value: i32) -> i32 { value }\n"
        "fn nested(values: &[i32]) -> i32 {\n let mut total = 0;\n"
        " for value in values {\n  if *value > 0 { total += value; }\n"
        " }\n total\n}\nfn abandoned() -> i32 { 99 }\n"
        'fn main() { println!("{}", plain(nested(&[1]))); }\n',
    ),
}


@pytest.mark.parametrize("language", ["python", "go", "rust"])
def test_pinned_analyzers_match_two_isolated_measurements(tmp_path: Path, language: str) -> None:
    tools = probe_language_tools(language)
    if any(row["status"] != "available" for row in tools.values()):
        pytest.skip("Optional pinned source observers are not installed")
    filename, source = SAMPLES[language]
    snapshot = tmp_path / "source"
    snapshot.mkdir()
    (snapshot / filename).write_text(source)
    if language == "python":
        (snapshot / "__init__.py").write_text("")
    if language == "go":
        (snapshot / "go.mod").write_text("module example.com/sample\n\ngo 1.25.0\n")
    config = prepare_analysis_config(language, ["*"], ["*"], {})
    reports = [
        collect_language_analysis(snapshot, config, tmp_path / f"repeat-{index}")[0]
        for index in (1, 2)
    ]
    assert reports[0] == reports[1]
    report = reports[0]
    assert report["version"] == VERSION
    functions = report["functions"]
    assert isinstance(functions, list)
    nested = next(row for row in functions if row["name"].lower() == "nested")
    assert nested["cognitive"] == nested["cyclomatic"] == 3
    assert nested["end_line"] > nested["line"]
    assert report["analyzed_paths"] == (
        ["__init__.py", filename] if language == "python" else [filename]
    )
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["cognitive"]["status"] == metrics["cyclomatic"]["status"] == "available"
    if language != "rust":
        assert metrics["dead_code"]["status"] == "available"
        candidates = report["unused_candidates"]
        assert isinstance(candidates, list)
        assert any("abandoned" in row["symbol"] for row in candidates)
    else:
        assert metrics["dead_code"]["status"] == "unavailable"
        assert "native" in metrics["dead_code"]["reason"]


def test_rca_nested_space_totals_do_not_double_count_child_functions() -> None:
    def space(
        name: str, cognitive: int, cyclomatic: int, children: list[object]
    ) -> dict[str, object]:
        return {
            "name": name,
            "kind": "function",
            "start_line": 1,
            "end_line": 10,
            "spaces": children,
            "metrics": {
                "cognitive": {"sum": float(cognitive)},
                "cyclomatic": {"sum": float(cyclomatic)},
                "nom": {
                    "functions": float(1 + len(children)),
                    "closures": 0.0,
                    "total": float(1 + len(children)),
                },
            },
        }

    inner = space("inner", 2, 2, [])
    parsed = RCASpace.model_validate(space("outer", 3, 4, [inner]))
    rows = _rca_functions(parsed, "nested.py")
    assert [(row["name"], row["cognitive"], row["cyclomatic"]) for row in rows] == [
        ("outer", 1, 2),
        ("inner", 2, 2),
    ]


def test_missing_rca_json_for_nonempty_python_source_is_not_zero(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("def live():\n    return 1\n")
    tools: dict[str, dict[str, object]] = {"rca": {"path": "/trusted/rca"}}
    with pytest.raises(ValueError, match="Invalid JSON"):
        _complexity(
            tmp_path,
            ["source.py"],
            "python",
            tools,
            lambda argv: "Found nodes: 0" if "-C" in argv else "",
        )


@pytest.mark.parametrize(
    ("source", "names"),
    [
        ("pick = lambda x: x if x else 1\n", []),
        ("pick = lambda x: x\ndef named(x):\n    return x\n", ["named"]),
        ("def named(x):\n    pick = lambda y: y if y else x\n    return pick(x)\n", ["named"]),
    ],
)
def test_python_rca_qualifies_reported_function_spaces(
    tmp_path: Path, source: str, names: list[str]
) -> None:
    if any(row["status"] != "available" for row in probe_language_tools("python").values()):
        pytest.skip("Optional pinned Python observers are not installed")
    snapshot = tmp_path / "source"
    snapshot.mkdir()
    (snapshot / "sample.py").write_text(source)
    config = prepare_analysis_config("python", ["*"], ["*"], {})
    reports = [
        collect_language_analysis(snapshot, config, tmp_path / f"repeat-{index}")[0]
        for index in (1, 2)
    ]
    assert reports[0] == reports[1]
    functions = reports[0]["functions"]
    assert isinstance(functions, list)
    assert [row["name"] for row in functions] == names
    metrics = reports[0]["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["cognitive"]["status"] == "available"
    assert reports[0]["settings"] == config["settings"]
    settings = config["settings"]
    assert isinstance(settings, dict)
    assert "anonymous closures have no separate score" in settings["python_rca_population"]


@pytest.mark.parametrize(
    "nom",
    [
        {"functions": -1.0, "closures": 2.0, "total": 1.0},
        {"functions": 0.5, "closures": 0.5, "total": 1.0},
        {"functions": 0.0, "closures": 1.0, "total": 2.0},
        {"functions": True, "closures": 0.0, "total": 1.0},
    ],
)
def test_rca_invalid_population_counts_are_rejected(nom: dict[str, object]) -> None:
    from pluginbench.language_analysis import RCANom

    with pytest.raises(ValueError):
        RCANom.model_validate(nom)


def test_rca_missing_named_function_span_remains_unavailable(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("def live():\n    return 1\n")
    raw = {
        "name": "source.py",
        "kind": "unit",
        "start_line": 1,
        "end_line": 2,
        "spaces": [],
        "metrics": {
            "cognitive": {"sum": 0.0},
            "cyclomatic": {"sum": 1.0},
            "nom": {"functions": 1.0, "closures": 0.0, "total": 1.0},
        },
    }
    with pytest.raises(AnalysisUnavailable, match="complete declared function spans"):
        _complexity(
            tmp_path,
            ["source.py"],
            "python",
            {"rca": {"path": "/trusted/rca"}},
            lambda argv: "Found nodes: 0" if "-C" in argv else json.dumps(raw),
        )


def test_go_missing_build_population_is_unknown_instead_of_empty() -> None:
    tools: dict[str, dict[str, object]] = {
        "go": {"path": "/trusted/go"},
        "staticcheck": {"path": "/trusted/staticcheck"},
    }
    with pytest.raises(AnalysisUnavailable, match="excluded"):
        _go_unused(
            Path("/source"), ["ignored.go"], tools, lambda argv: '{"Dir":"/source","GoFiles":[]}'
        )


def test_analyzer_location_outside_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sibling = tmp_path / "private.py"
    sibling.write_text("private = 1")
    with pytest.raises(ValueError, match="escapes"):
        _path(str(sibling), source)


def test_frozen_binary_identity_drift_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(
        "pluginbench.language_analysis.probe_language_tools",
        lambda *args: {"rca": {"sha256": "changed"}},
    )
    with pytest.raises(ValueError, match="identity changed"):
        collect_language_analysis(
            source,
            {"language": "rust", "tools": {}, "tool_identities": {"rca": {"sha256": "frozen"}}},
            tmp_path / "evidence",
        )


def test_parser_failure_does_not_hide_healthy_unused_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.py").write_text("def correct():\n    return 1\n")
    monkeypatch.setattr(
        "pluginbench.language_analysis.probe_language_tools",
        lambda *args: {
            "rca": {"path": "/trusted/rca", "status": "available"},
            "vulture": {"path": "/trusted/vulture", "status": "available"},
        },
    )

    def unsupported(*args: object) -> list[dict[str, object]]:
        raise AnalysisUnavailable("Pinned parser does not accept a supported newer construct")

    monkeypatch.setattr("pluginbench.language_analysis._complexity", unsupported)
    monkeypatch.setattr("pluginbench.language_analysis._execute", lambda *args: "")
    report, _ = collect_language_analysis(source, {"language": "python"}, tmp_path / "evidence")
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["cognitive"]["status"] == "unavailable"
    assert metrics["dead_code"]["status"] == "available"


def test_malformed_analyzer_evidence_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.go").write_text("package source\n")
    monkeypatch.setattr(
        "pluginbench.language_analysis.probe_language_tools",
        lambda *args: {
            "go_analysis": {"path": "/trusted/observer", "status": "available"},
            "staticcheck": {"path": None, "status": "unavailable"},
            "go": {"path": None, "status": "unavailable"},
        },
    )
    monkeypatch.setattr(
        "pluginbench.language_analysis._execute",
        lambda *args: json.dumps({"functions": "wrong shape"}),
    )
    with pytest.raises(ValueError, match="object list"):
        collect_language_analysis(source, {"language": "go"}, tmp_path / "evidence")


def test_go_observer_cache_requires_matching_source_and_binary_bytes(tmp_path: Path) -> None:
    if probe_language_tools("go")["go"]["status"] != "available":
        pytest.skip("Pinned Go compiler is not installed")
    binary = prepare_go_observer(tmp_path / "cache")
    assert prepare_go_observer(tmp_path / "cache") == binary
    binary.write_bytes(binary.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="cache bytes changed"):
        prepare_go_observer(tmp_path / "cache")


def test_native_rust_diagnostics_require_complete_compiled_source_population(
    tmp_path: Path,
) -> None:
    from pluginbench.language_analysis import _rust_report

    (tmp_path / "unused.rs").write_text("fn abandoned() {}\n")
    event = {
        "reason": "compiler-message",
        "message": {
            "code": {"code": "dead_code"},
            "message": "function `abandoned` is never used",
            "spans": [
                {"file_name": "unused.rs", "is_primary": True, "line_start": 1, "column_start": 4}
            ],
        },
    }
    finished = {"reason": "build-finished", "success": True}
    prefix = "rustc 1.92.0 (ded5c06cf 2025-12-08)\ncargo 1.92.0 (344c4567c 2025-10-21)\n"
    payload = (
        prefix
        + json.dumps(event)
        + "\n"
        + json.dumps(finished)
        + "\n__PLUGINBENCH_DEPINFO__\ntarget/sample: unused.rs\n"
    )
    rows = _rust_report(payload, tmp_path, ["unused.rs"], "/app")
    assert rows == [
        {
            "path": "unused.rs",
            "line": 1,
            "col": 4,
            "category": "rustc-dead_code",
            "symbol": "abandoned",
            "message": "function `abandoned` is never used",
            "tool": "rustc",
        }
    ]
    with pytest.raises(AnalysisUnavailable, match="dep-info"):
        _rust_report(payload, tmp_path, ["unused.rs", "feature_excluded.rs"], "/app")


def test_analysis_settings_and_implementation_drift_are_fatal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = prepare_analysis_config("rust", ["*"], ["*"], {})
    config["implementation_sha256"] = "tampered"
    with pytest.raises(ValueError, match="implementation changed"):
        collect_language_analysis(source, config, tmp_path / "evidence")
    config = prepare_analysis_config("rust", ["*"], ["*"], {})
    config["settings"] = {"vulture_min_confidence": 100}
    with pytest.raises(ValueError, match="settings differ"):
        collect_language_analysis(source, config, tmp_path / "evidence")


def test_vulture_distribution_bytes_are_frozen_and_required(tmp_path: Path) -> None:
    from pluginbench.language_analysis import _vulture_identity
    import sys

    executable = tmp_path / "bin/vulture"
    executable.parent.mkdir()
    executable.write_text("#!" + sys.executable + "\n")
    with pytest.raises(AnalysisUnavailable, match="distribution files"):
        _vulture_identity(executable)
    module = tmp_path / "lib/python3.14/site-packages/vulture/core.py"
    module.parent.mkdir(parents=True)
    module.write_text("original bytes\n")
    before = _vulture_identity(executable)
    module.write_text("modified bytes\n")
    after = _vulture_identity(executable)
    assert before["assets_sha256"] != after["assets_sha256"]
    assert before["interpreter"] == after["interpreter"]


@pytest.mark.skipif(
    os.environ.get("PLUGINBENCH_NATIVE_ANALYSIS_TESTS") != "1",
    reason="Explicit native Docker observer qualification",
)
def test_native_rust_cargo_cannot_redirect_the_qualified_compiler(tmp_path: Path) -> None:
    from pluginbench.harbor_backend import runtime_identity

    source = tmp_path / "source"
    source.mkdir()
    (source / "Cargo.toml").write_text(
        '[package]\nname="probe"\nversion="0.1.0"\nedition="2021"\n[[bin]]\nname="probe"\npath="sample.rs"\n'
    )
    (source / "Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "probe"\nversion = "0.1.0"\n'
    )
    (source / "sample.rs").write_text("fn abandoned() {}\nfn main() {}\n")
    fake = source / "fake-rustc"
    fake.write_text("#!/bin/sh\necho alternate-compiler-invoked >&2\nexit 99\n")
    fake.chmod(0o755)
    cargo = source / ".cargo"
    cargo.mkdir()
    (cargo / "config.toml").write_text(
        '[build]\nrustc="/app/fake-rustc"\nrustc-wrapper="/app/fake-rustc"\nrustc-workspace-wrapper="/app/fake-rustc"\n[env]\nRUSTC={value="/app/fake-rustc",force=true}\nRUSTC_WRAPPER={value="/app/fake-rustc",force=true}\nRUSTC_WORKSPACE_WRAPPER={value="/app/fake-rustc",force=true}\nCARGO_ENCODED_RUSTFLAGS={value="-Adead_code",force=true}\nRUSTFLAGS={value="-Adead_code",force=true}\n'
    )
    identity = {
        "isolation": runtime_identity(cpus=1, memory_mb=512),
        "image_identity": {
            "image_id": "sha256:f1f73538ebe623fd3673a35aff3df358ae1084c64c55646516e5b17b321b6c9b",
            "platform": "linux/arm64",
            "workdir": "/app",
        },
        "workdir": "/app",
    }
    config = prepare_analysis_config("rust", ["*"], ["*"], identity)
    reports = [
        collect_language_analysis(source, config, tmp_path / f"repeat-{repeat}")[0]
        for repeat in (1, 2)
    ]
    assert reports[0] == reports[1]
    metrics = reports[0]["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["dead_code"]["status"] == "available"
    candidates = reports[0]["unused_candidates"]
    assert isinstance(candidates, list)
    assert [row["symbol"] for row in candidates] == ["abandoned"]
