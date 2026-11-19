"""A read-only projection of one run directory, for `pluginbench status` and its dashboard.

It reads the ledger (read-only), evidence receipts, events.jsonl and the report, and never
writes anything. Runs from before the ledger fall back to evidence/run.json.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pluginbench import control_store, coordinator, grade, run_lock, trial_containers
from pluginbench.arms import normalize_arm_name
from pluginbench.ledger import LEDGER_NAME, Ledger

# Bump on any change that removes or renames a field, or adds a value to an existing enum;
# adding a field keeps the version. 2: `tokens_status` gained `unvalidated`. 3: `outcome` is
# pass, fail, infra or legacy (grade contract 1); overlap_pass, empty and unknown are gone.
SCHEMA_VERSION = 3

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
# Native attempts have one stage; its receipts live under attempts/<id>/handoff/.
_STAGE = "handoff"
# The `alerts` block's own version, bumped on the same rules as SCHEMA_VERSION; a new `kind`
# or `severity` is a new enum value. 2: `native_completion_failed` added. 3: it is removed; a
# failed session is an attribute of a graded attempt, or an infrastructure error.
ALERTS_SCHEMA_VERSION = 3
# Worst first; status lists alerts in this order.
_SEVERITIES = ("error", "warning")


def _read(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def _usage(execution: dict[str, Any] | None) -> tuple[dict[str, int] | None, str]:
    """Token usage and trust: exact, unvalidated, lower_bound, or unknown."""
    if execution is None:
        return None, "unknown"
    usage, status = execution.get("usage"), "exact"
    if (execution.get("usage_validation") or {}).get("status") != "validated":
        status = "unvalidated"
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
        "lower_bound": any(
            row["tokens_status"] in ("lower_bound", "unknown")
            or row.get("state") in ("running", "interrupted")
            for row in rows
        ),
        "unvalidated": any(row["tokens_status"] == "unvalidated" for row in rows),
    }


def _failure_reason(error: object) -> str | None:
    if isinstance(error, dict):  # Harbor's exception_info for the agent.
        return f"{error.get('exception_type')}: {error.get('exception_message')}"
    return str(error) if error else None


def _grades(evidence: Path, selection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each attempt's handoff grade record, or its grade-stage infrastructure error."""
    reference = selection.get("source_only_grades")
    index = _read(evidence / reference["path"]) if isinstance(reference, dict) else None
    if not isinstance(index, dict):
        return {}
    grades = {
        attempt: record
        for attempt, stages in index.get("records", {}).items()
        if isinstance(stage := stages.get(_STAGE), dict)
        and isinstance(record := _read(evidence / stage["path"]), dict)
    }
    for attempt, stages in index.get("pending", {}).items():
        if _STAGE in stages:
            grades[attempt] = {
                "grade_contract": index.get("grade_contract"),
                "status": "infra",
                "reason": stages[_STAGE],
            }
    return grades


def _outcome(
    row: dict[str, Any], handoff: Path, grades: dict[str, dict[str, Any]], graded_run: bool
) -> tuple[str | None, str | None]:
    """The attempt's grade (pass, fail, infra, legacy), from its one grade record; else None.

    An attempt whose coding ended in an infrastructure error is `infra` until a resume
    replaces it. A complete attempt reads None until the grade stage has graded it.
    """
    if row["state"] == "interrupted":
        receipt = _read(handoff / grade.INFRASTRUCTURE_ERROR)
        return ("infra", receipt.get("reason")) if isinstance(receipt, dict) else (None, None)
    if row["state"] != "complete":
        return None, None
    if not graded_run:
        return "legacy", f"graded before grade contract {grade.GRADE_CONTRACT}"
    return grade.outcome(grades.get(row["id"])) or (None, "not graded yet")


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
        if path.parent.name.startswith("."):
            continue  # A revision a crash left in staging, never published.
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


def _unavailable_measures(
    failures: list[dict[str, Any]], attempts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One alert per measure family and arm whose collection failed on some attempts."""
    arms = {attempt["id"]: attempt["arm"] for attempt in attempts}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for failure in failures:
        arm = arms.get(failure["attempt_id"], "unknown")
        groups.setdefault((failure["family"], arm), []).append(failure)
    alerts = []
    for (family, arm), rows in sorted(groups.items()):
        count = len({row["attempt_id"] for row in rows})
        tasks = sorted({row["task"] for row in rows})
        # The receipts' own reasons say what broke; the outcome's reason is the generic fallback.
        reasons = list(
            dict.fromkeys(
                str(reason)
                for row in rows
                for reason in [d for d in row.get("diagnostics", []) if d] or [row.get("reason")]
            )
        )
        alerts.append(
            {
                "kind": "measure_unavailable",
                # Without the vanilla value, no arm's comparison on that task has this measure.
                "severity": "error" if arm == "vanilla" else "warning",
                "message": f"{family} unavailable for {count} {arm} attempts on "
                f"{len(tasks)} tasks: {'; '.join(reasons)}",
                "family": family,
                "arm": arm,
                "attempts": count,
                "tasks": tasks,
                "reasons": reasons,
            }
        )
    return alerts


def _alerts(
    *,
    evidence: Path,
    selection: dict[str, Any],
    report: dict[str, Any] | None,
    error: str | None,
    attempts: list[dict[str, Any]],
    ungraded: dict[str, str],
) -> dict[str, Any]:
    """What an operator should act on, derived from the saved state on every read."""
    items: list[dict[str, Any]] = []
    if error:
        items.append({"kind": "run_failed", "severity": "error", "message": error})
    if ungraded:
        reasons = sorted(set(ungraded.values()))
        items.append(
            {
                "kind": "grades_pending",
                "severity": "error",
                "message": f"{len(ungraded)} attempts have no grade: the grade stage hit an "
                "infrastructure error, and the next `pluginbench run` grades them again: "
                + "; ".join(reasons),
                "attempts": len(ungraded),
                "tasks": sorted(
                    {row["task"] for row in attempts if row["id"] in ungraded}
                ),
                "reasons": reasons,
            }
        )
    # Only a report built from the selected revision counts: one that names an earlier
    # revision is not rebuilt yet, and its failures may since have been measured.
    collection = (report or {}).get("measurement_collection") or {}
    selected = selection.get("measurement_revision")
    revision = selected if selected is not None and collection.get("revision") == selected else None
    if revision is not None:
        items += _unavailable_measures(collection.get("failures", []), attempts)
    for row in selection.get("not_measured", []):
        items.append(
            {
                "kind": "not_measured",
                "severity": "warning",
                "message": f"{row['task']}: {row['measure']} not measured: {row['reason']}",
                **{key: row.get(key) for key in ("task", "measure", "reason")},
            }
        )
    items.sort(key=lambda item: _SEVERITIES.index(item["severity"]))
    return {
        "schema_version": ALERTS_SCHEMA_VERSION,
        "measurement_revision": revision,
        "items": items,
    }


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


def _pending(
    root: Path, selection: dict[str, Any], run: dict[str, Any] | None, *, grading: bool
) -> str | None:
    """The first phase the run has not finished, or None when nothing is left.

    `grading` is whether the grade stage has attempts left: not run yet, or an attempt with
    an infrastructure error, which is no result.
    """
    if run is None:
        return "starting"
    if run.get("status") != "complete":
        return "coding"
    if selection.get("measurement_required") and selection.get("measurement_revision") is None:
        return "measuring"
    if selection.get("reviewer") and selection.get("review_revision") is None:
        return "reviewing"
    if grading:
        return "grading"
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
    # A finished run that `review` or `measure` owns is revised, not complete.
    command = started[-1].get("command") if active and started else None
    if command == "review":
        return "reviewing"
    if command == "measure":
        return "measuring"
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


def _host(root: Path) -> dict[str, Any]:
    """What `app status` shows of the host, with what concerns this run: the coordinator, the
    verified execution lock owner, a reconciling reason, what the next command must verify
    first, and this run's background launches. Unlike `app status`, it never rolls back a write
    a killed command left in the control database."""
    try:
        current = coordinator.status(recover=False)
        blocked = [] if current["execution"]["held"] else run_lock.unreleased()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {"error": f"{exc}; `pluginbench app status` reads and repairs it"}
    target = str(root.resolve())
    execution = current["execution"]
    # Only a verified holder's record says who executes; a stale one names a past owner.
    owner = execution["owner"] if execution["verified"] else {}
    return {
        "error": None,
        "coordinator": current["coordinator"],
        "execution": {
            "verified": execution["verified"],
            "kind": owner.get("kind"),
            "root": owner.get("root"),
            "pid": owner.get("pid"),
            "this_run": owner.get("root") == target,
        }
        if execution["held"]
        else None,
        "reconciling": current["reconciling"],
        # A free lock whose last owner did not release it: every command waits on its checks.
        "blocked": [
            {
                "name": control_store.obligation_name(item),
                "kind": item.get("kind"),
                "root": item.get("root"),
                "this_run": item.get("root") == target,
                "problem": item.get("problem"),
            }
            for item in blocked
        ],
        "launches": [
            {key: launch[key] for key in ("launch_id", "kind", "status", "desired")}
            for launch in current["launches"]
            if launch["kind"] != "coordinator" and launch["target"] == target
        ],
    }


def _left_running(evidence: Path) -> dict[str, Any]:
    """Containers of this run's trials still running with no live owner, or why none was read."""
    try:
        # Status only reports, so it uses the selected Docker context: the engine a coding
        # attempt picks. Pinning the saved runtime would hide them once the Docker CLI changed.
        containers = trial_containers.running(evidence, expected_runtime=None)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"checked": False, "error": str(exc), "containers": [], "stop_command": None}
    return {
        "checked": True,
        "error": None,
        "containers": containers,
        "stop_command": shlex.join(["docker", "stop", *(row["id"] for row in containers)])
        if containers
        else None,
    }


def run_status(
    root: Path, *, now: datetime | None = None, containers: bool = True
) -> dict[str, Any]:
    """Everything `status` shows, as the documented JSON object; reads only.

    With `containers=False` it runs no Docker command and reads no host state: `left_running`
    and `host` are null, for observers that poll many runs.
    """
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
    slots, source = _slots(evidence, run)
    plan = _read(evidence / "plan.json") or {}
    rebuild = (
        f"rebuild with `{shlex.join(['pluginbench', 'report', str(root)])}` "
        "to re-validate token usage"
    )
    decisions = plan.get("baseline_cache") or {}
    graded_run = plan.get("grade_contract") == grade.GRADE_CONTRACT
    grades = _grades(evidence, selection)
    ungraded = {
        attempt: str(record.get("reason"))
        for attempt, record in grades.items()
        if record.get("status") == "infra"
    }
    pending = _pending(
        root,
        selection,
        run,
        grading=graded_run and (selection.get("source_only_grades") is None or bool(ungraded)),
    )
    origins = (run or {}).get("baseline_origin", {})
    report = _read(root / "report/results.json")
    measured, references = _measures(report)
    # A rebuilt report re-validates saved usage without writing evidence, so its verdict
    # stands in for a missing execution-current.json.
    report_validation = {
        row["attempt_id"]: validation
        for row in (report or {}).get("observations", [])
        if isinstance(validation := (row.get("provenance") or {}).get("usage_validation"), dict)
    }

    attempts = []
    for row in slots:
        handoff = evidence / "attempts" / row["id"] / _STAGE
        current = handoff / "execution-current.json"
        execution = _read(current if current.is_file() else handoff / "execution.json")
        validation = report_validation.get(row["id"])
        if execution is not None and not current.is_file() and validation is not None:
            execution = {
                **execution,
                "usage_validation": validation,
                # A rejection keeps its re-parsed usage for the record; it is not a usable figure.
                "usage": validation.get("validated_usage")
                if validation.get("status") == "validated"
                else None,
                "usage_accounting": None,
            }
        tokens, tokens_status = _usage(execution)
        if row["state"] == "pending":
            tokens_status = "not_started"
        elif row["state"] in ("running", "interrupted") and tokens_status == "exact":
            tokens_status = "lower_bound"  # Usage saved so far by an unfinished attempt.
        outcome, reason = _outcome(row, handoff, grades, graded_run)
        elapsed = (execution or {}).get("elapsed_seconds")
        attempts.append(
            {
                "id": row["id"],
                "slot": row["slot"],
                "task": row["task_id"],
                "arm": normalize_arm_name(row["arm"]),
                "repeat": row["attempt"],
                "state": row["state"],
                "outcome": outcome,
                "outcome_reason": reason,
                "reused": (origins.get(row["id"]) or {}).get("status") == "hit",
                "tokens": tokens,
                "tokens_status": tokens_status,
                "tokens_reason": rebuild
                if tokens_status == "unvalidated"
                else "; ".join(
                    ((execution or {}).get("usage_validation") or {}).get("reasons", [])
                ) or None,
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
                "arm": normalize_arm_name(receipt["arm"]),
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
    error = _error(events, run)
    return {
        "schema_version": SCHEMA_VERSION,
        "run": str(root),
        "name": selection.get("name"),
        "phase": _phase(pending=pending, run=run, active=active, events=events),
        # Whether the saved state can continue is `run`'s decision: it checks first and refuses
        # before any model call. Status only names the command for an unfinished, unowned run.
        "resume_command": shlex.join(["pluginbench", "run", str(root)])
        if pending and not active
        else None,
        "active": active,
        "owner": {key: owner.get(key) for key in ("pid", "pgid", "started_at")} if owner else None,
        "heartbeat": _heartbeat(root, evidence, now),
        "last_event": events[-1] if events else None,
        "error": error,
        "alerts": _alerts(
            evidence=evidence,
            selection=selection,
            report=report,
            error=error,
            attempts=attempts,
            ungraded=ungraded,
        ),
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
            "measurement_revisions": _revisions(
                evidence / "observer-revisions",
                "revision.json",
                selection.get("measurement_revision"),
            ),
            "review_revisions": _revisions(
                evidence / "review-revisions",
                "review-state.json",
                selection.get("review_revision"),
            ),
            "references": references,
            # Saved by check; a run saved before that list existed has none.
            "not_measured": selection.get("not_measured", []),
        },
        "report": str(root / "report/report.html")
        if (root / "report/report.html").is_file()
        else None,
        "log": str(log) if log.is_file() else None,
        # Only the run.lock holder stops them, so they are named while no process owns the run.
        "left_running": None if active or not containers else _left_running(evidence),
        "host": _host(root) if containers else None,
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


# A complete attempt with no grade yet shows its state, `complete`, until the grade stage runs.
_RESULTS = ("pass", "fail", "infra", "legacy", "complete", "running", "interrupted", "pending")


def _shown_state(state: str, active: bool) -> str:
    """Work recorded as in progress in a run no one owns is stale, so it shows as interrupted."""
    return "interrupted" if state in ("running", "collecting") and not active else state


def _result(row: dict[str, Any], active: bool) -> str:
    """An attempt's outcome, or the state shown for it."""
    return str(row["outcome"] or _shown_state(row["state"], active))


def _host_lines(host: dict[str, Any] | None) -> list[str]:
    """The host's coordinator and execution owner in one line, then only what needs action."""
    if host is None:
        return []
    if host["error"]:
        return [f"Host: could not read its state: {host['error']}"]
    found = host["coordinator"]
    line = f"Host: coordinator process {found['pid']}" if found else "Host: no coordinator"
    execution = host["execution"]
    if execution is None:
        line += "; idle"
    elif not execution["verified"]:
        line += "; executing for a process PluginBench cannot verify"
    elif execution["this_run"]:
        line += f"; executing this run ({execution['kind']}, process {execution['pid']})"
    else:
        line += f"; executing {execution['kind']} for {execution['root']}"
    lines = [line]
    if host["reconciling"]:
        lines.append(f"Reconciling: {host['reconciling']}")
    for item in host["blocked"]:
        if item["problem"]:
            lines.append(
                f"Blocked: the host's owner record cannot be trusted: {item['problem']}. Check "
                "that nothing PluginBench started still runs, then run `pluginbench app cleanup "
                f"--confirm-stopped {item['root']}`"
            )
            continue
        where = "this run" if item["this_run"] else item["root"]
        lines.append(
            f"Blocked: crashed {item['kind']} for {where} ({item['name']}) must be verified "
            "first; run `pluginbench app cleanup`"
        )
    lines.extend(
        f"Launch {launch['launch_id']}: {launch['kind']}, {launch['status']}, desired "
        f"{launch['desired']}"
        for launch in host["launches"]
    )
    return lines


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
    for alert in status["alerts"]["items"]:
        # Collector reasons can carry raw stderr; keep each alert on one terminal line.
        message = " ".join(alert["message"].split())
        lines.append(f"{alert['severity'].capitalize()}: {message}")
    if status["resume_command"]:
        lines.append(f"Resume: {status['resume_command']}")
    lines.extend(_host_lines(status["host"]))
    left = status["left_running"]
    if left and not left["checked"]:
        lines.append(f"Containers: could not check for ones left running: {left['error']}")
    elif left and left["containers"]:
        projects = ", ".join(dict.fromkeys(row["project"] for row in left["containers"]))
        lines.append(f"Containers left running with no live owner: {projects}")
        lines.append(f"Stop them: {left['stop_command']}")
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
    if any(
        row["tokens_status"] == "unvalidated"
        for row in [*status["attempts"], *status["discarded"]]
    ):
        lines.append(
            "Recorded token usage is unvalidated; "
            f"rebuild with `{shlex.join(['pluginbench', 'report', status['run']])}` "
            "to re-validate token usage"
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
