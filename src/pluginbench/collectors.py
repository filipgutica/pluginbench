"""Execute declared measurements on disposable copies and bind their receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench.assessment_metrics import (
    EvidenceBinding,
    bind_evidence,
    evaluate_evidence,
    inspect_snapshot,
)
from pluginbench import isolation


class Collector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fallow", "analysis", "coverage"]
    config: Path
    scope: list[str] = Field(default_factory=lambda: ["src/**"], min_length=1)
    command: list[str] | None = None
    tool_version: str | None = None
    syntax_command: list[str] | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=7200)
    failure_protocol: Literal["coverage-exit-v1"] | None = None
    report_format: (
        Literal["lcov", "coverage-py-json", "go-coverprofile", "pytest-contribution"] | None
    ) = None
    report_path: str | None = None

    @model_validator(mode="after")
    def supported_command(self) -> Collector:
        if (
            self.kind == "coverage"
            and self.command is None
            and self.report_format != "pytest-contribution"
        ):
            raise ValueError("coverage requires an adapter command")
        if self.kind in {"fallow", "analysis"} and self.command is not None:
            raise ValueError("Source analysis uses the built-in collector, not a custom command")
        if self.syntax_command is not None and (self.kind != "fallow" or not self.syntax_command):
            raise ValueError("syntax_command is an optional Fallow parser validator")
        if self.command is not None and (not self.command or not self.tool_version):
            raise ValueError("custom collectors require command and tool_version")
        if self.failure_protocol is not None and (self.kind != "coverage" or self.command is None):
            raise ValueError("coverage failure protocol requires a custom coverage adapter")
        if self.report_format is not None:
            from pathlib import PurePosixPath

            report = PurePosixPath(self.report_path or ".")
            if (
                self.kind != "coverage"
                or self.failure_protocol is not None
                or report.is_absolute()
                or ".." in report.parts
                or report.as_posix() == "."
                or "\\" in (self.report_path or "")
            ):
                raise ValueError("native coverage requires a safe relative report path")
        elif self.report_path is not None:
            raise ValueError("report_path requires an explicit report format")
        return self


def _json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def resolve_collector_command(base: Path, command: list[str]) -> list[str]:
    """Resolve local arguments without dereferencing a venv executable symlink."""
    resolved = []
    for index, argument in enumerate(command):
        expanded = Path(argument).expanduser()
        candidate = expanded if expanded.is_absolute() else base / expanded
        if index == 0:
            explicit_path = "/" in argument or argument.startswith("~")
            resolved.append(
                str(candidate.absolute()) if explicit_path and candidate.is_file() else argument
            )
        else:
            resolved.append(str(candidate.resolve()) if candidate.is_file() else argument)
    return resolved


def freeze_collectors(collectors: list[Collector], frozen_dir: Path) -> list[Collector]:
    result = []
    for index, collector in enumerate(collectors):
        destination = frozen_dir / str(index)
        destination.mkdir(parents=True)
        config = json.loads(collector.config.read_text())
        if not isinstance(config, dict) or config.get("extends"):
            raise ValueError("collector config must be resolved JSON without extends")
        shutil.copy2(collector.config, destination / "config.json")
        commands = {}
        for field in ("command", "syntax_command"):
            original = getattr(collector, field)
            command = list(original) if original else None
            if command and not (field == "command" and collector.report_format is not None):
                for position, argument in enumerate(command[1:], 1):
                    source = Path(argument)
                    if source.is_file():
                        if source.is_symlink():
                            raise ValueError("collector script cannot be a symlink")
                        target = destination / f"{field}-{position}{source.suffix}"
                        shutil.copy2(source, target)
                        command[position] = str(target.resolve())
            commands[field] = command
        result.append(
            collector.model_copy(update={"config": destination / "config.json", **commands})
        )
    return result


def _outside_job(path: Path, root: Path) -> bool:
    """Only canonical frozen paths can declare reads outside a submitted job."""
    try:
        canonical = path.resolve(strict=True)
        return path.absolute() == canonical and not any(
            canonical.is_relative_to(job) for job in (root.absolute(), root.resolve(strict=True))
        )
    except (OSError, RuntimeError):
        return False


def _execute(
    argv: list[str],
    root: Path,
    timeout: int,
    *,
    output: Path | None = None,
    config: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    readable: list[Path] = []
    if config is not None and config.is_file() and _outside_job(config, root):
        readable.extend(isolation.referenced_paths(json.loads(config.read_text())))
    for argument in argv[1:]:
        script = Path(argument)
        if (
            script.is_absolute()
            and script.suffix == ".py"
            and script.is_file()
            and _outside_job(script, root)
        ):
            try:
                readable.extend(isolation.verifier_paths(script))
            except (OSError, ValueError):
                # Dynamic PAYLOAD declarations grant nothing; execution still records their result.
                pass
    return isolation.run(
        argv,
        cwd=root,
        env=env,
        timeout=timeout,
        readable=[path for path in readable if _outside_job(path, root)],
        writable=[output] if output else [],
        receipt=output / "runtime-isolation.json" if output else None,
    )


class CollectorUnavailable(RuntimeError):
    """A collector could not execute; its output is not a measurement."""


def observer_identity(format: str) -> dict[str, str]:
    names = ["coverage_report.py"]
    if format == "pytest-contribution":
        names.append("test_contribution.py")
    return {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in names
    }


def _assert_observer(collector: Collector) -> None:
    assert collector.report_format is not None
    expected = json.loads(collector.config.read_text()).get("implementation")
    if expected != observer_identity(collector.report_format):
        raise ValueError("coverage observer implementation differs from its frozen profile")


def collector_identity(collector: Collector) -> dict[str, object]:
    """Bind failures to the same frozen config, scope and adapter as successes."""
    return {
        "kind": collector.kind,
        "config_digest": "sha256:" + hashlib.sha256(collector.config.read_bytes()).hexdigest(),
        "scope": collector.scope,
        "tool_version": collector.tool_version,
        "command": collector.command,
        "syntax_command": collector.syntax_command,
        "timeout_seconds": collector.timeout_seconds,
        "failure_protocol": collector.failure_protocol,
        "report_format": collector.report_format,
        "report_path": collector.report_path,
        "scripts": {
            argument: "sha256:" + hashlib.sha256(Path(argument).read_bytes()).hexdigest()
            for command in (
                collector.command if collector.report_format is None else None,
                collector.syntax_command,
            )
            for argument in (command or [])[1:]
            if Path(argument).is_file()
        },
    }


def _recorded_execute(
    argv: list[str],
    root: Path,
    timeout: int,
    destination: Path,
    receipts: list[dict[str, object]],
    *,
    config: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    filesystem_receipt = destination / "runtime-isolation.json"
    try:
        result = _execute(argv, root, timeout, output=destination, config=config)
    except (OSError, subprocess.TimeoutExpired) as exc:
        receipts.append(
            {
                "command": argv,
                "exit_code": None,
                "error": str(exc),
                "stdout": str(getattr(exc, "stdout", "") or ""),
                "stderr": str(getattr(exc, "stderr", "") or ""),
                "filesystem": json.loads(filesystem_receipt.read_text())
                if filesystem_receipt.is_file()
                else None,
            }
        )
        _json(destination / "execution.json", receipts)
        raise CollectorUnavailable(str(exc)) from exc
    receipts.append(
        {
            "command": argv,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "filesystem": json.loads(filesystem_receipt.read_text())
            if filesystem_receipt.is_file()
            else None,
        }
    )
    _json(destination / "execution.json", receipts)
    return result


def _fallow(root: Path, collector: Collector, destination: Path) -> tuple[dict[str, object], str]:
    argv = [
        "fallow",
        "--root",
        str(root),
        "--config",
        str(collector.config.resolve()),
        "--format",
        "json",
        "--quiet",
        "--explain",
        "--no-cache",
    ]
    receipts: list[dict[str, object]] = []
    outputs = []
    for suffix in ([], ["list", "--files"]):
        execution = _recorded_execute(
            argv + suffix,
            root,
            collector.timeout_seconds,
            destination,
            receipts,
            config=collector.config,
        )
        if execution.returncode not in (0, 1):
            raise CollectorUnavailable(f"Fallow exited {execution.returncode}: {execution.stderr}")
        outputs.append(json.loads(execution.stdout))
    report, discovery = outputs
    if (
        not isinstance(report, dict)
        or report.get("kind") != "combined"
        or not isinstance(discovery, dict)
    ):
        raise ValueError("unexpected Fallow output")
    report["discovery"] = discovery
    if collector.syntax_command:
        inputs = destination / "syntax-inputs.json"
        output = destination / "syntax-validation.json"
        _json(inputs, discovery)
        syntax_argv = [
            argument.replace("{snapshot}", str(root))
            .replace("{inputs}", str(inputs.resolve()))
            .replace("{output}", str(output.resolve()))
            for argument in collector.syntax_command
        ]
        execution = _recorded_execute(
            syntax_argv,
            root,
            collector.timeout_seconds,
            destination,
            receipts,
            config=collector.config,
        )
        receipt = receipts[-1]
        if execution.returncode:
            raise CollectorUnavailable(
                f"syntax validator exited {execution.returncode}: {execution.stderr}"
            )
        validation = json.loads(output.read_text())
        if not isinstance(validation, dict):
            raise ValueError("syntax validation must be an object")
        from hashlib import sha256

        validation["config_digest"] = "sha256:" + sha256(collector.config.read_bytes()).hexdigest()
        validation["producer_version"] = report.get("version")
        validation["producer_execution"] = receipts[:2]
        validation["validator_execution"] = receipt
        validation["validator_files"] = {
            str(index): "sha256:" + sha256(Path(argument).read_bytes()).hexdigest()
            for index, argument in enumerate(collector.syntax_command[1:], 1)
            if Path(argument).is_file()
        }
        report["syntax_validation"] = validation
    return report, str(report["version"])


def _native_coverage(
    root: Path, collector: Collector, destination: Path
) -> tuple[dict[str, object], str]:
    from pluginbench.coverage_report import CoveragePopulationUnavailable, parse_coverage
    from pluginbench.harbor_backend import run_native_command

    config = json.loads(collector.config.read_text())
    identity = config.get("native_task_identity")
    if not isinstance(identity, dict):
        raise ValueError("native coverage requires a frozen task identity")
    assert collector.command and collector.report_path and collector.tool_version
    assert collector.report_format is not None
    execution = run_native_command(
        snapshot=root,
        task_identity=identity,
        destination=destination,
        command=collector.command,
        report_path=collector.report_path,
        timeout=collector.timeout_seconds,
    )
    if execution.get("timed_out"):
        raise CollectorUnavailable("coverage command failed or timed out; see execution.json")
    if not execution.get("report_collected") or not (destination / "report.raw").is_file():
        status = execution.get("exit_code")
        reason = (
            "coverage tool/command unavailable"
            if status == 127
            else f"test command exited {status}; no usable coverage report produced"
        )
        raise CollectorUnavailable(reason + "; see execution.json")
    workdir = identity.get("workdir")
    test_scope = config.get("test_scope")
    if (
        not isinstance(workdir, str)
        or not isinstance(test_scope, list)
        or not all(isinstance(pattern, str) for pattern in test_scope)
    ):
        raise ValueError("native coverage identity requires workdir and explicit test_scope")
    try:
        report = parse_coverage(
            collector.report_format,
            destination / "report.raw",
            snapshot=root,
            source_scope=collector.scope,
            test_scope=test_scope,
            workdir=workdir,
        )
    except CoveragePopulationUnavailable as exc:
        raise CollectorUnavailable(str(exc)) from exc
    if (
        collector.report_format == "coverage-py-json"
        and report.get("tool_version") != collector.tool_version
    ):
        raise ValueError("coverage.py output version differs from frozen collector")
    report["test_command_exit_code"] = execution.get("exit_code")
    return report, collector.tool_version


def _run_collector(
    root: Path, collector: Collector, destination: Path, baseline: Path | None
) -> tuple[dict[str, object], str, str]:
    if collector.report_format is None:
        return _collect(root, collector, destination, baseline)
    _assert_observer(collector)
    try:
        return _collect(root, collector, destination, baseline)
    finally:
        _assert_observer(collector)


def _collect(
    root: Path, collector: Collector, destination: Path, baseline: Path | None
) -> tuple[dict[str, object], str, str]:
    if collector.kind == "fallow":
        report, version = _fallow(root, collector, destination)
        return report, version, "fallow"
    if collector.kind == "analysis":
        from pluginbench.language_analysis import collect_language_analysis

        report, version = collect_language_analysis(
            root,
            json.loads(collector.config.read_text()),
            destination,
            timeout=collector.timeout_seconds,
        )
        return report, version, "language-analysis"
    if collector.report_format == "pytest-contribution":
        from pluginbench.test_contribution import collect_pytest_contribution

        config = json.loads(collector.config.read_text())
        if baseline is None:
            raise ValueError("test contribution requires a bound before snapshot")
        report = collect_pytest_contribution(
            snapshot=root,
            before_snapshot=baseline,
            task_identity=config["native_task_identity"],
            destination=destination,
            source_scope=collector.scope,
            test_scope=config["test_scope"],
            **config["test_contribution"],
        )
        return report, str(collector.tool_version), "pytest-coverage"
    if collector.report_format is not None:
        report, version = _native_coverage(root, collector, destination)
        return report, version, str(collector.report_format)
    assert collector.command and collector.tool_version
    output = destination / "report.json"
    argv = [
        argument.replace("{snapshot}", str(root))
        .replace("{output}", str(output.resolve()))
        .replace("{before}", str(baseline) if baseline is not None else "")
        for argument in collector.command
    ]
    receipts: list[dict[str, object]] = []
    try:
        execution = _recorded_execute(
            argv,
            root,
            collector.timeout_seconds,
            destination,
            receipts,
            config=collector.config,
        )
    except CollectorUnavailable as exc:
        if collector.failure_protocol is not None:
            raise ValueError("trusted coverage adapter failure was not classified") from exc
        raise
    if execution.returncode:
        if collector.failure_protocol is not None and execution.returncode != 3:
            raise ValueError(
                f"trusted coverage adapter rejected evidence (exit {execution.returncode}): {execution.stderr}"
            )
        raise CollectorUnavailable(f"collector exited {execution.returncode}: {execution.stderr}")
    report = json.loads(output.read_text())
    return report, collector.tool_version, collector.command[0]


def collect_measurements(
    snapshot: Path, evidence_dir: Path, collectors: list[Collector], *, before: Path | None = None
) -> dict[str, Path]:
    results: dict[str, Path] = {}
    for collector in collectors:
        destination = evidence_dir / collector.kind
        destination.mkdir(parents=True, exist_ok=False)
        identity = collector_identity(collector)
        source_digest = inspect_snapshot(snapshot)["digest"]
        before_digest = inspect_snapshot(before)["digest"] if before is not None else None
        try:
            with tempfile.TemporaryDirectory(prefix="pluginbench-measure-") as temporary:
                root = (Path(temporary) / "source").resolve()
                shutil.copytree(snapshot, root)
                baseline = None
                if (
                    collector.report_format is None
                    and collector.command
                    and any("{before}" in item for item in collector.command)
                ) or collector.report_format == "pytest-contribution":
                    if before is None:
                        raise ValueError("collector command requires a bound before snapshot")
                    baseline = Path(temporary) / "before"
                    shutil.copytree(before, baseline)
                try:
                    report, version, tool = _run_collector(root, collector, destination, baseline)
                finally:
                    # Failed commands are not exempt from source/profile integrity checks.
                    if inspect_snapshot(root)["digest"] != source_digest:
                        raise ValueError("collector modified measured source")
                    if (
                        baseline is not None
                        and inspect_snapshot(baseline)["digest"] != before_digest
                    ):
                        raise ValueError("collector modified its before snapshot")
                    if inspect_snapshot(snapshot)["digest"] != source_digest:
                        raise ValueError("collector modified archived measured source")
                    if before is not None and inspect_snapshot(before)["digest"] != before_digest:
                        raise ValueError("collector modified archived before snapshot")
                    if collector_identity(collector) != identity:
                        raise ValueError("collector modified its frozen profile")
            _json(destination / "report.json", report)
            shutil.copy2(collector.config, destination / "config.json")
            envelope = bind_evidence(
                EvidenceBinding(
                    snapshot=snapshot,
                    report=destination / "report.json",
                    config=destination / "config.json",
                    tool=tool,
                    tool_version=version,
                    scope=tuple(collector.scope),
                )
            )
            envelope["config"], envelope["report"] = "config.json", "report.json"
            _json(destination / "envelope.json", envelope)
            evaluate_evidence(
                snapshot,
                {collector.kind: str((destination / "envelope.json").resolve())},
                evidence_dir.resolve(),
            )
            results[collector.kind] = destination / "envelope.json"
        except CollectorUnavailable as exc:
            if not (destination / "execution.json").exists():
                _json(destination / "execution.json", [])
            _json(
                destination / "failure.json",
                {
                    "schema_version": 1,
                    "status": "unavailable",
                    "reason": str(exc),
                    "snapshot_digest": source_digest,
                    "before_digest": before_digest,
                    "collector": identity,
                    "execution": {
                        "path": "execution.json",
                        "sha256": "sha256:"
                        + hashlib.sha256((destination / "execution.json").read_bytes()).hexdigest(),
                    },
                },
            )
    return results
