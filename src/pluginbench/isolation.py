"""Constrain local jobs to their owned files, not merely a private HOME."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import subprocess
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterable

from pluginbench.processes import run_command

PROTOCOL = "local-filesystem-v1"
SYSTEM_ROOTS = (
    "/System",
    "/usr/bin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/sbin",
    "/usr/share",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/Library/Apple",
    "/Library/Developer",
    "/Library/Keychains",
    "/private/etc",
    "/etc",
    "/dev",
    "/private/var/db",
    "/private/var/run",
    "/private/var/select",
    "/opt/homebrew/Cellar",
    "/opt/homebrew/opt",
)
TOOLS = ("codex", "claude", "node", "npm", "pnpm", "fallow", "git", "python3")


class RuntimeDriftError(RuntimeError):
    """A frozen comparison can no longer admit calls with this runtime."""


def _roots(paths: Iterable[Path]) -> list[Path]:
    return sorted(
        {
            variant
            for path in paths
            if path.exists()
            for variant in (path.absolute(), path.resolve())
        },
        key=str,
    )


def tool_roots() -> list[Path]:
    roots = [Path(sys.prefix), Path(sys.base_prefix)]
    for name in TOOLS:
        executable = shutil.which(name)
        if executable:
            path = Path(executable).resolve()
            roots.append(Path(executable))
            # Grant package assets, never an arbitrary executable's parent/home.
            for prefix in (
                Path("/opt/homebrew/Cellar"),
                Path("/opt/homebrew/Caskroom"),
                Path("/usr/local/Cellar"),
                Path("/usr/local/Caskroom"),
            ):
                if path.is_relative_to(prefix) and len(path.relative_to(prefix).parts) > 2:
                    roots.append(prefix.joinpath(*path.relative_to(prefix).parts[:2]))
            nvm = Path.home() / ".nvm/versions/node"
            if path.is_relative_to(nvm) and len(path.relative_to(nvm).parts) > 1:
                roots.append(nvm / path.relative_to(nvm).parts[0])
            for parent in path.parents:
                if parent.name == "node_modules":
                    parts = path.relative_to(parent).parts
                    size = 2 if parts[0].startswith("@") else 1
                    if len(parts) > size:
                        roots.append(parent.joinpath(*parts[:size]))
                    break
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"):
        if value := os.environ.get(key):
            roots.append(Path(value))
    return _roots(roots)


@lru_cache(maxsize=64)
def _file_hash(path: Path, signature: tuple[int, ...]) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binary_hash(path: Path) -> str:
    stat = path.stat()
    return _file_hash(
        path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    )


def runtime_identity() -> dict[str, object]:
    system = platform.system()
    backend = (
        Path("/usr/bin/sandbox-exec")
        if system == "Darwin"
        else Path(shutil.which("bwrap") or "/nonexistent/pluginbench-bwrap")
    )
    if system not in {"Darwin", "Linux"} or not backend.is_file():
        raise ValueError("local evaluation requires macOS sandbox-exec or Linux bubblewrap")
    return {
        "protocol": PROTOCOL,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "backend": str(backend),
        "backend_sha256": _binary_hash(backend),
        "os": system,
        "os_release": platform.release(),
        "system_read_roots": [str(path) for path in _roots(map(Path, SYSTEM_ROOTS))],
        "tool_read_roots": [str(path) for path in tool_roots()],
        "tools": {
            name: {
                "executable": str(Path(path).resolve()),
                "binary_sha256": _binary_hash(Path(path).resolve()),
            }
            for name in TOOLS
            if (path := shutil.which(name))
        },
        "filesystem": "deny reads and writes outside runtime, selected trusted assets, and owned job roots",
        "network": "agent egress allowed; trusted tests use host network",
        "process_limit": "POSIX process group; macOS does not provide a PID namespace",
    }


def assert_runtime(expected: dict[str, object]) -> None:
    try:
        actual = runtime_identity()
    except (OSError, ValueError) as error:
        raise RuntimeDriftError("frozen local runtime is no longer available") from error
    if actual != expected:
        raise RuntimeDriftError("local runtime changed after the comparison was frozen")


def referenced_paths(value: object) -> list[Path]:
    """Existing absolute paths in operator-owned adapter settings."""
    if isinstance(value, dict):
        return [path for item in value.values() for path in referenced_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in referenced_paths(item)]
    if isinstance(value, str) and value.startswith("/") and Path(value).exists():
        return [Path(value)]
    return []


def verifier_paths(script: Path) -> list[Path]:
    """Read dependency declarations without executing the prepared verifier."""
    try:
        nodes = ast.parse(script.read_text()).body
    except SyntaxError:
        return []  # Execution records an invalid verifier as an unavailable check.
    return [
        path
        for node in nodes
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PAYLOAD" for target in node.targets)
        for path in referenced_paths(ast.literal_eval(node.value))
    ]


def command(
    argv: list[str],
    *,
    writable: list[Path],
    readable: Iterable[Path] = (),
    expected: dict[str, object] | None = None,
) -> tuple[list[str], dict[str, object]]:
    try:
        identity = runtime_identity()
    except (OSError, ValueError) as error:
        if expected is not None:
            raise RuntimeDriftError("frozen local runtime is no longer available") from error
        raise
    if expected is not None and identity != expected:
        raise RuntimeDriftError("local runtime changed before native invocation")
    writes = _roots(writable)
    reads = _roots([*map(Path, SYSTEM_ROOTS), *tool_roots(), *readable, *writes])
    # Metadata traversal does not permit listing or reading sibling directories.
    ancestors = sorted({parent for root in reads for parent in root.parents}, key=str)

    def quote(path: Path) -> str:
        return json.dumps(str(path), ensure_ascii=True)

    def path_filter(path: Path) -> str:
        return f"({'literal' if path.is_file() else 'subpath'} {quote(path)})"

    if platform.system() == "Darwin":
        filters = [f"(require-not {path_filter(path)})" for path in reads]
        data = "\n".join([*filters, '(require-not (literal "/"))'])
        metadata = "\n".join(
            [*filters, *[f"(require-not (literal {quote(path)}))" for path in ancestors]]
        )
        write_filters = "\n".join(f"(subpath {quote(path)})" for path in writes)
        profile = f"""(version 1)
(allow default)
(deny file-read-data file-read-xattr (require-all (subpath "/") {data}))
(deny file-read-metadata (require-all (subpath "/") {metadata}))
(deny file-write*)
(allow file-write* {write_filters} (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty") (literal "/dev/dtracehelper"))
(deny process-info*)
(allow process-info* (target self))
(deny signal)
(allow signal (target self))
(allow signal (target pgrp))
"""
        wrapped = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    else:
        wrapped = [
            str(identity["backend"]),
            "--unshare-all",
            "--share-net",
            "--die-with-parent",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        mounted: list[Path] = [Path("/proc"), Path("/dev")]
        for root in sorted(reads, key=lambda path: len(path.parts)):
            if any(root.is_relative_to(parent) for parent in mounted):
                continue
            wrapped.extend(["--ro-bind", str(root), str(root)])
            mounted.append(root)
        for root in writes:
            wrapped.extend(["--bind", str(root), str(root)])
        wrapped.extend(["--", *argv])
    return wrapped, {
        **identity,
        "policy_sha256": hashlib.sha256(json.dumps(wrapped[: -len(argv)]).encode()).hexdigest(),
        "readable": list(map(str, reads)),
        "writable": list(map(str, writes)),
    }


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    readable: Iterable[Path] = (),
    writable: Iterable[Path] = (),
    receipt: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a trusted adapter and all submitted-code descendants in one boundary."""
    with tempfile.TemporaryDirectory(prefix="pluginbench-runtime-") as temporary:
        private = Path(temporary)
        home, tmp = private / "home", private / "tmp"
        home.mkdir()
        tmp.mkdir()
        arguments = [Path(arg) for arg in argv if arg.startswith("/") and Path(arg).exists()]
        package = Path(__file__).parent
        wrapped, filesystem = command(
            argv,
            writable=[cwd, private, *writable],
            readable=[*readable, *arguments, package.parent],
        )
        if receipt is not None:
            receipt.write_text(json.dumps(filesystem, indent=2) + "\n")
        return run_command(
            wrapped,
            cwd=cwd,
            env={
                **env,
                "HOME": str(home),
                "TMPDIR": str(tmp),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "PYTHONPATH": str(package.parent),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            timeout=timeout,
        )
