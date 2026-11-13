"""`pluginbench dashboard`: a local, read-only web view of the runs under some directories.

Run directories stay the record. The server keeps only an in-memory cache of `run_status`
snapshots, never writes under a root, and runs no Docker command unless the page asks for it.
It answers only on 127.0.0.1 and only to requests addressed to that host and port.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from pluginbench.attempt_rows import attempt_rows
from pluginbench.run_status import run_status

# Files a run rewrites as it moves; a change in any of them means its status may have changed.
_WATCHED = (
    "evaluation.json",
    "evaluation-binding.json",
    "run.lock",
    "events.jsonl",
    "run.log",
    "evidence/ledger.sqlite",
    "evidence/run.json",
    "report/results.json",
    "report/report.html",
)
# Never read a run that a process owns more often than `status --watch` does.
READ_INTERVAL = 2.0
# Walk the roots again at most this often, so new runs appear in the list.
WALK_INTERVAL = 10.0
# A file caught mid-write, a busy ledger, or a receipt without a field it should have.
_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error)
_STATIC = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
    "tokens.css": "text/css; charset=utf-8",
}
_REPORT_FILES = {
    "report.html": "text/html; charset=utf-8",
    "results.csv": "text/csv; charset=utf-8",
}
_PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def run_key(path: Path) -> str:
    """A short, stable, local name for a run directory; never published."""
    return sha256(str(path.resolve()).encode()).hexdigest()[:16]


def _regular_file(path: Path) -> bool:
    """A file that is not a symlink. False, not an error, where the folder cannot be searched."""
    return os.path.isfile(path) and not os.path.islink(path)


def _is_run(directory: Path) -> bool:
    return all(
        _regular_file(directory / name) for name in ("evaluation.json", "evaluation-binding.json")
    )


def discover(roots: Iterable[Path]) -> dict[str, Path]:
    """Run directories under the roots, by key. Symlinks are never followed; runs are leaves."""
    found: dict[str, Path] = {}
    pending = [root.expanduser().absolute() for root in roots]
    while pending:
        directory = pending.pop()
        if _is_run(directory):
            found[run_key(directory)] = directory
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # Unreadable or gone since the parent was listed: it holds no visible run.
        pending.extend(
            Path(entry.path)
            for entry in entries
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
        )
    return found


Fingerprint = tuple[tuple[str, int, int] | None, ...]


def _fingerprint(root: Path) -> Fingerprint:
    rows: list[tuple[str, int, int] | None] = []
    for name in _WATCHED:
        try:
            stat = (root / name).stat()
        except OSError:
            rows.append(None)
        else:
            rows.append((name, stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def _selection(root: Path) -> dict[str, Any]:
    selection = json.loads((root / "evaluation.json").read_text())
    return selection if isinstance(selection, dict) else {}


def _runner(root: Path) -> dict[str, Any]:
    runner = _selection(root).get("runner") or {}
    return {key: runner.get(key) for key in ("harness", "model", "reasoning", "cli_version")}


def _stamp(root: Path) -> tuple[tuple[int, int], ...]:
    """The report and settings as they stand: attempt rows change only when these do."""
    stats = ((root / name).stat() for name in ("report/results.json", "evaluation.json"))
    return tuple((stat.st_size, stat.st_mtime_ns) for stat in stats)


@dataclass
class _Snapshot:
    fingerprint: Fingerprint | None
    read_at: float
    status: dict[str, Any] | None
    runner: dict[str, Any] | None
    error: str | None


class Runs:
    """The runs under the roots and their last good status, refreshed on demand."""

    def __init__(
        self,
        roots: Iterable[Path],
        *,
        clock: Callable[[], float] = time.monotonic,
        status: Callable[..., dict[str, Any]] = run_status,
    ) -> None:
        self.roots = [root.expanduser().absolute() for root in roots]
        self._clock = clock
        self._status = status
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {}
        self._walked_at: float | None = None
        self._walking = threading.Lock()
        self._snapshots: dict[str, _Snapshot] = {}
        self._reading: dict[str, threading.Lock] = {}
        self._rows_lock = threading.Lock()
        self._rows: dict[str, tuple[tuple[tuple[int, int], ...], list[dict[str, Any]]]] = {}

    def paths(self) -> dict[str, Path]:
        with self._lock:
            first = self._walked_at is None
            due = first or self._clock() - (self._walked_at or 0) >= WALK_INTERVAL
        # Walk outside the lock, one walk at a time. Until the first walk ends every request
        # waits for it; after that, requests use the runs found last while a walk runs.
        if due and self._walking.acquire(blocking=first):
            try:
                if self._walked_at is None or self._clock() - self._walked_at >= WALK_INTERVAL:
                    found = discover(self.roots)
                    with self._lock:
                        self._paths, self._walked_at = found, self._clock()
            finally:
                self._walking.release()
        with self._lock:
            return dict(self._paths)

    def path(self, key: str) -> Path | None:
        return self.paths().get(key)

    def snapshot(self, key: str) -> _Snapshot | None:
        root = self.path(key)
        if root is None:
            return None
        with self._lock:
            reading = self._reading.setdefault(key, threading.Lock())
        # One read in flight per run: a second request waits and then reuses its result.
        with reading:
            cached = self._snapshots.get(key)
            now = self._clock()
            if cached is not None and cached.status is not None:
                live = bool(cached.status.get("active"))
                if now - cached.read_at < READ_INTERVAL:
                    return cached
                # A run that no process owns changes only by writing a watched file. An owned
                # run is read again every time: its owner can die without writing anything.
                if not live and cached.fingerprint == _fingerprint(root):
                    return cached
            fingerprint = _fingerprint(root)
            try:
                status = self._status(root, containers=False)
                runner = _runner(root)
            except _READ_ERRORS as exc:
                # Keep the last good snapshot and say why it is stale. No fingerprint is
                # saved, so the next poll reads again even when no file changed.
                fresh = _Snapshot(
                    fingerprint=None,
                    read_at=now,
                    status=cached.status if cached else None,
                    runner=cached.runner if cached else None,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                fresh = _Snapshot(fingerprint, now, status, runner, None)
            self._snapshots[key] = fresh
            return fresh

    def summaries(self) -> list[dict[str, Any]]:
        rows = []
        for key, root in self.paths().items():
            snapshot = self.snapshot(key)
            if snapshot is None:
                continue  # Removed between the walk and the read.
            rows.append(_summary(key, root, snapshot))
        return rows

    def attempts(self) -> dict[str, Any]:
        """Attempt rows of every run that has a report, and why any report could not be read."""
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        with self._rows_lock:
            for key, root in self.paths().items():
                if not _regular_file(root / "report/results.json"):
                    continue  # No report yet: its attempts appear once `report` writes one.
                cached = self._rows.get(key)
                try:
                    stamp = _stamp(root)
                    if cached is None or cached[0] != stamp:
                        selection = _selection(root)
                        report = json.loads((root / "report/results.json").read_text())
                        named = [
                            {"run_key": key, "run_name": selection.get("name"), **row}
                            for row in attempt_rows(report, _runner(root))
                        ]
                        cached = self._rows[key] = (stamp, named)
                except _READ_ERRORS as exc:
                    # A report caught mid-write: keep the rows read before, and say so.
                    name = cached[1][0]["run_name"] if cached and cached[1] else None
                    errors.append(
                        {
                            "key": key,
                            "run": name or str(root),
                            "kept": cached is not None,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                if cached is not None:
                    rows.extend(cached[1])
        return {"attempts": rows, "errors": errors}

    def containers(self, key: str) -> dict[str, Any] | None:
        """Containers a run left running, from Docker; only when the page asks."""
        root = self.path(key)
        if root is None:
            return None
        status = self._status(root)
        return {"active": status["active"], "left_running": status["left_running"]}


def _heartbeat(status: dict[str, Any]) -> dict[str, Any] | None:
    """The heartbeat with its age measured now, not when the snapshot was read."""
    heartbeat = status.get("heartbeat")
    if not isinstance(heartbeat, dict) or not isinstance(heartbeat.get("at"), str):
        return heartbeat
    age = datetime.now(UTC) - datetime.fromisoformat(heartbeat["at"])
    return {"at": heartbeat["at"], "age_seconds": round(age.total_seconds())}


def _summary(key: str, root: Path, snapshot: _Snapshot) -> dict[str, Any]:
    status = snapshot.status or {}
    return {
        "key": key,
        "path": str(root),
        "name": status.get("name"),
        "runner": snapshot.runner,
        "phase": status.get("phase"),
        "active": status.get("active"),
        "heartbeat": _heartbeat(status),
        "attempts_total": status.get("attempts_total"),
        "states": status.get("states"),
        "outcomes": status.get("outcomes"),
        "tokens": status.get("tokens"),
        "discarded": len(status.get("discarded") or []),
        "report": status.get("report") is not None,
        "stale": snapshot.error is not None,
        "error": snapshot.error,
    }


def _detail(key: str, snapshot: _Snapshot) -> dict[str, Any]:
    status = dict(snapshot.status or {})
    if status:
        status["heartbeat"] = _heartbeat(status)
    return {
        "key": key,
        "runner": snapshot.runner,
        "stale": snapshot.error is not None,
        "error": snapshot.error,
        "status": status or None,
    }


def _handler(runs: Runs) -> type[BaseHTTPRequestHandler]:
    static = files("pluginbench").joinpath("dashboard")

    class Handler(BaseHTTPRequestHandler):
        server_version = "pluginbench-dashboard"

        def _send(self, code: int, body: bytes, content_type: str, **headers: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: object, code: int = HTTPStatus.OK) -> None:
            body = json.dumps(value).encode()
            self._send(code, body, "application/json")

        def _error(self, code: HTTPStatus, message: str) -> None:
            self._json({"error": message}, code)

        def _addressed_here(self) -> bool:
            # Exactly one Host naming this loopback port: a page on another site that rebinds
            # its own name to 127.0.0.1 still sends its own name, and is refused.
            hosts = self.headers.get_all("Host") or []
            port = self.connection.getsockname()[1]
            return len(hosts) == 1 and hosts[0] in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self) -> None:
            if not self._addressed_here():
                self._error(HTTPStatus.FORBIDDEN, "requests must be addressed to this server")
                return
            parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
            try:
                self._route(parts)
            except _READ_ERRORS as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

        def _route(self, parts: list[str]) -> None:
            if not parts:
                page = static.joinpath("index.html").read_bytes()
                self._send(
                    HTTPStatus.OK,
                    page,
                    "text/html; charset=utf-8",
                    Content_Security_Policy=_PAGE_POLICY,
                )
            elif len(parts) == 2 and parts[0] == "static" and parts[1] in _STATIC:
                self._send(HTTPStatus.OK, static.joinpath(parts[1]).read_bytes(), _STATIC[parts[1]])
            elif parts == ["api", "attempts"]:
                self._json(runs.attempts())
            elif parts == ["api", "runs"]:
                self._json({"roots": [str(root) for root in runs.roots], "runs": runs.summaries()})
            elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
                snapshot = runs.snapshot(parts[2])
                if snapshot is None:
                    self._error(HTTPStatus.NOT_FOUND, "no such run")
                else:
                    self._json(_detail(parts[2], snapshot))
            elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "containers":
                containers = runs.containers(parts[2])
                if containers is None:
                    self._error(HTTPStatus.NOT_FOUND, "no such run")
                else:
                    self._json(containers)
            elif len(parts) == 3 and parts[0] == "runs" and parts[2] in _REPORT_FILES:
                self._report(parts[1], parts[2])
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")

        def _report(self, key: str, name: str) -> None:
            root = runs.path(key)
            if root is None:
                self._error(HTTPStatus.NOT_FOUND, "no such run")
                return
            directory, path = root / "report", root / "report" / name
            # Serve the run's own report file, never one a symlink points elsewhere.
            if os.path.islink(directory) or not _regular_file(path):
                self._error(HTTPStatus.NOT_FOUND, f"this run has no {name}")
                return
            # A sandboxed report gets an opaque origin: its scripts run, but cannot read the
            # dashboard's API, whoever produced the run directory.
            self._send(
                HTTPStatus.OK,
                path.read_bytes(),
                _REPORT_FILES[name],
                Content_Security_Policy="sandbox allow-scripts allow-downloads",
            )

        def _refuse(self) -> None:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "the dashboard only reads")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _refuse

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            pass  # The page polls every few seconds; errors are still logged.

    return Handler


def serve(roots: list[Path], *, port: int) -> ThreadingHTTPServer:
    """A dashboard server bound to 127.0.0.1; the caller runs serve_forever()."""
    missing = [str(root) for root in roots if not root.expanduser().is_dir()]
    if missing:
        raise ValueError(f"not a directory: {', '.join(missing)}")
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(Runs(roots)))
    server.daemon_threads = True
    return server
