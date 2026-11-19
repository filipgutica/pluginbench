from __future__ import annotations

import hashlib
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from pluginbench import benchmark, evaluation
from pluginbench.benchmark import (
    Cell,
    Configuration,
    Package,
    ReviewerRule,
    Suite,
    SuiteTask,
    Vanilla,
    check_release_binding,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _task(name: str, cluster: str | None = None) -> SuiteTask:
    return SuiteTask(
        content_sha256=_digest(f"content {name}"),
        repository_cluster=cluster or f"repo-{name}",
        coding_contract_sha256=_digest("coding contract"),
        observer_profile_sha256=_digest(f"profile {name}"),
        mutation_timeout_seconds=600,
    )


# The first fill (plan-v5-delta §8.4): Claude Code reviewed by Codex, and the paused Codex
# cell reviewed by Claude.
CLAUDE_REVIEWER = ReviewerRule(
    coding_harness="claude", reviewer_harness="codex", reviewer_model_id="gpt-6.2-sol",
    reviewer_effort="high", reviewer_cli_version="0.167.0", protocol_sha256=_digest("protocol"),
)
CODEX_REVIEWER = ReviewerRule(
    coding_harness="codex", reviewer_harness="claude", reviewer_model_id="claude-opus-5-5",
    reviewer_effort="high", reviewer_cli_version="2.1.322", protocol_sha256=_digest("protocol"),
)
SUITE = Suite(
    tasks=(_task("a"), _task("b")),
    attempts=3,
    seed=7,
    observer_contract_sha256=_digest("observers"),
    reviewers=(CLAUDE_REVIEWER, CODEX_REVIEWER),
)
CLAUDE = Configuration(
    harness="claude", model_id="claude-opus-5-5", effort="medium", cli_version="2.1.322",
    launch_sha256=_digest("claude launch"),
)
PACKAGE = Package(source_commit="a" * 40, source_tree="b" * 40)
CELL = Cell(entrant=PACKAGE, configuration=CLAUDE, suite=SUITE)


def _changed(value: Any) -> Any:
    """Another valid value of the same field."""
    if isinstance(value, bool):
        raise TypeError("no boolean fields are expected")
    if isinstance(value, int):
        return value + 1
    if value in get_args(benchmark.Harness):
        return "codex" if value == "claude" else "claude"
    if value in get_args(benchmark.Effort):
        return "high" if value != "high" else "low"
    if len(value) in (40, 64) and set(value) <= set("0123456789abcdef"):
        return ("c" if value[0] != "c" else "d") + value[1:]
    return value + "-other"


def _with(model: Any, field: str) -> Any:
    return type(model).model_validate({**model.model_dump(), field: _changed(getattr(model, field))})


@pytest.mark.parametrize(
    ("model", "identity"),
    [
        (PACKAGE, lambda value: value.id),
        (CLAUDE, lambda value: value.revision),
        (CLAUDE_REVIEWER, lambda value: value.contract),
    ],
    ids=["package", "configuration", "reviewer"],
)
def test_every_field_of_an_entrant_configuration_or_reviewer_is_identity(
    model: Any, identity: Any
) -> None:
    # A field added later without entering the digest fails here.
    for field in type(model).model_fields:
        if field == "kind":
            continue
        assert identity(_with(model, field)) != identity(model), field


@pytest.mark.parametrize("field", list(SuiteTask.model_fields))
def test_every_task_field_changes_the_cell_and_only_its_own_contract(field: str) -> None:
    changed = SUITE.model_copy(update={"tasks": (_with(SUITE.tasks[0], field), SUITE.tasks[1])})
    cell = CELL.model_copy(update={"suite": changed})

    assert cell.id != CELL.id
    before, after = CELL.slots(), cell.slots()
    coded = field in {"content_sha256", "coding_contract_sha256"}
    measured = field in {"observer_profile_sha256", "mutation_timeout_seconds"}
    # A cluster names the comparison population, not the work: no slot changes.
    assert ({s.id for s in after} != {s.id for s in before}) is coded
    if measured:
        assert [s.id for s in after] == [s.id for s in before]
        assert [s.review_measurement_id for s in after] == [s.review_measurement_id for s in before]
        observed = [b.observer_measurement_id != a.observer_measurement_id
                    for b, a in zip(before, after, strict=True)]
        # Only the changed task's three attempts are measured again.
        assert sum(observed) == SUITE.attempts


@pytest.mark.parametrize("field", ["attempts", "seed", "observer_contract_sha256"])
def test_every_suite_field_changes_the_cell(field: str) -> None:
    cell = CELL.model_copy(update={"suite": _with(SUITE, field)})

    assert cell.id != CELL.id and cell.suite.id != SUITE.id


def test_a_seed_orders_attempts_but_never_enters_a_slot() -> None:
    cell = CELL.model_copy(update={"suite": _with(SUITE, "seed")})

    assert cell.slots() == CELL.slots()


def test_adding_a_task_or_an_attempt_keeps_every_existing_slot() -> None:
    larger = SUITE.model_copy(update={"tasks": (*SUITE.tasks, _task("c")), "attempts": 4})
    grown = CELL.model_copy(update={"suite": larger})

    assert {slot.id for slot in CELL.slots()} < {slot.id for slot in grown.slots()}
    assert len(grown.slots()) == 3 * 4
    assert {s.observer_measurement_id for s in CELL.slots()} < {
        s.observer_measurement_id for s in grown.slots()
    }


def test_declaration_order_changes_no_id() -> None:
    reordered = Suite(
        tasks=tuple(reversed(SUITE.tasks)),
        attempts=SUITE.attempts,
        seed=SUITE.seed,
        observer_contract_sha256=SUITE.observer_contract_sha256,
        reviewers=tuple(reversed(SUITE.reviewers)),
    )
    cell = CELL.model_copy(update={"suite": reordered})

    assert reordered.id == SUITE.id and cell.id == CELL.id and cell.slots() == CELL.slots()


def test_packages_share_one_vanilla_baseline() -> None:
    other = CELL.model_copy(update={"entrant": Package(source_commit="c" * 40, source_tree="d" * 40)})

    assert other.baseline == CELL.baseline and other.baseline.id == CELL.baseline.id
    assert other.baseline.slots() == CELL.baseline.slots()
    assert {s.id for s in CELL.slots()}.isdisjoint(s.id for s in CELL.baseline.slots())
    assert {s.id for s in CELL.slots()}.isdisjoint(s.id for s in other.slots())


def test_a_new_reviewer_reviews_again_and_never_recodes_or_remeasures() -> None:
    reviewer = CLAUDE_REVIEWER.model_copy(update={"reviewer_cli_version": "0.168.0"})
    cell = CELL.model_copy(
        update={"suite": SUITE.model_copy(update={"reviewers": (reviewer, CODEX_REVIEWER)})}
    )

    before, after = CELL.slots(), cell.slots()
    assert [s.id for s in after] == [s.id for s in before]
    assert [s.observer_measurement_id for s in after] == [s.observer_measurement_id for s in before]
    assert all(
        b.review_measurement_id != a.review_measurement_id for b, a in zip(before, after, strict=True)
    )


def test_a_cell_binds_only_its_own_harness_reviewer() -> None:
    codex = CODEX_REVIEWER.model_copy(update={"reviewer_effort": "max"})
    cell = CELL.model_copy(
        update={"suite": SUITE.model_copy(update={"reviewers": (CLAUDE_REVIEWER, codex)})}
    )

    assert cell.id == CELL.id and cell.suite.id != SUITE.id


def test_a_cli_upgrade_keeps_the_display_key_and_starts_a_new_revision() -> None:
    upgraded = CLAUDE.model_copy(update={"cli_version": "2.1.400"})
    cell = CELL.model_copy(update={"configuration": upgraded})

    assert upgraded.key == CLAUDE.key and upgraded.revision != CLAUDE.revision
    assert {s.id for s in cell.slots()}.isdisjoint(s.id for s in CELL.slots())


def test_a_release_label_binds_one_entrant() -> None:
    check_release_binding(existing_id=None, proposed_id=PACKAGE.id)
    check_release_binding(existing_id=PACKAGE.id, proposed_id=PACKAGE.id)
    edited = PACKAGE.model_copy(update={"source_tree": "e" * 40})  # Same commit, new bytes.

    with pytest.raises(ValueError, match=f"already bound to entrant {PACKAGE.id}"):
        check_release_binding(existing_id=PACKAGE.id, proposed_id=edited.id)


def test_ids_are_pinned() -> None:
    # Changing these redefines every stored identity: bump benchmark.HASH_VERSION instead.
    assert Vanilla().id == "95d1e55f4a7cd2abcefb516b563b21824302721cdd7a22e3c8423f391b963601"
    assert PACKAGE.id == "cafcce55c350ae2bc4a26a1e7cf9fefc2075c1b45d535473d1ef701ab9adc051"
    assert CLAUDE.revision == "cafa581673918d8ec66796571c0f389adc605afbccf2df1856c8232e6cd53efb"
    assert CELL.slots()[0].id == "79ae6b3baf0d6503a6ae5649224357e612c9f916c4e0115ee2806f6b619d28eb"


def test_a_cell_survives_a_json_round_trip() -> None:
    copied = Cell.model_validate_json(CELL.model_dump_json())

    assert copied == CELL and copied.id == CELL.id and copied.slots() == CELL.slots()
    assert Cell.model_validate_json(CELL.baseline.model_dump_json()).entrant == Vanilla()


@pytest.mark.parametrize(
    "change",
    [
        {"tasks": ()},
        {"tasks": (_task("a"), _task("a"))},
        {"attempts": 0},
        {"reviewers": (CLAUDE_REVIEWER, CLAUDE_REVIEWER)},
        {"observer_contract_sha256": "sha256:" + _digest("observers")},
        {"unknown": 1},
    ],
    ids=["no-tasks", "duplicate-task", "no-attempts", "duplicate-reviewer", "prefixed", "extra"],
)
def test_an_invalid_suite_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Suite.model_validate({**SUITE.model_dump(), **change})


def test_a_cell_needs_a_reviewer_for_its_harness_and_a_full_commit() -> None:
    with pytest.raises(ValidationError, match="no reviewer rule for claude"):
        Cell(entrant=PACKAGE, configuration=CLAUDE,
             suite=SUITE.model_copy(update={"reviewers": (CODEX_REVIEWER,)}))
    with pytest.raises(ValidationError):
        Package(source_commit="a" * 12, source_tree="b" * 40)
    with pytest.raises(ValidationError):
        benchmark.Slot.model_validate({**CELL.slots()[0].model_dump(), "attempt_index": 0})


def test_harness_and_effort_match_the_runner() -> None:
    fields = evaluation.Runner.model_fields
    assert get_args(benchmark.Harness) == get_args(fields["harness"].annotation)
    assert get_args(benchmark.Effort) == get_args(fields["reasoning"].annotation)
