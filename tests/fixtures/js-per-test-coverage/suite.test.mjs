import "./hooks.mjs";
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { classify, later } from "./subject.mjs";

describe("classification", { concurrency: false }, () => {
  it("positive", () => assert.equal(classify(1), "positive"));
  it("redundant positive", () => assert.equal(classify(2), "positive"));
  it("distinct async", async () => {
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(classify(-1), "nonpositive");
    assert.equal(await later(3), 6);
  });
});
