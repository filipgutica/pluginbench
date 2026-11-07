"""The documented Mocha per-test hook, driven through Mocha's root-hook contract without Docker.

Mocha and c8 are not installed here, so a driver calls the hooks with `this.currentTest` as Mocha
does, and a stand-in v8-to-istanbul applies its line rule. The task-image run covers the real tools.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from pluginbench import assessment_metrics
from pluginbench.coverage_report import numbits_to_lines, parse_lcov

PROFILES = Path(__file__).parents[1] / "docs/coverage-profiles"
HOOK = PROFILES / "mocha-per-test.mjs"
FINALIZE = PROFILES / "per-test-finalize.mjs"

# v8-to-istanbul 9.3's rule: every line starts at 1; each range sets the lines it fully spans;
# "(empty-report)" zeroes the file.
V8_TO_ISTANBUL = """
module.exports = (path, wrapperLength, { source }) => {
  let offset = 0;
  const lines = source.trimEnd().split("\\n").map((text, index) => {
    const line = { line: index + 1, startCol: offset, endCol: offset + text.length, count: 1 };
    offset += text.length + 1;
    return line;
  });
  return {
    async load() {},
    applyCoverage(blocks) {
      for (const { functionName, ranges } of blocks) {
        for (const { startOffset, endOffset, count } of ranges) {
          for (const line of lines) {
            if (functionName === "(empty-report)") line.count = 0;
            else if (startOffset <= line.startCol && endOffset >= line.endCol) line.count = count;
          }
        }
      }
    },
    toIstanbul() {
      const statementMap = {};
      const s = {};
      for (const [index, line] of lines.entries()) {
        statementMap[index] = { start: { line: line.line } };
        s[index] = line.count;
      }
      return { [path]: { statementMap, s } };
    },
  };
};
"""

SUBJECT = """export function positive() {
  return "positive";
}
export function negative() {
  return "negative";
}
"""

# Calls the root hooks as Mocha does: the plugin loads first, then the spec imports, then each test
# of one registered suite, in order. Tests are [title, body] pairs; a body is a function source.
DRIVER = """
import { resolve } from "node:path";

const { mochaHooks: hooks } = await import(process.argv[2]);
const { positive, negative } = await import(resolve("lib/subject.js"));
const bodies = {
  positive: () => positive(),
  negative: () => negative(),
  both: () => [positive(), negative()],
  nothing: () => {},
};
const root = { root: true, tests: [], suites: [] };
const suite = { parent: root, tests: [], suites: [] };
root.suites.push(suite);
for (const [title, body] of JSON.parse(process.argv[4])) {
  suite.tests.push({ file: resolve("test/a.spec.js"), fullTitle: () => title, fn: bodies[body], parent: suite });
}
for (const currentTest of suite.tests) {
  const context = { currentTest };
  await hooks.beforeEach?.call(context);
  if (process.argv[3] === "overlap") await hooks.beforeEach?.call(context);
  await new Promise((done) => setImmediate(done));
  currentTest.fn();
  await hooks.afterEach?.call(context);
}
hooks.afterAll?.();
"""

TESTS = [
    ("suite positive", "positive"),
    ("suite positive #2", "negative"),
    ("suite positive", "positive"),
    ("suite nothing", "nothing"),
]


def lcov(project: Path, executed: set[int]) -> str:
    rows = "".join(f"DA:{line},{int(line in executed)}\n" for line in range(1, 7))
    return (
        f"SF:{project}/lib/subject.js\n{rows}end_of_record\n"
        f"SF:{project}/lib/unused.js\nDA:1,0\nend_of_record\n"
    )


def run_hook(
    tmp_path: Path,
    *,
    scenario: str = "",
    executed: frozenset[int] = frozenset({2, 3, 5, 6}),
    tests: list[tuple[str, str]] = TESTS,
) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    for relative, content in {
        "node_modules/c8/package.json": '{"name": "c8"}',
        "node_modules/v8-to-istanbul/index.js": V8_TO_ISTANBUL,
        "lib/subject.js": SUBJECT,
        "lib/unused.js": "export const unused = 1;\n",
        "lib/package.json": '{"type": "module"}',
        "driver.mjs": DRIVER,
    }.items():
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text(content)
    records = tmp_path / "records"
    records.mkdir()
    env = {**os.environ, "PLUGINBENCH_PER_TEST_DIR": str(records)}
    env.pop("NODE_V8_COVERAGE", None)
    if scenario == "under-c8":
        env["NODE_V8_COVERAGE"] = str(tmp_path / "v8")
    flags = [] if scenario == "jit" else ["--no-opt", "--no-maglev"]
    driven = subprocess.run(
        ["node", *flags, "driver.mjs", str(HOOK), scenario, json.dumps(tests)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert driven.returncode == 0, driven.stderr
    report = project / "coverage/lcov.info"
    report.parent.mkdir()
    report.write_text(lcov(project, set(executed)))
    finalized = subprocess.run(
        ["node", str(FINALIZE), str(report), str(records)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert finalized.returncode == 0, finalized.stderr
    return project, report, driven.stderr + finalized.stderr


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="the hook requires Node")


def test_hook_attributes_each_tests_lines_and_keeps_identities_unique(tmp_path: Path) -> None:
    project, report, _ = run_hook(tmp_path)
    body = hashlib.sha256(b"() => positive()").hexdigest()[:8]
    # Sorted keys make repeated runs byte-identical whatever order tests reported in.
    raw = json.loads(report.with_name("lcov.info.per-test.json").read_text())["per_test_numbits"]
    assert list(raw) == sorted(raw)
    assert all(list(names) == sorted(names) for names in raw.values())
    parsed = parse_lcov(
        report,
        snapshot=project,
        source_scope=["lib/**"],
        test_scope=["test/**"],
        workdir=str(project),
    )
    assert {
        name: {line for bits in files.values() for line in numbits_to_lines(bits)}
        for name, files in parsed["per_test_numbits"].items()
    } == {
        # Repeated titles carry a hash of the test's function source; identical bodies are then
        # numbered in run order. Unique titles, including a real "#2", stay bare.
        f"test/a.spec.js::suite positive #{body}": {2, 3},
        f"test/a.spec.js::suite positive #{body}-2": {2, 3},
        "test/a.spec.js::suite positive #2": {5, 6},
        "test/a.spec.js::suite nothing": set(),
    }


def test_inserting_a_same_title_test_flags_the_new_test_not_the_old_one(tmp_path: Path) -> None:
    """A new test that reuses an existing title must not take over the existing test's identity."""
    measured = []
    for name, tests in {
        "before": [("suite check", "both")],
        "after": [("suite check", "positive"), ("suite check", "both")],
    }.items():
        project, report, _ = run_hook(tmp_path / name, tests=tests)
        parsed = parse_lcov(
            report,
            snapshot=project,
            source_scope=["lib/**"],
            test_scope=["test/**"],
            workdir=str(project),
        )
        sources = {"lib/subject.js", "lib/unused.js"}
        measured.append(assessment_metrics._coverage(parsed, sources, ["lib/**"]))
    for name, source in (("old", ""), ("new", SUBJECT)):
        (tmp_path / name / "lib").mkdir(parents=True)
        (tmp_path / name / "lib/subject.js").write_text(source)
    change = assessment_metrics.compare_snapshots(tmp_path / "old", tmp_path / "new")
    changed = assessment_metrics._changed_line_coverage(*measured, change)
    inserted = next(
        name
        for name, files in parsed["per_test_numbits"].items()
        if {line for bits in files.values() for line in numbits_to_lines(bits)} == {2, 3}
    )
    # The inserted test reaches only lines the existing test also reaches, so it alone is
    # redundant. The existing test is renamed too (bare title to hashed), so it also counts as added.
    assert changed["redundant_added_test_names"] == [inserted]
    assert changed["added_tests"] == 2


@pytest.mark.parametrize(
    ("scenario", "executed", "reason"),
    [
        ("", frozenset({2, 3}), "lines LCOV reports unexecuted"),
        ("overlap", frozenset({2, 3, 5, 6}), "overlapping tests share coverage counters"),
        ("under-c8", frozenset({2, 3, 5, 6}), "found 0"),
        ("jit", frozenset({2, 3, 5, 6}), "--no-opt --no-maglev"),
    ],
)
def test_hook_writes_no_sidecar_when_attribution_is_unsound(
    tmp_path: Path, scenario: str, executed: frozenset[int], reason: str
) -> None:
    _, report, stderr = run_hook(tmp_path, scenario=scenario, executed=executed)
    assert not report.with_name("lcov.info.per-test.json").exists()
    assert "PluginBench per-test coverage unavailable" in stderr
    assert reason in stderr


@pytest.mark.parametrize(
    ("profile", "embedded"),
    [
        ("csstree.yaml", {"mocha-per-test.mjs", "per-test-finalize.mjs"}),
        (
            "true-myth.yaml",
            {
                "vitest-per-test.config.mjs",
                "vitest-seeded-random.mjs",
                "vitest-per-test-provider.mjs",
                "vitest-per-test-setup.mjs",
                "per-test-finalize.mjs",
            },
        ),
    ],
)
def test_documented_profiles_embed_the_scripts_verbatim(profile: str, embedded: set[str]) -> None:
    (entry,) = yaml.safe_load((PROFILES / profile).read_text()).values()
    heredocs = entry["command"][2].split("<<'JS'\n")[1:]
    bodies = {heredoc[: heredoc.index("\nJS\n") + 1] for heredoc in heredocs}
    assert {name for name in embedded if (PROFILES / name).read_text() in bodies} == embedded


# Stands in for c8 and `npx vitest`: the coverage run writes LCOV and exits 3; the sampling run
# leaves a well-formed record, then fails, as a run cut short after recording would.
RUNNER_STUB = """#!/bin/sh
if [ -n "${PLUGINBENCH_PER_TEST_DIR:-}" ]; then
  echo '{"tests": {"partial": {}}}' > "$PLUGINBENCH_PER_TEST_DIR/record-1.json"
  exit 1
fi
mkdir -p coverage
printf 'SF:%s/lib/a.js\\nDA:1,1\\nend_of_record\\n' "$PWD" > coverage/lcov.info
exit 3
"""
MOCHA_STUB = """require("node:fs").writeFileSync(
  `${process.env.PLUGINBENCH_PER_TEST_DIR}/record-1.json`, '{"tests": {"partial": {}}}'
);
process.exit(1);
"""


@pytest.mark.parametrize("profile", ["csstree.yaml", "true-myth.yaml"])
def test_profiles_write_no_sidecar_when_the_sampling_run_fails(
    tmp_path: Path, profile: str
) -> None:
    (entry,) = yaml.safe_load((PROFILES / profile).read_text()).values()
    project, bin_dir = tmp_path / "project", tmp_path / "bin"
    for path, content in {
        bin_dir / "npx": RUNNER_STUB,
        project / "node_modules/.bin/c8": RUNNER_STUB,
        project / "node_modules/mocha/bin/_mocha": MOCHA_STUB,
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    (tmp_path / "tmp").mkdir()
    command = entry["command"][2].replace("/tmp/", f"{tmp_path}/tmp/")
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=project,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The coverage run's status is the profile's status; its LCOV stays usable.
    assert result.returncode == 3, result.stderr
    assert (project / "coverage/lcov.info").exists()
    assert not (project / "coverage/lcov.info.per-test.json").exists()
