"""Host recovery: nothing starts on a host while a dead owner's work may still run.

The host execution lock's owner record is the fence. Its owner writes it before starting any
work, and records `released` only when it leaves cleanly: its block returned and every cleanup
on the way succeeded. The next owner carries every unreleased record forward as an obligation,
in that same write (`run_lock.hold_execution`).

An owner whose record has schema 3 (`run_lock.CONTAINED_SCHEMA`) ran every coding, grading,
review, collect and preflight call inside an operation it recorded first, and started every
container, build and trial only inside one, labelled with its invocation. Its checks below then
cover all those may have left, so its obligation clears by itself once they pass. The owner's
own host commands (such as `git apply`) run unscoped and start no container, so no check covers
them. An owner record from an earlier build, or one that cannot be read, is one more unknown
check that only `pluginbench repair --confirm-stopped ROOT` clears, after checking again
(`confirm_stopped`). The checks:
- processes: each invocation R's ledger still has open, and each the owner ended. A live process
  in the group the ledger recorded for it blocks, with a hint to stop it (`verify_invocation`).
  For a contained owner, a call with no process record is covered by its operation: one still
  recorded is checked below, one whose cleanup was proven left `cleanup.json`, and one with no
  runtime directory never started, since the directory comes before anything spawns.
- containers, for an owner from an earlier build: for each Docker runtime it recorded, once the
  processes are confirmed. The trial containers R left running are stopped, and two inventories
  at least a second apart must find none (`verify_containers`). One pass has RECOVERY_SECONDS,
  and every Docker command in it is capped by what is left. A contained owner's containers all
  carry a recorded operation's label, so the operation checks below find them.
- each cleanup the owner recorded as failed (`run_lock.note_leftover`), with its reason, unless
  it is a contained owner's failed cleanup of an operation it still records: that operation's
  checks prove it.
- each operation the owner recorded (`invocation_runtime.run_operation`): its systemd scope is
  stopped and proven gone with the processes in it, then the containers carrying its label are
  removed and proven gone (`invocation_runtime.verify_scope`, `verify_labelled`).

Once a contained owner's checks pass, each call and operation they covered gets a `cleanup.json`
in its runtime directory, the proof a resume's own checks read. Repair never confirms a check
that finds something running. A resume repeats the process and container checks for its own
crashed calls before it accepts saved output or marks a call unresolved (`verify_crashed`).
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pluginbench import harbor_backend, isolation, run_lock, trial_containers
from pluginbench.ledger import LEDGER_NAME, OPEN_STATES, Ledger
from pluginbench.run_lock import ExecutionLock, ExecutionOwner

CONTROL_NAME = "control.sqlite"
SCHEMA_VERSION = 2
# How long a held lock may name no verifiable owner before observers report `reconciling`.
UNKNOWN_OWNER_GRACE = 30.0
# Seconds between the two container inventories that must both find nothing.
INVENTORY_INTERVAL = 1.0
# One recovery pass's whole budget; every Docker command in it is capped by what is left.
RECOVERY_SECONDS = 120.0
# Kinds whose external processes no ledger records.
UNRECORDED_KINDS = ("preflight", "measure", "judge", "unknown")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    -- Set while an observer sees the lock held by an owner it cannot verify.
    reconciling TEXT,
    updated_at TEXT NOT NULL
);
-- A process started in the background in its own scope: the coordinator (kind "coordinator"),
-- or a worker that takes the host execution lock for one execution kind. Status moves only
-- forward: launching, then active (claimed by the process itself) or fenced, then finished.
CREATE TABLE IF NOT EXISTS launches (
    launch_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    initiator TEXT NOT NULL CHECK (initiator IN ('cli', 'app')),
    target TEXT NOT NULL,
    version TEXT NOT NULL,
    scope TEXT NOT NULL,
    desired TEXT NOT NULL CHECK (desired IN ('run', 'stop')),
    status TEXT NOT NULL CHECK (status IN ('launching', 'active', 'fenced', 'finished')),
    bootstrap TEXT,
    process TEXT,
    boot_id TEXT NOT NULL,
    expires REAL NOT NULL,
    updated_at TEXT NOT NULL
);
-- At most one active coordinator and one active worker.
CREATE UNIQUE INDEX IF NOT EXISTS one_active
    ON launches ((kind = 'coordinator')) WHERE status = 'active';
-- At most one worker the app is still launching.
CREATE UNIQUE INDEX IF NOT EXISTS one_app_launch
    ON launches (initiator) WHERE initiator = 'app' AND kind != 'coordinator'
    AND status = 'launching';
-- The operator's word that an unknown check left nothing running. Each covers one check.
CREATE TABLE IF NOT EXISTS confirmations (
    check_id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    reason TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
"""

# One thing to verify: {"id", "root", "kind", ...}. Kinds: "invocation" (a ledger call's process
# group), "scope" (an operation's systemd scope), "unrecorded" (unknown until confirmed); and the
# container kinds, "containers" (a run's trial containers) and "labelled" (an operation's).
Check = dict[str, Any]
CONTAINER_KINDS = ("containers", "labelled")


class CleanupBlocked(Exception):
    """Something a crashed owner or call started still runs, so no work starts on this host.

    Not a ValueError: no caller's error handling may turn it into a result or an
    infrastructure error and start more work. The CLI reports it as the command's error.
    """

    # Whether the call it ended had launched its scope, so the call is spent (`ledger.outcome`).
    launched = False


class CleanupUnknown(CleanupBlocked):
    """PluginBench cannot tell whether something still runs; only the operator can confirm."""


class LaunchFenced(ValueError):
    """A launch is no longer current: fenced, stopped, finished, or of another version. Its
    process exits without starting any work."""


@dataclass(frozen=True)
class Launch:
    """One row of `launches`. `expires` is the handshake deadline on the monotonic clock of the
    boot `boot_id` names; on any other boot it has passed."""

    launch_id: str
    kind: str
    initiator: str
    target: str
    version: str
    scope: str
    desired: str = "run"
    status: str = "launching"
    bootstrap: dict[str, Any] | None = None
    process: dict[str, Any] | None = None
    boot_id: str = ""
    expires: float = 0.0

    @property
    def expired(self) -> bool:
        return self.boot_id != run_lock.boot_id() or time.monotonic() >= self.expires

    def operation(self) -> dict[str, Any]:
        """This launch as `invocation_runtime.verify_scope` takes an operation."""
        payload = None if self.process is None else {"cgroup": self.process.get("cgroup")}
        return {"id": self.launch_id, "scope": self.scope, "bootstrap": self.bootstrap,
                "payload": payload}


_LAUNCH_COLUMNS = tuple(Launch.__dataclass_fields__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ControlStore:
    """The host control database. Short transactions; never held across an external command."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, timeout=5)
        try:
            path.chmod(0o600)
            self._db.execute("PRAGMA synchronous = FULL")
            # Only a new or older database is written to, so opening one to read writes nothing.
            # The tables and the version are one transaction: an opener that reads the current
            # version always finds the tables. Concurrent creators wait for each other, and a
            # second run of the script changes nothing.
            if self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                self._db.executescript(
                    f"BEGIN IMMEDIATE;{_SCHEMA}"
                    f"INSERT OR IGNORE INTO host_state (id, updated_at) VALUES (1, '{_now()}');"
                    f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
                )
        except BaseException:
            self._db.close()
            raise

    @classmethod
    @contextmanager
    def open(cls, state: Path) -> Iterator[ControlStore]:
        store = cls(state / CONTROL_NAME)
        try:
            yield store
        finally:
            store._db.close()

    def reconciling(self) -> str | None:
        reason: str | None = self._db.execute("SELECT reconciling FROM host_state").fetchone()[0]
        return reason

    def set_reconciling(self, reason: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE host_state SET reconciling = ?, updated_at = ? WHERE reconciling IS NOT ?",
                (reason, _now(), reason),
            )

    def create_launch(self, launch: Launch) -> None:
        """Record a new launch before anything spawns. Raises sqlite3.IntegrityError while the
        app already launches another worker."""
        values = [
            json.dumps(value) if column in ("bootstrap", "process") and value is not None
            else value
            for column, value in zip(_LAUNCH_COLUMNS, astuple(launch), strict=True)
        ]
        with self._db:
            self._db.execute(
                f"INSERT INTO launches ({', '.join(_LAUNCH_COLUMNS)}, updated_at) "
                f"VALUES ({', '.join('?' * len(_LAUNCH_COLUMNS))}, ?)",
                (*values, _now()),
            )

    def launch(self, launch_id: str) -> Launch | None:
        rows = self._launches("launch_id = ?", (launch_id,))
        return rows[0] if rows else None

    def launches(self, *statuses: str) -> list[Launch]:
        """Launches in any of `statuses`, coordinators included, oldest first."""
        marks = ", ".join("?" * len(statuses))
        return self._launches(f"status IN ({marks})", statuses)

    def _launches(self, where: str, parameters: Sequence[object]) -> list[Launch]:
        rows = self._db.execute(
            f"SELECT {', '.join(_LAUNCH_COLUMNS)} FROM launches WHERE {where} ORDER BY rowid",
            parameters,
        )
        return [
            Launch(**{
                column: json.loads(value) if column in ("bootstrap", "process") and value
                else value
                for column, value in zip(_LAUNCH_COLUMNS, row, strict=True)
            })
            for row in rows
        ]

    def _move(
        self,
        launch_id: str,
        sets: str,
        where: str,
        values: Sequence[object] = (),
        conditions: Sequence[object] = (),
    ) -> bool:
        """Update one launch only where `where` holds; True if it did."""
        with self._db:
            cursor = self._db.execute(
                f"UPDATE launches SET {sets}, updated_at = ? WHERE launch_id = ? AND {where}",
                (*values, _now(), launch_id, *conditions),
            )
        return cursor.rowcount == 1

    def record_bootstrap(self, launch_id: str, identity: dict[str, object]) -> bool:
        """Record a launch's bootstrap before its permit. False once the launch is no longer
        launching: then no permit may be sent."""
        return self._move(
            launch_id, "bootstrap = ?", "status = 'launching'", values=(json.dumps(identity),)
        )

    def claim(self, launch_id: str, process: dict[str, object]) -> bool:
        """The launched process marks its launch active. Only a launch still launching, still
        wanted, and of this process's version can be claimed."""
        return self._move(
            launch_id,
            "status = 'active', process = ?",
            "status = 'launching' AND desired = 'run' AND version = ?",
            values=(json.dumps(process),),
            conditions=(run_lock.execution_version(),),
        )

    def fence(self, launch_id: str) -> bool:
        """Make a launch that was never claimed impossible to claim."""
        return self._move(launch_id, "status = 'fenced'", "status = 'launching'")

    def finish(self, launch_id: str) -> bool:
        return self._move(launch_id, "status = 'finished'", "status != 'finished'")

    def request_stop(self, launch_id: str) -> bool:
        """Persist a stop for a launch that is not finished; its process polls for it."""
        return self._move(launch_id, "desired = 'stop'", "status IN ('launching', 'active')")

    def confirmed(self) -> set[tuple[str, str]]:
        """Each confirmed check as (check id, root): a confirmation covers only the check with
        that identity under that root."""
        return {(row[0], row[1]) for row in self._db.execute("SELECT check_id, root FROM confirmations")}

    def confirmations(self) -> list[dict[str, str]]:
        keys = ("check_id", "root", "reason", "confirmed_at")
        rows = self._db.execute(f"SELECT {', '.join(keys)} FROM confirmations ORDER BY check_id")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def confirm(self, checks: Sequence[tuple[Check, str]], root: str) -> str:
        """Record the operator's confirmation of each unknown check, with its reason; return
        the time recorded."""
        at = _now()
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO confirmations VALUES (?, ?, ?, ?)",
                [(check["id"], root, reason, at) for check, reason in checks],
            )
        return at


def _group_members(group: int) -> list[int]:
    """Live processes in process group `group`, from /proc; a zombie runs nothing."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # After the command name's last ")": state, ppid, pgrp, ...
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] != "Z" and int(fields[2]) == group:
            members.append(int(entry.name))
    return members


def _leader_runs(pid: int, start: object) -> bool:
    """Whether `pid` is still the recorded process, started at `start`, and not a zombie."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return False
    return fields[0] != "Z" and fields[19] == start


def _repair(root: str) -> str:
    return (
        "Once you have checked that nothing of it runs, confirm with "
        f"`pluginbench repair --confirm-stopped {root}`"
    )


def verify_invocation(check: Check) -> dict[str, Any]:
    """Prove that nothing in the invocation's recorded process group still runs.

    A live member blocks, even after the leader exits. A missing record is unknown unless the
    check says what covers it (`_coverage`). A record without its boot, or one from another PID
    namespace, is unknown. A record from an earlier boot is gone
    with that boot. Children that left the group with `setsid` are not seen; systemd scopes
    will cover them.
    """
    identity = check["identity"] or {}
    pid, group = identity.get("pid"), identity.get("process_group")
    name = f"{check['stage']} invocation {check['id']}"
    if type(pid) is not int or type(group) is not int or "boot_id" not in identity:
        if covered := check.get("covered"):
            return {"id": check["id"], "checked": covered, "at": _now()}
        raise CleanupUnknown(f"{name} has no verifiable process record. {_repair(check['root'])}")
    proof = {"id": check["id"], "checked": "process group", "at": _now()}
    if identity["boot_id"] != run_lock.boot_id():
        return {**proof, "checked": "earlier boot"}
    namespace = identity.get("pid_namespace")
    if not namespace or namespace != run_lock.pid_namespace():
        raise CleanupUnknown(
            f"{name} was recorded in another PID namespace. {_repair(check['root'])}"
        )
    start = identity.get("start_time")
    if start is not None and run_lock.started_at(group) not in (None, start):
        # Linux frees a PID only once no process uses it as a process-group ID, so a newer
        # process holding the leader's PID proves the recorded group has ended.
        proof = {**proof, "checked": "process group ended"}
    else:
        members = set(_group_members(group))
        leader = _leader_runs(pid, start)
        if leader:
            members.add(pid)
        if members:
            listed = ", ".join(map(str, sorted(members)))
            raise CleanupBlocked(
                f"process group {group} of {name} still has processes {listed}; stop them with "
                f"`kill -TERM -- -{group}`"
                if leader
                else f"process group {group} of {name} still has processes {listed}, and its "
                "leader has exited; check that they belong to this call before stopping them"
            )
    return proof


def _deadline() -> float:
    return time.monotonic() + RECOVERY_SECONDS


def verify_containers(
    check: Check, progress: Callable[[str], None] | None = None, *, deadline: float | None = None
) -> dict[str, Any]:
    """Stop the trial containers the run left running, and prove none still runs.

    Each inventory first proves the daemon is the one the runtime recorded. After the stop, two
    inventories at least INVENTORY_INTERVAL apart must both be empty, which catches a container
    created late, such as a sidecar. Every Docker command is capped by what is left before
    `deadline`, a `time.monotonic()` value; once it passes, the containers are unknown.
    """
    deadline = _deadline() if deadline is None else deadline
    runtime, evidence = check["runtime"], Path(check["evidence"])
    where = f"the trial containers of {evidence}"
    if runtime is None:
        missing = check.get("missing", "no Docker runtime is known")
        raise CleanupUnknown(f"{missing} for {where}. {_repair(check['root'])}")
    if not (evidence / "frozen" / "suite").is_dir():
        # The inventory finds trial containers by their frozen suite, so without it it would
        # find none, whatever runs.
        raise CleanupUnknown(
            f"the frozen suite that names {where} is missing. {_repair(check['root'])}"
        )

    def inventory() -> list[dict[str, str]]:
        if time.monotonic() >= deadline:
            raise CleanupUnknown(
                f"recovery used up its {RECOVERY_SECONDS:.0f} seconds before {where} were "
                f"checked. {_repair(check['root'])}"
            )
        try:
            harbor_backend._assert_daemon(runtime, timeout=trial_containers.timeout(deadline))
            return trial_containers.running(evidence, expected_runtime=runtime, deadline=deadline)
        except (
            OSError, ValueError, subprocess.SubprocessError, isolation.RuntimeDriftError
        ) as exc:
            raise CleanupUnknown(
                f"Docker could not be checked for {where}: {exc}. {_repair(check['root'])}"
            ) from exc

    if inventory():
        try:
            trial_containers.stop_left_running(
                evidence, expected_runtime=runtime, progress=progress, deadline=deadline
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CleanupBlocked(f"{where} could not be stopped: {exc}") from exc
    for _ in range(2):
        time.sleep(INVENTORY_INTERVAL)
        if left := inventory():
            names = ", ".join(container["name"] for container in left)
            raise CleanupBlocked(f"{where} still run after the stop: {names}")
    return {"id": check["id"], "checked": "trial containers", "at": _now()}


def _verify(
    checks: Sequence[Check],
    confirmed: Collection[tuple[str, str]],
    progress: Callable[[str], None] | None,
    deadline: float,
) -> tuple[list[str], list[tuple[Check, str]]]:
    """Run every check; return the blocking failures and the unknown checks with their reasons.

    Processes come first. Containers are checked only once every process is proven gone or
    confirmed, since until then a create may still be in flight; then each runtime is checked
    on its own, so one that cannot be checked never hides another.
    """
    from pluginbench import invocation_runtime

    blocked: list[str] = []
    unknown: list[tuple[Check, str]] = []
    producers = [check for check in checks if check["kind"] not in CONTAINER_KINDS]
    containers = [check for check in checks if check["kind"] in CONTAINER_KINDS]
    for check in producers:
        if (check["id"], check["root"]) in confirmed:
            continue
        try:
            if check["kind"] == "invocation":
                verify_invocation(check)
            elif check["kind"] == "scope":
                invocation_runtime.verify_scope(check["operation"], deadline=deadline)
            else:
                raise CleanupUnknown(f"{check['reason']}. {_repair(check['root'])}")
        except CleanupUnknown as exc:
            unknown.append((check, str(exc)))
        except CleanupBlocked as exc:
            blocked.append(str(exc))
    producers_gone = not blocked and not unknown
    for check in containers:
        if (check["id"], check["root"]) in confirmed:
            continue
        try:
            if not producers_gone:
                raise CleanupUnknown(
                    f"processes of {check.get('evidence', check['root'])} are not proven gone, "
                    "so a container may "
                    f"still be created. {_repair(check['root'])}"
                )
            if check["kind"] == "labelled":
                invocation_runtime.verify_labelled(check["operation"], deadline=deadline)
            else:
                verify_containers(check, progress, deadline=deadline)
        except CleanupUnknown as exc:
            unknown.append((check, str(exc)))
        except CleanupBlocked as exc:
            blocked.append(str(exc))
    return blocked, unknown


def _refusal(failures: Sequence[str]) -> CleanupBlocked:
    return CleanupBlocked(
        "cleanup after a crash is not verified, so nothing starts on this host: "
        f"{'; '.join(failures)}. Then run the command again"
    )


def _ledger(root: Path) -> Path | None:
    candidates = (root / LEDGER_NAME, root / "evidence" / LEDGER_NAME)
    return next((path for path in candidates if path.is_file()), None)


def _open(ledger: Ledger, stages: Collection[str] | None = None) -> list[dict[str, Any]]:
    return [
        row
        for row in ledger.invocations()
        if row["state"] in OPEN_STATES and (stages is None or row["stage"] in stages)
    ]


# Ended states that never started a process, or whose cleanup a recovery already verified.
_NOTHING_LEFT = ("aborted_prespawn", "unresolved")


def _since(acquired_at: object) -> datetime | None:
    """When a dead owner took the lock; None when unreadable, so every call counts as its."""
    try:
        return datetime.fromisoformat(str(acquired_at))
    except ValueError:
        return None


def _touched(row: dict[str, Any], since: datetime | None) -> bool:
    return since is None or datetime.fromisoformat(row["updated_at"]) >= since


def _owned(ledger: Ledger, since: datetime | None) -> list[dict[str, Any]]:
    """Every invocation a dead owner may have left running: each open one, and each it ended,
    since a call can end while its process-group cleanup failed. An ended call with no process
    record is unknown: its record may have failed while its child ran."""
    return [
        row
        for row in ledger.invocations()
        if row["state"] in OPEN_STATES
        or (row["state"] not in _NOTHING_LEFT and _touched(row, since))
    ]


def _invocations(rows: Sequence[dict[str, Any]], root: str) -> list[Check]:
    return [
        {
            "id": row["id"],
            "root": root,
            "kind": "invocation",
            "stage": row["stage"],
            "identity": row["identity"],
        }
        for row in rows
    ]


def contained(obligation: dict[str, Any]) -> bool:
    """Whether a dead owner ran everything it started as recorded operations: its record has
    schema 3 and was read whole."""
    return obligation.get("schema") == run_lock.CONTAINED_SCHEMA and "problem" not in obligation


def _coverage(
    invocation: str, state: Path, operations: Collection[str], *, contained: bool
) -> str | None:
    """What proves that a call with no process record left nothing running, or None.

    Every call of a contained owner runs as its operation, whose runtime directory exists
    before anything of it spawns. A recorded operation is checked first in the same pass; a
    `cleanup.json` is a proof already written. Without the directory, only a call `contained`
    says a contained owner made is known never to have started.
    """
    directory = state / "invocations" / invocation
    if (directory / "cleanup.json").is_file():
        return "its operation's cleanup was proven"
    if not contained:
        return None
    if invocation in operations:
        return "its operation's scope and labelled containers are checked first"
    return None if directory.exists() else "its operation never started"


def _obligation_checks(obligation: dict[str, Any], state: Path) -> list[Check]:
    """What a dead owner may have left running, as checks.

    For a contained owner they cover everything (see `contained`). Any other owner is one more
    unknown check that only `repair --confirm-stopped` clears; its other checks name processes
    and containers known to still run, which repair never confirms. Each cleanup the owner
    recorded as failed is an unknown check either way.
    """
    root = str(obligation["root"])
    name = f"{obligation.get('pid')}-{obligation.get('acquired_at')}"
    unreleased: Check = {
        "id": f"{name}:unreleased",
        "root": root,
        "kind": "unrecorded",
        "reason": (
            f"the host's owner record in {root} is unreadable or invalid "
            f"({obligation['problem']}), so nothing it says can be trusted and what earlier "
            "commands left is unknown. Check that no PluginBench command, process or container "
            "still runs; repair takes this directory as the root"
            if "problem" in obligation
            else f"`{obligation.get('kind')}` for {root} ended without a clean release, and the "
            "earlier PluginBench build that ran it cannot prove that everything it started has "
            "stopped"
            if Path(root).is_dir()
            else f"{root}, whose owner ended without a clean release, was moved or deleted"
        ),
    }
    # The owner record was validated when it was read (run_lock._record_problem). A contained
    # owner's failed cleanup of an operation it still records is proven by that operation's
    # checks below; any other failed cleanup is unknown until repair.
    recorded = {item["id"] for item in obligation.get("operations") or []}
    failed: list[Check] = [
        {
            "id": f"{name}:leftover:{index}",
            "root": root,
            "kind": "unrecorded",
            "reason": item["reason"],
        }
        for index, item in enumerate(obligation.get("leftovers") or [])
        if not (contained(obligation) and item.get("operation") in recorded)
    ]
    # Each operation it recorded: its scope first, then the containers carrying its label.
    operations: list[Check] = [
        {"id": f"{name}:{kind}:{item['id']}", "root": root, "kind": kind, "operation": item}
        for kind in ("scope", "labelled")
        for item in obligation.get("operations") or []
    ]
    owned = [*failed, *operations, *_work_checks(obligation, state)]
    if not contained(obligation):
        return [unreleased, *owned]
    return [{**check, "contained": True} for check in owned]


def _work_checks(obligation: dict[str, Any], state: Path) -> list[Check]:
    root = str(obligation["root"])
    name = f"{obligation.get('pid')}-{obligation.get('acquired_at')}"
    path = _ledger(Path(root))
    if obligation.get("kind") in UNRECORDED_KINDS or path is None:
        return []  # No ledger records its processes; the owner's own check covers them.
    since = _since(obligation.get("acquired_at"))
    ledger = Ledger.recover(path)  # Its writer is dead: undo a write it left half done.
    try:
        rows = _owned(ledger, since)
    finally:
        ledger.close()
    checks = _invocations(rows, root)
    recorded = {item["id"] for item in obligation.get("operations") or []}
    for check, row in zip(checks, rows, strict=True):
        # Only a call this owner touched is its own; an older one needs its own proof.
        own = contained(obligation) and since is not None and _touched(row, since)
        check["covered"] = _coverage(check["id"], state, recorded, contained=own)
    if contained(obligation):
        return checks  # Each container it started carries a recorded operation's label.
    evidence = path.parent
    # A recorded runtime stays an obligation whatever its directory now holds.
    runtimes: list[dict[str, Any] | None] = list(obligation.get("runtimes") or [])
    missing = "no Docker runtime is known"
    if not runtimes:
        if obligation.get("kind") != "run" or not (evidence / "frozen" / "suite").is_dir():
            return checks  # It recorded no runtime and froze no trial, so started no container.
        # The owner died before it recorded the runtime its trials use: the run's own.
        from pluginbench import study

        try:
            runtimes = [study.prepared_runtime(study._frozen_study(evidence))]
        except (OSError, ValueError) as exc:
            runtimes, missing = [None], f"the Docker runtime could not be found ({exc})"
    return checks + [
        {
            "id": f"{name}:containers:{index}",
            "root": root,
            "kind": "containers",
            "evidence": str(evidence),
            "runtime": runtime,
            "missing": missing,
        }
        for index, runtime in enumerate(runtimes)
    ]


@contextmanager
def execute(owner: ExecutionOwner, *, state: Path | None = None) -> Iterator[ExecutionLock]:
    """Take the host execution lock, verify every obligation it carries, then run the block.

    Every execution entry point enters here, before any run lock. While an obligation fails,
    nothing runs, whatever this command targets. A contained owner's obligation clears once its
    checks pass; any other only once a repair confirmed it.

    An owner with a `launch_id` was started in the background (`coordinator.launch`). It raises
    LaunchFenced, before anything else, unless its launch is still current, and claims it only
    after recovery. Every owner then ends each other worker launch (`_settle_launches`).
    """
    with run_lock.hold_execution(owner, state=state) as lock:
        with ControlStore.open(lock.state) as store:
            # A stale launch exits before it touches anyone else's launch or obligations.
            if owner.launch_id is not None:
                _require_current(store, owner.launch_id)
            if pending := lock.record["pending"]:
                checks = [
                    check for item in pending for check in _obligation_checks(item, lock.state)
                ]
                blocked, unknown = _verify(checks, store.confirmed(), None, _deadline())
                if blocked or unknown:
                    raise _refusal([*blocked, *(reason for _, reason in unknown)])
                _prove(lock.state, checks, store.confirmed())
                lock.settle([])
            # Holding the lock proves any holder an observer could not verify is gone.
            store.set_reconciling(None)
            _settle_launches(store, owner.launch_id)
            if owner.launch_id is not None:
                launch = store.launch(owner.launch_id)
                process = launched_process(launch.scope if launch else "")
                if not store.claim(owner.launch_id, process):
                    _require_current(store, owner.launch_id)
                    raise LaunchFenced(f"launch {owner.launch_id} could not be claimed")
        lock.publish("active")
        with _launched(lock, owner.launch_id):
            yield lock
        # Returning is a clean exit. Left by an exception after it recorded work (see
        # ExecutionLock.dirty), the owner stays unreleased, so the next one verifies this root.
        lock.dirty = False


def launched_process(scope: str) -> dict[str, object]:
    """This process's identity for its launch, with its cgroup only when that is `scope`: the
    cgroup recovery may check for survivors is the launch's own, never another."""
    from pluginbench import invocation_runtime

    pid = os.getpid()
    cgroup = invocation_runtime.cgroup_of(pid)
    in_scope = bool(scope) and cgroup is not None and cgroup.rsplit("/", 1)[-1] == scope
    return {**invocation_runtime.process_identity(pid), "cgroup": cgroup if in_scope else None}


def _require_current(store: ControlStore, launch_id: str) -> None:
    launch = store.launch(launch_id)
    if launch is None:
        raise LaunchFenced(f"launch {launch_id} is not recorded, so this process starts nothing")
    if launch.version != run_lock.execution_version():
        raise LaunchFenced(
            f"launch {launch_id} is for PluginBench {launch.version}, not "
            f"{run_lock.execution_version()}, so this process starts nothing"
        )
    if launch.status != "launching" or launch.desired != "run":
        raise LaunchFenced(
            f"launch {launch_id} is {launch.status} with desired {launch.desired}, so this "
            "process starts nothing"
        )


def _settle_launches(store: ControlStore, own: str | None) -> None:
    """End every other worker launch: holding the lock, this owner is the only executor. One
    never claimed is fenced so it never can be; then its scope must be proven gone, or this
    owner starts nothing. The coordinator is no executor and keeps running."""
    from pluginbench import invocation_runtime

    for listed in store.launches("launching", "fenced", "active"):
        if listed.kind == "coordinator" or listed.launch_id == own:
            continue
        store.fence(listed.launch_id)
        # Read again after the fence: its launcher may have recorded a bootstrap since the list.
        launch = store.launch(listed.launch_id)
        assert launch is not None
        invocation_runtime.verify_scope(launch.operation(), deadline=_deadline())
        store.finish(launch.launch_id)


# How often a launched owner reads whether a stop was requested for its launch.
STOP_POLL = 0.25


@contextmanager
def _launched(lock: ExecutionLock, launch_id: str | None) -> Iterator[None]:
    """For a launched owner: once a stop is requested for its launch, admit no new job
    (`run_lock.check_stop`, which `parallel.run_jobs` calls before each), and wake it with
    SIGTERM, as `stop` does. The signal is only a wake-up; a lost one cannot erase the
    request. In the main thread, SIGTERM raises KeyboardInterrupt for the whole block, as under
    `run_lock.hold`, so the wake-up never kills the owner with its launch still active.
    Elsewhere no signal is sent. The launch finishes with the block."""
    if launch_id is None:
        yield
        return
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, run_lock.interrupt) if main else None

    def requested() -> bool | None:
        """Whether a stop was requested; None while the control database cannot be read."""
        try:
            with ControlStore.open(lock.state) as store:
                launch = store.launch(launch_id)
        except sqlite3.Error:
            return None
        return launch is None or launch.desired == "stop"

    done = threading.Event()

    def wake() -> None:
        while not done.wait(STOP_POLL):
            # Unreadable (None) is checked again at the next poll, and before every job.
            if requested():
                os.kill(os.getpid(), signal.SIGTERM)
                return

    lock.stop_requested = requested
    watcher = threading.Thread(target=wake, name="pluginbench-stop", daemon=True)
    if main:
        watcher.start()
    try:
        yield
    finally:
        done.set()
        if main:
            watcher.join()
        lock.stop_requested = None
        try:
            with ControlStore.open(lock.state) as store:
                store.finish(launch_id)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)


def _prove(state: Path, checks: Sequence[Check], confirmed: Collection[tuple[str, str]]) -> None:
    """Save a `cleanup.json` for each operation and each covered call of a contained owner that
    these passed checks proved clean, so a resume's own checks of those calls read it
    (`_coverage`). What an operator confirmed was never proven, so it gets none."""

    def proven(check: Check) -> bool:
        ids = [check["id"]]
        if check["kind"] == "scope":
            ids.append(check["id"].replace(":scope:", ":labelled:"))
        return bool(check.get("contained")) and all(
            (item, check["root"]) not in confirmed for item in ids
        )

    proofs = {
        str(check["operation"]["id"] if check["kind"] == "scope" else check["id"]): str(
            check.get("covered") or "its scope and labelled containers were proven gone"
        )
        for check in checks
        if proven(check)
        and (check["kind"] == "scope" or (check["kind"] == "invocation" and check.get("covered")))
    }
    for invocation, proof in proofs.items():
        directory = state / "invocations" / invocation
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (directory / "cleanup.json").exists():
            (directory / "cleanup.json").write_text(
                json.dumps({"id": invocation, "recovered": proof, "at": _now()}, indent=2) + "\n"
            )


def _held() -> ExecutionLock:
    lock = run_lock.held_execution()
    if lock is None:
        raise RuntimeError("verifying cleanup after a crash needs the host execution lock")
    lock.assert_owned()
    return lock


def verify_crashed(
    ledger: Ledger,
    path: Path,
    stages: Collection[str],
    *,
    runtime: Callable[[], dict[str, object]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> frozenset[str]:
    """Verify cleanup of every open `stages` invocation in `ledger`; return their IDs.

    Call before accepting a crashed invocation's saved output or marking it unresolved, and pass
    the result to `Ledger.resolve_crashed`. With `runtime`, which finds the Docker runtime the
    run's trial containers ran under, those containers are verified too. Needs the host
    execution lock. Until the execution block returns, this owner's release stays unrecorded,
    so if a check fails the next owner verifies this run again.
    """
    rows = _open(ledger, stages)
    if not rows:
        return frozenset()
    lock = _held()
    lock.dirty = True  # Until the check passes and the block returns, this root stays fenced.
    root = str(lock.record["root"])
    checks = _invocations(rows, root)
    for check in checks:
        check["covered"] = _coverage(check["id"], lock.state, (), contained=False)
    if runtime is not None:
        try:
            known: dict[str, object] | None = runtime()
            missing = "no Docker runtime is known"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            known, missing = None, f"the Docker runtime could not be found ({exc})"
        checks.append(
            {
                "id": f"resume-{uuid4().hex}:containers",
                "root": root,
                "kind": "containers",
                "evidence": str(path.parent),
                "runtime": known,
                "missing": missing,
            }
        )
    with ControlStore.open(lock.state) as store:
        confirmed = store.confirmed()
    blocked, unknown = _verify(checks, confirmed, progress, _deadline())
    if blocked or unknown:
        raise _refusal([*blocked, *(reason for _, reason in unknown)])
    return frozenset(check["id"] for check in checks if check["kind"] == "invocation")


def confirm_stopped(
    lock: ExecutionLock, root: Path
) -> tuple[list[tuple[Check, str]], str, int]:
    """Verify the obligations that name exactly `root` again, then record the operator's word
    for each check that is still unknown. Refuses while any check finds a survivor.

    Returns the confirmed checks, the time recorded, and how many obligations of other roots
    remain.
    """
    target = str(root.resolve())
    pending: list[dict[str, Any]] = lock.record["pending"]
    matching = [item for item in pending if item.get("root") == target]
    if not matching:
        roots = sorted({str(item.get("root")) for item in pending}) or ["none"]
        raise ValueError(
            f"no open obligation names {target}; repair takes one of: {', '.join(roots)}"
        )
    checks = [check for item in matching for check in _obligation_checks(item, lock.state)]
    deadline = _deadline()  # Shared by both passes.
    with ControlStore.open(lock.state) as store:
        confirmed = store.confirmed()
        blocked, unknown = _verify(checks, confirmed, None, deadline)
        # Containers were unknown only because a process was; with the operator's word for the
        # processes, check the containers for real before confirming anything.
        processes = [
            (check, reason) for check, reason in unknown if check["kind"] not in CONTAINER_KINDS
        ]
        if not blocked and processes and len(processes) < len(unknown):
            vouched = {*confirmed, *((check["id"], check["root"]) for check, _ in processes)}
            blocked, containers = _verify(checks, vouched, None, deadline)
            unknown = [*processes, *containers]
        if blocked:
            raise CleanupBlocked(
                f"{'; '.join(blocked)}. repair confirms only what cannot be checked, so nothing "
                "was confirmed"
            )
        at = store.confirm(unknown, target)
    rest = [item for item in pending if item.get("root") != target]
    lock.settle(rest)
    return unknown, at, len(rest)


Observation = Literal["free", "verified", "unknown", "reconciling"]


class OwnerWatch:
    """Watch the host lock for a holder that cannot be verified, as a dispatcher does.

    An unknown holder blocks dispatch at once. After it has stayed unknown for 30 seconds of this
    watcher's monotonic clock, the watch publishes `reconciling` with `owner_unknown`. It never
    takes the lock or signals the holder.
    """

    def __init__(self, state: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._state = state
        self._clock = clock
        self._since: float | None = None

    def poll(self) -> Observation:
        snapshot = run_lock.execution_owner(self._state)
        if not snapshot.held or snapshot.verified:
            if self._since is not None:
                self._since = None
                with ControlStore.open(self._state) as store:
                    store.set_reconciling(None)
            return "verified" if snapshot.held else "free"
        now = self._clock()
        if self._since is None:
            self._since = now
        if now - self._since < UNKNOWN_OWNER_GRACE:
            return "unknown"
        with ControlStore.open(self._state) as store:
            store.set_reconciling("owner_unknown")
        return "reconciling"
