"""Label every container Harbor creates for one trial with its operation's invocation label.

Harbor 0.24.0 starts a trial's containers in two ways, and `labelled` covers both while the
trial runs in this process:
- Compose, for the agent, the separate verifier, and the egress sidecar. Its `-f` files come from
  `DockerEnvironment._docker_compose_paths`. The adapter appends one overlay that gives each of
  their services the label, so the label is there at creation, and nothing a task sets can
  override it.
- `docker container run`, for the egress kernel probe. Its argv comes from
  `DockerEnvironment._engine_cmd`, and the adapter adds the label to every `run` or `create`.

Recovery then finds these containers by the label alone (`invocation_runtime.verify_labelled`),
whatever the frozen suite or the Compose project. If Harbor's shape changes, nothing starts.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from pluginbench.harbor_backend import HARBOR_VERSION
from pluginbench.invocation_runtime import LABEL_KEY, with_label


# Top-level Compose keys that add no service. Any other, such as `include` or `models`, could
# start containers the overlay does not name, so it is refused.
_PLAIN_KEYS = {"name", "version", "services", "networks", "volumes", "configs", "secrets"}
# Service keys that take a definition from elsewhere or run something other than a container.
_FOREIGN_SERVICE_KEYS = {"extends", "provider"}


def _services(path: Path) -> set[str]:
    """The services one Compose file defines, refusing any shape that could define others."""
    document = yaml.safe_load(path.read_text()) or {}
    if not isinstance(document, dict):
        raise ValueError(f"Compose file {path} is not a mapping")
    extra = {key for key in document if not str(key).startswith("x-")} - _PLAIN_KEYS
    services = document.get("services") or {}
    if not isinstance(services, dict) or not all(
        isinstance(service, dict) for service in services.values()
    ):
        raise ValueError(f"Compose file {path} does not map its services")
    foreign = {
        f"{name}.{key}"
        for name, service in services.items()
        for key in _FOREIGN_SERVICE_KEYS & set(service)
    }
    if extra or foreign:
        raise ValueError(
            f"Compose file {path} uses {', '.join(sorted(extra | foreign))}, which could start "
            "containers without the invocation label; nothing started"
        )
    return {str(name) for name in services}


@contextmanager
def labelled(invocation: str) -> Iterator[None]:
    """Label every container Harbor creates in this process until the block ends."""
    docker = importlib.import_module("harbor.environments.docker.docker").DockerEnvironment
    paths = docker.__dict__.get("_docker_compose_paths")
    engine = docker.__dict__.get("_engine_cmd")
    if (
        importlib.metadata.version("harbor") != HARBOR_VERSION
        or not isinstance(paths, property)
        or paths.fget is None
        or not isinstance(engine, classmethod)
    ):
        raise ValueError(
            "Harbor's Docker environment differs from the pinned shape, so its containers "
            "cannot be labelled; nothing started"
        )
    original_paths, original_engine = paths.fget, engine.__func__
    with tempfile.TemporaryDirectory(prefix="pluginbench-labels-") as directory:

        def labelled_paths(self: Any) -> list[Path]:
            # Built from Harbor's own paths each time, never from an earlier overlay.
            original: list[Path] = original_paths(self)
            services: set[str] = set()
            for path in original:
                services.update(_services(Path(path)))
            if not services:
                raise ValueError("Harbor's Compose files name no service to label")
            session = hashlib.sha256(str(self.session_id).encode()).hexdigest()[:16]
            overlay = Path(directory) / f"labels-{session}.yaml"
            overlay.write_text(
                yaml.safe_dump(
                    {
                        "services": {
                            name: {"labels": {LABEL_KEY: invocation}} for name in sorted(services)
                        }
                    }
                )
            )
            return [*original, overlay]

        def labelled_engine(cls: Any, *arguments: str) -> list[str]:
            command: list[str] = original_engine(cls, *with_label(arguments, invocation))
            return command

        docker._docker_compose_paths = property(labelled_paths)
        docker._engine_cmd = classmethod(labelled_engine)
        try:
            yield
        finally:
            docker._docker_compose_paths = paths
            docker._engine_cmd = engine
