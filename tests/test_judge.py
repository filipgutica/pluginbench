"""`pluginbench judge`: blinded packets, the reviewer launch path, and the card's tally rule."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pluginbench import cli, codex, judge
from pluginbench.assessment_models import Attempt, Stage
from pluginbench.judge import normalize_diff, prepare, tally

TASKS = ["alpha", "beta"]
PACKET_DIFF = """\
diff --git after/src/new.py after/src/new.py
new file mode 100644
index 0000000..6ad41e8
--- /dev/null
+++ after/src/new.py
@@ -0,0 +1 @@
+index = 1
diff --git before/src/old.py before/src/old.py
deleted file mode 100644
index 1f2e3d4..0000000
--- before/src/old.py
+++ /dev/null
@@ -1 +0,0 @@
-gone = True
diff --git before/src/main.py after/src/main.py
index 3b18e51..a0b1c2d 100644
--- before/src/main.py
+++ after/src/main.py
@@ -1,2 +1,2 @@
 index = 0
-value = 1
+value = 2
"""
# The same change as another tool renders it: a/ b/ prefixes and full-length hashes.
GIT_DIFF = """\
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000000000000000000000000000000000000..6ad41e8f00d1c3a87b5e0c2a6f1b9d3e4c5a6b7c
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+index = 1
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 1f2e3d4c5b6a7980f1e2d3c4b5a6978877665544..0000000000000000000000000000000000000000
--- a/src/old.py
+++ /dev/null
@@ -1 +0,0 @@
-gone = True
diff --git a/src/main.py b/src/main.py
index 3b18e51aa..a0b1c2d3e 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
 index = 0
-value = 1
+value = 2
"""


def test_normalized_headers_do_not_fingerprint_the_source() -> None:
    normalized = normalize_diff(PACKET_DIFF)
    assert normalized == normalize_diff(GIT_DIFF)
    assert "before/" not in normalized and "after/" not in normalized
    assert "diff --git a/src/new.py b/src/new.py\nnew file mode 100644\n--- /dev/null\n" in (
        normalized
    )
    assert "--- a/src/old.py\n+++ /dev/null\n" in normalized
    # Only header index lines go; hunk lines that start with "index" stay.
    assert "+index = 1\n" in normalized and " index = 0\n" in normalized
    assert "\nindex " not in normalized


def test_quoted_paths_keep_their_quotes() -> None:
    diff = (
        'diff --git "before/a\\tb.py" "after/a\\tb.py"\n'
        '--- "before/a\\tb.py"\n+++ "after/a\\tb.py"\n@@ -1 +1 @@\n-x\n+y\n'
    )
    assert normalize_diff(diff).splitlines()[:3] == [
        'diff --git "a/a\\tb.py" "b/a\\tb.py"',
        '--- "a/a\\tb.py"',
        '+++ "b/a\\tb.py"',
    ]


def _run(root: Path, attempts: list[dict[str, object]]) -> Path:
    """A finished run with results.json and one review packet per attempt."""
    review = root / "evidence" / "review-revisions" / "review"
    mapping, rows = [], []
    for attempt in attempts:
        ident, task = str(attempt["id"]), str(attempt["task"])
        before = str(attempt.get("before", f"sha256:before-{task}"))
        packet = review / "packets" / f"packet-{ident}"
        (packet / "before" / "src").mkdir(parents=True)
        (packet / "before" / "src" / "main.py").write_text(f"# {task} starting source\n")
        (packet / "task.txt").write_text(f"Solve {task}.\n")
        (packet / "source.diff").write_text(PACKET_DIFF.replace("value = 2", f"value = {ident}"))
        (packet / "verifier.json").write_text('{"checks": {"behavior": true}}')
        packet.with_name(packet.name + ".json").write_text(json.dumps({"before_digest": before}))
        mapping.append(
            {
                "id": ident,
                "arm": attempt["guidance"],
                "packet_id": packet.name,
                "submission_digest": f"sha256:after-{ident}",
                "task_id": task,
            }
        )
        rows.append(
            {
                "id": ident,
                "task": task,
                "repeat": attempt["repeat"],
                "guidance": attempt["guidance"],
                "correctness": attempt.get("correctness", "pass"),
                "native_grading": attempt.get("native_grading", {}),
                "artifacts": {
                    "snapshot_digest": f"sha256:after-{ident}",
                    "before_digest": before,
                },
            }
        )
    (review / "mapping.json").write_text(json.dumps(mapping))
    (root / "report").mkdir(parents=True)
    (root / "report" / "results.json").write_text(
        json.dumps({"artifact_base": "evidence", "attempts": rows})
    )
    return root


def _two_arms(root: Path, **overrides: dict[str, object]) -> Path:
    attempts = [
        {"id": f"{arm[0]}{task[0]}{repeat}", "task": task, "repeat": repeat, "guidance": arm}
        for arm in ("baseline", "workflow-r9")
        for task in TASKS
        for repeat in (1, 2, 3)
    ]
    for attempt in attempts:
        attempt.update(overrides.get(str(attempt["id"]), {}))
    return _run(root / "run", attempts)


def _prepare(tmp_path: Path, run: Path, **options: object) -> Path:
    criteria = tmp_path / "standard.md"
    criteria.write_text("1. Correct behavior.\n")
    arguments: dict[str, object] = {
        "arms": [f"{run}=vanilla", f"{run}=workflow-r9"],
        "tasks": TASKS,
        "exclude": ["holdout"],
        "criteria": criteria,
        "seed": 7,  # Orders both arms first on some pairs.
        "output": tmp_path / "judging",
    }
    arguments.update(options)
    return prepare(**arguments)  # type: ignore[arg-type]


def test_prepare_writes_blinded_task_folders_and_a_separate_key(tmp_path: Path) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    key = json.loads((directory / "key.json").read_text())
    assert key["tasks"]["task-1"]["task"] == "alpha"
    assert set(key["tasks"]["task-2"]["pairs"]) == {"1", "2", "3"}
    first_sides = {
        pair["A"]["arm"] for entry in key["tasks"].values() for pair in entry["pairs"].values()
    }
    assert first_sides == {"vanilla", "workflow-r9"}
    folder = directory / "task-1"
    assert sorted(path.name for path in folder.iterdir()) == [
        "instruction.md",
        "pair-1-A.diff",
        "pair-1-B.diff",
        "pair-2-A.diff",
        "pair-2-B.diff",
        "pair-3-A.diff",
        "pair-3-B.diff",
        "response-schema.json",
        "review-standard.md",
        "source",
    ]
    assert (folder / "source" / "src" / "main.py").read_text() == "# alpha starting source\n"
    text = "\n".join(path.read_text() for path in folder.glob("*.*"))
    for leak in ("baseline", "vanilla", "workflow", "before/", "after/", "\nindex "):
        assert leak not in text
    brief = (directory / "task-1.brief.md").read_text()
    assert "Pair 3: A = pair-3-A.diff, B = pair-3-B.diff" in brief
    assert str(tmp_path) not in brief


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"tasks": ["alpha", "holdout"]}, "refusing excluded tasks: holdout"),
        ({"arms": ["run=vanilla", "run=vanilla"]}, "two different arms"),
    ],
)
def test_prepare_refuses_holdouts_and_one_arm_twice(
    tmp_path: Path, options: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _prepare(tmp_path, tmp_path / "unread", **options)
    assert not (tmp_path / "judging").exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"before": "sha256:other"}, "start from different source"),
        ({"artifacts_mismatch": True}, "bound to another submission"),
    ],
)
def test_prepare_refuses_unbound_or_unmatched_submissions(
    tmp_path: Path, override: dict[str, object], message: str
) -> None:
    run = _two_arms(tmp_path, wa1={"before": override.get("before", "sha256:before-alpha")})
    if override.get("artifacts_mismatch"):
        results = run / "report" / "results.json"
        data = json.loads(results.read_text())
        data["attempts"][0]["artifacts"]["snapshot_digest"] = "sha256:another"
        results.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message):
        _prepare(tmp_path, run)


def test_prepare_names_an_attempt_that_has_neither_a_packet_nor_readable_snapshots(
    tmp_path: Path,
) -> None:
    run = _two_arms(tmp_path)
    (run / "evidence/review-revisions/review/packets/packet-wa1/source.diff").unlink()
    with pytest.raises(ValueError, match="no review packet, and its saved snapshots cannot be"):
        _prepare(tmp_path, run)


def _unreviewed(run: Path, *, reported: str | None) -> None:
    """Make attempt wa1 one with no packet, whose report row names `reported` as its submission."""
    shutil.rmtree(run / "evidence/review-revisions/review/packets/packet-wa1")
    results = run / "report/results.json"
    data = json.loads(results.read_text())
    row = next(row for row in data["attempts"] if row["id"] == "wa1")
    row["artifacts"]["snapshot_digest"] = reported
    results.write_text(json.dumps(data))


def _never_coded() -> Attempt:
    stage = Stage(id="handoff", snapshot=None, correctness=None, evidence=None, work=None)
    return Attempt("wa1", "alpha", 1, "workflow-r9", None, None, (stage,), {})


def test_an_attempt_with_no_submission_is_left_out_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _two_arms(tmp_path)
    _unreviewed(run, reported=None)
    monkeypatch.setattr(judge, "_saved_evidence", lambda evidence: ({"wa1": _never_coded()}, {}))
    criteria = tmp_path / "standard.md"
    criteria.write_text("1. Correct behavior.\n")
    output = tmp_path / "judging"
    arguments = ["judge", "prepare", f"{run}=vanilla", f"{run}=workflow-r9", "--tasks", "alpha"]
    arguments += ["--criteria", str(criteria), "--seed", "7", "--output", str(output)]
    prepared = CliRunner().invoke(cli.app, arguments)
    assert prepared.exit_code == 0, prepared.output
    assert (
        "Left out, with their pairs, the attempts that have no saved submission: "
        "workflow-r9 alpha repeat 1." in prepared.stderr
    )
    key = json.loads((output / "key.json").read_text())
    assert key["omitted"] == [{"arm": "workflow-r9", "task": "alpha", "repeat": 1}]
    assert sorted(key["tasks"]["task-1"]["pairs"]) == ["1", "2"]  # Repeats 2 and 3 remain.


@pytest.mark.parametrize(
    ("records", "reported", "message"),
    [
        ({}, None, "the run's records have no such attempt"),
        ({"wa1": _never_coded()}, "sha256:after-wa1", "a submission the run did not save"),
    ],
)
def test_evidence_that_contradicts_the_report_is_refused_not_left_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: dict[str, Attempt],
    reported: str | None,
    message: str,
) -> None:
    run = _two_arms(tmp_path)
    _unreviewed(run, reported=reported)
    monkeypatch.setattr(judge, "_saved_evidence", lambda evidence: (records, {}))
    with pytest.raises(ValueError, match=message):
        _prepare(tmp_path, run)


def _fake_judge(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, str],
    *,
    root: Path,
    tool_failure: bool = False,
    expect_unable_contract: bool = False,
    usage: dict[str, int] | None = None,
    on_call: Callable[[], None] | None = None,
) -> list[Path]:
    """Answer every pair with answers[model]; check what the judge's workspace holds."""
    calls: list[Path] = []

    def invoke(
        *,
        area: Path,
        entry: dict[str, str],
        protocol: dict[str, object],
        prompt: str,
        output_schema: Path,
        artifacts: Path | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(area)
        workspace = area / "workspace"
        assert output_schema == workspace / "response-schema.json"
        if expect_unable_contract:
            schema = json.loads(output_schema.read_text())
            assert "unable" in schema["$defs"]["PairVerdict"]["properties"]["preferred"]["enum"]
            assert "unable" in prompt
        assert not workspace.is_relative_to(root)
        source = workspace / "source"
        assert source.is_dir() and not source.is_symlink()
        assert not list(workspace.rglob("key.json")) and not list(workspace.rglob("*brief*"))
        text = "\n".join(p.read_text() for p in workspace.rglob("*") if p.is_file())
        assert "workflow" not in text and "baseline" not in text and "vanilla" not in text
        model = str(protocol["models"][entry["host"]])  # type: ignore[index]
        pairs = sorted(int(path.name.split("-")[1]) for path in workspace.glob("pair-*-A.diff"))
        response = {
            "pairs": [
                {"pair": n, "preferred": answers[model], "decisive_checks": [1], "reason": "r"}
                for n in pairs
            ]
        }
        artifacts = area / "artifacts" if artifacts is None else artifacts
        artifacts.mkdir(exist_ok=True)
        (artifacts / "stdout.jsonl").write_text(json.dumps(response) + "\n")
        (artifacts / "host-evidence.json").write_text(json.dumps({"usage": usage}))
        trace = (
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "read-inputs",
                        "output": "Error: failed to start codex-code-mode-host: No such file or directory (os error 2)",
                    },
                }
            )
            if tool_failure
            else ""
        )
        if trace:
            (artifacts / "host-trace-0.jsonl").write_text(trace + "\n")
        parsed = codex.parse_events("", [trace], "codex")
        if on_call is not None:
            on_call()
        return {
            **parsed,
            "usage": usage,
            "successful_native_completion": True,
            "actual_primary_models": [model],
            "runtime_effort_evidence": [protocol["reasoning"]],
            "command_items": [],
            "final": json.dumps(response),
        }

    monkeypatch.setattr(codex, "invoke", invoke)
    return calls


def test_run_uses_the_reviewer_launch_path_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    calls = _fake_judge(monkeypatch, {"m1": "A", "m2": "B"}, root=tmp_path)
    judges = ["codex:m1:high", "codex:m2:high"]
    assert judge.run(directory, judges=judges, timeout=60, jobs=2, progress=print) == 0
    assert len(calls) == 4
    verdict = json.loads((directory / "results/codex-m1-high/task-2.json").read_text())
    assert [row["preferred"] for row in verdict["pairs"]] == ["A", "A", "A"]
    assert (directory / "results/codex-m1-high/task-2/call-1/stdout.jsonl").is_file()
    assert judge.run(directory, judges=judges, timeout=60, jobs=1, progress=print) == 0
    assert len(calls) == 4


def test_a_rejected_answer_leaves_no_verdict_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    calls = _fake_judge(monkeypatch, {"m1": "maybe"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 2
    assert not list((directory / "results/codex-m1-high").glob("*.json"))
    _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    assert len(calls) == 2
    assert (directory / "results/codex-m1-high/task-1/call-2").is_dir()


def _votes(directory: Path, judges: dict[str, dict[tuple[str, str], str]]) -> None:
    """Write each judge's verdict per pair, by arm: {judge: {(task folder, pair): arm or tie}}."""
    key = json.loads((directory / "key.json").read_text())
    for name, chosen in judges.items():
        for folder, entry in key["tasks"].items():
            rows = []
            for number, sides in entry["pairs"].items():
                arm = chosen.get((folder, number), "tie")
                side = next((s for s in "AB" if sides[s]["arm"] == arm), "tie")
                rows.append(
                    {"pair": int(number), "preferred": side, "decisive_checks": [], "reason": ""}
                )
            path = directory / "results" / name / f"{folder}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pairs": rows}))


def test_tally_applies_correctness_first_then_a_strict_majority(tmp_path: Path) -> None:
    overlap: dict[str, object] = {
        "correctness": "unknown",
        "native_grading": {
            "reason": "trusted verifier tests overwrite authored patch paths",
            "native_verifier_result": {
                "rewards": {
                    "reward": 1,
                    "f2p_passed": 2,
                    "f2p_total": 2,
                    "p2p_passed": 5,
                    "p2p_total": 5,
                }
            },
        },
    }
    run = _two_arms(
        tmp_path,
        # alpha 1: only vanilla passes, so it wins although every judge prefers the other side.
        wa1={"correctness": "fail"},
        # alpha 2: vanilla's overlap pass is a pass; both pass, so judges decide.
        ba2=overlap,
        # alpha 3: both fail; judges decide.
        ba3={"correctness": "fail"},
        wa3={"correctness": "fail"},
    )
    directory = _prepare(tmp_path, run)
    v, w = "vanilla", "workflow-r9"
    _votes(
        directory,
        {
            "j1": {("task-1", "1"): w, ("task-1", "2"): w, ("task-1", "3"): v, ("task-2", "1"): v},
            "j2": {("task-1", "1"): w, ("task-1", "2"): w, ("task-1", "3"): w, ("task-2", "1"): v},
            "j3": {("task-1", "1"): w, ("task-1", "2"): v, ("task-2", "1"): w},
        },
    )
    result = tally(directory)
    alpha, beta = ({pair["pair"]: pair for pair in task["pairs"]} for task in result["tasks"])
    assert alpha[1]["decided_by"] == "correctness" and alpha[1]["wins"] == {v: 1.0, w: 0.0}
    assert alpha[2]["decided_by"] == "judges" and alpha[2]["wins"] == {v: 0.0, w: 1.0}
    # v, w, tie: no strict majority, so half each.
    assert alpha[3]["decided_by"] == "judges" and alpha[3]["wins"] == {v: 0.5, w: 0.5}
    assert beta[1]["wins"] == {v: 1.0, w: 0.0}
    # Two remaining beta pairs are all ties: half each.
    assert result["weighted_wins"] == {v: 3.5, w: 2.5}
    assert result["correctness_decided"] == {"pairs": 1, "wins": {v: 1.0, w: 0.0}}
    assert result["judged"]["pairs"] == 5
    stats = result["judge_stats"]
    assert set(stats) == {"j1", "j2", "j3"}
    assert all(0 <= row["side_a_rate"] <= 1 for row in stats.values())
    # j1 and j2 agree on 5 pairs (not alpha 3); j3 differs from them on alpha 2 and beta 1.
    assert stats["j3"]["agreement"] == pytest.approx(3 / 5)
    assert stats["j3"]["agreement_pairs"] == 5
    assert "Weighted wins over 6 pairs: vanilla 3.5, workflow-r9 2.5" in judge.format_tally(result)


def test_tally_refuses_a_partial_panel(tmp_path: Path) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _votes(directory, {"j1": {}, "j2": {}})
    (directory / "results/j2/task-2.json").unlink()
    with pytest.raises(ValueError, match="judge results missing: j2 task-2"):
        tally(directory)


def test_tally_refuses_a_requested_judge_that_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "A", "m2": "B"}, root=tmp_path)
    judges = ["codex:m1:high", "codex:m2:high"]
    assert judge.run(directory, judges=judges, timeout=60, jobs=1, progress=print) == 0
    # As if the run stopped before the second judge's first call.
    shutil.rmtree(directory / "results/codex-m2-high")
    with pytest.raises(ValueError, match="judge results missing: codex-m2-high task-1"):
        tally(directory)


def test_cli_prepare_and_tally(tmp_path: Path) -> None:
    run = _two_arms(tmp_path)
    criteria = tmp_path / "standard.md"
    criteria.write_text("1. Correct behavior.\n")
    output = tmp_path / "judging"
    runner = CliRunner()
    prepared = runner.invoke(
        cli.app,
        [
            "judge",
            "prepare",
            f"{run}=vanilla",
            f"{run}=workflow-r9",
            "--tasks",
            "alpha,beta",
            "--exclude",
            "holdout",
            "--criteria",
            str(criteria),
            "--seed",
            "7",
            "--output",
            str(output),
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    _votes(output, {"j1": {}})
    tallied = runner.invoke(cli.app, ["judge", "tally", str(output)])
    assert tallied.exit_code == 0, tallied.output
    assert "Weighted wins over 6 pairs: vanilla 3, workflow-r9 3" in tallied.output
    assert json.loads((output / "tally.json").read_text())["pairs"] == 6


def test_prepared_judge_schema_and_brief_distinguish_unable_from_tie(tmp_path: Path) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    schema = json.loads((directory / "task-1/response-schema.json").read_text())
    assert "unable" in schema["$defs"]["PairVerdict"]["properties"]["preferred"]["enum"]
    assert "unable" in (directory / "task-1.brief.md").read_text()


def test_unable_answers_fail_by_pair_and_retry_without_losing_raw_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    calls = _fake_judge(monkeypatch, {"m1": "unable"}, root=tmp_path)
    messages: list[str] = []
    assert (
        judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=messages.append)
        == 2
    )
    assert any(
        "codex-m1-high task-1: failed:" in line and "unable" in line and "1, 2, 3" in line
        for line in messages
    )
    with pytest.raises(ValueError, match="judge results missing"):
        tally(directory)
    _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    assert len(calls) == 2
    assert (directory / "results/codex-m1-high/task-1/call-2").is_dir()
    assert tally(directory)["weighted_wins"] == {"vanilla": 3.0, "workflow-r9": 3.0}


def test_saved_unable_blocks_tally_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    path = directory / "results/codex-m1-high/task-1.json"
    verdicts = json.loads(path.read_text())
    verdicts["pairs"][0]["preferred"] = "unable"
    path.write_text(json.dumps(verdicts))
    with pytest.raises(ValueError, match="codex-m1-high task-1.*pair.*1.*unable"):
        tally(directory)
    calls = _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    assert len(calls) == 1  # The complete task stays cached.
    assert json.loads(path.read_text())["pairs"][0]["preferred"] == "A"


def test_tool_startup_failure_cannot_be_accepted_as_a_tie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path, tool_failure=True)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 2
    assert not list((directory / "results/codex-m1-high").glob("*.json"))
    with pytest.raises(ValueError, match="judge results missing"):
        tally(directory)


def test_previously_saved_tie_with_failed_tool_trace_blocks_tally_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _votes(directory, {"codex-m1-high": {}})
    raw = directory / "results/codex-m1-high/task-1/call-1"
    raw.mkdir(parents=True)
    (raw / "host-trace-0.jsonl").write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "read-inputs",
                    "output": "Error: failed to start codex-code-mode-host: No such file or directory (os error 2)",
                },
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="codex-m1-high task-1.*unable"):
        tally(directory)
    calls = _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    assert len(calls) == 1
    assert (raw.parent / "call-2").is_dir()
    assert tally(directory)["weighted_wins"] == {"vanilla": 3.0, "workflow-r9": 3.0}


def test_retry_of_old_prepared_folder_uses_the_unable_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    for folder in directory.glob("task-*"):
        if not folder.is_dir():
            continue
        path = folder / "response-schema.json"
        schema = json.loads(path.read_text())
        schema["$defs"]["PairVerdict"]["properties"]["preferred"]["enum"] = ["A", "B", "tie"]
        path.write_text(json.dumps(schema))
        (directory / f"{folder.name}.brief.md").write_text("Read the inputs and return A, B, or tie.")
    _fake_judge(monkeypatch, {"m1": "unable"}, root=tmp_path, expect_unable_contract=True)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 2


@pytest.mark.parametrize(
    "field",
    [
        "pair-1-A.diff",
        "pair-1-B.diff",
        "instruction.md",
        "review-standard.md",
        "response-schema.json",
        "brief",
        "judge_spec",
        "prepare_seed",
        "key",
        "source/src/main.py",
    ],
)
def test_tally_refuses_changed_bound_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print)
    verdict = directory / "results/codex-m1-high/task-1.json"
    saved = json.loads(verdict.read_text())
    assert saved["binding"]
    assert tally(directory, require_bound=True)["unbound"] == 0
    if field == "judge_spec":
        path = directory / "judge-specs/codex-m1-high.json"
        path.write_text(json.dumps({"harness": "codex", "model": "changed", "effort": "high"}))
    elif field in {"prepare_seed", "key"}:
        path = directory / "key.json"
        key = json.loads(path.read_text())
        key["seed" if field == "prepare_seed" else "criteria"] = "changed"
        path.write_text(json.dumps(key))
    else:
        path = directory / "task-1.brief.md" if field == "brief" else directory / "task-1" / field
        path.write_text(path.read_text() + "\nchanged\n")
    with pytest.raises(ValueError) as error:
        tally(directory)
    assert str(verdict) in str(error.value)
    assert field in str(error.value)
    # Resume must also refuse stale results rather than silently counting them as done.
    with pytest.raises(ValueError, match="binding mismatch"):
        judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print)


def test_legacy_binding_policy_is_visible_and_can_be_strict(tmp_path: Path) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _votes(directory, {"j1": {}})
    runner = CliRunner()
    result = runner.invoke(cli.app, ["judge", "tally", str(directory)])
    assert result.exit_code == 0, result.output
    assert "unbound verdict (written before binding)" in result.output
    assert "task-1.json" in result.output and "task-2.json" in result.output
    assert json.loads((directory / "tally.json").read_text())["unbound"] == 2
    strict = runner.invoke(cli.app, ["judge", "tally", str(directory), "--require-bound"])
    assert strict.exit_code != 0
    assert "task-1.json" in strict.output and "unbound verdict" in strict.output


@pytest.mark.parametrize(
    ("pairs", "a_votes", "status"),
    [
        (9, 9, "too_small"),
        (10, 10, "flagged"),
        (10, 0, "flagged"),
        (10, 1, "flagged"),
        (10, 9, "flagged"),
        (10, 2, "within_band"),
        (10, 8, "within_band"),
        (10, 5, "within_band"),
    ],
)
def test_position_bias_counts_ties_in_denominator(
    tmp_path: Path, pairs: int, a_votes: int, status: str
) -> None:
    attempts = [
        {"id": f"{arm[0]}{repeat}", "task": "alpha", "repeat": repeat, "guidance": arm}
        for arm in ("baseline", "workflow-r9")
        for repeat in range(1, pairs + 1)
    ]
    directory = _prepare(tmp_path, _run(tmp_path / "run", attempts), tasks=["alpha"])
    _votes(directory, {"j1": {}})
    path = directory / "results/j1/task-1.json"
    verdict = json.loads(path.read_text())
    for row in verdict["pairs"][:a_votes]:
        row["preferred"] = "A"
    path.write_text(json.dumps(verdict))
    result = tally(directory)
    stats = result["judge_stats"]["j1"]
    assert stats["side_a_rate"] == a_votes / pairs
    assert stats["position_bias"] == status
    expected = {
        "too_small": "sample too small to tell",
        "flagged": "FLAG: position bias",
        "within_band": "within 0.2–0.8",
    }[status]
    assert expected in judge.format_tally(result)


@pytest.mark.parametrize("unknown", [False, True])
def test_judge_token_totals_include_retries_and_unknown_calls(
    tmp_path: Path, unknown: bool
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _votes(directory, {"claude-m1-high": {}})
    # Legacy raw logs remain usable: no new run or API call is needed to tally cost.
    for task, call in [("task-1", 1), ("task-1", 2), ("task-2", 1)]:
        raw = directory / f"results/claude-m1-high/{task}/call-{call}"
        raw.mkdir(parents=True)
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 4,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
            },
        }
        (raw / "stdout.jsonl").write_text(json.dumps(event) + "\n")
    if unknown:
        (directory / "results/claude-m1-high/task-1/call-1/stdout.jsonl").unlink()
    result = tally(directory)
    usage = result["judge_stats"]["claude-m1-high"]["usage"]
    assert usage["calls"] == 3
    assert usage["unknown_calls"] == int(unknown)
    assert usage["known_tokens"]["input_tokens"] == 35 * (2 if unknown else 3)
    assert usage["known_tokens"]["output_tokens"] == 4 * (2 if unknown else 3)
    assert usage["tokens"] == (None if unknown else usage["known_tokens"])
    assert ("unknown" if unknown else "117") in judge.format_tally(result)


def test_run_records_usage_even_for_rejected_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(
        monkeypatch, {"m1": "unable"}, root=tmp_path, usage={"input_tokens": 10, "output_tokens": 3}
    )
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 2
    _fake_judge(
        monkeypatch, {"m1": "tie"}, root=tmp_path, usage={"input_tokens": 20, "output_tokens": 4}
    )
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    usage = tally(directory)["judge_stats"]["codex-m1-high"]["usage"]
    assert usage == {
        "tokens": {"input_tokens": 60, "output_tokens": 14},
        "known_tokens": {"input_tokens": 60, "output_tokens": 14},
        "calls": 4,
        "unknown_calls": 0,
    }


@pytest.mark.parametrize(
    "usage", [None, {"input_tokens": 2}, {"input_tokens": -1, "output_tokens": 1}]
)
def test_incomplete_call_usage_does_not_become_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: dict[str, int] | None
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "tie"}, root=tmp_path, usage=usage)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    result = tally(directory)
    totals = result["judge_stats"]["codex-m1-high"]["usage"]
    assert totals["tokens"] is None and totals["known_tokens"] is None
    assert totals["unknown_calls"] == 2
    assert "unknown (2 of 2 calls)" in judge.format_tally(result)


@pytest.mark.parametrize("change", ["delete_diff", "during_call", "reprepare"])
def test_verdict_binding_rejects_input_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    if change == "during_call":

        def change_diff() -> None:
            path = directory / "task-1/pair-1-A.diff"
            path.write_text("changed during judging")

        _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path, on_call=change_diff)
        assert (
            judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 1
        )
        assert not (directory / "results/codex-m1-high/task-1.json").exists()
        return
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    if change == "delete_diff":
        (directory / "task-1/pair-1-A.diff").unlink()
        field = "files/pair-1-A.diff"
    else:
        # Same presentation can unblind to a different arm after re-preparing the key.
        key_path = directory / "key.json"
        key = json.loads(key_path.read_text())
        pair = key["tasks"]["task-1"]["pairs"]["1"]
        pair["A"], pair["B"] = pair["B"], pair["A"]
        key_path.write_text(json.dumps(key))
        field = "key"
    with pytest.raises(ValueError, match=f"task-1.json.*binding mismatch: {field}"):
        tally(directory)


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("when", ["before_call", "after_verdict"])
def test_bound_tree_symlinks_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, when: str
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    source = (directory / "task-1/source").resolve()
    target = source / ("src/main.py" if kind == "file" else "src")
    calls = _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    if when == "after_verdict":
        assert (
            judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
        )
    (source / "current").symlink_to(target, target_is_directory=kind == "directory")
    if when == "before_call":
        messages: list[str] = []
        assert (
            judge.run(
                directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=messages.append
            )
            == 1
        )
        assert len(calls) == 1  # Only the task without a nested link can launch.
        assert any("symlink" in line and "source/current" in line for line in messages)
    else:
        with pytest.raises(ValueError, match="binding mismatch: symlink.*source/current"):
            tally(directory, require_bound=True)
        with pytest.raises(ValueError, match="binding mismatch: symlink.*source/current"):
            judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print)


def test_killed_judge_attempt_remains_unknown_after_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    script = """
import os
from pathlib import Path
import signal
import sys
from pluginbench import codex, judge

def killed(*, area, artifacts=None, **kwargs):
    destination = artifacts if artifacts is not None else area / "artifacts"
    destination.mkdir(exist_ok=True)
    (destination / "stdout.jsonl").write_text('{"type":"turn.started"}\\n')
    os.kill(os.getpid(), signal.SIGKILL)

codex.invoke = killed
judge.run(Path(sys.argv[1]), judges=["codex:m1:high"], timeout=60, jobs=1, progress=print)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(directory)],
        env={**os.environ, "TMPDIR": str(tmp_path)},
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode()
    raw = directory / "results/codex-m1-high/task-1"
    assert (raw / "call-1/stdout.jsonl").is_file()
    _fake_judge(
        monkeypatch, {"m1": "A"}, root=tmp_path, usage={"input_tokens": 10, "output_tokens": 3}
    )
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    usage = tally(directory)["judge_stats"]["codex-m1-high"]["usage"]
    assert usage["calls"] == 3 and usage["unknown_calls"] == 1
    assert usage["tokens"] is None
    assert usage["known_tokens"] == {"input_tokens": 20, "output_tokens": 6}


@pytest.mark.parametrize("target", ["key", "verdict"])
def test_tally_uses_single_validated_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    path = directory / ("key.json" if target == "key" else "results/codex-m1-high/task-1.json")
    original = path.read_bytes()
    replacement = json.loads(original)
    if target == "key":
        pair = replacement["tasks"]["task-1"]["pairs"]["1"]
        pair["A"], pair["B"] = pair["B"], pair["A"]
        path.write_text(json.dumps(replacement))
        after_first_read = original  # Restore the bound key after the first read.
    else:
        replacement["pairs"][0]["preferred"] = "unable"
        after_first_read = json.dumps(replacement).encode()
    reads = 0
    original_bytes, original_text = Path.read_bytes, Path.read_text

    def replace_after_read(item: Path) -> None:
        nonlocal reads
        if item == path:
            reads += 1
            if reads == 1:
                path.write_bytes(after_first_read)

    def read_bytes(item: Path) -> bytes:
        value = original_bytes(item)
        replace_after_read(item)
        return value

    def read_text(item: Path, encoding: str | None = None, errors: str | None = None) -> str:
        value = original_text(item, encoding=encoding, errors=errors)
        replace_after_read(item)
        return value

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    if target == "key":
        with pytest.raises(ValueError, match="task-1.json.*binding mismatch: key"):
            tally(directory, require_bound=True)
    else:
        result = tally(directory, require_bound=True)
        assert result["tasks"][0]["pairs"][0]["votes"] == {"codex-m1-high": "A"}
    assert reads == 1


def test_queued_call_refuses_changed_launch_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path))
    calls = _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    messages: list[str] = []

    def progress(message: str) -> None:
        messages.append(message)
        if message == "codex-m1-high task-1: done":
            (directory / "judge-specs/codex-m1-high.json").write_text(
                json.dumps({"harness": "codex", "model": "changed", "effort": "high"})
            )

    assert (
        judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=progress) == 1
    )
    assert len(calls) == 1
    assert not (directory / "results/codex-m1-high/task-2.json").exists()
    assert any("task-2: failed:" in message and "judge_spec" in message for message in messages)


def test_workspace_filename_cannot_replace_key_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _prepare(tmp_path, _two_arms(tmp_path), tasks=["alpha"])
    (directory / "task-1/key").write_text("ordinary workspace file")
    _fake_judge(monkeypatch, {"m1": "A"}, root=tmp_path)
    assert judge.run(directory, judges=["codex:m1:high"], timeout=60, jobs=1, progress=print) == 0
    path = directory / "key.json"
    key = json.loads(path.read_text())
    pair = key["tasks"]["task-1"]["pairs"]["1"]
    pair["A"], pair["B"] = pair["B"], pair["A"]
    path.write_text(json.dumps(key))
    with pytest.raises(ValueError, match="task-1.json.*binding mismatch: key"):
        tally(directory, require_bound=True)
