# Evaluation details

An evaluation fixes the harness, model, reasoning, and tasks. It includes vanilla plus each named guidance package. Attempt order is randomized; there are no automatic repair rounds.

Vanilla submissions are cached in `.baseline-cache` beside the run directories. Reuse requires matching task code, prompt, harness, model, reasoning, runtime, timeout, jobs, and repeat count. Runtime includes the execution backend, CLI version, isolation boundary, and toolchain. Harbor also binds its version, Docker client and plugins, daemon, immutable task and prepared agent image identities, platform, and container resource limits. Guidance packages, reviewer settings, and selected measurements do not change the coding cache key.

A cached cohort includes failed attempts and unknown values. Missing isolation receipts, corrupt entries, or a concurrently reserved cohort stop reuse. Imported submissions keep their original tokens, timing, and evidence. Current grading, measurements, and reviews run again. Reports identify reuse and count new coding invocations separately. Reused baselines are not fresh independent samples; changing the repeat count creates a new cohort.

## Choose inputs

Use [the example YAML](../examples/evaluation.yaml). JSON accepts the same fields. Config paths are relative to the config file; flag paths use your current directory. Use a config or flags. Both accept `--jobs`, `--output`, `--dry-run`, and `--yes`.

Guidance accepts Markdown/text files, skill directories, or a plugin directory containing text skills. Files are supplied in the task prompt. Installation, automatic activation, scripts, hooks, and MCP tools are not measured. Vanilla receives no custom instructions; the harness's built-in instructions remain.

Evaluations use Harbor, installed with `pip install -e '.[harbor]'`. Docker must run Linux containers through a local Unix endpoint, with Compose and Buildx available. Preparation freezes the task image and exact selected harness CLI, retaining the image's Node/npm. It installs locked Linux dependencies and runs the task's visible base checks before model calls. Initial source must remain unchanged; generated test artifacts and personal settings are removed. Each attempt starts from that validated image, including nested workspace dependencies.

A prepared image is reusable only with a successful receipt binding its content, source, installation and check recipes, resources, and cleanup. Missing or inconsistent receipts stop execution. Visible base checks establish environment fitness; separate trusted checks grade each submitted solution. The task prompt leaves verification choices to the agent. This evaluates guidance in a prepared repository, including verification choices, rather than dependency setup skill. Native coding and trusted host grading can use different Node versions; their recorded environments are separate.

CPU and memory limits and Harbor's task environment build timeout come from each task's `task.toml`. Tasks with different CPU or memory declarations need explicit common `container_cpus` and `container_memory_mb` settings. Limits below a task's declaration are rejected; smaller budgets need native fitness validation, which is not yet supported. Docker capacity is checked before preparation or model calls; PluginBench never increases the VM allocation automatically.

The agent's `timeout_seconds` remains a separate evaluation budget, defaulting to 1,800 seconds. The plan records declared and effective limits and labels a changed agent budget. It does not promise model completion within that budget. PluginBench stops the container, records its ID, owner and failure state, exports and freezes the submission, then removes owned resources. An OOM flag or container exit code alone does not determine native agent success: a child process can recover, and a normal stop can produce a nonzero container exit. Partial token counters remain lower bounds, while missing final totals stay unknown.

Separate trusted graders and observers work on disposable copies on the host. This is an adapted verifier environment, rather than the upstream task's separate Linux verifier. Harbor reward scores are not used as replacements for raw measurements.

Codex trials capture JSON events and stderr separately. Diagnostics remain available; malformed stdout, missing captures, and native failures remain failures.

Tasks use the public [DeepSWE Harbor format](https://github.com/datacurve-ai/deep-swe): `task.toml`, `instruction.md`, environment, tests, and solution. The current adapter supports these five tasks at revision `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea`:

- `ofetch-per-origin-circuit-breaker`
- `testem-per-launcher-reports`
- `happy-dom-abort-pending-body-reads`
- `true-myth-iterable-collection-combinators`
- `happy-dom-deterministic-intersectionobserver`

PluginBench prepares source snapshots and checks each verifier against an oracle and negative controls before model calls. Hidden tests stay separate from agent code. These five adapters define trusted correctness, coverage, and Fallow scopes; other Harbor tasks are rejected before model calls. True-myth covers iterable, async, and curried collection APIs across four modules. IntersectionObserver adds asynchronous geometry and threshold notifications to Happy DOM.

Coverage observers use visible tests, never held-out correctness tests. Four adapters run fixed reference tests plus added or modified eligible visible test files; ofetch runs all declared visible test files. Testem coverage uses a controlled clock; its correctness checks use the original clock. True-myth uses pinned Vitest/Istanbul 3.2.4 because its V8 provider reports ambiguous branch coordinates. Other tasks retain their declared providers; coverage rates across tasks describe different populations.

True-myth coverage includes the feature-owning Maybe, Result, Task, and toolbelt modules (including `src/task/**` helpers); LOC and Fallow cover all `src/**`. Helpers added outside those modules are outside its coverage population but remain in LOC and Fallow. True-myth’s observer disables coverage thresholds and measures runtime tests only; typechecking cases do not execute branches. Its correctness verifier retains the original typechecking runner. Testem receives a generated npm lock shared by all groups, recorded in task provenance.

Fallow flags the reference IntersectionObserver’s required public getters (`root`, `rootMargin`, `thresholds`) as unused. These warning counts can include public APIs and are not proof of dead code.

## Read results

The overview averages each task's median so tasks have equal weight. Partial averages include only tasks with measured values; check the task counts before comparing them. Overview deltas appear only when both groups have compatible measurements for every assigned task. Fractional finding counts are averages; each individual review has a whole-number count. Drill into a task for its repetitions. Each package's delta is its value minus vanilla. Separate evaluations can be compared through those deltas when their task, runtime, measurement, and review identities match.

| Measurement | Meaning |
| --- | --- |
| Tokens | Input/output totals from native CLI evidence; missing usage remains unknown. Judge usage is separate. |
| Lines | Source/test lines added and removed in declared scopes, not total patch size. |
| Complexity | Cognitive/cyclomatic maxima for touched functions and new-file functions, separately. |
| Dead code | Unused-code warnings present in the submission but absent from the starting scan. |
| Coverage | Branch/function coverage in the declared source scope. |
| Test overlap | Cases in added or modified visible test files whose removal leaves branch coverage unchanged; a redundancy proxy. |
| Review | Independent AI findings by severity/category, with fixed criteria for every group. |

Unknown measurements are never zero. Coverage and overlap cannot prove assertion value; review findings are judgments. Empty function populations have no complexity maximum. Task-specific correctness checks are always shown, regardless of selected measurements; passing them does not establish that every existing behavior is preserved.

Reviews add one native reviewer invocation per submitted attempt. A native invocation can make several provider requests. Test overlap can take hours and must be explicitly selected. The dry run shows maximum new invocations; cache hits are resolved after preparation. The launch requires confirmation unless `--yes` is supplied.

Independent coding attempts, measurement inputs, and reviews run concurrently. For Harbor, actual jobs are capped by the requested limit, four, Docker CPU capacity, and an 80% Docker memory budget. The default request is the smaller of four and host CPU count. Set `jobs: 2` or `--jobs 2` to request fewer; the resolved limit is saved. Dependency preparation and the two measurements of each snapshot stay sequential. Jobs changes scheduling, not planned model calls, and is part of the comparison identity because contention can affect timing and tests.

On interruption or a fatal error, no further jobs are dispatched; active jobs finish or reach their timeout, and their receipts are preserved. Interruption can therefore take time. Resume uses the saved jobs setting.

Use `pluginbench run --resume runs/<run> --dry-run` to check what can continue, then omit `--dry-run` to resume. Resume requires a completed coding phase. It finishes measurement work and can start a review phase that has not begun. Once a review phase starts, interruption requires a fresh evaluation, even if some reviews remain unstarted. Saved submissions and failed evidence are preserved; existing model calls are never replayed. Resume cannot repair a submitted solution or turn an unavailable measurement into a zero. Citation recovery runs offline and can remove extra indentation or one redundant `after/` prefix when the exact quoted source supports the correction.

Codex and Claude are selectable. Use exact model IDs; Claude aliases such as `sonnet` or `opus` are rejected before calls. Harbor rejects unsupported `ultra` reasoning. Requested effort is labelled separately when native evidence does not report effective effort. Codex coding, trusted grading, measurements, and fixed reviews have been exercised under adapted budgets. Repeated clean end-to-end runs and native Claude execution remain unvalidated.

## Development

```sh
pip install -e '.[dev,harbor]'
pytest
ruff check src tests
mypy src tests
npm ci
npm run build:charts
```

These checks make no model calls.

For a deliberate reviewer repair, the Python API can create a separate `review_study(..., revision=...)` on frozen submissions. `regenerate_report(..., review_revision=...)` validates the fixed review settings and writes beneath that revision. Original settings, reports, failed calls, and usage remain available; coding is not replayed. This is separate from CLI resume.
