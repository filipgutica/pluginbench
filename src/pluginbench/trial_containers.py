"""Find the containers a run's Harbor trials left running, and stop them; never remove any.

A killed run leaves its trial's compose project up: the kill skips the cleanup that removes it.
Ownership is proven from the run's own files. Harbor starts each trial's compose project with
`--project-directory` set inside the run's frozen task (evidence/frozen/suite/<i>/task), and
Docker Compose records that directory as the `com.docker.compose.project.working_dir` label.
The trial directory itself is no proof: it lives in a temporary area outside the run.

Removal stays with harbor_backend.cleanup(); this module only lists and stops.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from pluginbench import harbor_backend

_WORKING_DIR = "com.docker.compose.project.working_dir"
# harbor_backend names each trial `pluginbench-<uuid4 hex>`; Harbor adds `__env` for the agent's
# project and `__verifier__<key>` for a separate verifier's.
_TRIAL_PROJECT = re.compile(r"pluginbench-[0-9a-f]{32}__.+")
_FORMAT = (
    "[{{json .ID}},{{json .Names}},"
    '{{json (.Label "com.docker.compose.project")}},'
    f'{{{{json (.Label "{_WORKING_DIR}")}}}}]'
)


def running(evidence: Path, *, expected_runtime: dict[str, object] | None) -> list[dict[str, str]]:
    """Running containers of this run's Harbor trials: id, name and compose project.

    `expected_runtime` pins the Docker CLI and engine, as for every other Docker call of a run;
    None uses the selected Docker context, which is how a coding attempt picks its engine.
    """
    suite = (evidence / "frozen" / "suite").resolve()
    if not suite.is_dir():
        return []  # No task was frozen, so no trial ever started.
    # `docker ps` without --all lists only running containers.
    output = harbor_backend._docker(
        "ps",
        "--no-trunc",
        "--filter",
        "status=running",
        "--filter",
        f"label={_WORKING_DIR}",
        "--format",
        _FORMAT,
        expected_runtime=expected_runtime,
    )
    found = []
    for line in output.splitlines():
        if not line.strip():
            continue
        container, name, project, working_dir = json.loads(line)
        if (
            _TRIAL_PROJECT.fullmatch(project)
            and Path(working_dir).is_absolute()
            and Path(working_dir).resolve().is_relative_to(suite)
        ):
            found.append({"id": container, "name": name, "project": project})
    return found


def stop_left_running(
    evidence: Path,
    *,
    expected_runtime: dict[str, object],
    progress: Callable[[str], None] | None,
) -> None:
    """Stop every running container of this run's trials. Only the run.lock holder calls this.

    The holder is the only process working on this run, so whatever is still running was left
    by an attempt that a kill interrupted.
    """
    found = running(evidence, expected_runtime=expected_runtime)
    for project in dict.fromkeys(container["project"] for container in found):
        ids = [container["id"] for container in found if container["project"] == project]
        try:
            harbor_backend._docker("stop", "--time", "10", *ids, expected_runtime=expected_runtime)
        except ValueError:
            # A surviving Harbor worker may remove its own containers in the meantime; only a
            # container that is still running is a failure.
            still = running(evidence, expected_runtime=expected_runtime)
            if any(container["id"] in ids for container in still):
                raise
            continue
        if progress:
            progress(
                f"Stopped {project} ({len(ids)} container{'s' * (len(ids) != 1)}): left running "
                "by an attempt that a kill interrupted"
            )
