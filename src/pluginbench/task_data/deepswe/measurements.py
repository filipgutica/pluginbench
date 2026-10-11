"""Prepare only the selected calibrated DeepSWE measurements."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from pluginbench.collectors import Collector
from pluginbench.task_data.deepswe.verify import dependency_digest

ROOT = Path(__file__).resolve().parent
PARSER = ROOT.parents[1] / "fallow_parse.mjs"
MEASUREMENT_NAMES = frozenset(
    {
        "tokens",
        "lines",
        "cyclomatic",
        "cognitive",
        "dead_code",
        "coverage",
        "test_overlap",
        "review",
    }
)
PROFILE_VERSION = "deepswe-maintainability-v1"
OBSERVER_NAMES = {
    "happy-dom-deterministic-intersectionobserver": "happy-vitest-4.0.16",
    "true-myth-iterable-collection-combinators": "true-myth-istanbul-3.2.4",
    "happy-dom-abort-pending-body-reads": "happy-vitest-4.0.16",
    "testem-per-launcher-reports": "testem-istanbul-6.0.3",
}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def observer_cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pluginbench/observers"


def safe_cache_destination(*, root: Path, destination: Path) -> bool:
    """Reject descendant redirects while allowing an explicitly resolved cache root."""
    return destination.is_relative_to(root) and destination.resolve() == destination


def prepare_observer(name: str, cache: Path) -> dict[str, str]:
    """Reuse only a cache with the exact declared lock, manifest, and installed bytes."""
    source = ROOT / "observers" / name
    declared = {
        file: (source / file).read_bytes() for file in ("package.json", "package-lock.json")
    }
    lock_digest = hashlib.sha256(declared["package-lock.json"]).hexdigest()
    cache_root = cache.expanduser().resolve()
    destination = cache_root / name / lock_digest
    if not safe_cache_destination(
        root=cache_root, destination=destination
    ) or not safe_cache_destination(root=cache_root, destination=destination / "node_modules"):
        raise ValueError("observer cache descendants must not be symlinks or escape the cache root")
    receipt = destination / "receipt.json"
    if destination.exists():
        if any((destination / file).read_bytes() != content for file, content in declared.items()):
            raise ValueError(f"observer cache manifest or lock changed: {destination}")
        expected = json.loads(receipt.read_text())
        if (
            expected["lock_sha256"] != lock_digest
            or not (destination / "node_modules").is_dir()
            or dependency_digest(destination / "node_modules") != expected["sha256"]
        ):
            raise ValueError(f"observer dependency cache changed: {destination}")
    else:
        destination.mkdir(parents=True)
        try:
            for file, content in declared.items():
                (destination / file).write_bytes(content)
            subprocess.run(
                ["corepack", "npm@10.9.2", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=destination,
                env={**os.environ, "npm_config_userconfig": "/dev/null"},
                check=True,
            )
            expected = {
                "lock_sha256": lock_digest,
                "sha256": dependency_digest(destination / "node_modules"),
            }
            write_json(receipt, expected)
        except Exception:
            # Clean up only the directory we created, never a redirected external tree.
            if safe_cache_destination(root=cache_root, destination=destination):
                shutil.rmtree(destination)
            raise
    return {"path": str((destination / "node_modules").resolve()), "sha256": expected["sha256"]}


def validate_task_dependencies(
    *, task: str, prepared: Path, payload: dict[str, object], config: dict[str, object]
) -> None:
    dependency_path = payload["dependencies"]
    if not isinstance(dependency_path, str):
        raise ValueError("prepared dependency path must be a string")
    dependencies = Path(dependency_path)
    if dependency_digest(dependencies) != payload["dependency_sha256"]:
        raise ValueError("prepared task dependency cache changed")
    framework = "mocha" if task == "testem-per-launcher-reports" else "vitest"
    version = json.loads((dependencies / framework / "package.json").read_text())["version"]
    if version != config["framework_version"]:
        raise ValueError("pinned task framework version mismatch")
    if task == "ofetch-per-origin-circuit-breaker":
        provider = json.loads((dependencies / "@vitest/coverage-v8/package.json").read_text())
        lock = yaml.safe_load((prepared / "fixtures" / task / "pnpm-lock.yaml").read_text())
        locked = lock["importers"]["."]["devDependencies"]
        if provider["version"] != config["version"] or any(
            locked[name]["version"].split("(")[0] != config["version"]
            for name in ("vitest", "@vitest/coverage-v8")
        ):
            raise ValueError("ofetch requires its locked Vitest/V8 4.0.5 task dependencies")


def selected_measurements(selection: Iterable[str] | None) -> frozenset[str]:
    selected = MEASUREMENT_NAMES if selection is None else frozenset(selection)
    unknown = selected - MEASUREMENT_NAMES
    if unknown:
        raise ValueError("unsupported measurements: " + ", ".join(sorted(unknown)))
    return selected


def measurement_metadata(task: str, selection: Iterable[str] | None = None) -> dict[str, str]:
    preset = json.loads((ROOT / "measurement-presets.json").read_text())[task]
    coverage = preset["coverage"]
    return {
        "measurement_profile_version": PROFILE_VERSION,
        "source_scope": json.dumps(preset.get("source_scope", coverage["source_scope"])),
        "test_scope": json.dumps(coverage["test_scope"]),
        "selected_measurements": json.dumps(sorted(selected_measurements(selection))),
    }


def prepare_measurements(
    *,
    prepared: Path,
    task: str,
    payload: dict[str, object],
    verifier: Path,
    selection: Iterable[str] | None = None,
    observer_cache: Path | None = None,
) -> list[Collector]:
    prepared = prepared.resolve()
    selected = selected_measurements(selection)
    need_fallow = bool(selected & {"cyclomatic", "cognitive", "dead_code"})
    need_coverage = bool(selected & {"coverage", "test_overlap"})
    if need_fallow:
        fallow_version = subprocess.check_output(["fallow", "--version"], text=True).strip()
        if fallow_version != "fallow 3.18.0":
            raise ValueError(f"expected Fallow 3.18.0, observed {fallow_version}")
    preset = json.loads((ROOT / "measurement-presets.json").read_text())[task]
    coverage = preset["coverage"]
    coverage["measure_test_overlap"] = "test_overlap" in selected
    if need_coverage:
        validate_task_dependencies(task=task, prepared=prepared, payload=payload, config=coverage)
    cache = observer_cache if observer_cache is not None else observer_cache_root()
    fixtures = (prepared / "fixtures").resolve()
    if (need_fallow or need_coverage) and cache.expanduser().resolve().is_relative_to(fixtures):
        raise ValueError("observer caches must be outside task fixtures")
    observers = []
    parser = None
    if need_fallow:
        parser = prepare_observer("oxc-parser-0.126.0", cache)
        observers.append(parser)
    if need_coverage and task in OBSERVER_NAMES:
        coverage["observer"] = prepare_observer(OBSERVER_NAMES[task], cache)
        observers.append(coverage["observer"])
    if any(Path(observer["path"]).resolve().is_relative_to(fixtures) for observer in observers):
        raise ValueError("resolved observer caches must be outside task fixtures")
    configs = prepared / "measurements"
    if need_fallow or need_coverage:
        configs.mkdir(exist_ok=True)
    python = str(Path(sys.executable).absolute())
    collectors = []
    if need_coverage:
        coverage_config = configs / "coverage.json"
        write_json(coverage_config, coverage)
        collectors.append(
            Collector(
                kind="coverage",
                failure_protocol="coverage-exit-v1",
                config=coverage_config,
                scope=coverage["source_scope"],
                command=[
                    python,
                    str(ROOT / "coverage.py"),
                    str(verifier.absolute()),
                    "{output}",
                    "--config",
                    str(coverage_config),
                    "--before",
                    "{before}",
                ],
                tool_version=coverage["version"],
                timeout_seconds=7200,
            )
        )
    if need_fallow:
        assert parser is not None
        fallow_config = configs / "fallow.json"
        write_json(fallow_config, preset["fallow"])
        collectors.append(
            Collector(
                kind="fallow",
                config=fallow_config,
                scope=preset.get("source_scope", coverage["source_scope"]),
                syntax_command=[
                    "node",
                    str(PARSER),
                    str(Path(parser["path"]).parent),
                    "{snapshot}",
                    "{inputs}",
                    "{output}",
                    parser["sha256"],
                ],
                tool_version="3.18.0",
                timeout_seconds=7200,
            )
        )
    definitions = [collector.model_dump(mode="json") for collector in collectors]
    write_json(prepared / "measurements.json", {task: definitions})
    provenance_path = prepared / "provenance.json"
    provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else {}
    provenance.update(measurement_metadata(task, selected))
    write_json(provenance_path, provenance)
    return collectors


def verifier_payload(verifier: Path) -> dict[str, object]:
    """Read embedded assets without importing or executing the generated verifier."""
    for statement in ast.parse(verifier.read_text()).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PAYLOAD" for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, dict):
                return value
    raise ValueError("generated verifier has no embedded payload")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--measurement", choices=sorted(MEASUREMENT_NAMES), action="append")
    parser.add_argument("--observer-cache", type=Path)
    args = parser.parse_args()
    catalog = json.loads((args.prepared / "tasks.json").read_text())
    if len(catalog["tasks"]) != 1:
        raise ValueError("real-task preparation requires a single-task catalog")
    task = catalog["tasks"][0]
    verifier = args.prepared / task["verifier"]["script"]
    prepare_measurements(
        prepared=args.prepared,
        task=task["id"],
        payload=verifier_payload(verifier),
        verifier=verifier,
        selection=args.measurement,
        observer_cache=args.observer_cache,
    )
    print(args.prepared / "measurements.json")


if __name__ == "__main__":
    main()
