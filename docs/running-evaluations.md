# Evaluation details

An evaluation fixes the runner and tasks, includes fresh vanilla runs, and adds one group for each named guidance package. Every attempt starts from the same task code with a private runtime home. Attempts run in randomized order. There are no automatic repair rounds.

## Choose inputs

Use [the example YAML](../examples/evaluation.yaml). JSON accepts the same fields. Paths are relative to the config file; flags use your current directory. Use a config or flags, with `--jobs`, `--output`, `--dry-run`, and `--yes` available for either.

Guidance accepts Markdown/text files, skill directories, or a plugin directory containing text skills. Files are supplied in the task prompt. Installation, automatic activation, scripts, hooks, and MCP tools are not measured. Vanilla receives no custom instructions; the harness's built-in instructions remain.

Tasks use the public [DeepSWE Harbor format](https://github.com/datacurve-ai/deep-swe): `task.toml`, `instruction.md`, environment, tests, and solution. The current adapter supports these five tasks at revision `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea`:

- `ofetch-per-origin-circuit-breaker`
- `testem-per-launcher-reports`
- `happy-dom-abort-pending-body-reads`
- `true-myth-iterable-collection-combinators`
- `happy-dom-deterministic-intersectionobserver`

PluginBench prepares source snapshots, keeps hidden tests separate, and checks the verifier against an oracle and negative controls before model calls. This is a local adaptation, not unrestricted Harbor or container support. Unsupported tasks are rejected before spending on models. True-myth covers iterable, async, and curried collection APIs across four modules; IntersectionObserver adds asynchronous geometry and threshold notifications to Happy DOM. They are feature tasks, not targets for producing extra lines.

Coverage observers use the submitted visible tests, never held-out correctness tests. True-myth uses pinned Vitest/Istanbul 3.2.4 because its V8 provider reports ambiguous branch coordinates. Other tasks retain their declared providers; coverage rates across tasks describe different populations.

True-myth coverage includes the feature-owning Maybe, Result, Task, and toolbelt modules (including `src/task/**` helpers); LOC and Fallow cover all `src/**`. Helpers added outside those modules are outside its coverage population but remain in LOC and Fallow. True-myth’s observer disables coverage thresholds and measures runtime tests only; typechecking cases do not execute branches. Its correctness verifier retains the original typechecking runner. Testem receives a generated npm lock shared by all groups, recorded in task provenance.

Fallow flags the reference IntersectionObserver’s required public getters (`root`, `rootMargin`, `thresholds`) as unused. These warning counts can include public APIs and are not proof of dead code.

## Read results

The overview averages each task's median so tasks have equal weight. Partial averages include only tasks with measured values; check the task counts before comparing them. Overview deltas appear only when both groups have compatible measurements for every assigned task. Fractional finding counts are averages; each individual review has a whole-number count. Drill into a task for its repetitions. Each package's delta is its value minus vanilla. Separate evaluations can be compared through those deltas when their task, runtime, measurement, and review identities match.

| Measurement | Meaning |
| --- | --- |
| Tokens | Native input/output totals; judge usage is separate. |
| Lines | Source/test lines added and removed in declared scopes. |
| Complexity | Cognitive/cyclomatic maxima for touched functions and new-file functions, separately. |
| Dead code | Unused-code warnings present in the submission but absent from the starting scan. |
| Coverage | Branch/function coverage in the declared source scope. |
| Test overlap | Cases in added or modified visible test files whose removal leaves branch coverage unchanged; a redundancy proxy. |
| Review | Independent AI findings by severity/category, with fixed criteria for every group. |

Unknown measurements are never zero. Coverage and overlap cannot prove assertion value; review findings are judgments. Empty function populations have no complexity maximum. Task-specific correctness checks are always shown, regardless of selected measurements; passing them does not establish that every existing behavior is preserved.

Reviews add one model call per submitted attempt. Test overlap can take hours. The dry run shows planned calls; the launch requires confirmation unless `--yes` is supplied.

Independent coding attempts, measurement inputs, and reviews run concurrently. The default is the smaller of four and your available CPU cores, resolved and saved before execution. Use `jobs: 2` or `--jobs 2` to override it (1–16). Dependency preparation and the two measurement repeats within a snapshot stay sequential. Jobs changes scheduling, not the number of planned model calls. It is recorded in reports and comparison identity because resource contention can affect timings and test outcomes.

On interruption or a fatal error, no further jobs are dispatched; active jobs finish or reach their timeout, and their receipts are preserved. Interruption can therefore take time. Resume uses the saved jobs setting; older runs use one.

Use `pluginbench run --resume runs/<run> --dry-run` to check what can continue, then omit `--dry-run` to resume. Resume requires a completed coding phase. It finishes measurement work and can start a review phase that has not begun. Once a review phase starts, interruption requires a fresh evaluation, even if some reviews remain unstarted. Saved submissions and failed evidence are preserved; existing model calls are never replayed. Resume cannot repair a submitted solution or turn an unavailable measurement into a zero. Citation recovery runs offline and can remove extra indentation or one redundant `after/` prefix when the exact quoted source supports the correction.

Codex and Claude are selectable. Use exact model IDs; Claude aliases such as `sonnet` or `opus` are rejected before calls. Claude's isolation and receipt handling have fixture coverage; a paid Claude evaluation has not been validated. Requested effort is labelled separately when the harness does not report effective effort.

## Development

```sh
pip install -e '.[dev]'
pytest
ruff check src tests
mypy src tests
npm ci
npm run build:charts
```

These checks make no model calls. Saved runs and their frozen evidence are preserved.
