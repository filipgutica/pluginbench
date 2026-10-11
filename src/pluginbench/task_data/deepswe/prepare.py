"""Prepare a pinned public coding task without exposing its hidden tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib

from pluginbench.task_data.deepswe.verify import dependency_digest
from pluginbench.task_data.deepswe.measurements import (
    MEASUREMENT_NAMES,
    measurement_metadata,
    prepare_measurements,
)

SUITE_SHA = "0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea"
TASK = "ofetch-per-origin-circuit-breaker"
ROOT = Path(__file__).resolve().parent
NODE_VERSION = "v22.20.0"


def command(arguments: list[str], cwd: Path | None = None) -> bytes:
    return subprocess.check_output(
        arguments, cwd=cwd, env={**os.environ, "npm_config_userconfig": "/dev/null"}
    )


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


def suite_root(path: Path, task: str) -> Path:
    """Accept a checkout root or its standard tasks directory."""
    resolved = path.resolve()
    if not (resolved / "tasks").is_dir() and (resolved / task).is_dir():
        return resolved.parent
    return resolved


def validate_suite(suite: Path, task: str) -> None:
    if command(["git", "rev-parse", "HEAD"], suite).decode().strip() != SUITE_SHA:
        raise ValueError(f"DeepSWE checkout must be at {SUITE_SHA}")
    dirty = command(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", f"tasks/{task}"], suite
    )
    if dirty.strip():
        raise ValueError(f"selected DeepSWE task assets are modified: {task}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        choices=[
            TASK,
            "happy-dom-abort-pending-body-reads",
            "testem-per-launcher-reports",
            "true-myth-iterable-collection-combinators",
            "happy-dom-deterministic-intersectionobserver",
        ],
        default=TASK,
    )
    parser.add_argument(
        "--suite", type=Path, required=True, help="DeepSWE checkout at the pinned revision"
    )
    parser.add_argument(
        "--repo", type=Path, help="existing upstream Git clone (otherwise clone into generated/)"
    )
    parser.add_argument("--output", type=Path, required=True, help="fresh preparation directory")
    parser.add_argument(
        "--measurement",
        choices=sorted(MEASUREMENT_NAMES),
        action="append",
        help="collect only these measurements; repeat to select more",
    )
    parser.add_argument("--observer-cache", type=Path, help="observer cache outside task fixtures")
    args = parser.parse_args()
    observed_node = command(["node", "--version"]).decode().strip()
    if observed_node != NODE_VERSION:
        raise ValueError(f"expected Node {NODE_VERSION}, observed {observed_node}")
    suite = suite_root(args.suite, args.task)
    validate_suite(suite, args.task)
    generated = args.output.resolve()
    generated.mkdir(parents=True, exist_ok=False)
    task = args.task
    task_source = suite / "tasks" / task
    metadata = tomllib.loads((task_source / "task.toml").read_text())["metadata"]
    repository = metadata["repository_url"]
    base_sha = metadata["base_commit_hash"]
    repo = args.repo or generated / "upstream-repository"
    if not repo.exists():
        subprocess.run(["git", "clone", repository, str(repo)], check=True)
    if command(["git", "rev-parse", f"{base_sha}^{{commit}}"], repo).decode().strip() != base_sha:
        raise ValueError("upstream base commit unavailable")
    fixture = generated / "fixtures" / task
    if fixture.exists():
        raise ValueError(
            f"prepared fixture already exists: {fixture}; use --output with a fresh directory"
        )
    fixture.mkdir(parents=True)
    archive = command(["git", "archive", base_sha], repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(fixture, filter="data")
    stripped = []
    for item in sorted(fixture.rglob("*"), reverse=True):
        if item.name in {"AGENTS.md", "CLAUDE.md", ".agents", ".claude", ".codex"}:
            stripped.append(item.relative_to(fixture).as_posix())
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        elif item.is_symlink():
            raise ValueError(f"unsupported upstream symlink: {item}")
    # Separate dependency installation; generated fixture remains source-only.
    dependencies = generated / "dependencies" / task
    manager = json.loads((fixture / "package.json").read_text()).get("packageManager", "npm@10.9.2")
    if task == "true-myth-iterable-collection-combinators":
        manager = "pnpm@10.28.1"
    if task == TASK:
        dependencies.mkdir(parents=True)
        lock = "pnpm-lock.yaml"
        for name in ("package.json", lock):
            shutil.copy2(fixture / name, dependencies / name)
        install = ["install", "--frozen-lockfile", "--ignore-scripts"]
        trusted = ["package.json", lock, "vitest.config.ts", "test/index.test.ts"]
        runs = [
            ("base", ["test/index.test.ts", "-t", "ok|baseURL|404"]),
            ("new", ["test/circuit-breaker.test.ts"]),
        ]
        test_cwd = "."
        held_out_paths = ["test/circuit-breaker.test.ts", "test.sh"]
        test_command = "corepack pnpm vitest run"
    elif task in {
        "happy-dom-abort-pending-body-reads",
        "happy-dom-deterministic-intersectionobserver",
    }:
        shutil.copytree(fixture, dependencies)
        lock = "package-lock.json"
        install = ["ci", "--include=dev", "--ignore-scripts"]
        test_cwd = "packages/happy-dom"
        base_tests = [
            "fetch/Request.test.ts",
            "fetch/Response.test.ts",
            "browser/Browser.test.ts",
            "browser/BrowserPage.test.ts",
            "window/DetachedWindowAPI.test.ts",
        ]
        trusted = [
            "package.json",
            lock,
            ".npmrc",
            f"{test_cwd}/vitest.config.ts",
            f"{test_cwd}/test/setup.ts",
            *[f"{test_cwd}/test/{name}" for name in base_tests],
        ]
        runs = [
            ("base", ["--testTimeout=30000", *[f"test/{name}" for name in base_tests]]),
            ("new", ["test/window/AsyncTeardown.test.ts"]),
        ]
        held_out_paths = [f"{test_cwd}/test/window/AsyncTeardown.test.ts", "test.sh"]
        if task == "happy-dom-deterministic-intersectionobserver":
            base_tests = ["event/EventTarget.test.ts"]
            trusted = [
                "package.json",
                lock,
                ".npmrc",
                f"{test_cwd}/vitest.config.ts",
                f"{test_cwd}/test/setup.ts",
                *[f"{test_cwd}/test/{name}" for name in base_tests],
            ]
            runs = [
                ("base", ["test/event/EventTarget.test.ts", "-t", "addEventListener"]),
                ("new", ["test/intersection-observer/IntersectionObserver.challenge.test.ts"]),
            ]
            held_out_paths = [
                f"{test_cwd}/test/intersection-observer/IntersectionObserver.challenge.test.ts",
                "test.sh",
            ]
        test_command = "corepack npm@10.9.2 exec --workspace=packages/happy-dom -- vitest run"
    elif task == "true-myth-iterable-collection-combinators":
        shutil.copytree(fixture, dependencies)
        lock = "pnpm-lock.yaml"
        install = ["install", "--frozen-lockfile", "--ignore-scripts"]
        test_cwd = "."
        base_tests = [
            "test/maybe.test.ts",
            "test/result.test.ts",
            "test/task.test.ts",
            "test/toolbelt.test.ts",
            "test/unit.test.ts",
            "test/interop.test.ts",
            "test/test-support.test.ts",
            "test/standard-schema.test.ts",
        ]
        trusted = ["package.json", lock, "vitest.config.ts", "ts/test.tsconfig.json", *base_tests]
        runs = [
            ("base", ["--coverage=false", *base_tests]),
            ("new", ["--coverage=false", "test/traversal.test.ts", "test/extras.test.ts"]),
        ]
        held_out_paths = ["test/traversal.test.ts", "test/extras.test.ts", "test.sh"]
        test_command = f"corepack {manager} vitest run --coverage=false"
    else:
        shutil.copytree(fixture, dependencies)
        lock = "package-lock.json"
        install = ["install", "--include=dev", "--ignore-scripts", "--package-lock=true"]
        test_cwd = "."
        trusted = ["package.json", lock, ".npmrc", ".mocharc.js", "tests/_prepare.js"]
        trusted.extend(
            path.relative_to(fixture).as_posix()
            for path in sorted((fixture / "tests").rglob("*_tests.js"))
        )
        runs = [
            (
                "base",
                [
                    "tests/*_tests.js",
                    "tests/**/*_tests.js",
                    "--fgrep",
                    "does not proxy testem files",
                    "--invert",
                    "--exclude",
                    "tests/ci/ci_tests.js",
                    "--exclude",
                    "tests/ci/dev_tests.js",
                    "--exclude",
                    "tests/api_tests.js",
                    "--exclude",
                    "tests/utils/per_launcher_reporter_tests.js",
                ],
            ),
            ("new", ["tests/utils/per_launcher_reporter_tests.js"]),
        ]
        held_out_paths = ["tests/utils/per_launcher_reporter_tests.js", "test.sh"]
        test_command = "corepack npm@10.9.2 exec -- mocha tests/*_tests.js tests/**/*_tests.js"
    derived_lock = task == "testem-per-launcher-reports"
    command(["corepack", manager, *install], dependencies)
    if derived_lock:
        shutil.copy2(dependencies / lock, fixture / lock)
        install = ["ci", "--include=dev", "--ignore-scripts", "--package-lock=true"]
    payload = {
        "node": NODE_VERSION,
        "dependencies": str((dependencies / "node_modules").resolve()),
        "dependency_sha256": dependency_digest(dependencies / "node_modules"),
        "trusted_files": {name: encoded((fixture / name).read_bytes()) for name in trusted},
        "test_patch": encoded((task_source / "tests/test.patch").read_bytes()),
        "config": json.loads((task_source / "tests/config.json").read_text()),
        "runs": runs,
        "test_cwd": test_cwd,
        "held_out_paths": held_out_paths,
        "framework": "mocha" if task == "testem-per-launcher-reports" else "vitest",
        "extra_dependency_trees": [
            {
                "relative": str(tree.relative_to(dependencies)),
                "path": str(tree.resolve()),
                "sha256": dependency_digest(tree),
            }
            for tree in dependencies.rglob("node_modules")
            if tree.is_dir()
            and not tree.is_symlink()
            and tree.parent != dependencies
            and "node_modules" not in tree.relative_to(dependencies).parts[:-1]
        ]
        if task != TASK
        else [],
    }
    verifier = generated / ("verify-ofetch.py" if task == TASK else f"verify-{task}.py")
    template = (ROOT / "verify.py").read_text()
    verifier.write_text(template.replace("PAYLOAD = {}", "PAYLOAD = " + repr(payload)))
    original = (task_source / "instruction.md").read_text()
    commit_instruction = "IMPORTANT: Please work on this in a new branch from main and commit everything when you are done."
    if original.count(commit_instruction) != 1:
        raise ValueError("upstream delivery instruction changed")
    prompt = original.replace(
        commit_instruction,
        "Work in the supplied source snapshot; do not create branches or commits. "
        f"Install locked dependencies with corepack {manager} {' '.join(install)}. "
        f"Run existing tests with {test_command}.",
    )
    prompt += (
        "\n\nExecution contract: work as a single implementation agent. "
        "Independent review happens externally after source handoff. "
        "Do not claim completed delegation or an unavailable independent pre-edit plan critique.\n"
    )
    provenance = {
        **measurement_metadata(task, args.measurement),
        "suite": "DeepSWE",
        "suite_commit": SUITE_SHA,
        "suite_license": "Apache-2.0; original upstream license retained in DEEPSWE-LICENSE",
        "task_id": task,
        "repository": repository,
        "base_commit": base_sha,
        "license": "MIT (fixture LICENSE.md)"
        if task == "testem-per-launcher-reports"
        else "MIT (fixture LICENSE)",
        "node": payload["node"],
        "package_manager": manager,
        "lock_sha256": hashlib.sha256((fixture / lock).read_bytes()).hexdigest(),
        "test_patch_sha256": hashlib.sha256(
            (task_source / "tests/test.patch").read_bytes()
        ).hexdigest(),
        "instruction_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "dependency_sha256": payload["dependency_sha256"],
        "prompt_adaptation": "replace branch/commit instruction with source-snapshot delivery and original package commands; append identical single-agent execution contract with external post-handoff review and no claims of unavailable delegation/pre-edit critique",
        "stripped_guidance": json.dumps(stripped),
        "backend_difference": "native macOS; no enforced network isolation; locked ignore-scripts dependency setup",
        "derived_lock_adaptation": "generated package-lock.json with npm10.9.2 --package-lock=true; added to both fixtures with original code bytes unchanged"
        if derived_lock
        else "none; original lock retained",
    }
    catalog = {
        "schema_version": 1,
        "tasks": [
            {
                "id": task,
                "fixture": f"fixtures/{task}",
                "prompt": prompt,
                "provenance": provenance,
                "verifier": {
                    "command": ["python3"],
                    "script": verifier.name,
                    "timeout_seconds": 600,
                },
            }
        ],
    }
    (generated / "tasks.json").write_text(json.dumps(catalog, indent=2) + "\n")
    (generated / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    shutil.copy2(suite / "LICENSE", generated / "DEEPSWE-LICENSE")
    prepare_measurements(
        prepared=generated,
        task=task,
        payload=payload,
        verifier=verifier,
        selection=args.measurement,
        observer_cache=args.observer_cache,
    )
    print(generated / "tasks.json")
    print(generated / "measurements.json")


if __name__ == "__main__":
    main()
