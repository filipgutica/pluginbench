import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { Session } from "node:inspector/promises";
import { join } from "node:path";
import { after, afterEach, before, beforeEach } from "node:test";

const session = new Session();
const sourceURL = new URL("./subject.mjs", import.meta.url);
const sourcePath = "subject.mjs";
const testPath = "suite.test.mjs";
const source = await readFile(sourceURL, "utf8");
// This spike uses a hand-checked statement population, not a general JS mapper.
const executableLines = [2, 3, 5, 9, 10, 14];
const offsets = source.split("\n").reduce((result, line) => {
  result.push((result.at(-1) ?? 0) + line.length + 1);
  return result;
}, [0]);
const contexts = {};
let activeTest;

before(async () => {
  session.connect();
  await session.post("Profiler.enable");
  await session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
});

beforeEach(async (test) => {
  assert.equal(activeTest, undefined, "concurrent tests cannot share coverage counters");
  activeTest = `${testPath}::${test.fullName}`;
  assert.equal(Object.hasOwn(contexts, activeTest), false, "duplicate test identity");
  // takePreciseCoverage resets the counters, excluding imports and suite setup.
  await session.post("Profiler.takePreciseCoverage");
});

afterEach(async () => {
  const { result } = await session.post("Profiler.takePreciseCoverage");
  const script = result.find(({ url }) => url === sourceURL.href);
  const ranges = script?.functions.flatMap(({ ranges }) => ranges) ?? [];
  const lines = executableLines.filter((line) => {
    const text = source.split("\n")[line - 1];
    const offset = offsets[line - 1] + text.search(/\S/);
    // The narrowest enclosing V8 range wins, including zero-count branches.
    const enclosing = ranges.filter(({ startOffset, endOffset }) =>
      startOffset <= offset && offset < endOffset);
    enclosing.sort((a, b) => (a.endOffset - a.startOffset) - (b.endOffset - b.startOffset));
    return (enclosing[0]?.count ?? 0) > 0;
  });
  contexts[activeTest] = lines;
  activeTest = undefined;
});

after(async () => {
  await session.post("Profiler.stopPreciseCoverage");
  session.disconnect();
  const directory = process.env.PLUGINBENCH_SPIKE_OUTPUT;
  assert.ok(directory, "set PLUGINBENCH_SPIKE_OUTPUT to an output directory");
  await mkdir(directory, { recursive: true });
  const encoded = Object.fromEntries(Object.entries(contexts).map(([name, lines]) => {
    const bits = Buffer.alloc(lines.length ? Math.floor(Math.max(...lines) / 8) + 1 : 0);
    for (const line of lines) bits[Math.floor(line / 8)] |= 1 << (line % 8);
    return [name, bits.toString("base64")];
  }));
  const lcov = [`SF:${sourcePath}`, ...executableLines.map((line) =>
    `DA:${line},${Number(Object.values(contexts).some((lines) => lines.includes(line)))}`),
  "end_of_record", ""].join("\n");
  await writeFile(join(directory, "lcov.info"), lcov);
  await writeFile(join(directory, "lcov.info.per-test.json"), JSON.stringify({
    per_test_numbits: { [sourcePath]: encoded },
  }, null, 2) + "\n");
  console.log(JSON.stringify({ node: process.version, per_test_lines: contexts }));
});
