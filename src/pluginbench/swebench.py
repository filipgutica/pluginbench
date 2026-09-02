from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from pluginbench.datasets import SWEbenchSpec


def _run_git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=workspace,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=60,
    )


def apply_starting_patch(workspace: Path, patch: Path) -> str:
    applied = _run_git(workspace, "apply", "--index", "--whitespace=nowarn", str(patch))
    if applied.returncode != 0:
        detail = applied.stderr.strip() or applied.stdout.strip() or "git apply failed"
        raise ValueError(f"cannot apply starting patch {patch}: {detail}")
    changed = _run_git(workspace, "diff", "--cached", "--name-only", "-z")
    if changed.returncode != 0:
        raise ValueError(f"cannot inspect starting patch {patch}: {changed.stderr.strip()}")
    paths = [item for item in changed.stdout.split("\0") if item]
    if any(path == ".agents" or path.startswith(".agents/") for path in paths):
        raise ValueError("starting patch must not modify .agents")
    tree = _run_git(workspace, "write-tree")
    tree_id = tree.stdout.strip()
    if tree.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", tree_id) is None:
        raise ValueError(f"cannot snapshot starting patch tree: {tree.stderr.strip()}")
    return tree_id


def build_clone_argv(
    *, repo: str, base_commit: str, destination: Path
) -> tuple[tuple[str, ...], ...]:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError(f"invalid SWE-bench repository identifier: {repo}")
    if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
        raise ValueError(f"invalid SWE-bench base commit: {base_commit}")
    root = str(destination)
    return (
        ("git", "init", root),
        ("git", "-C", root, "remote", "add", "origin", f"https://github.com/{repo}.git"),
        ("git", "-C", root, "fetch", "--depth=1", "origin", base_commit),
        ("git", "-C", root, "checkout", "--detach", "FETCH_HEAD"),
    )


def materialize_repository(spec: SWEbenchSpec, destination: Path) -> None:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    for argv in build_clone_argv(
        repo=spec.repo, base_commit=spec.base_commit, destination=destination
    ):
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=300,
            env=environment,
        )
        if result.returncode != 0:
            raise ValueError(
                f"cannot prepare {spec.repo} at {spec.base_commit}: {result.stderr.strip()}"
            )


def build_eval_argv(
    *,
    executable: Path,
    dataset_path: Path,
    predictions_path: Path,
    instance_id: str,
    run_id: str,
    timeout_seconds: int,
    report_dir: Path,
) -> tuple[str, ...]:
    return (
        str(executable),
        "eval",
        str(dataset_path),
        "--predictions",
        str(predictions_path),
        "--run-id",
        run_id,
        "--instance",
        instance_id,
        "--workers",
        "1",
        "--timeout",
        str(timeout_seconds),
        "--report-dir",
        str(report_dir),
    )


def parse_swebench_report(path: Path, instance_id: str) -> tuple[float | None, str | None]:
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read SWE-bench report {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("SWE-bench report must be a JSON object")
    if instance_id in report.get("infra_failure_ids", []):
        return None, "swebench_infrastructure_error"
    if instance_id in report.get("error_ids", []):
        return None, "swebench_error"
    if instance_id in report.get("resolved_ids", []):
        return 1.0, None
    if instance_id in report.get("unresolved_ids", []) or instance_id in report.get(
        "empty_patch_ids", []
    ):
        return 0.0, None
    return None, "swebench_missing_result"


def validate_harness(spec: SWEbenchSpec) -> None:
    if not spec.harness_executable.is_file():
        raise ValueError(f"SWE-bench executable does not exist: {spec.harness_executable}")
    python = spec.harness_executable.with_name("python")
    if not python.is_file():
        raise ValueError(f"cannot locate Python next to SWE-bench executable: {python}")
    try:
        result = subprocess.run(
            [str(python), "-c", "import swebench; print(swebench.__version__)"],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("SWE-bench version check timed out after 30 seconds") from exc
    actual = result.stdout.strip()
    if result.returncode != 0 or actual != spec.harness_version:
        raise ValueError(
            f"SWE-bench version mismatch: expected {spec.harness_version}, found {actual or 'unknown'}"
        )


def prepare_image(spec: SWEbenchSpec) -> None:
    if spec.image_platform is None:
        return
    result = subprocess.run(
        ("docker", "pull", "--platform", spec.image_platform, spec.image),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=1800,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot pull SWE-bench image {spec.image}: {result.stderr.strip()}")


def capture_patch(workspace: Path, base_commit: str) -> str:
    untracked = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=workspace,
        capture_output=True,
        check=False,
        shell=False,
        timeout=30,
    )
    if untracked.returncode != 0:
        raise ValueError("cannot list untracked files in SWE-bench workspace")
    paths = [
        item.decode()
        for item in untracked.stdout.split(b"\0")
        if item and not item.decode().startswith(".agents/")
    ]
    if paths:
        added = subprocess.run(
            ("git", "add", "--intent-to-add", "--", *paths),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=30,
        )
        if added.returncode != 0:
            raise ValueError(f"cannot stage new files for patch capture: {added.stderr.strip()}")
    diff = subprocess.run(
        ("git", "diff", "--binary", base_commit, "--", ".", ":(exclude).agents/**"),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=60,
    )
    if diff.returncode != 0:
        raise ValueError(f"cannot capture SWE-bench patch: {diff.stderr.strip()}")
    return diff.stdout


def verifier_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def run_official_evaluation(
    *,
    spec: SWEbenchSpec,
    instance_id: str,
    workspace: Path,
    artifact_dir: Path,
    run_id: str,
    model_name: str,
    timeout_seconds: int,
    starting_tree: str | None = None,
) -> tuple[float | None, str | None]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = artifact_dir / "dataset.json"
    predictions_path = artifact_dir / "predictions.jsonl"
    patch_path = artifact_dir / "patch.diff"
    patch = capture_patch(workspace, spec.base_commit)
    if starting_tree is not None:
        (artifact_dir / "agent-change.diff").write_text(capture_patch(workspace, starting_tree))
    dataset_path.write_text(json.dumps([spec.row], indent=2) + "\n")
    patch_path.write_text(patch)
    predictions_path.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "model_name_or_path": model_name,
                "model_patch": patch,
            }
        )
        + "\n"
    )
    argv = build_eval_argv(
        executable=spec.harness_executable,
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        instance_id=instance_id,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
        report_dir=artifact_dir,
    )
    result = subprocess.run(
        argv,
        cwd=artifact_dir,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=timeout_seconds + 120,
        env=verifier_environment(),
    )
    (artifact_dir / "command.json").write_text(json.dumps(list(argv), indent=2) + "\n")
    (artifact_dir / "stdout.log").write_text(result.stdout)
    (artifact_dir / "stderr.log").write_text(result.stderr)
    if result.returncode != 0:
        return None, "swebench_evaluator_failed"
    report_path = artifact_dir / f"{model_name.replace('/', '__')}.{run_id}.json"
    if not report_path.is_file():
        return None, "swebench_report_missing"
    return parse_swebench_report(report_path, instance_id)
