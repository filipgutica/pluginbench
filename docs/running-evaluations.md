# Evaluation details

An evaluation runs the same tasks with vanilla and each named guidance package. Harness, model, reasoning, tasks, and comparison settings stay fixed. Attempt order is randomized; failed attempts are retained.

## Choose inputs

Use [the example YAML](../examples/evaluation.yaml). JSON accepts the same fields. Config paths are relative to the config file; flag paths use your current directory. Use a config or flags. Both accept `--jobs`, `--output`, `--dry-run`, and `--yes`.

Choose one task source: a Harbor `dataset` with `version` and `ids`, GitHub task-folder `urls`, or a local `source` with `ids`.

```yaml
tasks:
  urls:
    - https://github.com/datacurve-ai/deep-swe/tree/0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea/tasks/testem-per-launcher-reports
```

`pluginbench tasks pick --dataset datacurve/deep-swe-1-1 --copy` opens an optional fzf multiselect. Search names and preview prompts. Enter prints a pinned `tasks:` YAML block; `--copy` copies the same block. Escape produces no YAML and leaves the clipboard unchanged. A clipboard error preserves the terminal output and returns a nonzero exit code.

The picker browses the full dataset and downloads focused previews and selected tasks. There is no task-name allowlist. Before evaluation, PluginBench checks each task's execution format and selected measurement capabilities.

Links accept a commit, tag, or single-component branch name. PluginBench pins Git refs to commits and registry versions to content hashes. Cached task assets are checked against their complete file, permission, and origin fingerprints before reuse. The cache lives under `$XDG_CACHE_HOME/pluginbench/tasks` or `~/.cache/pluginbench/tasks`.

Local tasks use `tasks: {source: /path/to/tasks, ids: [task-name]}`. Report rebuilding and resume read frozen evidence without resolving remote sources again.

## Native treatments

Each named treatment declares native instruction files, standalone skill roots or collections, and complete plugin directories:

```yaml
treatments:
  core:
    guidance: [/path/to/core.md]
  core-quality:
    guidance: [/path/to/core.md]
    skills: [/path/to/quality-skill]
  workflow:
    plugins: [/path/to/workflow-plugin]
capabilities:
  subagents: true
  max_threads: 4
```

Vanilla is included automatically. The task prompt is identical across arms. Guidance files become the attempt's private native global `AGENTS.md`; they are never appended to the task prompt or written into the source repository. Combined guidance is limited to 32,768 bytes, including separators, to prevent native truncation.

Skills retain their complete directories, including scripts, references, and executable modes. Plugins retain their manifests and full package trees and install through a private local marketplace. Git internals are excluded. Symlinks, nonregular assets, and ambient dotenv files are rejected. Skill dependencies, MCP servers, apps, custom agents, commands, plugin dependencies, asynchronous hooks, and non-command hooks are currently unsupported. Admission rejects declared unsupported components.

The private Codex configuration disables the remote plugin catalog and app integrations for every arm. Account-level plugin installations cannot enter the evaluation through authentication.

Before coding, the pinned CLI must discover every declared skill and synchronous command hook with the expected identity and package contents. Missing or undeclared components stop the attempt. Discovery receipts prove availability; they do not prove skill activation or hook execution. Hook execution uses a per-invocation trust bypass inside the owned container for the verified frozen packages.

The same capability policy applies to vanilla and every treatment. Subagents default to enabled, with at most four concurrently running children. `subagents: false` disables native agents explicitly. The selected model determines the native agent runtime. V2 enforces concurrency only; the optional `max_depth` setting (default one) is a V1 preference and has no effect on V2. There is no enforced depth limit for V2 runs. Children inherit the selected model and effort by default; explicit spawn settings can override them. These settings are neither model allowlists nor lifetime call or dollar limits. The attempt timeout bounds runtime, including child work. Dry runs count coding attempts, not total model requests.

New native treatment runs currently require Codex. Legacy file-only `guidance` maps migrate to native instructions; directory entries must move to `skills` or `plugins`. Existing single-handoff text-injection campaigns remain readable, but their delivery identity differs and their baselines cannot be reused for native treatments. Removed multi-stage standards/ledger formats are not supported by report regeneration. CLI flags support `--guidance`, `--skill`, and `--plugin`, each grouped by `--guidance-name`.

## Execution and isolation

Install with `pip install -e '.[harbor]'`. Docker must run Linux containers through a local Unix endpoint, with Compose and Buildx available. The current runner accepts single-step Linux CPU tasks with separate verifiers and Dockerfiles or prebuilt images. Shared verifiers, task-authored Compose, multistep tasks, GPUs, and TPUs are rejected before model calls.

Harbor owns the task instructions, environment, artifact collection, and correctness checks. PluginBench freezes both task images, installs the exact selected agent CLI, and removes inherited personal guidance. It exports the initial repository and records the prepared image before looking up a reusable vanilla baseline. Preparation does not run a task-specific PluginBench dependency recipe or calibrate the task's tests.

Every task, guidance package, and repeat gets a fresh coding container. PluginBench exports the working tree only after Docker confirms that container has stopped and preserves it as diagnostic evidence. Harbor's separate verifier receives the task-defined artifacts and its own trusted tests. Collectors work on copies of the canonical submission; reviewers receive read-only code with identical criteria for every package.

Fresh repository exports omit generated `.pytest_cache` directories and record those exclusions. Archived snapshots keep their original bytes and hashes; measurement replays do not apply this cleanup.

Canonical submission support currently covers four reviewed DeepSWE verifier contracts: True Myth iterable collection combinators, CSSTree shorthand expansion/compression, Adaptix name-mapping aliases, and Dateutil RFC5545 timezone interoperability. Admission checks the complete trusted verifier payload and exact artifact-collection command. Preparation rejects grader entrypoints or path settings that could redirect verification. It also checks the grader image's starting source, Git base inventory, file modes, and verifier payload against the frozen inputs, then records cleanup.

For these contracts, PluginBench reconstructs the hash-bound `model.patch` over the frozen starting source. That reconstruction supplies measurements and review. Generated files present only in the stopped working tree remain diagnostics; files included in the patch remain part of the submission. Unsupported contracts, missing preparation proof, or unsafe/unverifiable patches produce no canonical submission. They never fall back to the diagnostic tree.

A native reward applies to the canonical submission only when its source binding is valid and the task supplies one binary reward. Patches that overlap paths replaced by trusted tests remain measurable, but their source-bound correctness is unknown. Failed attempts and usage receipts remain visible. The raw verifier result is retained even when source-bound correctness is unknown; it does not establish correctness of authored tests that the verifier replaced. The legacy `graded_source_digest` and `measured_source_digest` fields both identify the reconstructed canonical submission, not separate post-verification exports. Their equality alone is not proof: validation independently reconstructs the patch and compares content and file modes against the saved canonical snapshot.

CPU and memory budgets must cover both coding and verifier requirements. Tasks with different declarations need explicit common `container_cpus` and `container_memory_mb` settings. Smaller budgets are rejected. Docker capacity is checked before preparation; PluginBench never increases the VM allocation automatically.

`timeout_seconds` is a separate agent budget, defaulting to 1,800 seconds. The plan records native and effective budgets. Setup, artifact collection, and verification have separate bounded allowances. A timeout preserves the attempt; incomplete usage remains unknown or a labelled lower bound.

Cleanup removes owned containers, networks, sidecars, temporary homes, and job copies, and records residual resources. Frozen evidence and reusable task/prepared-image/baseline caches remain. A restart can bypass cleanup; PluginBench does not delete unowned resources. See [Security](../SECURITY.md).

## Measurements

The same frozen settings apply before and after each submission. The dry run reports unsupported measurements before paid calls; preparation freezes observer settings and executes selected collectors twice on the actual starting source. An available collector can still fail during measurement. Those failures are retained separately from missing support.

| Measurement | Meaning and current support |
| --- | --- |
| Tokens | Native root and reachable descendant input/output totals, deduplicated by session. Missing final totals or child logs make totals unknown; observed usage remains a lower bound. Arbitrary hook subprocess usage is unmetered, so hook treatments retain native-session usage separately and report total usage as unknown. Reviewer usage is separate. |
| Lines | Source/test lines added and removed. Default scope covers the repository, with tests classified separately. |
| Complexity | Cognitive/cyclomatic maxima for touched functions and new-file functions, separately. Fallow covers JS/TS; pinned language analyzers cover Python, Go, and Rust. |
| Dead code | Static unused-code warnings introduced since the starting scan. Fallow covers JS/TS; Python, Go, and native Rust analyzers report unused candidates. Warnings can include required public APIs. |
| Coverage | Independent line/statement/branch/function counts from LCOV, coverage.py JSON, or Go coverprofiles. Go profiles measure statements. |
| Test overlap | Explicitly selected pytest leave-one-out branch contribution for runnable cases in changed test files. Unchanged coverage does not prove redundant assertions. |
| Review | Independent AI findings by severity/category, with fixed criteria across vanilla and guidance. |

JS/TS analysis requires Node, Corepack, and Fallow 3.18.0. PluginBench retains real entrypoint inference and checks discovery/parse completeness. A missing or different tool version makes those measurements unavailable, without blocking tokens, lines, or review.

Coverage requires a command supported by the frozen task image, a relative report path, and the installed tool version:

```yaml
measurement_settings:
  coverage:
    command: [npm, run, coverage]
    report_path: coverage/lcov.info
    tool_version: "the exact installed coverage tool version"
```

The command runs without model credentials in a fresh copy of the frozen task image. PluginBench does not install a coverage framework automatically. Each available coverage population is retained independently; missing units stay unknown. The coverage.py producer version is checked against JSON metadata. LCOV and Go producer versions are operator-declared and bound to the frozen image. Optional `source_scope`, `test_scope`, and a fully resolved JSON `fallow_config` can restrict measurement. Fallow `extends` and linked configuration files are rejected.

Use `format: coverage-py-json` for coverage.py JSON, or `format: go-coverprofile` for Go. `coverage_profiles` maps a language or exact task metadata `repository_url` to the same command/format settings; repository entries override language entries and the shared `coverage` default.

Coverage.py can trace lines outside its executable-statement population. A trusted reporting command can add `statement_lines` from `Coverage.analysis2` to every file, with `meta.statement_lines_source: coverage.Coverage.analysis2`. Use the same reporting configuration, retain the original JSON, and keep strict covered/missing and summary checks; without statement coordinates, an inconsistent report remains unavailable.

A coverage profile's `source_scope` declares its measured package independently of repository-wide LOC and analysis.

LCOV callbacks can share names. When mixed hit counts cannot be matched to source lines, function coverage for those files stays unknown, with a reason. Line and branch coverage remain available.

For optional pytest contribution, select `test_overlap` and provide:

```yaml
measurement_settings:
  test_contribution:
    python: python
    pytest_args: [-q]
    pytest_version: "8.4.2"
    coverage_version: "7.10.7"
```

These exact versions must be installed in the frozen task image. If you also select `coverage`, the pytest reference supplies its line and branch coverage. Omit the ordinary coverage profile for that repository; conflicting profiles are rejected before coding. Each reference and omission gets a fresh container. Candidates are runnable cases in added or modified visible test files. Skipped cases remain distinct. Every remaining reference case must pass; changed discovery or invalid evidence stops collection. Unchanged branch coverage is not proof of equivalent assertions, and candidates cannot necessarily be removed together.

`measurement_settings.require_ready: true` requires a qualified submission source and repeatable selected collectors before any coding call. Selected unused-code measurements also require unambiguous declaration matching in the starting population. Receipts are saved in `measurement-readiness.json`; test-command outcomes remain separate from coverage availability. A preparation/readiness interruption makes no coding calls. Repair its inputs and start a new evaluation. Post-coding resume continues to use frozen submissions.

Optional local analyzer versions are pinned: Mozilla rust-code-analysis CLI 0.0.25, Vulture 2.16, Go 1.26.4, Staticcheck 0.8.2, gocognit 1.3.0, and gocyclo 0.6.0. Install RCA/Vulture/Staticcheck on PATH when selecting these measurements. Rust unused analysis requires Rust/Cargo 1.92.0 inside the frozen task image and a locked offline build. The shipped Go observer builds its pinned scoring libraries in the observer cache during preparation. Analysis does not download project dependencies. Go unused-code collection needs packages to load under the fixed build settings; missing dependencies or excluded files stay unavailable.

Python complexity counts RCA's reported function spaces. Anonymous closures have no separate score; enclosing function scores may include their complexity. Counts and maxima describe this declared population, not all closures.

## Baselines and reports

Vanilla submissions are cached in `.baseline-cache` beside run directories. Reuse requires matching task assets, source policy, source, prompt, harness, model, reasoning, CLI, runtime, image identities, transport and treatment-delivery policies, capability settings, resources, timeout, jobs, seed, and repeat count. Guidance, reviewer settings, and selected measurements do not change the coding cache key. Cache admission validates the diagnostic export and canonical reconstruction independently, including file modes. Older cache entries without the current source policy cannot be reused.

A cached cohort includes failures and unknowns. Missing isolation receipts, corrupt entries, or a concurrent reservation stop reuse. Imported submissions retain original usage and timing; current native verification, selected measurements, and reviews run again. Reports disclose reuse. Reused runs are not fresh independent samples.

Open `report/report.html`. `results.json` contains raw observations, capabilities, grading evidence, and baseline deltas. `results.csv` contains individual measurements. Rebuild with `pluginbench report runs/<run>` without model calls.

The overview gives each task equal weight by averaging its median. Complexity and unused candidates are grouped by language and analyzer; unlike scores are not pooled. Fractional finding counts are averages; individual reviews have whole-number counts. Drill into a task to see repetitions. A package's delta is its value minus vanilla. Separate evaluations need matching task, runtime, measurement, and review identities before comparing deltas.

Unknown values are never zero. Empty function populations have no complexity maximum. Smaller numbers alone do not establish better work. Coverage cannot prove assertion value, and AI findings do not measure human review churn. Task-supplied checks do not establish preservation of every behavior.

## Concurrency and resume

Coding attempts, measurement inputs, and reviews run concurrently within their phases. Jobs are capped by the requested limit, four, Docker CPU capacity, and an 80% Docker memory budget. Preparation and each snapshot's two measurement repeats stay sequential. Jobs affect scheduling and comparison identity, not the number of coding attempts. Subagents can make additional model requests.

On interruption or fatal failure, new jobs stop. Active jobs finish or time out; their receipts remain. Resume requires a completed coding phase:

```sh
pluginbench run --resume runs/<run> --dry-run
pluginbench run --resume runs/<run>
```

Resume completes pending measurements or starts a review phase that has not begun. Interrupted coding or started review phases require a fresh evaluation. Existing model calls are never replayed. Offline citation recovery can fix formatting only when the exact source supports it.

Use exact model IDs. Harbor does not support `ultra` reasoning. Requested effort is separate from effective effort when native evidence cannot verify it. Claude remains available for review configuration; new native treatment coding is qualified for Codex only.

## Development

```sh
pip install -e '.[dev,harbor]'
pytest
ruff check src tests
mypy src tests
python -m build
npm ci
npm run build:charts
```

These checks make no model calls. The Python API can create a separate review revision on frozen submissions. Report projection validates fixed criteria and preserves earlier failed calls and usage; it never replays coding.

Codex runs explicitly enable Code Mode tool access in the private harness configuration. This fixed setting applies to vanilla, treatments, and review calls; host configuration remains excluded. CLI arguments and effective model/effort evidence are retained.
