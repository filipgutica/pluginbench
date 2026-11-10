"""Regenerate the saved state that PluginBench 942f12c wrote, for upgrade tests.

Run from the repository root: `python tests/fixtures/legacy-942f12c/generate.py`.
It exports 942f12c with `git archive`, runs `driver.py` against that export with Docker
stubbed, and replaces this directory's generated files. It never touches a real cache.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

COMMIT = "942f12c"
HERE = Path(__file__).resolve().parent
KEEP = {"generate.py", "driver.py", "README.md"}


def _forbidden() -> list[str]:
    values = [str(Path.home()), tempfile.gettempdir(), os.environ.get("TMPDIR", "")]
    return [value for value in values if value and value != "/"]


def main() -> None:
    repository = Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    )
    with tempfile.TemporaryDirectory(prefix="pluginbench-legacy-") as temporary:
        root = Path(temporary)
        archive = root / "export.tar"
        with archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(repository), "archive", COMMIT, "src", "tests"],
                stdout=stream,
                check=True,
            )
        export = root / "export"
        with tarfile.open(archive) as bundle:
            bundle.extractall(export, filter="data")
        output, work = root / "output", root / "work"
        output.mkdir()
        work.mkdir()
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "HOME"}}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["XDG_CACHE_HOME"] = str(root / "xdg-cache")
        subprocess.run(
            [sys.executable, "-I", str(HERE / "driver.py"), str(export), str(output), str(work)],
            env=env,
            check=True,
        )
        leaks = [
            f"{path.relative_to(output)} contains {value}"
            for path in output.rglob("*")
            if path.is_file()
            for value in [*_forbidden(), str(root)]
            if value.encode() in path.read_bytes()
        ]
        if leaks:
            raise SystemExit("generated fixtures hold local paths:\n" + "\n".join(leaks))
        for path in HERE.iterdir():
            if path.name not in KEEP:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for path in output.iterdir():
            shutil.move(str(path), HERE / path.name)


if __name__ == "__main__":
    main()
