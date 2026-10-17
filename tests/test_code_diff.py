from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pluginbench.code_diff import analyze_patch


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_diff_metrics_separate_source_tests_and_manifests() -> None:
    patch = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,3 @@\n-old\n+new\n+if ready:\n+    run()\n"
        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-old test\n+new test\n"
        "diff --git a/package.json b/package.json\n"
        '--- a/package.json\n+++ b/package.json\n@@ -1 +1 @@\n-{}\n+{"x": 1}\n'
    )

    metrics = analyze_patch(patch)

    assert metrics["changed_paths"] == ["package.json", "src/app.py", "tests/test_app.py"]
    assert metrics["files_changed"] == 3
    assert metrics["source_files_changed"] == 1
    assert metrics["test_files_changed"] == 1
    assert metrics["lines_added"] == 5
    assert metrics["lines_removed"] == 3
    assert metrics["churn"] == 8
    assert metrics["source_lines_added"] == 3
    assert metrics["source_lines_removed"] == 1
    assert metrics["source_churn"] == 4
    assert metrics["test_lines_added"] == 1
    assert metrics["test_lines_removed"] == 1
    assert metrics["test_churn"] == 2
    assert metrics["dependency_manifest_changes"] == ["package.json"]
    assert metrics["binary_patch"] is False
    assert metrics["empty_patch"] is False

    assert analyze_patch("")["empty_patch"] is True
    binary = analyze_patch(
        "diff --git a/logo.png b/logo.png\nnew file mode 100644\nGIT binary patch\nliteral 1\nA\n"
    )
    assert binary["binary_patch"] is True
    assert binary["files_changed"] == 1


@pytest.mark.parametrize("name", ["spaced module.py", "café.py"])
def test_git_generated_paths_are_decoded_without_shell_rules(tmp_path: Path, name: str) -> None:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    source = tmp_path / name
    source.write_text("value = 1\n")
    _git("add", name, cwd=tmp_path)
    _git("-c", "commit.gpgsign=false", "commit", "-m", "base", cwd=tmp_path)
    source.write_text("value = 2\n")

    metrics = analyze_patch(_git("diff", "--binary", "HEAD", cwd=tmp_path))

    assert metrics["changed_paths"] == [name]
    assert metrics["source_files_changed"] == 1
    assert metrics["lines_added"] == metrics["lines_removed"] == 1


def test_git_generated_hunk_payload_is_not_a_file_marker(tmp_path: Path) -> None:
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    source = tmp_path / "counter.js"
    source.write_text("-- counter;\n")
    _git("add", source.name, cwd=tmp_path)
    _git("-c", "commit.gpgsign=false", "commit", "-m", "base", cwd=tmp_path)
    source.write_text("++ counter;\n")

    metrics = analyze_patch(_git("diff", "--binary", "HEAD", cwd=tmp_path))

    assert metrics["changed_paths"] == [source.name]
    assert metrics["lines_added"] == metrics["lines_removed"] == 1


def test_plain_unified_diff_is_not_silently_measured_as_zero() -> None:
    patch = "--- src/app.ts\n+++ src/app.ts\n@@ -1 +1 @@\n-old\n+new\n"

    with pytest.raises(ValueError, match="Git diff"):
        analyze_patch(patch)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pkg/counter_test.go", 1),
        ("pkg/counter.go", 0),
        ("TESTS/check.go", 1),
        ("lib/__tests__/lexer-match.js", 1),
        ("lib/__tests/lexer-match-atrule-prelude.js", 1),
        ("src/example.test.dir/value.ts", 0),
    ],
)
def test_shared_classifier_recognizes_go_tests_without_matching_parent_filename(
    path: str, expected: int
) -> None:
    patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
    metrics = analyze_patch(patch)
    assert metrics["test_files_changed"] == expected
    assert metrics["source_files_changed"] == 1 - expected
