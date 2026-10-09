from __future__ import annotations

import ast
import io
import json
import re
import statistics
import subprocess
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pluginbench.reporting import build_report
from pluginbench.results import ArmName, ArmResult, load_arm

Outcome = Literal[
    "resolved",
    "unresolved",
    "empty_patch",
    "error",
    "infrastructure_error",
    "missing_summary",
    "missing_result",
]

_OUTCOME_FIELDS: tuple[tuple[str, Outcome], ...] = (
    ("resolved_ids", "resolved"),
    ("unresolved_ids", "unresolved"),
    ("empty_patch_ids", "empty_patch"),
    ("error_ids", "error"),
    ("infra_failure_ids", "infrastructure_error"),
)
_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
_DEPENDENCY_FILES = {
    "cargo.lock",
    "cargo.toml",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "gradle.lockfile",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "package.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pom.xml",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "yarn.lock",
}


@dataclass(frozen=True)
class FileChange:
    old_path: str | None
    new_path: str | None
    added: int
    removed: int
    binary: bool

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        if path is None:
            raise ValueError("diff file has no usable path")
        return path


@dataclass(frozen=True)
class ArmSource:
    result: ArmResult
    arm_dir: Path
    run_dir: Path


@dataclass(frozen=True)
class AttemptArtifact:
    task_id: str
    attempt: int
    slug: str
    directory: Path
    outcome: Outcome


@dataclass(frozen=True)
class AstSnapshot:
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    classes: set[str]
    function_count: int
    class_count: int
    top_level_public_symbols: set[str]
    imports: int
    decisions: int
    calls: Counter[str]


class _StructureVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.classes: set[str] = set()
        self.function_count = 0
        self.class_count = 0
        self.top_level_public_symbols: set[str] = set()
        self.imports = 0
        self.calls: Counter[str] = Counter()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_count += 1
        qualified = ".".join((*self.scope, node.name))
        self.functions[qualified] = node
        if not self.scope and not node.name.startswith("_"):
            self.top_level_public_symbols.add(node.name)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.class_count += 1
        qualified = ".".join((*self.scope, node.name))
        self.classes.add(qualified)
        if not self.scope and not node.name.startswith("_"):
            self.top_level_public_symbols.add(node.name)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self.imports += 1
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.imports += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name: str | None = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name is not None:
            self.calls[name] += 1
        self.generic_visit(node)


def _decision_points(tree: ast.AST) -> int:
    decisions = 0
    single_points = (
        ast.If,
        ast.IfExp,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ExceptHandler,
        ast.match_case,
    )
    for node in ast.walk(tree):
        if isinstance(node, single_points):
            decisions += 1
        elif isinstance(node, ast.BoolOp):
            decisions += max(0, len(node.values) - 1)
        elif isinstance(node, ast.comprehension):
            decisions += len(node.ifs)
    return decisions


def _snapshot(tree: ast.Module) -> AstSnapshot:
    visitor = _StructureVisitor()
    visitor.visit(tree)
    return AstSnapshot(
        functions=visitor.functions,
        classes=visitor.classes,
        function_count=visitor.function_count,
        class_count=visitor.class_count,
        top_level_public_symbols=visitor.top_level_public_symbols,
        imports=visitor.imports,
        decisions=_decision_points(tree),
        calls=visitor.calls,
    )


def _marker_path(value: str) -> str | None:
    path = value.split("\t", 1)[0]
    if path.startswith('"'):
        if not path.endswith('"'):
            raise ValueError(f"cannot parse diff path: {value}")
        # Git's C/octal escapes represent path bytes, not Unicode code points.
        decoded = path[1:-1].encode().decode("unicode_escape")
        path = decoded.encode("latin-1").decode("utf-8", errors="surrogateescape")
    if not path or path == "/dev/null":
        return None
    return path[2:] if path.startswith(("a/", "b/")) else path


def _file_changes(patch: str) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    old_path: str | None = None
    new_path: str | None = None
    added = 0
    removed = 0
    binary = False
    in_hunk = False
    active = False

    def flush() -> None:
        nonlocal active, old_path, new_path, added, removed, binary, in_hunk
        if active:
            changes.append(FileChange(old_path, new_path, added, removed, binary))
        active = False
        old_path = None
        new_path = None
        added = 0
        removed = 0
        binary = False
        in_hunk = False

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            header = re.fullmatch(
                r'diff --git ("(?:\\.|[^"\\])*"|a/.*?) ("(?:\\.|[^"\\])*"|b/.*)', line
            )
            if header is None:
                raise ValueError(f"invalid diff header: {line}")
            old_path = _marker_path(header[1])
            new_path = _marker_path(header[2])
            active = True
        elif active and not in_hunk and line.startswith("--- "):
            old_path = _marker_path(line[4:])
        elif active and not in_hunk and line.startswith("+++ "):
            new_path = _marker_path(line[4:])
        elif active and line.startswith("@@"):
            in_hunk = True
        elif active and (line == "GIT binary patch" or line.startswith("Binary files ")):
            binary = True
            in_hunk = False
        elif active and in_hunk and line.startswith("+"):
            added += 1
        elif active and in_hunk and line.startswith("-"):
            removed += 1
    flush()
    return tuple(changes)


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        any(part.lower() in {"test", "tests", "testing", "spec", "specs"} for part in parts)
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _is_source_path(path: str) -> bool:
    return not _is_test_path(path) and Path(path).suffix.lower() in _SOURCE_SUFFIXES


def _is_dependency_manifest(path: str) -> bool:
    name = Path(path).name.lower()
    return (
        name in _DEPENDENCY_FILES
        or (name.startswith("requirements") and name.endswith(".txt"))
        or name.startswith("build.gradle")
    )


def analyze_patch(patch: str) -> dict[str, Any]:
    """Return transparent line and path metrics for one saved Git patch."""
    changes = _file_changes(patch)
    source = [change for change in changes if _is_source_path(change.path)]
    tests = [change for change in changes if _is_test_path(change.path)]
    manifests = sorted(change.path for change in changes if _is_dependency_manifest(change.path))
    lines_added = sum(change.added for change in changes)
    lines_removed = sum(change.removed for change in changes)
    source_added = sum(change.added for change in source)
    source_removed = sum(change.removed for change in source)
    test_added = sum(change.added for change in tests)
    test_removed = sum(change.removed for change in tests)
    return {
        "changed_paths": sorted(change.path for change in changes),
        "files_changed": len(changes),
        "source_files_changed": len(source),
        "test_files_changed": len(tests),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "churn": lines_added + lines_removed,
        "source_lines_added": source_added,
        "source_lines_removed": source_removed,
        "source_churn": source_added + source_removed,
        "test_lines_added": test_added,
        "test_lines_removed": test_removed,
        "test_churn": test_added + test_removed,
        "binary_patch": any(change.binary for change in changes),
        "empty_patch": not patch.strip(),
        "dependency_manifest_changes": manifests,
    }


def _resolve_arm(path: Path, expected: ArmName) -> ArmSource:
    resolved = path.resolve()
    if resolved.is_file():
        arm_path = resolved
        arm_dir = resolved.parent
    elif (resolved / "arm.json").is_file():
        arm_path = resolved / "arm.json"
        arm_dir = resolved
    elif (resolved / expected / "arm.json").is_file():
        arm_path = resolved / expected / "arm.json"
        arm_dir = resolved / expected
    else:
        raise ValueError(f"cannot find {expected} arm.json under {path}")
    result = load_arm(arm_path)
    if result.arm != expected:
        raise ValueError(f"{expected} input must contain arm={expected}")
    return ArmSource(result=result, arm_dir=arm_dir, run_dir=arm_dir.parent)


def _task_id(attempt_dir: Path, known_tasks: set[str]) -> str:
    predictions = attempt_dir / "predictions.jsonl"
    if predictions.is_file():
        for line in predictions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"cannot read predictions {predictions}: {exc}") from exc
            task_id = row.get("instance_id") if isinstance(row, dict) else None
            if isinstance(task_id, str):
                return task_id
            break
    dataset = attempt_dir / "dataset.json"
    if dataset.is_file():
        try:
            rows = json.loads(dataset.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"cannot read dataset {dataset}: {exc}") from exc
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            task_id = rows[0].get("instance_id")
            if isinstance(task_id, str):
                return task_id
    slug = attempt_dir.parent.name
    if slug in known_tasks:
        return slug
    raise ValueError(f"cannot determine task ID for verifier attempt {attempt_dir}")


def _official_outcome(attempt_dir: Path, task_id: str) -> Outcome:
    summaries: list[dict[str, Any]] = []
    for candidate in sorted(attempt_dir.glob("*.json")):
        if candidate.name in {"command.json", "dataset.json"}:
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and any(field in value for field, _ in _OUTCOME_FIELDS):
            summaries.append(value)
    if not summaries:
        return "missing_summary"
    if len(summaries) > 1:
        raise ValueError(f"multiple SWE-bench summary JSON files in {attempt_dir}")
    matches: list[Outcome] = []
    for field, outcome in _OUTCOME_FIELDS:
        values = summaries[0].get(field, [])
        if isinstance(values, list) and task_id in values:
            matches.append(outcome)
    if len(matches) > 1:
        raise ValueError(f"SWE-bench summary has multiple outcomes for {task_id}")
    return matches[0] if matches else "missing_result"


def _discover_attempts(source: ArmSource) -> dict[tuple[str, int], AttemptArtifact]:
    attempts: dict[tuple[str, int], AttemptArtifact] = {}
    known_tasks = set(source.result.tasks)
    for directory in sorted((source.arm_dir / "verifiers").glob("*/attempt-*")):
        if not directory.is_dir():
            continue
        match = re.fullmatch(r"attempt-(\d+)", directory.name)
        if match is None:
            continue
        task_id = _task_id(directory, known_tasks)
        if task_id not in known_tasks:
            raise ValueError(f"verifier attempt references unknown task: {task_id}")
        key = (task_id, int(match.group(1)))
        if key in attempts:
            raise ValueError(f"duplicate verifier attempt {key[1]} for {key[0]}")
        attempts[key] = AttemptArtifact(
            task_id=task_id,
            attempt=key[1],
            slug=directory.parent.name,
            directory=directory,
            outcome=_official_outcome(directory, task_id),
        )
    return attempts


def _attempt_label(key: tuple[str, int]) -> str:
    return f"{key[0]}/attempt-{key[1]:02d}"


def _validate_declared_attempts(
    source: ArmSource, attempts: dict[tuple[str, int], AttemptArtifact]
) -> None:
    expected = {
        (task_id, attempt)
        for task_id, task in source.result.tasks.items()
        for attempt in range(1, task.attempts + 1)
    }
    actual = set(attempts)
    differences: list[str] = []
    if missing := sorted(expected - actual):
        differences.append("missing " + ", ".join(_attempt_label(key) for key in missing))
    if unexpected := sorted(actual - expected):
        differences.append("unexpected " + ", ".join(_attempt_label(key) for key in unexpected))
    if differences:
        raise ValueError(
            f"{source.result.arm} verifier artifacts do not match declared attempts: "
            + "; ".join(differences)
        )


def _decode_python(content: bytes, label: str) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
        return content.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot decode Python source {label}: {exc}") from exc


def _starting_reference(source: ArmSource, artifact: AttemptArtifact, seeded: bool) -> str:
    metadata_path = source.run_dir / "inputs" / "tasks" / artifact.slug / "task.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read task metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"task metadata must be an object: {metadata_path}")
    if seeded:
        starting = metadata.get("starting_patch")
        reference = starting.get("tree") if isinstance(starting, dict) else None
    else:
        benchmark = metadata.get("benchmark")
        reference = benchmark.get("base_commit") if isinstance(benchmark, dict) else None
    if not isinstance(reference, str):
        kind = "starting tree" if seeded else "base commit"
        raise ValueError(f"task metadata lacks {kind}: {metadata_path}")
    return reference


def _git_starting_content(workspace: Path, reference: str, path: str | None) -> bytes:
    if path is None:
        return b""
    result = subprocess.run(
        ("git", "show", f"{reference}:{path}"),
        cwd=workspace,
        capture_output=True,
        check=False,
        shell=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip() or "git show failed"
        raise ValueError(f"starting content is missing for {path}: {detail}")
    return result.stdout


def _final_content(workspace: Path, path: str | None) -> bytes:
    if path is None:
        return b""
    root = workspace.resolve()
    target = (workspace / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"patch path escapes workspace: {path}")
    if not target.is_file():
        raise ValueError(f"final content is missing for {path}")
    return target.read_bytes()


def _verify_workspace_patch(workspace: Path, reference: str, saved_patch: bytes) -> None:
    current = subprocess.run(
        ("git", "diff", "--binary", reference, "--", ".", ":(exclude).agents/**"),
        cwd=workspace,
        capture_output=True,
        check=False,
        shell=False,
        timeout=60,
    )
    if current.returncode != 0:
        detail = current.stderr.decode(errors="replace").strip() or "git diff failed"
        raise ValueError(f"cannot verify saved patch against workspace: {detail}")
    if current.stdout != saved_patch:
        raise ValueError(
            "workspace no longer matches selected saved patch for "
            f"{workspace} relative to {reference}"
        )


def _delta(before: int, after: int) -> dict[str, int]:
    return {"before": before, "after": after, "delta": after - before}


def _python_ast_metrics(
    source: ArmSource,
    artifact: AttemptArtifact,
    changes: tuple[FileChange, ...],
    seeded: bool,
    saved_patch: bytes,
) -> dict[str, Any]:
    python_changes = [
        change
        for change in changes
        if _is_source_path(change.path) and change.path.lower().endswith(".py")
    ]
    if not python_changes:
        return {
            "available": False,
            "unavailable_reason": "no_changed_production_python_files",
            "changed_python_files": 0,
            "analyzed_python_files": 0,
            "parse_error_count": 0,
            "parse_errors": [],
        }
    unavailable = {
        "available": False,
        "unavailable_reason": "production_python_parse_incomplete",
        "changed_python_files": len(python_changes),
        "analyzed_python_files": 0,
        "parse_error_count": 0,
        "parse_errors": [],
    }
    workspace = source.arm_dir / "workspaces" / artifact.slug / f"attempt-{artifact.attempt:02d}"
    if not workspace.is_dir():
        raise ValueError(f"workspace is missing for Python analysis: {workspace}")
    reference = _starting_reference(source, artifact, seeded)
    valid_ref = subprocess.run(
        ("git", "cat-file", "-e", f"{reference}^{{tree}}"),
        cwd=workspace,
        capture_output=True,
        check=False,
        shell=False,
        timeout=30,
    )
    if valid_ref.returncode != 0:
        raise ValueError(f"starting Git tree is unavailable in {workspace}: {reference}")
    _verify_workspace_patch(workspace, reference, saved_patch)

    before_snapshots: dict[str, AstSnapshot] = {}
    after_snapshots: dict[str, AstSnapshot] = {}
    parse_errors: list[dict[str, str]] = []
    for change in python_changes:
        path = change.path
        before_content = _git_starting_content(workspace, reference, change.old_path)
        after_content = _final_content(workspace, change.new_path)
        before_tree: ast.Module | None = None
        after_tree: ast.Module | None = None
        try:
            before_tree = ast.parse(_decode_python(before_content, f"{path} before"), filename=path)
        except (SyntaxError, ValueError) as exc:
            parse_errors.append({"path": path, "version": "before", "error": str(exc)})
        try:
            after_tree = ast.parse(_decode_python(after_content, f"{path} after"), filename=path)
        except (SyntaxError, ValueError) as exc:
            parse_errors.append({"path": path, "version": "after", "error": str(exc)})
        if before_tree is not None and after_tree is not None:
            before_snapshots[path] = _snapshot(before_tree)
            after_snapshots[path] = _snapshot(after_tree)
    if parse_errors:
        unavailable["analyzed_python_files"] = len(before_snapshots)
        unavailable["parse_error_count"] = len(parse_errors)
        unavailable["parse_errors"] = parse_errors
        return unavailable

    available = {
        "available": True,
        "changed_python_files": len(python_changes),
        "analyzed_python_files": len(before_snapshots),
        "parse_error_count": 0,
        "parse_errors": [],
    }

    before_functions = sum(item.function_count for item in before_snapshots.values())
    after_functions = sum(item.function_count for item in after_snapshots.values())
    before_classes = sum(item.class_count for item in before_snapshots.values())
    after_classes = sum(item.class_count for item in after_snapshots.values())
    before_imports = sum(item.imports for item in before_snapshots.values())
    after_imports = sum(item.imports for item in after_snapshots.values())
    before_decisions = sum(item.decisions for item in before_snapshots.values())
    after_decisions = sum(item.decisions for item in after_snapshots.values())
    existing_calls_before = 0
    existing_calls_after = 0
    new_functions: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef, int]] = []
    new_public_symbols: list[str] = []
    for path in sorted(before_snapshots):
        before = before_snapshots[path]
        after = after_snapshots[path]
        existing_names = {
            qualified.rsplit(".", 1)[-1] for qualified in (*before.functions, *before.classes)
        }
        existing_calls_before += sum(before.calls[name] for name in existing_names)
        existing_calls_after += sum(after.calls[name] for name in existing_names)
        new_public_symbols.extend(
            f"{path}:{name}"
            for name in after.top_level_public_symbols - before.top_level_public_symbols
        )
        for qualified, node in after.functions.items():
            if qualified not in before.functions:
                leaf = qualified.rsplit(".", 1)[-1]
                new_functions.append((path, qualified, node, after.calls[leaf]))

    single_use = sorted(
        f"{path}:{qualified}" for path, qualified, _, uses in new_functions if uses == 1
    )
    helper_call_counts = {
        f"{path}:{qualified}": uses
        for path, qualified, _, uses in sorted(new_functions, key=lambda item: item[:2])
    }
    body_counts = Counter(
        ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False)
        for _, _, node, _ in new_functions
    )
    duplicates = sum(count - 1 for count in body_counts.values() if count > 1)
    return {
        **available,
        "function_definitions": _delta(before_functions, after_functions),
        "class_definitions": _delta(before_classes, after_classes),
        "import_statements": _delta(before_imports, after_imports),
        "explicit_decision_points": _delta(before_decisions, after_decisions),
        "existing_local_symbol_calls": _delta(existing_calls_before, existing_calls_after),
        "new_single_use_helpers": single_use,
        "new_helper_call_counts": helper_call_counts,
        "new_helpers_called_at_least_twice_count": sum(
            uses >= 2 for uses in helper_call_counts.values()
        ),
        "new_top_level_public_symbols": sorted(new_public_symbols),
        "new_top_level_public_symbol_count": len(new_public_symbols),
        "duplicate_new_function_body_count": duplicates,
    }


def _attempt_metrics(
    source: ArmSource,
    artifact: AttemptArtifact,
) -> dict[str, Any]:
    seeded = source.result.tasks[artifact.task_id].starting_patch is not None
    patch_name = "agent-change.diff" if seeded else "patch.diff"
    patch_path = artifact.directory / patch_name
    if not patch_path.is_file():
        raise ValueError(f"selected patch is missing: {patch_path}")
    saved_patch = patch_path.read_bytes()
    try:
        patch = saved_patch.decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"selected patch is not UTF-8: {patch_path}") from exc
    changes = _file_changes(patch)
    result: dict[str, Any] = {
        "outcome": artifact.outcome,
        "patch_file": patch_name,
        "diff": analyze_patch(patch),
        "python_ast": _python_ast_metrics(source, artifact, changes, seeded, saved_patch),
    }
    return result


def _numeric_metrics(attempt: dict[str, Any]) -> dict[str, int | float]:
    diff = attempt["diff"]
    python = attempt["python_ast"]
    metrics: dict[str, int | float] = {
        "files_changed": diff["files_changed"],
        "source_files_changed": diff["source_files_changed"],
        "test_files_changed": diff["test_files_changed"],
        "lines_added": diff["lines_added"],
        "lines_removed": diff["lines_removed"],
        "churn": diff["churn"],
        "source_lines_added": diff["source_lines_added"],
        "source_lines_removed": diff["source_lines_removed"],
        "source_churn": diff["source_churn"],
        "test_lines_added": diff["test_lines_added"],
        "test_lines_removed": diff["test_lines_removed"],
        "test_churn": diff["test_churn"],
        "dependency_manifest_files_changed": len(diff["dependency_manifest_changes"]),
        "binary_patch": int(diff["binary_patch"]),
        "empty_patch": int(diff["empty_patch"]),
        "production_python_files_changed": python["changed_python_files"],
        "python_ast_available": int(python["available"]),
        "python_files_analyzed": python["analyzed_python_files"],
        "python_parse_error_count": python["parse_error_count"],
    }
    if python["available"]:
        metrics.update(
            {
                "function_definition_delta": python["function_definitions"]["delta"],
                "class_definition_delta": python["class_definitions"]["delta"],
                "import_statement_delta": python["import_statements"]["delta"],
                "explicit_decision_point_delta": python["explicit_decision_points"]["delta"],
                "existing_local_symbol_call_delta": python["existing_local_symbol_calls"]["delta"],
                "new_single_use_helper_count": len(python["new_single_use_helpers"]),
                "new_helpers_called_at_least_twice_count": python[
                    "new_helpers_called_at_least_twice_count"
                ],
                "new_top_level_public_symbol_count": python["new_top_level_public_symbol_count"],
                "duplicate_new_function_body_count": python["duplicate_new_function_body_count"],
            }
        )
    return metrics


def _aggregates(population: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, list[int | float]]] = {"baseline": {}, "treatment": {}}
    for aligned in population:
        baseline = _numeric_metrics(aligned["baseline"])
        treatment = _numeric_metrics(aligned["treatment"])
        for key in baseline.keys() & treatment.keys():
            by_arm["baseline"].setdefault(key, []).append(baseline[key])
            by_arm["treatment"].setdefault(key, []).append(treatment[key])
    distributions = {
        arm: {
            key: {
                "values": values,
                "sample_count": len(values),
                "median": statistics.median(values),
            }
            for key, values in sorted(metrics.items())
        }
        for arm, metrics in by_arm.items()
    }
    median_differences: dict[str, int | float] = {}
    for key in sorted(by_arm["baseline"].keys() & by_arm["treatment"].keys()):
        baseline_median = statistics.median(by_arm["baseline"][key])
        treatment_median = statistics.median(by_arm["treatment"][key])
        median_differences[key] = treatment_median - baseline_median
    return {
        "distributions": distributions,
        "treatment_minus_baseline_medians": median_differences,
    }


def build_quality_report(baseline_path: Path, treatment_path: Path) -> dict[str, Any]:
    """Build an offline quality comparison from saved PluginBench arms or runs."""
    baseline = _resolve_arm(baseline_path, "baseline")
    treatment = _resolve_arm(treatment_path, "treatment")
    build_report(baseline.result, treatment.result, evaluate_decision=False)

    baseline_attempts = _discover_attempts(baseline)
    treatment_attempts = _discover_attempts(treatment)
    _validate_declared_attempts(baseline, baseline_attempts)
    _validate_declared_attempts(treatment, treatment_attempts)
    aligned_keys = sorted(baseline_attempts.keys() & treatment_attempts.keys())
    alignments: list[dict[str, Any]] = []
    population: list[dict[str, Any]] = []
    for task_id, attempt in aligned_keys:
        baseline_attempt = baseline_attempts[(task_id, attempt)]
        treatment_attempt = treatment_attempts[(task_id, attempt)]
        included = (
            baseline_attempt.outcome == "resolved" and treatment_attempt.outcome == "resolved"
        )
        alignments.append(
            {
                "task_id": task_id,
                "attempt": attempt,
                "baseline_outcome": baseline_attempt.outcome,
                "treatment_outcome": treatment_attempt.outcome,
                "included_in_quality_population": included,
            }
        )
        if included:
            population.append(
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "baseline": _attempt_metrics(baseline, baseline_attempt),
                    "treatment": _attempt_metrics(treatment, treatment_attempt),
                }
            )
    unaligned = [
        {
            "task_id": task_id,
            "attempt": attempt,
            "missing_arm": "treatment" if key in baseline_attempts else "baseline",
        }
        for key in sorted(baseline_attempts.keys() ^ treatment_attempts.keys())
        for task_id, attempt in (key,)
    ]
    baseline_resolved = sum(attempt.outcome == "resolved" for attempt in baseline_attempts.values())
    treatment_resolved = sum(
        attempt.outcome == "resolved" for attempt in treatment_attempts.values()
    )
    matched_fraction = len(population) / len(aligned_keys) if aligned_keys else None
    return {
        "schema_version": 1,
        "report_type": "successful_patch_quality",
        "inputs": {
            "baseline": str(baseline.arm_dir),
            "treatment": str(treatment.arm_dir),
            "compatibility_fingerprint": baseline.result.compatibility_fingerprint,
            "task_checksums": baseline.result.task_checksums,
        },
        "attempt_alignment": {
            "method": (
                "Task ID and ordinal attempt number align saved artifacts and official "
                "outcomes for bookkeeping only; alignment does not imply shared randomness "
                "or justify a paired-effect estimate."
            ),
            "baseline_attempts_discovered": len(baseline_attempts),
            "treatment_attempts_discovered": len(treatment_attempts),
            "baseline_resolved_attempts": baseline_resolved,
            "treatment_resolved_attempts": treatment_resolved,
            "aligned_attempts": len(aligned_keys),
            "matched_success_alignments": len(population),
            "excluded_alignment_count": len(aligned_keys) - len(population),
            "matched_success_fraction": matched_fraction,
            "matched_success_numerator": len(population),
            "matched_success_denominator": len(aligned_keys),
            "unaligned_attempts": unaligned,
            "alignments": alignments,
        },
        "quality_population": population,
        "aggregates": _aggregates(population),
        "measurement_limits": [
            "Only aligned attempts resolved in both arms are included in code-quality metrics.",
            "Ordinal attempt alignment is bookkeeping only and does not imply shared randomness or a paired effect.",
            "Correction turns and human review time are not measured.",
            "Use the normal PluginBench reports for token, cost, and latency measurements.",
            "The report exposes separate proxies and does not calculate a composite quality score.",
        ],
    }


def _display(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _markdown(report: dict[str, Any]) -> str:
    alignment = report["attempt_alignment"]
    lines = [
        "# Successful-patch quality report",
        "",
        "This offline comparison analyzes only attempts that the official SWE-bench summaries mark resolved in both arms.",
        "It reports separate size, structure, and reuse metrics; it does not calculate a composite quality score.",
        alignment["method"],
        "",
        "## Population",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
        f"| Baseline verifier attempts discovered | {alignment['baseline_attempts_discovered']} |",
        f"| Treatment verifier attempts discovered | {alignment['treatment_attempts_discovered']} |",
        f"| Baseline resolved verifier attempts | {alignment['baseline_resolved_attempts']} |",
        f"| Treatment resolved verifier attempts | {alignment['treatment_resolved_attempts']} |",
        f"| Aligned attempts | {alignment['aligned_attempts']} |",
        f"| Matched-success alignments | {alignment['matched_success_alignments']} |",
        f"| Excluded alignments | {alignment['excluded_alignment_count']} |",
        "| Matched-success fraction | "
        f"{_display(alignment['matched_success_fraction'])} "
        f"({alignment['matched_success_numerator']}/{alignment['matched_success_denominator']}) |",
        "",
        "## Median metrics",
        "",
        "| Metric | Baseline | Treatment | Treatment - baseline median |",
        "| --- | ---: | ---: | ---: |",
    ]
    distributions = report["aggregates"]["distributions"]
    differences = report["aggregates"]["treatment_minus_baseline_medians"]
    for metric in sorted(differences):
        lines.append(
            f"| {metric} | {_display(distributions['baseline'][metric]['median'])} | "
            f"{_display(distributions['treatment'][metric]['median'])} | "
            f"{_display(differences[metric])} |"
        )
    if not differences:
        lines.append("| No matched-success alignments | - | - | - |")
    lines.extend(
        [
            "",
            "## Aligned official outcomes",
            "",
            "| Task | Attempt | Baseline | Treatment | Included |",
            "| --- | ---: | --- | --- | --- |",
        ]
    )
    for aligned in alignment["alignments"]:
        lines.append(
            f"| {aligned['task_id']} | {aligned['attempt']} | "
            f"{aligned['baseline_outcome']} | {aligned['treatment_outcome']} | "
            f"{'yes' if aligned['included_in_quality_population'] else 'no'} |"
        )
    if not alignment["alignments"]:
        lines.append("| No aligned attempts | - | - | - | no |")
    lines.extend(["", "## Limits", ""])
    lines.extend(f"- {limit}" for limit in report["measurement_limits"])
    lines.extend(
        [
            "",
            "Source lines are non-test files with common programming-language suffixes. Test paths use test/spec directory and filename conventions.",
            "Python decision points count conditionals, loops, exception handlers, match cases, boolean branches, and comprehension filters.",
            "Existing-local-symbol calls mean calls to functions or classes that already existed in the same changed Python module.",
            "New-helper call counts report syntactic calls by function name within the same final changed module.",
            "New top-level public symbols are new function or class definitions whose names do not start with an underscore; dynamic exports are not interpreted.",
            "",
        ]
    )
    return "\n".join(lines)


def write_quality_reports(output: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """Write deterministic JSON and Markdown quality reports."""
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "quality-report.json"
    markdown_path = output / "quality-report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path
