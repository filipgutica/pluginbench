# Coverage profiles with per-test data

These files give JavaScript tasks the per-test sidecar that
[redundant added tests](../running-evaluations.md) need. A profile runs inside
the task image without network access, so the profile command writes each
helper script inline. The YAML files are `coverage_profiles` entries to copy
into an evaluation config. A test checks that each embedded copy matches its
script.

## Mocha with c8: `mocha-per-test.mjs`

[`csstree.yaml`](csstree.yaml) runs three steps:

1. The unchanged c8 command writes `coverage/lcov.info`. Its exit status is the
   command's status.
2. Mocha runs the same suite again without c8, as
   `node --no-opt --no-maglev node_modules/mocha/bin/_mocha`, with the hook as
   the first `--require`. Before each test, the hook resets V8's precise
   coverage. After each test, it converts that test's sample with c8's own
   `v8-to-istanbul`.
3. `node mocha-per-test.mjs coverage/lcov.info <dir>` writes
   `coverage/lcov.info.per-test.json`. It keeps only LCOV sources and rejects
   any per-test line that the LCOV run did not execute.

Why two runs: inspector counter resets are isolate-wide. In one c8 run they
lowered hit counts, and the interval samples split ranges into extra branches.
For csstree, branch entries rose from 3,011 to 3,209.

Why the flags: optimized code loses executions of calls that it inlines. With
the default tiers, two identical csstree runs disagreed on the lines of 235 of
16,725 tests; without Maglev, on 144. With `--no-opt --no-maglev` and with
`--jitless`, all 16,725 agreed. The hook refuses to record without both flags.

### Identity

A test's identity is its spec path relative to the working directory, then
`::`, then Mocha's `fullTitle()`. The first test with a title keeps the bare
identity. Later tests with the same title get ` #2`, ` #3`, and so on, in run
order. A suffix skips any identity that a real title already uses. csstree has
8 repeated titles among 16,725 tests. A verbatim copy of a test is therefore a
new test.

### Limits

- Tests must run serially in one process. Parallel mode and overlapping tests
  leave no sidecar.
- Work that a test leaves running after it finishes is counted in a later test.
  Suite-level `before` and `after` hooks belong to no test. `beforeEach` and
  `afterEach` hooks belong to their test.
- Retried tests get a new ordinal identity for each attempt.
- Worker threads and child processes are not sampled.
- Order-dependent state stays in the measurement. A test that reuses a warm
  cache does not get the lines that built the cache. Running each test alone
  would attribute those lines differently.
- The two runs must execute the same code. If run 2 executes a line that the
  LCOV run did not, finalize writes no sidecar. A line that only run 1 executed
  is missing from every test, so that check cannot catch it.
- The per-test pass makes the suite slower. For csstree, the profile took about
  90 seconds instead of under 2 seconds. The sidecar is about 26 MB, and it is
  copied into the evaluation evidence.
