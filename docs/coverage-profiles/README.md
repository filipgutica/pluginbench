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
3. `node per-test-finalize.mjs coverage/lcov.info <dir>` writes
   `coverage/lcov.info.per-test.json`. It keeps only LCOV sources, records
   tests that reach none of them with an empty bitset, and writes nothing if
   any per-test line was not executed in the LCOV run.

Why two runs: inspector counter resets are isolate-wide. In one c8 run they
lowered hit counts, and the interval samples split ranges into extra branches.
For csstree, branch entries rose from 3,011 to 3,209.

Why the flags: optimized code loses executions of calls that it inlines. With
the default tiers, two identical csstree runs disagreed on the lines of 235 of
16,725 tests; without Maglev, on 144. With `--no-opt --no-maglev` and with
`--jitless`, all 16,725 agreed. The hook refuses to record without both flags.

### Identity

A test's identity is its spec path relative to the working directory, then
`::`, then Mocha's `fullTitle()`. Vitest uses its full test name (`suite >
test`) in the same position. The first test with a title keeps the bare
identity. Later tests with the same title get ` #2`, ` #3`, and so on, in run
order. A suffix skips any identity that a real title already uses. csstree has
8 repeated titles among 16,725 tests; true-myth has 3 among 564. A verbatim
copy of a test is therefore a new test.

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

## Vitest with the v8 provider: `vitest-per-test-*.mjs`

[`true-myth.yaml`](true-myth.yaml) also runs the suite twice:

1. The coverage run writes `coverage/lcov.info`. Its exit status is the
   command's status.
2. The helpers are written to `coverage/pluginbench/`, where they resolve the
   project's `vitest` packages. A wrapper config adds the setup file and starts
   workers with `--no-opt --no-maglev`. The run uses the custom provider and a
   throwaway reports directory. The setup file samples each test in its
   worker. The provider subclasses the stock `V8CoverageProvider` and converts
   each sample with that provider's `convertCoverage` and source-map
   transform, which is the pipeline that produced the LCOV report.
3. `per-test-finalize.mjs` writes the sidecar, as for Mocha.

Both runs pass `--coverage.experimentalAstAwareRemapping=true`. This changes
the existing true-myth profile, so measure both snapshots with the new profile.
The default v8-to-istanbul remapping starts every line at one hit, and Vite's
source maps rarely let a range span a whole TypeScript line. In the true-myth
image, a never-called function added to `src/unit.ts` reported its lines as
executed. 889 of 1,114 LCOV lines had exactly one hit. Per-test lines from that
pipeline were nearly file-level: every test that touched `src/maybe.ts`
received 215 of its 223 lines. With AST-aware remapping, the uncalled lines
report zero hits. On the starting repository, the report has 436 statement
lines with real counts instead of 1,114 lines, 159 branches instead of 277, and
103 functions instead of 50. A function header or an `if` line can leave the
line population; it stays covered by the statements inside it.

The provider and setup file depend on Vitest 3.2 internals:
`readCoverageFiles`, `convertCoverage`, the worker `takeCoverage` options, and
the provider name that workers use to load the runtime. Recheck them after a
Vitest upgrade. The remapping option is labelled experimental.

Limits beyond the Mocha ones:

- 18 true-myth tests choose branches with `Math.random()`. Their lines differ
  between runs, and run 2 can execute a line that run 1 did not, which leaves
  that snapshot's redundancy unknown. All other tests recorded identical lines
  in repeated runs.
- Readiness probes compare whole coverage metrics, including per-test lines,
  across two runs. The random tests make true-myth fail that comparison, so
  `measurement_settings.require_ready: true` stops before coding.
- `test.concurrent` and other overlapping tests leave no sidecar.
- Typecheck-only tests have no runtime sample and are not recorded.
