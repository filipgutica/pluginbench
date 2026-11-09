"""Blinded pairwise judging of two arms' matched submissions: prepare, run, and tally.

`prepare` reads saved evidence only. Each submission's diff comes from its review packet,
with headers normalized so neither arm's tooling fingerprints it. `run` sends one read-only
call per judge and task through the reviewer launch path; the judge's workspace is a copy of
one task folder, so the key, arm names, traces, and verifier results are never inside it.
`tally` unblinds with the key and applies the correctness-first, strict-majority rule.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Literal

from pydantic import StrictInt, StrictStr

from pluginbench import codex, study
from pluginbench.assessment_models import InputModel
from pluginbench.compare import ARM_NAMES
from pluginbench.evaluation_report import correctness_state
from pluginbench.parallel import run_jobs, validate_jobs
from pluginbench.study_review import check_read_only_trace, verified_final

KEY = "key.json"
PANEL = "panel.json"
SCHEMA = "response-schema.json"
# An overlap pass counts as a pass: the verifier graded the production code with its own tests.
PASSING = {"pass", "overlap_pass"}
HARNESSES = {"codex", "claude"}

BRIEF = """\
You are judging code changes as the engineer who will maintain this code next. Work \
read-only: do not edit any file, run state-changing commands, or start subagents.

Read the review standard first: review-standard.md. Its checks are the only criteria.

Task given to the authors: instruction.md. Read-only starting source: source/. Read the \
starting source wherever you need context.

Each pair below holds two independent changes, A and B, that try to solve the same task from \
the same starting source. Read each diff. Judge the code each change leaves behind. Do not \
reward length, comments, or explanation. Do not try to guess which tool or process wrote \
either change. An empty diff makes no change.

{pairs}

For each pair, decide which change leaves the code in better shape for its next maintainer \
under the review standard: A, B, or tie. Use tie only when you cannot separate them on any \
check.

End your final message with only this JSON, one entry per pair:

{{"pairs": [{{"pair": 1, "preferred": "A", "decisive_checks": [3, 8], "reason": "One or two \
sentences that name the concrete code that decided it."}}]}}
"""


class PairVerdict(InputModel):
    pair: StrictInt
    preferred: Literal["A", "B", "tie"]
    decisive_checks: list[StrictInt]
    reason: StrictStr


class Verdicts(InputModel):
    pairs: list[PairVerdict]


@dataclass(frozen=True)
class Submission:
    run: Path
    arm: str
    attempt: str
    repeat: int
    correctness: str
    before_digest: str
    packet: Path


def normalize_diff(text: str) -> str:
    """Rewrite file headers to `a/` and `b/` and drop `index` lines.

    Review packets diff `before/` against `after/` with `--no-prefix`; other sources use
    `a/` and `b/` with other hash lengths. Either fingerprints its source, so both become
    `diff --git a/<path> b/<path>` with no `index` line. Hunks are untouched.
    """
    lines = []
    in_header = False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        if body.startswith("diff --git "):
            in_header = True
            path = _header_path(body.removeprefix("diff --git "))
            line = f"diff --git {_prefixed('a', path)} {_prefixed('b', path)}\n"
        elif in_header and (body.startswith("@@") or body == "GIT binary patch"):
            in_header = False
        elif in_header and body.startswith("index "):
            continue
        elif in_header and body.startswith(("--- ", "+++ ")) and body[4:] != "/dev/null":
            marker, side = body[:4], "a" if body.startswith("---") else "b"
            line = f"{marker}{_prefixed(side, _strip_prefix(body[4:]))}\n"
        lines.append(line)
    return "".join(lines)


_PREFIXES = ("before/", "after/", "a/", "b/")


def _strip_prefix(token: str) -> str:
    quote = '"' if token.startswith('"') and token.endswith('"') else ""
    inner = token[1:-1] if quote else token
    for prefix in _PREFIXES:
        if inner.startswith(prefix):
            return quote + inner.removeprefix(prefix) + quote
    raise ValueError(f"unexpected diff path {token!r}")


def _prefixed(side: str, path: str) -> str:
    return f'"{side}/{path[1:]}' if path.startswith('"') else f"{side}/{path}"


def _header_path(header: str) -> str:
    """The one path in `X/p Y/p`; renames are off, so both sides name the same file."""
    for quote in ("", '"'):
        for first in _PREFIXES:
            for second in _PREFIXES:
                extra = len(first) + len(second) + 1 + 4 * len(quote)
                length, odd = divmod(len(header) - extra, 2)
                if odd or length <= 0:
                    continue
                path = header[len(quote) + len(first) :][:length]
                if header == f"{quote}{first}{path}{quote} {quote}{second}{path}{quote}":
                    return f'"{path}"' if quote else path
    raise ValueError(f"unexpected diff header {header!r}")


def parse_arm(spec: str) -> tuple[Path, str]:
    directory, _, arm = spec.partition("=")
    if not directory or not arm:
        raise ValueError(f"{spec!r}: give RUN_DIR=ARM")
    return Path(directory), ARM_NAMES.get(arm, arm)


def _packets(run: Path, base: str, wanted: set[str]) -> dict[str, tuple[str, Path]]:
    """Each wanted attempt's (submission digest, packet), from the first revision that has one."""
    found: dict[str, tuple[str, Path]] = {}
    revisions = run / base / "review-revisions"
    for revision in sorted(revisions.iterdir()) if revisions.is_dir() else []:
        mapping = revision / "mapping.json"
        if not mapping.is_file():
            continue
        for row in json.loads(mapping.read_text()):
            packet = revision / "packets" / str(row["packet_id"])
            if (
                row["id"] in wanted
                and row["id"] not in found
                and (packet / "source.diff").is_file()
            ):
                found[row["id"]] = (str(row["submission_digest"]), packet)
    return found


def load_submissions(
    run: Path, arm: str, tasks: Sequence[str]
) -> dict[tuple[str, int], Submission]:
    """One arm's submissions on the given tasks, keyed by (task, repeat), bound to packets."""
    results = run / "report" / "results.json"
    if not results.is_file():
        raise ValueError(f"{run} has no report/results.json; run `pluginbench report`")
    data = json.loads(results.read_text())
    rows = [
        row
        for row in data["attempts"]
        if ARM_NAMES.get(row["guidance"], row["guidance"]) == arm and row["task"] in tasks
    ]
    if not rows:
        raise ValueError(f"{run} has no {arm} attempts on the given tasks")
    packets = _packets(run, str(data.get("artifact_base", "evidence")), {row["id"] for row in rows})
    submissions: dict[tuple[str, int], Submission] = {}
    for row in rows:
        key = (row["task"], int(row["repeat"]))
        where = f"{run} {arm} {row['task']} repeat {key[1]}"
        if key in submissions:
            raise ValueError(f"{where} appears twice")
        if row["id"] not in packets:
            raise ValueError(f"{where} has no review packet with source.diff")
        digest, packet = packets[row["id"]]
        artifacts = row["artifacts"]
        binding = json.loads(packet.with_name(packet.name + ".json").read_text())
        if digest != artifacts["snapshot_digest"]:
            raise ValueError(f"{where}: review packet is bound to another submission")
        if binding["before_digest"] != artifacts["before_digest"]:
            raise ValueError(f"{where}: review packet is bound to another starting source")
        submissions[key] = Submission(
            run=run,
            arm=arm,
            attempt=row["id"],
            repeat=key[1],
            correctness=correctness_state(row["correctness"], row.get("native_grading")),
            before_digest=artifacts["before_digest"],
            packet=packet,
        )
    return submissions


def prepare(
    *,
    arms: Sequence[str],
    tasks: Sequence[str],
    exclude: Sequence[str],
    criteria: Path,
    seed: int,
    output: Path,
) -> Path:
    """Write one blinded task folder and brief per task, and the key that unblinds them."""
    if len(arms) != 2:
        raise ValueError("give exactly two RUN_DIR=ARM specs")
    (first_run, first_arm), (second_run, second_arm) = (parse_arm(spec) for spec in arms)
    if first_arm == second_arm:
        raise ValueError(f"both sides are {first_arm}; judge two different arms")
    if not tasks or len(set(tasks)) != len(tasks):
        raise ValueError("--tasks must name at least one task, each once")
    refused = sorted(set(tasks) & set(exclude))
    if refused:
        raise ValueError(f"refusing excluded tasks: {', '.join(refused)}")
    if output.exists():
        raise ValueError(f"{output} already exists; give a new directory")
    standard = criteria.read_text()
    sides = {
        first_arm: load_submissions(first_run, first_arm, tasks),
        second_arm: load_submissions(second_run, second_arm, tasks),
    }
    rng = random.Random(seed)
    output.mkdir(parents=True)
    folders: dict[str, object] = {}
    for index, task in enumerate(tasks, start=1):
        name = f"task-{index}"
        repeats = sorted(
            repeat
            for (owner, repeat) in sides[first_arm].keys() & sides[second_arm].keys()
            if owner == task
        )
        if not repeats:
            raise ValueError(f"{task} has no repeat present in both {first_arm} and {second_arm}")
        folder = output / name
        folder.mkdir()
        instruction: bytes | None = None
        pairs: dict[str, object] = {}
        lines = []
        for number, repeat in enumerate(repeats, start=1):
            pair = (sides[first_arm][task, repeat], sides[second_arm][task, repeat])
            if pair[0].before_digest != pair[1].before_digest:
                raise ValueError(f"{task} repeat {repeat}: the arms start from different source")
            for submission in pair:
                text = (submission.packet / "task.txt").read_bytes()
                if instruction not in (None, text):
                    raise ValueError(f"{task}: the arms were given different instructions")
                instruction = text
            if rng.random() < 0.5:
                pair = (pair[1], pair[0])
            for side, submission in zip("AB", pair, strict=True):
                diff = (submission.packet / "source.diff").read_text()
                (folder / f"pair-{number}-{side}.diff").write_text(normalize_diff(diff))
            pairs[str(number)] = {
                side: {
                    "arm": submission.arm,
                    "run": str(submission.run.resolve()),
                    "attempt": submission.attempt,
                    "repeat": submission.repeat,
                    "correctness": submission.correctness,
                }
                for side, submission in zip("AB", pair, strict=True)
            }
            lines.append(f"- Pair {number}: A = pair-{number}-A.diff, B = pair-{number}-B.diff")
        assert instruction is not None
        (folder / "instruction.md").write_bytes(instruction)
        (folder / "review-standard.md").write_text(standard)
        (folder / "source").symlink_to(
            (sides[first_arm][task, repeats[0]].packet / "before").resolve()
        )
        study._write(folder / SCHEMA, Verdicts.model_json_schema())
        (output / f"{name}.brief.md").write_text(BRIEF.format(pairs="\n".join(lines)))
        folders[name] = {"task": task, "pairs": pairs}
    study._write(
        output / KEY,
        {
            "schema_version": 1,
            "arms": {first_arm: str(first_run.resolve()), second_arm: str(second_run.resolve())},
            "seed": seed,
            "criteria": str(criteria.resolve()),
            "tasks": folders,
        },
    )
    return output


@dataclass(frozen=True)
class Judge:
    harness: str
    model: str
    effort: str

    @property
    def label(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "-", f"{self.harness}-{self.model}-{self.effort}")


def parse_judge(spec: str) -> Judge:
    harness, model, effort = (spec.split(":") + ["", ""])[:3]
    if harness not in HARNESSES or not model or not effort or spec.count(":") != 2:
        raise ValueError(f"{spec!r}: give HARNESS:MODEL:EFFORT with harness codex or claude")
    return Judge(harness, model, effort)


def _read_key(directory: Path) -> dict[str, Any]:
    path = directory / KEY
    if not path.is_file():
        raise ValueError(f"{directory} has no {KEY}; run `pluginbench judge prepare` first")
    key: dict[str, Any] = json.loads(path.read_text())
    return key


def _panel(directory: Path) -> list[str]:
    path = directory / PANEL
    return list(json.loads(path.read_text())) if path.is_file() else []


def _verdict_path(directory: Path, judge: str, task: str) -> Path:
    return directory / "results" / judge / f"{task}.json"


def run(
    directory: Path,
    *,
    judges: Sequence[str],
    timeout: int,
    jobs: int,
    progress: Callable[[str], None],
) -> int:
    """Make each missing judge call; return how many calls failed. Completed calls are kept."""
    validate_jobs(jobs)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    panel = [parse_judge(spec) for spec in judges]
    if not panel or len({judge.label for judge in panel}) != len(panel):
        raise ValueError("give at least one --judge, each once")
    tasks = _read_key(directory)["tasks"]
    # Record every requested judge before any call, so tally can refuse a panel cut short.
    study._write(directory / PANEL, sorted(set(_panel(directory)) | {j.label for j in panel}))
    calls = [
        (judge, task)
        for judge in panel
        for task in tasks
        if not _verdict_path(directory, judge.label, task).is_file()
    ]
    progress(f"{len(calls)} judge calls to make; {len(panel) * len(tasks) - len(calls)} done.")
    failures: list[str] = []

    def call(
        item: tuple[Judge, str], cancelled: Event, emit: Callable[[Callable[[], None]], None]
    ) -> None:
        judge, task = item
        if cancelled.is_set():
            return
        try:
            _judge_task(directory, judge, task, expected=set(tasks[task]["pairs"]), timeout=timeout)
            progress(f"{judge.label} {task}: done")
        # A failed call leaves no verdict file, so the next run makes it again.
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append(f"{judge.label} {task}")
            progress(f"{judge.label} {task}: failed: {exc}")

    run_jobs(calls, jobs=jobs, operation=call)
    return len(failures)


def _judge_task(
    directory: Path, judge: Judge, task: str, *, expected: set[str], timeout: int
) -> None:
    """One read-only call; raw evidence goes to a new call-N folder, the verdict file last."""
    verdict = _verdict_path(directory, judge.label, task)
    raw = verdict.with_suffix("")
    raw.mkdir(parents=True, exist_ok=True)
    attempt = raw / f"call-{len(list(raw.glob('call-*'))) + 1}"
    folder = directory / task
    with tempfile.TemporaryDirectory(prefix="pluginbench-judge-") as temporary:
        area = Path(temporary)
        workspace = area / "workspace"
        # Copy, never link: the judge's sandbox can read only its workspace.
        shutil.copytree(folder, workspace, symlinks=True, ignore=shutil.ignore_patterns("source"))
        shutil.copytree((folder / "source").resolve(), workspace / "source", symlinks=True)
        try:
            execution = codex.invoke(
                area=area,
                entry={"host": judge.harness},
                protocol={"models": {judge.harness: judge.model}, "reasoning": judge.effort},
                prompt=(directory / f"{task}.brief.md").read_text(),
                timeout=timeout,
                output_schema=workspace / SCHEMA,
            )
        finally:
            artifacts = area / "artifacts"
            if artifacts.exists():
                codex.copy_artifacts(artifacts, attempt)
    final = verified_final(execution, model=judge.model, effort=judge.effort, harness=judge.harness)
    check_read_only_trace(
        execution, harness=judge.harness, forbidden=(str(directory.resolve()), "../", KEY)
    )
    verdicts = Verdicts.model_validate_json(final)
    answered = [str(row.pair) for row in verdicts.pairs]
    if sorted(answered) != sorted(expected):
        raise ValueError(f"judge answered pairs {answered}, expected {sorted(expected)}")
    study._write(verdict, verdicts.model_dump(mode="json"))


def _majority(votes: Sequence[str]) -> str | None:
    """The verdict more than half of the votes share, if any."""
    if not votes:
        return None
    verdict, count = Counter(votes).most_common(1)[0]
    return verdict if count * 2 > len(votes) else None


def tally(directory: Path) -> dict[str, Any]:
    """Unblind and score every pair: correctness first, then a strict majority of judges."""
    key = _read_key(directory)
    tasks: dict[str, Any] = key["tasks"]
    results = directory / "results"
    started = (
        {path.name for path in results.iterdir() if path.is_dir()} if results.is_dir() else set()
    )
    judges = sorted(started | set(_panel(directory)))
    if not judges:
        raise ValueError(f"{directory} has no judge results; run `pluginbench judge run` first")
    missing = [
        f"{judge} {task}"
        for judge in judges
        for task in tasks
        if not _verdict_path(directory, judge, task).is_file()
    ]
    if missing:
        raise ValueError("judge results missing: " + ", ".join(missing))
    votes = {
        (judge, task): {
            str(row["pair"]): str(row["preferred"])
            for row in json.loads(_verdict_path(directory, judge, task).read_text())["pairs"]
        }
        for judge in judges
        for task in tasks
    }
    arms = list(key["arms"])
    wins = dict.fromkeys(arms, 0.0)
    decided = {"correctness": dict.fromkeys(arms, 0.0), "judges": dict.fromkeys(arms, 0.0)}
    side_a = dict.fromkeys(judges, 0)
    agreed = dict.fromkeys(judges, 0)
    comparable = dict.fromkeys(judges, 0)
    per_task = []
    for name, entry in tasks.items():
        task_wins = dict.fromkeys(arms, 0.0)
        rows = []
        for number, sides in entry["pairs"].items():
            pair_votes = {judge: votes[judge, name][number] for judge in judges}
            for judge, vote in pair_votes.items():
                side_a[judge] += vote == "A"
                others = _majority([v for other, v in pair_votes.items() if other != judge])
                if others is not None:
                    comparable[judge] += 1
                    agreed[judge] += vote == others
            passed = {side: sides[side]["correctness"] in PASSING for side in "AB"}
            if passed["A"] != passed["B"]:
                by, shares = "correctness", {"A": float(passed["A"]), "B": float(passed["B"])}
            else:
                majority = _majority(list(pair_votes.values()))
                by = "judges"
                shares = (
                    {"A": float(majority == "A"), "B": float(majority == "B")}
                    if majority in ("A", "B")
                    else {"A": 0.5, "B": 0.5}
                )
            for side in "AB":
                arm = sides[side]["arm"]
                wins[arm] += shares[side]
                task_wins[arm] += shares[side]
                decided[by][arm] += shares[side]
            rows.append(
                {
                    "pair": int(number),
                    "decided_by": by,
                    "wins": {sides[side]["arm"]: shares[side] for side in "AB"},
                    "sides": sides,
                    "votes": pair_votes,
                }
            )
        per_task.append({"task": entry["task"], "folder": name, "wins": task_wins, "pairs": rows})
    pairs_total = sum(len(row["pairs"]) for row in per_task)
    by_correctness = sum(
        pair["decided_by"] == "correctness" for row in per_task for pair in row["pairs"]
    )
    return {
        "schema_version": 1,
        "rule": "If exactly one side passed (pass or overlap pass), it wins without a vote. "
        "Otherwise the side more than half of the judges prefer wins; anything else is half each.",
        "judges": judges,
        "pairs": pairs_total,
        "weighted_wins": wins,
        "correctness_decided": {"pairs": by_correctness, "wins": decided["correctness"]},
        "judged": {"pairs": pairs_total - by_correctness, "wins": decided["judges"]},
        "judge_stats": {
            judge: {
                "side_a_rate": side_a[judge] / pairs_total,
                "agreement": agreed[judge] / comparable[judge] if comparable[judge] else None,
                "agreement_pairs": comparable[judge],
            }
            for judge in judges
        },
        "agreement_definition": "Share of pairs where the judge's vote equals the verdict "
        "more than half of the other judges gave; pairs without such a verdict are left out. "
        "Side-A rate and agreement cover every pair, including correctness-decided ones.",
        "tasks": per_task,
    }


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows
    ]


def format_tally(result: dict[str, Any]) -> str:
    """The tally as text: totals, one row per task, then one row per judge."""
    wins: dict[str, float] = result["weighted_wins"]
    arms = list(wins)

    def scores(values: dict[str, float]) -> str:
        return ", ".join(f"{arm} {values[arm]:g}" for arm in arms)

    correctness, judged = result["correctness_decided"], result["judged"]
    tasks = [["task", "pairs", *arms, "correctness-decided"]] + [
        [
            row["task"],
            str(len(row["pairs"])),
            *(f"{row['wins'][arm]:g}" for arm in arms),
            str(sum(pair["decided_by"] == "correctness" for pair in row["pairs"])),
        ]
        for row in result["tasks"]
    ]
    judges = [["judge", "side-A rate", "agreement"]] + [
        [
            judge,
            f"{row['side_a_rate']:.2f}",
            "-"
            if row["agreement"] is None
            else f"{row['agreement']:.2f} of {row['agreement_pairs']}",
        ]
        for judge, row in result["judge_stats"].items()
    ]
    return "\n".join(
        [
            f"Weighted wins over {result['pairs']} pairs: {scores(wins)}",
            f"Correctness-decided pairs: {correctness['pairs']} ({scores(correctness['wins'])})",
            f"Judged pairs: {judged['pairs']} ({scores(judged['wins'])})",
            "",
            *_table(tasks),
            "",
            *_table(judges),
        ]
    )
