// Wrapper around the project's Vitest config for both coverage runs; the profile writes it to
// coverage/pluginbench/. Both runs seed Math.random. With PLUGINBENCH_PER_TEST_DIR set, the
// per-test run also samples each test and starts workers without optimizing compilers.
import { mergeConfig } from "vitest/config";
import base from "../../vitest.config.ts";

const helper = (name) => new URL(name, import.meta.url).pathname;
const perTest = Boolean(process.env.PLUGINBENCH_PER_TEST_DIR);
const flags = ["--no-opt", "--no-maglev"];

const config = mergeConfig(base, {
  test: {
    setupFiles: [helper("./seeded-random.mjs"), ...(perTest ? [helper("./setup.mjs")] : [])],
    ...(perTest && {
      // Same-title tests are identified by the source at their location.
      includeTaskLocation: true,
      poolOptions: { forks: { execArgv: flags }, threads: { execArgv: flags } },
    }),
  },
});
// The per-test run's own report is discarded, so its thresholds would only add noise.
if (perTest) delete config.test.coverage.thresholds;
export default config;
