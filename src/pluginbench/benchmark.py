"""Identities of the benchmark's objects: entrants, configurations, suites, cells and slots.

Every ID is derived from the inputs that affect what runs or what is measured, never from
labels, paths or times, so equal work has one ID wherever and whenever it was described.
Callers supply resolved values (commits, exact CLI and model IDs, digests of frozen
contracts); this module reads no files and stores nothing. B6 stores what these IDs key.

A coding slot is keyed by task × entrant × configuration revision × attempt index, so a
suite that adds tasks or attempts keeps every existing slot. Measurements have two kinds,
each with its own contract: observers (per task) and review (per coding harness). Changing
one re-measures or re-reviews saved submissions and never recodes them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

# The hash envelope's version. Changing what a kind's digest binds, or how it is encoded,
# must bump it, so an existing ID is never silently redefined.
HASH_VERSION = 1

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObject = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Harness = Literal["codex", "claude"]
Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
Name = Annotated[str, Field(min_length=1)]


def _identity(kind: str, fields: dict[str, Any]) -> str:
    """The SHA-256 of `fields` under `kind`, in canonical JSON: keys sorted, no whitespace,
    ASCII only. Callers sort set-like lists; order-bearing lists keep their order."""
    envelope = {"kind": kind, "v": HASH_VERSION, "fields": fields}
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vanilla(_Value):
    """The harness with no package: one entrant shared by every package contrast."""

    kind: Literal["vanilla"] = "vanilla"

    @property
    def id(self) -> str:
        return _identity("entrant", {"kind": self.kind})


class Package(_Value):
    """A package release by content: its source commit and the git tree of its root.

    A release label, package name or displayed version is not identity; a label binds to
    one ID (see check_release_binding). What each harness receives from the tree is the
    cell's certification, not the entrant, so one release is one entrant across harnesses.
    """

    kind: Literal["package"] = "package"
    source_commit: GitObject
    source_tree: GitObject

    @property
    def id(self) -> str:
        return _identity(
            "entrant",
            {"kind": self.kind, "source_commit": self.source_commit, "source_tree": self.source_tree},
        )


Entrant = Annotated[Vanilla | Package, Field(discriminator="kind")]


class Configuration(_Value):
    """What codes: a display key (harness, model, effort) and the revision pinned under it.

    `launch_sha256` binds the harness launch contract by value (argv and environment policy,
    capabilities, delivery and isolation, and the launcher version), but not the timeout or
    resources, which each task's coding contract owns. A CLI upgrade keeps the key and
    starts a new revision.
    """

    harness: Harness
    model_id: Name
    effort: Effort
    cli_version: Name
    launch_sha256: Digest

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.harness, self.model_id, self.effort)

    @property
    def revision(self) -> str:
        return _identity("configuration", self.model_dump())


class SuiteTask(_Value):
    """One task as the suite runs and measures it.

    `content_sha256` binds the task's frozen prompt, source, assets, verifier and images.
    `coding_contract_sha256` binds how it is coded and graded: source and submission
    policy, grade contract, timeout and resources. `observer_profile_sha256` binds how its
    observers measure it.
    """

    content_sha256: Digest
    repository_cluster: Name
    coding_contract_sha256: Digest
    observer_profile_sha256: Digest
    mutation_timeout_seconds: PositiveInt

    @property
    def coding_id(self) -> str:
        """The task as coded: what a coding slot binds."""
        return _identity(
            "coding-task",
            {"content_sha256": self.content_sha256,
             "coding_contract_sha256": self.coding_contract_sha256},
        )


class ReviewerRule(_Value):
    """Who reviews the attempts of one coding harness, and under which protocol.

    `protocol_sha256` binds the criteria, packet and selection protocol, review launch and
    sandbox contract, timeout and seed.
    """

    coding_harness: Harness
    reviewer_harness: Harness
    reviewer_model_id: Name
    reviewer_effort: Effort
    reviewer_cli_version: Name
    protocol_sha256: Digest

    @property
    def contract(self) -> str:
        return _identity("review-contract", self.model_dump())


class Suite(_Value):
    """A coding population (tasks, clusters, attempts, seed) and its measurement contracts.

    `observer_contract_sha256` binds what every task's observers share: required and
    diagnostic measures, observer and collector versions, mutation sampling and
    confirmation, and measurement CPU and memory limits.
    """

    tasks: tuple[SuiteTask, ...] = Field(min_length=1)
    attempts: PositiveInt
    seed: int
    observer_contract_sha256: Digest
    reviewers: tuple[ReviewerRule, ...]

    @model_validator(mode="after")
    def _unique(self) -> Suite:
        if len({task.coding_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("the suite lists one task twice")
        if len({rule.coding_harness for rule in self.reviewers}) != len(self.reviewers):
            raise ValueError("the suite has two reviewer rules for one coding harness")
        return self

    def reviewer(self, harness: str) -> ReviewerRule:
        for rule in self.reviewers:
            if rule.coding_harness == harness:
                return rule
        raise ValueError(f"the suite has no reviewer rule for {harness} attempts")

    def observer_contract(self, task: SuiteTask) -> str:
        """The contract one task's observer measurements are made under."""
        return _identity(
            "observer-contract",
            {"observer_contract_sha256": self.observer_contract_sha256,
             "observer_profile_sha256": task.observer_profile_sha256,
             "mutation_timeout_seconds": task.mutation_timeout_seconds},
        )

    @property
    def coding_population_id(self) -> str:
        rows = sorted([task.coding_id, task.repository_cluster] for task in self.tasks)
        return _identity(
            "coding-population", {"tasks": rows, "attempts": self.attempts, "seed": self.seed}
        )

    @property
    def observer_revision(self) -> str:
        rows = sorted([task.coding_id, self.observer_contract(task)] for task in self.tasks)
        return _identity("observer-revision", {"tasks": rows})

    @property
    def id(self) -> str:
        reviews = sorted([rule.coding_harness, rule.contract] for rule in self.reviewers)
        return _identity(
            "suite",
            {"coding_population_id": self.coding_population_id,
             "observer_revision": self.observer_revision, "reviews": reviews},
        )


class Slot(_Value):
    """One prescribed attempt of a cell and the contracts its measurements are made under."""

    coding_task_id: Digest
    entrant_id: Digest
    configuration_revision: Digest
    attempt_index: PositiveInt
    observer_contract: Digest
    review_contract: Digest

    @property
    def id(self) -> str:
        """The coding attempt: equal in every cell and suite that prescribes this work."""
        return _identity(
            "coding-slot",
            {"coding_task_id": self.coding_task_id, "entrant_id": self.entrant_id,
             "configuration_revision": self.configuration_revision,
             "attempt_index": self.attempt_index},
        )

    @property
    def observer_measurement_id(self) -> str:
        return _measurement(self.id, "observer", self.observer_contract)

    @property
    def review_measurement_id(self) -> str:
        return _measurement(self.id, "review", self.review_contract)


def _measurement(attempt_id: str, kind: str, contract: str) -> str:
    return _identity("measurement", {"attempt_id": attempt_id, "kind": kind, "contract": contract})


class Cell(_Value):
    """An entrant coded by one configuration on one suite: every slot that prescribes."""

    entrant: Entrant
    configuration: Configuration
    suite: Suite

    @model_validator(mode="after")
    def _reviewed(self) -> Cell:
        self.suite.reviewer(self.configuration.harness)
        return self

    @property
    def id(self) -> str:
        """Binds only what this cell's work depends on: another harness's reviewer, or a
        configuration added to the suite, leaves it unchanged."""
        return _identity(
            "cell",
            {"entrant_id": self.entrant.id,
             "configuration_revision": self.configuration.revision,
             "coding_population_id": self.suite.coding_population_id,
             "observer_revision": self.suite.observer_revision,
             "review_contract": self.suite.reviewer(self.configuration.harness).contract},
        )

    @property
    def baseline(self) -> Cell:
        """The shared vanilla cell this cell's package is compared with."""
        return self.model_copy(update={"entrant": Vanilla()})

    def slots(self) -> tuple[Slot, ...]:
        """Every attempt the cell prescribes, by task then attempt index, from 1."""
        review = self.suite.reviewer(self.configuration.harness).contract
        tasks = sorted(self.suite.tasks, key=lambda task: task.coding_id)
        return tuple(
            Slot(
                coding_task_id=task.coding_id,
                entrant_id=self.entrant.id,
                configuration_revision=self.configuration.revision,
                attempt_index=index,
                observer_contract=self.suite.observer_contract(task),
                review_contract=review,
            )
            for task in tasks
            for index in range(1, self.suite.attempts + 1)
        )


def check_release_binding(*, existing_id: str | None, proposed_id: str) -> None:
    """Refuse to bind a release label that already names one entrant to another.

    The caller looks up the label's current entrant, if any. A first binding and a replay of
    the same one pass. B6 makes the lookup, this check and the write one transaction.
    """
    if existing_id is not None and existing_id != proposed_id:
        raise ValueError(
            f"this release is already bound to entrant {existing_id}; its content cannot change "
            f"to entrant {proposed_id}. Publish the new content under a new release"
        )
