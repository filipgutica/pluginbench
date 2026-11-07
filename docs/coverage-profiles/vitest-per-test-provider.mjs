// Vitest custom coverage provider that records per-test lines for PluginBench (Vitest 3.2).
//
// Inspector coverage resets are isolate-wide, so per-test samples cannot be taken inside the
// coverage run without changing its report. After the unchanged coverage run, run the suite again
// with this provider, vitest-per-test-setup.mjs as a setup file, workers started with
// --no-opt --no-maglev, a throwaway reportsDirectory and PLUGINBENCH_PER_TEST_DIR set; then write
// the sidecar with per-test-finalize.mjs.
//
// The setup file samples each test in its worker. This provider converts each sample with the
// stock v8 provider's own convertCoverage and source-map transform, which needs AST-aware
// remapping: default v8-to-istanbul remapping counts nearly every line of a loaded file as run.
import { Session } from "node:inspector/promises";
import { realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import v8 from "@vitest/coverage-v8";

// Shared with the setup file, whichever module loads first.
const state = (globalThis.__pluginbenchPerTest ??= { samples: [] });

export default {
  async startCoverage() {
    if (state.session) return;
    try {
      // Optimized code drops counts of inlined calls, so samples would randomly lose lines.
      if (!["--no-opt", "--no-maglev"].every((flag) => process.execArgv.includes(flag))) {
        throw new Error("start Vitest workers with --no-opt --no-maglev");
      }
      state.session = new Session();
      state.session.connect();
      await state.session.post("Profiler.enable");
      await state.session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
    } catch (error) {
      state.failure ??= String(error?.stack ?? error);
    }
  },
  takeCoverage({ moduleExecutionInfo } = {}) {
    // The same module wrapper offsets the stock runtime attaches to its file-level sample.
    const perTest = state.samples.splice(0).map(({ name, result }) => ({
      name,
      result: result.map((script) => ({
        ...script,
        startOffset: moduleExecutionInfo?.get(fileURLToPath(script.url))?.startOffset || 0,
      })),
    }));
    return { result: [], perTest, failure: state.failure };
  },
  // The session stays open for the worker's later test files.
  stopCoverage() {},
  async getProvider() {
    const stock = await v8.getProvider();
    const coverageV8 = realpathSync(resolve("node_modules/@vitest/coverage-v8/package.json"));
    const libSourceMaps = createRequire(coverageV8)("istanbul-lib-source-maps");
    const tests = {};
    let failure;

    class PerTestProvider extends stock.constructor {
      // Workers load the runtime named by the provider, so it must stay "custom".
      name = "custom";

      async readCoverageFiles({ onFileRead, onFinished, onDebug }) {
        let samples = [];
        await super.readCoverageFiles({
          onFileRead(coverage) {
            failure ??= coverage.failure;
            samples.push(...(coverage.perTest ?? []));
            onFileRead(coverage);
          },
          onFinished: async (project, transformMode) => {
            try {
              if (!this.options.experimentalAstAwareRemapping) {
                throw new Error("per-test lines need --coverage.experimentalAstAwareRemapping=true");
              }
              // Tests that share a title are one test, as parametrized cases share one coverage.py
              // context, so a repeated title adds its lines to the earlier test's.
              for (const { name, result } of samples) {
                const files = (tests[name] ??= {});
                const executed = await this.executedLines(result, project, transformMode);
                for (const [path, lines] of Object.entries(executed)) {
                  files[path] = [...new Set([...(files[path] ?? []), ...lines])];
                }
              }
            } catch (error) {
              failure ??= String(error?.stack ?? error);
            }
            samples = [];
            await onFinished(project, transformMode);
          },
          onDebug,
        });
      }

      async executedLines(result, project, transformMode) {
        const converted = await this.convertCoverage({ result }, project, transformMode);
        const map = await libSourceMaps.createSourceMapStore().transformCoverage(converted);
        const files = {};
        for (const path of map.files()) {
          const lines = Object.entries(map.fileCoverageFor(path).getLineCoverage())
            .filter(([, count]) => count > 0)
            .map(([line]) => Number(line));
          if (lines.length) files[path] = lines;
        }
        return files;
      }

      async generateCoverage(options) {
        const map = await super.generateCoverage(options);
        try {
          const record = failure === undefined ? { tests } : { failure };
          const output = join(process.env.PLUGINBENCH_PER_TEST_DIR, `record-${process.pid}.json`);
          writeFileSync(output, JSON.stringify(record), { flag: "wx" });
        } catch (error) {
          console.error(`PluginBench per-test coverage not recorded: ${error.message}`);
        }
        return map;
      }
    }
    return new PerTestProvider();
  },
};
