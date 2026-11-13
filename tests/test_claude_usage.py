"""Claude coding token accounting (design D4) on 2.1.322 shapes.

See fixtures/claude-2.1.322/coding/README.md: `plain`, `one-subagent` and `two-parallel` are
calibrated probe runs; `compaction` is derived from a two-turn probe, since no single-prompt
auto-compaction has been observed.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pluginbench import claude, claude_usage
from pluginbench.saved_usage import revalidate_usage

FIXTURES = Path(__file__).parent / "fixtures/claude-2.1.322/coding"
RUNNER = "claude-opus-5-5"
MODEL = "claude-opus-5-5"
VERSION = "2.1.322"


def _copy(case: str, directory: Path) -> Path:
    shutil.copytree(FIXTURES / case, directory)
    return directory


def _validate(run: Path, runner: str = RUNNER, version: str = VERSION) -> dict[str, Any]:
    return claude_usage.coding_usage(
        (run / "stream.jsonl").read_text(), run / "sessions", runner_model=runner,
        cli_version=version,
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _rewrite(path: Path, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in edit(_lines(path))))


def _root(run: Path) -> Path:
    (root,) = (run / "sessions/projects/-app").glob("*.jsonl")
    return root


def _subagent(run: Path) -> Path:
    return next((run / "sessions/projects/-app").rglob("subagents/agent-*.jsonl"))


def _result(edit: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def mutate(run: Path) -> None:
        def apply(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for event in events:
                if event["type"] == "result":
                    edit(event)
            return events

        _rewrite(run / "stream.jsonl", apply)

    return mutate


def _assistant(
    path: Callable[[Path], Path], edit: Callable[[dict[str, Any]], None]
) -> Callable[[Path], None]:
    """Edit the first assistant message of a transcript."""

    def mutate(run: Path) -> None:
        def apply(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
            edit(next(event for event in events if event["type"] == "assistant")["message"])
            return events

        _rewrite(path(run), apply)

    return mutate


@pytest.mark.parametrize(
    ("case", "input_tokens", "output_tokens", "depth"),
    [("plain", 15340, 4, 0), ("one-subagent", 76976, 565, 1), ("two-parallel", 91615, 634, 1)],
)
def test_well_formed_runs_validate_with_transcripts_equal_to_model_usage(
    tmp_path: Path, case: str, input_tokens: int, output_tokens: int, depth: int
) -> None:
    run = _copy(case, tmp_path / case)
    parsed = _validate(run)
    accounting = parsed["usage_accounting"]
    assert accounting["reasons"] == [] and accounting["status"] == "complete"
    assert accounting["rule"] == "equal" and accounting["untranscribed"] == {}
    assert accounting["observed_subagent_depth"] == depth
    model = accounting["per_model"][MODEL]
    assert (model["input_tokens"], model["output_tokens"]) == (input_tokens, output_tokens)
    assert (model["transcript_input_tokens"], model["transcript_output_tokens"]) == (
        input_tokens, output_tokens,
    )
    # The headline is the recorded attempt usage, so a saved run never disagrees with it.
    assert parsed["usage"] == claude.parse_events((run / "stream.jsonl").read_text(), [])["usage"]
    assert parsed["usage"]["input_tokens"] == input_tokens


def test_compaction_validates_and_reports_the_untranscribed_excess(tmp_path: Path) -> None:
    parsed = _validate(_copy("compaction", tmp_path / "run"))
    accounting = parsed["usage_accounting"]
    assert accounting["reasons"] == [] and accounting["rule"] == "compaction"
    assert parsed["usage"]["input_tokens"] == 32677 and parsed["usage"]["output_tokens"] == 425
    assert accounting["untranscribed"] == {MODEL: {"input_tokens": 17338, "output_tokens": 421}}


def _orphan(run: Path) -> None:
    # The copy repeats the real subagent's messages, so only its link is wrong.
    source = _subagent(run)
    orphan = source.with_name("agent-orphan.jsonl")
    shutil.copyfile(source, orphan)
    orphan.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": "toolu_orphan"}))


def _unlinked(run: Path) -> None:
    _subagent(run).with_suffix(".meta.json").unlink()


def _no_subagent(run: Path) -> None:
    for path in _subagent(run).parent.iterdir():
        path.unlink()


def _second_root(run: Path) -> None:
    shutil.copyfile(_root(run), _root(run).with_name("second.jsonl"))


def _repeated_id(run: Path) -> None:
    root_id = next(e for e in _lines(_root(run)) if e["type"] == "assistant")["message"]["id"]
    _assistant(_subagent, lambda message: message.update(id=root_id))(run)


def _drop_result(run: Path) -> None:
    _rewrite(run / "stream.jsonl", lambda events: [e for e in events if e["type"] != "result"])


def _exceed(message: dict[str, Any]) -> None:
    message["usage"]["cache_read_input_tokens"] += 20000


@pytest.mark.parametrize(
    ("case", "mutate", "reason"),
    [
        ("one-subagent", _orphan, "a subagent transcript has no matching Agent tool call"),
        ("one-subagent", _unlinked, "a subagent transcript has no matching Agent tool call"),
        ("one-subagent", _no_subagent, "an Agent tool call has no subagent transcript"),
        ("plain", _second_root, "the sessions need exactly one root transcript"),
        ("one-subagent", _repeated_id, "a message id repeats with different usage"),
        ("compaction", _assistant(_root, _exceed), f"transcripts exceed modelUsage for {MODEL}"),
        (
            "plain",
            _assistant(_root, lambda message: message["usage"].update(output_tokens=3)),
            f"transcripts differ from modelUsage for {MODEL}",
        ),
        (
            "one-subagent",
            _assistant(_subagent, lambda message: message.update(model="claude-haiku-5-5")),
            "transcript model claude-haiku-5-5 is missing from modelUsage",
        ),
        ("plain", _drop_result, "the stream needs exactly one system/init and one result event"),
        (
            "plain",
            _result(lambda result: result.update(is_error=True)),
            "the result is an error or not a success",
        ),
        (
            "plain",
            _result(lambda result: result.update(subtype="error_max_turns")),
            "the result is an error or not a success",
        ),
        (
            "plain",
            _result(lambda result: result.update(session_id="another")),
            "the init and result session ids disagree",
        ),
        (
            "plain",
            _result(lambda result: result["modelUsage"][MODEL].update(outputTokens="4")),
            "a modelUsage count is missing or ill-typed",
        ),
    ],
)
def test_each_accounting_rule_leaves_usage_unvalidated(
    tmp_path: Path, case: str, mutate: Callable[[Path], None], reason: str
) -> None:
    run = _copy(case, tmp_path / "run")
    mutate(run)
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["status"] == "incomplete"
    assert reason in parsed["usage_accounting"]["reasons"]


def test_the_primary_model_must_be_the_runner_model(tmp_path: Path) -> None:
    parsed = _validate(_copy("plain", tmp_path / "run"), runner="claude-sonnet-5-5")
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["reasons"] == [
        "the primary model differs from the runner model"
    ]


def _init_version(version: str | None) -> Callable[[Path], None]:
    def mutate(run: Path) -> None:
        def apply(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
            events[0].pop("claude_code_version")
            if version is not None:
                events[0]["claude_code_version"] = version
            return events

        _rewrite(run / "stream.jsonl", apply)

    return mutate


@pytest.mark.parametrize("version", ["2.1.400", None])
def test_the_launched_version_picks_the_rule_and_the_stream_must_agree(
    tmp_path: Path, version: str | None
) -> None:
    # A stream claiming another version, or none, would otherwise get the one-sided rule.
    run = _copy("plain", tmp_path / "run")
    _init_version(version)(run)
    _result(lambda result: result["modelUsage"][MODEL].update(inputTokens=500002))(run)
    parsed = _validate(run)
    assert parsed["usage"] is None and parsed["usage_accounting"]["rule"] == "equal"
    assert "the init CLI version is missing or differs from the launched version" in (
        parsed["usage_accounting"]["reasons"]
    )


def test_an_unqualified_version_compares_input_only_and_never_more_than_model_usage(
    tmp_path: Path,
) -> None:
    run = _copy("plain", tmp_path / "run")
    _init_version("2.1.400")(run)
    # Output counts are not compared, and transcribed input below modelUsage is reported.
    _assistant(_root, lambda message: message["usage"].update(output_tokens=1))(run)
    _assistant(_root, lambda message: message["usage"].update(cache_creation_input_tokens=15000))(
        run
    )
    parsed = _validate(run, version="2.1.400")
    accounting = parsed["usage_accounting"]
    assert accounting["reasons"] == [] and accounting["rule"] == "one-sided"
    assert accounting["cli_version"] == "2.1.400"
    assert accounting["untranscribed"] == {MODEL: {"input_tokens": 338}}

    _assistant(_root, _exceed)(run)
    refused = _validate(run, version="2.1.400")
    assert refused["usage"] is None
    assert refused["usage_accounting"]["reasons"] == [f"transcripts exceed modelUsage for {MODEL}"]


def test_stderr_lines_in_the_harbor_capture_are_not_stream_events(tmp_path: Path) -> None:
    run = _copy("plain", tmp_path / "run")
    stream = run / "stream.jsonl"
    stream.write_text("warning: written to stderr\n" + stream.read_text())
    assert _validate(run)["usage_accounting"]["reasons"] == []
    stream.write_text(stream.read_text() + '{"type": "result"\n')
    assert "the stream has a malformed JSON event" in _validate(run)["usage_accounting"]["reasons"]


def test_a_stream_line_that_hides_an_event_is_refused(tmp_path: Path) -> None:
    # Prefixing a second result would otherwise drop it from the one-result rule.
    run = _copy("plain", tmp_path / "run")
    stream = run / "stream.jsonl"
    (result,) = [line for line in stream.read_text().splitlines() if '"type": "result"' in line]
    # As the real CLI writes it: compact, with "type" not the first key.
    event = json.loads(result)
    event = {"is_error": event.pop("is_error"), **event}
    hidden = json.dumps(event, separators=(",", ":"))
    stream.write_text(stream.read_text() + "warning: " + hidden + "\n")
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["reasons"] == [
        "a stream line hides an event behind other output"
    ]


def _stream_compaction(run: Path) -> None:
    def apply(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        boundary = {"type": "system", "subtype": "compact_boundary",
                    "session_id": events[0]["session_id"]}
        return [*events[:-1], boundary, events[-1]]

    _rewrite(run / "stream.jsonl", apply)


def _root_compaction_only(run: Path) -> None:
    _rewrite(run / "stream.jsonl", lambda events: [
        e for e in events if e.get("subtype") != "compact_boundary"
    ])


def test_compaction_counts_only_when_the_root_transcript_records_it(tmp_path: Path) -> None:
    run = _copy("plain", tmp_path / "stream-only")
    _stream_compaction(run)
    _result(lambda result: result["modelUsage"][MODEL].update(inputTokens=500002))(run)
    parsed = _validate(run)
    assert parsed["usage"] is None and parsed["usage_accounting"]["rule"] == "equal"
    assert parsed["usage_accounting"]["reasons"] == [
        "the stream and the root transcript disagree on compaction",
        f"transcripts differ from modelUsage for {MODEL}",
    ]
    run = _copy("compaction", tmp_path / "root-only")
    _root_compaction_only(run)
    parsed = _validate(run)
    assert "the stream and the root transcript disagree on compaction" in (
        parsed["usage_accounting"]["reasons"]
    )


def test_only_the_runner_model_may_have_untranscribed_usage(tmp_path: Path) -> None:
    run = _copy("compaction", tmp_path / "run")
    _result(lambda result: result["modelUsage"].update({"claude-haiku-5-5": {
        "inputTokens": 50000, "outputTokens": 0,
        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
    }}))(run)
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["reasons"] == [
        "transcripts differ from modelUsage for claude-haiku-5-5"
    ]


# The compaction fixture's bound: preTokens 15,343 plus the prompt allowance of input, and
# postTokens 4,799 of output. Its untranscribed excess is 17,338 input and 421 output.
_BOUND = {"inputTokens": 15343 + claude_usage.COMPACTION_PROMPT_TOKENS, "outputTokens": 4799}
_EXCESS = {"inputTokens": 17338, "outputTokens": 421}


def _add_to_model_usage(key: str, tokens: int) -> Callable[[Path], None]:
    return _result(lambda result: result["modelUsage"][MODEL].update(
        {key: result["modelUsage"][MODEL][key] + tokens}
    ))


@pytest.mark.parametrize("key", ["inputTokens", "outputTokens"])
def test_untranscribed_usage_is_bounded_by_the_compaction(tmp_path: Path, key: str) -> None:
    run = _copy("compaction", tmp_path / "at-bound")
    _add_to_model_usage(key, _BOUND[key] - _EXCESS[key])(run)
    assert _validate(run)["usage_accounting"]["reasons"] == []
    _add_to_model_usage(key, 1)(run)
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["reasons"] == [
        f"untranscribed usage for {MODEL} exceeds its compactions' bound"
    ]


@pytest.mark.parametrize("key", ["inputTokens", "outputTokens"])
def test_two_compactions_sum_their_bounds(tmp_path: Path, key: str) -> None:
    run = _copy("compaction", tmp_path / "run")
    _rewrite(_root(run), lambda events: [
        *events, *(e for e in events if e.get("subtype") == "compact_boundary"),
    ])
    _add_to_model_usage(key, 2 * _BOUND[key] - _EXCESS[key])(run)
    assert _validate(run)["usage_accounting"]["reasons"] == []
    _add_to_model_usage(key, 1)(run)
    assert _validate(run)["usage_accounting"]["reasons"] == [
        f"untranscribed usage for {MODEL} exceeds its compactions' bound"
    ]


def _metadata(edit: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    return lambda boundary: edit(boundary["compactMetadata"])


@pytest.mark.parametrize(
    "edit",
    [
        lambda boundary: boundary.pop("compactMetadata"),
        _metadata(lambda meta: meta.pop("preTokens")),
        _metadata(lambda meta: meta.update(preTokens="15343")),
        _metadata(lambda meta: meta.update(preTokens=True)),
        _metadata(lambda meta: meta.update(preTokens=-1)),
        _metadata(lambda meta: meta.pop("postTokens")),
        _metadata(lambda meta: meta.update(postTokens=4799.0)),
    ],
    ids=["no-metadata", "no-pre", "string-pre", "bool-pre", "negative-pre", "no-post", "float-post"],
)
def test_a_compaction_without_well_typed_token_counts_is_refused(
    tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> None:
    run = _copy("compaction", tmp_path / "run")

    def apply(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        edit(next(e for e in events if e.get("subtype") == "compact_boundary"))
        return events

    _rewrite(_root(run), apply)
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert parsed["usage_accounting"]["reasons"] == [
        "a compact boundary's preTokens or postTokens is missing or ill-typed"
    ]


def test_the_stream_agent_calls_must_match_the_transcripts(tmp_path: Path) -> None:
    # Deleting a subagent and its call from the transcripts leaves the stream's call behind.
    run = _copy("one-subagent", tmp_path / "run")
    _no_subagent(run)
    _rewrite(_root(run), lambda events: [
        e for e in events
        if not any(block.get("name") == "Agent" for block in _content(e))
    ])
    parsed = _validate(run)
    assert parsed["usage"] is None
    assert "the stream's Agent calls differ from the transcripts'" in (
        parsed["usage_accounting"]["reasons"]
    )


def _content(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = event.get("message", {}).get("content")
    return content if isinstance(content, list) else []


def test_a_transcript_message_without_usage_is_not_a_model_call(tmp_path: Path) -> None:
    run = _copy("plain", tmp_path / "run")
    synthetic = {"type": "assistant", "sessionId": _root(run).stem, "message": {
        "id": "msg_synthetic", "model": "<synthetic>", "role": "assistant",
        "content": [{"type": "text", "text": "<text>"}],
        "usage": dict.fromkeys(
            ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
             "output_tokens"), 0,
        ),
    }}
    _rewrite(_root(run), lambda events: [*events, synthetic])
    assert _validate(run)["usage_accounting"]["reasons"] == []


def _saved(case: str, directory: Path, **execution: Any) -> Path:
    """A saved Claude attempt in the layout Harbor and `_native_evidence` leave behind."""
    agent = directory / "artifacts/harbor/trial/agent"
    agent.mkdir(parents=True)
    shutil.copytree(FIXTURES / case / "sessions", agent / "sessions")
    shutil.copyfile(FIXTURES / case / "stream.jsonl", directory / "artifacts/stdout.jsonl")
    stream = (FIXTURES / case / "stream.jsonl").read_text()
    receipt = directory / "execution.json"
    receipt.write_text(json.dumps({
        "launch": {"harness": "claude-cli", "requested_model": RUNNER, "harness_version": VERSION},
        "usage": claude.parse_events(stream, [])["usage"],
        **execution,
    }))
    return receipt


@pytest.mark.parametrize("case", ["plain", "one-subagent", "two-parallel", "compaction"])
def test_a_well_formed_saved_claude_run_revalidates(tmp_path: Path, case: str) -> None:
    receipt = _saved(case, tmp_path / "attempt")
    execution = json.loads(receipt.read_text())
    revalidated = revalidate_usage(execution, receipt)
    assert revalidated["usage_validation"]["status"] == "validated"
    assert revalidated["usage_validation"]["reasons"] == []
    assert revalidated["usage"] == execution["usage"]
    assert revalidated["usage_accounting"]["status"] == "complete"
    assert revalidated["usage_accounting"]["per_model"][MODEL]["input_tokens"] == (
        execution["usage"]["input_tokens"]
    )


def test_a_saved_claude_run_falls_back_to_the_harbor_capture(tmp_path: Path) -> None:
    receipt = _saved("plain", tmp_path / "attempt")
    stdout = tmp_path / "attempt/artifacts/stdout.jsonl"
    stdout.rename(tmp_path / "attempt/artifacts/harbor/trial/agent/claude-code.txt")
    validation = revalidate_usage(json.loads(receipt.read_text()), receipt)["usage_validation"]
    assert validation["status"] == "validated"


@pytest.mark.parametrize(
    ("execution", "reason"),
    [
        (
            {"usage": {"input_tokens": 1, "output_tokens": 4}},
            "revalidated usage disagrees with recorded usage",
        ),
        (
            {"launch": {"harness": "claude-cli", "requested_model": "claude-sonnet-5-5",
                        "harness_version": VERSION}},
            "the primary model differs from the runner model",
        ),
        (
            {"launch": {"harness": "claude-cli", "requested_model": RUNNER,
                        "harness_version": "2.1.400"}},
            "the init CLI version is missing or differs from the launched version",
        ),
        (
            {"native_treatment": {"hooks": {"hooks": [{"event": "SessionStart"}]}}},
            "command-hook subprocess usage is not independently metered",
        ),
    ],
)
def test_a_saved_claude_run_is_refused_on_reuse_and_report(
    tmp_path: Path, execution: dict[str, Any], reason: str
) -> None:
    receipt = _saved("one-subagent", tmp_path / "attempt", **execution)
    revalidated = revalidate_usage(json.loads(receipt.read_text()), receipt)
    assert revalidated["usage"] is None
    assert revalidated["usage_validation"]["status"] == "unavailable"
    assert reason in revalidated["usage_validation"]["reasons"]


HOOKED = {"native_treatment": {"hooks": {"hooks": [{"event": "SessionStart"}]}}}


def test_a_hooked_saved_claude_run_checks_the_session_usage_kept_at_launch(tmp_path: Path) -> None:
    # At launch a hooked run keeps the session usage as native_session_usage and clears usage.
    receipt = _saved("one-subagent", tmp_path / "attempt", **HOOKED)
    execution = json.loads(receipt.read_text())
    execution.update(native_session_usage=execution["usage"], usage=None)
    reasons = revalidate_usage(execution, receipt)["usage_validation"]["reasons"]
    assert reasons == ["command-hook subprocess usage is not independently metered"]
    execution["native_session_usage"] = {"input_tokens": 1, "output_tokens": 4}
    reasons = revalidate_usage(execution, receipt)["usage_validation"]["reasons"]
    assert reasons == [
        "revalidated usage disagrees with recorded usage",
        "command-hook subprocess usage is not independently metered",
    ]


def test_a_saved_claude_run_with_a_broken_transcript_set_is_refused(tmp_path: Path) -> None:
    receipt = _saved("one-subagent", tmp_path / "attempt")
    _no_subagent(tmp_path / "attempt/artifacts/harbor/trial/agent")
    revalidated = revalidate_usage(json.loads(receipt.read_text()), receipt)
    assert revalidated["usage"] is None
    assert "an Agent tool call has no subagent transcript" in (
        revalidated["usage_validation"]["reasons"]
    )


def test_the_fixtures_carry_only_synthetic_ids_and_no_cost() -> None:
    real = re.compile(
        r"msg_[A-Za-z0-9]{20,}|toolu_[A-Za-z0-9]{20,}|total_cost_usd"
        r"|(?!00000000-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        r"|a(?!0{8})[0-9a-f]{16}"
    )
    for path in FIXTURES.rglob("*"):
        assert not real.search(str(path.relative_to(FIXTURES))), path
        if path.is_file():
            assert not real.search(path.read_text()), path
