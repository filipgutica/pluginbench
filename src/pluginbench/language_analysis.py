"""Pinned, isolated source analyzers; unused findings are candidates, not defects."""

from __future__ import annotations

import ast
from copy import deepcopy
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, model_validator

from pluginbench import isolation
from pluginbench.assessment_metrics import inspect_snapshot
from pluginbench.code_diff import _is_test_path

VERSION = "language-analysis-v1"
SUFFIXES = {"python": ".py", "go": ".go", "rust": ".rs"}
PINNED = {
    "rca": ("rust-code-analysis-cli", "--version", "rust-code-analysis-cli 0.0.25"),
    "vulture": ("vulture", "--version", "vulture 2.16"),
    "go_analysis": (
        "pluginbench-go-analysis",
        "--version",
        "go-analysis-v1 gocognit=1.3.0 gocyclo=0.6.0",
    ),
    "staticcheck": ("staticcheck", "-version", "staticcheck 2026.2.2 (0.8.2)"),
    "go": ("go", "version", "go version go1.26.4"),
}

ANALYSIS_SETTINGS: dict[str, object] = {
    "vulture_min_confidence": 60,
    "staticcheck_checks": ["U1000"],
    "staticcheck_tests": False,
    "staticcheck_show_ignored": True,
    "go_build": "host platform; no cgo; no downloads; readonly modules",
    "go_cognitive": "pinned declaration API; direct recursion only; closures represented as declarations",
    "rca_function_scores": "space sums minus immediate child-space sums",
    "python_rca_population": "RCA emitted function spaces; anonymous closures have no separate score, not a known zero; enclosing scores may include closure complexity",
    "rustc": "native exact Rust/Cargo 1.92.0 binaries; CLI-pinned compiler and forced environment; force-warn dead_code; offline locked Cargo; wrappers disabled; compiler dep-info closes source population",
}


def default_tool_paths(language: str) -> dict[str, str]:
    """Use pinned optional observer installations; never install during a measurement."""
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "pluginbench/language-tools"
    )
    candidates = {
        "rca": cache / "rca-0.0.25/bin/rust-code-analysis-cli",
        "vulture": cache / "vulture-2.16/bin/vulture",
        "staticcheck": cache / "go/staticcheck",
    }
    result = {name: str(path) for name, path in candidates.items() if path.is_file()}
    if language == "go":
        source = Path(__file__).parent / "observer_data/go-analysis-v1"
        identity = hashlib.sha256(
            b"".join((source / name).read_bytes() for name in ("go.mod", "go.sum", "main.go"))
        ).hexdigest()
        bridge = cache / "go-observer" / identity / "pluginbench-go-analysis"
        if bridge.is_file():
            result["go_analysis"] = str(bridge)
    return result


def language_capability(language: str) -> dict[str, dict[str, object]]:
    if language not in SUFFIXES:
        return {
            name: {"status": "unavailable", "reason": f"No pinned observer for {language}"}
            for name in ("cognitive", "cyclomatic", "dead_code")
        }
    tools = probe_language_tools(language)
    complexity = tools["go_analysis" if language == "go" else "rca"]
    if language == "go" and complexity["path"] is None and tools["go"]["status"] == "available":
        complexity = {
            **complexity,
            "status": "available",
            "reason": None,
            "requires_preparation": True,
            "version": PINNED["go_analysis"][2],
        }
    unused = tools.get("vulture" if language == "python" else "staticcheck")
    return {
        "cognitive": {**complexity, "collector": "analysis"},
        "cyclomatic": {**complexity, "collector": "analysis"},
        "dead_code": {**unused, "collector": "analysis"}
        if unused
        else {
            "reason": None,
            "status": "available",
            "requires_preparation": True,
            "version": "rustc 1.92.0",
            "collector": "analysis",
        },
    }


def prepare_analysis_config(
    language: str,
    source_scope: list[str],
    test_scope: list[str],
    native_task_identity: dict[str, object],
) -> dict[str, object]:
    tools = default_tool_paths(language)
    if (
        language == "go"
        and "go_analysis" not in tools
        and probe_language_tools(language, tools)["go"]["status"] == "available"
    ):
        cache = (
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "pluginbench/language-tools/go-observer"
        )
        tools["go_analysis"] = str(prepare_go_observer(cache))
    identities = probe_language_tools(language, tools)
    if language == "rust" and native_task_identity:
        image = _object(native_task_identity["image_identity"])
        identities["rustc"] = {
            "status": "available",
            "reason": None,
            "path": "native:rustc",
            "version": "rustc 1.92.0 (ded5c06cf 2025-12-08)",
            "cargo_version": "cargo 1.92.0 (344c4567c 2025-10-21)",
            "image_id": image["image_id"],
        }
    return {
        "language": language,
        "source_scope": source_scope,
        "test_scope": test_scope,
        "native_task_identity": native_task_identity,
        "tools": tools,
        "tool_identities": identities,
        "supported_suffixes": [SUFFIXES[language]],
        "settings": deepcopy(ANALYSIS_SETTINGS),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _interpreter_identity(binary: Path) -> dict[str, object]:
    binary = binary.resolve(strict=True)
    return {
        "path": str(binary),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "version": subprocess.check_output(
            [str(binary), "--version"], text=True, cwd="/", timeout=10
        ).strip(),
    }


def _vulture_identity(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    modules = sorted(path.parent.parent.glob("lib/python*/site-packages/vulture*"))
    if not modules:
        raise AnalysisUnavailable(
            "Install pinned Vulture in an explicit venv; its distribution files could not be frozen"
        )
    for module in modules:
        for asset in sorted(module.rglob("*")):
            if asset.is_file() and "__pycache__" not in asset.parts:
                digest.update(
                    asset.relative_to(module.parent).as_posix().encode()
                    + b"\0"
                    + asset.read_bytes()
                    + b"\0"
                )
    shebang = path.read_text().splitlines()[0]
    if not shebang.startswith("#!/"):
        raise ValueError("Pinned Vulture requires an absolute interpreter shebang")
    return {
        "assets_sha256": digest.hexdigest(),
        "interpreter": _interpreter_identity(Path(shebang[2:])),
    }


def _probe_binary(name: str, path: str | None) -> dict[str, object]:
    _, flag, expected = PINNED[name]
    reason = None
    observed = None
    if not path:
        reason = f"Install the pinned {expected} observer"
    else:
        try:
            observed = subprocess.check_output(
                [str(Path(path).resolve()), flag],
                text=True,
                timeout=10,
                cwd="/",
                stderr=subprocess.STDOUT,
            ).strip()
            if observed != expected and not (name == "go" and observed.startswith(expected + " ")):
                reason = f"Expected {expected}; observed {observed}"
        except (OSError, subprocess.SubprocessError) as exc:
            reason = f"Could not probe {name}: {exc}"
    result: dict[str, object] = {
        "status": "unavailable" if reason else "available",
        "reason": reason,
        "path": str(Path(path).resolve()) if path else None,
        "version": observed,
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if path and not reason
        else None,
    }
    if name == "vulture" and path and not reason:
        try:
            result.update(_vulture_identity(Path(path)))
        except AnalysisUnavailable as exc:
            result.update({"status": "unavailable", "reason": str(exc)})
    return result


def probe_language_tools(
    language: str, tools: dict[str, str] | None = None
) -> dict[str, dict[str, object]]:
    """Probe trusted executables only; never load the measured project's configuration."""
    if language not in SUFFIXES:
        raise ValueError(f"unsupported analysis language: {language}")
    tools = tools or default_tool_paths(language)
    names = ["go_analysis", "staticcheck", "go"] if language == "go" else ["rca"]
    if language == "python":
        names.append("vulture")
    result = {
        name: _probe_binary(name, tools.get(name) or shutil.which(PINNED[name][0]))
        for name in names
    }
    if language == "python":
        result["python_parser"] = {
            "status": "available",
            "reason": None,
            **_interpreter_identity(Path(sys.executable)),
            "version": sys.version,
        }
    return result


def prepare_go_observer(cache: Path) -> Path:
    """Build only shipped observer source, with locked libraries, outside submissions."""
    source = Path(__file__).parent / "observer_data/go-analysis-v1"
    identity = hashlib.sha256(
        b"".join((source / name).read_bytes() for name in ("go.mod", "go.sum", "main.go"))
    ).hexdigest()
    destination = cache.expanduser().resolve() / identity
    binary = destination / "pluginbench-go-analysis"
    if destination.resolve() != destination or any(
        (destination / name).is_symlink()
        for name in ("pluginbench-go-analysis", "receipt.json", "go.mod", "go.sum", "main.go")
    ):
        raise ValueError("Go observer cache cannot redirect outside its owned directory")
    receipt = destination / "receipt.json"
    if binary.is_file():
        if (
            not receipt.is_file()
            or json.loads(receipt.read_text())
            != {
                "source_sha256": identity,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
            or any(
                (destination / name).read_bytes() != (source / name).read_bytes()
                for name in ("go.mod", "go.sum", "main.go")
            )
        ):
            raise ValueError("Go observer cache bytes changed")
        return binary
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("go.mod", "go.sum", "main.go"):
        shutil.copy2(source / name, destination / name)
    subprocess.run(
        ["go", "build", "-mod=readonly", "-trimpath", "-o", str(binary), "."],
        cwd=destination,
        env={**os.environ, "GOTOOLCHAIN": "local", "GOENV": "off", "CGO_ENABLED": "0"},
        check=True,
        timeout=120,
    )
    receipt.write_text(
        json.dumps(
            {
                "source_sha256": identity,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )
    return binary


class AnalysisUnavailable(RuntimeError):
    """An analyzer could not establish its declared source population."""


def _execute(
    argv: list[str], root: Path, destination: Path, readable: list[Path], timeout: int
) -> str:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    go = shutil.which("go")
    if go:
        env["PATH"] = str(Path(go).resolve().parent) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "CGO_ENABLED": "0",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOFLAGS": "-mod=readonly",
            "GOCACHE": str(destination / "go-cache"),
            "GOPATH": str(destination / "go-path"),
        }
    )
    index = len(list(destination.glob("execution-*.json")))
    receipt = destination / f"isolation-{index}.json"
    try:
        execution = isolation.run(
            argv,
            cwd=root,
            env=env,
            timeout=timeout,
            readable=readable,
            writable=[destination],
            receipt=receipt,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        (destination / f"execution-{index}.json").write_text(
            json.dumps({"argv": argv, "exit_code": None, "error": str(exc)}) + "\n"
        )
        raise AnalysisUnavailable(str(exc)) from exc
    (destination / f"execution-{index}.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "exit_code": execution.returncode,
                "stdout": execution.stdout,
                "stderr": execution.stderr,
            }
        )
        + "\n"
    )
    # Vulture returns 3 for findings; Staticcheck uses 1 for diagnostics.
    if execution.returncode not in (0, 1, 3):
        raise AnalysisUnavailable(f"Analyzer exited {execution.returncode}: {execution.stderr}")
    if execution.returncode and not execution.stdout:
        raise AnalysisUnavailable(f"Analyzer failed: {execution.stderr}")
    return execution.stdout


def _path(raw: str, root: Path) -> str:
    path = Path(raw)
    path = path if path.is_absolute() else root / path
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Analyzer location escapes measured source")
    return resolved.relative_to(root.resolve()).as_posix()


def _score(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or int(value) != value
    ):
        raise ValueError("Analyzer score must be a nonnegative integer")
    return int(value)


class RCAMetric(BaseModel):
    model_config = ConfigDict(strict=True)
    sum: float


class RCANom(BaseModel):
    model_config = ConfigDict(strict=True)
    functions: float
    closures: float
    total: float

    @model_validator(mode="after")
    def consistent_counts(self) -> RCANom:
        if _score(self.functions) + _score(self.closures) != _score(self.total):
            raise ValueError("Analyzer function and closure counts must sum to total")
        return self


class RCAMetrics(BaseModel):
    model_config = ConfigDict(strict=True)
    cognitive: RCAMetric
    cyclomatic: RCAMetric
    nom: RCANom


class RCASpace(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str | None
    start_line: int
    end_line: int
    kind: str
    spaces: list[RCASpace]
    metrics: RCAMetrics


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Analyzer evidence must be an object")
    return dict(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Analyzer evidence requires a string")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Analyzer evidence requires a string list")
    return list(value)


def _tool_path(tools: dict[str, dict[str, object]], name: str) -> str:
    return _string(tools[name]["path"])


def _rca_functions(space: RCASpace, path: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if space.kind == "function":
        scores = {
            name: _score(
                getattr(space.metrics, name).sum
                - sum(getattr(child.metrics, name).sum for child in space.spaces)
            )
            for name in ("cognitive", "cyclomatic")
        }
        rows.append(
            {
                "path": path,
                "name": space.name or f"anonymous@{space.start_line}",
                "line": space.start_line,
                "col": None,
                "end_line": space.end_line,
                "line_count": space.end_line - space.start_line + 1,
                **scores,
            }
        )
    for child in space.spaces:
        rows.extend(_rca_functions(child, path))
    return rows


def _complexity(
    root: Path,
    paths: list[str],
    language: str,
    tools: dict[str, dict[str, object]],
    execute: Callable[[list[str]], str],
) -> list[dict[str, object]]:
    if language == "go":
        rows = json.loads(
            execute([_tool_path(tools, "go_analysis"), *[str(root / path) for path in paths]])
        )
        if not isinstance(rows, list):
            raise ValueError("Go observer functions must be an object list")
        functions = [_object(row) for row in rows]
        for row in functions:
            row["path"] = _path(_string(row["path"]), root)
        return functions
    rows = []
    for path in paths:
        if language == "python" and not ast.parse((root / path).read_bytes(), filename=path).body:
            continue
        errors = execute([_tool_path(tools, "rca"), "-C", "ERROR", "-p", str(root / path)])
        found = re.search(r"Found nodes: (\d+)", errors)
        if found is None or int(found[1]):
            raise AnalysisUnavailable(f"Pinned parser does not accept {path}")
        space = RCASpace.model_validate_json(
            execute([_tool_path(tools, "rca"), "-m", "-O", "json", "-p", str(root / path)])
        )
        functions = _rca_functions(space, path)
        expected = _score(
            space.metrics.nom.functions if language == "python" else space.metrics.nom.total
        )
        if len(functions) != expected:
            raise AnalysisUnavailable(
                f"Parser did not expose complete declared function spans for {path}"
            )
        rows.extend(functions)
    return rows


def _python_unused(
    root: Path,
    paths: list[str],
    tools: dict[str, dict[str, object]],
    execute: Callable[[list[str]], str],
) -> list[dict[str, object]]:
    for path in paths:
        ast.parse((root / path).read_bytes(), filename=path)
    output = execute(
        [
            _tool_path(tools, "vulture"),
            "--config",
            "/dev/null",
            "--min-confidence",
            "60",
            *[str(root / path) for path in paths],
        ]
    )
    rows = []
    for line in output.splitlines():
        match = re.fullmatch(r"(.+):(\d+): (.+) \((\d+)% confidence\)", line)
        if match is None:
            raise AnalysisUnavailable("Vulture did not produce a complete candidate stream")
        message = match[3]
        symbol = re.search(r"'([^']+)'", message)
        rows.append(
            {
                "path": _path(match[1], root),
                "line": int(match[2]),
                "category": "vulture-" + message.split(" '", 1)[0].replace(" ", "-"),
                "symbol": symbol[1] if symbol else message,
                "message": message,
                "confidence": int(match[4]),
                "tool": "vulture",
            }
        )
    return rows


def _json_stream(output: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    rows = []
    while output.strip():
        row, end = decoder.raw_decode(output.lstrip())
        rows.append(_object(row))
        output = output.lstrip()[end:]
    return rows


def _go_unused(
    root: Path,
    paths: list[str],
    tools: dict[str, dict[str, object]],
    execute: Callable[[list[str]], str],
) -> list[dict[str, object]]:
    packages = _json_stream(execute([_tool_path(tools, "go"), "list", "-json", "./..."]))
    included = {
        _path(str(Path(_string(package["Dir"])) / path), root)
        for package in packages
        for path in _strings(package.get("GoFiles", []))
    }
    if not set(paths) <= included:
        raise AnalysisUnavailable(
            "Some declared Go source files are excluded by the active build configuration"
        )
    diagnostics = _json_stream(
        execute(
            [
                _tool_path(tools, "staticcheck"),
                "-f",
                "json",
                "-checks=U1000",
                "-show-ignored",
                "-tests=false",
                "./...",
            ]
        )
    )
    rows = []
    for diagnostic in diagnostics:
        if diagnostic.get("code") != "U1000":
            raise AnalysisUnavailable("Staticcheck failed to load/compile the measured packages")
        location = _object(diagnostic["location"])
        path = _path(_string(location["file"]), root)
        if path not in paths:
            continue
        message = _string(diagnostic["message"])
        rows.append(
            {
                "path": path,
                "line": location["line"],
                "col": location["column"],
                "category": "U1000",
                "symbol": message.split(" is unused")[0],
                "message": message,
                "tool": "staticcheck",
                "ignored": diagnostic.get("ignored", False),
            }
        )
    return rows


RUST_COMMAND = [
    "bash",
    "-c",
    """set -eu
mkdir -p .pluginbench-measure
export RUSTUP_TOOLCHAIN=1.92.0
rust_observer_compiler=$(rustup which --toolchain 1.92.0 rustc)
rust_observer_cargo=$(rustup which --toolchain 1.92.0 cargo)
"$rust_observer_compiler" --version > .pluginbench-measure/rust-unused.txt
"$rust_observer_cargo" --version >> .pluginbench-measure/rust-unused.txt
export RUSTC="$rust_observer_compiler" RUSTC_WRAPPER= RUSTC_WORKSPACE_WRAPPER=
export CARGO_ENCODED_RUSTFLAGS=--force-warn=dead_code RUSTFLAGS=--force-warn=dead_code
cat > /tmp/pluginbench-rust-observer.toml <<TOML
[build]
rustc = "$rust_observer_compiler"
rustc-wrapper = ""
rustc-workspace-wrapper = ""
[env]
RUSTC = { value = "$rust_observer_compiler", force = true }
RUSTC_WRAPPER = { value = "", force = true }
RUSTC_WORKSPACE_WRAPPER = { value = "", force = true }
CARGO_ENCODED_RUSTFLAGS = { value = "--force-warn=dead_code", force = true }
RUSTFLAGS = { value = "--force-warn=dead_code", force = true }
LD_PRELOAD = { value = "", force = true }
LD_LIBRARY_PATH = { value = "", force = true }
TOML
"$rust_observer_cargo" --config /tmp/pluginbench-rust-observer.toml check --offline --locked \
    --target-dir /tmp/pluginbench-rust-target --message-format=json \
    >> .pluginbench-measure/rust-unused.txt
printf '\n__PLUGINBENCH_DEPINFO__\n' >> .pluginbench-measure/rust-unused.txt
find /tmp/pluginbench-rust-target -name '*.d' -type f -exec cat -- {} + \
    >> .pluginbench-measure/rust-unused.txt
""",
]


def _rust_unused(
    root: Path, paths: list[str], config: dict[str, object], destination: Path, timeout: int
) -> list[dict[str, object]]:
    from pluginbench.harbor_backend import run_native_command

    identity_value = config.get("native_task_identity")
    if not identity_value:
        raise AnalysisUnavailable("Rust unused diagnostics require a frozen native Cargo observer")
    identity = _object(identity_value)
    if not (root / "Cargo.toml").is_file() or not (root / "Cargo.lock").is_file():
        raise AnalysisUnavailable(
            "Rust unused diagnostics require a Cargo manifest and frozen lockfile"
        )
    execution = run_native_command(
        snapshot=root,
        task_identity=identity,
        destination=destination,
        command=RUST_COMMAND,
        report_path=".pluginbench-measure/rust-unused.txt",
        timeout=timeout,
    )
    if (
        execution.get("exit_code") != 0
        or execution.get("timed_out")
        or not execution.get("report_collected")
    ):
        raise AnalysisUnavailable("Native pinned Cargo check failed; inspect its execution receipt")
    return _rust_report(
        (destination / "report.raw").read_text(), root, paths, _string(identity["workdir"])
    )


def _rust_report(raw: str, root: Path, paths: list[str], workdir: str) -> list[dict[str, object]]:
    lines = raw.splitlines()
    if len(lines) < 3 or lines[:2] != [
        "rustc 1.92.0 (ded5c06cf 2025-12-08)",
        "cargo 1.92.0 (344c4567c 2025-10-21)",
    ]:
        raise AnalysisUnavailable(
            "Native Rust compiler version is not the qualified pinned toolchain"
        )
    payload = "\n".join(lines[2:])
    diagnostics_text, marker, depinfo = payload.partition("__PLUGINBENCH_DEPINFO__")
    if not marker:
        raise ValueError("Native Rust diagnostics did not include source population evidence")
    workdir = workdir.rstrip("/")
    compiled = set()
    for line in depinfo.splitlines():
        if ": " not in line:
            continue
        for filename in shlex.split(line.split(": ", 1)[1]):
            relative = filename.removeprefix(workdir + "/")
            if relative in paths:
                compiled.add(relative)
    if not set(paths) <= compiled:
        raise AnalysisUnavailable(
            "Some declared Rust source files are outside compiler dep-info for this build configuration"
        )
    return _rust_candidates(diagnostics_text, workdir, root, paths)


def _rust_candidates(
    diagnostics_text: str, workdir: str, root: Path, paths: list[str]
) -> list[dict[str, object]]:
    rows = []
    finished = False
    for event in _json_stream(diagnostics_text):
        if event.get("reason") == "build-finished":
            finished = event.get("success") is True
        if event.get("reason") != "compiler-message":
            continue
        message = _object(event["message"])
        code = message.get("code")
        if code is not None and _object(code).get("code") == "dead_code":
            rows.extend(_rust_message_candidates(message, workdir, root, paths))
    if not finished:
        raise AnalysisUnavailable("Native Cargo did not attest a successful complete build")
    unique = {(row["path"], row["line"], row["col"], row["symbol"]): row for row in rows}
    return list(unique.values())


def _rust_message_candidates(
    message: dict[str, object], workdir: str, root: Path, paths: list[str]
) -> list[dict[str, object]]:
    rows = []
    spans = message.get("spans")
    if not isinstance(spans, list):
        raise ValueError("Rust diagnostics require source spans")
    for value in spans:
        span = _object(value)
        if span.get("is_primary") is not True:
            continue
        filename = _string(span["file_name"])
        if filename.startswith("/") and not filename.startswith(workdir + "/"):
            continue
        path = _path(filename.removeprefix(workdir + "/"), root)
        if path not in paths:
            continue
        text = _string(message["message"])
        symbol = re.search(r"`([^`]+)`", text)
        rows.append(
            {
                "path": path,
                "line": _score(span["line_start"]),
                "col": _score(span["column_start"]),
                "category": "rustc-dead_code",
                "symbol": symbol[1] if symbol else text,
                "message": text,
                "tool": "rustc",
            }
        )
    return rows


def _collect_language_analysis(
    root: Path,
    config: dict[str, object],
    destination: Path,
    tools: dict[str, dict[str, object]],
    *,
    timeout: int = 120,
) -> tuple[dict[str, object], str]:
    """Analyze a disposable snapshot; callers retain/bind its immutable source receipt."""
    root = root.resolve(strict=True)
    destination = destination.resolve()
    language = _string(config["language"])
    suffix = SUFFIXES[language]
    inventory = inspect_snapshot(root)
    files = _object(inventory["files"])
    scope = _strings(config.get("source_scope", ["*"]))
    paths = sorted(
        path
        for path in files
        if Path(path).suffix == suffix
        and not _is_test_path(path)
        and any(fnmatch.fnmatchcase(path, pattern) for pattern in scope)
    )
    destination.mkdir(parents=True, exist_ok=True)
    readable = [
        Path(_string(row["path"])).parent
        for row in tools.values()
        if row["path"] and not _string(row["path"]).startswith("native:")
    ]
    if "vulture" in tools and tools["vulture"]["path"]:
        readable.append(Path(_tool_path(tools, "vulture")).parent.parent)
    go = shutil.which("go")
    if language == "go" and go:
        readable.append(Path(go).resolve().parent.parent)

    def execute(argv: list[str]) -> str:
        return _execute(argv, root, destination, readable, timeout)

    functions: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    complexity_tool = tools["go_analysis" if language == "go" else "rca"]
    try:
        if complexity_tool["status"] != "available":
            raise AnalysisUnavailable(complexity_tool["reason"])
        functions = _complexity(root, paths, language, tools, execute) if paths else []
        complexity_status = {"status": "available", "reason": None}
    except (AnalysisUnavailable, SyntaxError) as exc:
        complexity_status = {"status": "unavailable", "reason": str(exc)}
    try:
        if language == "rust":
            candidates = _rust_unused(root, paths, config, destination / "native-rust", timeout)
        else:
            tool = tools["vulture" if language == "python" else "staticcheck"]
            if tool["status"] != "available":
                raise AnalysisUnavailable(tool["reason"])
            if language == "go" and tools["go"]["status"] != "available":
                raise AnalysisUnavailable(tools["go"]["reason"])
            candidates = (
                (_python_unused if language == "python" else _go_unused)(
                    root, paths, tools, execute
                )
                if paths
                else []
            )
        unused_status = {"status": "available", "reason": None}
    except (AnalysisUnavailable, SyntaxError) as exc:
        unused_status = {"status": "unavailable", "reason": str(exc)}
    functions.sort(
        key=lambda row: (
            _string(row["path"]),
            _score(row["line"]),
            _score(row["col"] or 0),
            _string(row["name"]),
        )
    )
    candidates.sort(
        key=lambda row: (
            _string(row["path"]),
            _score(row["line"]),
            _string(row["category"]),
            _string(row["symbol"]),
        )
    )
    return {
        "version": VERSION,
        "language": language,
        "tools": tools,
        "supported_suffixes": [suffix],
        "analyzed_paths": paths,
        "functions": functions,
        "unused_candidates": candidates,
        "metrics": {
            "cognitive": complexity_status,
            "cyclomatic": complexity_status,
            "dead_code": unused_status,
        },
        "settings": deepcopy(ANALYSIS_SETTINGS),
    }, VERSION


def _analysis_tools(language: str, config: dict[str, object]) -> dict[str, dict[str, object]]:
    implementation = config.get("implementation_sha256")
    if (
        implementation is not None
        and implementation != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ValueError("Analysis observer implementation changed after freezing")
    if config.get("settings", ANALYSIS_SETTINGS) != ANALYSIS_SETTINGS:
        raise ValueError("Analysis settings differ from the qualified observer")
    if config.get("supported_suffixes", [SUFFIXES[language]]) != [SUFFIXES[language]]:
        raise ValueError("Analysis source language population changed after freezing")
    configured = config.get("tools")
    tool_paths = (
        {name: _string(path) for name, path in _object(configured).items()}
        if configured is not None
        else None
    )
    tools = probe_language_tools(language, tool_paths)
    frozen_tools = config.get("tool_identities")
    if language == "rust" and isinstance(frozen_tools, dict) and "rustc" in frozen_tools:
        tools["rustc"] = _object(frozen_tools["rustc"])
    if frozen_tools is not None and frozen_tools != tools:
        raise ValueError("Analysis tool identity changed after freezing")
    return tools


def collect_language_analysis(
    root: Path, config: dict[str, object], destination: Path, *, timeout: int = 120
) -> tuple[dict[str, object], str]:
    language = _string(config["language"])
    tools = _analysis_tools(language, config)
    before = inspect_snapshot(root)["digest"]
    try:
        return _collect_language_analysis(root, config, destination, tools, timeout=timeout)
    finally:
        if _analysis_tools(language, config) != tools:
            raise ValueError("Analysis observer bytes changed during execution")
        if inspect_snapshot(root)["digest"] != before:
            raise ValueError("Analysis modified measured source")
