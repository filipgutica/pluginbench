// Vitest setup file that makes Math.random repeatable per test, so the coverage run and the
// per-test run execute the same branches. Each test reseeds from its own identity, so its values do
// not depend on run order or on which other tests ran.
import { beforeEach, expect } from "vitest";
import { relative } from "node:path";

let state = 0;

// FNV-1a: a 32-bit seed from the test identity.
const seedFrom = (text) => {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(index), 0x01000193);
  }
  return hash >>> 0;
};

// mulberry32, a small seeded generator with Math.random's range [0, 1).
Math.random = () => {
  state = (state + 0x6d2b79f5) | 0;
  let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
  mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
  return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
};

beforeEach(() => {
  const { testPath, currentTestName } = expect.getState();
  state = seedFrom(`${relative(process.cwd(), testPath)}::${currentTestName}`);
});
