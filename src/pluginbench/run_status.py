"""A read-only projection of one run directory, for `pluginbench status` and its dashboard.

It reads the ledger (read-only), evidence receipts, events.jsonl and the report, and never
writes anything. Runs from before the ledger fall back to evidence/run.json.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pluginbench import run_lock
from pluginbench.evaluation_report import correctness_state
from pluginbench.ledger import LEDGER_NAME, Ledger

# Bump on any change that removes or renames a field; adding a field keeps the version.
SCHEMA_VERSION = 1

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
# Native attempts have one stage; its receipts live under attempts/<id>/handoff/.
_STAGE = "handoff"
# sha256 of no bytes: the verifier graded an empty patch.
_EMPTY_PATCH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _arm(name: str) -> str:
    """Runs before the rename call the vanilla arm `baseline`."""
    return "vanilla" if name == "baseline" else name


def _read(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def _usage(execution: dict[str, Any] | None) -> tuple[dict[str, int] | None, str]:
    """Token usage and how far to trust it: exact, lower_bound, or unknown."""
    if execution is None:
        return None, "unknown"
    usage, status = execution.get("usage"), "exact"
    if not isinstance(usage, dict):
        usage = (execution.get("usage_accounting") or {}).get("observed_usage_lower_bound")
        status = "lower_bound"
        if not isinstance(usage, dict):
            return None, "unknown"
    if isinstance(usage.get("total_tokens"), int):
        return usage, status
    # Receipts from runs without subagents carry no total. Codex counts total as input plus
    # output; cached input and reasoning output are subsets of those.
    parts = (usage.get("input_tokens"), usage.get("output_tokens"))
    if all(isinstance(part, int) for part in parts):
        return {**usage, "total_tokens": sum(part for part in parts if part)}, status
    return usage, "lower_bound"


def _add(total: dict[str, int], usage: dict[str, int] | None) -> None:
    for field in TOKEN_FIELDS:
        value = (usage or {}).get(field)
        if isinstance(value, int):
            total[field] = total.get(field, 0) + value


def _tokens(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summed usage; a lower bound when any started attempt lacks exact final usage."""
    total: dict[str, int] = {}
    for row in rows:
        _add(total, row["tokens"])
    return {
        **{field: total.get(field, 0) for field in TOKEN_FIELDS},
        "lower_bound": any(row["tokens_status"] in ("lower_bound", "unknown") for row in rows),
    }


def _outcome(handoff: Path) -> tuple[str, str | None]:
    """pass, overlap_pass, fail, empty, or unknown with the verifier's reason, from the receipts."""
    verifier = _read(handoff / "verifier-result.json") or {}
    if verifier.get("patch_sha256") == _EMPTY_PATCH:
        return "empty", None
    behavior = ((_read(handoff / "correctness.json") or {}).get("checks") or {}).get("behavior")
    if behavior is True:
        return "pass", None
    if behavior is False:
        return "fail", None
    outcome = correctness_state("unknown", verifier)
    if outcome != "unknown":
        return outcome, None
    return "unknown", verifier.get("reason") or "no correctness receipt"


def _slots(evidence: Path, run: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """Live attempts in slot order, and where their states came from."""
    path = evidence / LEDGER_NAME
    if path.is_file():
        try:
            ledger = Ledger.read(path)
        except (sqlite3.Error, ValueError):
            pass  # Busy, or a hot journal a reader cannot roll back: use run.json instead.
        else:
            try:
                rows = ledger.attempts()
            except sqlite3.Error:
                rows = None
            finally:
                ledger.close()
            if rows is not None:
                live = [row for row in rows if row["state"] != "discarded-interrupted"]
                return live, "ledger"
    plan = _read(evidence / "plan.json")
    if plan is None or run is None:
        return [], "none"
    states = run.get("attempt_states", {})
    return [
        {**entry, "slot": slot, "state": states.get(entry["id"], "pending")}
        for slot, entry in enumerate(plan["entries"])
    ], "run.json"


def _measures(report: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per-attempt observations and per-task reference values from report/results.json."""
    if report is None:
        return {}, {}
    by_attempt: dict[str, dict[str, Any]] = {}
    for row in report.get("observations", []):
        by_attempt.setdefault(row["attempt_id"], {})[row["metric"]] = {
            key: row.get(key) for key in ("status", "value", "unit", "reason")
        }
    references: dict[str, dict[str, Any]] = {}
    for row in report.get("reference_values", []):
        references.setdefault(row["task"], {})[row["metric"]] = {
            key: row.get(key) for key in ("status", "value", "reason")
        }
    return by_attempt, references


def _revisions(directory: Path, state_name: str, selected: object) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(directory.glob(f"*/{state_name}")):
        state = _read(path) or {}
        row: dict[str, Any] = {
            "revision": path.parent.name,
            "status": state.get("status"),
            "selected": path.parent.name == selected,
        }
        if "assigned" in state:
            row["assigned"] = state["assigned"]
            row["reviewed"] = sum(
                record.get("status") == "reviewed" for record in state.get("records", [])
            )
        rows.append(row)
    return rows


def _events(root: Path) -> list[dict[str, Any]]:
    path = root / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # A line the writer is still appending; the next poll reads it whole.
    return events


def _error(events: list[dict[str, Any]], run: dict[str, Any] | None) -> str | None:
    """Why the run failed. A stop is not a failure; runs without events only have run.json."""
    if events:
        return events[-1].get("error") if events[-1].get("event") == "failed" else None
    return (run or {}).get("error")


def _heartbeat(root: Path, evidence: Path, now: datetime) -> dict[str, Any] | None:
    """The most recent write to any file the run updates as it works."""
    times = [
        path.stat().st_mtime
        for path in (
            root / "events.jsonl",
            root / "run.log",
            evidence / LEDGER_NAME,
            evidence / "run.json",
        )
        if path.is_file()
    ]
    if not times:
        return None
    at = datetime.fromtimestamp(max(times), UTC)
    return {"at": at.isoformat(), "age_seconds": round((now - at).total_seconds())}


def _pending(root: Path, selection: dict[str, Any], run: dict[str, Any] | None) -> str | None:
    """The first phase the run has not finished, or None when nothing is left."""
    if run is None:
        return "starting"
    if run.get("status") != "complete":
        return "coding"
    if selection.get("measurement_required") and selection.get("measurement_revision") is None:
        return "measuring"
    if selection.get("reviewer") and selection.get("review_revision") is None:
        return "reviewing"
    if not (root / "report/report.html").is_file():
        return "reporting"
    return None


def _phase(
    *,
    pending: str | None,
    run: dict[str, Any] | None,
    active: bool,
    events: list[dict[str, Any]],
) -> str:
    started = [event for event in events if event.get("event") == "started"]
    if active and started and started[-1].get("command") == "review":
        return "reviewing"
    if active and pending:
        return pending
    # The latest operation, such as a re-review of a finished run, may have stopped or failed.
    if events and events[-1].get("event") in ("stopped", "failed"):
        return str(events[-1]["event"])
    # A worker records how it ended before it releases the run. One that was killed, or lost
    # with its host, leaves `started` or `progress` as the last event.
    if not active and events and events[-1].get("event") in ("started", "progress"):
        return "interrupted"
    if pending is None:
        return "complete"
    return "not_started" if run is None else "stopped"


def _resume_blocked(
    pending: str | None, evidence: Path, measurement_revisions: list[dict[str, Any]]
) -> str | None:
    """Why `run` refuses to continue, for the two phases whose started work it cannot replay."""
    if pending == "measuring" and any(
        row["status"] == "collecting" for row in measurement_revisions
    ):
        return "measurement was interrupted while it was collecting and cannot be replayed"
    if pending == "reviewing" and (evidence / "review-revisions/review").exists():
        return "the first review had started, and its model calls cannot be replayed"
    return None


def run_status(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything `status` shows, as the documented JSON object; reads only."""
    root = root.expanduser().absolute()
    selection = _read(root / "evaluation.json")
    if not isinstance(selection, dict):
        raise ValueError(
            f"{root} has no evaluation.json, so it is not a prepared run; give the run "
            "directory that `pluginbench run` printed"
        )
    now = now or datetime.now(UTC)
    evidence = root / "evidence"
    run = _read(evidence / "run.json")
    try:
        owner = run_lock.owner(root)
    except ValueError:
        owner = {}  # Held, but its record is unreadable mid-write.
    active = owner is not None
    events = _events(root)
    pending = _pending(root, selection, run)
    slots, source = _slots(evidence, run)
    plan = _read(evidence / "plan.json") or {}
    decisions = plan.get("baseline_cache") or {}
    origins = (run or {}).get("baseline_origin", {})
    report = _read(root / "report/results.json")
    measured, references = _measures(report)

    attempts = []
    for row in slots:
        handoff = evidence / "attempts" / row["id"] / _STAGE
        execution = _read(handoff / "execution.json")
        tokens, tokens_status = _usage(execution)
        if row["state"] == "pending":
            tokens_status = "not_started"
        elif row["state"] in ("running", "interrupted") and tokens_status == "exact":
            tokens_status = "lower_bound"  # Usage saved so far by an unfinished attempt.
        outcome, reason = _outcome(handoff) if row["state"] == "complete" else (None, None)
        elapsed = (execution or {}).get("elapsed_seconds")
        attempts.append(
            {
                "id": row["id"],
                "slot": row["slot"],
                "task": row["task_id"],
                "arm": _arm(row["arm"]),
                "repeat": row["attempt"],
                "state": row["state"],
                "outcome": outcome,
                "outcome_reason": reason,
                "reused": (origins.get(row["id"]) or {}).get("status") == "hit",
                "tokens": tokens,
                "tokens_status": tokens_status,
                "minutes": round(elapsed / 60, 1) if isinstance(elapsed, int | float) else None,
                "measures": measured.get(row["id"], {}),
            }
        )

    discarded = []
    for path in sorted((evidence / "discarded").glob("*.json")):
        receipt = json.loads(path.read_text())
        stage = (receipt.get("stages") or {}).get(_STAGE) or {}
        reference = stage.get("execution")
        execution = _read(evidence / reference["path"]) if isinstance(reference, dict) else None
        tokens, tokens_status = _usage(execution)
        discarded.append(
            {
                "id": receipt["id"],
                "slot": receipt.get("slot"),
                "task": receipt["task_id"],
                "arm": _arm(receipt["arm"]),
                "repeat": receipt["attempt"],
                "replaced_by": receipt.get("replaced_by"),
                "tokens": tokens,
                # Usage the attempt saved before it was interrupted is at most a floor.
                "tokens_status": "lower_bound" if tokens_status == "exact" else tokens_status,
            }
        )

    arms: dict[str, dict[str, Any]] = {}
    names = dict.fromkeys(attempt["arm"] for attempt in attempts)
    for name in sorted(names, key=lambda arm: arm != "vanilla"):
        rows = [attempt for attempt in attempts if attempt["arm"] == name]
        arms[name] = {
            "attempts": len(rows),
            "states": dict(Counter(row["state"] for row in rows)),
            "outcomes": dict(Counter(row["outcome"] for row in rows if row["outcome"])),
            "reused": sum(row["reused"] for row in rows),
            "tokens": _tokens(rows),
        }
    log = root / "run.log"
    measurement_revisions = _revisions(
        evidence / "observer-revisions", "revision.json", selection.get("measurement_revision")
    )
    blocked = _resume_blocked(pending, evidence, measurement_revisions)
    unfinished = bool(pending) and not active
    return {
        "schema_version": SCHEMA_VERSION,
        "run": str(root),
        "name": selection.get("name"),
        "phase": _phase(pending=pending, run=run, active=active, events=events),
        # Both are null while a process owns the run, and when the run has nothing left to do.
        "resume_command": shlex.join(["pluginbench", "run", str(root)])
        if unfinished and not blocked
        else None,
        "resume_blocked": blocked if unfinished else None,
        "active": active,
        "owner": {key: owner.get(key) for key in ("pid", "pgid", "started_at")} if owner else None,
        "heartbeat": _heartbeat(root, evidence, now),
        "last_event": events[-1] if events else None,
        "error": _error(events, run),
        "states_source": source,
        "attempts_total": len(attempts),
        "states": dict(Counter(attempt["state"] for attempt in attempts)),
        "outcomes": dict(Counter(attempt["outcome"] for attempt in attempts if attempt["outcome"])),
        "tokens": _tokens([attempt for attempt in attempts if not attempt["reused"]] + discarded),
        "reuse": {
            "vanilla_attempts": sum(attempt["arm"] == "vanilla" for attempt in attempts),
            "planned_hits": sum(
                (decision or {}).get("status") == "hit" for decision in decisions.values()
            ),
            "reused": sum(attempt["reused"] for attempt in attempts),
        },
        "discarded": discarded,
        "arms": arms,
        "measures": {
            "selected": selection.get("measurements", []),
            "measurement_revisions": measurement_revisions,
            "review_revisions": _revisions(
                evidence / "review-revisions",
                "review-state.json",
                selection.get("review_revision"),
            ),
            "references": references,
        },
        "report": str(root / "report/report.html")
        if (root / "report/report.html").is_file()
        else None,
        "log": str(log) if log.is_file() else None,
        "attempts": attempts,
    }


def _count(value: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if value >= size:
            return f"{value / size:.1f}{suffix}"
    return str(value)


def _age(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


_RESULTS = ("pass", "overlap_pass", "fail", "unknown", "empty", "running", "interrupted", "pending")


def _shown_state(state: str, active: bool) -> str:
    """Work recorded as in progress in a run no one owns is stale, so it shows as interrupted."""
    return "interrupted" if state in ("running", "collecting") and not active else state


def _result(row: dict[str, Any], active: bool) -> str:
    """An attempt's outcome, or the state shown for it."""
    return str(row["outcome"] or _shown_state(row["state"], active))


def format_status(status: dict[str, Any]) -> str:
    """The terminal view of run_status()."""
    lines = [f"Run: {status['run']}" + (f"  ({status['name']})" if status["name"] else "")]
    phase = f"Phase: {status['phase']}"
    if status["phase"] == "interrupted":
        phase += " (no exit record)"
    if status["owner"]:
        phase += f"   process {status['owner']['pid']} since {status['owner']['started_at']}"
    if status["heartbeat"]:
        phase += f"   last activity {_age(status['heartbeat']['age_seconds'])} ago"
    lines.append(phase)
    if status["error"]:
        lines.append(f"Error: {status['error']}")
    if status["resume_command"]:
        lines.append(f"Resume: {status['resume_command']}")
    if status["resume_blocked"]:
        lines.append(f"Resume: not possible; {status['resume_blocked']}")
    states: Counter[str] = Counter()
    for state, count in status["states"].items():
        states[_shown_state(state, status["active"])] += count
    summary = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
    lines.append(
        f"Attempts: {status['attempts_total']} slots" + (f": {summary}" if summary else "")
    )
    if status["discarded"]:
        lines.append(f"Discarded: {len(status['discarded'])} interrupted attempts, replaced")
    reuse = status["reuse"]
    if reuse["vanilla_attempts"]:
        lines.append(
            f"Reuse: {reuse['reused']} of {reuse['vanilla_attempts']} vanilla attempts from the "
            f"cache ({reuse['planned_hits']} planned)"
        )
    tokens = status["tokens"]
    lines.append(
        f"Tokens spent: {_count(tokens['total_tokens'])}"
        + (
            " (lower bound: some usage is partial or not yet saved)"
            if tokens["lower_bound"]
            else ""
        )
    )
    if status["attempts"]:
        width = max(len("arm"), *(len(arm) for arm in status["arms"]))
        columns = (*_RESULTS, "tokens")
        sizes = [len(column) + 2 for column in columns]
        lines += ["", "arm".ljust(width) + "".join(c.rjust(n) for c, n in zip(columns, sizes))]
        for arm, summary_row in status["arms"].items():
            counts = Counter(
                _result(row, status["active"]) for row in status["attempts"] if row["arm"] == arm
            )
            total = summary_row["tokens"]
            lines.append(
                arm.ljust(width)
                + "".join(str(counts.get(r, 0)).rjust(n) for r, n in zip(_RESULTS, sizes))
                + (_count(total["total_tokens"]) + ("+" if total["lower_bound"] else "")).rjust(
                    max(sizes[-1], 8)
                )
            )
        task_width = max(len(row["task"]) for row in status["attempts"])
        lines.append("")
        results: dict[tuple[str, str], list[str]] = {}
        for row in sorted(status["attempts"], key=lambda row: row["repeat"]):
            results.setdefault((row["task"], row["arm"]), []).append(_result(row, status["active"]))
        for (task, arm), values in sorted(results.items()):
            lines.append(f"  {task.ljust(task_width)}  {arm.ljust(width)}  {' '.join(values)}")
    measures = status["measures"]
    lines.append("")
    for label, key in (("Measurement", "measurement_revisions"), ("Review", "review_revisions")):
        for row in measures[key]:
            progress = f" {row['reviewed']}/{row['assigned']}" if "assigned" in row else ""
            selected = " (selected)" if row["selected"] else ""
            shown = _shown_state(row["status"], status["active"])
            lines.append(f"{label}: {row['revision']} {shown}{progress}{selected}")
    if status["report"]:
        lines.append(f"Report: {status['report']}")
    if status["log"]:
        lines.append(f"Log: {status['log']}")
    return "\n".join(lines)
