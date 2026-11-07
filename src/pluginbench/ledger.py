"""Single-writer SQLite ledger of coding attempt and stage states.

Evidence files stay the record of what happened; the ledger only says how far each attempt got.
One coordinator thread writes it. Every write is its own transaction, and a state is marked
complete only after the receipt it points to is on disk. The default rollback journal leaves
no sidecar files once a transaction ends.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
LEDGER_NAME = "ledger.sqlite"

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def create(
        cls, path: Path, entries: Iterable[dict[str, Any]], states: dict[str, str]
    ) -> Ledger:
        """A new ledger with one row per plan entry, in plan (slot) order.

        It is built in a temporary file and renamed into place, so a killed build never leaves
        a ledger that looks valid but lacks attempts.
        """
        if path.exists():
            raise ValueError(f"ledger already exists: {path}")
        temporary = path.with_name(f".{path.name}.tmp")
        # Left by a build that was killed; a stale journal must not roll into the new file.
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-journal").unlink(missing_ok=True)
        connection = sqlite3.connect(temporary)
        with connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
        connection.close()
        temporary.replace(path)
        return cls.open(path)

    @classmethod
    def open(cls, path: Path) -> Ledger:
        if not path.is_file():
            raise ValueError(f"ledger is missing: {path}")
        connection = sqlite3.connect(path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
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
        if version != SCHEMA_VERSION:
            connection.close()
            raise ValueError(f"unsupported ledger schema version {version}")
        return cls(connection)

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
        """Mark complete; `record` references the attempt's record receipt, already on disk."""
        encoded = json.dumps(record, sort_keys=True)
        with self._db:
            changed = self._db.execute(
                "UPDATE attempts SET state = 'complete', record = ?, updated_at = ?"
                " WHERE id = ? AND state IN ('pending', 'running', 'interrupted')",
                (encoded, _now(), attempt_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"ledger cannot complete attempt {attempt_id}")
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
