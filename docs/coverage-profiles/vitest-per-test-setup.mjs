// Vitest setup file that samples each test for vitest-per-test-provider.mjs. Errors are recorded,
// never thrown, so the hook cannot fail a test.
import { afterEach, beforeEach, expect } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { relative, resolve } from "node:path";

// Shared with the provider's worker runtime, whichever module loads first.
const state = (globalThis.__pluginbenchPerTest ??= { samples: [] });
const names = new Set();
const titleCounts = new WeakMap();

// Test titles under a file, as Vitest's currentTestName spells them.
const countTitles = (tasks, prefix, counts) => {
  for (const task of tasks) {
    const title = [prefix, task.name].filter(Boolean).join(" > ");
    if (task.type === "suite") countTitles(task.tasks, title, counts);
    else counts.set(title, (counts.get(title) ?? 0) + 1);
  }
  return counts;
};

// Vitest keeps only a wrapper of the test function, so the test's own call expression in its
// source file stands in for the body: `test(name, fn)`, located with includeTaskLocation.
const callSource = (path, { line, column }) => {
  const ts = createRequire(resolve("package.json"))("typescript");
  const file = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
  const position = file.getPositionOfLineAndCharacter(line - 1, column - 1);
  let call;
  const visit = (node) => {
    if (node.getStart(file) > position || node.end <= position) return;
    if (ts.isCallExpression(node)) call = node;
    ts.forEachChild(node, visit);
  };
  visit(file);
  // `test.each(table)(name, fn)` is located at its inner call.
  while (call && ts.isCallExpression(call.parent) && call.parent.expression === call) call = call.parent;
  if (!call) throw new Error(`no test call at ${path}:${line}:${column}`);
  return call.getText(file);
};

// A unique title is the identity. Tests that share a title are told apart by a hash of their own
// source, so adding one does not rename another; run order numbers only identical sources.
const identityOf = (task) => {
  const { testPath, currentTestName } = expect.getState();
  const title = `${relative(process.cwd(), testPath)}::${currentTestName}`;
  if (!titleCounts.has(task.file)) titleCounts.set(task.file, countTitles(task.file.tasks, "", new Map()));
  if (titleCounts.get(task.file).get(currentTestName) === 1) return title;
  if (!task.location) throw new Error("same-title tests need Vitest's includeTaskLocation");
  const source = callSource(testPath, task.location);
  const hashed = `${title} #${createHash("sha256").update(source).digest("hex").slice(0, 8)}`;
  let name = hashed;
  for (let seen = 2; names.has(name); seen += 1) name = `${hashed}-${seen}`;
  return name;
};

beforeEach(async ({ task }) => {
  try {
    if (!state.session) throw new Error("the PluginBench coverage provider is not active");
    if (state.active !== undefined) throw new Error("overlapping tests share coverage counters");
    state.active = identityOf(task);
    if (names.has(state.active)) throw new Error(`duplicate test identity: ${state.active}`);
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
