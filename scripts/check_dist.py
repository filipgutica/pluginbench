"""Check the built wheel and source distribution in dist/ before they are published.

Run from the repository root after `python -m build`.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def main() -> int:
    [wheel] = Path("dist").glob("pluginbench-*.whl")
    [sdist] = Path("dist").glob("pluginbench-*.tar.gz")
    tracked = subprocess.run(
        ["git", "ls-files", "src/pluginbench"], capture_output=True, text=True, check=True
    ).stdout.split()
    problems = []

    # Collectors, reports, and treatments read these files at run time, so a file the
    # repository tracks and the wheel lacks breaks an installed PluginBench.
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
    problems += [
        f"{wheel.name} lacks {path}" for path in tracked if path.removeprefix("src/") not in shipped
    ]

    with tarfile.open(sdist) as archive:
        members = [name.split("/", 1)[1] for name in archive.getnames() if "/" in name]
    problems += [
        f"{sdist.name} lacks {path}" for path in [*tracked, "LICENSE"] if path not in members
    ]
    problems += [
        f"{sdist.name} holds {name}, which is not needed to build the wheel"
        for name in members
        if name.startswith(("tests/", "docs/", "scripts/", ".github/"))
    ]

    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
