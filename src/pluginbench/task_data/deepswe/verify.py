"""Standalone verifier template; prepare.py embeds pinned upstream assets."""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import TypedDict
import xml.etree.ElementTree as ET


class VerifierPayload(TypedDict, total=False):
    node: str
    dependencies: str
    dependency_sha256: str
    trusted_files: dict[str, str]
    test_patch: str
    config: dict[str, list[str]]
    held_out_paths: list[str]
    extra_dependency_trees: list[dict[str, str]]
    framework: str
    runs: list[tuple[str, list[str]]]
    test_cwd: str


PAYLOAD: VerifierPayload
PAYLOAD = {}  # Replaced with immutable task assets during preparation.


def dependency_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            content = b"link:" + os.readlink(path).encode()
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(content + b"\0")
    return digest.hexdigest()


def run(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


def reported_tests(
    report: Path, framework: str
) -> list[tuple[str, bool, dict[str, object] | None]]:
    if framework == "mocha":
        data = json.loads(report.read_text())
        passed = {row["fullTitle"] for row in data["passes"]}
        failures = {row["fullTitle"]: row["err"] for row in data["failures"]}
        pending = {row["fullTitle"] for row in data["pending"]}
        return [
            (
                row["fullTitle"],
                row["fullTitle"] in passed
                and row["fullTitle"] not in failures
                and row["fullTitle"] not in pending,
                failures.get(row["fullTitle"]),
            )
            for row in data["tests"]
        ]
    if framework != "vitest":
        raise ValueError(f"unsupported framework: {framework}")
    rows: list[tuple[str, bool, dict[str, object] | None]] = []
    for case in ET.parse(report).iter("testcase"):
        identity = f"{case.attrib['classname']}: {case.attrib['name']}"
        case_passed = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
        case_failures = [
            case.find(tag) for tag in ("failure", "error") if case.find(tag) is not None
        ]
        failure = case_failures[0] if case_failures else None
        detail: dict[str, object] | None = (
            {"message": failure.attrib.get("message"), "detail": failure.text}
            if failure is not None
            else None
        )
        rows.append((identity, case_passed, detail))
    return rows


def verify() -> dict[str, object]:
    submitted = Path.cwd()
    env = {**os.environ, "npm_config_userconfig": "/dev/null"}
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    if node != PAYLOAD["node"]:
        raise ValueError(f"expected Node {PAYLOAD['node']}, observed {node}")
    with tempfile.TemporaryDirectory(prefix="pluginbench-ofetch-verifier-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(submitted, workspace, ignore=shutil.ignore_patterns("node_modules", ".git"))
        # Trusted runner and baseline tests cannot be weakened by a submitted edit.
        metadata_changes = []
        for name, encoded in PAYLOAD["trusted_files"].items():
            content = base64.b64decode(encoded)
            target = workspace / name
            if not target.exists() or target.read_bytes() != content:
                metadata_changes.append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        patch = Path(temporary) / "held-out.patch"
        patch.write_bytes(base64.b64decode(PAYLOAD["test_patch"]))
        for name in PAYLOAD.get("held_out_paths", ["test/circuit-breaker.test.ts", "test.sh"]):
            path = workspace / name
            path.unlink(missing_ok=True)
        applied = run(["git", "apply", str(patch)], workspace, env)
        if applied.returncode:
            raise ValueError(f"held-out patch failed: {applied.stderr}")
        dependency_root = Path(PAYLOAD["dependencies"])
        if not dependency_root.is_dir():
            raise ValueError(
                f"prepared dependency cache missing: {dependency_root}; rerun prepare.py"
            )
        if dependency_digest(dependency_root) != PAYLOAD["dependency_sha256"]:
            raise ValueError(
                "prepared dependency cache changed; rerun preparation in a fresh directory"
            )
        shutil.copytree(dependency_root, workspace / "node_modules", symlinks=True)
        for tree in PAYLOAD.get("extra_dependency_trees", []):
            directory = Path(tree["path"])
            if dependency_digest(directory) != tree["sha256"]:
                raise ValueError(f"prepared dependency cache changed: {tree['relative']}")
            shutil.copytree(directory, workspace / tree["relative"], symlinks=True)
        results: dict[str, bool] = {}
        row_counts: dict[str, int] = {}
        failures = {}
        logs = {}
        framework = PAYLOAD.get("framework", "vitest")
        for name, arguments in PAYLOAD.get(
            "runs",
            [
                ("base", ["test/index.test.ts", "-t", "ok|baseURL|404"]),
                ("new", ["test/circuit-breaker.test.ts"]),
            ],
        ):
            report = Path(temporary) / f"{name}.{'json' if framework == 'mocha' else 'xml'}"
            if framework == "mocha":
                expanded: list[str] = []
                for argument in arguments:
                    if "*" in argument:
                        expanded.extend(
                            Path(path).relative_to(workspace).as_posix()
                            for path in sorted(glob.glob(str(workspace / argument)))
                        )
                    else:
                        expanded.append(argument)
                command = [
                    "node",
                    str(workspace / "node_modules/mocha/bin/mocha.js"),
                    *expanded,
                    "--reporter=json",
                    f"--reporter-option=output={report}",
                ]
            else:
                command = [
                    "node",
                    str(workspace / "node_modules/vitest/vitest.mjs"),
                    "run",
                    *arguments,
                    "--reporter=junit",
                    f"--outputFile={report}",
                ]
            execution = run(
                command,
                workspace / PAYLOAD.get("test_cwd", "."),
                env,
            )
            logs[name] = {
                "exit_code": execution.returncode,
                "stdout": execution.stdout,
                "stderr": execution.stderr,
            }
            if not report.exists():
                raise ValueError(f"missing {name} JUnit report: {execution.stderr}")
            for identity, passed, detail in reported_tests(report, framework):
                # Upstream grader uses worst-status-wins for repeated node IDs.
                results[identity] = results.get(identity, True) and passed
                row_counts[identity] = row_counts.get(identity, 0) + 1
                if detail:
                    failures[identity] = detail
        groups = {}
        for name in ("f2p_node_ids", "p2p_node_ids"):
            ids = PAYLOAD["config"][name]
            groups[name] = {
                "total": len(ids),
                "passed": sum(results.get(item, False) for item in ids),
                "failed": [item for item in ids if not results.get(item, False)],
            }
        return {
            "pass": all(group["passed"] == group["total"] for group in groups.values()),
            "tool": "upstream-mocha-json" if framework == "mocha" else "upstream-vitest-junit",
            "node": node,
            "groups": groups,
            "trusted_file_changes": metadata_changes,
            "suites": logs,
            "failures": failures,
            "dependency_sha256": PAYLOAD["dependency_sha256"],
            "duplicate_rows": {name: count for name, count in row_counts.items() if count > 1},
            "test_patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "network_isolation": "not enforced by local backend; tests use local mocks/server",
        }


if __name__ == "__main__":
    try:
        result = verify()
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as error:
        print(json.dumps({"pass": None, "error": str(error)}))
        raise SystemExit(2)
    print(json.dumps(result))
    raise SystemExit(0 if result["pass"] else 1)
