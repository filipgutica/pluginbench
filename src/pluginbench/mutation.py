"""Mutation bug-catch rate (preregistration M1): single-site mutants of an attempt's own code.

Sites are decision operators on the production lines an attempt added or changed. A
deterministic sample of at most M mutants, one per line, each runs the task's frozen
coverage-profile command in the task's base image, offline. The collector records raw facts:
every candidate site, the selected mutants, each run's suite status, clock readings and
receipt. `mutation_metrics` derives every outcome and count from those facts, so a report
cannot carry a count its runs do not support. `compare_mutation` binds a report to its
attempt's diff and adds the descriptive split by line execution.

Version 2 reports (this observer) are bounded. Every clock reading is the whole milliseconds
since the attempt started, on the monotonic clock, rounded up; a duration is the difference of
two readings, so it can be up to 1 ms below the exact interval rounded up:

- Baseline: two clean runs of the unmutated tree, each capped at `Limits.baseline_ms` (4T for
  a submission, T being the task's qualified reference baseline; a calibration ceiling for the
  reference that measures T). B is the slower of the two, timed around the executor call.
- Site: each mutant has 6B from its dispatch, shared by its build or type check, its first run
  and its confirmation. The first run is also capped at 3B; a check and a confirmation have no
  cap of their own.
- Attempt: `Limits.attempt_ms` from before site discovery, if set.

Every call gets the least of the clocks that apply, as absolute deadlines; on a tie the attempt
wins, then the site, then the command's own cap. B and every deadline are on the same clock,
timed around the whole executor call (`TIMING_BOUNDARY`). The launcher rounds a timeout up to
whole seconds and times only its exec, so a call can return after its deadline: such a late
return counts as cut by the limit it overran, and a late green is never `survived`. A first run stopped at its own 3B cap is a `timeout_kill`. A site
deadline that stops a run or leaves no time for one makes the mutant `unconfirmed`, excluded
from the denominator: an unfinished confirmation never confirms a kill. The attempt limit
expiring before every required mutant settles is an infrastructure failure, so the attempt is
retried, never observed. Version 1 reports (ten mutants, serial, a `3 x slowest + 60 s`
timeout and confirmations past it counted as kills) still validate.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from pluginbench import components, isolation, source_only
from pluginbench.assessment_metrics import _added_source_lines, compare_snapshots, inspect_snapshot
from pluginbench.collectors import CollectorUnavailable

VERSION = "mutation-v2"
# Reports this observer still validates, oldest first.
V1 = "mutation-v1"
GO_OBSERVER = "go-mutation-v1"
OPERATORS = (
    "relational_boundary",
    "equality_negation",
    "boolean_connective",
    "condition_negation",
)
# M, the most mutants one attempt measures. A timing pilot pins it; its first ten sites are the
# version 1 sample, so M = 10 and M = 20 measure the same first ten mutants.
SAMPLE_SIZE = 20
V1_SAMPLE_SIZE = 10
# Mutants run in up to this many lanes at once, one container each, where the caller runs one
# measurement at a time. Every other caller keeps one lane (`Limits.lanes`).
LANES = 4
# Each container's limits: four lanes need 8 CPUs and 8,192 MiB.
MEASUREMENT_CPUS, MEASUREMENT_MEMORY_MB = 2, 2048
# A submission baseline's cap in reference baselines (T), and a site's deadline and its first
# run's cap in submission baselines (B). Version 2 adds no grace: B is timed around the whole
# executor call, so it already holds the container start, copy and export that version 1's
# 60 s grace allowed for, and the launcher bounds those steps itself (60 s, 300 s for copies).
BASELINE_FACTOR, SITE_FACTOR, FIRST_RUN_FACTOR = 4, 6, 3
# What a version 2 run's elapsed time covers. The launcher records no exec-only time, so B is
# the whole `run_native_command` call: container create, copies, exec, export and cleanup.
TIMING_BOUNDARY = "executor_call"
LANGUAGES = frozenset({"python", "go", "typescript", "javascript"})
# Generated files the preregistration names: anko's goyacc parser and sql-formatter's nearley
# grammar. They are excluded by task repository, as the frozen task.toml records it.
GENERATED_FILES: dict[str, tuple[str, ...]] = {
    "https://github.com/mattn/anko": ("parser/parser.go",),
    "https://github.com/sql-formatter-org/sql-formatter": ("src/parser/grammar.ts",),
}
# csstree's profile prints no `PB-SUITE-EXIT` marker, so its suite status is the profile's own
# process exit, the c8 Mocha run's status. Every other task must print exactly one marker.
PROCESS_EXIT_REPOSITORIES = frozenset({"https://github.com/csstree/csstree"})
# The invalid-mutant checks, run in the frozen task image on the mutated tree.
GO_CHECK = ["bash", "-c", "go build ./... && go test -run '^$' ./..."]
TSC_CHECK = ["bash", "-c", "npx --no-install tsc --noEmit --pretty false"]
# Bash's exits for a command it could not run, such as a toolchain missing from the image.
_NOT_RUNNABLE = frozenset({126, 127})
# Version 1's mutant timeout, `ceil(3 x slowest baseline + 60 s)`; operations.py also bounds a
# mutation collect with it.
TIMEOUT_FACTOR, TIMEOUT_GRACE_SECONDS = 3, 60
SUITE_MARKER = re.compile(r"^PB-SUITE-EXIT (\d+)\r?$", re.MULTILINE)
_TSC_ERROR = re.compile(r"^(?:(?P<file>.+)\(\d+,\d+\): )?error (?P<code>TS\d+): (?P<message>.*)$")
_TSC_LOCATED = re.compile(r"^(?P<file>.+)\((?P<line>\d+),(?P<column>\d+)\): error ")
_REPLACEMENTS = {
    "relational_boundary": {"<": "<=", "<=": "<", ">": ">=", ">=": ">"},
    "equality_negation": {
        "==": "!=",
        "!=": "==",
        "===": "!==",
        "!==": "===",
        "is": "is not",
        "is not": "is",
    },
    "boolean_connective": {"&&": "||", "||": "&&", "and": "or", "or": "and"},
}
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_SUFFIXES = {
    ".py": "python",
    ".go": "go",
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", *_TS_SUFFIXES), "oxc"),
}
# A docker command's own failure, as the launcher's `_docker` helper words it.
_DOCKER_FAILURE = re.compile(r"^docker \S+ failed: ")
# `assert_runtime`'s one message for any failure to read the current Docker runtime.
_RUNTIME_UNVERIFIED = "Cannot verify the frozen Harbor and Docker runtime"
# The executor's record of a run whose launcher stopped before it wrote execution.json.
PRELAUNCH_FAILURE = "prelaunch-failure.json"
# The receipt directory names each report version writes: serial runs are numbered; version 2
# names each mutant's runs by its site, so lanes never share a counter or a directory.
_RECEIPTS = {
    V1: re.compile(r"runs/\d{3,}-[a-z0-9-]+"),
    VERSION: re.compile(r"runs/(?:\d{3,}-[a-z0-9-]+|(?:source-only-)?site-\d{3,}/\d{2,}-[a-z0-9-]+)"),
}
METHOD = {
    V1: (
        "single-site mutants of changed production lines (relational boundary, equality "
        "negation, boolean connective, condition negation); at most one mutant per line, ten "
        "per attempt, sampled by sha256 order and round-robin over operators; each runs the "
        "frozen coverage-profile command; kills confirmed by one rerun"
    ),
    VERSION: (
        "single-site mutants of changed production lines (relational boundary, equality "
        "negation, boolean connective, condition negation); at most one mutant per line and M "
        "per attempt, sampled by sha256 order and round-robin over operators; each runs the "
        "frozen coverage-profile command in the task's base image, four at a time; kills "
        "confirmed by one rerun within the mutant's deadline"
    ),
}
LIMITATIONS = {
    V1: (
        "arms produce different mutant populations, so this compares test suites on each arm's "
        "own code, not test quality in isolation; timeouts count as kills and are reported "
        "separately; the timeout baseline includes container start and source export time"
    ),
    VERSION: (
        "arms produce different mutant populations, so this compares test suites on each arm's "
        "own code, not test quality in isolation; a first run stopped at three baselines counts "
        "as a kill and is reported separately; a mutant whose deadline cut a run is unconfirmed "
        "and excluded; baselines include container start and source export time"
    ),
}


class SiteError(ValueError):
    """A parser reported a span that is not the operator it names; its file stays unparsed."""


class SiteFinderFailure(Exception):
    """A pinned site finder did not run to a result; the files it was given stay unparsed."""


class InfrastructureFailure(Exception):
    """Docker or the host could not run a command: the attempt is incomplete, not the revision.

    Not a ValueError, so it is never mistaken for an integrity violation, which fails closed.
    `receipts` names the failing run's receipt directory once the engine knows it.
    """

    def __init__(self, reason: str, receipts: str | None = None) -> None:
        super().__init__(reason)
        self.receipts = receipts


class AttemptDeadline(InfrastructureFailure):
    """The whole attempt's limit expired before every required mutant settled.

    Infrastructure, never an observation of the submission: the attempt is retried.
    """


def observer_identity() -> dict[str, str]:
    root = Path(__file__).parent
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in components.FILES["mutation_observer"]
    }


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    column: int
    operator: str
    start: int
    end: int
    original: str
    replacement: str

    def coordinate(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class Run:
    exit_code: int | None
    timed_out: bool
    stdout: str


# Runs `command` on a copy of `tree` with a timeout in milliseconds, leaving receipts in a
# directory. The engine times each call itself. Lanes call it from several threads at once.
Executor = Callable[[Path, list[str], int, Path], Run]
# Monotonic nanoseconds; `time.monotonic_ns` outside tests.
Clock = Callable[[], int]


@dataclass(frozen=True)
class Limits:
    """One attempt's sample size, lanes and clocks, in whole milliseconds.

    `baseline_ms` caps each unmutated baseline run; None takes the profile's coverage timeout,
    the calibration ceiling a reference measures under. `attempt_ms` is the whole attempt's
    limit; None leaves only the outer operation's bound. `lanes` stays 1 unless the caller
    guarantees that no other measurement runs at the same time: remeasurement can collect two
    attempts at once, and four lanes each would need eight containers.
    """

    sample_size: int = SAMPLE_SIZE
    lanes: int = 1
    baseline_ms: int | None = None
    attempt_ms: int | None = None

    def __post_init__(self) -> None:
        if self.sample_size < 1 or self.lanes < 1:
            raise ValueError("mutation needs at least one mutant and one lane")
        if any(value is not None and value < 1 for value in (self.baseline_ms, self.attempt_ms)):
            raise ValueError("mutation clocks must be positive milliseconds")


def submission_baseline_ms(reference_ms: int) -> int:
    """A submission baseline's cap, 4T, from the task's qualified reference baseline T."""
    return BASELINE_FACTOR * reference_ms


def elapsed_ms(start_ns: int, end_ns: int) -> int:
    """Whole milliseconds between two monotonic readings, rounded up."""
    return -(-(end_ns - start_ns) // 1_000_000)


def call_limit(
    *, started_ms: int, cap_ms: int | None, deadline_ms: int | None, attempt_ms: int | None
) -> tuple[str, int]:
    """The clock that bounds a call starting at `started_ms`, and the time it leaves.

    A call gets the least of the attempt's remaining time, its site's remaining time and its
    own cap. On a tie the attempt wins, then the site, then the command, so a timeout at a
    shared limit never counts as the command's own. Time left at or below zero means the call
    is not started.
    """
    limits = [
        (name, value)
        for name, value in (
            ("attempt", None if attempt_ms is None else attempt_ms - started_ms),
            ("site", None if deadline_ms is None else deadline_ms - started_ms),
            ("command", cap_ms),
        )
        if value is not None
    ]
    least = min(value for _, value in limits)
    return next(item for item in limits if item[1] == least)


# --- Site finding -------------------------------------------------------------------------


def _site(
    path: str,
    data: bytes,
    operator: str,
    start: int,
    end: int,
    language: str,
    tokens: str | None = None,
) -> Site:
    """A finder's span as a site, refusing a span whose text is not the operator it names.

    `tokens` is the operator as its tokens, for a Python `is not` whose span can hold a comment
    or a line continuation between them; it is otherwise the span's text.
    """
    if not 0 <= start < end <= len(data):
        raise SiteError(f"{path}: site span {start}-{end} is outside the file")
    original = data[start:end].decode("utf-8")
    if operator == "condition_negation":
        replacement = f"not ({original})" if language == "python" else f"!({original})"
    else:
        text = original if tokens is None else tokens
        table = _REPLACEMENTS.get(operator, {})
        if text not in table:
            raise SiteError(f"{path}: {original!r} is not a {operator} operator")
        replacement = table[text]
        if text == "is not" and original.split() != ["is", "not"]:
            # A comment or line continuation sits between the tokens: only the `not` token
            # goes, so the comment stays one and no line moves.
            replacement = original.removesuffix("not")
    line_start = data.rfind(b"\n", 0, start) + 1
    return Site(
        path=path,
        line=data.count(b"\n", 0, start) + 1,
        column=start - line_start + 1,
        operator=operator,
        start=start,
        end=end,
        original=original,
        replacement=replacement,
    )


def python_sites(path: str, data: bytes) -> list[Site]:
    """Sites in one Python file from `ast`, with operator tokens located by `tokenize`."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise SyntaxError("a byte-order mark shifts byte columns")
    text = data.decode("utf-8")
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    if encoding != "utf-8":
        raise SyntaxError(f"source declares encoding {encoding}")
    tree = ast.parse(data)
    starts = [0, *(match.end() for match in re.finditer(rb"\n", data))]
    lines = text.split("\n")

    def offset(line: int, byte_column: int) -> int:
        return starts[line - 1] + byte_column

    tokens = [
        (
            offset(token.start[0], len(lines[token.start[0] - 1][: token.start[1]].encode())),
            token.string,
        )
        for token in tokenize.tokenize(io.BytesIO(data).readline)
        if token.type in (tokenize.OP, tokenize.NAME)
    ]

    def between(left: ast.expr, right: ast.expr) -> tuple[int, int, str]:
        """The span of the operator tokens between two operands, ignoring parentheses, and the
        operator as its tokens, without any comment or line continuation between them."""
        assert left.end_lineno is not None and left.end_col_offset is not None
        low, high = (
            offset(left.end_lineno, left.end_col_offset),
            offset(right.lineno, right.col_offset),
        )
        found = [(at, value) for at, value in tokens if low <= at < high and value not in "()"]
        if not found:
            raise SiteError(f"{path}:{left.lineno}: no operator token between operands")
        return (
            found[0][0],
            found[-1][0] + len(found[-1][1]),
            " ".join(value for _, value in found),
        )

    sites = []

    def add(operator: str, start: int, end: int, tokens: str | None = None) -> None:
        sites.append(_site(path, data, operator, start, end, "python", tokens))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for index, op in enumerate(node.ops):
                operator = (
                    "relational_boundary"
                    if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                    else "equality_negation"
                    if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot))
                    else None
                )
                if operator:
                    left = node.left if index == 0 else node.comparators[index - 1]
                    add(operator, *between(left, node.comparators[index]))
        elif isinstance(node, ast.BoolOp):
            for left, right in zip(node.values, node.values[1:]):
                add("boolean_connective", *between(left, right))
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
            test = node.test
            assert test.end_lineno is not None and test.end_col_offset is not None
            add(
                "condition_negation",
                offset(test.lineno, test.col_offset),
                offset(test.end_lineno, test.end_col_offset),
            )
    return sites


def find_sites(
    root: Path, paths: Iterable[str], finder: Mapping[str, object], *, timeout: int = 600
) -> tuple[list[Site], dict[str, str]]:
    """Every candidate site in `paths`, and the paths no parser could read, with reasons."""
    sites: list[Site] = []
    unparsed: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for path in sorted(set(paths)):
        parser = _SUFFIXES.get(PurePosixPath(path).suffix.lower())
        if parser is None:
            unparsed[path] = "no PluginBench mutation parser for this file type"
        else:
            groups.setdefault(parser, []).append(path)
    for path in groups.get("python", []):
        try:
            sites += python_sites(path, (root / path).read_bytes())
        except (SyntaxError, UnicodeDecodeError, ValueError, SiteError) as error:
            unparsed[path] = f"python parser: {error}"
    for parser, kind in (("go", "go"), ("oxc", "oxc")):
        selected = groups.get(parser, [])
        if not selected:
            continue
        tool = finder.get(kind)
        if not isinstance(tool, dict):
            for path in selected:
                unparsed[path] = f"the profile prepared no {kind} site finder"
            continue
        binary = Path(str(tool["path"]))
        if parser == "go":
            if hashlib.sha256(binary.read_bytes()).hexdigest() != tool["sha256"]:
                raise CollectorUnavailable("the Go site finder changed after the profile froze it")
            argv, readable = [str(binary), *selected], [binary]
        else:
            from pluginbench.measurement_profiles import dependency_digest

            if dependency_digest(binary) != tool["sha256"]:
                raise CollectorUnavailable(
                    "the oxc parser cache changed after the profile froze it"
                )
            script = Path(__file__).with_name("mutation_sites.mjs")
            argv, readable = (
                ["node", str(script), str(binary), str(root), *selected],
                [binary, script],
            )
        try:
            output = _observer_output(argv, root, readable, timeout)
        except SiteFinderFailure as error:
            for path in selected:
                unparsed[path] = f"{kind} site finder: {error}"
            continue
        for failure in output["failures"]:
            unparsed[str(failure["path"])] = f"{parser} parser: {failure['reason']}"
        for row in output["sites"]:
            path = str(row["path"])
            try:
                sites.append(
                    _site(
                        path,
                        (root / path).read_bytes(),
                        str(row["operator"]),
                        int(str(row["start"])),
                        int(str(row["end"])),
                        parser,
                    )
                )
            except (SiteError, UnicodeDecodeError) as error:
                unparsed[path] = f"{parser} site: {error}"
    return sites, unparsed


def _observer_output(
    argv: list[str], root: Path, readable: list[Path], timeout: int
) -> dict[str, list[dict[str, object]]]:
    """Run a pinned site finder in the local sandbox; it reads the source and writes nothing."""
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    try:
        result = isolation.run(argv, cwd=root, env=env, timeout=timeout, readable=readable)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SiteFinderFailure(f"failed: {error}") from error
    if result.returncode:
        raise SiteFinderFailure(f"exited {result.returncode}: {result.stderr[-2000:]}")
    try:
        output = json.loads(result.stdout)
    except ValueError as error:
        raise SiteFinderFailure(f"printed no JSON: {error}") from error
    if (
        not isinstance(output, dict)
        or not isinstance(output.get("sites"), list)
        or not isinstance(output.get("failures"), list)
    ):
        raise SiteFinderFailure("output has no site and failure lists")
    for row in output["sites"]:
        if not (
            isinstance(row, dict)
            and all(isinstance(row.get(key), str) for key in ("path", "operator"))
            and all(type(row.get(key)) is int for key in ("start", "end"))
        ):
            raise SiteFinderFailure("output has a malformed site row")
    for row in output["failures"]:
        if not (isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("path", "reason"))):
            raise SiteFinderFailure("output has a malformed failure row")
    return output


# --- Sampling and classification ------------------------------------------------------------


def sample_key(site: Site, after_digest: str) -> str:
    """The preregistered order: sha256(after digest | path | line | column | operator)."""
    fields = (after_digest, site.path, str(site.line), str(site.column), site.operator)
    return hashlib.sha256("\0".join(fields).encode()).hexdigest()


def sample(candidates: list[Site], after_digest: str, size: int = SAMPLE_SIZE) -> list[Site]:
    """At most one mutant per line, round-robin over operators in order, each in sha256 order."""
    queues = {
        operator: sorted(
            (site for site in candidates if site.operator == operator),
            key=lambda site: sample_key(site, after_digest),
        )
        for operator in OPERATORS
    }
    chosen: list[Site] = []
    lines: set[str] = set()
    while len(chosen) < size and any(queues.values()):
        for operator in OPERATORS:
            queue = queues[operator]
            while queue and queue[0].coordinate() in lines:
                queue.pop(0)
            if queue and len(chosen) < size:
                site = queue.pop(0)
                chosen.append(site)
                lines.add(site.coordinate())
    return chosen


def suite_status(run: Run, process_exit: bool = False) -> int | None:
    """The one `PB-SUITE-EXIT <n>` line the profile prints; None when the run has no status.

    A run has a status only when it finished with a recorded exit. `process_exit` takes that exit
    as the status instead, for the task whose profile prints no marker.
    """
    if run.timed_out or run.exit_code is None:
        return None
    if process_exit:
        return run.exit_code
    markers = SUITE_MARKER.findall(run.stdout)
    return int(markers[0]) if len(markers) == 1 else None


def _cut(call: Mapping[str, object]) -> str | None:
    """The clock that stopped a version 2 call, left it no time, or that it overran; None when it
    ran to an exit within its time.

    The launcher rounds a timeout up to whole seconds and starts its own timer after container
    setup, so it can return an exit after the engine's limit. Such a late exit counts as cut by
    the limit it overran, whatever its status: a late green is never `survived`. An exit exactly
    at the limit is on time.
    """
    if (
        call["dispatched"] is True
        and call["timed_out"] is not True
        and int(str(call["elapsed_ms"])) <= int(str(call["timeout_ms"]))
    ):
        return None
    return str(call["bound"])


# A version 2 outcome when a clock, not the suite, ended the mutant's runs. The attempt's own
# limit leaves the mutant interrupted: unsettled, and its attempt is retried.
_CUT_FIRST = {"command": "timeout_kill", "site": "unconfirmed", "attempt": "interrupted"}
# A later run or a check has no cap of its own, so whatever stopped it short of the attempt's
# limit leaves the mutant unconfirmed.
_CUT_LATER = {"command": "unconfirmed", "site": "unconfirmed", "attempt": "interrupted"}


def site_outcome(runs: list[dict[str, object]]) -> str | None:
    """A version 2 mutant's outcome from its suite runs; None needs another run.

    killed (two nonzero statuses), timeout_kill (a first run stopped at its own 3B cap),
    survived (one green run), flaky (nonzero, then green), unconfirmed (the site deadline cut
    a run or left none), error (a run with no suite status) or interrupted (the attempt's limit
    cut it). suite_qualification re-derives this table for the reference rule, and
    tests/test_mutation.py checks the two agree.
    """
    if not runs:
        return None
    first = runs[0]
    if (cut := _cut(first)) is not None:
        return _CUT_FIRST[cut]
    if first["suite_status"] is None:
        return "error"
    if first["suite_status"] == 0:
        return "survived"
    if len(runs) == 1:
        return None
    second = runs[1]
    if (cut := _cut(second)) is not None:
        return _CUT_LATER[cut]
    if second["suite_status"] is None:
        return "error"
    return "flaky" if second["suite_status"] == 0 else "killed"


# A mutant's outcomes that settle it, and those its counts pool.
SETTLED = ("killed", "timeout_kill", "survived", "invalid", "flaky", "unconfirmed", "error")
KILLS = ("killed", "timeout_kill")


def outcome(runs: list[dict[str, object]]) -> str | None:
    """Version 1: killed, timeout, survived, flaky or error from a mutant's runs; None needs a
    rerun."""
    if not runs:
        return None
    first = runs[0]
    if first["timed_out"]:
        return "timeout"
    if first["suite_status"] is None:
        return "error"
    if first["suite_status"] == 0:
        return "survived"
    if len(runs) == 1:
        return None
    second = runs[1]
    # A confirmation past the timeout confirms the kill, and the mutant is reported timed out.
    if second["timed_out"]:
        return "timeout"
    if second["suite_status"] not in (None, 0):
        return "killed"
    return "error" if second["suite_status"] is None else "flaky"


def tsc_errors(stdout: str, site: Site | None = None) -> Counter[str]:
    """tsc diagnostics, counted, so a second copy of an existing error is a new error.

    A mutation edits one line and adds or removes no line, so only diagnostics on the mutated
    line can move: their column is dropped, and every other diagnostic keeps its own.
    """
    errors: Counter[str] = Counter()
    for line in stdout.splitlines():
        match = _TSC_ERROR.match(line.strip())
        if match:
            located = _TSC_LOCATED.match(line.strip())
            position = [located["line"], located["column"]] if located else []
            if (
                located
                and site
                and (located["file"], int(located["line"])) == (site.path, site.line)
            ):
                position = [located["line"]]
            errors[json.dumps([match["file"], *position, match["code"], match["message"]])] += 1
    return errors


# --- Collection ----------------------------------------------------------------------------


def measurement_identity(identity: Mapping[str, object]) -> dict[str, object]:
    """The task identity mutation runs under: the task's base image, 2 CPUs and 2,048 MiB.

    Preparation installs the coding harness on the task's base image and names the result the
    task's image. Mutation measures in the base image it saved as
    `toolchain_identity.base_image_identity`, so submissions from every harness, and the
    reference, run in one image.
    """
    toolchain = identity.get("toolchain_identity")
    base = toolchain.get("base_image_identity") if isinstance(toolchain, dict) else None
    runtime = identity.get("isolation")
    if not isinstance(base, dict) or not isinstance(runtime, dict):
        raise ValueError(
            "mutation measures in the task's base image, and this task identity records no "
            "toolchain_identity.base_image_identity or isolation runtime"
        )
    resources = {"cpus": MEASUREMENT_CPUS, "memory_mb": MEASUREMENT_MEMORY_MB}
    return {
        **identity,
        "image_identity": base,
        "resources": resources,
        "isolation": {**runtime, "resources": resources},
    }


def _check_capacity(identity: Mapping[str, object], lanes: int) -> None:
    """Refuse to start unless Docker holds every lane's container at once."""
    from pluginbench.harbor_backend import assert_capacity

    runtime = identity["isolation"]
    assert isinstance(runtime, dict)
    assert_capacity([[runtime]] * lanes, jobs=lanes)


def native_executor(
    identity: dict[str, object], report_path: str, *, provisioned_tool: object = None,
    provisioned_scope: list[str] | None = None
) -> Executor:
    """Run commands with `run_native_command` under `identity`, in a fresh container each.

    The launcher takes whole seconds, so a timeout is rounded up to the next second; the engine
    classifies each run on its own millisecond clocks.
    """
    from pluginbench.harbor_backend import run_native_command
    from pluginbench.measurement_tools import ProvisionedCommandOptions, read_tool

    tool = read_tool(provisioned_tool)
    tool_options: ProvisionedCommandOptions = (
        {"provisioned_tool": tool, "provisioned_scope": provisioned_scope} if tool is not None else {}
    )

    def execute(tree: Path, command: list[str], timeout_ms: int, destination: Path) -> Run:
        try:
            receipt = run_native_command(
                snapshot=tree,
                task_identity=identity,
                destination=destination,
                command=command,
                report_path=report_path,
                timeout=max(1, -(-timeout_ms // 1000)),
                produce_report=False,
                **tool_options,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            # The launcher, a pinned file, raises ValueError for an integrity violation and for a
            # failed docker command alike; only the second names the docker command that
            # failed. Anything else, or a failure that left no receipt, fails closed.
            if (isinstance(error, ValueError) and not _DOCKER_FAILURE.match(str(error))) or not (
                destination / "execution.json"
            ).is_file():
                raise
            raise InfrastructureFailure(str(error)) from error
        except isolation.RuntimeDriftError as error:
            # Docker stopped answering the launcher's runtime check, before it launched or wrote
            # a receipt, so this executor records the failure. Any other drift fails closed.
            if not _runtime_unreachable(error) or (destination / "execution.json").exists():
                raise
            reason = f"{error}: {error.__cause__}"
            (destination / PRELAUNCH_FAILURE).write_text(json.dumps({"error": reason}))
            raise InfrastructureFailure(reason) from error
        finally:
            # Mutation keeps status/integrity receipts, never coverage reports or source copies.
            for name in ("before", "after"):
                shutil.rmtree(destination / name, ignore_errors=True)
            for report in destination.glob("report.raw*"):
                report.unlink()
        exit_code = receipt.get("exit_code")
        return Run(
            exit_code=exit_code if isinstance(exit_code, int) else None,
            timed_out=receipt.get("timed_out") is True,
            stdout=str(receipt.get("stdout") or ""),
        )

    return execute


def _runtime_unreachable(error: isolation.RuntimeDriftError) -> bool:
    """Whether the launcher's runtime check failed only because a docker command did not run.

    `assert_runtime` words every failure to read the runtime the same way, and chains the cause.
    A docker command that failed, timed out or could not start is infrastructure. A runtime that
    Docker reported and the check refused, such as another Harbor version or platform, is not.
    """
    cause = error.__cause__
    return str(error) == _RUNTIME_UNVERIFIED and (
        isinstance(cause, (OSError, subprocess.TimeoutExpired))
        or (isinstance(cause, ValueError) and bool(_DOCKER_FAILURE.match(str(cause))))
    )


def _digest(path: Path) -> str:
    return _bytes_digest(path.read_bytes())


def _bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tree_digest(files: Mapping[str, object]) -> str:
    """`inspect_snapshot`'s digest of a tree holding these file digests, without building it."""
    return _bytes_digest(json.dumps(dict(files), sort_keys=True, separators=(",", ":")).encode())


def _check_command(path: str) -> list[str] | None:
    """The invalid-mutant check for a mutated file's language: Go builds, TypeScript type-checks."""
    suffix = PurePosixPath(path).suffix.lower()
    return GO_CHECK if suffix == ".go" else TSC_CHECK if suffix in _TS_SUFFIXES else None


class _Attempt:
    """One attempt's clock, executor and serial receipt names, shared by its lanes.

    Times are whole milliseconds since the attempt started, before site discovery.
    """

    def __init__(
        self, execute: Executor, destination: Path, process_exit: bool, clock: Clock,
        started: int, attempt_ms: int | None,
    ) -> None:
        self.execute, self.destination, self.process_exit = execute, destination, process_exit
        self.clock, self.started, self.attempt_ms = clock, started, attempt_ms
        # Only the serial phase numbers its runs; a lane names its runs by their site.
        self.serial = 0

    def now(self) -> int:
        return elapsed_ms(self.started, self.clock())

    def expired(self, now: int) -> AttemptDeadline:
        return AttemptDeadline(f"the attempt's {self.attempt_ms} ms limit expired at {now} ms")

    def admit(self) -> int:
        """A site's dispatch time; refused once the attempt's limit has passed."""
        now = self.now()
        if self.attempt_ms is not None and now >= self.attempt_ms:
            raise self.expired(now)
        return now

    def kept_limit(self) -> int:
        """The reading that ends the required work; refused if it is past the attempt's limit.

        A call cut by its own cap or its site can still return after the attempt's limit, so
        the last return is checked here, once every call has returned. Exactly at the limit is
        on time.
        """
        now = self.now()
        if self.attempt_ms is not None and now > self.attempt_ms:
            raise self.expired(now)
        return now

    def serial_name(self, label: str) -> str:
        name = f"runs/{self.serial:03d}-{label}"
        self.serial += 1
        return name

    def call(
        self,
        calls: list[dict[str, object]],
        tree: Path,
        command: list[str],
        name: str,
        *,
        label: str,
        cap_ms: int | None,
        deadline_ms: int | None = None,
    ) -> None:
        """Run `command` within its clocks and append its record to `calls`.

        A call with no time left is recorded as not dispatched. Once the attempt's limit cuts a
        call or leaves it no time, the record is kept and AttemptDeadline is raised.
        """
        started = self.now()
        bound, left = call_limit(
            started_ms=started, cap_ms=cap_ms, deadline_ms=deadline_ms, attempt_ms=self.attempt_ms
        )
        record: dict[str, object] = {
            "label": label,
            "started_ms": started,
            "bound": bound,
            "timeout_ms": left,
            "dispatched": left > 0,
        }
        if left <= 0:
            calls.append(record)
            if bound == "attempt":
                raise self.expired(started)
            return
        directory = self.destination / name
        directory.mkdir(parents=True)
        try:
            run = self.execute(tree, command, left, directory)
        except InfrastructureFailure as error:
            error.receipts = name
            raise
        took = self.now() - started
        (directory / "stdout.txt").write_bytes(run.stdout.encode())
        record.update(
            exit_code=run.exit_code,
            timed_out=run.timed_out,
            suite_status=suite_status(run, self.process_exit),
            elapsed_ms=took,
            receipts=name,
            stdout_sha256=_digest(directory / "stdout.txt"),
        )
        calls.append(record)
        if bound == "attempt" and _cut(record) is not None:
            raise self.expired(started + took)


def _in_lanes(lanes: int, count: int, work: Callable[[int, int], None]) -> None:
    """`work(lane, index)` for every index in order, in at most `lanes` threads at once.

    Each lane takes the next index when it is free, so indices start in order and finish in any
    order. The first failure stops new indices; running ones finish, each within its own clocks.
    An integrity failure is raised before an infrastructure one, so the attempt fails closed.
    """
    lock = threading.Lock()
    next_index = 0
    failures: list[BaseException] = []

    def lane(number: int) -> None:
        nonlocal next_index
        while True:
            with lock:
                if failures or next_index >= count:
                    return
                index, next_index = next_index, next_index + 1
            try:
                work(number, index)
            except BaseException as error:  # Re-raised in the caller's thread below.
                with lock:
                    failures.append(error)
                return

    threads = [
        threading.Thread(target=lane, args=(number,), name=f"mutation-lane-{number}")
        for number in range(min(lanes, count))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise next(
            (error for error in failures if not isinstance(error, InfrastructureFailure)),
            failures[0],
        )


def _mutated(tree: Path, site: Site, work: Path) -> tuple[Path, str]:
    target = work / "tree"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(tree, target, symlinks=True)
    path = target / site.path
    data = path.read_bytes()
    if data[site.start : site.end].decode("utf-8") != site.original:
        raise SiteError(f"{site.path}:{site.line}: the site text changed before mutation")
    path.write_bytes(data[: site.start] + site.replacement.encode() + data[site.end :])
    return target, _digest(path)


def _baseline(runs: list[dict[str, object]]) -> bool:
    return all(run.get("suite_status") == 0 for run in runs)


def _green(runs: list[dict[str, object]]) -> bool:
    """Two version 2 baselines that each ran green within their limits."""
    return len(runs) == 2 and all(_cut(run) is None for run in runs) and _baseline(runs)


def _tsc_ran_v2(destination: Path, result: dict[str, object]) -> bool:
    return _cut(result) is None and _tsc_ran(destination, result)


def _slowest(runs: list[dict[str, object]]) -> int:
    """B: the slower of two green version 2 baselines, in milliseconds."""
    return max(int(str(run["elapsed_ms"])) for run in runs)


def _timeout(runs: list[dict[str, object]]) -> int:
    """Version 1's mutant timeout in seconds."""
    slowest = max(float(str(run["elapsed_seconds"])) for run in runs)
    return math.ceil(TIMEOUT_FACTOR * slowest + TIMEOUT_GRACE_SECONDS)


def _source_only_state(before: Path, after: Path, reference: object) -> dict[str, object]:
    """The files the source-only filter restores from before, decided before any run."""
    if not isinstance(reference, list):
        return {
            "status": "unavailable",
            "changed_test_files": None,
            "reason": "the task ships no reference solution to tell its files from tests",
        }
    old = _object(inspect_snapshot(before)["files"])
    new = _object(inspect_snapshot(after)["files"])
    kept = {str(item) for item in reference}
    left_out = sorted(
        path
        for path in set(old) | set(new)
        if old.get(path) != new.get(path) and source_only.left_out(path, kept)
    )
    return {
        "status": "measured",
        "changed_test_files": left_out,
        # Each left-out file's starting digest, or None for a file the attempt added.
        "restored": {path: old.get(path) for path in left_out},
        "baseline": [],
    }


def _source_only_tree(before: Path, after: Path, left_out: list[str], target: Path) -> None:
    """The after tree with each file the source-only filter leaves out restored from before."""
    shutil.copytree(after, target, symlinks=True)
    for path in left_out:
        destination = target / path
        if (before / path).is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(before / path, destination)
        else:
            destination.unlink()


def collect_mutation(
    root: Path,
    before: Path,
    config: dict[str, object],
    destination: Path,
    *,
    execute: Executor | None = None,
    limits: Limits | None = None,
    clock: Clock | None = None,
) -> dict[str, object]:
    """Measure one submission; `root` is a disposable copy of it, never modified.

    Without `execute`, each run is a fresh container of the task's base image
    (`measurement_identity`), and Docker must hold every lane at once.
    """
    if config.get("observer_version") != components.VERSIONS["mutation_observer"]:
        raise components.ObserverError(
            f"mutation observer version {config.get('observer_version')} froze this profile, and "
            f"this PluginBench measures with version {components.VERSIONS['mutation_observer']}: "
            f"{components.OBSERVER_NEXT_STEP}"
        )
    implementation = observer_identity()
    try:
        return _collect(
            root, before, config, destination, execute, limits or Limits(),
            clock or time.monotonic_ns,
        )
    finally:
        if observer_identity() != implementation:
            raise components.ObserverError(
                "the mutation observer's bytes changed during execution: measure again with an "
                "unchanged PluginBench"
            )


def _failure(error: InfrastructureFailure, attempt: _Attempt) -> dict[str, object]:
    return {
        "reason": str(error),
        "receipts": error.receipts,
        "cause": "attempt_deadline" if isinstance(error, AttemptDeadline) else "executor",
        "at_ms": attempt.now(),
    }


def _collect(
    root: Path,
    before: Path,
    config: dict[str, object],
    destination: Path,
    execute: Executor | None,
    limits: Limits,
    clock: Clock,
) -> dict[str, object]:
    started = clock()  # The attempt's clock starts before site discovery.
    coverage = config["coverage"]
    identity = config["native_task_identity"]
    assert isinstance(coverage, dict) and isinstance(identity, dict)
    baseline_ms = (
        limits.baseline_ms if limits.baseline_ms is not None
        else 1000 * int(coverage["timeout_seconds"])
    )
    if execute is None:
        measured = measurement_identity(identity)
        if limits.lanes > 1:
            _check_capacity(measured, limits.lanes)
        execute = native_executor(
            measured, str(coverage["report_path"]), provisioned_tool=coverage.get("provisioned_tool"),
            provisioned_scope=_strings(coverage.get("source_scope") or ["*"])
        )
    attempt = _Attempt(
        execute, destination, _process_exit(config), clock, started, limits.attempt_ms
    )
    change = compare_snapshots(before, root)
    generated = set(_strings(config["generated_files"]))
    changed = sorted(
        line for line in _added_source_lines(change) if line.rsplit(":", 1)[0] not in generated
    )
    files = {line.rsplit(":", 1)[0] for line in changed}
    supported = {path for path in files if PurePosixPath(path).suffix.lower() in _SUFFIXES}
    sites, unparsed = find_sites(root, supported, _object(config["site_finders"]))
    changed_set = set(changed)
    candidates = sorted(
        {site for site in sites if site.coordinate() in changed_set},
        key=lambda site: (site.path, site.start, site.end, OPERATORS.index(site.operator)),
    )
    after_digest = str(change["after_digest"])
    selected = sample(candidates, after_digest, limits.sample_size)
    report: dict[str, object] = {
        "version": VERSION,
        "language": str(config["language"]),
        "suite_status": config["suite_status"],
        "before_digest": change["before_digest"],
        "after_digest": after_digest,
        "operators": list(OPERATORS),
        "sample_size": limits.sample_size,
        "lanes": limits.lanes,
        "clocks": {
            "baseline_ms": baseline_ms,
            "attempt_ms": limits.attempt_ms,
            "timing_boundary": TIMING_BOUNDARY,
        },
        "generated_files": sorted(generated),
        "changed_production_lines": changed,
        "unsupported_files": sorted(files - supported),
        "unparsed_files": unparsed,
        "candidates": [asdict(site) for site in candidates],
        "selected": [asdict(site) for site in selected],
        "baseline": [],
        "check_baseline": None,
        "mutants": [],
        # When every required mutant had settled: later work is diagnostic only.
        "required_settled_ms": None,
        "source_only": _source_only_state(before, root, config.get("source_only_reference_paths")),
        "infrastructure_failure": None,
        "finished_ms": None,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    if not selected:
        # Discovery counts against the attempt; with sites, the first baseline checks it.
        try:
            attempt.kept_limit()
        except AttemptDeadline as error:
            report["infrastructure_failure"] = _failure(error, attempt)
    if selected:
        command = [str(item) for item in coverage["command"]]
        with tempfile.TemporaryDirectory(prefix="pluginbench-mutation-") as temporary:
            work = Path(temporary)
            try:
                _run_required(attempt, root, report, selected, command, baseline_ms, limits, work)
            except InfrastructureFailure as error:
                report["infrastructure_failure"] = _failure(error, attempt)
            else:
                if report["required_settled_ms"] is not None:
                    source = _object(report["source_only"])
                    try:
                        _run_diagnostics(
                            attempt, before, root, report, selected, command, baseline_ms,
                            limits, work,
                        )
                    except InfrastructureFailure as error:
                        # The headline is sealed: only the diagnostic is incomplete.
                        source["interrupted"] = _failure(error, attempt)
    report["finished_ms"] = attempt.now()
    return report


def _run_required(
    attempt: _Attempt,
    root: Path,
    report: dict[str, object],
    selected: list[Site],
    command: list[str],
    baseline_ms: int,
    limits: Limits,
    work: Path,
) -> None:
    """The baselines, serially, then every selected mutant in lanes, recorded into `report`."""
    baseline: list[dict[str, object]] = []
    report["baseline"] = baseline
    for _ in range(2):
        attempt.call(
            baseline, root, command, attempt.serial_name("baseline"), label="baseline",
            cap_ms=baseline_ms,
        )
    if not _green(baseline):
        attempt.kept_limit()
        return
    slowest = _slowest(baseline)
    if any(_check_command(site.path) == TSC_CHECK for site in selected):
        checks: list[dict[str, object]] = []
        try:
            attempt.call(
                checks, root, TSC_CHECK, attempt.serial_name("tsc-baseline"),
                label="tsc-baseline", cap_ms=baseline_ms,
            )
        finally:
            report["check_baseline"] = checks[0] if checks else None
        if not _tsc_ran_v2(attempt.destination, checks[0]):
            attempt.kept_limit()
            return
    records: dict[int, dict[str, object]] = {}

    def measure(lane: int, index: int) -> None:
        _measure_mutant(
            attempt, root, selected[index], index, work / f"lane-{lane}", command, slowest,
            report["check_baseline"], records,
        )

    try:
        _in_lanes(limits.lanes, len(selected), measure)
    finally:
        # In sample order, whatever order the lanes finished in.
        report["mutants"] = [records[index] for index in sorted(records)]
    report["required_settled_ms"] = attempt.kept_limit()


def _namer(prefix: str) -> Callable[[str], str]:
    """Receipt names for one site's runs, numbered within the site."""
    count = 0

    def name(label: str) -> str:
        nonlocal count
        count += 1
        return f"{prefix}/{count - 1:02d}-{label}"

    return name


def _measure_mutant(
    attempt: _Attempt,
    root: Path,
    site: Site,
    index: int,
    scratch: Path,
    command: list[str],
    slowest: int,
    check_baseline: object,
    records: dict[int, dict[str, object]],
) -> None:
    """One mutant: its check, then runs until its outcome is known, within 6B of dispatch."""
    dispatched = attempt.admit()
    tree, mutated = _mutated(root, site, scratch)
    deadline = dispatched + SITE_FACTOR * slowest
    runs: list[dict[str, object]] = []
    record: dict[str, object] = {
        "site": index,
        "mutated_sha256": mutated,
        "dispatched_ms": dispatched,
        "check": None,
        "runs": runs,
        "settled_ms": None,
    }
    records[index] = record
    name = _namer(f"runs/site-{index:03d}")
    check = _check_command(site.path)
    if check is not None:
        checks: list[dict[str, object]] = []
        try:
            attempt.call(
                checks, tree, check, name("check"), label="check", cap_ms=None,
                deadline_ms=deadline,
            )
        finally:
            record["check"] = checks[0] if checks else None
        if _check_state_v2(attempt.destination, checks[0], check_baseline, site) != "valid":
            record["settled_ms"] = attempt.now()
            return
    while site_outcome(runs) is None:
        attempt.call(
            runs, tree, command, name("mutant"), label="mutant",
            cap_ms=None if runs else FIRST_RUN_FACTOR * slowest, deadline_ms=deadline,
        )
    record["settled_ms"] = attempt.now()


def _run_diagnostics(
    attempt: _Attempt,
    before: Path,
    root: Path,
    report: dict[str, object],
    selected: list[Site],
    command: list[str],
    baseline_ms: int,
    limits: Limits,
    work: Path,
) -> None:
    """The source-only suite on each kill, after every required mutant has settled."""
    source = _object(report["source_only"])
    if source["status"] != "measured" or not source["changed_test_files"]:
        return
    kills = [
        record for record in _runs(report["mutants"]) if site_outcome(_runs(record["runs"])) in KILLS
    ]
    if not kills:
        return
    tree = work / "source-only"
    _source_only_tree(before, root, _strings(source["changed_test_files"]), tree)
    baseline: list[dict[str, object]] = []
    source["baseline"] = baseline
    for _ in range(2):
        attempt.call(
            baseline, tree, command, attempt.serial_name("source-only-baseline"),
            label="source-only-baseline", cap_ms=baseline_ms,
        )
    if not _green(baseline):
        attempt.kept_limit()
        return
    slowest = _slowest(baseline)

    def measure(lane: int, number: int) -> None:
        record = kills[number]
        index = int(str(record["site"]))
        site = selected[index]
        placed = tree / site.path
        if not placed.is_file() or placed.read_bytes() != (root / site.path).read_bytes():
            record["source_only"] = {"placed": False}
            return
        dispatched = attempt.admit()
        mutant, mutated = _mutated(tree, site, work / f"lane-{lane}")
        runs: list[dict[str, object]] = []
        diagnostic: dict[str, object] = {
            "placed": True,
            "mutated_sha256": mutated,
            "dispatched_ms": dispatched,
            "runs": runs,
            "settled_ms": None,
        }
        record["source_only"] = diagnostic
        name = _namer(f"runs/source-only-site-{index:03d}")
        deadline = dispatched + SITE_FACTOR * slowest
        while site_outcome(runs) is None:
            attempt.call(
                runs, mutant, command, name("mutant"), label="source-only-mutant",
                cap_ms=None if runs else FIRST_RUN_FACTOR * slowest, deadline_ms=deadline,
            )
        diagnostic["settled_ms"] = attempt.now()

    _in_lanes(limits.lanes, len(kills), measure)
    attempt.kept_limit()


def _stdout(destination: Path, result: dict[str, object]) -> str:
    """A run's saved output, refused if it differs from the digest its record binds."""
    path = destination / str(result["receipts"]) / "stdout.txt"
    if not path.resolve().is_relative_to(destination.resolve()) or _digest(path) != result.get(
        "stdout_sha256"
    ):
        raise ValueError("a mutation run's saved output differs from its record")
    return path.read_bytes().decode()


def _tsc_ran(destination: Path, result: dict[str, object]) -> bool:
    """Whether tsc ran on the unmutated tree to a known result: clean, or with diagnostics."""
    exit_code = result["exit_code"]
    if result["timed_out"] or exit_code is None or exit_code in _NOT_RUNNABLE:
        return False
    return exit_code == 0 or bool(tsc_errors(_stdout(destination, result)))


def _check_state(destination: Path, check: object, baseline: object, site: Site) -> str:
    """valid, invalid or error for one mutant's Go build or tsc check.

    A check that could not run, such as a toolchain missing from the image, is an error, so the
    attempt is incomplete rather than its mutant silently excluded.
    """
    result = _object(check)
    exit_code = result["exit_code"]
    if result["timed_out"] or exit_code is None or exit_code in _NOT_RUNNABLE:
        return "error"
    if _check_command(site.path) == GO_CHECK:
        return "valid" if exit_code == 0 else "invalid"
    errors = tsc_errors(_stdout(destination, result), site)
    if exit_code and not errors:
        return "error"
    known = tsc_errors(_stdout(destination, _object(baseline)), site)
    return "invalid" if errors - known else "valid"


def _check_state_v2(destination: Path, check: object, baseline: object, site: Site) -> str:
    """A version 2 check: unconfirmed or interrupted when a clock stopped it, else as version 1.

    A check has no cap of its own, so only the site deadline or the attempt's limit stops it.
    """
    result = _object(check)
    if (cut := _cut(result)) is not None:
        return _CUT_LATER[cut]
    return _check_state(destination, result, baseline, site)


# --- Metrics -------------------------------------------------------------------------------


def _process_exit(config: Mapping[str, object]) -> bool:
    rule = config["suite_status"]
    if rule not in ("marker", "process_exit"):
        raise ValueError("mutation suite status rule must be marker or process_exit")
    return rule == "process_exit"


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("mutation report requires JSON objects")
    return value


def _runs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("mutation report requires run lists")
    return [_object(item) for item in value]


class _Receipts:
    """Checks run records against their saved output and the launcher's execution.json.

    A version 1 run used the profile's own task image; a version 2 run used
    `measurement_identity`: the base image, at two CPUs and 2,048 MiB.
    """

    def __init__(self, directory: Path, config: dict[str, object], version: str) -> None:
        self.directory, self.version = directory, version
        self.process_exit = _process_exit(config)
        identity = _object(config["native_task_identity"])
        self.resources: dict[str, object] | None = None
        if version == VERSION:
            identity = measurement_identity(identity)
            self.resources = _object(identity["resources"])
        self.image = _object(identity["image_identity"])["image_id"]
        self.seen: set[str] = set()

    def _own(self, receipts: object) -> Path:
        """A run's receipt directory, named as its report version names it; no two runs share
        one.

        Only that one spelling is accepted, and it must not pass through a link, so the name
        alone tells two directories apart.
        """
        name = str(receipts)
        path = self.directory / name
        if (
            not _RECEIPTS[self.version].fullmatch(name)
            or name in self.seen
            or path.resolve() != self.directory.resolve() / name
        ):
            raise ValueError("a mutation run does not have its own receipt directory")
        self.seen.add(name)
        return path

    def receipt(self, receipts: object) -> dict[str, object]:
        """A run's launcher receipt."""
        return _object(json.loads((self._own(receipts) / "execution.json").read_text()))

    def _check(self, item: dict[str, object], command: list[str], tree: str) -> None:
        """One run record against its saved output and its launcher receipt."""
        stdout = _stdout(self.directory, item)
        exit_code = item["exit_code"]
        recorded = Run(
            exit_code=exit_code if isinstance(exit_code, int) else None,
            timed_out=item["timed_out"] is True,
            stdout=stdout,
        )
        if suite_status(recorded, self.process_exit) != item["suite_status"]:
            raise ValueError("a mutation run's suite status differs from its saved output")
        receipt = self.receipt(item["receipts"])
        if (
            receipt.get("command") != command
            or receipt.get("image_id") != self.image
            or receipt.get("before_digest") != tree
            or receipt.get("exit_code") != exit_code
            or receipt.get("timed_out") is not item["timed_out"]
            or str(receipt.get("stdout") or "") != stdout
            or (self.resources is not None and receipt.get("resources") != self.resources)
        ):
            raise ValueError("a mutation run's record differs from its launcher receipt")

    def runs(
        self, value: object, command: list[str], tree: str, timeout: int
    ) -> list[dict[str, object]]:
        """Version 1 run records that re-derive from their output and match their receipts.

        `tree` is the digest of the tree each run must have measured, and `timeout` its limit.
        """
        runs = _runs(value)
        for item in runs:
            self._check(item, command, tree)
            if item["timeout_seconds"] != timeout:
                raise ValueError("a mutation run's record differs from its launcher receipt")
        return runs

    def calls(
        self, value: object, command: list[str], tree: str, clocks: _CallClocks
    ) -> list[dict[str, object]]:
        """Version 2 call records: each started within its clocks, in order, and each that ran
        matches its saved output and launcher receipt.

        `clocks(position)` gives the cap and site deadline of the call at that position.
        """
        calls = _runs(value)
        earliest = 0
        for position, item in enumerate(calls):
            cap, deadline, attempt_ms = clocks(position)
            started = _whole(item["started_ms"])
            bound, left = call_limit(
                started_ms=started, cap_ms=cap, deadline_ms=deadline, attempt_ms=attempt_ms
            )
            if (
                started < earliest
                or item["bound"] != bound
                or item["timeout_ms"] != left
                or item["dispatched"] is not (left > 0)
            ):
                raise ValueError("a mutation run's clocks differ from its recorded start")
            if left > 0:
                self._check(item, command, tree)
                earliest = started + _whole(item["elapsed_ms"])
            elif set(item) != {"label", "started_ms", "bound", "timeout_ms", "dispatched"}:
                raise ValueError("a mutation run that never started records a result")
        return calls

    def infrastructure(self, value: object, attempt_ms: object = None) -> str:
        """An infrastructure failure's reason, as the failing run's receipt records it.

        A version 2 attempt-limit expiry has no failing run: it needs the limit it names.
        """
        record = _object(value)
        reason = str(record["reason"])
        if self.version == VERSION and record.get("cause") == "attempt_deadline":
            if record["receipts"] is not None or attempt_ms is None:
                raise ValueError("a mutation attempt-limit expiry differs from its clocks")
            return reason
        if self.version == VERSION and record.get("cause") != "executor":
            raise ValueError("a mutation infrastructure failure names no known cause")
        directory = self._own(record["receipts"])
        launched, prelaunch = directory / "execution.json", directory / PRELAUNCH_FAILURE
        if launched.is_file() == prelaunch.is_file():
            raise ValueError("a mutation infrastructure failure needs exactly one receipt")
        if prelaunch.is_file():
            if _object(json.loads(prelaunch.read_text())).get("error") != reason:
                raise ValueError(
                    "a mutation infrastructure failure differs from its prelaunch record"
                )
            return reason
        receipt = _object(json.loads(launched.read_text()))
        cleanup = receipt.get("cleanup")
        if reason not in (
            receipt.get("error"),
            cleanup.get("error") if isinstance(cleanup, dict) else None,
        ):
            raise ValueError("a mutation infrastructure failure differs from its launcher receipt")
        return reason


# A version 2 call's cap, site deadline and attempt limit, by its position among its site's runs.
_CallClocks = Callable[[int], tuple[int | None, int | None, int | None]]


def _whole(value: object) -> int:
    """A recorded millisecond reading: a whole number, never negative."""
    if type(value) is not int or value < 0:
        raise ValueError("mutation clocks are whole milliseconds")
    return value


def _settled(runs: list[dict[str, object]]) -> str:
    """A version 1 mutant's outcome; its runs must end exactly where the outcome became known."""
    state = outcome(runs)
    if state is None or any(outcome(runs[:count]) is not None for count in range(1, len(runs))):
        raise ValueError("a mutant's runs do not end where its outcome is known")
    return state


def _sites(value: object) -> list[Site]:
    if not isinstance(value, list):
        raise ValueError("mutation report requires site lists")
    sites = []
    for record in value:
        item = _object(record)
        site = Site(
            path=str(item.get("path")),
            line=int(str(item.get("line"))),
            column=int(str(item.get("column"))),
            operator=str(item.get("operator")),
            start=int(str(item.get("start"))),
            end=int(str(item.get("end"))),
            original=str(item.get("original")),
            replacement=str(item.get("replacement")),
        )
        if asdict(site) != item:
            raise ValueError("mutation site record is malformed")
        sites.append(site)
    return sites


def _mutant_digest(snapshot: Path, site: Site) -> str:
    """The digest of the site's file once mutated, from the measured snapshot's bytes."""
    data = (snapshot / site.path).read_bytes()
    if data[site.start : site.end] != site.original.encode():
        raise ValueError("a mutation site is not in the measured snapshot")
    return _bytes_digest(data[: site.start] + site.replacement.encode() + data[site.end :])


@dataclass(frozen=True)
class _Measured:
    """What a report's runs are checked against: the profile, the snapshot and the receipts."""

    directory: Path
    snapshot: Path
    files: dict[str, object]
    after: str
    command: list[str]
    suite_timeout: int
    receipts: _Receipts


# The outcome counts a report publishes, and the outcome each counts.
_COUNTS = (
    ("killed", "killed"),
    ("timed_out", "timeout"),
    ("survived", "survived"),
    ("invalid", "invalid"),
    ("flaky", "flaky"),
    ("errors", "error"),
)
_COUNTS_V2 = (
    ("killed", "killed"),
    ("timed_out", "timeout_kill"),
    ("survived", "survived"),
    ("invalid", "invalid"),
    ("flaky", "flaky"),
    ("unconfirmed", "unconfirmed"),
    ("errors", "error"),
)


def mutation_metrics(
    report: dict[str, object], directory: Path, *, config: dict[str, object], snapshot: Path
) -> dict[str, object]:
    """Every outcome and count, derived from the report's raw sites and runs.

    `directory` holds the report's run receipts, `config` is its frozen profile and `snapshot`
    the measured submission. Each run must match its launcher receipt: the profile's command
    and image, and the tree that run measured, which is rebuilt here from the snapshot. A
    version 1 and a version 2 report decode alike, each by its own rules.
    """
    version = report.get("version")
    if version not in (V1, VERSION):
        raise ValueError("mutation report version differs from this observer")
    assert isinstance(version, str)
    if report.get("suite_status") != config.get("suite_status"):
        raise ValueError("mutation report's suite status rule differs from its frozen profile")
    size = V1_SAMPLE_SIZE if version == V1 else report.get("sample_size")
    if type(size) is not int or size < 1:
        raise ValueError("mutation report's sample size is not a positive whole number")
    candidates = _sites(report["candidates"])
    selected = _sites(report["selected"])
    if selected != sample(candidates, str(report["after_digest"]), size):
        raise ValueError("selected mutants are not the preregistered sample of the candidates")
    if not set(site.coordinate() for site in candidates) <= set(
        _strings(report["changed_production_lines"])
    ):
        raise ValueError("a mutation candidate is not on a changed production line")
    files = _object(inspect_snapshot(snapshot)["files"])
    coverage = _object(config["coverage"])
    measured = _Measured(
        directory=directory,
        snapshot=snapshot,
        files=files,
        after=_tree_digest(files),
        command=_strings(coverage["command"]),
        suite_timeout=int(str(coverage["timeout_seconds"])),
        receipts=_Receipts(directory, config, version),
    )
    if measured.after != report["after_digest"]:
        raise ValueError("mutation report differs from its measured snapshot")
    candidate_lines = sorted({site.coordinate() for site in candidates})
    unsupported = _strings(report["unsupported_files"])
    result: dict[str, object] = {
        "before_digest": report["before_digest"],
        "method": METHOD[version],
        "limitations": LIMITATIONS[version],
        "candidate_count": len(candidates),
        "candidate_line_count": len(candidate_lines),
        "candidate_lines": candidate_lines,
        "changed_production_line_count": len(_strings(report["changed_production_lines"])),
        "selected": len(selected),
        "unparsed_files": report["unparsed_files"],
        # Changed production files in a language with no site finder: they hold no sites.
        "unsupported_files": unsupported,
        "unsupported_file_count": len(unsupported),
    }
    if version == VERSION:
        return _metrics_v2(report, measured, selected, result)
    unavailable: str | None = None
    incomplete = sorted(_object(report["unparsed_files"]))
    rows: list[dict[str, object]] = []
    share = _share_state(_object(report["source_only"]))
    if report["infrastructure_failure"] is not None:
        # Checked first: the runs before the failure are partial, whatever they show.
        incomplete.append(
            "infrastructure failure: "
            + measured.receipts.infrastructure(report["infrastructure_failure"])
        )
    elif not selected:
        # With an unparsed changed file the site population itself is unknown.
        if not incomplete:
            unavailable = "no candidate sites on changed production lines"
    else:
        baseline = measured.receipts.runs(
            report["baseline"], measured.command, measured.after, measured.suite_timeout
        )
        if len(baseline) != 2 or not _baseline(baseline):
            unavailable = "baseline red or unstable"
        elif problem := _tsc_baseline_problem(measured, report["check_baseline"], selected):
            incomplete.append(problem)
        else:
            share, rows = _mutant_rows(measured, report, selected, share, _timeout(baseline))
    counts = {key: sum(row["outcome"] == state for row in rows) for key, state in _COUNTS}
    if counts["errors"]:
        incomplete.append(f"{counts['errors']} mutant runs reported no suite status")
    killed = counts["killed"] + counts["timed_out"]
    valid = killed + counts["survived"]
    result.update(counts)
    result.update(killed=killed, valid=valid, mutants=rows)
    if unavailable is None and not incomplete and not valid:
        unavailable = "no valid mutants"
    # An incomplete attempt stays incomplete whatever else made it unavailable.
    status = "incomplete" if incomplete else "unavailable" if unavailable else "available"
    result["status"] = status
    result["reason"] = "; ".join(incomplete) if incomplete else unavailable
    result["bug_catch_percent"] = round(100 * killed / valid, 6) if status == "available" else None
    result["test_change_kill_share"] = _share(share, rows, valid, status)
    return result


def _tsc_baseline_problem(
    measured: _Measured,
    tsc: object,
    selected: list[Site],
    validate: Callable[[object], object] | None = None,
    ran: Callable[[Path, dict[str, object]], bool] = _tsc_ran,
) -> str | None:
    """Why the TypeScript mutants cannot be checked, if so; the tsc baseline runs only for them.

    `validate` checks the tsc run's record and `ran` whether it ran to a known result; version
    1's rules are the defaults.
    """
    if any(_check_command(site.path) == TSC_CHECK for site in selected) != (tsc is not None):
        raise ValueError("the tsc baseline run differs from the TypeScript mutants it serves")
    if tsc is None:
        return None
    if validate is None:
        measured.receipts.runs([tsc], TSC_CHECK, measured.after, measured.suite_timeout)
    else:
        validate(tsc)
    if not ran(measured.directory, _object(tsc)):
        return "tsc did not report diagnostics on the unmutated tree"
    return None


def _mutant_rows(
    measured: _Measured,
    report: dict[str, object],
    selected: list[Site],
    share: dict[str, object],
    timeout: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Version 1: each selected mutant's outcome row, and the share state after the source-only
    baseline."""
    source = _object(report["source_only"])
    source_files = _source_files(measured, source)
    source_timeout = 0
    if share["status"] == "available" and share["changed_test_files"]:
        source_baseline = measured.receipts.runs(
            source["baseline"],
            measured.command,
            _tree_digest(source_files),
            measured.suite_timeout,
        )
        if len(source_baseline) != 2 or not _baseline(source_baseline):
            share = {"status": "unavailable", "reason": "source-only baseline red or unstable"}
        else:
            source_timeout = _timeout(source_baseline)
    mutants = _runs(report["mutants"])
    if [item["site"] for item in mutants] != list(range(len(selected))):
        raise ValueError("mutation report does not record every selected mutant once")
    rows: list[dict[str, object]] = []
    for record in mutants:
        site = selected[int(str(record["site"]))]
        mutated = _mutant_digest(measured.snapshot, site)
        if record["mutated_sha256"] != mutated:
            raise ValueError("a mutant's recorded bytes are not its site's mutation")
        tree = _tree_digest({**measured.files, site.path: mutated})
        check = _check_command(site.path)
        if (check is None) != (record["check"] is None):
            raise ValueError("a mutant's build or tsc check is missing or unexpected")
        state = "valid"
        if check is not None:
            measured.receipts.runs([record["check"]], check, tree, measured.suite_timeout)
            state = _check_state(
                measured.directory, record["check"], report["check_baseline"], site
            )
        if state == "valid":
            runs = measured.receipts.runs(record["runs"], measured.command, tree, timeout)
            state = _settled(runs)
        row: dict[str, object] = {
            "path": site.path,
            "line": site.line,
            "operator": site.operator,
            "outcome": state,
        }
        # Source-only runs exist only for kills, in attempts that changed test files.
        if state in ("killed", "timeout") and source_timeout:
            row["source_only"] = _source_only_outcome(
                measured, record.get("source_only"), site, mutated, source_files, source_timeout
            )
        rows.append(row)
    return share, rows


def _source_files(measured: _Measured, source: dict[str, object]) -> dict[str, object]:
    """The source-only tree's files: the measured snapshot with the left-out files restored."""
    restored = _object(source.get("restored") or {})
    files = {path: digest for path, digest in measured.files.items() if path not in restored}
    files.update({path: digest for path, digest in restored.items() if digest is not None})
    return files


def _metrics_v2(
    report: dict[str, object],
    measured: _Measured,
    selected: list[Site],
    result: dict[str, object],
) -> dict[str, object]:
    """A version 2 report's outcomes, counts and the partition of its sample.

    The partition is settled, interrupted (started, unsettled) and unstarted site indices. An
    incomplete attempt publishes its counts only as `partial_counts`, never as the pooled
    `killed` and `valid`.
    """
    clocks = _object(report["clocks"])
    baseline_ms = _whole(clocks["baseline_ms"])
    attempt_ms = clocks["attempt_ms"]
    if (
        baseline_ms < 1
        or not (attempt_ms is None or (type(attempt_ms) is int and attempt_ms >= 1))
        or clocks["timing_boundary"] != TIMING_BOUNDARY
        or type(report["lanes"]) is not int
        or int(str(report["lanes"])) < 1
    ):
        raise ValueError("mutation report's clocks or lanes are malformed")
    attempt = attempt_ms if isinstance(attempt_ms, int) else None
    unavailable: str | None = None
    incomplete = sorted(_object(report["unparsed_files"]))
    failure = report["infrastructure_failure"]
    if failure is not None:
        incomplete.append(
            "infrastructure failure: " + measured.receipts.infrastructure(failure, attempt)
        )
    rows: list[dict[str, object]] = []
    states: dict[int, str | None] = {}
    share = _share_state(_object(report["source_only"]))
    required = False
    if not selected:
        if not incomplete:
            unavailable = "no candidate sites on changed production lines"
    else:
        baseline = measured.receipts.calls(
            report["baseline"], measured.command, measured.after,
            lambda _: (baseline_ms, None, attempt),
        )
        green = _green(baseline)
        cut_before_tsc = (
            failure is not None
            and report["check_baseline"] is None
            and any(_check_command(site.path) == TSC_CHECK for site in selected)
        )
        if failure is None and not green:
            unavailable = "baseline red or unstable"
        elif green and not cut_before_tsc:
            problem = _tsc_baseline_problem(
                measured, report["check_baseline"], selected,
                lambda tsc: measured.receipts.calls(
                    [tsc], TSC_CHECK, measured.after, lambda _: (baseline_ms, None, attempt)
                ),
                _tsc_ran_v2,
            )
            if problem:
                incomplete.append(problem)
            else:
                required = True
                states, rows = _mutant_rows_v2(
                    measured, report, selected, _slowest(baseline), attempt, failure is not None
                )
    if (report["required_settled_ms"] is not None) != (required and failure is None):
        raise ValueError("mutation report's required phase differs from its mutants")
    if failure is None and attempt is not None and _required_end(report) > attempt:
        raise ValueError("an uninterrupted mutation attempt overran its limit")
    settled = sorted(index for index, state in states.items() if state is not None)
    counts = {key: sum(row["outcome"] == state for row in rows) for key, state in _COUNTS_V2}
    if counts["errors"]:
        incomplete.append(f"{counts['errors']} mutant runs reported no suite status")
    killed = counts["killed"] + counts["timed_out"]
    valid = killed + counts["survived"]
    if unavailable is None and not incomplete and not valid:
        unavailable = "no valid mutants"
    status = "incomplete" if incomplete else "unavailable" if unavailable else "available"
    result["sample_size"] = report["sample_size"]
    result["sites"] = {
        "settled": settled,
        "interrupted": sorted(index for index, state in states.items() if state is None),
        "unstarted": [index for index in range(len(selected)) if index not in states],
    }
    if status == "incomplete":
        # Partial: no consumer may pool these with a complete attempt's counts.
        result.update({key: None for key, _ in _COUNTS_V2}, killed=None, valid=None)
        result["partial_counts"] = {**counts, "kills": killed, "valid": valid}
    else:
        result.update(counts, killed=killed, valid=valid)
        result["partial_counts"] = None
    result["mutants"] = rows
    result["status"] = status
    result["reason"] = "; ".join(incomplete) if incomplete else unavailable
    result["bug_catch_percent"] = round(100 * killed / valid, 6) if status == "available" else None
    if required and failure is None:
        share = _source_only_v2(measured, report, selected, share, rows, attempt)
    result["test_change_kill_share"] = _share(share, rows, valid, status)
    return result


def _last_return(calls: list[dict[str, object]], readings: list[object]) -> int:
    """The latest of these calls' ends and these settlement readings (None when unset)."""
    ends = [
        _whole(call["started_ms"]) + (_whole(call["elapsed_ms"]) if call["dispatched"] else 0)
        for call in calls
    ]
    return max([0, *ends, *(_whole(value) for value in readings if value is not None)])


def _required_end(report: dict[str, object]) -> int:
    """When the required work last returned: its sealing reading, or its latest call's end.

    The source-only diagnostic runs after the seal and is not part of it.
    """
    calls = _runs(report["baseline"])
    if report["check_baseline"] is not None:
        calls.append(_object(report["check_baseline"]))
    for record in _runs(report["mutants"]):
        calls.extend(_runs([record["check"]] if record["check"] is not None else []))
        calls.extend(_runs(record["runs"]))
    return _last_return(calls, [report["required_settled_ms"]])


def _mutant_rows_v2(
    measured: _Measured,
    report: dict[str, object],
    selected: list[Site],
    slowest: int,
    attempt_ms: int | None,
    interrupted: bool,
) -> tuple[dict[int, str | None], list[dict[str, object]]]:
    """Each started mutant's state (None while unsettled) and the settled mutants' rows.

    Lanes start sites in sample order, so a complete attempt records every site once; an
    interrupted one records the sites it started, in order.
    """
    mutants = _runs(report["mutants"])
    if any(type(item["site"]) is not int for item in mutants):
        raise ValueError("mutation report does not record every selected mutant once")
    indices = [int(str(item["site"])) for item in mutants]
    if (
        indices != list(range(len(selected)))
        if not interrupted
        else indices != sorted(set(indices)) or not set(indices) <= set(range(len(selected)))
    ):
        raise ValueError("mutation report does not record every selected mutant once")
    states: dict[int, str | None] = {}
    rows: list[dict[str, object]] = []
    for record in mutants:
        index = int(str(record["site"]))
        site = selected[index]
        mutated = _mutant_digest(measured.snapshot, site)
        if record["mutated_sha256"] != mutated:
            raise ValueError("a mutant's recorded bytes are not its site's mutation")
        tree = _tree_digest({**measured.files, site.path: mutated})
        deadline = _whole(record["dispatched_ms"]) + SITE_FACTOR * slowest
        state: str | None = "valid"
        check = _check_command(site.path)
        if check is None and record["check"] is not None:
            raise ValueError("a mutant's build or tsc check is missing or unexpected")
        if check is not None:
            if record["check"] is None:
                if not interrupted or record["runs"]:
                    raise ValueError("a mutant's build or tsc check is missing or unexpected")
                state = None
            else:
                [result] = measured.receipts.calls(
                    [record["check"]], check, tree, lambda _: (None, deadline, attempt_ms)
                )
                state = _check_state_v2(
                    measured.directory, result, report["check_baseline"], site
                )
                if state != "valid" and record["runs"]:
                    raise ValueError("a mutant ran after its check settled it")
        if state == "valid":
            runs = measured.receipts.calls(
                record["runs"], measured.command, tree,
                lambda position: (
                    FIRST_RUN_FACTOR * slowest if position == 0 else None, deadline, attempt_ms
                ),
            )
            state = site_outcome(runs)
            if any(site_outcome(runs[:count]) is not None for count in range(1, len(runs))):
                raise ValueError("a mutant's runs do not end where its outcome is known")
        if state == "interrupted":
            state = None
        if (state is None) != (record["settled_ms"] is None) or (state is None and not interrupted):
            raise ValueError("a mutant's settlement differs from its runs")
        states[index] = state
        if state is not None:
            rows.append({
                "site": index,
                "path": site.path,
                "line": site.line,
                "operator": site.operator,
                "outcome": state,
            })
    return states, rows


def _source_only_v2(
    measured: _Measured,
    report: dict[str, object],
    selected: list[Site],
    share: dict[str, object],
    rows: list[dict[str, object]],
    attempt_ms: int | None,
) -> dict[str, object]:
    """Each kill's source-only outcome, onto its row, and the share state after the source-only
    baseline. The diagnostic runs only after every required mutant settled, so cutting it never
    changes the headline."""
    source = _object(report["source_only"])
    if share["status"] != "available" or not share["changed_test_files"]:
        return share
    clocks = _object(report["clocks"])
    baseline_ms = _whole(clocks["baseline_ms"])
    files = _source_files(measured, source)
    kills = [row for row in rows if row["outcome"] in KILLS]
    interruption = source.get("interrupted")
    if interruption is not None:
        measured.receipts.infrastructure(interruption, attempt_ms)
    baseline = measured.receipts.calls(
        source["baseline"], measured.command, _tree_digest(files),
        lambda _: (baseline_ms, None, attempt_ms),
    )
    cut: dict[str, object] = {"status": "incomplete", "reason": "the source-only diagnostic was cut"}

    def kept_limit(calls: list[dict[str, object]], readings: list[object]) -> None:
        if attempt_ms is not None and _last_return(calls, readings) > attempt_ms:
            raise ValueError("an uninterrupted source-only diagnostic overran the attempt limit")

    if not kills:
        if baseline:
            raise ValueError("a source-only baseline ran with no kill to diagnose")
        return share
    if not _green(baseline):
        if interruption is not None:
            return cut
        kept_limit(baseline, [])
        return {"status": "unavailable", "reason": "source-only baseline red or unstable"}
    slowest = _slowest(baseline)
    records = {int(str(item["site"])): item for item in _runs(report["mutants"])}
    for row in kills:
        index = int(str(row["site"]))
        record = records[index]
        if "source_only" not in record:
            if interruption is None:
                raise ValueError("a kill has no source-only diagnostic")
            row["source_only"] = "not measured"
            continue
        row["source_only"] = _source_only_outcome_v2(
            measured, record["source_only"], selected[index], files, slowest, attempt_ms,
            interruption is not None,
        )
    if interruption is not None:
        return cut  # Even when every kill has an outcome: the attempt overran its limit.
    diagnostics = [
        _object(record["source_only"]) for record in records.values()
        if "source_only" in record and _object(record["source_only"])["placed"] is True
    ]
    kept_limit(
        baseline + [run for item in diagnostics for run in _runs(item["runs"])],
        [item["settled_ms"] for item in diagnostics],
    )
    return share


def _source_only_outcome_v2(
    measured: _Measured,
    value: object,
    site: Site,
    source_files: dict[str, object],
    slowest: int,
    attempt_ms: int | None,
    interrupted: bool,
) -> str:
    record = _object(value)
    placed = source_files.get(site.path) == measured.files[site.path]
    if record.get("placed") is not placed:
        raise ValueError("a source-only mutant's placement differs from its source-only tree")
    if not placed:
        return "not placed"
    mutated = _mutant_digest(measured.snapshot, site)
    if record["mutated_sha256"] != mutated:
        raise ValueError("a source-only mutant's bytes are not its site's mutation")
    tree = _tree_digest({**source_files, site.path: mutated})
    deadline = _whole(record["dispatched_ms"]) + SITE_FACTOR * slowest
    runs = measured.receipts.calls(
        record["runs"], measured.command, tree,
        lambda position: (
            FIRST_RUN_FACTOR * slowest if position == 0 else None, deadline, attempt_ms
        ),
    )
    state = site_outcome(runs)
    if any(site_outcome(runs[:count]) is not None for count in range(1, len(runs))):
        raise ValueError("a mutant's runs do not end where its outcome is known")
    if state is None or state == "interrupted":
        if not interrupted or record["settled_ms"] is not None:
            raise ValueError("a source-only mutant's settlement differs from its runs")
        return "interrupted"
    if record["settled_ms"] is None:
        raise ValueError("a source-only mutant's settlement differs from its runs")
    return state


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("mutation report requires string lists")
    return value


def _share_state(source: dict[str, object]) -> dict[str, object]:
    """Whether the test-change kill share can be measured, decided before any run."""
    if source["status"] != "measured":
        return {"status": "unavailable", "reason": str(source.get("reason"))}
    changed = _strings(source["changed_test_files"])
    if sorted(_object(source["restored"])) != changed:
        raise ValueError("the source-only tree does not restore exactly the changed test files")
    return {"status": "available", "changed_test_files": changed}


def _source_only_outcome(
    measured: _Measured,
    value: object,
    site: Site,
    mutated: str,
    source_files: dict[str, object],
    timeout: int,
) -> str:
    record = _object(value)
    placed = source_files.get(site.path) == measured.files[site.path]
    if record.get("placed") is not placed:
        raise ValueError("a source-only mutant's placement differs from its source-only tree")
    if not placed:
        return "not placed"
    if record["mutated_sha256"] != mutated:
        raise ValueError("a source-only mutant's bytes are not its site's mutation")
    tree = _tree_digest({**source_files, site.path: mutated})
    return _settled(measured.receipts.runs(record["runs"], measured.command, tree, timeout))


# Source-only outcomes that leave a kill's need for the test changes unknown.
_UNSURE = ("flaky", "error", "not placed", "unconfirmed", "interrupted", "not measured")


def _share(
    state: dict[str, object], rows: list[dict[str, object]], valid: int, headline: str
) -> dict[str, object]:
    """Kills only the attempt's test changes made, over valid mutants.

    Decided apart from the headline: with no changed test file no kill needed one, so the share
    is a measured 0 even when the bug-catch rate is unavailable.
    """
    if state["status"] != "available":
        return state
    if not _strings(state["changed_test_files"]):
        return {**state, "kills_needing_test_changes": 0, "percent": 0.0}
    if headline != "available":
        return {"status": headline, "reason": "the bug-catch rate is not available"}
    outcomes = [str(row["source_only"]) for row in rows if "source_only" in row]
    if unsure := sorted({item for item in outcomes if item in _UNSURE}):
        return {
            "status": "incomplete",
            "reason": "source-only suite outcome unknown for a mutant: " + ", ".join(unsure),
        }
    needing = sum(item == "survived" for item in outcomes)
    return {
        **state,
        "kills_needing_test_changes": needing,
        "percent": round(100 * needing / valid, 6),
    }


def _decision_line_coverage(
    metrics: dict[str, object], coverage: dict[str, object]
) -> dict[str, object]:
    """Execution of all candidate lines before sampling, independent of mutant outcomes."""
    if _object(metrics.get("unparsed_files", {})):
        return {"status": "incomplete", "reason": "mutation candidate population is incomplete"}
    candidates = set(_strings(metrics["candidate_lines"]))
    if not candidates:
        return {"status": "unavailable", "reason": "no candidate lines on changed production lines"}
    lines = _object(coverage.get("coordinates") or {}).get("line")
    if not isinstance(lines, dict):
        return {"status": "unavailable", "reason": "the submission has no line coverage"}
    files = {line.rsplit(":", 1)[0] for line in candidates}
    unmeasured = sorted(set(_strings(coverage.get("unmeasured_source_files", []))) & files)
    limits = _object(coverage.get("unavailable_populations", {}))
    line_limit = _object(limits.get("lines", {}))
    unlined = sorted(set(_strings(line_limit.get("files", []))) & files)
    if unmeasured or unlined:
        return {
            "status": "incomplete",
            "reason": "candidate source files missing line coverage: "
            + ", ".join(sorted(set(unmeasured + unlined))),
        }
    # Coverage tools record a multi-line statement on one line, so a site on a continuation
    # line has no coverage coordinate. It leaves the denominator rather than counting as missed.
    eligible = candidates & set(_strings(lines["total"]))
    not_in_population = len(candidates - eligible)
    if not eligible:
        return {
            "status": "unavailable",
            "reason": "no candidate line is in the line coverage population",
            "not_in_population": not_in_population,
        }
    covered = len(eligible & set(_strings(lines["covered"])))
    return {
        "status": "available",
        "covered": covered,
        "total": len(eligible),
        "not_in_population": not_in_population,
        "percent": round(100 * covered / len(eligible), 6),
        "method": "unique changed production lines holding at least one M1 candidate site, before sampling, that the line coverage population holds, intersected with the submission's executed line coordinates",
        "limitations": "execution only, not assertion strength or branch completeness; candidate lines without a coverage coordinate, such as continuation lines of a multi-line statement, are excluded and counted; risky-logic catch rate is the M1 bug-catch rate",
    }


def compare_mutation(
    mutation: dict[str, object],
    coverage: dict[str, object] | None,
    change: dict[str, object] | None,
) -> dict[str, object]:
    """The after-stage mutation result, bound to the attempt's diff, with the execution split."""
    if change is None:
        return {"status": "unavailable", "reason": "bound source diff missing"}
    metrics = _object(mutation.get("metrics"))
    if metrics["before_digest"] != change.get("before_digest"):
        raise ValueError("mutation report does not bind the attempt's starting source")
    candidates = set(_strings(metrics["candidate_lines"]))
    if not candidates <= _added_source_lines(change):
        raise ValueError("mutation candidates are not on the attempt's changed production lines")
    result = {
        key: metrics[key]
        for key in (
            "status",
            "reason",
            "method",
            "limitations",
            "bug_catch_percent",
            "killed",
            "timed_out",
            "survived",
            "invalid",
            "flaky",
            "errors",
            "valid",
            "selected",
            "candidate_count",
            "candidate_line_count",
            "candidate_lines",
            "unsupported_file_count",
            "test_change_kill_share",
        )
    }
    # Version 2 only: unconfirmed mutants, the sample's partition and an incomplete attempt's
    # counts, which stay apart from the pooled ones.
    result.update(
        {key: metrics.get(key) for key in ("unconfirmed", "sites", "partial_counts") if key in metrics}
    )
    coverage_metrics = _object((coverage or {}).get("metrics") or {})
    result["decision_line_coverage"] = _decision_line_coverage(metrics, coverage_metrics)
    coordinates = _object(coverage_metrics.get("coordinates") or {})
    lines = coordinates.get("line")
    if "partial_counts" in metrics and metrics["status"] == "incomplete":
        # A version 2 attempt's settled mutants are partial: no split of them is published.
        result["execution_split"] = {
            "status": "incomplete",
            "reason": "the attempt's mutant outcomes are partial",
        }
        return result
    if not isinstance(lines, dict):
        result["execution_split"] = {
            "status": "unavailable",
            "reason": "the submission has no line coverage",
        }
        return result
    total, covered = set(_strings(lines["total"])), set(_strings(lines["covered"]))
    split: dict[str, dict[str, int]] = {
        group: {"killed": 0, "survived": 0}
        for group in ("executed", "unexecuted", "not_in_population")
    }
    for row in _runs(metrics["mutants"]):
        if row["outcome"] not in ("killed", "timeout", "timeout_kill", "survived"):
            continue
        line = f"{row['path']}:{row['line']}"
        group = (
            "executed"
            if line in covered
            else "unexecuted"
            if line in total
            else "not_in_population"
        )
        split[group]["survived" if row["outcome"] == "survived" else "killed"] += 1
    result["execution_split"] = {"status": "available", **split}
    return result
