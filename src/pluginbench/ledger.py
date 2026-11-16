"""Single-writer SQLite ledger of coding attempts, their stages and every external invocation.

Evidence files stay the record of what happened; the ledger only says how far each attempt got
and how many invocations each slot has spent (contract C2). One coordinator thread writes it.
Every write is its own transaction, and a state is marked complete only after the receipt it
points to is on disk. The default rollback journal leaves no sidecar files once a transaction
ends.

An invocation is committed as `intent` before anything external starts, so a crash at any later
point still counts it: a slot's cap is a row count, and no restart can reset it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pluginbench.run_lock import boot_id, pid_namespace, process_start

# Version 2 adds `invocations` and `budget`; a writable open migrates version 1 in place.
SCHEMA_VERSION = 2
LEDGER_NAME = "ledger.sqlite"

# Contract C2. `intent` is committed before spawn and becomes `started` once the child's
# identity is recorded; the others end an invocation. `unresolved` is an open row a crash left.
INVOCATION_STATES = (
    "intent",
    "aborted_prespawn",
    "started",
    "finished",
    "failed_transient",
    "unresolved",
    "interrupted_operator",
    "halted_provider",
)
OPEN_STATES = ("intent", "started")
# The only states that give a slot its invocation back. Every other row, an open one included,
# counts toward the slot's cap.
REFUNDED_STATES = ("aborted_prespawn", "interrupted_operator", "halted_provider")
SLOT_CAP = 3
# Stages that call a model. Every row of these counts toward the paid-call ceiling, refunded
# ones included, so not even a loop of halts or pre-spawn aborts can pass it.
PAID_STAGES = ("coding", "review")

# pending -> running -> complete | interrupted. A resumed run moves a started but unfinished
# attempt to discarded-interrupted and gives its slot to a new pending attempt.
ATTEMPT_STATES = ("pending", "running", "complete", "interrupted", "discarded-interrupted")

_SCHEMA = f"""
CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    slot INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    repeat INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN {ATTEMPT_STATES}),
    record TEXT,
    replaced_by TEXT REFERENCES attempts (id),
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_live_attempt_per_slot ON attempts (slot)
    WHERE state != 'discarded-interrupted';
CREATE TABLE stages (
    attempt_id TEXT NOT NULL REFERENCES attempts (id),
    stage TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started', 'complete')),
    receipt TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, stage)
);
"""

# One row per external invocation, keyed (slot, stage, n). Its systemd scope and container
# label are fixed here, before spawn, so recovery can find what the invocation started.
_INVOCATION_SCHEMA = f"""
CREATE TABLE invocations (
    id TEXT PRIMARY KEY,
    slot INTEGER NOT NULL,
    stage TEXT NOT NULL,
    n INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN {INVOCATION_STATES}),
    scope TEXT NOT NULL,
    container_label TEXT NOT NULL,
    identity TEXT,
    error TEXT,
    -- A fingerprint of what a review call was given, so the short-circuit compares only calls
    -- with the same inputs. NULL for coding and grading, whose slot fixes theirs.
    inputs TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (slot, stage, n)
);
CREATE TABLE budget (paid_call_ceiling INTEGER NOT NULL);
"""


class InvocationRefused(ValueError):
    """No new invocation may start; nothing was recorded for it."""


class SlotExhausted(InvocationRefused):
    """A slot used its invocations for a stage, or failed it twice the same way."""


class CeilingReached(InvocationRefused):
    """The run reached its paid-call ceiling."""


# (slot, stage, attempt ID, state) of an invocation a run's evidence shows, for a ledger built
# or migrated after the calls it records.
HistoryRow = tuple[int, str, str, str]


def _insert(
    db: sqlite3.Connection,
    *,
    slot: int,
    stage: str,
    attempt_id: str,
    state: str,
    inputs: str | None = None,
) -> str:
    """Add the next invocation of `slot` and `stage`; return its ID."""
    n = db.execute(
        "SELECT COUNT(*) FROM invocations WHERE slot = ? AND stage = ?", (slot, stage)
    ).fetchone()[0]
    # Hex only: valid in a systemd unit name and in a Docker label value.
    invocation = uuid4().hex
    db.execute(
        "INSERT INTO invocations (id, slot, stage, n, attempt_id, state, scope,"
        " container_label, inputs, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invocation,
            slot,
            stage,
            n + 1,
            attempt_id,
            state,
            f"pb-inv-{invocation}.scope",
            f"pluginbench.invocation={invocation}",
            inputs,
            _now(),
        ),
    )
    return invocation


def default_ceiling(paid_slots: int) -> int:
    """The paid-call ceiling: every paid slot's cap. Pass coding plus review slots."""
    return SLOT_CAP * paid_slots


def child_identity(pid: int) -> dict[str, object]:
    """A spawned child's PID, kernel start time and the process group it leads, with the boot
    and PID namespace they are valid in, so recovery can find what is left of it.

    `processes.run_command` starts each child in a new session, so its group ID is its PID.
    A child that already exited has no start time left to read.
    """
    try:
        start: str | None = process_start(pid)
    except ProcessLookupError:
        start = None
    return {
        "pid": pid,
        "start_time": start,
        "process_group": pid,
        "boot_id": boot_id(),
        "pid_namespace": pid_namespace(),
    }


def outcome(error: BaseException, *, spawned: bool) -> tuple[str, str | None]:
    """The state and error signature that end an invocation which raised `error`.

    Nothing external started unless the spawn reported its child, so that is the one refund
    besides an operator interrupt. The signature is what the short-circuit compares: the
    error's stable `reason` when it has one, which holds no attempt ID or temporary path.
    """
    if not spawned:
        return "aborted_prespawn", None
    if isinstance(error, KeyboardInterrupt):
        return "interrupted_operator", None
    reason = getattr(error, "reason", None)
    return "failed_transient", f"{type(error).__name__}: {reason or error}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, connection: sqlite3.Connection, version: int = SCHEMA_VERSION) -> None:
        self._db = connection
        self._version = version

    @classmethod
    def create(
        cls,
        path: Path,
        entries: Iterable[dict[str, Any]],
        states: dict[str, str],
        *,
        paid_call_ceiling: int | None = None,
        history: Iterable[HistoryRow] = (),
    ) -> Ledger:
        """A new ledger with one row per plan entry, in plan (slot) order.

        It is built in a temporary file and renamed into place, so a killed build never leaves
        a ledger that looks valid but lacks attempts. The ceiling defaults to every slot's
        coding cap. `history` holds the invocations a run made before it had this ledger.
        """
        entries = list(entries)
        if path.exists():
            raise ValueError(f"ledger already exists: {path}")
        temporary = path.with_name(f".{path.name}.tmp")
        # Left by a build that was killed; a stale journal must not roll into the new file.
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-journal").unlink(missing_ok=True)
        connection = sqlite3.connect(temporary)
        with connection:
            connection.executescript(_SCHEMA + _INVOCATION_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute(
                "INSERT INTO budget VALUES (?)",
                (paid_call_ceiling or default_ceiling(len(entries)),),
            )
            connection.executemany(
                "INSERT INTO attempts (id, slot, task_id, arm, repeat, state, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        entry["id"],
                        slot,
                        entry["task_id"],
                        entry["arm"],
                        entry["attempt"],
                        states[entry["id"]],
                        _now(),
                    )
                    for slot, entry in enumerate(entries)
                ],
            )
            for slot, stage, attempt_id, state in history:
                _insert(connection, slot=slot, stage=stage, attempt_id=attempt_id, state=state)
        connection.close()
        temporary.replace(path)
        return cls.open(path)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        paid_call_ceiling: int | None = None,
        history: Callable[[], Iterable[HistoryRow]] | None = None,
    ) -> Ledger:
        """Open for writing.

        A version 1 ledger predates invocation rows. It is migrated only with `history`, the
        invocations its run's evidence shows, so a migration never grants a slot calls it has
        spent; without it, opening refuses. The ceiling is `paid_call_ceiling`, or else every
        slot's coding cap.
        """
        if not path.is_file():
            raise ValueError(f"ledger is missing: {path}")
        connection = sqlite3.connect(path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            try:
                if history is None:
                    raise ValueError(
                        "this ledger predates invocation rows and no invocation history was "
                        "given to migrate it"
                    )
                rows = list(history())
                slots = connection.execute(
                    "SELECT COUNT(DISTINCT slot) FROM attempts"
                ).fetchone()[0]
                with connection:
                    connection.execute("BEGIN")
                    for statement in _INVOCATION_SCHEMA.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        "INSERT INTO budget VALUES (?)",
                        (paid_call_ceiling or default_ceiling(slots),),
                    )
                    for slot, stage, attempt_id, state in rows:
                        _insert(
                            connection, slot=slot, stage=stage, attempt_id=attempt_id, state=state
                        )
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except BaseException:
                connection.close()
                raise
            version = SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            connection.close()
            raise ValueError(f"unsupported ledger schema version {version}")
        return cls(connection)

    @classmethod
    def read(cls, path: Path) -> Ledger:
        """Open read-only, for observers such as `status`; any write raises sqlite3.Error.

        PluginBench ledgers use the rollback journal. A WAL database is refused, because even a
        read-only connection to one creates -wal and -shm files.
        """
        with path.open("rb") as stream:
            header = stream.read(20)
        # Bytes 18 and 19 are the write and read format versions: 2 means WAL.
        if len(header) == 20 and header[18:20] != b"\x01\x01":
            raise ValueError(f"ledger is not in rollback-journal mode: {path}")
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error:
            connection.close()
            raise
        # An observer reads a version 1 ledger as it is; only a writer migrates it.
        if version not in (1, SCHEMA_VERSION):
            connection.close()
            raise ValueError(f"unsupported ledger schema version {version}")
        return cls(connection, version)

    def close(self) -> None:
        self._db.close()

    def set_state(self, attempt_id: str, state: str) -> None:
        with self._db:
            changed = self._db.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE id = ?"
                " AND state NOT IN ('complete', 'discarded-interrupted')",
                (state, _now(), attempt_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"ledger cannot move attempt {attempt_id} to {state}")

    def stage_started(self, attempt_id: str, stage: str, receipt: dict[str, str]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO stages (attempt_id, stage, state, receipt, updated_at)"
                " VALUES (?, ?, 'started', ?, ?)",
                (attempt_id, stage, json.dumps(receipt, sort_keys=True), _now()),
            )

    def complete(self, attempt_id: str, record: dict[str, str], stages: Iterable[str]) -> None:
        """Mark complete; `record` references the attempt's record receipt, already on disk.

        Its open invocations (its coding call, or a cached attempt's regrade) finish in the
        same transaction: their output is accepted.
        """
        encoded = json.dumps(record, sort_keys=True)
        with self._db:
            changed = self._db.execute(
                "UPDATE attempts SET state = 'complete', record = ?, updated_at = ?"
                " WHERE id = ? AND state IN ('pending', 'running', 'interrupted')",
                (encoded, _now(), attempt_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"ledger cannot complete attempt {attempt_id}")
            self._db.execute(
                "UPDATE invocations SET state = 'finished', updated_at = ?"
                f" WHERE attempt_id = ? AND state IN {OPEN_STATES}",
                (_now(), attempt_id),
            )
            for stage in stages:
                self._db.execute(
                    "INSERT INTO stages (attempt_id, stage, state, receipt, updated_at)"
                    " VALUES (?, ?, 'complete', ?, ?) ON CONFLICT (attempt_id, stage)"
                    " DO UPDATE SET state = 'complete', receipt = excluded.receipt,"
                    " updated_at = excluded.updated_at",
                    (attempt_id, stage, encoded, _now()),
                )

    def replace(self, attempt_id: str, replacement: dict[str, Any]) -> None:
        """Discard an interrupted attempt and give its slot to a new pending attempt."""
        with self._db:
            row = self._db.execute(
                "SELECT slot FROM attempts WHERE id = ? AND state IN ('running', 'interrupted')",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"ledger cannot discard attempt {attempt_id}")
            self._db.execute(
                "UPDATE attempts SET state = 'discarded-interrupted', replaced_by = ?,"
                " updated_at = ? WHERE id = ?",
                (replacement["id"], _now(), attempt_id),
            )
            self._db.execute(
                "INSERT INTO attempts (id, slot, task_id, arm, repeat, state, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    replacement["id"],
                    row[0],
                    replacement["task_id"],
                    replacement["arm"],
                    replacement["attempt"],
                    _now(),
                ),
            )

    def attempts(self) -> list[dict[str, Any]]:
        """Every attempt, discarded ones included, in slot then insertion order."""
        rows = self._db.execute(
            "SELECT id, slot, task_id, arm, repeat, state, record, replaced_by"
            " FROM attempts ORDER BY slot, rowid"
        ).fetchall()
        return [
            {
                "id": row[0],
                "slot": row[1],
                "task_id": row[2],
                "arm": row[3],
                "attempt": row[4],
                "state": row[5],
                "record": json.loads(row[6]) if row[6] else None,
                "replaced_by": row[7],
            }
            for row in rows
        ]

    def refusal(self, slot: int, stage: str, inputs: str | None = None) -> str | None:
        """Why `slot` may start no further `stage` invocation, or None while it may.

        Deterministic short-circuit: two transient failures with one signature on the same
        frozen `inputs` end the slot for those inputs, with no third identical try. The cap
        counts every input.
        """
        used = self._db.execute(
            "SELECT COUNT(*) FROM invocations WHERE slot = ? AND stage = ?"
            f" AND state NOT IN {REFUNDED_STATES}",
            (slot, stage),
        ).fetchone()[0]
        if used >= SLOT_CAP:
            return f"slot {slot} used its {SLOT_CAP} {stage} invocations"
        repeated = self._db.execute(
            "SELECT error FROM invocations WHERE slot = ? AND stage = ?"
            " AND inputs IS ? AND state = 'failed_transient' GROUP BY error HAVING COUNT(*) >= 2",
            (slot, stage, inputs),
        ).fetchone()
        if repeated is not None:
            return f"slot {slot} failed its {stage} twice with the same error: {repeated[0]}"
        return None

    def intent(
        self, *, slot: int, stage: str, attempt_id: str, inputs: str | None = None
    ) -> str:
        """Commit a new invocation before anything external starts; return its ID.

        The cap, short-circuit and ceiling checks share the insert's transaction.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if (reason := self.refusal(slot, stage, inputs)) is not None:
                raise SlotExhausted(reason)
            if stage in PAID_STAGES and self.paid_room() <= 0:
                raise CeilingReached(
                    f"the run reached its ceiling of {self._ceiling()} paid invocations, so no "
                    "further model call starts"
                )
            return _insert(
                self._db,
                slot=slot,
                stage=stage,
                attempt_id=attempt_id,
                state="intent",
                inputs=inputs,
            )

    def _ceiling(self) -> int:
        ceiling: int = self._db.execute("SELECT paid_call_ceiling FROM budget").fetchone()[0]
        return ceiling

    def paid_room(self) -> int:
        """Paid invocations the ceiling still allows. Every paid row counts, in every state:
        coding and review read this one figure."""
        spent: int = self._db.execute(
            f"SELECT COUNT(*) FROM invocations WHERE stage IN {PAID_STAGES}"
        ).fetchone()[0]
        return self._ceiling() - spent

    def invocation_started(self, invocation: str, pid: int) -> None:
        """Record the spawned child's identity: the invocation is `started`."""
        with self._db:
            changed = self._db.execute(
                "UPDATE invocations SET state = 'started', identity = ?, updated_at = ?"
                " WHERE id = ? AND state = 'intent'",
                (json.dumps(child_identity(pid), sort_keys=True), _now(), invocation),
            ).rowcount
        if changed != 1:
            raise ValueError(f"ledger cannot start invocation {invocation}")

    def end(self, invocation: str, state: str, error: str | None = None) -> None:
        """Move an open invocation to the state it ended in."""
        if state in OPEN_STATES or state not in INVOCATION_STATES:
            raise ValueError(f"{state} does not end an invocation")
        with self._db:
            changed = self._db.execute(
                "UPDATE invocations SET state = ?, error = ?, updated_at = ?"
                f" WHERE id = ? AND state IN {OPEN_STATES}",
                (state, error, _now(), invocation),
            ).rowcount
        if changed != 1:
            raise ValueError(f"ledger cannot end invocation {invocation} as {state}")

    def resolve_crashed(self, stage: str, *, cleaned: Collection[str]) -> int:
        """Mark the `stage` invocations a dead process left open `unresolved`; return how many.

        `cleaned` holds the invocations whose cleanup was verified under the host execution lock
        (`control_store.verify_crashed`); an open row outside it is refused and stays open. Their
        usage is unknown, and each still counts toward its slot's cap.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            unverified = sorted(
                row[0]
                for row in self._db.execute(
                    f"SELECT id FROM invocations WHERE stage = ? AND state IN {OPEN_STATES}",
                    (stage,),
                )
                if row[0] not in cleaned
            )
            if unverified:
                raise ValueError(
                    f"cleanup of {stage} invocations {', '.join(unverified)} is not verified, "
                    "so none is marked unresolved"
                )
            return self._db.execute(
                "UPDATE invocations SET state = 'unresolved', updated_at = ?"
                f" WHERE stage = ? AND state IN {OPEN_STATES}",
                (_now(), stage),
            ).rowcount

    def invocations(self) -> list[dict[str, Any]]:
        """Every invocation by slot, stage and order; none in a version 1 ledger."""
        if self._version == 1:
            return []
        keys = ("id", "slot", "stage", "n", "attempt_id", "state", "scope", "container_label")
        rows = self._db.execute(
            f"SELECT {', '.join(keys)}, identity, error FROM invocations ORDER BY slot, stage, n"
        ).fetchall()
        return [
            {
                **dict(zip(keys, row, strict=False)),
                "identity": json.loads(row[-2]) if row[-2] else None,
                "error": row[-1],
            }
            for row in rows
        ]
