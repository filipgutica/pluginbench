"""`pluginbench dashboard` serves a read-only view of run directories on loopback only."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pluginbench import dashboard, harbor_backend, run_lock
from pluginbench.ledger import LEDGER_NAME, Ledger
from pluginbench.run_status import run_status

ENTRIES = [
    {"id": "a1", "task_id": "task-a", "arm": "baseline", "attempt": 1},
    {"id": "a2", "task_id": "task-a", "arm": "workflow", "attempt": 1},
]


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _run(root: Path, *, name: str = "fixture") -> Path:
    """A run directory with a ledger: a1 complete and passing, a2 running."""
    _write(
        root / "evaluation.json",
        {"name": name, "runner": {"harness": "codex", "model": "m", "reasoning": "high"}},
    )
    _write(root / "evaluation-binding.json", {"sha256": "0" * 64})
    evidence = root / "evidence"
    _write(evidence / "plan.json", {"entries": ENTRIES})
    _write(evidence / "run.json", {"status": "running", "attempt_states": {}})
    handoff = evidence / "attempts/a1/handoff"
    _write(handoff / "execution.json", {"usage": {"input_tokens": 10, "output_tokens": 5}})
    _write(handoff / "correctness.json", {"checks": {"behavior": True}})
    _write(handoff / "verifier-result.json", {"patch_sha256": "ab" * 32})
    Ledger.create(evidence / LEDGER_NAME, ENTRIES, {"a1": "complete", "a2": "running"}).close()
    return root


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _CountingStatus:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next: Exception | None = None

    def __call__(self, root: Path, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        return run_status(root, **kwargs)


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str, **kwargs: object) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(harbor_backend, "_docker", docker)
    return calls


def test_discovery_finds_nested_runs_but_not_symlinks_hidden_folders_or_runs_inside_runs(
    tmp_path: Path,
) -> None:
    outer = _run(tmp_path / "batch/one")
    deep = _run(tmp_path / "batch/two/deep/run")
    _run(outer / "inner")  # Inside a run: never a run of its own.
    _run(tmp_path / ".hidden/run")
    (tmp_path / "linked").symlink_to(tmp_path / "batch", target_is_directory=True)

    found = dashboard.discover([tmp_path])

    assert sorted(found.values()) == [outer, deep]
    assert found[dashboard.run_key(outer)] == outer


def test_a_folder_that_cannot_be_searched_hides_no_other_run(tmp_path: Path) -> None:
    run = _run(tmp_path / "run")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)  # Such as a folder a container left owned by root.
    try:
        assert dashboard.discover([tmp_path]) == {dashboard.run_key(run): run}
    finally:
        locked.chmod(0o700)


def test_a_run_no_process_owns_is_read_again_only_after_a_watched_file_changes(
    tmp_path: Path, no_docker: list[tuple[str, ...]]
) -> None:
    root = _run(tmp_path / "run")
    clock, status = _Clock(), _CountingStatus()
    runs = dashboard.Runs([tmp_path], clock=clock, status=status)
    key = dashboard.run_key(root)

    first = runs.snapshot(key)
    clock.now += 60
    again = runs.snapshot(key)
    assert first is again
    assert len(status.calls) == 1

    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": "started"}) + "\n")
    clock.now += 60
    changed = runs.snapshot(key)

    assert len(status.calls) == 2
    assert changed is not None and changed.status is not None
    assert changed.status["last_event"] == {"event": "started"}
    # Polls never ask Docker; the run page asks only when the reader does.
    assert all(call == {"containers": False} for call in status.calls)
    assert not no_docker


def test_an_owned_run_is_read_on_every_poll_so_a_killed_owner_shows_at_once(
    tmp_path: Path, no_docker: list[tuple[str, ...]]
) -> None:
    root = _run(tmp_path / "run")
    clock, status = _Clock(), _CountingStatus()
    runs = dashboard.Runs([tmp_path], clock=clock, status=status)
    key = dashboard.run_key(root)

    with run_lock.hold(root):
        owned = runs.snapshot(key)
        clock.now += 1
        assert runs.snapshot(key) is owned  # Never more often than `status --watch`.
    assert owned is not None and owned.status is not None and owned.status["active"]
    # The owner is gone and wrote nothing a fingerprint sees.
    clock.now += dashboard.READ_INTERVAL
    after = runs.snapshot(key)

    assert after is not None and after.status is not None
    assert after.status["active"] is False
    assert len(status.calls) == 2


def test_a_run_shows_as_owned_as_soon_as_a_process_takes_it(
    tmp_path: Path, no_docker: list[tuple[str, ...]]
) -> None:
    root = _run(tmp_path / "run")
    clock = _Clock()
    runs = dashboard.Runs([tmp_path], clock=clock)
    key = dashboard.run_key(root)
    before = runs.snapshot(key)
    assert before is not None and before.status is not None and not before.status["active"]

    clock.now += dashboard.READ_INTERVAL
    with run_lock.hold(root):
        after = runs.snapshot(key)

    assert after is not None and after.status is not None and after.status["active"]


def test_a_failed_read_keeps_the_last_good_status_says_why_and_tries_again(
    tmp_path: Path, no_docker: list[tuple[str, ...]]
) -> None:
    root = _run(tmp_path / "run")
    clock, status = _Clock(), _CountingStatus()
    runs = dashboard.Runs([tmp_path], clock=clock, status=status)
    key = dashboard.run_key(root)
    good = runs.snapshot(key)
    assert good is not None

    (root / "events.jsonl").write_text("")  # A change, so the next poll reads again.
    status.fail_next = json.JSONDecodeError("Expecting value", "", 0)
    clock.now += 60
    stale = runs.snapshot(key)

    assert stale is not None
    assert stale.status == good.status
    assert stale.error == "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
    assert dashboard._summary(key, root, stale)["stale"] is True
    clock.now += 60
    healed = runs.snapshot(key)  # No file changed since the failure; it reads again anyway.
    assert healed is not None and healed.error is None
    assert len(status.calls) == 3


@pytest.fixture
def served(tmp_path: Path, no_docker: list[tuple[str, ...]]) -> Iterator[tuple[int, Path]]:
    root = _run(tmp_path / "runs/one")
    server = dashboard.serve([tmp_path / "runs"], port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], root
    finally:
        server.shutdown()
        server.server_close()


def _get(
    port: int, path: str, *, method: str = "GET", host: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def test_the_api_lists_runs_and_their_status_without_writing_or_calling_docker(
    served: tuple[int, Path], no_docker: list[tuple[str, ...]]
) -> None:
    port, root = served
    before = _tree(root)
    key = dashboard.run_key(root)

    code, _, body = _get(port, "/api/runs")
    [row] = json.loads(body)["runs"]
    detail_code, _, detail = _get(port, f"/api/runs/{key}")
    run = json.loads(detail)

    assert code == 200 and detail_code == 200
    assert row["key"] == key
    assert row["name"] == "fixture"
    assert row["runner"] == {"harness": "codex", "model": "m", "reasoning": "high", "cli_version": None}
    assert row["states"] == {"complete": 1, "running": 1}
    assert row["stale"] is False
    assert run["status"]["attempts_total"] == 2
    assert run["status"]["left_running"] is None
    assert not no_docker
    assert _tree(root) == before


def test_containers_are_checked_only_when_asked(
    served: tuple[int, Path], no_docker: list[tuple[str, ...]]
) -> None:
    port, root = served
    (root / "evidence/frozen/suite/0/task").mkdir(parents=True)  # Trials Docker could hold.
    code, _, body = _get(port, f"/api/runs/{dashboard.run_key(root)}/containers")

    assert code == 200
    assert json.loads(body)["left_running"]["checked"] is True
    assert {call[0] for call in no_docker} == {"ps"}


@pytest.mark.parametrize(
    "host",
    ["evil.example:{port}", "127.0.0.1:1", "localhost", "", "127.0.0.1:{port}, evil.example"],
)
def test_requests_not_addressed_to_this_server_are_refused(
    served: tuple[int, Path], host: str
) -> None:
    port, _ = served
    code, _, body = _get(port, "/api/runs", host=host.format(port=port))

    assert code == 403
    assert "runs" not in json.loads(body)


def test_a_duplicate_host_header_is_refused(served: tuple[int, Path]) -> None:
    port, _ = served
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("GET", "/api/runs", skip_host=True)
    connection.putheader("Host", f"127.0.0.1:{port}")
    connection.putheader("Host", "evil.example")
    connection.endheaders()

    assert connection.getresponse().status == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_the_dashboard_only_reads(served: tuple[int, Path], method: str) -> None:
    port, _ = served
    assert _get(port, "/api/runs", method=method)[0] == 405


def test_reports_are_served_by_run_key_and_fixed_name_only(served: tuple[int, Path]) -> None:
    port, root = served
    key = dashboard.run_key(root)
    _write(root / "report/results.json", {"schema_version": 2})
    (root / "report/report.html").write_text("<p>report</p>")
    (root / "report/results.csv").write_text("arm\n")
    (root / "secret.txt").write_text("private")

    code, headers, body = _get(port, f"/runs/{key}/report.html")
    assert (code, body) == (200, b"<p>report</p>")
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    # Whoever produced the run, its report cannot read the dashboard's API.
    assert headers["Content-Security-Policy"].startswith("sandbox allow-scripts")
    assert _get(port, f"/runs/{key}/results.csv")[2] == b"arm\n"
    assert _get(port, f"/runs/{key}/results.json")[0] == 404
    assert _get(port, f"/runs/{key}/../secret.txt")[0] == 404
    assert _get(port, "/runs/0000000000000000/report.html")[0] == 404
    assert _get(port, "/static/../dashboard.py")[0] == 404

    (root / "report/report.html").unlink()
    (root / "report/report.html").symlink_to(root / "secret.txt")
    assert _get(port, f"/runs/{key}/report.html")[0] == 404


def test_the_page_and_its_assets_load_under_a_strict_policy(served: tuple[int, Path]) -> None:
    port, _ = served
    code, headers, page = _get(port, "/")

    assert code == 200
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    for asset in ("tokens.css", "dashboard.css", "dashboard.js"):
        assert f"/static/{asset}".encode() in page
        assert _get(port, f"/static/{asset}")[0] == 200


def test_serve_refuses_a_root_that_is_not_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        dashboard.serve([tmp_path / "missing"], port=0)
