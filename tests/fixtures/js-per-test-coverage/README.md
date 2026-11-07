# JavaScript per-test coverage spike

Inspector precise coverage can distinguish serial plain JavaScript tests,
including awaited async work. This fixture uses `node:test` because Mocha was
not installed locally. It installs nothing. Mocha/csstree and the pinned task
images remain unverified.

From the repository root:

```sh
PLUGINBENCH_SPIKE_OUTPUT="$PWD/logs/js-per-test-coverage" \
  node --test tests/fixtures/js-per-test-coverage/suite.test.mjs
```

Node v25.6.1 produced these executed statement lines in `subject.mjs`:

| Test identity | Lines |
| --- | --- |
| `suite.test.mjs::classification > positive` | 2, 3 |
| `suite.test.mjs::classification > redundant positive` | 2, 3 |
| `suite.test.mjs::classification > distinct async` | 2, 5, 9, 10 |

Line 14, in the unused function, stays uncovered. The output directory contains
`lcov.info` and `lcov.info.per-test.json`. Both come from the same observations.
The sidecar stores `{per_test_numbits: {source_path: {test_identity: base64}}}`,
with bit n representing line n, as in coverage.py.

## Boundaries

- The statement population is hand-checked for this fixture. The mapper picks
  the smallest V8 range containing each statement's first character, including
  zero-count ranges. It is not a general JavaScript line-coverage converter.
- Before each test, `takePreciseCoverage` clears earlier counters. After each
  test, another call records and clears its counters. Imports and suite setup
  are excluded; code inside the hook interval is attributed to that test.
- The async test awaits a `setImmediate` and an async source function. Detached
  work after test completion can contaminate later tests. It is unsupported.
- Tests must run serially within each inspector session. Overlapping test hooks
  are rejected. Worker threads and child processes need their own sessions and
  output files; this fixture does not collect them.
- Identity is the repository-relative test filename plus `::` plus the runner's
  full suite/test name (`test.fullName`). Duplicate identities are rejected.
  A renamed test appears new. Parameter cases need distinct names.
- A Mocha root-hook plugin would reset in `beforeEach`, take in `afterEach`,
  and form identity from the relative `this.currentTest.file` and
  `this.currentTest.fullTitle()`. Promise/callback completion must precede the
  final sample. [`docs/coverage-profiles`](../../../docs/coverage-profiles/README.md)
  now implements and validates this design for csstree.

## Vitest and TypeScript: proposed next experiment

Vitest's [V8 provider](https://vitest.dev/guide/coverage.html) uses AST-based
coverage remapping. A TypeScript experiment must retain each worker's transformed
JavaScript, script identity, and composed Vite source maps. Take per-test V8
samples inside that worker, then apply the same transformation/remapping pipeline
as the LCOV producer to each sample before encoding original TypeScript lines.
Do not map generated offsets directly onto TypeScript text or use whole-suite
coverage as per-test data.

Start with serial tests and no concurrent cases. Use a stable relative filename
plus the full suite/case name, reject collisions, and merge worker output only
after mapping. Confirm exact original line sets for duplicate cases, a distinct
branch, and awaited async work. Missing source maps or overlapping cases must
leave redundancy unknown. No Vitest integration or TypeScript remapping is
implemented by this spike.

## Baseline

At `9c7ae3f`, `PYTHONPATH=src ~/code/pluginbench/.venv/bin/pytest -q` returned
1049 passed, 3 failed, and 11 skipped. The existing failures were:

- `tests/test_codex.py::test_readonly_invocation_uses_one_sandbox_and_cannot_escape_packet`
- `tests/test_isolation.py::test_owned_job_cannot_read_shared_files_or_follow_an_escape`
- `tests/test_isolation.py::test_runtime_identity_tracks_replacement_of_selected_executable`
