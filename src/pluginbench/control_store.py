"""Host execution control: the recovery barrier, cleanups left to verify, an unknown lock holder.

One small SQLite database beside the host execution lock. Each run's ledger stays the only
authority on retries and spend; this store records only what blocks the whole host: a cleanup
not yet verified, whichever run it belongs to. While such a barrier stands, every execution entry
point first verifies that cleanup again and refuses to run until it passes.

Recovery fences before it checks. It records every cleanup it is about to verify, first in a
marker file and then in the barrier, so a recovery that dies or cannot finish its writes still
leaves the host blocked. A crashed invocation is marked `unresolved`, or its saved output
accepted, only after cleanup of everything it started is verified (`verify_crashed`).

Two cleanups are checked now: the process group the ledger recorded for each crashed invocation
(`verify_invocation`), and the containers a run's killed trials left running
(`stop_left_containers`). Per-invocation systemd scopes and container labels will replace the
process check. Whatever cannot be checked is unknown and blocks, until the operator confirms with
`pluginbench repair --confirm-stopped` that nothing still runs (`confirm_stopped`).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pluginbench import run_lock, trial_containers
from pluginbench.ledger import OPEN_STATES, Ledger
from pluginbench.run_lock import ExecutionLock, ExecutionOwner, process_start

CONTROL_NAME = "control.sqlite"
# Written before the barrier and removed only after it lifts: a fence that survives a store
# that cannot be written.
MARKER_NAME = "recovery-pending.json"
SCHEMA_VERSION = 1
# How long a held lock may name no verifiable owner before observers report `reconciling`.
UNKNOWN_OWNER_GRACE = 30.0
HOST_CLEANUP = "host_cleanup"
OPERATOR = "operator"

# Only cleanup state: references into runs, never a second cap or spend counter. An item is an
# invocation's processes (`invocation`) or the trial containers a run left running (`containers`).
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
    id TEXT PRIMARY KEY,
    run TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('invocation', 'containers')),
    detail TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('intent', 'launching', 'active', 'cleaning', 'clean')),
    proof TEXT,
    updated_at TEXT NOT NULL
);
"""

# An item to clean up: {"id", "run", "kind", "detail"}.
Item = dict[str, Any]


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

    def items(self, *, unclean: bool) -> list[Item]:
        """Registered items: those whose cleanup is not yet verified, or every one."""
        rows = self._db.execute(
            "SELECT id, run, kind, detail, status, proof FROM operations"
            + (" WHERE status != 'clean'" if unclean else "")
            + " ORDER BY id"
        ).fetchall()
        return [
            {
                "id": row[0],
                "run": row[1],
                "kind": row[2],
                "detail": json.loads(row[3]),
                "status": row[4],
                "proof": json.loads(row[5] or "null"),
            }
            for row in rows
        ]

    def operator_confirmed(self) -> set[str]:
        """Items the operator confirmed stopped; recovery does not check them again."""
        return {
            item["id"]
            for item in self.items(unclean=False)
            if (item["proof"] or {}).get("confirmed_by") == OPERATOR
        }

    def fence(self, items: Sequence[Item], detail: str) -> None:
        """Persist the barrier and the items to verify, in one transaction, before any check.

        An item the operator confirmed stopped stays clean.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            for item in items:
                self._db.execute(
                    "INSERT INTO operations (id, run, kind, detail, status, updated_at)"
                    " VALUES (?, ?, ?, ?, 'cleaning', ?)"
                    " ON CONFLICT (id) DO UPDATE SET status = 'cleaning', proof = NULL,"
                    " updated_at = excluded.updated_at"
                    " WHERE json_extract(proof, '$.confirmed_by') IS NOT ?",
                    (
                        item["id"],
                        item["run"],
                        item["kind"],
                        json.dumps(item["detail"], sort_keys=True),
                        _now(),
                        OPERATOR,
                    ),
                )
            self._db.execute(
                "UPDATE host_state SET revision = revision + 1, barrier = ?, detail = ?,"
                " updated_at = ?",
                (HOST_CLEANUP, detail, _now()),
            )

    def record_clean(self, item_id: str, proof: Mapping[str, Any]) -> None:
        """Mark one item clean; only with the proof that verified it."""
        with self._db:
            changed = self._db.execute(
                "UPDATE operations SET status = 'clean', proof = ?, updated_at = ?"
                " WHERE id = ? AND status != 'clean'",
                (json.dumps(proof, sort_keys=True), _now(), item_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"control store cannot mark {item_id} clean")

    def clear_barrier(self) -> bool:
        """Lift the barrier once nothing is left unclean; return whether it lifted."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self.items(unclean=True):
                return False
            self._db.execute(
                "UPDATE host_state SET revision = revision + 1, barrier = NULL, detail = NULL,"
                " reconciling = NULL, updated_at = ?",
                (_now(),),
            )
        return True

    def set_reconciling(self, reason: str | None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE host_state SET revision = revision + 1, reconciling = ?, updated_at = ?"
                " WHERE reconciling IS NOT ?",
                (reason, _now(), reason),
            )


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


def _unknown(item: Item, why: str) -> CleanupBlocked:
    return CleanupBlocked(
        f"{item['detail']['stage']} invocation {item['id']} {why}, so PluginBench cannot tell "
        "whether what it started still runs. Once you have checked that nothing of it runs, "
        f"confirm with `pluginbench repair --confirm-stopped {item['run']}`"
    )


def verify_invocation(item: Item) -> dict[str, Any]:
    """Prove nothing the invocation started still runs, or raise CleanupBlocked.

    Checks the process group the ledger recorded, conservatively: any live member blocks, even
    after the leader exits. A missing record, or one from another PID namespace, is unknown and
    blocks. A record from an earlier boot is gone with that boot. Children that left the group
    with `setsid` are not found; systemd scopes will cover them.
    """
    identity = item["detail"]["identity"] or {}
    pid, group = identity.get("pid"), identity.get("process_group")
    if type(pid) is not int or type(group) is not int or "boot_id" not in identity:
        raise _unknown(item, "has no verifiable process record")
    proof = {"id": item["id"], "checked": "process group", "at": _now()}
    if identity["boot_id"] != run_lock.boot_id():
        return {**proof, "checked": "earlier boot"}
    namespace = identity.get("pid_namespace")
    if not namespace or namespace != run_lock.pid_namespace():
        raise _unknown(item, "was recorded in another PID namespace")
    members = set(_group_members(group))
    try:
        if identity.get("start_time") is not None and process_start(pid) == identity["start_time"]:
            members.add(pid)
    except ProcessLookupError:
        pass
    if members:
        raise CleanupBlocked(
            f"process group {group} of {item['detail']['stage']} invocation {item['id']} still "
            f"has processes {', '.join(map(str, sorted(members)))}; stop them with "
            f"`kill -TERM -- -{group}`"
        )
    return proof


def stop_left_containers(
    item: Item, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Stop the trial containers the item's run left running and confirm none still runs."""
    evidence = Path(item["run"])
    if not (evidence / "frozen" / "suite").is_dir():
        raise CleanupBlocked(
            f"run {evidence} moved or was deleted, so the trial containers it left cannot be "
            "found. Once you have checked that none still runs, confirm with "
            f"`pluginbench repair --confirm-stopped {evidence}`"
        )
    try:
        trial_containers.stop_left_running(
            evidence, expected_runtime=item["detail"]["runtime"], progress=progress
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise CleanupBlocked(
            f"trial containers that {evidence} left running could not be stopped: {exc}"
        ) from exc
    return {"id": item["id"], "checked": "trial containers", "at": _now()}


def _refusal(failures: Sequence[str]) -> CleanupBlocked:
    count = f"{len(failures)} crashed call{'s' if len(failures) != 1 else ''}"
    return CleanupBlocked(
        f"cleanup of {count} is not verified, so nothing starts on this host: "
        f"{'; '.join(failures)}. Then run the command again"
    )


def _write_marker(state: Path, items: Sequence[Item]) -> None:
    pending = {item["id"]: item for item in [*_marker(state), *items]}
    run_lock._write_atomic(
        state / MARKER_NAME, {"items": list(pending.values()), "written_at": _now()}
    )


def _marker(state: Path) -> list[Item]:
    try:
        items: list[Item] = json.loads((state / MARKER_NAME).read_text())["items"]
    except FileNotFoundError:
        return []
    return items


def _hold_after_exit(lock: ExecutionLock) -> int:
    """Keep the host lock held after this process exits: a detached sleeper inherits it."""
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        pass_fds=(lock.fd,),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return sleeper.pid


def _fence(lock: ExecutionLock, store: ControlStore, items: Sequence[Item]) -> None:
    """Record `items` as pending before any check: the marker first, then the barrier.

    If neither write lands, the host stays locked after this process exits, rather than look
    free while a crashed call may still run.
    """
    try:
        _write_marker(lock.state, items)
        marked = True
    except Exception:
        marked = False
    try:
        store.fence(items, "recovery in progress")
    except Exception as exc:
        if marked:
            raise  # The marker fences the host; the next owner moves it into the barrier.
        sleeper = _hold_after_exit(lock)
        raise CleanupBlocked(
            f"PluginBench could not record the cleanup it must verify ({exc}), so process "
            f"{sleeper} keeps the host execution lock and nothing starts. Fix {lock.state}, "
            f"then stop that process with `kill {sleeper}` and run the command again"
        ) from exc
    lock.publish("reconciling")


def _settle(
    lock: ExecutionLock,
    store: ControlStore,
    items: Sequence[Item],
    progress: Callable[[str], None] | None = None,
) -> None:
    """Verify the fenced `items`; lift the barrier when nothing is left unclean, or raise."""
    confirmed = store.operator_confirmed()
    failures = []
    for item in items:
        if item["id"] in confirmed:
            continue
        try:
            proof = (
                verify_invocation(item)
                if item["kind"] == "invocation"
                else stop_left_containers(item, progress)
            )
        except CleanupBlocked as exc:
            failures.append(str(exc))
            continue
        store.record_clean(item["id"], proof)
    if failures:
        store.fence([], "; ".join(failures))
        lock.publish("blocked")
        raise _refusal(failures)
    lock.publish("active")
    if store.clear_barrier():
        (lock.state / MARKER_NAME).unlink(missing_ok=True)


def _recover(store: ControlStore, lock: ExecutionLock) -> None:
    """Verify again the cleanup a barrier or marker left; lift it, or keep it and raise."""
    pending = _marker(lock.state)
    if pending:
        store.fence(pending, "recovery in progress")
    if store.host().barrier is None:
        return
    lock.publish("reconciling")
    _settle(lock, store, store.items(unclean=True))


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


def _held() -> ExecutionLock:
    lock = run_lock.held_execution()
    if lock is None:
        raise RuntimeError("verifying cleanup after a crash needs the host execution lock")
    lock.assert_owned()
    return lock


def verify_crashed(ledger: Ledger, path: Path, stages: Collection[str]) -> frozenset[str]:
    """Verify cleanup of every open `stages` invocation in `ledger`; return their IDs.

    Call before accepting a crashed invocation's saved output or marking it unresolved, and pass
    the result to `Ledger.resolve_crashed`. Needs the host execution lock. A failure keeps the
    host barrier with the failing invocations, leaves every row open, and raises CleanupBlocked.
    """
    items = [
        {
            "id": row["id"],
            "run": str(path.parent),
            "kind": "invocation",
            "detail": {
                "stage": row["stage"],
                "scope": row["scope"],
                "label": row["container_label"],
                "identity": row["identity"],
            },
        }
        for row in ledger.invocations()
        if row["stage"] in stages and row["state"] in OPEN_STATES
    ]
    if not items:
        return frozenset()
    lock = _held()
    with ControlStore.open(lock.state) as store:
        _fence(lock, store, items)
        _settle(lock, store, items)
    return frozenset(item["id"] for item in items)


def stop_containers_left_by(
    evidence: Path, runtime: dict[str, object], progress: Callable[[str], None] | None
) -> None:
    """Stop the trial containers a killed run left running, before its crashed calls close.

    Needs the host execution lock. A failure keeps the host barrier and raises CleanupBlocked.
    """
    lock = _held()
    item = {
        "id": f"containers-{uuid4().hex}",
        "run": str(evidence),
        "kind": "containers",
        "detail": {"runtime": runtime},
    }
    with ControlStore.open(lock.state) as store:
        _fence(lock, store, [item])
        _settle(lock, store, [item], progress)


def confirm_stopped(lock: ExecutionLock, run: Path) -> tuple[list[Item], bool]:
    """Record the operator's word that nothing `run`'s unverified cleanups started still runs.

    Marks each clean with that confirmation and its time. Returns them and whether the barrier
    lifted; another run's unverified cleanup keeps it.
    """
    run = run.resolve()
    with ControlStore.open(lock.state) as store:
        pending = _marker(lock.state)
        if pending:
            store.fence(pending, "recovery in progress")
        confirmed = [
            item for item in store.items(unclean=True) if Path(item["run"]).is_relative_to(run)
        ]
        at = _now()
        for item in confirmed:
            store.record_clean(
                item["id"], {"id": item["id"], "confirmed_by": OPERATOR, "at": at}
            )
        lifted = store.host().barrier is None or store.clear_barrier()
        if lifted:
            (lock.state / MARKER_NAME).unlink(missing_ok=True)
    return confirmed, lifted


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
