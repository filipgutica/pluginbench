# Coverage profiles with per-test data

These files give JavaScript tasks the per-test sidecar that
[redundant added tests](../running-evaluations.md) need. A profile runs inside
the task image without network access, so the profile command writes each
helper script inline. Profiles can also request a [vendored coverage tool](#vendored-coverage-tools). The YAML files are `coverage_profiles` entries to copy
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
   `v8-to-istanbul`. Run 2 sets `--timeout 0`: converting a sample without
   V8's optimizing compilers can take longer than Mocha's 2-second hook limit on
   a busy machine, and one hook timeout would fail run 2. The collector's
   timeout still bounds the command.
3. If run 2 exits with status 0, `node per-test-finalize.mjs
   coverage/lcov.info <dir>` writes `coverage/lcov.info.per-test.json`. It
   keeps only LCOV sources, records tests that reach none of them with an empty
   bitset, and writes nothing if any per-test line was not executed in the LCOV
   run. A failed run 2 can leave a partial record, so it gets no sidecar.

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
test`) in the same position. Tests that share an identity count as one test,
whose lines are the union of theirs, as parametrized cases share one
coverage.py context. csstree has 8 titles shared by 18 tests among 16,725;
true-myth has 3 titles shared by 6 tests among 564.

A test that a submission adds under an existing title therefore joins that
test: it neither renames the existing test nor counts as added. A verbatim copy
that keeps the original's title is not detected as redundant. A copy with a new
title is a new test and is checked as usual.

### Limits

- Tests must run serially in one process. Parallel mode and overlapping tests
  leave no sidecar.
- Work that a test leaves running after it finishes is counted in a later test.
  Suite-level `before` and `after` hooks belong to no test. `beforeEach` and
  `afterEach` hooks belong to their test.
- Retried attempts merge into their test, like repeated titles.
- Worker threads and child processes are not sampled.
- Order-dependent state stays in the measurement. A test that reuses a warm
  cache does not get the lines that built the cache. Running each test alone
  would attribute those lines differently.
- The two runs must execute the same code. If run 2 executes a line that the
  LCOV run did not, finalize writes no sidecar. A line that only run 1 executed
  is missing from every test, so that check cannot catch it.
- A failing test in run 2 leaves no sidecar, so redundancy is unknown for a
  snapshot whose suite fails.
- The per-test pass makes the suite slower. For csstree, the profile took about
  90 seconds instead of under 2 seconds. The sidecar is about 26 MB, and it is
  copied into the evaluation evidence.

## Vitest with the v8 provider: `vitest-*.mjs`

[`true-myth.yaml`](true-myth.yaml) writes its helpers to
`coverage/pluginbench/`, where they resolve the project's `vitest` packages,
and runs the suite twice through the wrapper config
`vitest-per-test.config.mjs`:

1. The coverage run writes `coverage/lcov.info` with
   `--coverage.clean=false`, so it keeps the helpers. Its exit status is the
   command's status.
2. The per-test run sets `PLUGINBENCH_PER_TEST_DIR`. The wrapper then adds the
   per-test setup file and starts workers with `--no-opt --no-maglev`. The
   setup file samples each test in its worker. The custom provider subclasses
   the stock `V8CoverageProvider` and converts each sample with that
   provider's `convertCoverage` and source-map transform, which is the
   pipeline that produced the LCOV report. Its own report is discarded.
3. If the per-test run exits with status 0, `per-test-finalize.mjs` writes
   the sidecar, as for Mocha.

Both runs load `vitest-seeded-random.mjs`. It replaces `Math.random` with a
seeded generator and reseeds it before each test from the test's title, so a
test draws the same values in both runs and in any order. 18 true-myth tests
choose branches with `Math.random()`; without seeding, their lines differed
between runs. With seeding, two runs of the profile produced byte-identical
LCOV reports and sidecars. Other sources of variation, such as `Date.now()` or
`crypto.randomUUID()`, are not seeded.

### AST-aware remapping

Both runs pass `--coverage.experimentalAstAwareRemapping=true`. The default
v8-to-istanbul remapping starts every line at one hit, and Vite's source maps
rarely let a range span a whole TypeScript line, so it reports code that never
ran as executed:

- A never-called function added to `src/unit.ts` reported its lines as executed.
- In a partly covered `if`/`else`, the untaken `else` line reported one hit.
- 889 of 1,114 LCOV lines had exactly one hit, and every test that touched
  `src/maybe.ts` received 215 of its 223 lines as per-test lines.

With AST-aware remapping, those lines report zero hits, and the untaken arm is
missing from the test's per-test lines. On the starting repository the report
has 436 statement lines instead of 1,114, 159 branches instead of 277, and 103
functions instead of 50. In both modes, function declarations and `if`
statements produce no function or branch entries. In AST mode their header
lines leave the line population; the statements inside them stay measured.

**Earlier true-myth coverage is withdrawn.** The default profile read 100% on
every population: 1,114 of 1,114 lines, 277 of 277 branches, and 50 of 50
functions. Those numbers cannot show uncovered code, so earlier true-myth
coverage results need a remeasure with this profile. Measure both snapshots of
a comparison with the same profile.

The provider and setup file depend on Vitest 3.2 internals:
`readCoverageFiles`, `convertCoverage`, the worker `takeCoverage` options, and
the provider name that workers use to load the runtime. Recheck them after a
Vitest upgrade. The remapping option is labelled experimental.

Limits beyond the Mocha ones:

- `test.concurrent` and other overlapping tests leave no sidecar.
- Typecheck-only tests have no runtime sample and are not recorded.
- Seeding changes `Math.random` for the whole suite, including tests that a
  submission adds.


## Vendored coverage tools

A coverage profile can supply a pinned tool when the task image lacks one.
Declare `provisioned_tool` beside the existing command and version:

```yaml
measurement_settings:
  coverage:
    provisioned_tool: {name: coverage.py, version: 7.6.12}
    tool_version: 7.6.12
    format: coverage-py-json
    report_path: coverage.json
    command: [python, -I, -S, /opt/pluginbench-tools/coverage.py/launch-coverage.py, run-module, pytest]
```

Use the task image's Python executable. The portable wheel requires Python 3.9+
and uses the Python tracer. Use `run <script> [arguments]` or
`run-module <module> [arguments]`. The profile runs collection only; PluginBench
runs `json` in a fresh reporting container and writes `coverage.json`.
Other coverage.py options and operations refuse.
PluginBench stages the profile's exact source-file list outside the task tree;
reports include only those files, including unloaded files.

For c8, use `provisioned_tool: {name: c8, version: 10.1.3}` and
`tool_version: 10.1.3`. The image must supply Node 18+. Invoke
`env -u NODE_OPTIONS -u NODE_PATH -u EXPERIMENTAL_MONOCART node
/opt/pluginbench-tools/c8/launch-c8.cjs --all -- <test command>`.
The backend also clears these environment variables before starting Node.
The launcher accepts exactly that collection shape. PluginBench runs `report` in a
fresh reporting container and writes `coverage/lcov.info`, including unloaded
files and filtering output to the staged source-file list. Remapping is off by
default. For the kysely TypeScript profile, set `source_maps: true` inside
`provisioned_tool`; leave it off for cattrs, returns, and yjs.

PluginBench verifies the vendored artifact before runtime checks. Unknown names,
unsupported versions, missing files, changed hashes, and incompatible provisioner
versions refuse. Frozen profiles record the name, version, artifact SHA-256, and
provisioner component version, so each change produces a measurement revision.
The same tool is supplied when mutation reuses the coverage command.

The selected distribution is copied into `/opt/pluginbench-tools/<name>` in each
fresh container. Provisioning leaves `/app`, task dependencies, and Git state
untouched. It refuses a workdir overlapping the tool directory or a symlink at
`/opt` or the tool root. Commands choose when and how to invoke the tool.
The container still runs without network access. Existing profiles without
`provisioned_tool` retain their serialized shape and behavior.

Only the two releases above are supplied. This does not provision runtimes,
install task dependencies, qualify Route B tasks, or configure per-test coverage.
See [artifact provenance and licences](../../src/pluginbench/coverage_tools/README.md).


The Python launcher starts with `-I -S`, places the staged distribution first,
and checks `coverage.__file__` and package dependencies. A finder also refuses
coverage submodules or a loaded optional `tomli` dependency outside that directory.
Collection passes the staged `coverage-config.ini` with `--rcfile`; reporting
passes that same absolute file through coverage.py's `config_file` API. Neither
operation discovers task coverage configuration. The pinned config loads no
plugins, disables parallel mode, and selects one absolute data file outside `/app`.
Reporting reads only the single transferred database. Extra files and links
refuse at the host transfer boundary. There is no append or combine operation.

The measurement container stops before raw output is copied. PluginBench then
creates another container from the same pinned image through the same labelled
Docker path. It stages a new extraction from the verified artifact and copies in
only raw collection data and frozen host source. It reuses no tool directory,
interpreter, dependencies, or filesystem layer the measured process touched.
Reports produced by the measurement container are discarded. Both container
receipts and the transferred data hashes are recorded in `execution.json`.

Collection enables image site packages and task imports because it executes the
measured program. JSON reporting runs in its own `-I -S` process, clears the
environment, and never calls `site.main()` or enables task import paths. It uses
the reporting API because coverage.py's JSON CLI adds the working directory to
`sys.path`. Standard-library code comes from the task interpreter; coverage code
comes from the staged directory. Always invoke the launcher in a new process for
reporting.

The c8 launcher calls the pinned report implementation with the staged
`c8-config.json`, built-in `lcovonly`, and fixed output paths. It never invokes
c8's option parser or discovers `.c8rc*`, `.nycrc*`, or task `package.json` config.
Before conversion, it validates cached, external, and inline source maps against
the frozen source files. Empty mappings, a listed source with zero mapped segments, and unmapped nonblank
generated lines other than line comments refuse. Every mapped source must resolve
inside the task tree;
any embedded `sourcesContent` must match its source file byte for byte. A mismatch
refuses the measurement before LCOV is written.

Both tools use the same exclusion policy: source hints cannot exclude lines or
credit unexecuted lines. The pinned INI clears `exclude_lines` and
`partial_branches`. The pinned c8 converter has no public hint switch, so the
reporting launcher disables its single `_parseIgnore` method. This disables c8,
v8, and `node:coverage` hints, including block and line forms.

Residual risk: code inside the measured process can forge the raw collection data.

Provisioning records resolved module, dependency, configuration, and staged
source-file-list origins in `execution.json` under
`provisioned_tool.runtime_origin`. Provisioner version 4 refuses older saved
profiles. Prepare a new measurement revision with these launcher commands.
Resume fingerprints bind only the distributions selected by the profile.

The portable wheel omits optional `tomli`. The pinned INI configuration works on
Python 3.9/3.10. The measured program can still import its own optional dependencies
after tool verification.
