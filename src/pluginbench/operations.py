"""The operations a systemd scope may run (`invocation_runtime.HANDLERS`), and their arguments.

Each handler validates its JSON arguments with its model, calls the existing implementation, and
returns a JSON value. A caller builds the same model and sends `model_dump(mode="json")`, so both
sides share one contract. Paths must be absolute: a handler runs in its operation's runtime
directory, not in the caller's working directory. A handler reports no progress yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

from pluginbench.collectors import Collector
from pluginbench.evaluation import Evaluation


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


def preflight(arguments: dict[str, Any]) -> object:
    """The check's report, or the prepared run's path as a string."""
    from pluginbench import evaluation

    request = Preflight.model_validate(arguments)
    if request.mode == "check":
        return evaluation.check_evaluation(request.config, request.output)
    return str(evaluation.prepare_evaluation(request.config, request.output))
