from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pluginbench.config import ConfigOverrides, PluginbenchConfig, load_config
from pluginbench.promptfoo import CommandResult
from pluginbench.results import ArmResult, TaskResult, Usage, canonical_fingerprint, save_arm


class FakePromptfooRunner:
    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable or Path("/fake/promptfoo")
        self.calls: list[tuple[Path, Path, int]] = []
        self.container_images: list[str | None] = []
        self.timeouts: list[float] = []

    def validate_version(self, expected: str) -> str:
        return expected

    def validate_codex_sdk_version(self, expected: str) -> str:
        return expected

    def validate_container_image(
        self, image: str, *, promptfoo_version: str, codex_sdk_version: str
    ) -> str:
        return "sha256:fake-pluginbench-runtime"

    def execute(
        self,
        config_path: Path,
        output_path: Path,
        *,
        concurrency: int,
        timeout_seconds: float,
        container_image: str | None = None,
    ) -> CommandResult:
        self.calls.append((config_path, output_path, concurrency))
        self.container_images.append(container_image)
        self.timeouts.append(timeout_seconds)
        config = yaml.safe_load(config_path.read_text())
        provider_config = config["providers"][0]["config"]
        response_output = (
            json.dumps({"verdict": "accept", "findings": []})
            if "output_schema" in provider_config
            else "done"
        )
        results = []
        for index, test in enumerate(config["tests"]):
            variables = test["vars"]
            workspace = Path(variables["workspace_dir"])
            treatment = (workspace / ".agents" / "skills").is_dir()
            if treatment:
                (workspace / "answer.txt").write_text("skill used\n")
            results.append(
                {
                    "id": f"result-{index}",
                    "testIdx": index,
                    "vars": variables,
                    "success": True,
                    "score": 1,
                    "latencyMs": 250,
                    "cost": 0.001,
                    "response": {
                        "output": response_output,
                        "tokenUsage": {
                            "prompt": 10,
                            "cached": 2,
                            "completion": 5,
                            "total": 15,
                        },
                    },
                }
            )
        output_path.write_text(
            json.dumps(
                {
                    "evalId": "fixture-eval",
                    "results": {
                        "version": 3,
                        "timestamp": "2026-08-31T10:00:00Z",
                        "results": results,
                        "prompts": [],
                        "stats": {
                            "successes": len(results),
                            "failures": 0,
                            "errors": 0,
                            "tokenUsage": {},
                        },
                    },
                    "config": {},
                    "shareableUrl": None,
                }
            )
        )
        return CommandResult(
            argv=(str(self.executable), "eval", "--config", str(config_path)),
            returncode=0,
            stdout="complete\n",
            stderr="",
        )


def write_evaluation_config(path: Path, *, baseline_mode: str = "run") -> Path:
    skill_dir = path.parent / "skills" / "review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review fixture tasks.\n---\nReview carefully.\n"
    )
    fixture = path.parent / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "README.md").write_text("Create answer.txt.\n")
    (path.parent / "verify.py").write_text(
        "from pathlib import Path\n"
        "answer = Path('answer.txt')\n"
        "raise SystemExit(0 if answer.exists() and answer.read_text() == 'skill used\\n' else 1)\n"
    )
    catalog = path.parent / "tasks.yaml"
    catalog.write_text(
        """
schema_version: 1
tasks:
  - id: task-a
    prompt: Create answer.txt with the correct content.
    fixture: fixture
    verifier:
      command: [python3]
      script: verify.py
      timeout_seconds: 10
  - id: task-b
    prompt: Create answer.txt with the correct content.
    fixture: fixture
    verifier:
      command: [python3]
      script: verify.py
      timeout_seconds: 10
""".strip()
        + "\n"
    )
    auth = path.parent / "auth.json"
    auth.write_text("{}\n")
    path.write_text(
        f"""
schema_version: 1
experiment:
  name: smoke
dataset:
  adapter: local
  name: fixture-tasks
  version: "1"
  source: tasks.yaml
  task_ids: [task-a, task-b]
skill:
  path: skills
agent:
  provider: openai:codex-sdk
  codex_sdk_version: 0.151.0
  model: gpt-5.6-luna
  reasoning: high
  sandbox_mode: workspace-write
  approval_policy: never
  network_access_enabled: false
  web_search_enabled: false
  auth_file: auth.json
tooling:
  promptfoo_version: 0.122.2
execution:
  baseline:
    mode: {baseline_mode}
  attempts: 1
  concurrency: 2
  batch_size: 2
  max_tasks: 4
output:
  runs_dir: runs
""".strip()
        + "\n"
    )
    return path


def load_evaluation_config(
    tmp_path: Path, overrides: ConfigOverrides | None = None
) -> PluginbenchConfig:
    return load_config(write_evaluation_config(tmp_path / "eval.yaml"), overrides)


def write_swebench_config(path: Path) -> Path:
    skill_dir = path.parent / "skills" / "review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review repository tasks.\n---\nReview carefully.\n"
    )
    dataset = path.parent / "swebench.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "repo": "sympy/sympy",
                    "instance_id": "sympy__sympy-20590",
                    "base_commit": "cffd4e0f86fefd4802349a9f9b19ed70934ea354",
                    "problem_statement": "Symbol instances unexpectedly have a __dict__.",
                    "version": "1.7",
                    "image": (
                        "swebench/sweb.eval.x86_64.sympy_1776_sympy-20590@sha256:" + "0" * 64
                    ),
                    "eval_script": "#!/bin/bash\npython -m pytest -q",
                    "log_parser": "parse_log_sympy",
                    "eval_type": "pass_and_fail",
                    "FAIL_TO_PASS": ["test_immutable"],
                    "PASS_TO_PASS": ["test_structure"],
                    "patch": "diff --git a/a b/a\n",
                    "test_patch": "diff --git a/t b/t\n",
                }
            ],
            indent=2,
        )
        + "\n"
    )
    auth = path.parent / "auth.json"
    auth.write_text("{}\n")
    harness = path.parent / "bin" / "swebench"
    harness.parent.mkdir()
    harness.write_text("")
    path.write_text(
        """
schema_version: 1
experiment:
  name: swebench-smoke
dataset:
  adapter: swe-bench
  name: SWE-bench_Verified
  version: 78f471bf655a3137b2e8a75af1501690ec009ec3
  source: swebench.json
  task_ids: [sympy__sympy-20590]
  harness:
    executable: bin/swebench
    version: 5.0.2
    image_platform: linux/amd64
skill:
  path: skills
agent:
  provider: openai:codex-sdk
  codex_sdk_version: 0.151.0
  model: gpt-5.6-luna
  reasoning: high
  sandbox_mode: workspace-write
  approval_policy: never
  network_access_enabled: false
  web_search_enabled: false
  auth_file: auth.json
tooling:
  promptfoo_version: 0.122.2
  container_image: pluginbench-runtime:0.2.0
execution:
  baseline:
    mode: run
  attempts: 1
  concurrency: 1
  batch_size: 1
  max_tasks: 1
  timeout_seconds: 900
output:
  runs_dir: runs
""".strip()
        + "\n"
    )
    return path


@pytest.fixture(scope="session")
def review_config_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from pluginbench.review import _content_digest, _load_snapshot, _task_digest

    root = tmp_path_factory.mktemp("review-study")
    template = root / "template-repository"
    template.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=template, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=template, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=template, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=template, check=True)
    (template / "source.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "source.py"], cwd=template, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=template, check=True)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=template,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    task_ids = [
        "pydata__xarray-6992",
        "sphinx-doc__sphinx-7590",
        "sympy__sympy-13878",
        "astropy__astropy-13398",
        "django__django-16560",
        "pylint-dev__pylint-4551",
        "pytest-dev__pytest-5787",
        "scikit-learn__scikit-learn-25102",
    ]

    def addition_patch(filename: str, content: str) -> str:
        return (
            f"diff --git a/{filename} b/{filename}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{filename}\n"
            "@@ -0,0 +1 @@\n"
            f"+{content}\n"
        )

    rows = [
        {
            "repo": "example/repository",
            "instance_id": task_id,
            "base_commit": base_commit,
            "problem_statement": f"Fix the regression for {task_id}.",
            "version": "1",
            "image": "example/image@sha256:" + "0" * 64,
            "eval_script": "#!/bin/sh\nexit 0",
            "log_parser": "parse_log",
            "eval_type": "pass_and_fail",
            "FAIL_TO_PASS": ["test_regression"],
            "PASS_TO_PASS": ["test_existing"],
            "patch": addition_patch("gold.txt", f"gold {task_id}"),
            "test_patch": addition_patch("test.txt", f"test {task_id}"),
        }
        for task_id in task_ids
    ]
    snapshot_path = root / "tasks.json"
    snapshot_path.write_text(json.dumps(rows, indent=2) + "\n")
    normalized_rows = _load_snapshot(snapshot_path)
    task_digests = {task_id: _task_digest(normalized_rows[task_id]) for task_id in task_ids}
    dataset_name = "SWE-bench/SWE-bench_Verified"
    dataset_version = "fixture-revision"
    payload = {
        "dataset": {
            "name": dataset_name,
            "version": dataset_version,
            "task_ids": task_ids,
            "task_digests": task_digests,
        },
        "agent": {"model": "gpt-5.6-luna"},
    }
    fingerprint = canonical_fingerprint(payload)
    run_roots = {
        "baseline": root / "full-run",
        "full": root / "full-run",
        "core": root / "core-run",
    }
    arm_directories = {
        "baseline": run_roots["baseline"] / "baseline",
        "full": run_roots["full"] / "treatment",
        "core": run_roots["core"] / "treatment",
    }
    for directory in arm_directories.values():
        directory.mkdir(parents=True, exist_ok=True)

    for label, run_root in run_roots.items():
        for task_id in task_ids:
            repository = run_root / "inputs" / "tasks" / task_id / "repository"
            if not repository.exists():
                shutil.copytree(template, repository)
            patch = addition_patch(f"{label}.txt", f"{label} {task_id}")
            attempt = arm_directories[label] / "verifiers" / task_id / "attempt-01"
            attempt.mkdir(parents=True)
            (attempt / "patch.diff").write_text(patch)
            (attempt / "predictions.jsonl").write_text(
                json.dumps(
                    {
                        "instance_id": task_id,
                        "model_name_or_path": "gpt-5.6-luna",
                        "model_patch": patch,
                    }
                )
                + "\n"
            )

    for label, directory in arm_directories.items():
        results = {
            task_id: TaskResult(
                task_id=task_id,
                attempts=1,
                score=(
                    None
                    if label == "core" and task_id == "scikit-learn__scikit-learn-25102"
                    else 1.0
                ),
                passed=not (label == "core" and task_id == "scikit-learn__scikit-learn-25102"),
                duration_seconds=1.0,
                infrastructure_errors=(
                    ["fixture_infrastructure_error"]
                    if label == "core" and task_id == "scikit-learn__scikit-learn-25102"
                    else []
                ),
                usage=Usage(),
            )
            for task_id in task_ids
        }
        arm = ArmResult.from_tasks(
            arm="baseline" if label == "baseline" else "treatment",
            compatibility_fingerprint=fingerprint,
            compatibility_payload=payload,
            tasks=results,
            task_checksums=task_digests,
            model_identifiers=["gpt-5.6-luna"],
            agent_versions=["0.151.0"],
            promptfoo_version="0.122.2",
            skill_digest=None if label == "baseline" else f"sha256:{label:0<64}"[:71],
        )
        save_arm(directory / "arm.json", arm)

    oracle_path = root / "oracle.json"
    oracle_path.write_text(json.dumps({"resolved_ids": task_ids}, sort_keys=True) + "\n")
    evidence_path = root / "gold-calibration.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_name": dataset_name,
                "dataset_version": dataset_version,
                "oracle_result_digest": _content_digest(oracle_path.read_bytes()),
                "tasks": {
                    task_id: {
                        "task_digest": task_digests[task_id],
                        "patch_digest": _content_digest(
                            str(normalized_rows[task_id]["patch"]).encode()
                        ),
                    }
                    for task_id in task_ids
                },
            },
            indent=2,
        )
        + "\n"
    )
    codex_home = root / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n")
    config_path = root / "eval.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "study": {"name": "fixture-review", "randomization_seed": "fixture-seed"},
                "sources": {
                    "baseline": {"arm": str(arm_directories["baseline"] / "arm.json")},
                    "full": {"arm": str(arm_directories["full"] / "arm.json")},
                    "core": {"arm": str(arm_directories["core"] / "arm.json")},
                    "compatibility_fingerprint": fingerprint,
                },
                "dataset": {
                    "name": dataset_name,
                    "version": dataset_version,
                    "source": str(snapshot_path),
                },
                "reviewer": {
                    "provider": "openai:codex-sdk",
                    "codex_sdk_version": "0.151.0",
                    "model": "gpt-5.6-luna",
                    "reasoning": "high",
                    "attempts": 2,
                    "timeout_seconds": 30,
                    "concurrency": 1,
                    "auth": {"mode": "subscription", "codex_home": str(codex_home)},
                },
                "tooling": {
                    "promptfoo_version": "0.122.2",
                    "container_image": "pluginbench-runtime:0.2.0",
                },
                "calibration": {
                    "enabled": True,
                    "oracle_result": str(oracle_path),
                    "evidence_manifest": str(evidence_path),
                },
                "limits": {"max_tokens": 4_000_000, "max_cost_usd": 5.0},
                "estimates": {"tokens_per_review": 50_000, "cost_per_review_usd": 0.08},
                "output": {"runs_dir": str(root / "reviews")},
            },
            sort_keys=False,
        )
    )
    return config_path


@pytest.fixture(scope="session")
def completed_review_run(
    tmp_path_factory: pytest.TempPathFactory, review_config_path: Path
) -> Path:
    from pluginbench.review import resolve_review_study
    from pluginbench.review_models import load_review_config

    study = resolve_review_study(load_review_config(review_config_path))
    run = tmp_path_factory.mktemp("completed-review")
    (run / "private").mkdir()
    workflow = study.workflow_candidates
    (run / "private" / "candidate-map.json").write_text(
        json.dumps(
            {
                candidate.candidate_id: {
                    "task_id": candidate.task_id,
                    "source_label": candidate.source_label,
                }
                for candidate in workflow
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "compatibility_fingerprint": study.config.sources.compatibility_fingerprint,
                "dataset": study.config.dataset.model_dump(mode="json"),
                "tasks": [{"task_id": task.task_id, "digest": task.digest} for task in study.tasks],
                "reviewer": study.config.reviewer.model_dump(mode="json", exclude={"auth"}),
                "review_tooling": study.config.tooling.model_dump(mode="json"),
                "candidates": {
                    candidate.candidate_id: {
                        "patch_files": [],
                        "lines_added": 1,
                        "lines_removed": 0,
                    }
                    for candidate in workflow
                },
                "expected_attempts_per_candidate": study.config.reviewer.attempts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return run
