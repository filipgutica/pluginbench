"""Execute declared measurements on disposable copies and bind their receipts."""

from __future__ import annotations

import fnmatch
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


class Collector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fallow", "coverage", "mutation"]
    config: Path
    scope: list[str] = Field(default_factory=lambda: ["src/**"], min_length=1)
    command: list[str] | None = None
    tool_version: str | None = None
    syntax_command: list[str] | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=7200)
    failure_protocol: Literal["coverage-exit-v1"] | None = None

    @model_validator(mode="after")
    def supported_command(self) -> Collector:
        if self.kind == "fallow" and self.command is not None:
            raise ValueError("Fallow uses the built-in collector, not a custom command")
        if self.syntax_command is not None and (self.kind != "fallow" or not self.syntax_command):
            raise ValueError("syntax_command is an optional Fallow parser validator")
        if self.kind == "mutation" and self.command is None:
            raise ValueError("mutation requires a command producing normalized JSON")
        if self.command is not None and (not self.command or not self.tool_version):
            raise ValueError("custom collectors require command and tool_version")
        if self.failure_protocol is not None and (self.kind != "coverage" or self.command is None):
            raise ValueError("coverage failure protocol requires a custom coverage adapter")
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
            if command:
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


def _execute(argv: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        argv, cwd=root, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


class CollectorUnavailable(RuntimeError):
    """A collector could not execute; its output is not a measurement."""


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
        "scripts": {
            argument: "sha256:" + hashlib.sha256(Path(argument).read_bytes()).hexdigest()
            for command in (collector.command, collector.syntax_command)
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
) -> subprocess.CompletedProcess[str]:
    try:
        result = _execute(argv, root, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        receipts.append(
            {
                "command": argv,
                "exit_code": None,
                "error": str(exc),
                "stdout": str(getattr(exc, "stdout", "") or ""),
                "stderr": str(getattr(exc, "stderr", "") or ""),
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
        }
    )
    _json(destination / "execution.json", receipts)
    return result


def _fallow(
    root: Path, collector: Collector, destination: Path
) -> tuple[dict[str, object], str, list[dict[str, object]]]:
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
            argv + suffix, root, collector.timeout_seconds, destination, receipts
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
            syntax_argv, root, collector.timeout_seconds, destination, receipts
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
    _json(destination / "execution.json", receipts)
    return report, str(report["version"]), receipts


_NODE_REPORTER = """export default async function* (source) {
  for await (const event of source) {
    if (event.type === 'test:coverage') yield JSON.stringify(event.data.summary);
  }
}
"""


def _node_coverage(
    root: Path, collector: Collector, destination: Path
) -> tuple[dict[str, object], str, list[dict[str, object]]]:
    config = json.loads(collector.config.read_text())
    patterns = config.get("test_globs", ["test/*.test.ts"])
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise ValueError("coverage test_globs must be a list of paths")
    tests = sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})
    if not tests:
        raise CollectorUnavailable("coverage found no tests")
    reporter = destination / "node-reporter.mjs"
    reporter.write_text(_NODE_REPORTER)
    node = str(config.get("node", "node"))
    receipts: list[dict[str, object]] = []
    version = _recorded_execute([node, "--version"], root, 10, destination, receipts)
    if version.returncode:
        raise CollectorUnavailable("Node version unavailable")
    argv = [
        node,
        "--experimental-strip-types",
        "--test",
        "--experimental-test-coverage",
        f"--test-reporter={reporter.resolve()}",
        *map(str, tests),
    ]
    execution = _recorded_execute(argv, root, collector.timeout_seconds, destination, receipts)
    (destination / "coverage-stdout.json").write_text(execution.stdout)
    if execution.returncode:
        raise CollectorUnavailable(f"coverage tests failed: {execution.stderr}")
    summary = json.loads(execution.stdout)
    measured = sorted(
        Path(file["path"]).relative_to(root).as_posix()
        for file in summary["files"]
        if any(
            fnmatch.fnmatchcase(Path(file["path"]).relative_to(root).as_posix(), pattern)
            for pattern in collector.scope
        )
    )
    report: dict[str, object] = {"files_measured": measured}
    for category in ("branches", "functions"):
        total, covered = [], []
        for file in summary["files"]:
            path = Path(file["path"]).relative_to(root).as_posix()
            if not any(fnmatch.fnmatchcase(path, pattern) for pattern in collector.scope):
                continue
            for index, item in enumerate(file[category]):
                coordinate = f"{path}:{item['line']}:{index}"
                total.append(coordinate)
                if item["count"] > 0:
                    covered.append(coordinate)
        report[category] = {"total": total, "covered": covered}
    return report, version.stdout.strip(), receipts


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
                if collector.command and any("{before}" in item for item in collector.command):
                    if before is None:
                        raise ValueError("collector command requires a bound before snapshot")
                    baseline = Path(temporary) / "before"
                    shutil.copytree(before, baseline)
                try:
                    if collector.kind == "fallow":
                        report, version, receipts = _fallow(root, collector, destination)
                        tool = "fallow"
                    elif collector.kind == "coverage" and collector.command is None:
                        report, version, receipts = _node_coverage(root, collector, destination)
                        tool = "node-test-coverage"
                    else:
                        assert collector.command and collector.tool_version
                        output = destination / "report.json"
                        argv = [
                            argument.replace("{snapshot}", str(root))
                            .replace("{output}", str(output.resolve()))
                            .replace("{before}", str(baseline) if baseline is not None else "")
                            for argument in collector.command
                        ]
                        receipts = []
                        try:
                            execution = _recorded_execute(
                                argv, root, collector.timeout_seconds, destination, receipts
                            )
                        except CollectorUnavailable as exc:
                            if collector.failure_protocol is not None:
                                raise ValueError(
                                    "trusted coverage adapter failure was not classified"
                                ) from exc
                            raise
                        if execution.returncode:
                            if collector.failure_protocol is not None and execution.returncode != 3:
                                raise ValueError(
                                    f"trusted coverage adapter rejected evidence (exit {execution.returncode}): {execution.stderr}"
                                )
                            raise CollectorUnavailable(
                                f"collector exited {execution.returncode}: {execution.stderr}"
                            )
                        report = json.loads(output.read_text())
                        version, tool = collector.tool_version, collector.command[0]
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
            _json(destination / "execution.json", receipts)
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
                {},
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
