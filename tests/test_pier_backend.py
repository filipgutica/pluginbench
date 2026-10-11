from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from pluginbench import codex, pier_backend, study
from pluginbench.assessment_metrics import inspect_snapshot


def _catalog(tmp_path: Path) -> Path:
    entries = []
    for name in ("first", "second"):
        directory = tmp_path / "tasks" / name
        (directory / "tests").mkdir(parents=True)
        (directory / "instruction.md").write_text(f"Implement {name}.\n")
        (directory / "task.toml").write_text(
            '[metadata]\nbase_commit_hash="'
            + "a" * 40
            + '"\nrepository_url="https://example.com/repo"\n'
            '[environment]\ndocker_image="image@sha256:' + "b" * 64 + '"\n'
            '[verifier]\nenvironment_mode="separate"\n'
        )
        (directory / "tests/Dockerfile").write_text("FROM image@sha256:" + "b" * 64 + "\n")
        entries.append({"id": name, "path": f"tasks/{name}"})
    catalog = tmp_path / "tasks.yaml"
    catalog.write_text(
        yaml.safe_dump({"schema_version": 1, "source": {"revision": "c" * 40}, "tasks": entries})
    )
    return catalog


def _recipe(tmp_path: Path) -> study.ResolvedStudy:
    _catalog(tmp_path)
    (tmp_path / "guidance.md").write_text("Reuse existing helpers.\n")
    recipe = tmp_path / "study.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "Real task smoke",
                "backend": "pier",
                "tasks": "tasks.yaml",
                "guidance": {"engineering": ["guidance.md"]},
                "repeats": 1,
            }
        )
    )
    return study.load_study(recipe)


def test_pinned_catalog_and_freeze_bind_all_task_bytes(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    tasks = pier_backend.load_tasks(catalog)
    frozen = pier_backend.freeze_tasks(tasks, tmp_path / "frozen")
    assert frozen[0].digest == tasks[0].digest
    (tasks[0].directory / "tests/Dockerfile").write_text("FROM image:latest\n")
    with pytest.raises(ValueError, match="changed before freezing"):
        pier_backend.freeze_tasks(tasks, tmp_path / "another")
    with pytest.raises(ValueError, match="pinned by digest"):
        pier_backend.load_tasks(catalog)


def test_catalog_rejects_symlink_ancestors_and_unpinned_source(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "tasks", target_is_directory=True)
    data = yaml.safe_load(catalog.read_text())
    data["tasks"][0]["path"] = "alias/first"
    catalog.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="without symlinks"):
        pier_backend.load_tasks(catalog)
    data["source"]["revision"] = "main"
    catalog.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="exact source revision"):
        pier_backend.load_tasks(catalog)


def test_pier_rejects_repairs_before_launch(tmp_path: Path) -> None:
    resolved = _recipe(tmp_path)
    data = yaml.safe_load(resolved.source.read_text())
    data["max_repairs"] = 1
    resolved.source.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="max_repairs: 0"):
        study.load_study(resolved.source)


def test_pier_canonical_run_retains_failure_and_incomplete_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _recipe(tmp_path)
    calls: list[dict[str, Any]] = []
    calibrated: list[str] = []
    monkeypatch.setattr(
        pier_backend, "preflight", lambda _: {"pier_version": "0.3.1", "paid_calls": 0}
    )

    def calibrate(
        tasks: tuple[pier_backend.PierTask, ...], target: Path, python: Path
    ) -> dict[str, Any]:
        calibrated.extend(task.digest for task in tasks)
        return {"paid_calls": 0, "passed": True}

    def invoke(**kwargs: Any) -> dict[str, Any]:
        assert len(calibrated) == 2
        calls.append(kwargs)
        target: Path = kwargs["target"]
        target.mkdir(parents=True)
        if len(calls) == 1:
            return {
                "execution": {"successful_native_completion": False, "usage": None},
                "checks": {"behavior": None},
                "before": None,
                "handoff": None,
            }
        for stage in ("before", "handoff"):
            (target / stage / "src").mkdir(parents=True)
            (target / stage / "src/index.ts").write_text("export const answer = 1;\n")
        return {
            "execution": {
                "successful_native_completion": True,
                "actual_primary_models": ["gpt-6.1-sol"],
                "runtime_effort_evidence": ["medium"],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
            "checks": {
                "behavior": True,
                "delivery_complete": len(calls) != 2,
                "submission_collected": True,
            },
            "before": str(target / "before"),
            "handoff": str(target / "handoff"),
            "harness_version": "codex-cli 0.162.0",
        }

    monkeypatch.setattr(pier_backend, "calibrate", calibrate)
    monkeypatch.setattr(pier_backend, "invoke", invoke)
    root = study.run_study(resolved, tmp_path / "run")
    report = json.loads((root / "report/assessment-report.json").read_text())
    assert len(calls) == len(report["attempts"]) == 4
    assert report["attempts"][0]["before_digest"] is None
    assert report["attempts"][0]["model"] is None
    assert report["attempts"][0]["provenance"]["assigned_model"] == "gpt-6.1-sol"
    assert report["attempts"][1]["stages"][0]["correctness"]["status"] == "fail"
    assert report["attempts"][2]["stages"][0]["correctness"]["status"] == "pass"
    assert "Reuse existing helpers." in next(
        call["prompt"] for call in calls if "guidance" in call["prompt"]
    )
    assert all(
        "Reuse existing helpers." not in call["prompt"]
        for call in calls
        if "guidance" not in call["prompt"]
    )
    (root / "calibration.json").write_text("{}")
    with pytest.raises(ValueError, match="changed after the run"):
        study.report_study(root)


def test_calibration_failure_prevents_paid_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _recipe(tmp_path)
    monkeypatch.setattr(pier_backend, "preflight", lambda _: {})
    calls = []

    def calibrate(**kwargs: Any) -> None:
        raise ValueError("calibration failed")

    monkeypatch.setattr(pier_backend, "calibrate", lambda *args: calibrate())
    monkeypatch.setattr(pier_backend, "invoke", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="calibration failed"):
        study.run_study(resolved, tmp_path / "run")
    assert calls == []


@pytest.mark.parametrize("dirty,backend_lost", [(False, False), (True, False), (False, True)])
def test_native_wrapper_fresh_home_flags_and_retained_dirty_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: bool, backend_lost: bool
) -> None:
    commands: list[dict[str, Any]] = []
    base = "a" * 40
    status_calls = 0

    class Environment:
        async def exec(self, *, command: str, **kwargs: Any) -> SimpleNamespace:
            nonlocal status_calls
            commands.append({"command": command, **kwargs})
            output: str | None = None
            if command.endswith("command -v codex"):
                output = "/root/.nvm/bin/codex"
            if command.endswith("command -v node"):
                output = "/root/.nvm/bin/node"
            if command == 'printf "%s" "$PATH"':
                output = "/usr/bin:/bin"
            if "rev-parse" in command:
                output = base + "\n"
            if "status --porcelain" in command:
                status_calls += 1
                if backend_lost and status_calls > 1:
                    raise RuntimeError("collection cannot connect to Docker")
                output = " M src/index.ts\n" if dirty and status_calls > 1 else None
            return SimpleNamespace(stdout=output, return_code=0)

    class Agent:
        skills_dir = None
        mcp_servers: list[object] = []

        def __init__(self, **kwargs: Any) -> None:
            self._extra_env: dict[str, str] = {}

        def build_cli_flags(self) -> str:
            return "--json"

        def network_allowlist(self) -> SimpleNamespace:
            return SimpleNamespace(domains=["api.openai.com"])

        async def exec_as_agent(
            self, environment: Any, command: str, **kwargs: Any
        ) -> SimpleNamespace:
            commands.append(
                {"command": command, "env": {**(kwargs.get("env") or {}), **self._extra_env}}
            )
            if backend_lost and "codex exec --json" in command:
                raise RuntimeError("native command failed with exit code 255")
            output = (
                "codex-cli 0.162.0"
                if command == "codex --version"
                else "--ignore-user-config --ignore-rules --json --model --config"
            )
            return SimpleNamespace(stdout=output, return_code=0)

        async def run(self, instruction: str, environment: Any, context: Any) -> None:
            await self.exec_as_agent(
                environment,
                "codex exec --json -- 'task' 2>&1 </dev/null | tee /logs/agent/codex.txt",
            )

    monkeypatch.setattr(importlib, "import_module", lambda _: SimpleNamespace(Codex=Agent))

    async def export(environment: Any, revision: str, target: Path) -> None:
        target.mkdir()

    monkeypatch.setattr(pier_backend, "_export", export)
    request = {
        "target": str(tmp_path),
        "base_commit": base,
        "codex_version": "0.162.0",
        "prompt": "Frozen guidance",
    }
    agent = pier_backend.build_agent(logs_dir=tmp_path, model_name="gpt-6.1-sol", request=request)
    if backend_lost:
        with pytest.raises(RuntimeError, match="native command failed with exit code 255"):
            asyncio.run(agent.run("unused", Environment(), None))
        assert "exit code 255" in json.loads((tmp_path / "native-error.json").read_text())["error"]
        assert (
            "cannot connect to Docker"
            in json.loads((tmp_path / "collection-error.json").read_text())["error"]
        )
        assert (tmp_path / "before").is_dir()
        return
    asyncio.run(agent.run("unused", Environment(), None))
    preflights = [
        command
        for command in commands
        if command["command"] in ("codex --version", "codex exec --help")
    ]
    assert len(preflights) == 2
    assert all(
        command["env"]["HOME"] == "/home/pluginbench"
        and command["env"]["CODEX_HOME"] == "/home/pluginbench/.codex"
        for command in preflights
    )
    assert "--ignore-user-config" in agent.build_cli_flags()
    assert all(command["env"]["PATH"].startswith("/root/.nvm/bin:") for command in preflights)
    inventory = next(row["command"] for row in commands if row["command"].startswith("find /app"))
    assert "for p in / /tmp /home /home/pluginbench;" in inventory
    assert '"$HOME"' not in inventory
    assert "features.multi_agent=false" in agent.build_cli_flags()
    assert any("2>/logs/agent/codex-stderr.log" in command["command"] for command in commands)
    assert json.loads((tmp_path / "native-exit.json").read_text())["exit_code"] == 0
    submission = json.loads((tmp_path / "submission.json").read_text())
    assert submission["delivery_complete"] is (not dirty)
    assert submission["git_status"] == (" M src/index.ts\n" if dirty else "")
    assert (tmp_path / "expected-model.patch").read_bytes() == b""


def test_verifier_reset_removes_agent_supplied_reward(tmp_path: Path) -> None:
    directory = tmp_path / "verifier"
    directory.mkdir()
    (directory / "reward.json").write_text('{"reward":1}')
    hooks = []
    trial = SimpleNamespace(add_hook=lambda event, callback: hooks.append(callback))
    pier_backend._protect_verifier(
        trial, directory, SimpleNamespace(VERIFICATION_START="verification")
    )
    asyncio.run(hooks[0](None))
    assert directory.is_dir()
    assert list(directory.iterdir()) == []


def test_auth_scrubbing_shared_with_local_native_runner(tmp_path: Path) -> None:
    secret = "a-credential-scalar-long-enough-to-redact"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": secret}}))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "trace.jsonl").write_text(json.dumps({"message": secret}))
    (logs / "binary").write_bytes(b"\xff")
    codex.scrub_artifacts(logs, codex.credential_values(auth))
    assert secret not in (logs / "trace.jsonl").read_text()
    assert (logs / "binary").read_bytes() == b"\xff"
    source = logs / "source.ts"
    original = b"export const first = 1;\r\nexport const second = 2;\r\n"
    source.write_bytes(original)
    codex.scrub_artifacts(logs, ())
    assert source.read_bytes() == original
    handoff = logs / "handoff"
    handoff.mkdir()
    (handoff / "source.ts").write_text(secret)
    (logs / "handoff-export.json").write_text("{}")
    (logs / "expected-model.patch").write_text(secret)
    pier_backend._redact_attempt(logs, auth)
    assert not handoff.exists()
    assert not (logs / "handoff-export.json").exists()
    assert not (logs / "expected-model.patch").exists()


@pytest.mark.parametrize(
    "patch_matches,reward,expected,partial_export",
    [
        (True, 1, True, False),
        (True, 0, False, False),
        (False, 1, None, False),
        (True, -1, None, False),
        (True, 0.5, None, False),
        (True, 1, None, True),
    ],
)
def test_trial_result_requires_collected_patch_and_binary_reward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_matches: bool,
    reward: float,
    expected: bool | None,
    partial_export: bool,
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    raw = tmp_path / "pier/native/agent"
    (raw / "sessions").mkdir(parents=True)
    (raw / "sessions/trace.jsonl").write_text(
        json.dumps(
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "medium"}}
        )
    )
    (raw / "codex.txt").write_text(
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}})
    )
    (raw / "native-exit-code.txt").write_text("0\n")
    (tmp_path / "handoff").mkdir()
    pier_backend._write(
        tmp_path / "handoff-export.json",
        {"snapshot_digest": inspect_snapshot(tmp_path / "handoff")["digest"]},
    )
    if partial_export:
        (tmp_path / "handoff/link.ts").symlink_to("/outside-source")
    (tmp_path / "expected-model.patch").write_text("trusted patch\n")
    artifacts = tmp_path / "pier/native/artifacts"
    artifacts.mkdir()
    (artifacts / "model.patch").write_text("trusted patch\n" if patch_matches else "forged patch\n")
    (tmp_path / "submission.json").write_text('{"delivery_complete":true}')
    verifier = tmp_path / "pier/native/verifier"
    verifier.mkdir()
    (verifier / "reward.json").write_text('{"reward":1}')

    class Trial:
        @classmethod
        async def create(cls, config: Any) -> Trial:
            return cls()

        def add_hook(self, event: Any, callback: Any) -> None:
            self.callback = callback

        async def run(self) -> SimpleNamespace:
            await self.callback(None)
            assert list(verifier.iterdir()) == []
            return SimpleNamespace(
                exception_info=None, verifier_result=SimpleNamespace(rewards={"reward": reward})
            )

    original = importlib.import_module

    def module(name: str) -> Any:
        if name == "pier.trial.trial":
            return SimpleNamespace(
                Trial=Trial, TrialEvent=SimpleNamespace(VERIFICATION_START="verify")
            )
        if name == "pier.models.trial.config":
            return SimpleNamespace(TrialConfig=lambda **kwargs: kwargs)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", module)
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.3.1")
    result = asyncio.run(
        pier_backend._run(
            {
                "target": str(tmp_path),
                "task_dir": "/frozen/task",
                "model": "gpt-6.1-sol",
                "timeout": 10,
                "auth_file": str(auth),
                "codex_version": "0.162.0",
                "reasoning": "medium",
            }
        )
    )
    assert result["checks"]["behavior"] is expected
    assert result["checks"]["submission_collected"] is (patch_matches and not partial_export)
    assert result["execution"]["usage"] == {"input_tokens": 10, "output_tokens": 3}
    assert result["execution"]["exit_code"] == 0
    assert result["execution"]["actual_primary_models"] == ["gpt-6.1-sol"]
    assert (
        result["handoff"] is None
        if not patch_matches or partial_export
        else result["handoff"] is not None
    )


def test_calibration_receipt_bound_to_frozen_task_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = pier_backend.load_tasks(_catalog(tmp_path))
    calls = []

    def execute(target: Path, request: dict[str, Any], python: Path) -> dict[str, Any]:
        calls.append(request)
        return {"passed": False, "checks": {"nop": {"reward": 0}, "oracle": {"reward": 0}}}

    monkeypatch.setattr(pier_backend, "_execute_request", execute)
    with pytest.raises(ValueError, match="no paid attempts launched"):
        pier_backend.calibrate(tasks, tmp_path / "calibration", Path("python"))
    receipt = json.loads((tmp_path / "calibration/calibration.json").read_text())
    assert receipt["paid_calls"] == 0
    assert receipt["tasks"][0]["task_digest"] == tasks[0].digest
    assert len(calls) == 1


@pytest.mark.parametrize("unsupported", [False, True])
def test_source_export_reads_raw_blobs_despite_git_attributes(
    tmp_path: Path, unsupported: bool
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], stderr=subprocess.PIPE
        )

    git("init")
    git("config", "user.email", "offline@example.com")
    git("config", "user.name", "Offline test")
    (repository / "src").mkdir()
    contents = b"const first = 1;\nconst second = 2;\nconst third = 3;\n"
    (repository / "src/code.ts").write_bytes(contents)
    (repository / "src/subst.ts").write_text("export const value = '$Format:%H$';\n")
    (repository / ".gitattributes").write_text(
        "src/code.ts filter=compact export-ignore\nsrc/subst.ts export-subst\n"
    )
    git("config", "filter.compact.smudge", "head -n 1")
    if unsupported:
        (repository / "src/link.ts").symlink_to("code.ts")
    git("add", ".")
    git("commit", "-m", "Fixture")
    head = git("rev-parse", "HEAD").decode().strip()

    class Environment:
        async def exec(self, *, command: str, **kwargs: Any) -> SimpleNamespace:
            result = subprocess.run(
                command.replace("/app", str(repository)),
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            return SimpleNamespace(return_code=result.returncode, stdout=result.stdout)

        async def download_file(self, remote: str, local: Path) -> None:
            shutil.copyfile(remote, local)

    if unsupported:
        with pytest.raises(RuntimeError, match="adapter command failed"):
            asyncio.run(
                pier_backend._export(Environment(), head, tmp_path / "raw-export-unsupported")
            )
        assert not (tmp_path / "raw-export-unsupported").exists()
    else:
        target = tmp_path / "raw-export"
        asyncio.run(pier_backend._export(Environment(), head, target))
        assert (target / "src/code.ts").read_bytes() == git("show", "HEAD:src/code.ts") == contents
        assert (target / "src/subst.ts").read_bytes() == git("show", "HEAD:src/subst.ts")


def test_backend_loss_keeps_bound_base_and_live_runtime_evidence(tmp_path: Path) -> None:
    before = tmp_path / "before"
    before.mkdir()
    (before / "index.ts").write_text("export const answer = 0;\n")
    pier_backend._write(
        tmp_path / "before-export.json", {"snapshot_digest": inspect_snapshot(before)["digest"]}
    )
    live = tmp_path / "pier/native/agent/live-sessions"
    live.mkdir(parents=True)
    trace = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol", "effort": "medium"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 50, "output_tokens": 7}},
                },
            },
        ]
    )
    (live / "trace.jsonl").write_text(trace)
    result = pier_backend._failure("Docker unreachable", tmp_path)
    assert result["before"] == str(before)
    assert result["handoff"] is None
    assert result["execution"]["usage"] is None
    assert result["execution"]["partial_usage"][0]["tokens"] == {
        "input_tokens": 50,
        "output_tokens": 7,
    }
    assert result["execution"]["actual_primary_models"] == ["gpt-6.1-sol"]
    assert result["execution"]["runtime_effort_evidence"] == ["medium"]
    assert result["checks"]["behavior"] is None


def test_backend_cancellation_stops_process_before_redacting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "fake-oauth-credential-long-enough-to-redact"
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": secret}}))
    target = tmp_path / "attempt"
    target.mkdir()
    events = []

    class Process:
        pid = 987654
        returncode: int | None = None

        def wait(self, timeout: int | None = None) -> int:
            events.append("wait")
            if self.returncode is None:
                raise KeyboardInterrupt()
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

    process = Process()

    def start(*args: Any, **kwargs: Any) -> Process:
        kwargs["stderr"].write(secret)
        kwargs["stderr"].flush()
        return process

    def stop(pid: int, sig: int) -> None:
        events.append("stop")
        process.returncode = -15

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(os, "killpg", stop)

    def cleanup(*args: Any) -> dict[str, Any]:
        events.append("containers stopped")
        return {"status": "complete"}

    monkeypatch.setattr(pier_backend, "_stop_attempt_containers", cleanup)
    with pytest.raises(KeyboardInterrupt):
        pier_backend._execute_request(target, {"auth_file": str(auth)}, Path("python3"))
    assert events == ["wait", "stop", "wait", "containers stopped"]
    assert secret not in (target / "backend-stderr.log").read_text()
    assert "<redacted-credential>" in (target / "backend-stderr.log").read_text()


def test_calibration_checkpoint_survives_preflight_alias_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = _recipe(tmp_path)
    root = tmp_path / "run"
    monkeypatch.setattr(
        pier_backend, "preflight", lambda _: {"pier_version": "test", "paid_calls": 0}
    )

    def calibration(*args: Any) -> dict[str, Any]:
        study.report_study(root)
        run = json.loads((root / "run.json").read_text())
        assert run["status"] == "calibration"
        (root / "runtime-preflight.json").write_text("{}")
        study.report_study(root)
        raise KeyboardInterrupt()

    monkeypatch.setattr(pier_backend, "calibrate", calibration)
    with pytest.raises(KeyboardInterrupt):
        study.run_study(resolved, root)
    study.report_study(root)
    assert json.loads((root / "run.json").read_text())["actual_calls"] == 0


def test_cancellation_cleanup_never_stops_unrelated_native_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = "a" * 12
    other = "b" * 12
    verifier = "c" * 12
    commands = []
    target = tmp_path / "attempt"

    def docker(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[1] == "ps":
            assert argv[-1] == "label=com.docker.compose.project"
            return subprocess.CompletedProcess(
                argv, 0, own + "\n" + other + "\n" + verifier + "\n", ""
            )
        if argv[1] == "inspect":
            path = (
                target / "pier/native/docker-compose-mounts.json"
                if argv[-1] in (own, verifier)
                else tmp_path / "unrelated/docker-compose-mounts.json"
            )
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "com.docker.compose.project.config_files": str(path),
                        "com.docker.compose.project": "native__verifier__key"
                        if argv[-1] == verifier
                        else "native",
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(argv, 0, own + "\n", "")

    monkeypatch.setattr(subprocess, "run", docker)
    result = pier_backend._stop_attempt_containers(target, {"PATH": "/usr/bin"})
    assert result["status"] == "complete"
    assert result["removed_owned_containers"] == [own, verifier]
    assert [argv for argv in commands if argv[1] == "rm"] == [
        ["docker", "rm", "-f", own],
        ["docker", "rm", "-f", verifier],
    ]
