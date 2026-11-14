"""Resume keeps every finished attempt, discards only started ones, and never replays a call.

Killed runs come from a child process that SIGKILLs itself at a named boundary, so no except
or finally block runs, as when the interrupted pilot was killed.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import pluginbench
import native_support
import resume_support
from legacy_support import current_runtime, legacy_json
from native_support import execution as native_execution
from pluginbench import harbor_backend, ledger, parallel, run_status, study, trial_containers
from pluginbench.ledger import Ledger
from pluginbench import codex
from test_codex import _OUTAGE, outage_trace, stream
from test_provider_stops import CODEX_LIMITED_STDOUT, codex_rollout

_REAL_RUNNING = trial_containers.running
_PATH = os.pathsep.join([str(Path(pluginbench.__file__).parents[1]), str(Path(__file__).parent)])


def _killed_run(
    tmp_path: Path,
    *,
    boundary: str,
    at: int,
    tasks: int = 2,
    treatments: int = 1,
    repeats: int = 3,
    jobs: int = 1,
    cache: Path | None = None,
) -> Path:
    recipe = resume_support.recipe(
        tmp_path / "config", tasks=tasks, treatments=treatments, repeats=repeats, jobs=jobs
    )
    root = tmp_path / "run"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resume_support; resume_support.child()",
            str(recipe),
            str(root),
            boundary,
            str(at),
            *([str(cache)] if cache else []),
        ],
        env={**os.environ, "PYTHONPATH": _PATH},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    return root


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    ("boundary", "kept", "discarded"),
    [
        ("before-receipt", 2, 0),
        ("during-call", 2, 1),
        ("after-execution", 2, 1),
        ("after-record", 3, 0),
        ("after-ledger", 3, 0),
    ],
)
def test_resume_after_sigkill_at_each_stage_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, kept: int, discarded: int
) -> None:
    root = _killed_run(tmp_path, boundary=boundary, at=3)
    assert _json(root / "run.json")["status"] == "running"
    killed = _json(root / "plan.json")["entries"]
    finished = [entry["id"] for entry in killed[:kept]]
    receipts = {key: (root / "records" / f"{key}.json").read_bytes() for key in finished}
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    study.resume_study(root)

    run = _json(root / "run.json")
    plan = _json(root / "plan.json")
    assert run["status"] == "complete"
    assert Counter(run["attempt_states"].values()) == {"complete": 12}
    # Finished attempts keep their receipts and are never coded again.
    assert not set(calls) & set(finished)
    assert len(calls) == len(set(calls)) == 12 - kept
    assert {key: (root / "records" / f"{key}.json").read_bytes() for key in finished} == receipts
    assert len(run["discarded_attempts"]) == discarded
    third = killed[2]
    if discarded:
        old = run["discarded_attempts"][third["id"]]
        assert old["state"] == "discarded-interrupted"
        stage = old["stages"]["handoff"]
        if boundary == "during-call":
            assert stage["usage_status"].startswith("unknown") and stage["work"] is None
        else:
            assert stage["usage_status"] == "saved before the interruption"
            assert stage["work"]["tokens"] is not None
        replacement = plan["entries"][2]
        assert old["replaced_by"] == replacement["id"] != third["id"]
        assert {key: replacement[key] for key in ("task_id", "arm", "attempt")} == {
            key: third[key] for key in ("task_id", "arm", "attempt")
        }
        assert replacement["id"] in calls
    else:
        assert plan["entries"][2]["id"] == third["id"]
        assert (third["id"] in calls) is (kept == 2)
    rows = Ledger.open(root / "ledger.sqlite").attempts()
    assert Counter(row["state"] for row in rows) == {
        "complete": 12,
        **({"discarded-interrupted": discarded} if discarded else {}),
    }
    report = _json(root / "report/assessment-report.json")
    assert len(report["attempts"]) == 12
    assert set(report["discarded_attempts"]) == set(run["discarded_attempts"])


class _Docker:
    """A Docker engine holding labelled containers; answers only `ps` and `stop`."""

    def __init__(self, containers: list[dict[str, Any]]) -> None:
        self.containers = containers
        self.stopped: list[list[str]] = []
        self.runtimes: list[object] = []

    def __call__(self, *arguments: str, expected_runtime: object = None, **kwargs: Any) -> str:
        self.runtimes.append(expected_runtime)
        if arguments[0] == "stop":
            ids = list(arguments[3:])
            self.stopped.append(ids)
            for container in self.containers:
                if container["id"] in ids:
                    container["running"] = False
            return "\n".join(ids)
        assert arguments[0] == "ps", arguments
        listed = [
            container
            for container in self.containers
            # Without --all, `docker ps` lists running containers only.
            if container["running"] or "--all" in arguments or "-a" in arguments
        ]
        return "".join(
            json.dumps([row["id"], row["name"], row["project"], row["working_dir"]]) + "\n"
            for row in listed
        )


def _container(
    container: str, project: str, working_dir: str, *, running: bool = True
) -> dict[str, Any]:
    name = f"{project}-{container}-1"
    return {
        "id": container,
        "name": name,
        "project": project,
        "working_dir": working_dir,
        "running": running,
    }


def test_resume_stops_only_the_containers_its_killed_attempt_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _killed_run(tmp_path, boundary="during-call", at=3)
    task = str((root / "frozen/suite/0/task").resolve())
    other_run = str(tmp_path / "other-run/evidence/frozen/suite/0/task/environment")
    killed = "pluginbench-" + "a" * 32 + "__env"
    finished = "pluginbench-" + "b" * 32 + "__env"
    foreign = "pluginbench-" + "c" * 32 + "__env"
    here = task + "/environment"
    docker = _Docker(
        [
            # The killed attempt's agent and its egress sidecar share one compose project.
            _container("main", killed, here),
            _container("sidecar", killed, here),
            # Already stopped: never stopped again.
            _container("exited", finished, here, running=False),
            # Another run's trial, and a non-trial project inside this run: never touched.
            _container("foreign", foreign, other_run),
            _container("measure", "pluginbench-measure-1", here),
        ]
    )
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(trial_containers, "running", _REAL_RUNNING)
    monkeypatch.setattr(harbor_backend, "_docker", docker)
    progress: list[str] = []

    study.resume_study(root, progress=progress.append)

    assert docker.stopped == [["main", "sidecar"]]
    assert {row["id"] for row in docker.containers if row["running"]} == {"foreign", "measure"}
    stopped = f"Stopped {killed} (2 containers): left running by an attempt that a kill interrupted"
    # Named before the first attempt is coded again.
    coding = next(index for index, line in enumerate(progress) if line.startswith("Coding"))
    assert stopped in progress[:coding]
    # Every Docker call runs with the runtime the resume verified, never the ambient context.
    assert docker.runtimes and None not in docker.runtimes
    assert _json(root / "run.json")["status"] == "complete"


def test_a_left_container_removed_while_it_is_stopped_does_not_stop_the_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    environment = evidence / "frozen/suite/0/task/environment"
    environment.mkdir(parents=True)
    project = "pluginbench-" + "a" * 32 + "__env"
    container = _container("main", project, str(environment.resolve()))
    docker = _Docker([container])
    errors = iter(["No such container: main", "permission denied"])

    def failing_stop(*arguments: str, **kwargs: Any) -> str:
        if arguments[0] != "stop":
            return docker(*arguments, **kwargs)
        error = next(errors)
        if error.startswith("No such"):
            docker.containers.clear()  # The killed run's Harbor worker removed it first.
        raise ValueError(f"docker stop failed: {error}")

    monkeypatch.setattr(harbor_backend, "_docker", failing_stop)
    progress: list[str] = []

    trial_containers.stop_left_running(evidence, expected_runtime={}, progress=progress.append)
    assert progress == []
    # A container that is still running after a failed stop stops the resume before coding.
    docker.containers.append(container)
    with pytest.raises(ValueError, match="permission denied"):
        trial_containers.stop_left_running(evidence, expected_runtime={}, progress=progress.append)


def test_interrupted_pilot_shape_resumes_without_recoding_complete_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pilot's shape: 4 tasks x 3 arms x 3 repeats, killed with 2 calls in flight."""
    root = _killed_run(
        tmp_path, boundary="two-in-flight", at=20, tasks=4, treatments=2, repeats=3, jobs=2
    )
    # Runs from before the ledger kept their states only in run.json.
    (root / "ledger.sqlite").unlink()
    shutil.rmtree(root / "records")
    killed = _json(root / "run.json")
    killed.pop("discarded_attempts")
    (root / "run.json").write_text(json.dumps(killed))
    states = killed["attempt_states"]
    assert killed["status"] == "running"
    assert Counter(states.values()) == {"complete": 19, "running": 2, "pending": 15}
    running = sorted(key for key, state in states.items() if state == "running")
    complete = {key for key, state in states.items() if state == "complete"}
    assert all((root / "invocations" / key / "handoff.json").is_file() for key in running)
    assert not any((root / "attempts" / key).exists() for key in running)
    saved = {
        record["id"]: record
        for record in study._bound_document(root, killed["assessment_input"])["records"]
    }
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    study.resume_study(root)

    run = _json(root / "run.json")
    assert run["status"] == "complete"
    assert len(calls) == 17 and not set(calls) & complete
    assert sorted(run["discarded_attempts"]) == running
    assert all(
        row["state"] == "discarded-interrupted"
        and row["stages"]["handoff"]["usage_status"].startswith("unknown")
        and row["replaced_by"] in calls
        for row in run["discarded_attempts"].values()
    )
    assert Counter(run["attempt_states"].values()) == {"complete": 36}
    resumed = {
        record["id"]: record
        for record in study._bound_document(root, run["assessment_input"])["records"]
    }
    assert all(resumed[key] == saved[key] for key in complete)
    assert run["actual_calls"] == 21 + 17


def test_work_finished_while_a_stop_drains_is_kept_with_interrupted_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Barrier, Event, Lock

    recipe = resume_support.recipe(tmp_path / "config", tasks=2, treatments=1, repeats=1, jobs=2)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    gate, lock = Barrier(2), Lock()
    cancellation = Event()
    monkeypatch.setattr(parallel, "Event", lambda: cancellation)
    usage = {"input_tokens": 10, "output_tokens": 5}

    def invoke(**kwargs: Any) -> dict[str, Any]:
        with lock:
            calls.append(kwargs["entry"]["id"])
            ordinal = len(calls)
        gate.wait(timeout=5)
        area = kwargs["area"]
        if ordinal == 1:
            (area / "artifacts").mkdir()
            (area / "artifacts/execution.json").write_text(json.dumps({"usage": usage}))
            raise KeyboardInterrupt("stopped by SIGTERM")
        assert cancellation.wait(timeout=5)
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        return native_execution(area, kwargs["protocol"]["native_task_identity"], reward=1)

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke)
    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        study.run_study(study.load_study(recipe), root)
    states = _json(root / "run.json")["attempt_states"]
    stopped, drained = calls
    assert states[stopped] == "interrupted"
    assert states[drained] == "complete"
    ledger = Ledger.open(root / "ledger.sqlite")
    assert {row["id"]: row["state"] for row in ledger.attempts()} == states
    ledger.close()

    calls.clear()
    monkeypatch.setattr(parallel, "Event", Event)  # The captured event is already set.
    resume_support.install(monkeypatch, calls)
    study.resume_study(root)
    run = _json(root / "run.json")
    assert drained not in calls and len(calls) == 3
    assert run["discarded_attempts"][stopped]["stages"]["handoff"]["work"]["tokens"] == usage
    assert Counter(run["attempt_states"].values()) == {"complete": 4}


def _vanilla_first(tmp_path: Path) -> study.ResolvedStudy:
    """12 attempts whose first is vanilla, so a scored failure of it would be cached."""
    seed = next(
        seed
        for seed in range(100)
        if study.plan_study(
            study.load_study(
                resume_support.recipe(
                    tmp_path / f"probe-{seed}", tasks=2, treatments=1, repeats=3, jobs=1, seed=seed
                )
            )
        )["entries"][0]["arm"]
        == "baseline"
    )
    return study.load_study(
        resume_support.recipe(
            tmp_path / "config", tasks=2, treatments=1, repeats=3, jobs=1, seed=seed
        )
    )


@pytest.mark.parametrize("startup", ["header", "user_input", "rate_limit"])
def test_backend_failure_before_the_first_turn_stops_the_run_unscored_and_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup: str,
) -> None:
    resolved = _vanilla_first(tmp_path)
    cache, root = tmp_path / "cache", tmp_path / "run"
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def outage(**kwargs: Any) -> dict[str, Any]:
        # The shape of a Codex session cut off from its backend: an empty patch, no usage.
        calls.append(kwargs["entry"]["id"])
        execution = native_execution(
            kwargs["area"], kwargs["protocol"]["native_task_identity"], reward=0
        )
        execution.update(codex.parse_events(stream(*_OUTAGE), [outage_trace(startup)], "codex"))
        execution["timed_out"] = False
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", outage)
    with pytest.raises(study.BackendUnavailable, match="workspace routing discovery failed") as error:
        study.run_study(
            resolved,
            root,
            baseline_cache=cache,
            baseline_identity=resume_support.vanilla_identity(resolved),
        )
    assert " · vanilla · repeat " in str(error.value)
    assert " · baseline · " not in str(error.value)
    [failed] = calls
    assert _json(root / "run.json")["attempt_states"][failed] == "interrupted"
    assert not (root / "records" / f"{failed}.json").exists()
    assert not list(cache.glob("*/*/manifest.json"))

    calls.clear()
    resume_support.install(monkeypatch, calls)
    study.resume_study(
        root,
        baseline_cache=cache,
        baseline_identity=resume_support.vanilla_identity(study._frozen_study(root)),
    )
    run = _json(root / "run.json")
    assert list(run["discarded_attempts"]) == [failed]
    assert run["discarded_attempts"][failed]["replaced_by"] in calls
    assert len(calls) == 12 and failed not in calls
    assert Counter(run["attempt_states"].values()) == {"complete": 12}
    assert len(list(cache.glob("*/*/manifest.json"))) == 6


def test_a_codex_usage_limit_mid_session_stops_the_run_unscored_and_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _vanilla_first(tmp_path)
    cache, root = tmp_path / "cache", tmp_path / "run"
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def limited(**kwargs: Any) -> dict[str, Any]:
        # The agent ran a command and changed a file before the limit cut the turn off.
        calls.append(kwargs["entry"]["id"])
        area = kwargs["area"]
        (area / "workspace/src/answer.py").write_text("answer = 1\n")
        execution = native_execution(
            area, kwargs["protocol"]["native_task_identity"], reward=0
        )
        trace = codex_rollout("usage_limit_exceeded")
        (area / "artifacts/stdout.jsonl").write_text(CODEX_LIMITED_STDOUT)
        (area / "artifacts/host-trace-0.jsonl").write_text(trace)
        execution.update(codex.parse_events(CODEX_LIMITED_STDOUT, [trace], "codex"))
        execution["timed_out"] = False
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", limited)
    with pytest.raises(
        study.BackendUnavailable,
        match=r"codex stopped on a usage or rate limit \(usage_limit_exceeded\): You’ve hit",
    ) as error:
        study.run_study(
            resolved,
            root,
            baseline_cache=cache,
            baseline_identity=resume_support.vanilla_identity(resolved),
        )
    assert " · vanilla · repeat " in str(error.value)
    [failed] = calls
    run = _json(root / "run.json")
    assert run["attempt_states"][failed] == "interrupted"
    # `pluginbench run` records the same error as its `failed` event, which `status` prints.
    assert "usage or rate limit (usage_limit_exceeded)" in run["error"]
    execution = _json(root / "attempts" / failed / "handoff/execution.json")
    assert execution["command_items"] and execution["provider_stop"].startswith("a usage")
    assert not (root / "attempts" / failed / "handoff/correctness.json").exists()
    assert not (root / "records" / f"{failed}.json").exists()
    assert not list(cache.glob("*/*/manifest.json"))

    calls.clear()
    resume_support.install(monkeypatch, calls)
    study.resume_study(
        root,
        baseline_cache=cache,
        baseline_identity=resume_support.vanilla_identity(study._frozen_study(root)),
    )
    run = _json(root / "run.json")
    assert list(run["discarded_attempts"]) == [failed]
    assert run["discarded_attempts"][failed]["replaced_by"] in calls
    assert Counter(run["attempt_states"].values()) == {"complete": 12}
    assert len(list(cache.glob("*/*/manifest.json"))) == 6


def test_resume_reads_a_moved_copy_from_its_own_frozen_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _killed_run(tmp_path, boundary="during-call", at=2)
    moved = tmp_path / "moved"
    shutil.copytree(original, moved, symlinks=True)
    # Anything still read from the original location would now fail.
    shutil.rmtree(tmp_path / "config")
    shutil.rmtree(original)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    paths: list[str] = []
    invoke = harbor_backend.invoke_native

    def recording(**kwargs: Any) -> dict[str, Any]:
        paths.append(str(kwargs["task_dir"]))
        paths.append(str(kwargs["protocol"]["native_task_identity"]["task_dir"]))
        return invoke(**kwargs)

    monkeypatch.setattr(harbor_backend, "invoke_native", recording)
    study.resume_study(moved)
    assert len(calls) == 11
    assert paths and all(path.startswith(str(moved.resolve())) for path in paths)


def test_vanilla_attempts_finished_before_a_kill_are_already_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    root = _killed_run(tmp_path, boundary="during-call", at=7, cache=cache)
    entries = _json(root / "plan.json")["entries"]
    finished = {entry["id"] for entry in entries[:6] if entry["arm"] == "baseline"}
    assert finished
    cached = [_json(path)["records"][0]["id"] for path in cache.glob("*/*/manifest.json")]
    assert sorted(cached) == sorted(finished)

    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    resolved = study._frozen_study(root)
    study.resume_study(
        root, baseline_cache=cache, baseline_identity=resume_support.vanilla_identity(resolved)
    )
    assert not set(calls) & finished
    assert len(list(cache.glob("*/*/manifest.json"))) == 6


def _killed_resume(root: Path, boundary: str) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resume_support; resume_support.child_resume()",
            str(root),
            boundary,
        ],
        env={**os.environ, "PYTHONPATH": _PATH},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr


def _as_legacy_run(root: Path) -> None:
    """Remove what the ledger added, leaving states only in run.json as older runs had."""
    (root / "ledger.sqlite").unlink()
    shutil.rmtree(root / "records")
    run = _json(root / "run.json")
    run.pop("discarded_attempts")
    (root / "run.json").write_text(json.dumps(run))


def test_resume_killed_while_it_rewrites_the_plan_can_resume_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _killed_run(tmp_path, boundary="during-call", at=3)
    finished = [entry["id"] for entry in _json(root / "plan.json")["entries"][:2]]
    _killed_resume(root, "after-plan-write")
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    study.resume_study(root)

    run = _json(root / "run.json")
    assert run["status"] == "complete" and len(run["discarded_attempts"]) == 1
    assert len(calls) == 10 and not set(calls) & set(finished)


def test_resume_killed_while_it_builds_a_legacy_ledger_recovers_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _killed_run(tmp_path, boundary="during-call", at=3)
    _as_legacy_run(root)
    finished = [entry["id"] for entry in _json(root / "plan.json")["entries"][:2]]
    _killed_resume(root, "ledger-insert")
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    study.resume_study(root)

    run = _json(root / "run.json")
    assert run["status"] == "complete" and Counter(run["attempt_states"].values()) == {
        "complete": 12
    }
    assert len(calls) == 10 and not set(calls) & set(finished)


def test_moving_the_run_during_a_call_never_writes_to_the_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import run_lock

    recipe = resume_support.recipe(tmp_path / "config", tasks=1, treatments=1, repeats=1, jobs=1)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    invoke = harbor_backend.invoke_native
    owned, moved = tmp_path / "run", tmp_path / "moved"
    owned.mkdir()

    def moving(**kwargs: Any) -> dict[str, Any]:
        result = invoke(**kwargs)
        owned.rename(moved)  # The operator moves the run while its call runs.
        return result

    monkeypatch.setattr(harbor_backend, "invoke_native", moving)
    with run_lock.hold(owned) as lock:
        with pytest.raises(run_lock.RunMoved):
            study.run_study(
                study.load_study(recipe), owned / "evidence", guard=lock.assert_in_place
            )
    assert not owned.exists()
    # The call started before the move, so its receipt is in the moved run; resume discards it.
    assert (moved / "evidence/invocations" / calls[0] / "handoff.json").is_file()
    (moved / "run.lock").unlink()
    resume_support.install(monkeypatch, calls)
    study.resume_study(moved / "evidence")
    assert set(_json(moved / "evidence/run.json")["discarded_attempts"]) == {calls[0]}


def test_moving_the_run_during_preflight_never_writes_to_the_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pluginbench import run_lock

    recipe = resume_support.recipe(tmp_path / "config", tasks=1, treatments=1, repeats=1, jobs=1)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    preflight = harbor_backend.preflight
    owned, moved = tmp_path / "run", tmp_path / "moved"
    owned.mkdir()

    def moving(**kwargs: Any) -> dict[str, Any]:
        result = preflight(**kwargs)
        owned.rename(moved)  # The operator moves the run while Docker is checked.
        return result

    monkeypatch.setattr(harbor_backend, "preflight", moving)
    with run_lock.hold(owned) as lock:
        with pytest.raises(run_lock.RunMoved):
            study.run_study(
                study.load_study(recipe), owned / "evidence", guard=lock.assert_in_place
            )
    assert not owned.exists()
    assert calls == []


@pytest.mark.parametrize("obstacle", ["undeclared-launcher", "busy-lock"])
def test_a_run_with_nothing_left_to_code_finishes_without_its_vanilla_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, obstacle: str
) -> None:
    # Every attempt finished; caching a finished vanilla repeat must not decide completion.
    cache = tmp_path / "cache"
    root = _killed_run(tmp_path, boundary="after-ledger", at=12, cache=cache)
    identity = resume_support.vanilla_identity(study._frozen_study(root))
    held = []
    if obstacle == "undeclared-launcher":
        identity["launcher_sha256"] = {"codex.py": "f" * 64}
    else:
        for path in sorted((cache / ".locks").glob("*.lock")):
            handle = path.open("a")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(handle)
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    messages: list[str] = []
    try:
        study.resume_study(
            root, progress=messages.append, baseline_cache=cache, baseline_identity=identity
        )
    finally:
        for handle in held:
            handle.close()
    assert _json(root / "run.json")["status"] == "complete"
    assert calls == []
    kind = "UnknownSourceError" if obstacle == "undeclared-launcher" else "ValueError"
    prefix = f"Some finished vanilla repeats were not cached in {cache}: {kind}: "
    assert any(line.startswith(prefix) for line in messages)


def test_resume_that_fails_before_coding_starts_closes_its_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _killed_run(tmp_path, boundary="after-ledger", at=12, cache=tmp_path / "cache")
    closed: list[Ledger] = []
    close = Ledger.close

    def recording(ledger: Ledger) -> None:
        closed.append(ledger)
        close(ledger)

    monkeypatch.setattr(Ledger, "close", recording)

    def failing(message: str) -> None:
        raise RuntimeError("the progress sink is gone")

    with pytest.raises(RuntimeError, match="progress sink"):
        study.resume_study(root, progress=failing)
    assert len(closed) == 1


def test_vanilla_attempt_killed_before_it_was_cached_is_cached_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A seed whose last attempt is vanilla, so the kill leaves no coding to resume.
    seed = next(
        seed
        for seed in range(100)
        if study.plan_study(
            study.load_study(
                resume_support.recipe(
                    tmp_path / f"probe-{seed}", tasks=2, treatments=1, repeats=3, jobs=1, seed=seed
                )
            )
        )["entries"][-1]["arm"]
        == "baseline"
    )
    recipe = resume_support.recipe(
        tmp_path / "config-seeded", tasks=2, treatments=1, repeats=3, jobs=1, seed=seed
    )
    cache, root = tmp_path / "cache", tmp_path / "run"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resume_support; resume_support.child()",
            str(recipe),
            str(root),
            "before-publish",
            "12",
            str(cache),
        ],
        env={**os.environ, "PYTHONPATH": _PATH},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    assert len(list(cache.glob("*/*/manifest.json"))) == 5
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    study.resume_study(
        root,
        baseline_cache=cache,
        baseline_identity=resume_support.vanilla_identity(study._frozen_study(root)),
    )

    assert calls == []
    assert len(list(cache.glob("*/*/manifest.json"))) == 6


def test_a_942f12c_vanilla_run_resumes_after_docker_memory_moved_by_28_kib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The incident: a resume with vanilla reuse was refused because Docker reported 28 KiB
    # less memory than at preparation. Only the saved runtime checks are real here.
    cache = tmp_path / "cache"
    saved = legacy_json("runtime.json")
    # The tasks were prepared on the same 942f12c runtime, on the engine the child runs on.
    prepared = legacy_json("runtime.json")
    prepared["docker_engine"]["id"] = "daemon-one"
    with monkeypatch.context() as tasks:
        tasks.setattr(native_support, "runtime_identity", lambda: prepared)
        root = _killed_run(tmp_path, boundary="during-call", at=7, cache=cache)
    calls: list[str] = []
    check = harbor_backend.assert_runtime
    resume_support.install(monkeypatch, calls)
    monkeypatch.setattr(harbor_backend, "assert_runtime", check)
    current = current_runtime(saved)
    current["docker_engine"]["id"] = "daemon-one"
    monkeypatch.setattr(harbor_backend, "runtime_identity", lambda **kwargs: current)
    resolved = study._frozen_study(root)
    identity = {**resume_support.vanilla_identity(resolved), "isolation": saved}
    study.resume_study(root, baseline_cache=cache, baseline_identity=identity)
    assert _json(root / "run.json")["status"] == "complete"
    assert len(calls) == 6


# The command a timed-out Harbor worker ran; its paths must not reach the saved error.
_WORKER = ["python", "-m", "pluginbench.harbor_backend", "/private/trial.json", "/a/worker.json"]
_USAGE = {"input_tokens": 10, "output_tokens": 5}


# Set by a test whose workers hang after Harbor collected the patch, as in a hung verifier.
_COLLECTED = {"patch": False}


def _overrun(kwargs: dict[str, Any]) -> None:
    """A worker that overran its budget after the agent used tokens, as Harbor records it."""
    artifacts = kwargs["area"] / "artifacts"
    artifacts.mkdir()
    (artifacts / "execution.json").write_text(
        json.dumps({"usage": _USAGE, "timed_out": True, "submission_collected": _COLLECTED["patch"]})
    )
    raise subprocess.TimeoutExpired(_WORKER, 5400)


# A patch that Harbor collected before the worker hung has no verifier result. It must stay
# ungraded and unknown: graded without one, the attempt would be scored as a fail.
@pytest.mark.parametrize("collected", [False, True])
def test_a_worker_that_overruns_its_budget_is_kept_ungraded_and_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collected: bool
) -> None:
    monkeypatch.setitem(_COLLECTED, "patch", collected)
    resolved = _vanilla_first(tmp_path)
    failed = study.plan_study(resolved)["entries"][0]["id"]
    cache, run = tmp_path / "cache", tmp_path / "run"
    root = run / "evidence"
    run.mkdir()
    (run / "evaluation.json").write_text(json.dumps({"name": "fixture"}))
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    invoke = harbor_backend.invoke_native

    def first_overruns(**kwargs: Any) -> dict[str, Any]:
        if kwargs["entry"]["id"] != failed:
            return invoke(**kwargs)
        calls.append(failed)
        _overrun(kwargs)
        raise AssertionError("unreachable")

    monkeypatch.setattr(harbor_backend, "invoke_native", first_overruns)
    identity = resume_support.vanilla_identity(resolved)
    study.run_study(resolved, root, baseline_cache=cache, baseline_identity=identity)

    saved = _json(root / "run.json")
    assert saved["status"] == "complete" and len(calls) == 12  # The other attempts were coded.
    assert Counter(saved["attempt_states"].values()) == {"complete": 12}
    reason = "the Harbor worker exceeded its budget of 5400 seconds"
    assert _json(root / "attempts" / failed / "handoff/execution.json")["error"] == reason
    report = _json(root / "report/assessment-report.json")
    stage = next(row for row in report["attempts"] if row["id"] == failed)["stages"][0]
    assert stage["snapshot_digest"] is None and stage["correctness"]["status"] == "unknown"
    assert stage["work"]["tokens"] is None
    validation = stage["work"]["usage_validation"]
    assert validation["recorded_usage"] == _USAGE
    assert "legacy usage has no saved session evidence to validate" in validation["reasons"]
    assert stage["execution_provenance"]["timed_out"] is True
    assert stage["execution_provenance"]["error"] == reason
    assert len(list(cache.glob("*/*/manifest.json"))) == 5  # Not a vanilla baseline.
    row = next(row for row in run_status.run_status(run)["attempts"] if row["id"] == failed)
    assert (row["outcome"], row["outcome_reason"]) == ("unknown", reason)

    calls.clear()
    resume_support.install(monkeypatch, calls)
    study.resume_study(root, baseline_cache=cache, baseline_identity=identity)
    assert calls == []


def _failing_calls(monkeypatch: pytest.MonkeyPatch, calls: list[str], failing: set[int]) -> None:
    """The calls with these ordinals overrun their budget; the others pass."""
    resume_support.install(monkeypatch, calls)
    invoke = harbor_backend.invoke_native

    def invoke_or_overrun(**kwargs: Any) -> dict[str, Any]:
        if len(calls) + 1 not in failing:
            return invoke(**kwargs)
        calls.append(kwargs["entry"]["id"])
        _overrun(kwargs)
        raise AssertionError("unreachable")

    monkeypatch.setattr(harbor_backend, "invoke_native", invoke_or_overrun)


@pytest.mark.parametrize("collected", [False, True])
def test_three_failed_calls_in_a_row_stop_new_attempts_and_resume_codes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collected: bool
) -> None:
    monkeypatch.setitem(_COLLECTED, "patch", collected)
    recipe = resume_support.recipe(tmp_path / "config", tasks=2, treatments=1, repeats=3, jobs=1)
    root = tmp_path / "run"
    calls: list[str] = []
    _failing_calls(monkeypatch, calls, {1, 2, 3})
    with pytest.raises(ValueError, match="the last 3 attempts to finish each failed") as stop:
        study.run_study(study.load_study(recipe), root)
    assert len(calls) == 3
    assert str(stop.value).count("the Harbor worker exceeded its budget of 5400 seconds") == 3
    assert "`pluginbench run <run-dir>` codes the pending slots" in str(stop.value)
    saved = _json(root / "run.json")
    assert saved["status"] == "interrupted"
    assert Counter(saved["attempt_states"].values()) == {"complete": 3, "pending": 9}

    coded = list(calls)
    calls.clear()
    resume_support.install(monkeypatch, calls)
    study.resume_study(root)
    assert len(calls) == 9 and not set(calls) & set(coded)  # The three stay as recorded.
    assert _json(root / "run.json")["status"] == "complete"


def test_a_finished_attempt_without_a_failed_call_resets_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = resume_support.recipe(tmp_path / "config", tasks=2, treatments=1, repeats=3, jobs=1)
    root = tmp_path / "run"
    calls: list[str] = []
    _failing_calls(monkeypatch, calls, {1, 2, 4, 5})
    study.run_study(study.load_study(recipe), root)
    assert len(calls) == 12
    assert _json(root / "run.json")["status"] == "complete"


def test_with_two_jobs_the_count_follows_the_order_attempts_finish_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Call 1 fails but finishes after call 2 passes, so calls 1, 3 and 4 fail in a row.

    In the order they started, call 2's pass would sit between them and no stop would follow.
    """
    from threading import Event, Lock

    recipe = resume_support.recipe(tmp_path / "config", tasks=2, treatments=1, repeats=3, jobs=2)
    root = tmp_path / "run"
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)
    invoke = harbor_backend.invoke_native
    lock = Lock()
    ordinals: dict[str, int] = {}
    committed = {ordinal: Event() for ordinal in range(1, 13)}
    complete = ledger.Ledger.complete

    def completing(self: ledger.Ledger, attempt_id: str, *args: Any, **kwargs: Any) -> None:
        complete(self, attempt_id, *args, **kwargs)
        committed[ordinals[attempt_id]].set()

    # Each held call waits until the named call has been recorded complete. Call 4 also waits
    # for call 5, admitted when call 3 finished, to be in flight when the run stops.
    after = {1: 2, 3: 1, 4: 3}
    fifth = Event()

    def ordered(**kwargs: Any) -> dict[str, Any]:
        with lock:
            ordinal = len(ordinals) + 1
            ordinals[kwargs["entry"]["id"]] = ordinal
        if ordinal in after:
            assert committed[after[ordinal]].wait(timeout=10)
        if ordinal == 4:
            assert fifth.wait(timeout=10)
        if ordinal == 5:
            fifth.set()
            # Call 5 must finish after call 4, or its pass would reset the count.
            assert committed[4].wait(timeout=10)
        if ordinal == 2 or ordinal > 4:
            return invoke(**kwargs)
        calls.append(kwargs["entry"]["id"])
        _overrun(kwargs)
        raise AssertionError("unreachable")

    monkeypatch.setattr(ledger.Ledger, "complete", completing)
    monkeypatch.setattr(harbor_backend, "invoke_native", ordered)
    with pytest.raises(ValueError, match="the last 3 attempts to finish each failed"):
        study.run_study(study.load_study(recipe), root)
    by_ordinal = {ordinal: attempt for attempt, ordinal in ordinals.items()}
    # Call 5 was in flight when the run stopped; it finished, and no call 6 started.
    assert len(ordinals) == 5
    states = _json(root / "run.json")["attempt_states"]
    assert all(states[by_ordinal[ordinal]] == "complete" for ordinal in range(1, 6))
    assert Counter(states.values()) == {"complete": 5, "pending": 7}
