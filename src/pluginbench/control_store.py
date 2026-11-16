"""Host execution control: the recovery barrier, unclean invocations, and an unknown lock holder.

One small SQLite database beside the host execution lock. Each run's ledger stays the only
authority on retries and spend; this store records only what blocks the whole host: an
invocation whose cleanup failed verification, whichever run it belongs to. While such a barrier
stands, every execution entry point first verifies that cleanup again and refuses to run until it
passes.

A crashed invocation is marked `unresolved`, or its saved output accepted, only after cleanup of
everything it started is verified (`verify_crashed`). `verify_invocation` is that check. It now
checks the process the ledger recorded; per-invocation systemd scopes and container labels will
replace it with a scope and label check.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pluginbench import run_lock
from pluginbench.ledger import OPEN_STATES, Ledger
from pluginbench.run_lock import ExecutionLock, ExecutionOwner, process_start

CONTROL_NAME = "control.sqlite"
SCHEMA_VERSION = 1
# How long a held lock may name no verifiable owner before observers report `reconciling`.
UNKNOWN_OWNER_GRACE = 30.0
HOST_CLEANUP = "host_cleanup"

# Only cleanup state: references into run ledgers, never a second cap or spend counter.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL,
    -- Why no work may start until recovery verifies cleanup, such as host_cleanup.
    barrier TEXT,
    detail TEXT,
    -- Set while an observer sees the lock held by an owner it cannot verify.
    reconciling TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    invocation_id TEXT PRIMARY KEY,
    ledger TEXT NOT NULL,
    stage TEXT NOT NULL,
    scope TEXT NOT NULL,
    label TEXT NOT NULL,
    identity TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('intent', 'launching', 'active', 'cleaning', 'clean')),
    proof TEXT,
    updated_at TEXT NOT NULL
);
"""


class CleanupBlocked(ValueError):
    """Cleanup of a crashed invocation is not verified, so no work starts on this host."""


@dataclass(frozen=True)
class HostState:
    revision: int
    barrier: str | None
    detail: str | None
    reconciling: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ControlStore:
    """The host control database. Short `BEGIN IMMEDIATE` transactions; never held across a
    process launch or an external command."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, timeout=5)
        try:
            path.chmod(0o600)
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.executescript(f"PRAGMA user_version = {SCHEMA_VERSION};" + _SCHEMA)
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO host_state (id, revision, updated_at) VALUES (1, 0, ?)",
                    (_now(),),
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

    def host(self) -> HostState:
        row = self._db.execute(
            "SELECT revision, barrier, detail, reconciling FROM host_state"
        ).fetchone()
        return HostState(*row)

    def unclean(self) -> list[dict[str, Any]]:
        """Every registered invocation whose cleanup is not yet verified."""
        keys = ("invocation_id", "ledger", "stage", "scope", "label", "identity", "status")
        rows = self._db.execute(
            f"SELECT {', '.join(keys)} FROM operations WHERE status != 'clean'"
            " ORDER BY invocation_id"
        ).fetchall()
        return [
            {**dict(zip(keys, row, strict=True)), "identity": json.loads(row[5] or "null")}
            for row in rows
        ]

    def block(self, detail: str, operations: Sequence[Mapping[str, Any]]) -> None:
        """Persist the host barrier and the invocations whose cleanup failed, in one transaction."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            for operation in operations:
                self._db.execute(
                    "INSERT INTO operations (invocation_id, ledger, stage, scope, label, identity,"
                    " status, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'cleaning', ?)"
                    " ON CONFLICT (invocation_id) DO UPDATE SET status = 'cleaning',"
                    " proof = NULL, updated_at = excluded.updated_at",
                    (
                        operation["invocation_id"],
                        operation["ledger"],
                        operation["stage"],
                        operation["scope"],
                        operation["label"],
                        json.dumps(operation["identity"], sort_keys=True),
                        _now(),
                    ),
                )
            self._db.execute(
                "UPDATE host_state SET revision = revision + 1, barrier = ?, detail = ?,"
                " updated_at = ?",
                (HOST_CLEANUP, detail, _now()),
            )

    def record_clean(self, invocation_id: str, proof: Mapping[str, Any]) -> None:
        """Mark one invocation clean; only with the proof that verified it."""
        with self._db:
            changed = self._db.execute(
                "UPDATE operations SET status = 'clean', proof = ?, updated_at = ?"
                " WHERE invocation_id = ? AND status != 'clean'",
                (json.dumps(proof, sort_keys=True), _now(), invocation_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"control store cannot mark invocation {invocation_id} clean")

    def clear_barrier(self, revision: int) -> None:
        """Lift the barrier read at `revision`, once nothing is left unclean."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self.unclean():
                raise ValueError("cleanup is still unverified; the host barrier stays")
            changed = self._db.execute(
                "UPDATE host_state SET revision = revision + 1, barrier = NULL, detail = NULL,"
                " reconciling = NULL, updated_at = ? WHERE revision = ?",
                (_now(), revision),
            ).rowcount
        if changed != 1:
            raise ValueError("the host barrier changed while it was being cleared")

    def set_reconciling(self, reason: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE host_state SET revision = revision + 1, reconciling = ?, updated_at = ?"
                " WHERE reconciling IS NOT ?",
                (reason, _now(), reason),
            )


def verify_invocation(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Prove nothing `operation` started still runs, or raise CleanupBlocked.

    Checks the child the ledger recorded: the same PID with the same kernel start time means it
    still runs. An invocation killed before its child was recorded has nothing to check.
    """
    identity = operation["identity"] or {}
    pid, start = identity.get("pid"), identity.get("start_time")
    if type(pid) is int and start is not None:
        try:
            alive = process_start(pid) == start
        except ProcessLookupError:
            alive = False
        if alive:
            group = identity.get("process_group", pid)
            raise CleanupBlocked(
                f"process {pid} of {operation['stage']} invocation {operation['invocation_id']} "
                f"still runs; stop it with `kill -TERM -- -{group}`"
            )
    return {"invocation_id": operation["invocation_id"], "checked": "process", "at": _now()}


def _refusal(failures: Sequence[str]) -> CleanupBlocked:
    count = f"{len(failures)} crashed invocation{'s' if len(failures) != 1 else ''}"
    return CleanupBlocked(
        f"cleanup of {count} is not verified, so nothing starts on this host: "
        f"{'; '.join(failures)}. Then run the command again"
    )


def _verify(operations: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify each operation; return the proofs and the failures."""
    proofs: list[dict[str, Any]] = []
    failures: list[str] = []
    for operation in operations:
        try:
            proofs.append(verify_invocation(operation))
        except CleanupBlocked as exc:
            failures.append(str(exc))
    return proofs, failures


def _recover(store: ControlStore, lock: ExecutionLock) -> None:
    """Verify again the cleanup a barrier left; lift it, or keep it and raise CleanupBlocked."""
    host, unclean = store.host(), store.unclean()
    if host.barrier is None and not unclean:
        return
    lock.publish("reconciling")
    proofs, failures = _verify(unclean)
    for proof in proofs:
        store.record_clean(proof["invocation_id"], proof)
    if failures:
        store.block("; ".join(failures), [])
        lock.publish("blocked")
        raise _refusal(failures)
    store.clear_barrier(host.revision)


@contextmanager
def execute(owner: ExecutionOwner, *, state: Path | None = None) -> Iterator[ExecutionLock]:
    """Take the host execution lock, finish any pending recovery, then run the block.

    Every execution entry point enters here, before any run lock. A barrier left by failed
    cleanup is verified again first, whichever run left it; while it fails, nothing runs.
    """
    with run_lock.hold_execution(owner, state=state) as lock:
        with ControlStore.open(lock.state) as store:
            _recover(store, lock)
            # Holding the lock proves any holder an observer could not verify is gone.
            store.set_reconciling(None)
        lock.publish("active")
        yield lock


def verify_crashed(ledger: Ledger, path: Path, stages: Collection[str]) -> frozenset[str]:
    """Verify cleanup of every open `stages` invocation in `ledger`; return their IDs.

    Call before accepting a crashed invocation's saved output or marking it unresolved, and pass
    the result to `Ledger.resolve_crashed`. Needs the host execution lock. A failure persists the
    host barrier with the failing invocations, leaves every row open, and raises CleanupBlocked.
    """
    operations = [
        {
            "invocation_id": row["id"],
            "ledger": str(path),
            "stage": row["stage"],
            "scope": row["scope"],
            "label": row["container_label"],
            "identity": row["identity"],
        }
        for row in ledger.invocations()
        if row["stage"] in stages and row["state"] in OPEN_STATES
    ]
    if not operations:
        return frozenset()
    lock = run_lock.held_execution()
    if lock is None:
        raise RuntimeError("verifying cleanup of crashed invocations needs the host execution lock")
    lock.assert_owned()
    proofs, failures = _verify(operations)
    if failures:
        verified = {proof["invocation_id"] for proof in proofs}
        with ControlStore.open(lock.state) as store:
            store.block(
                "; ".join(failures),
                [item for item in operations if item["invocation_id"] not in verified],
            )
        lock.publish("blocked")
        raise _refusal(failures)
    return frozenset(proof["invocation_id"] for proof in proofs)


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
