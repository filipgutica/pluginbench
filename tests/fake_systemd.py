"""A fake systemd user manager for tests that cannot reach a real one.

`systemd-run` and `systemctl` are executables first on PATH. A scope is a file in the fake's
state directory, and its members are the processes whose environment holds PB_FAKE_SCOPE with its
name, which every descendant inherits, as a cgroup holds every descendant. So a `setsid` child
stays a member, and `systemctl stop` and `kill` signal every member.

Flags, as files in the state directory: `bus-down` makes every query fail as an unreachable bus
does, `stuck` makes stop and kill do nothing, and `manager` holds what is-system-running prints.
"""

from __future__ import annotations

import inspect
import os
import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from pluginbench import invocation_runtime

MARKER = "PB_FAKE_SCOPE"
HANDLERS = Path(__file__).parent / "scope_handlers"

_RUN = r'''
import os, sys
state = os.environ["FAKE_SYSTEMD"]
if sys.argv[1:] == ["--version"]:
    version = os.path.join(state, "version")
    print(open(version).read() if os.path.exists(version) else "systemd 255 (fake)")
    sys.exit(0)
if os.path.exists(os.path.join(state, "run-fails")):
    print("Failed to start transient scope unit", file=sys.stderr)
    sys.exit(1)
split = sys.argv.index("--")
unit = next(item.split("=", 1)[1] for item in sys.argv[1:split] if item.startswith("--unit="))
with open(os.path.join(state, "units", unit), "w") as stream:
    stream.write(" ".join(sys.argv[1:split]))
os.environ["PB_FAKE_SCOPE"] = unit
os.execv(sys.argv[split + 1], sys.argv[split + 1:])
'''

_CTL = r'''
import os, signal, sys
state = os.environ["FAKE_SYSTEMD"]
args = [item for item in sys.argv[1:] if item not in ("--user", "--no-block")]
if os.path.exists(os.path.join(state, "bus-down")):
    print("Failed to connect to bus: No medium found", file=sys.stderr)
    sys.exit(1)
command, unit = args[0], args[-1]
if command == "is-system-running":
    path = os.path.join(state, "manager")
    state_name = open(path).read().strip() if os.path.exists(path) else "running"
    print(state_name)
    sys.exit(0 if state_name == "running" else 1)  # As systemd does, even when degraded.
path = os.path.join(state, "units", unit)
alive = members(unit)
if command == "show":
    if os.path.exists(path) and not alive:
        os.unlink(path)  # Collected once empty, as --collect does.
    if os.path.exists(path):
        print(f"LoadState=loaded\nActiveState=active\nControlGroup=/fake/{unit}")
    else:
        print("ControlGroup=\nLoadState=not-found\nActiveState=inactive")
    sys.exit(0)
if command in ("stop", "kill") and not os.path.exists(os.path.join(state, "stuck")):
    with open(os.path.join(state, "log"), "a") as log:
        log.write(" ".join(args) + "\n")
    name = next((item.split("=", 1)[1] for item in args if item.startswith("--signal=")), "SIGKILL")
    for pid in alive:
        try:
            os.kill(pid, getattr(signal, name))
        except ProcessLookupError:
            pass
sys.exit(0)
'''


def members(unit: str) -> list[int]:
    """Live processes in the fake scope `unit`. A zombie's environment reads empty."""
    found = []
    needle = f"{MARKER}={unit}".encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            environment = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if needle in environment:
            found.append(int(entry))
    return found


class FakeSystemd:
    def __init__(self, root: Path) -> None:
        self.root = root

    def flag(self, name: str, content: str = "") -> None:
        (self.root / name).write_text(content)

    def clear(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)

    def units(self) -> list[str]:
        return sorted(path.name for path in (self.root / "units").iterdir())

    def log(self) -> list[str]:
        path = self.root / "log"
        return path.read_text().splitlines() if path.exists() else []


def fake_cgroup(pid: int) -> str | None:
    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    prefix = f"{MARKER}=".encode()
    unit = next(
        (item[len(prefix) :].decode() for item in environment if item.startswith(prefix)), None
    )
    return f"/fake/{unit}" if unit else "/not-a-scope"


def fake_populated(cgroup: str) -> bool:
    return bool(members(cgroup.rsplit("/", 1)[-1]))


def fake_cgroups(root: Path) -> dict[str, object]:
    """What `invocation_runtime` reads of cgroups, faked for the scopes under `root`. An owner
    in another process applies these too (see test_invocation_runtime._OWNER_SCRIPT)."""
    return {"CGROUP_ROOT": root / "cgroup", "_cgroup": fake_cgroup, "_populated": fake_populated}


@pytest.fixture(name="fake_systemd")
def fake_systemd_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSystemd]:
    root = tmp_path / "fake-systemd"
    (root / "units").mkdir(parents=True)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    helpers = f"from pathlib import Path\nMARKER = {MARKER!r}\n{inspect.getsource(members)}"
    for name, source in (("systemd-run", _RUN), ("systemctl", helpers + _CTL)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
    (root / "cgroup").mkdir()
    (root / "cgroup" / "cgroup.controllers").write_text("cpu memory pids\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMD", str(root))
    monkeypatch.setenv("PYTHONPATH", f"{HANDLERS}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    for name, value in fake_cgroups(root).items():
        monkeypatch.setattr(invocation_runtime, name, value)
    register_handlers(monkeypatch)
    fake = FakeSystemd(root)
    try:
        yield fake
    finally:
        for unit in fake.units():
            for pid in members(unit):
                with suppress(ProcessLookupError):
                    os.kill(pid, 9)


def register_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow the test handlers in this process too, which validates each request."""
    for name in ("echo", "escape", "hang", "fail", "chatty", "nested_collect", "descriptors"):
        monkeypatch.setitem(invocation_runtime.HANDLERS, name, f"sitecustomize:{name}")
