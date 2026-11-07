// Per-test line coverage for a Mocha suite, as the PluginBench sidecar of c8's LCOV report.
//
// Inspector coverage resets are isolate-wide, so per-test samples cannot be taken inside the c8
// run without changing its report. Run the unchanged c8 command first, then the same suite again
// without c8, with this file as the first Mocha `--require`:
//   PLUGINBENCH_PER_TEST_DIR=/tmp/pb node --no-opt --no-maglev node_modules/mocha/bin/_mocha \
//     --require <this file> ...
// and finally write the sidecar beside the LCOV report with per-test-finalize.mjs.
//
// Each test's sample is converted with c8's own v8-to-istanbul. Any problem leaves no sidecar, so
// redundancy stays unknown; the hook never fails a test or changes which tests run.
import { Session } from "node:inspector/promises";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

async function hooks(output) {
  const session = new Session();
  const sources = new Map();
  const occurrences = new Map();
  const tests = {};
  let failure;
  let activeTest;
  let v8toIstanbul;

  const fail = (error) => {
    failure ??= String(error?.stack ?? error);
  };
  const executedLines = async ({ url, functions }) => {
    const path = fileURLToPath(url);
    if (!sources.has(path)) sources.set(path, readFileSync(path, "utf8"));
    const source = sources.get(path);
    const converter = v8toIstanbul(path, undefined, { source });
    await converter.load();
    // V8 omits uncalled functions under an uncalled parent, and v8-to-istanbul starts every line at
    // one, so zero the whole file first, as c8 does for files that never loaded.
    const zero = { startOffset: 0, endOffset: source.length, count: 0 };
    converter.applyCoverage([
      { functionName: "(empty-report)", isBlockCoverage: true, ranges: [zero] },
      ...functions,
    ]);
    const { statementMap, s } = converter.toIstanbul()[path];
    return Object.keys(s)
      .filter((key) => s[key] > 0)
      .map((key) => statementMap[key].start.line);
  };

  try {
    // Optimized code drops counts of inlined calls, so per-test samples randomly lose executed
    // lines; csstree lost lines in 235 of 16,725 tests between two identical runs.
    if (!["--no-opt", "--no-maglev"].every((flag) => process.execArgv.includes(flag))) {
      throw new Error("run Mocha as `node --no-opt --no-maglev <mocha>/bin/_mocha`");
    }
    // Loaded before the suite's own setup files, which may guard Object.prototype.
    v8toIstanbul = createRequire(resolve("node_modules/c8/package.json"))("v8-to-istanbul");
    // Starting coverage clears V8's invocation counts, so it must precede the spec imports.
    session.connect();
    await session.post("Profiler.enable");
    await session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
  } catch (error) {
    fail(error);
  }

  return {
    async beforeEach() {
      try {
        if (activeTest !== undefined) throw new Error("overlapping tests share coverage counters");
        const title = `${relative(process.cwd(), this.currentTest.file)}::${this.currentTest.fullTitle()}`;
        // Repeated titles stay distinct in run order: the first is bare, later ones get #n, skipping
        // names a real title already took.
        let seen = occurrences.get(title) ?? 0;
        do {
          seen += 1;
          activeTest = seen === 1 ? title : `${title} #${seen}`;
        } while (Object.hasOwn(tests, activeTest));
        occurrences.set(title, seen);
        // Taking coverage resets the counters, so the test's own sample starts here.
        await session.post("Profiler.takePreciseCoverage");
      } catch (error) {
        fail(error);
      }
    },
    async afterEach() {
      try {
        const { result } = await session.post("Profiler.takePreciseCoverage");
        const files = {};
        for (const script of result) {
          // c8 excludes node: internals and node_modules; finalize drops anything else LCOV lacks.
          if (!script.url.startsWith("file:") || script.url.includes("/node_modules/")) continue;
          if (!script.functions.some(({ ranges }) => ranges.some(({ count }) => count > 0))) continue;
          const lines = await executedLines(script);
          if (lines.length) files[fileURLToPath(script.url)] = lines;
        }
        tests[activeTest] = files;
      } catch (error) {
        fail(error);
      } finally {
        activeTest = undefined;
      }
    },
    afterAll() {
      try {
        const record = failure === undefined ? { tests } : { failure };
        writeFileSync(join(output, `record-${process.pid}.json`), JSON.stringify(record), { flag: "wx" });
      } catch (error) {
        console.error(`PluginBench per-test coverage not recorded: ${error.message}`);
      }
    },
  };
}

const { PLUGINBENCH_PER_TEST_DIR: output, NODE_V8_COVERAGE: v8Output } = process.env;
if (output && v8Output) {
  console.error("PluginBench per-test coverage disabled: its resets would corrupt NODE_V8_COVERAGE (c8)");
}
// Without a record, finalize leaves redundancy unknown.
export const mochaHooks = output && !v8Output ? await hooks(output) : {};
