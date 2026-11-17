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
CONFIG = ROOT / "coverage-config.ini"
DATA = ROOT / "data/.coverage"
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
    arguments = sys.argv[1:]
    reporting = arguments == ["json"]
    if reporting or arguments == ["--pluginbench-verify"]:
        os.environ.clear()
    else:
        for name in tuple(os.environ):
            if name.startswith("COVERAGE_"):
                del os.environ[name]
    os.environ["PLUGINBENCH_COVERAGE_DATA"] = str(DATA)
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
    origin = {"executable": str(Path(sys.executable).resolve()), "workdir": str(Path.cwd().resolve()),
              "module": checked_origin(coverage.__file__), "dependencies": modules,
              "config": checked_origin(str(CONFIG)), "data_file": str(DATA),
              "source_files": checked_origin(str(ROOT / "source-files.json"))}
    if sys.argv[1:] == ["--pluginbench-verify"]:
        print(json.dumps(origin, sort_keys=True))
        return 0
    if not reporting and (
        len(arguments) < 2 or arguments[0] not in {"run", "run-module"}
        or arguments[1].startswith("-")
    ):
        raise SystemExit("PluginBench coverage: use run <script>, run-module <module>, or json")
    if reporting:
        if set(DATA.parent.iterdir()) != {DATA}:
            raise SystemExit("PluginBench coverage: unexpected data files; combining is forbidden")
        # config_file is the API equivalent of the pinned --rcfile used for run.
        # The CLI's reporting path inserts cwd on sys.path, so use the reporting API.
        measured = coverage.Coverage(config_file=str(CONFIG), data_file=str(DATA))
        measured.load()
        source_files = json.loads((ROOT / "source-files.json").read_text())
        if source_files == []:
            raise SystemExit("PluginBench coverage: no files in the profile source scope")
        # include= treats filenames as globs. Select literal paths already in the database;
        # this does not invent data for unloaded files coverage.py itself could not discover.
        files: list[str] | None = None
        if source_files is not None:
            allowed = {Path(name).resolve() for name in source_files}
            files = [name for name in measured.get_data().measured_files() if Path(name).resolve() in allowed]
            if not files:
                raise SystemExit("PluginBench coverage: no measured files in the profile source scope")
        measured.json_report(morfs=files, outfile=str(Path.cwd() / "coverage.json"))
        return 0
    # Each fresh measurement container permits one collection. No append, parallel
    # databases, or combine operation is exposed, and old data cannot be selected.
    DATA.parent.mkdir()
    TOOL_PACKAGES.intersection_update(sys.modules)
    site.main()
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] in TOOL_PACKAGES and module is not None:
            checked_origin(module.__file__)
    task_paths = [os.getcwd(), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    sys.path[:] = [str(ROOT), *task_paths, *(item for item in sys.path if item != str(ROOT))]
    program = (["-m"] if arguments[0] == "run-module" else ["--"]) + arguments[1:]
    result: object = command.main([
        "run", "--rcfile=" + str(CONFIG), "--data-file=" + str(DATA), *program,
    ])
    if not isinstance(result, int):
        raise RuntimeError("coverage CLI did not return an exit code")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
