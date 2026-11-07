// Vitest setup file that samples each test for vitest-per-test-provider.mjs. Errors are recorded,
// never thrown, so the hook cannot fail a test.
import { afterEach, beforeEach, expect } from "vitest";
import { relative } from "node:path";

// Shared with the provider's worker runtime, whichever module loads first.
const state = (globalThis.__pluginbenchPerTest ??= { samples: [] });
const occurrences = new Map();
const names = new Set();

beforeEach(async () => {
  try {
    if (!state.session) throw new Error("the PluginBench coverage provider is not active");
    if (state.active !== undefined) throw new Error("overlapping tests share coverage counters");
    const { testPath, currentTestName } = expect.getState();
    const title = `${relative(process.cwd(), testPath)}::${currentTestName}`;
    // Repeated titles stay distinct in run order: the first is bare, later ones get #n, skipping
    // names a real title already took.
    let seen = occurrences.get(title) ?? 0;
    do {
      seen += 1;
      state.active = seen === 1 ? title : `${title} #${seen}`;
    } while (names.has(state.active));
    occurrences.set(title, seen);
    names.add(state.active);
    // Taking coverage resets the counters, so the test's own sample starts here.
    await state.session.post("Profiler.takePreciseCoverage");
  } catch (error) {
    state.failure ??= String(error?.stack ?? error);
  }
});

afterEach(async () => {
  try {
    if (!state.session) return;
    const { result } = await state.session.post("Profiler.takePreciseCoverage");
    // The stock runtime's filter, keeping only scripts this test ran.
    const executed = result.filter(
      ({ url, functions }) =>
        url.startsWith("file://") &&
        !url.includes("/node_modules/") &&
        functions.some(({ ranges }) => ranges.some(({ count }) => count > 0)),
    );
    state.samples.push({ name: state.active, result: executed });
  } catch (error) {
    state.failure ??= String(error?.stack ?? error);
  } finally {
    state.active = undefined;
  }
});
