"""Reference mutation evidence: the `mutation` and `mutation_time` qualification receipts.

`measure_reference` runs the bounded mutation engine on a task's frozen starting tree and its
verified oracle change, three independent times, each into its own directory. Every repeat's
report is validated against its run receipts (`mutation.mutation_metrics`), and its sample is
checked against its candidates here, because sampling lives outside the qualification rules'
import boundary. The raw facts and clock readings of all three repeats then go into one
receipt per gate, pinning exactly the task's content, its observer profile and the common
observer contract. `suite_qualification` re-derives every verdict from those facts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pluginbench import mutation
from pluginbench.suite_qualification import REFERENCE_REPEATS, Receipt

PRODUCER = "reference-mutation-v1"
# The pins a reference mutation receipt binds: never the coding contract or the time limit.
PINS = ("content_sha256", "observer_profile_sha256", "observer_contract_sha256")
GATES = ("mutation", "mutation_time")


@dataclass(frozen=True)
class Contract:
    """The mutation part of the common observer contract.

    `headroom_percent` is how much the task's mutation time limit must exceed the projected
    full-M reference time: 150 means one and a half times.
    """

    headroom_percent: int
    sample_size: int = mutation.SAMPLE_SIZE
    lanes: int = mutation.LANES

    def facts(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "lanes": self.lanes,
            "resources": {
                "cpus": mutation.MEASUREMENT_CPUS, "memory_mb": mutation.MEASUREMENT_MEMORY_MB,
            },
            "baseline_factor": mutation.BASELINE_FACTOR,
            "site_factor": mutation.SITE_FACTOR,
            "first_run_factor": mutation.FIRST_RUN_FACTOR,
            "headroom_percent": self.headroom_percent,
        }


def measure_reference(
    *,
    before: Path,
    after: Path,
    config: dict[str, object],
    contract: Contract,
    pins: Mapping[str, str],
    destination: Path,
    baseline_ms: int | None = None,
    attempt_ms: int | None = None,
    execute: mutation.Executor | None = None,
    clock: mutation.Clock | None = None,
) -> tuple[Receipt, Receipt]:
    """Measure the reference three times and write its `mutation` and `mutation_time` receipts.

    `before` is the task's frozen starting tree and `after` that tree with the verified oracle
    change. `baseline_ms` is the calibration ceiling for each baseline run (the profile's
    coverage timeout when None), and `attempt_ms` bounds each repeat. A repeat that hits
    infrastructure, including its own limit, is recorded as censored; an integrity violation
    raises.
    """
    if sorted(pins) != sorted(PINS):
        raise ValueError(f"reference mutation receipts pin exactly {', '.join(PINS)}")
    limits = mutation.Limits(
        sample_size=contract.sample_size, lanes=contract.lanes, baseline_ms=baseline_ms,
        attempt_ms=attempt_ms,
    )
    measured = mutation.measurement_identity(_object(config["native_task_identity"]))
    repeats: list[dict[str, object]] = []
    candidates: list[object] = []
    for index in range(REFERENCE_REPEATS):
        directory = destination / f"repeat-{index}"
        directory.mkdir(parents=True)  # A repeat never reuses another's evidence.
        report = mutation.collect_mutation(
            after, before, config, directory, execute=execute, limits=limits, clock=clock
        )
        data = json.dumps(report, sort_keys=True).encode()
        (directory / "report.json").write_bytes(data)
        # Validates every run against its receipt, and names the files discovery could not use.
        metrics = mutation.mutation_metrics(report, directory, config=config, snapshot=after)
        _check_sample(report, contract)
        if index and report["candidates"] != candidates:
            raise ValueError("reference repeats found different mutation candidates")
        candidates = _list(report["candidates"])
        repeat = _repeat(report, directory, measured, hashlib.sha256(data).hexdigest())
        repeat.update(
            unparsed_files=sorted(_object(metrics["unparsed_files"])),
            unsupported_files=sorted(str(path) for path in _list(metrics["unsupported_files"])),
        )
        repeats.append(repeat)
    facts = {"contract": contract.facts(), "repeats": repeats}
    receipts = tuple(
        Receipt(
            task=pins["content_sha256"], gate=gate, pins=dict(pins), producer=PRODUCER,
            facts=facts,
        )
        for gate in GATES
    )
    for receipt in receipts:
        (destination / f"{receipt.gate}.receipt.json").write_text(receipt.model_dump_json())
    return receipts[0], receipts[1]


def _check_sample(report: dict[str, object], contract: Contract) -> None:
    """The repeat sampled M mutants, in the contract's lanes, as the preregistered order does."""
    candidates = mutation._sites(report["candidates"])
    selected = mutation._sites(report["selected"])
    if (
        report["sample_size"] != contract.sample_size
        or report["lanes"] != contract.lanes
        or selected != mutation.sample(candidates, str(report["after_digest"]), contract.sample_size)
    ):
        raise ValueError("a reference repeat's sample is not the contract's preregistered sample")


def _repeat(
    report: dict[str, object], directory: Path, measured: dict[str, object], digest: str
) -> dict[str, object]:
    """One validated repeat's raw facts: inputs, image, clocks, and every run's readings."""
    clocks = _object(report["clocks"])
    selected = mutation._sites(report["selected"])
    check_baseline = report["check_baseline"]
    tsc_stdout = (
        _stdout(directory, _object(check_baseline)) if check_baseline is not None else None
    )
    failure = report["infrastructure_failure"]
    sites = []
    for item in _list(report["mutants"]):
        record = _object(item)
        site = selected[int(str(record["site"]))]
        check = record["check"]
        sites.append({
            "site": record["site"],
            "dispatched_ms": record["dispatched_ms"],
            "settled_ms": record["settled_ms"],
            "check": None if check is None else _check(
                _object(check), site, directory, tsc_stdout
            ),
            "runs": [_call(_object(run)) for run in _list(record["runs"])],
        })
    return {
        "run_id": uuid4().hex,
        "report_sha256": digest,
        "before_digest": report["before_digest"],
        "after_digest": report["after_digest"],
        "image_id": _object(measured["image_identity"])["image_id"],
        "resources": measured["resources"],
        "timing_boundary": clocks["timing_boundary"],
        "candidate_count": len(_list(report["candidates"])),
        "selected": [f"{s.path}:{s.line}:{s.column}:{s.operator}" for s in selected],
        "baseline_ms": clocks["baseline_ms"],
        "attempt_ms": clocks["attempt_ms"],
        "baseline": [_call(_object(run)) for run in _list(report["baseline"])],
        "check_baseline": None if check_baseline is None else {
            **_call(_object(check_baseline)),
            "kind": "tsc",
            "diagnostics": sum(mutation.tsc_errors(tsc_stdout or "").values()),
        },
        "sites": sites,
        "required_settled_ms": report["required_settled_ms"],
        "infrastructure_failure": None if failure is None else _object(failure)["cause"],
    }


_RESULT = ("elapsed_ms", "exit_code", "timed_out", "suite_status")


def _call(record: dict[str, object]) -> dict[str, object]:
    """A run's start and, if it started, its exit: no derived bound or outcome."""
    keys = ("started_ms", "dispatched", *(_RESULT if record["dispatched"] else ()))
    return {key: record[key] for key in keys}


def _check(
    record: dict[str, object], site: mutation.Site, directory: Path, baseline: str | None
) -> dict[str, object]:
    """A mutant's check, with tsc's diagnostic counts against the unmutated tree's."""
    kind = "go" if mutation._check_command(site.path) == mutation.GO_CHECK else "tsc"
    facts = {**_call(record), "kind": kind}
    if kind == "tsc" and record["dispatched"]:
        errors = mutation.tsc_errors(_stdout(directory, record), site)
        known = mutation.tsc_errors(baseline or "", site)
        facts.update(
            diagnostics=sum(errors.values()), new_diagnostics=sum((errors - known).values())
        )
    return facts


def _stdout(directory: Path, record: dict[str, object]) -> str:
    return (directory / str(record["receipts"]) / "stdout.txt").read_text()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("reference mutation evidence requires JSON objects")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("reference mutation evidence requires lists")
    return value
