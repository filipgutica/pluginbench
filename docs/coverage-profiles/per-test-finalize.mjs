// Writes PluginBench's per-test sidecar beside an LCOV report from the single record that a per-test
// run left in <record dir> (mocha-per-test.mjs or vitest-per-test-provider.mjs):
//   node per-test-finalize.mjs <lcov report> <record dir>
// The record maps test identity -> absolute source path -> executed lines. Any problem leaves no
// sidecar, so redundancy stays unknown.
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

function finalize(report, directory) {
  if (existsSync(`${report}.per-test.json`)) throw new Error("per-test sidecar already exists");
  const records = readdirSync(directory).filter((name) => /^record-\d+\.json$/.test(name));
  if (records.length !== 1) {
    throw new Error(`expected one per-test record, found ${records.length}; tests must report from one process`);
  }
  const { failure, tests } = JSON.parse(readFileSync(join(directory, records[0]), "utf8"));
  if (failure !== undefined) throw new Error(failure);
  // Absolute source path -> LCOV's own SF spelling and executed lines.
  const covered = new Map();
  let lines;
  for (const row of readFileSync(report, "utf8").split("\n")) {
    if (row.startsWith("SF:")) covered.set(resolve(row.slice(3)), { sf: row.slice(3), lines: (lines = new Set()) });
    if (row.startsWith("DA:") && Number(row.split(",")[1]) > 0) lines.add(Number(row.slice(3).split(",")[0]));
  }
  if (covered.size === 0) throw new Error("LCOV report names no sources");
  const perTest = {};
  for (const [name, files] of Object.entries(tests)) {
    let recorded = false;
    for (const [path, executed] of Object.entries(files)) {
      const source = covered.get(path);
      if (source === undefined) continue; // outside the LCOV source scope
      // The LCOV comes from the coverage run; a line it never saw executed means the runs diverged.
      const outside = executed.filter((line) => !source.lines.has(line));
      if (outside.length) throw new Error(`${name} executed ${path} lines LCOV reports unexecuted: ${outside}`);
      const bits = Buffer.alloc((Math.max(...executed) >> 3) + 1);
      for (const line of executed) bits[line >> 3] |= 1 << (line & 7);
      (perTest[source.sf] ??= {})[name] = bits.toString("base64");
      recorded = true;
    }
    // Tests that reach no measured source are still recorded, with an empty bitset.
    if (!recorded) (perTest[covered.values().next().value.sf] ??= {})[name] = "";
  }
  writeFileSync(`${report}.per-test.json`, JSON.stringify({ per_test_numbits: perTest }), { flag: "wx" });
}

try {
  finalize(...process.argv.slice(2, 4));
} catch (error) {
  console.error(`PluginBench per-test coverage unavailable: ${error.message}`);
}
