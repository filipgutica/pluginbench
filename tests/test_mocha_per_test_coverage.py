"""The documented Mocha per-test hook, driven through Mocha's root-hook contract without Docker.

Mocha and c8 are not installed here, so a driver calls the hooks with `this.currentTest` as Mocha
does, and a stand-in v8-to-istanbul applies its line rule. The task-image run covers the real tools.
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from pluginbench.coverage_report import numbits_to_lines, parse_lcov

PROFILES = Path(__file__).parents[1] / "docs/coverage-profiles"
HOOK = PROFILES / "mocha-per-test.mjs"

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

# Calls the root hooks as Mocha does: the plugin loads first, then the spec imports, then each test.
DRIVER = """
import { resolve } from "node:path";

const { mochaHooks: hooks } = await import(process.argv[2]);
const { positive, negative } = await import(resolve("lib/subject.js"));
const run = async (title, body, { overlap = false } = {}) => {
  const context = { currentTest: { file: resolve("test/a.spec.js"), fullTitle: () => title } };
  await hooks.beforeEach?.call(context);
  if (overlap) await hooks.beforeEach?.call(context);
  await new Promise((done) => setImmediate(done));
  body();
  await hooks.afterEach?.call(context);
};
await run("suite positive", positive);
await run("suite positive #2", negative, { overlap: process.argv[3] === "overlap" });
await run("suite positive", positive);
await run("suite nothing", () => {});
hooks.afterAll?.();
"""


def lcov(project: Path, executed: set[int]) -> str:
    rows = "".join(f"DA:{line},{int(line in executed)}\n" for line in range(1, 7))
    return (
        f"SF:{project}/lib/subject.js\n{rows}end_of_record\n"
        f"SF:{project}/lib/unused.js\nDA:1,0\nend_of_record\n"
    )


def run_hook(
    tmp_path: Path, *, scenario: str = "", executed: frozenset[int] = frozenset({2, 3, 5, 6})
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
        ["node", *flags, "driver.mjs", str(HOOK), scenario],
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
        ["node", str(HOOK), str(report), str(records)],
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
        "test/a.spec.js::suite positive": {2, 3},
        # A real "#2" title keeps its name; the repeated title skips to #3.
        "test/a.spec.js::suite positive #2": {5, 6},
        "test/a.spec.js::suite positive #3": {2, 3},
        "test/a.spec.js::suite nothing": set(),
    }


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


def test_documented_csstree_profile_embeds_the_hook_verbatim() -> None:
    (profile,) = yaml.safe_load((PROFILES / "csstree.yaml").read_text()).values()
    script = profile["command"][2]
    start = script.index("<<'JS'\n") + len("<<'JS'\n")
    assert script[start : script.index("\nJS\n", start) + 1] == HOOK.read_text()
