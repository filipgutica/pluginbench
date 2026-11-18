"""Mutation bug-catch rate (preregistration M1): single-site mutants of an attempt's own code.

Sites are decision operators on the production lines an attempt added or changed. A
deterministic sample of at most ten mutants, one per line, each runs the task's frozen
coverage-profile command in its frozen image, offline. The collector records raw facts: every
candidate site, the selected mutants, each run's suite status and receipt. `mutation_metrics`
derives every outcome and count from those facts, so a report cannot carry a count its runs do
not support. `compare_mutation` binds a report to its attempt's diff and adds the descriptive
split by line execution.
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
import time
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from pluginbench import components, isolation, source_only
from pluginbench.assessment_metrics import _added_source_lines, compare_snapshots, inspect_snapshot
from pluginbench.collectors import CollectorUnavailable

VERSION = "mutation-v1"
GO_OBSERVER = "go-mutation-v1"
OPERATORS = (
    "relational_boundary",
    "equality_negation",
    "boolean_connective",
    "condition_negation",
)
SAMPLE_SIZE = 10
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
# The only receipt directory name `_Runner` writes.
_RECEIPTS = re.compile(r"runs/\d{3,}-[a-z0-9-]+")
METHOD = (
    "single-site mutants of changed production lines (relational boundary, equality negation, "
    "boolean connective, condition negation); at most one mutant per line, ten per attempt, "
    "sampled by sha256 order and round-robin over operators; each runs the frozen "
    "coverage-profile command; kills confirmed by one rerun"
)
LIMITATIONS = (
    "arms produce different mutant populations, so this compares test suites on each arm's own "
    "code, not test quality in isolation; timeouts count as kills and are reported separately; "
    "the timeout baseline includes container start and source export time"
)


class SiteError(ValueError):
    """A parser reported a span that is not the operator it names; its file stays unparsed."""


class SiteFinderFailure(Exception):
    """A pinned site finder did not run to a result; the files it was given stay unparsed."""


class InfrastructureFailure(Exception):
    """Docker or the host could not run a command: the attempt is incomplete, not the revision.

    Not a ValueError, so it is never mistaken for an integrity violation, which fails closed.
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
    elapsed_seconds: float


# Runs `command` on a copy of `tree` with a timeout in seconds, leaving receipts in a directory.
Executor = Callable[[Path, list[str], int, Path], Run]


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


def outcome(runs: list[dict[str, object]]) -> str | None:
    """killed, timeout, survived, flaky or error from a mutant's runs; None needs a rerun."""
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


def native_executor(
    identity: dict[str, object], report_path: str, *, provisioned_tool: object = None,
    provisioned_scope: list[str] | None = None
) -> Executor:
    """Run commands with `run_native_command` in the frozen task image, timing each run."""
    from pluginbench.harbor_backend import run_native_command
    from pluginbench.measurement_tools import ProvisionedCommandOptions, read_tool

    tool = read_tool(provisioned_tool)
    tool_options: ProvisionedCommandOptions = (
        {"provisioned_tool": tool, "provisioned_scope": provisioned_scope} if tool is not None else {}
    )

    def execute(tree: Path, command: list[str], timeout: int, destination: Path) -> Run:
        started = time.monotonic()
        try:
            receipt = run_native_command(
                snapshot=tree,
                task_identity=identity,
                destination=destination,
                command=command,
                report_path=report_path,
                timeout=timeout,
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
        elapsed = time.monotonic() - started
        exit_code = receipt.get("exit_code")
        return Run(
            exit_code=exit_code if isinstance(exit_code, int) else None,
            timed_out=receipt.get("timed_out") is True,
            stdout=str(receipt.get("stdout") or ""),
            elapsed_seconds=elapsed,
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


class _Runner:
    """Numbers each run's receipt directory and records what the report needs from it."""

    def __init__(self, execute: Executor, destination: Path, process_exit: bool) -> None:
        self.execute, self.destination, self.count = execute, destination, 0
        self.process_exit = process_exit
        # The receipt directory of the run in progress, named by an infrastructure failure.
        self.current: str | None = None

    def __call__(
        self, tree: Path, command: list[str], timeout: int, label: str
    ) -> dict[str, object]:
        directory = self.destination / "runs" / f"{self.count:03d}-{label}"
        self.count += 1
        directory.mkdir(parents=True)
        self.current = directory.relative_to(self.destination).as_posix()
        run = self.execute(tree, command, timeout, directory)
        (directory / "stdout.txt").write_bytes(run.stdout.encode())
        return {
            "label": label,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "suite_status": suite_status(run, self.process_exit),
            "elapsed_seconds": round(run.elapsed_seconds, 3),
            "timeout_seconds": timeout,
            "receipts": self.current,
            "stdout_sha256": _digest(directory / "stdout.txt"),
        }


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
    return all(run["suite_status"] == 0 for run in runs)


def _timeout(runs: list[dict[str, object]]) -> int:
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
) -> dict[str, object]:
    """Measure one submission; `root` is a disposable copy of it, never modified."""
    if config.get("observer_version") != components.VERSIONS["mutation_observer"]:
        raise components.ObserverError(
            f"mutation observer version {config.get('observer_version')} froze this profile, and "
            f"this PluginBench measures with version {components.VERSIONS['mutation_observer']}: "
            f"{components.OBSERVER_NEXT_STEP}"
        )
    implementation = observer_identity()
    try:
        return _collect(root, before, config, destination, execute)
    finally:
        if observer_identity() != implementation:
            raise components.ObserverError(
                "the mutation observer's bytes changed during execution: measure again with an "
                "unchanged PluginBench"
            )


def _collect(
    root: Path,
    before: Path,
    config: dict[str, object],
    destination: Path,
    execute: Executor | None,
) -> dict[str, object]:
    coverage = config["coverage"]
    identity = config["native_task_identity"]
    assert isinstance(coverage, dict) and isinstance(identity, dict)
    if execute is None:
        execute = native_executor(
            identity, str(coverage["report_path"]), provisioned_tool=coverage.get("provisioned_tool"),
            provisioned_scope=_strings(coverage.get("source_scope") or ["*"])
        )
    run = _Runner(execute, destination, _process_exit(config))
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
    selected = sample(candidates, after_digest)
    report: dict[str, object] = {
        "version": VERSION,
        "language": str(config["language"]),
        "suite_status": config["suite_status"],
        "before_digest": change["before_digest"],
        "after_digest": after_digest,
        "operators": list(OPERATORS),
        "sample_size": SAMPLE_SIZE,
        "generated_files": sorted(generated),
        "changed_production_lines": changed,
        "unsupported_files": sorted(files - supported),
        "unparsed_files": unparsed,
        "candidates": [asdict(site) for site in candidates],
        "selected": [asdict(site) for site in selected],
        "baseline": [],
        "check_baseline": None,
        "source_only": _source_only_state(before, root, config.get("source_only_reference_paths")),
        "mutants": [],
        "infrastructure_failure": None,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    if selected:
        command = [str(item) for item in coverage["command"]]
        try:
            _run_mutants(
                run, root, before, report, selected, command, int(coverage["timeout_seconds"])
            )
        except InfrastructureFailure as error:
            report["infrastructure_failure"] = {"reason": str(error), "receipts": run.current}
    return report


def _run_mutants(
    run: _Runner,
    root: Path,
    before: Path,
    report: dict[str, object],
    selected: list[Site],
    command: list[str],
    suite_timeout: int,
) -> None:
    """The baseline, then each selected mutant, recorded into `report` as each run ends."""
    baseline = [run(root, command, suite_timeout, "baseline") for _ in range(2)]
    report["baseline"] = baseline
    if not _baseline(baseline):
        return
    timeout = _timeout(baseline)
    if any(_check_command(site.path) == TSC_CHECK for site in selected):
        result = run(root, TSC_CHECK, suite_timeout, "tsc-baseline")
        report["check_baseline"] = result
        if not _tsc_ran(run.destination, result):
            return
    source_state = _object(report["source_only"])
    with tempfile.TemporaryDirectory(prefix="pluginbench-mutation-") as temporary:
        work = Path(temporary)
        source_tree = work / "source-only"
        source_baseline: list[dict[str, object]] = []
        if source_state["status"] == "measured" and source_state["changed_test_files"]:
            _source_only_tree(
                before, root, _strings(source_state["changed_test_files"]), source_tree
            )
            source_baseline = [
                run(source_tree, command, suite_timeout, "source-only-baseline") for _ in range(2)
            ]
            source_state["baseline"] = source_baseline
        mutants: list[dict[str, object]] = []
        report["mutants"] = mutants
        for index, site in enumerate(selected):
            tree, mutated = _mutated(root, site, work / "mutant")
            record: dict[str, object] = {"site": index, "mutated_sha256": mutated, "check": None}
            check = _check_command(site.path)
            if check is not None:
                record["check"] = run(tree, check, suite_timeout, f"mutant-{index}-check")
                state = _check_state(
                    run.destination, record["check"], report["check_baseline"], site
                )
                if state != "valid":
                    mutants.append(record)
                    continue
            runs: list[dict[str, object]] = []
            while outcome(runs) is None:
                runs.append(run(tree, command, timeout, f"mutant-{index}"))
            record["runs"] = runs
            if (
                source_baseline
                and _baseline(source_baseline)
                and outcome(runs) in ("killed", "timeout")
            ):
                record["source_only"] = _source_only_runs(
                    run, site, source_tree, root, work, command, _timeout(source_baseline), index
                )
            mutants.append(record)


def _source_only_runs(
    run: _Runner,
    site: Site,
    source_tree: Path,
    root: Path,
    work: Path,
    command: list[str],
    timeout: int,
    index: int,
) -> dict[str, object]:
    """The source-only suite on the same mutant; a site the filter changed has none."""
    placed = source_tree / site.path
    if not placed.is_file() or placed.read_bytes() != (root / site.path).read_bytes():
        return {"placed": False}
    tree, mutated = _mutated(source_tree, site, work / "source-only-mutant")
    runs: list[dict[str, object]] = []
    while outcome(runs) is None:
        runs.append(run(tree, command, timeout, f"mutant-{index}-source-only"))
    return {"placed": True, "mutated_sha256": mutated, "runs": runs}


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
    """Checks run records against their saved output and the launcher's execution.json."""

    def __init__(self, directory: Path, config: dict[str, object]) -> None:
        self.directory = directory
        self.process_exit = _process_exit(config)
        identity = _object(_object(config["native_task_identity"])["image_identity"])
        self.image = identity["image_id"]
        self.seen: set[str] = set()

    def _own(self, receipts: object) -> Path:
        """A run's receipt directory, named as `_Runner` names it; no two runs share one.

        Only that one spelling is accepted, and it must not pass through a link, so the name
        alone tells two directories apart.
        """
        name = str(receipts)
        path = self.directory / name
        if (
            not _RECEIPTS.fullmatch(name)
            or name in self.seen
            or path.resolve() != self.directory.resolve() / name
        ):
            raise ValueError("a mutation run does not have its own receipt directory")
        self.seen.add(name)
        return path

    def receipt(self, receipts: object) -> dict[str, object]:
        """A run's launcher receipt."""
        return _object(json.loads((self._own(receipts) / "execution.json").read_text()))

    def runs(
        self, value: object, command: list[str], tree: str, timeout: int
    ) -> list[dict[str, object]]:
        """Run records that re-derive from their output and match their receipts.

        `tree` is the digest of the tree each run must have measured, and `timeout` its limit.
        """
        runs = _runs(value)
        for item in runs:
            stdout = _stdout(self.directory, item)
            exit_code = item["exit_code"]
            recorded = Run(
                exit_code=exit_code if isinstance(exit_code, int) else None,
                timed_out=item["timed_out"] is True,
                stdout=stdout,
                elapsed_seconds=float(str(item["elapsed_seconds"])),
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
                or item["timeout_seconds"] != timeout
            ):
                raise ValueError("a mutation run's record differs from its launcher receipt")
        return runs

    def infrastructure(self, value: object) -> str:
        """An infrastructure failure's reason, as the failing run's receipt records it."""
        record = _object(value)
        reason = str(record["reason"])
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


def _settled(runs: list[dict[str, object]]) -> str:
    """A mutant's outcome; its runs must end exactly where the outcome became known."""
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


def mutation_metrics(
    report: dict[str, object], directory: Path, *, config: dict[str, object], snapshot: Path
) -> dict[str, object]:
    """Every outcome and count, derived from the report's raw sites and runs.

    `directory` holds the report's run receipts, `config` is its frozen profile and `snapshot`
    the measured submission. Each run must match its launcher receipt: the profile's command
    and image, and the tree that run measured, which is rebuilt here from the snapshot.
    """
    if report.get("version") != VERSION:
        raise ValueError("mutation report version differs from this observer")
    if report.get("suite_status") != config.get("suite_status"):
        raise ValueError("mutation report's suite status rule differs from its frozen profile")
    candidates = _sites(report["candidates"])
    selected = _sites(report["selected"])
    if selected != sample(candidates, str(report["after_digest"])):
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
        receipts=_Receipts(directory, config),
    )
    if measured.after != report["after_digest"]:
        raise ValueError("mutation report differs from its measured snapshot")
    candidate_lines = sorted({site.coordinate() for site in candidates})
    unsupported = _strings(report["unsupported_files"])
    result: dict[str, object] = {
        "before_digest": report["before_digest"],
        "method": METHOD,
        "limitations": LIMITATIONS,
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
    counts = {
        key: sum(row["outcome"] == state for row in rows)
        for key, state in (
            ("killed", "killed"),
            ("timed_out", "timeout"),
            ("survived", "survived"),
            ("invalid", "invalid"),
            ("flaky", "flaky"),
            ("errors", "error"),
        )
    }
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


def _tsc_baseline_problem(measured: _Measured, tsc: object, selected: list[Site]) -> str | None:
    """Why the TypeScript mutants cannot be checked, if so; the tsc baseline runs only for them."""
    if any(_check_command(site.path) == TSC_CHECK for site in selected) != (tsc is not None):
        raise ValueError("the tsc baseline run differs from the TypeScript mutants it serves")
    if tsc is None:
        return None
    measured.receipts.runs([tsc], TSC_CHECK, measured.after, measured.suite_timeout)
    if not _tsc_ran(measured.directory, _object(tsc)):
        return "tsc did not report diagnostics on the unmutated tree"
    return None


def _mutant_rows(
    measured: _Measured,
    report: dict[str, object],
    selected: list[Site],
    share: dict[str, object],
    timeout: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Each selected mutant's outcome row, and the share state after the source-only baseline."""
    source = _object(report["source_only"])
    restored = _object(source.get("restored") or {})
    source_files = {path: digest for path, digest in measured.files.items() if path not in restored}
    source_files.update({path: digest for path, digest in restored.items() if digest is not None})
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
    if unsure := sorted({item for item in outcomes if item in ("flaky", "error", "not placed")}):
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
    coverage_metrics = _object((coverage or {}).get("metrics") or {})
    result["decision_line_coverage"] = _decision_line_coverage(metrics, coverage_metrics)
    coordinates = _object(coverage_metrics.get("coordinates") or {})
    lines = coordinates.get("line")
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
        if row["outcome"] not in ("killed", "timeout", "survived"):
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
