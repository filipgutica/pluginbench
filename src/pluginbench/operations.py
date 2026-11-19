"""The operations a systemd scope may run (`invocation_runtime.HANDLERS`), and their arguments.

Each handler validates its JSON arguments with its model, calls the existing implementation, and
returns a JSON value. A caller builds the same model and passes it to `run`, which sends
`model_dump(mode="json")`, so both sides share one contract. Paths must be absolute: a handler
runs in its operation's runtime directory, not in the caller's working directory. A
preflight reports its progress (`invocation_runtime.report`).

Every model call, Harbor trial and container PluginBench starts runs through `run`, each with a
finite budget that leaves its implementation's own timeouts room to end it first.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict

from pluginbench import invocation_runtime
from pluginbench.benchmark import Count, Digest
from pluginbench.collectors import Collector
from pluginbench.evaluation import Evaluation
from pluginbench.reference_mutation import Contract

# What an operation has beyond the timeout its implementation applies to its main command:
# after a Harbor worker, its export and cleanup; after a reviewer, its trace and receipts;
# around a collector's command, its container's setup and export.
AFTER_WORKER = 600
AFTER_REVIEW = 120
AROUND_COLLECTOR = 600
# A requalification runs nop and oracle, one after the other.
REFERENCE_AGENTS = 2
# A task's measurement preparation: observer installs and profile setup, which apply no timeout
# of their own.
MEASUREMENT_PREPARATION = 1800
# The images a task's preparation resolves: its base, its grader, and the grader with its tests
# bundled in. Each is pulled or built, within the timeouts `harbor_backend` gives a pull
# (`_inspect_image`) and a build (`_build_native_image`).
PREPARED_IMAGES = 3
IMAGE_PULL = 600
IMAGE_BUILD = 1800
# What one measurement container call (`harbor_backend.run_native_command`) may take beyond its
# command's own timeout, every Docker step it can make at its own cap, one after another: the
# runtime check; create and start; the tool's directory, copy and probe; the workdir clearing,
# snapshot copy and the export of it; the stale report's removal; stop and the export after;
# the report copy; and cleanup. tests/test_operations.py recomputes it from that file.
MEASUREMENT_CALL_OVERHEAD = 3660


def _absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{path} is not an absolute path")
    return path


AbsolutePath = Annotated[Path, AfterValidator(_absolute)]


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Coding(_Arguments):
    """`harbor_backend.invoke_native`: one coding attempt with its native verifier."""

    area: AbsolutePath
    task_dir: AbsolutePath
    entry: dict[str, Any]
    protocol: dict[str, Any]
    prompt: str
    timeout: int


class Grading(_Arguments):
    """`harbor_backend.regrade_native`: the native verifier replayed over saved artifacts."""

    artifacts: AbsolutePath
    task_dir: AbsolutePath
    identity: dict[str, Any]
    directory: AbsolutePath
    artifact_hashes: dict[str, str]


class Review(_Arguments):
    """`codex.invoke`: one reviewer call, whichever harness the protocol selects."""

    area: AbsolutePath
    entry: dict[str, Any]
    protocol: dict[str, Any]
    prompt: str
    timeout: int
    output_schema: AbsolutePath | None = None
    artifacts: AbsolutePath | None = None


def _absolute_paths(value: Any) -> Any:
    """Refuse a relative path anywhere in `value`, a validated model, or its lists and maps."""
    if isinstance(value, Path):
        _absolute(value)
    elif isinstance(value, BaseModel):
        for item in value.__dict__.values():
            _absolute_paths(item)
    elif isinstance(value, dict):
        for item in value.values():
            _absolute_paths(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _absolute_paths(item)
    return value


class Collect(_Arguments):
    """`collectors._run_collector`: one collector over one prepared source tree."""

    root: AbsolutePath
    collector: Annotated[Collector, AfterValidator(_absolute_paths)]
    destination: AbsolutePath
    baseline: AbsolutePath | None = None


class Preflight(_Arguments):
    """`evaluation.check_evaluation` or `prepare_evaluation`, as `mode` selects, for a resolved
    evaluation (`evaluation.load_evaluation`): every path in it must be absolute."""

    mode: Literal["check", "prepare"]
    config: Annotated[Evaluation, AfterValidator(_absolute_paths)]
    output: AbsolutePath


class Requalify(_Arguments):
    """`harbor_backend.run_reference_agents`: a prepared task's nop and oracle run again, as a
    resume's source-only grading asks. The preflight handler runs it."""

    mode: Literal["requalify"] = "requalify"
    identity: dict[str, Any]
    destination: AbsolutePath


class Reference(_Arguments):
    """`reference_mutation.measure_reference`: a task's reference measured three times into
    its `mutation` and `mutation_time` receipts. `config` is the task's prepared mutation
    profile, whose frozen Docker runtime the operation records. Unlike a submission's collect,
    each repeat needs a declared limit: the operation's budget derives from it."""

    before: AbsolutePath
    after: AbsolutePath
    config: dict[str, Any]
    contract: Contract
    pins: dict[str, Digest]
    destination: AbsolutePath
    attempt_ms: Count


def run(
    operation: str,
    arguments: _Arguments,
    *,
    timeout: float,
    docker: dict[str, object] | None,
    invocation: str | None = None,
    started: Callable[[int], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Any:
    """Run `operation` with `arguments` in its own scope (`invocation_runtime.run_operation`).

    `invocation` is its ledger call's ID when it has one; any other operation gets a new ID.
    `docker` is the runtime its containers start under, or None for one that starts none: it
    can create containers on that daemon only, each labelled with the invocation. `progress`
    gets each line the handler reports.
    """
    spec = invocation_runtime.InvocationSpec(
        invocation or uuid4().hex,
        None
        if docker is None
        else {key: docker[key] for key in ("docker_cli", "docker_engine") if key in docker},
    )
    request = invocation_runtime.OperationRequest(
        operation, json.loads(arguments.model_dump_json()), timeout, spec
    )
    return invocation_runtime.run_operation(request, started=started, progress=progress)


def coding_budget(task_dir: Path, agent_seconds: int) -> int:
    """A coding operation's whole budget: its Harbor worker's, then its export and cleanup."""
    from pluginbench import harbor_backend

    return harbor_backend._native_worker_budget(task_dir=task_dir, agent_seconds=agent_seconds) + (
        AFTER_WORKER
    )


def grading_budget(task_dir: Path) -> int:
    """A verifier replay's whole budget, as `coding_budget` with no agent."""
    return coding_budget(task_dir, 0)


def requalify_budget(task_dir: Path) -> int:
    """Nop and oracle, each with its Harbor worker's budget and its export and cleanup."""
    from pluginbench import harbor_backend

    return REFERENCE_AGENTS * coding_budget(task_dir, harbor_backend._REFERENCE_AGENT_TIMEOUT)


def reference_budget(attempt_ms: int) -> int:
    """A reference measurement's whole budget. Each repeat gets its attempt limit, then the
    second its last command's timeout is rounded up to, then one launcher call's overhead:
    a call starts only while the limit has time left, and the lanes' last calls run at once."""
    from pluginbench.suite_qualification import REFERENCE_REPEATS

    return REFERENCE_REPEATS * (math.ceil(attempt_ms / 1000) + 1 + MEASUREMENT_CALL_OVERHEAD)


def review_budget(timeout: int) -> int:
    """A reviewer call's whole budget: its own timeout, then its trace and receipts."""
    return timeout + AFTER_REVIEW


def collect_budget(collector: Collector) -> int:
    """One collector's whole budget. Mutation runs its suite for two baselines, a type check and
    two source-only baselines, then up to a check and four runs for each sampled mutant, each
    bounded by its mutant timeout; P7 replaces this coarse bound with its admitted one."""
    if collector.kind != "mutation":
        return collector.timeout_seconds + AROUND_COLLECTOR
    config = json.loads(collector.config.read_text())
    return _mutation_budget(int(config["coverage"]["timeout_seconds"]))


def _mutation_budget(suite: int) -> int:
    """A mutation collect's budget, given its coverage suite's timeout (see `collect_budget`)."""
    from pluginbench import mutation

    runs = 5 + 5 * mutation.SAMPLE_SIZE
    return runs * (mutation.TIMEOUT_FACTOR * suite + mutation.TIMEOUT_GRACE_SECONDS) + (
        AROUND_COLLECTOR
    )


def readiness_budget(task_dir: Path, config: Evaluation) -> int:
    """A task's measurement preparation, then every readiness repeat of each collector its
    selected measures can use, each at its whole collect budget. A preflight's collects run
    in its own scope (`collectors._contained`), so its budget holds them."""
    from pluginbench import measurement_profiles as profiles

    selected = profiles.selected_measurements(config.measurements)
    coverage = profiles.coverage_settings(task_dir, config.measurement_settings)
    suite = coverage.timeout_seconds if coverage is not None else 0
    collects = 0
    if selected & profiles.FALLOW_MEASUREMENTS:  # Analysis or Fallow, never both.
        collects += profiles.OBSERVER_TIMEOUT + AROUND_COLLECTOR
    if "coverage" in selected and coverage is not None:
        collects += suite + AROUND_COLLECTOR
    if "mutation" in selected and coverage is not None:
        collects += _mutation_budget(suite)
    return MEASUREMENT_PREPARATION + profiles.READINESS_REPEATS * collects


def preflight_budget(config: Evaluation) -> int:
    """A check or preparation's whole budget, summed over its tasks and never divided by
    parallelism: each task's image pulls and builds, its agent installation, its measurement
    preparation and readiness, then its nop and oracle, each with its export and cleanup."""
    from pluginbench import harbor_backend

    source = config.tasks.resolve()
    suite = source.parent if source.name == "tasks" else source
    total = 0
    for task in config.tasks.task_ids:
        task_dir = suite / "tasks" / task
        total += PREPARED_IMAGES * (IMAGE_PULL + IMAGE_BUILD)
        total += harbor_backend._native_worker_budget(task_dir=task_dir, prepare=True)
        total += readiness_budget(task_dir, config)
        total += requalify_budget(task_dir) + AFTER_WORKER
    return total


# Each handler passes its model's fields, unchanged, as the implementation's keyword arguments.


def coding(arguments: dict[str, Any]) -> dict[str, object]:
    from pluginbench import harbor_backend

    return harbor_backend.invoke_native(**dict(Coding.model_validate(arguments)))


def grading(arguments: dict[str, Any]) -> dict[str, object]:
    from pluginbench import harbor_backend

    return harbor_backend.regrade_native(**dict(Grading.model_validate(arguments)))


def review(arguments: dict[str, Any]) -> dict[str, Any]:
    from pluginbench import codex

    return codex.invoke(**dict(Review.model_validate(arguments)))


def collect(arguments: dict[str, Any]) -> list[object]:
    """The collector's (report, tool version, tool name), as a list."""
    from pluginbench import collectors

    return list(collectors._run_collector(**dict(Collect.model_validate(arguments))))


def reference(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """The `mutation` and `mutation_time` receipts, as JSON, once both are written."""
    from pluginbench import reference_mutation

    receipts = reference_mutation.measure_reference(**dict(Reference.model_validate(arguments)))
    return [receipt.model_dump(mode="json") for receipt in receipts]


def preflight(arguments: dict[str, Any]) -> object:
    """The check's report, the prepared run's path as a string, or the reference agents' rows."""
    from pluginbench import evaluation, harbor_backend

    if arguments.get("mode") == "requalify":
        again = Requalify.model_validate(arguments)
        return harbor_backend.run_reference_agents(again.identity, again.destination)
    request = Preflight.model_validate(arguments)
    progress = invocation_runtime.report
    if request.mode == "check":
        return evaluation.check_evaluation(request.config, request.output, progress=progress)
    return str(evaluation.prepare_evaluation(request.config, request.output, progress=progress))
