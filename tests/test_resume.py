"""Resume keeps every finished attempt, discards only started ones, and never replays a call.

Killed runs come from a child process that SIGKILLs itself at a named boundary, so no except
or finally block runs, as when the interrupted pilot was killed.
"""

from __future__ import annotations

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
from pluginbench import harbor_backend, parallel, study
from pluginbench.ledger import Ledger

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


def test_backend_failure_before_the_first_turn_stops_the_run_unscored_and_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A seed whose first attempt is vanilla, so a scored failure would be cached.
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
    recipe = resume_support.recipe(
        tmp_path / "config", tasks=2, treatments=1, repeats=3, jobs=1, seed=seed
    )
    resolved = study.load_study(recipe)
    cache, root = tmp_path / "cache", tmp_path / "run"
    calls: list[str] = []
    resume_support.install(monkeypatch, calls)

    def outage(**kwargs: Any) -> dict[str, Any]:
        # The shape of a Codex session cut off from its backend: an empty patch, no usage.
        calls.append(kwargs["entry"]["id"])
        execution = native_execution(
            kwargs["area"], kwargs["protocol"]["native_task_identity"], reward=0
        )
        execution.update(
            usage=None,
            timed_out=False,
            successful_native_completion=False,
            stream_errors=[
                {"type": "error", "message": "workspace routing discovery failed"},
                {"type": "turn.failed", "error": {"message": "workspace routing discovery failed"}},
            ],
        )
        return execution

    monkeypatch.setattr(harbor_backend, "invoke_native", outage)
    with pytest.raises(study.BackendUnavailable, match="workspace routing discovery failed"):
        study.run_study(
            resolved,
            root,
            baseline_cache=cache,
            baseline_identity=resume_support.vanilla_identity(resolved),
        )
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
