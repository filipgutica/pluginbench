"""Run only the staged coverage distribution, then enable the measured program's imports."""

from __future__ import annotations

import sys

if not sys.flags.isolated or not sys.flags.no_site:
    raise SystemExit("PluginBench coverage launcher requires Python -I -S")

import importlib
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder
import json
import os
from pathlib import Path
import site
from types import ModuleType
from collections.abc import Sequence

ROOT = Path(__file__).resolve().parent
# tomli is coverage's only optional third-party dependency (on Python before 3.11).
TOOL_PACKAGES = {"coverage", "tomli"}


def checked_origin(origin: str | None) -> str:
    if not origin or not Path(origin).resolve().is_relative_to(ROOT):
        raise ImportError(f"coverage import outside staged distribution: {origin}")
    return str(Path(origin).resolve())


class StagedImports(MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname.split(".")[0] not in TOOL_PACKAGES:
            return None
        spec = PathFinder.find_spec(fullname, [str(ROOT)] if path is None else path)
        if spec is None:
            raise ImportError(f"coverage dependency is not staged: {fullname}")
        checked_origin(spec.origin)
        return spec


def main() -> int:
    sys.path.insert(0, str(ROOT))
    # Check every coverage submodule before executing it, including later lazy imports.
    sys.meta_path.insert(0, StagedImports())
    coverage = importlib.import_module("coverage")
    command = importlib.import_module("coverage.cmdline")
    modules = {
        name: checked_origin(module.__file__)
        for name, module in tuple(sys.modules.items())
        if name.split(".")[0] in TOOL_PACKAGES and module is not None
    }
    origin = {"module": checked_origin(coverage.__file__), "dependencies": modules}
    if sys.argv[1:] == ["--pluginbench-verify"]:
        print(json.dumps(origin, sort_keys=True))
        return 0
    # Reserve only dependencies coverage actually imported. An unused optional
    # package name can still belong to the measured program.
    TOOL_PACKAGES.intersection_update(sys.modules)
    # Site packages belong to the task image. Load them while the task's directory and
    # PYTHONPATH are still absent, so a task-local sitecustomize cannot run at startup.
    site.main()
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] in TOOL_PACKAGES and module is not None:
            checked_origin(module.__file__)
    task_paths = [os.getcwd(), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    sys.path[:] = [str(ROOT), *task_paths, *(item for item in sys.path if item != str(ROOT))]
    result: object = command.main()
    if not isinstance(result, int):
        raise RuntimeError("coverage CLI did not return an exit code")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
