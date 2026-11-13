# Evaluation details

An evaluation runs the same tasks with vanilla and each named guidance package. Harness, model, reasoning, tasks, and comparison settings stay fixed. Attempt order is randomized; failed attempts are retained.

[Configuration reference](config-reference.md): evaluation and gates fields, types, defaults, and descriptions.

[Metrics glossary](metrics.md): definitions, units, and exact metric names for gates.

[Reproduce an evaluation](reproduce.md): pinned inputs, saved receipts, and a vanilla-only public example.

## Choose inputs

`pluginbench init [path]` writes [the starter config](../src/pluginbench/evaluation.example.yaml) to `evaluation.yaml`, or to the path you give, and never overwrites a file. JSON accepts the same fields. Paths are relative to the config file. Every setting lives in the config. `run` takes only `--output` (the new run directory, default `runs/<timestamp>`), `--dry-run`, `--json` (with `--dry-run`, the full plan as JSON), `--yes`, `--foreground`, and `--notify`. A run directory resumes with its saved settings, so `--output` applies only to a config.

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

Vanilla is included automatically. Use `treatments: {}` for a vanilla-only screen. The task prompt is identical across arms. Guidance files become the attempt's private native global `AGENTS.md` (Codex) or `CLAUDE.md` (Claude Code); they are never appended to the task prompt or written into the source repository. Combined guidance is limited to 32,768 bytes, including separators, to prevent native truncation.

Skills retain their complete directories, including scripts, references, and executable modes. Plugins retain their manifests and full package trees and install through a private local marketplace. Git internals are excluded. Symlinks, nonregular assets, and ambient dotenv files are rejected. Skill dependencies, MCP servers, apps, custom agents, commands, plugin dependencies, asynchronous hooks, and non-command hooks are currently unsupported. Admission rejects declared unsupported components.

The private Codex configuration disables the remote plugin catalog and app integrations for every arm. Account-level plugin installations cannot enter the evaluation through authentication.

Before coding, the pinned CLI must discover every declared skill and synchronous command hook with the expected identity and package contents. Missing or undeclared components stop the attempt. Discovery receipts prove availability; they do not prove skill activation or hook execution. On Codex, hook execution uses a per-invocation trust bypass inside the owned container for the verified frozen packages.

The same capability policy applies to vanilla and every treatment. Subagents default to enabled. Codex runs at most four concurrently running children. `subagents: false` disables native agents explicitly. The selected model determines the native agent runtime. V2 enforces concurrency only; the optional `max_depth` setting (default one) is a V1 preference and has no effect on V2. There is no enforced depth limit for V2 runs. Children inherit the selected model and effort by default; explicit spawn settings can override them. Claude Code has no concurrency limit, and a Claude run refuses `max_threads` or `max_depth` when either is set, as in the example above; see [Claude Code coding](#claude-code-coding). These settings are neither model allowlists nor lifetime call or dollar limits. The attempt timeout bounds runtime, including child work. Dry runs count coding attempts, not total model requests.

Legacy file-only `guidance` maps migrate to native instructions; directory entries must move to `skills` or `plugins`. Existing single-handoff text-injection campaigns remain readable, but their delivery identity differs and their baselines cannot be reused for native treatments. Removed multi-stage standards/ledger formats are not supported by report regeneration.

### Claude Code treatments

With `runner.harness: claude`, every arm, vanilla included, uses delivery policy `claude-native-v1`. Each component goes into the attempt's private Claude Code configuration directory:

| Component | Delivered as |
| --- | --- |
| `guidance` | The private `CLAUDE.md`, under the same 32,768-byte limit. Auto-memory is off. |
| `skills` | Skill directories in the private configuration |
| `plugins` | Installed in user scope from the private local marketplace `pluginbench-frozen`. The CLI loads each plugin from that staged folder. |
| Hooks | Synchronous `command` hooks from plugins only |

Admission applies these Claude Code rules in addition to the shared ones. A treatment that breaks one stops the run before any model call.

| Part | Rule |
| --- | --- |
| Plugin manifest | Only `.claude-plugin/plugin.json` is read. A root `plugin.json` is refused, because Claude Code would ignore it. `displayName` is accepted as metadata. |
| Plugin default paths | `agents/`, `commands/`, `workflows/`, `output-styles/`, `monitors/`, `.mcp.json`, `.lsp.json`, a root `settings.json`, and a root `SKILL.md` are refused. `bin/` and `themes/` are admitted and recorded. |
| Plugin skills | `skills/` is always scanned, even when the manifest names other skill paths. |
| Skill frontmatter | Only `name`, `description`, `dependencies`, `license`, `metadata`, `allowed-tools`, `disable-model-invocation`, `user-invocable`, and `argument-hint`. Keys such as `model`, `effort`, `context`, `agent`, and `hooks` are refused. |
| Hook files | Only the `hooks` and `description` keys. The default `hooks/hooks.json` is checked even when the manifest names other hook files. |
| Hook matchers | Empty, `*`, or exact names of letters, digits, and `_` joined by `\|`. Names match exactly: `start` does not match `startup`. |
| Duplicate hooks | Two hooks with the same event and command are refused, even in different plugins. The CLI names a hook run only by its event and command, so the session could not tell them apart. |

Before coding, a discovery run of the pinned CLI checks what loaded. It sends control requests only, never a user message, so no model turn starts. Hooks are disabled during discovery. The attempt stops before coding unless every check passes:

| Check | Requirement |
| --- | --- |
| No model call | The session cost reports zero usage and $0. |
| CLI | The reported version equals the pinned version. |
| Authentication | The subscription credentials file is the only credential source. |
| Skills and commands | The skills dialog lists exactly the declared skills, each from its declared source. Each skill is on, or user-invocable-only when its frontmatter sets `disable-model-invocation: true`. The non-built-in commands equal the declared skills without `user-invocable: false`. No undeclared agent is present. |
| Guidance | The only memory file is the declared `CLAUDE.md`, or none when the arm declares no guidance. |
| Plugins | Only the declared plugins are installed and enabled, and their trees match the frozen inventory. |
| Hooks | The hook listing equals the declared hooks, and no hook ran. |
| Settings | The effective settings equal the pinned settings. |
| Environment | No refused variable is set, the pinned variables have their pinned values, and `/etc/claude-code` managed settings are absent. |

The receipt is saved as `native-treatment.json`. After coding, the attempt fails unless:

- its single `system/init` reports the receipt's CLI version, skills, commands, agents, and plugins, the runner model, no MCP servers, and no API key;
- the permission mode is `bypassPermissions` and no permission denial is recorded;
- the offered tools equal the pinned tool set for CLI 2.1.322; other versions are checked only for the disallowed tools;
- each listed `SessionStart` hook that matches `startup` ran once before the first model reply;
- every hook that ran is listed and responded once, with exit 0 for `SessionStart` hooks and 0 or 2 for others;
- the delivered skill and plugin trees are unchanged at the end of the run.

A failed check keeps the attempt and its verifier result. Its execution outcome is a failure, with the reason in `execution.json`.

## Execution and isolation

Installing PluginBench also installs Harbor 0.24.0; see [Install](../README.md#install). Docker must run Linux containers through a local Unix endpoint, with Compose and Buildx available. The current runner accepts single-step Linux CPU tasks with separate verifiers and Dockerfiles or prebuilt images. Shared verifiers, task-authored Compose, multistep tasks, GPUs, and TPUs are rejected before model calls.

### Verified coding CLIs

Codex CLI trace layouts are verified for **0.162.1**, **0.167.0**, and **0.167.1** in [`codex_events.py`](../src/pluginbench/codex_events.py).
Other versions warn once and continue: matching plain sessions are measured, while incompatible traces or unverified inherited-history layouts leave that attempt's tokens unavailable.
Reports mark missing tokens as unknown rather than zero; comparisons show unmeasured attempts, and token gates cannot pass on missing measurements.
Cache reuse and `report` re-run the current per-attempt validator on saved Codex stdout and session traces. Invalid evidence or totals that disagree with the recorded usage make tokens unavailable, with a reason. Legacy stdout-only totals also become unavailable. Token gates treat unavailable usage as unknown and cannot pass on it.
The `usage_validation` receipt retains the recorded totals, partial usage, accounting, and recording time beside the new result. Historical receipts use their file modification time, explicitly labelled as such. Reports and `results.json` expose this receipt in token provenance. `compare` accepts numeric token observations only when their `usage_validation.status` is `validated`, regardless of report schema version. Older observations without a validation receipt count as unknown, and compare prints one report-rebuild note per affected run. An existing `unavailable` or other non-validated verdict also stays unknown, with its validation reason in the missing-values section, but produces no rebuild note. Raw evidence files are never rewritten by re-validation.
Set `runner.cli_version` to an exact version string, such as `"0.167.0"`; ranges and floating tags are rejected.
Prerelease and build suffixes are accepted, such as `"0.167.0-beta.1"` or `"0.167.0+build.2"`.
When omitted or null, preparation reads the host CLI version through [`codex.preflight`](../src/pluginbench/codex.py), which also checks isolation flags.
PluginBench installs the pinned version in the task image and checks it before coding.
`evaluation.json` records `cli_version_source` as `runner.cli_version` or `host`; `inputs/study.json` records the resolved `harness_version`.
The source does not affect comparison or vanilla cache identity; the resolved version does.
Resume uses the saved container version even when the host CLI has updated.
Independent reviews run the host CLI and retain their own preflight and launch evidence; `reviewer.cli_version` must be null.
Codex reviews also require an executable `codex-code-mode-host` beside `codex`.

The container reads credentials copied from the host. Codex 0.167.0 has accepted credentials written by 0.167.1; larger gaps are untested.
Before a coding model call, the container runs `codex login status` without a model request.
If it rejects the credentials, the failure names the pinned container and current host CLI versions.
Re-authenticate with the pinned CLI, or start a new run with a compatible `runner.cli_version`.
This check proves that the CLI can read the credentials; it does not prove that the provider will accept them.

### Claude Code coding

Set `runner.harness: claude` and an exact `claude-*` model ID. Floating aliases such as `sonnet` or `opus` are refused. Reasoning is `low`, `medium`, `high`, `xhigh`, or `max`.

Claude Code coding evidence is qualified for CLI **2.1.322** in [`claude.py`](../src/pluginbench/claude.py). That covers the discovery control requests, the session transcript format, and token reconciliation. Other versions warn once and continue, with the weaker token rule below.
`runner.cli_version` pins the container CLI as it does for Codex. When omitted, [`claude.preflight`](../src/pluginbench/claude.py) reads the host `claude --version` and checks the required isolation flags.

Coding authenticates only with the operator's Claude subscription token:

- Set `CLAUDE_CODE_OAUTH_TOKEN` in the environment that runs `pluginbench`. A service does not inherit a variable that a shell profile exports.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, Bedrock, Vertex, and Foundry routing, `DISABLE_PROMPT_CACHING`, and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` are refused in the operator environment, before any container starts, and in every container command.
- The token reaches the container only as a credentials file in the CLI's private configuration, never in a command's environment. The file is removed before Harbor collects artifacts.
- After the run, the saved artifacts and the exported workspace are scanned for the token. A match fails the attempt.
- This applies to coding only. Independent reviews and judges accept `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`.

PluginBench runs Harbor 0.24.0's `ClaudeCode` agent and refuses to launch when the hash of its `run` source changes. It allows only Harbor's setup, the version check, and one coding command, which it replaces with the pinned launch:

| Setting | Value in every arm |
| --- | --- |
| Model and effort | `--model <runner.model> --effort <runner.reasoning>`; subagents use the runner model |
| Permission mode | `bypassPermissions` |
| Setting sources | `user` only, with no MCP servers (`--strict-mcp-config`) |
| Disallowed tools | `RemoteTrigger`, `CronCreate`, `ScheduleWakeup`, `SendMessage`, and `Workflow`; `Agent` too when `subagents: false` |
| Web tools | `WebSearch` and `WebFetch` stay available |
| Background tasks | Disabled, so subagents and Bash commands run in the foreground |
| Other | Auto-memory, updates, and nonessential traffic disabled; hook events included in the stream |

`capabilities.max_threads` and `capabilities.max_depth` are refused on Claude runs when set, because Claude Code does not enforce them. Plans and comparison identities record both as `not_enforced`. `subagents` still applies. `usage_accounting` records the observed subagent depth without limiting it.

Claude token usage is the result event's `modelUsage`, which includes subagents. Each attempt cross-checks it against the session transcripts: one root transcript and one transcript per subagent, matched to its `Agent` call. Any failed rule leaves usage unavailable, with the reasons in `usage_accounting`. The launched CLI version selects the rule:

| CLI version | Compaction in the stream and root transcript | Rule |
| --- | --- | --- |
| 2.1.322 | None | Transcript input and output must equal `modelUsage` for every model. |
| 2.1.322 | Both | Transcripts must not exceed `modelUsage`. The runner model's excess is reported as untranscribed, and other models must be equal. |
| Other | Either | Only input is compared. Transcripts must not exceed `modelUsage`; the runner model's excess is untranscribed, and other models must be equal. |

The stream and root transcript must agree on compaction. The stream also needs one `system/init` and one successful result for the same session, with the runner model and the launched CLI version.
A fresh attempt and `report` run the same rule on the saved stream and transcripts, as Codex re-validation does. `status` shows a fresh Claude attempt as `unvalidated` until the report has been built. `compare` accepts the figure only when the report's validation is `validated`. Claude records no reasoning-output count.

Known limits:

- Claude Code attempts never enter the vanilla cache. Every Claude run codes its own vanilla arm.
- A timeout leaves no result event, so its usage is unknown with no lower bound. Codex keeps a lower bound.
- Automatic compaction within a single prompt has not been observed. The compaction rule is calibrated on a manual compaction only.
- On a compaction run, the runner model's untranscribed excess has no upper bound.
- Command hooks make total usage unknown, as for Codex; see [Measurements](#measurements).

### Container boundary

The host home and sibling run directories are not mounted into coding containers.
Hidden tests and reference solutions stay in the separate verifier environment.
Only the selected provider credential enters a temporary private runtime; synchronous plugin hooks share the coding agent's access. Claude Code reads its token from a private credentials file, which tool commands can also read.
Use trusted task images and packages, and inspect saved traces before sharing them.

Local collectors and reviews require macOS Seatbelt or Linux Bubblewrap; a missing boundary stops execution.
They can access their job copy, private runtime, selected trusted assets, and read-only toolchain paths.
Review code and trusted scripts remain read-only. Codex's inner sandbox is disabled inside this mandatory OS wrapper.
On Linux, Bubblewrap also isolates descendant processes, and `/tmp` is read-only; tools use their private `TMPDIR`.
On macOS, local processes share host networking and process IDs, and system/runtime paths remain readable.
Local static analysis and reviews share host resources. This requires a trusted machine and is not a shared-hosting boundary.

Harbor owns the task instructions, environment, artifact collection, and correctness checks. PluginBench freezes both task images, installs the exact selected agent CLI, and removes inherited personal guidance. It exports the initial repository and records the prepared image before looking up a reusable vanilla baseline. Preparation does not run a task-specific PluginBench dependency recipe or calibrate the task's tests.

Every task, guidance package, and repeat gets a fresh coding container. PluginBench exports the working tree only after Docker confirms that container has stopped and preserves it as diagnostic evidence. Harbor's separate verifier receives the task-defined artifacts and its own trusted tests. Collectors work on copies of the canonical submission; reviewers receive read-only code with identical criteria for every package.

Fresh repository exports omit generated `.pytest_cache` directories and record those exclusions. Archived snapshots keep their original bytes and hashes; measurement replays do not apply this cleanup.

Canonical submission support covers the explicitly reviewed DeepSWE verifier contracts in [`submissions.py`](../src/pluginbench/submissions.py). The [panel manifest](evidence/deepswe-panel-20261109/manifest.json) records the additional contracts reviewed at DeepSWE commit `0b9fabbb`. Admission checks the complete trusted verifier payload and exact artifact-collection command. Preparation rejects grader entrypoints or path settings that could redirect verification. It also checks the grader image's starting source, Git base inventory, file modes, and verifier payload against the frozen inputs, then records cleanup.

For these contracts, PluginBench reconstructs the hash-bound `model.patch` over the frozen starting source. That reconstruction supplies measurements and review. Generated files present only in the stopped working tree remain diagnostics; files included in the patch remain part of the submission. Unsupported contracts, missing preparation proof, or unsafe/unverifiable patches produce no canonical submission. They never fall back to the diagnostic tree.

A native reward applies to the canonical submission only when its source binding is valid and the task supplies one binary reward. Patches that overlap paths replaced by trusted tests remain measurable, but their source-bound correctness is withheld. Such an attempt is an `overlap_pass` when its native reward is 1 and every fail-to-pass and pass-to-pass test passed, and `fail` when its reward is 0. It is `unknown` otherwise, and also when its reward is 0 while it has pass-to-pass tests and none of them passed: a replaced test file can remove code that the agent's other test files use, so the existing suite may fail to build before any test runs. Reports show overlap passes beside confirmed passes, and a gates file decides whether they count. Failed attempts and usage receipts remain visible. The raw verifier result is retained even when source-bound correctness is unknown; it does not establish correctness of authored tests that the verifier replaced. The legacy `graded_source_digest` and `measured_source_digest` fields both identify the reconstructed canonical submission, not separate post-verification exports. Their equality alone is not proof: validation independently reconstructs the patch and compares content and file modes against the saved canonical snapshot.

CPU and memory budgets must cover both coding and verifier requirements. Tasks with different declarations need explicit common `container_cpus` and `container_memory_mb` settings. Smaller budgets are rejected. Docker capacity is checked before preparation; PluginBench never increases the VM allocation automatically.

`timeout_seconds` is the coding agent budget, defaulting to 1,800 seconds. Set `reviewer.timeout_seconds: 600` to give each independent review a separate 600-second budget. The review timeout must be a positive integer; when omitted or null, it inherits `timeout_seconds`, including for saved evaluations. The plan records native and effective coding budgets. Setup, artifact collection, and verification have separate bounded allowances. A timeout preserves the attempt; incomplete usage remains unknown or a labelled lower bound.

Cleanup removes owned containers, networks, sidecars, temporary homes, and job copies, and records residual resources. Frozen evidence and reusable task/prepared-image/baseline caches remain. A restart can bypass cleanup; PluginBench does not delete unowned resources. See [Security](../SECURITY.md).

## Measurements

[The measurement specification](measurement-spec.md) defines what each measure answers. The same frozen settings apply before and after each submission. The dry run reports unsupported measurements before paid calls; preparation freezes observer settings and executes selected collectors twice on the actual starting source. A frozen profile binds the analysis and coverage observers by declared version. A PluginBench fix that keeps those versions can still measure saved runs. A different observer version, or a source hash that no version declares, is refused. An available collector can still fail during measurement. Those failures are retained separately from missing support.

| Measurement | Meaning and current support |
| --- | --- |
| Tokens and time | Native root and reachable descendant input, cached input, uncached input, output and reasoning-output totals, plus coding time, deduplicated by session. Missing final totals or child logs make totals unknown; observed usage remains a lower bound. Arbitrary hook subprocess usage is unmetered, so hook treatments retain native-session usage separately and report total usage as unknown. Reviewer usage is separate. |
| Lines | Source/test lines added and removed, and added test lines that repeat in six-line blocks. Default scope covers the repository, with tests classified separately. |
| Complexity | Net cognitive/cyclomatic complexity the submission added to source code, functions and source files added, and new or grown functions with a cognitive score above 15. Fallow covers JS/TS; pinned language analyzers cover Python, Go, and Rust. |
| Dead code | Static unused-code warnings and (JS/TS) duplicate-code groups introduced since the starting scan. Fallow covers JS/TS; Python, Go, and native Rust analyzers report unused candidates. Warnings can include required public APIs. |
| Coverage | Changed-line and changed-branch coverage: the share of the submission's added source lines its own tests run. Redundant added tests come from per-test coverage. Whole-suite line/statement/branch/function rates stay in JSON/CSV as diagnostics. Inputs: LCOV, coverage.py JSON, or Go coverprofiles. Go coverprofiles supply statements only, so Go changed-line rates are unknown. |
| Review | Independent AI findings by severity/category, with fixed criteria across vanilla and guidance. |
| Reference | When a task ships `solution/solution.patch`, each measurement revision applies it to the starting source and measures it with the same complexity, unused-code and line collectors. Reports show code measures as a ratio to the reference. |

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

An added test is redundant when the added source lines it executes are all executed by the union of the other tests, existing tests included. Candidates are checked smallest first, and each flagged test leaves the pool, so of two identical added tests one is flagged. Redundant added tests need per-test coverage. For coverage.py, run with `[run] dynamic_context = test_function`, then have the reporting step add `per_test_numbits` to the JSON report: for each measured file, map each context name to its executed lines in coverage.py numbits form (base64), using `CoverageData.contexts_by_lineno`. Do not use `coverage json --show-contexts`; on large suites its output can reach hundreds of megabytes. PluginBench ignores the empty context (code run outside a test). Parametrized cases share one test-function context, so redundancy is per test function. Without per-test data, redundant added tests are unknown.

For LCOV, the same command can generate `<report_path>.per-test.json` next to its
report. For example, `coverage/lcov.info` uses `coverage/lcov.info.per-test.json`:

```json
{
  "per_test_numbits": {
    "src/example.js": {
      "test/example.test.js::suite > case": "Bg=="
    }
  }
}
```

`Bg==` encodes lines 1 and 2: bit n represents source line n. Paths follow the
LCOV source-path rules. Each set bit must name a line LCOV records as executed;
unmeasured files, uncovered lines, invalid base64, and duplicate keys are errors.
Unlike coverage.py's module-entry convention, LCOV sidecars cannot include line 0.
PluginBench normalizes this file-to-test mapping to the existing test-to-file
form and uses the same redundancy calculation. Named tests with empty bitsets
are retained; the empty context name is ignored.

The producer must record all measured test cases, including cases that reach no
source lines, and use stable, unique identities across snapshots. The coverage profiles
merge tests that share a file and full title into one test, as coverage.py does for
parametrized cases, so a same-title copy of a test is not detected as redundant. Use the
report's original-source line population. Generate both reports from the same
run when per-test sampling leaves the report unchanged. V8 inspector coverage
cannot: its counter resets are isolate-wide, so sampling inside c8 or Vitest's
v8 provider changes the report. Such a command may take per-test samples in a
second run of the same suite. Every per-test line must then be executed in the
report, or the command writes no sidecar. A missing sidecar
or one without named test contexts leaves redundancy unknown with a reason; file-level hits never substitute
for per-test data. Both snapshots need per-test data. The frozen input must not
already contain the sidecar, to prevent stale attribution. Native collection
retains it as `report.raw.per-test.json` beside `report.raw`.

The [JavaScript inspector spike](../tests/fixtures/js-per-test-coverage/README.md)
demonstrates serial plain JavaScript and awaited async work with `node:test`.
[Coverage profiles](coverage-profiles/README.md) has per-test helpers for Mocha
with c8 and for Vitest's v8 provider, with the csstree and true-myth profiles
that use them. Vitest needs AST-aware remapping: its default remapping reports
never-called TypeScript lines as executed.

Changed-line coverage is incomplete when an added source file is missing from the coverage report. Configure coverage to report every source file, for example coverage.py `--source=<package>` or c8 `--all`.

LCOV callbacks can share names. When mixed hit counts cannot be matched to source lines, function coverage for those files stays unknown, with a reason. Line and branch coverage remain available.

`pluginbench check <config>` runs every check that needs no model, and `run` runs the same checks before its first coding attempt. Each task gets one readiness state per measure and check:

| State | Meaning | Blocks the run |
| --- | --- | --- |
| `ready` | Measured, or the check passed | No |
| `not_configured` | No collector for this task, such as coverage without a profile | Only with `measurement_settings.require_ready: true`, or when the measure is ready on no task |
| `broken` | Configured but failing, such as a missing tool or a probe that failed | For a measure, only with `measurement_settings.require_ready: true`, or when the measure is ready on no task. For every other check, yes |

A measure that is `broken` for a task is not measured for that task. Other tasks still measure it. `check` prints a warning with the task, the measure, and the reason, and saves it under `warnings` in `check.json`. `check` also saves the list of pairs it did not measure, in `check.json` and with the run. `status` and the report read that list. `status` shows the same warning. With `measurement_settings.require_ready: true`, the check stops instead, so it saves no list and prints no such warning. The report shows each listed measure as unavailable for each attempt on that task, with the reason. It shows no value, result counts, or population for it, counts it as incomplete, and reports its reference value as unavailable too. One collector can serve several measures, so a listed measure's collector can still run after coding. Any value it produces for the listed measure is not reported. If that collector fails as unavailable, only its own measures on that task are affected. Any other failure of that collector after coding fails the measurement of every task, and the run stops without a report or reviews. Such failures include malformed output, evidence that fails validation, and a change to the source or profile it measures. A run with listed pairs saves its measurement revision with protocol version 3. An earlier PluginBench reads only version 2, so it refuses to rebuild that run's report instead of publishing the hidden values. A run saved before this list existed has no such warning, and its report does not change. A measure that is `ready` on no task stops the run in both modes, because the run would measure nothing for it. A broken coding environment, submission source, oracle, or nop always stops the run.

When `dead_code` is `broken` because of an unresolved import, the reason names the importing file and the import. Fallow could not find that file or package, often a build output that the source snapshot does not hold, so it cannot tell which exports are used. To measure dead code without the importing file, add it to `ignorePatterns` in the task's `fallow_config`. Otherwise the measure stays not measured for that task.

The checks:

- The Codex CLI version must be one whose session traces the usage parser was checked against.
- A Codex reviewer needs `codex-code-mode-host` beside the launched `codex`, inside the review sandbox.
- Selected collectors run twice on the starting source. Selected unused-code measurements also require unambiguous declaration matching in the starting population.
- The submission source must qualify when the task has a reviewed patch-grading contract.
- `git commit` must work in the coding image. Graded patches are `git diff BASE HEAD`, so a failing commit makes every patch empty. The first probe uses only the task's environment. If git has no author or committer identity there, PluginBench supplies `PluginBench Agent <agent@pluginbench.invalid>` to that task's agents and probes again. `check` and `check.json` say when this happens, and the identity becomes part of the task's cache key. A task that sets its own identity gets nothing added. A prebuilt `docker_image` is used as published; `check` warns when a Dockerfile beside it is never built.
- Harbor's oracle agent must produce a nonempty patch with reward 1, and its nop agent must get reward 0, through the same commit, patch and verifier path.

Oracle and nop results are cached by preparation key, image IDs and any supplied git identity. A prepared entry reused from an older key keeps that key, so its oracle and nop results are reused too. Only passing results are cached, so a failure is retried on the next check.

`check` also prints hit or miss for every vanilla attempt, computed from saved entries and their contexts without validating their evidence. `run` validates each hit again before it reuses it.

Results are saved in `check.json`, with measurement receipts in `measurement-readiness.json`. Test-command outcomes remain separate from coverage availability. A failed check or interrupted preparation makes no coding calls. Repair its inputs and start a new evaluation. Post-coding resume continues to use frozen submissions.

Optional local analyzer versions are pinned: Mozilla rust-code-analysis CLI 0.0.25, Vulture 2.16, Go 1.26.4, Staticcheck 0.8.2, gocognit 1.3.0, and gocyclo 0.6.0. Install RCA/Vulture/Staticcheck on PATH when selecting these measurements. Rust unused analysis requires Rust/Cargo 1.92.0 inside the frozen task image and a locked offline build. The shipped Go observer builds its pinned scoring libraries in the observer cache during preparation. Analysis does not download project dependencies. Go unused-code collection needs packages to load under the fixed build settings; missing dependencies or excluded files stay unavailable.

Python complexity counts RCA's reported function spaces. Anonymous closures have no separate score; enclosing function scores may include their complexity. Counts and sums describe this declared population, not all closures.

## Baselines and reports

PluginBench keeps prepared task images, vanilla submissions, and oracle and nop results in one cache root: `$XDG_CACHE_HOME/pluginbench`, or `~/.cache/pluginbench`. Every output folder shares it, so a new folder reuses prepared images and their committed image IDs, and gets the same vanilla cache keys. The cache root is resolved before preparation, so a symlinked `~/.cache` works. A symlink inside the cache root, or an output folder that overlaps it, fails the check. Cache decisions name their cohort relative to the cache root (`origin_cohort`). Runs made before this change record an absolute `origin_run` path instead; reports still read it. Earlier `.baseline-cache` and `.prepared-images` directories beside run folders are not migrated.

Cache keys bind the launcher's declared version, not the source of the modules that launch an attempt, and the runtime's exact fields. Docker memory jitter, a Docker update, and a PluginBench fix that keeps the launcher version leave those keys unchanged. Codex session parsing lives in `codex_events.py`, outside the launcher. Prepared-image and vanilla entries saved under an older key by an earlier PluginBench version are still found when their saved identity matches. A prepared entry under the current key is used when it exists. Otherwise, saved entries are scanned in name order, and the first that matches and whose images Docker still holds is used. Each vanilla repeat is read from the run's own key first, then from matching saved cohorts in name order. A cohort can therefore span old and new keys. A saved entry that is unreadable or does not match is skipped. Reuse requires matching task assets, source policy, source, prompt, harness, model, reasoning, CLI, runtime, image identities, transport and treatment-delivery policies, capability settings, resources, timeout, jobs, seed, and repeat count. Guidance, reviewer settings, and selected measurements do not change the coding cache key. Cache admission validates the diagnostic export and canonical reconstruction independently, including file modes. Older cache entries without the current source policy cannot be reused.

Each vanilla repeat is its own cache entry, `<key>/<repeat>`, published as soon as that attempt finishes. A run that stops midway still caches the repeats it finished. A cached cohort includes failures and unknowns. Missing isolation receipts, corrupt entries, or a concurrent reservation stop reuse. Imported submissions retain original receipts and timing; token usage is re-validated from saved evidence. A separate `execution-current.json` holds the current usage projection and native regrade result. Current native verification, selected measurements, and reviews run again. Re-validation does not change cache identity or keys. Claude Code attempts are never cached. Reports disclose reuse. Reused runs are not fresh independent samples.

Open `report/report.html`. `results.json` contains raw observations, capabilities, grading evidence, vanilla deltas, reference values and ratios, and cost rows. `results.csv` contains individual measurements. Rebuild with `pluginbench report runs/<run>` without model calls.
Public reports use `vanilla` for the no-guidance arm. See the [report format](metrics.md#report-files) for schema version 2 and compatibility with older reports.

The overview gives each task equal weight by averaging its median. Complexity and unused candidates are grouped by language and analyzer; unlike scores are not pooled. Fractional finding counts are averages; individual reviews have whole-number counts. Drill into a task to see repetitions. A package's delta is its value minus vanilla. Separate evaluations need matching task, runtime, measurement, and review identities before comparing deltas.

To report dollars, put a dated price table in `prices.json` beside the saved `evaluation.json`, then run `pluginbench report`. Each model maps to `as_of`, `input_per_million`, `cached_input_per_million`, and `output_per_million`. Without it, dollars are unknown.

Unknown values are never zero. A complete scan of a submission that changed no functions adds zero complexity; a missing scan stays unknown. Smaller numbers alone do not establish better work. Coverage cannot prove assertion value, and AI findings do not measure human review churn. Task-supplied checks do not establish preservation of every behavior.

## Concurrency and resume

Coding attempts, measurement inputs, and reviews run concurrently within their phases. `jobs` defaults to 2 on every machine and accepts 1 to 4. Docker capacity is bounded by Docker CPU capacity and an 80% Docker memory budget. A `jobs` value above that capacity fails before preparation instead of being lowered, because jobs is part of the comparison identity. Preparation and each snapshot's two measurement repeats stay sequential. Jobs affect scheduling and comparison identity, not the number of coding attempts. Subagents can make additional model requests.

On interruption or fatal failure, new jobs stop. Active jobs finish or time out; their receipts remain, and an attempt that finishes while the run drains is kept as complete.

A Codex session that fails before its first model turn is a fatal failure, not an outcome. This is a `turn.failed` event with no usage, commands, or agent messages, as when a network or backend outage cuts the container off from Codex. The run stops after the first one with an error that names the attempt and Codex's message. The attempt stays `interrupted` and never enters the vanilla cache. Resume once Codex is reachable: the attempt is discarded and its slot is coded again. Timeouts, and failures after the agent has used tokens, run a command, or sent a message, stay scored.

A Harbor worker process that overruns its budget is recorded like any other failed call. The attempt is kept with its saved usage and logs. Its error says that the worker exceeded its budget, without the command line. When Harbor collected no submission, the attempt is not graded and its correctness is unknown, never a fail. It is not coded again, and the run continues.

When 3 attempts in a row each end with a failed call and no submission, no new attempt starts. "In a row" follows the order in which attempts finish in this invocation. Any attempt that finishes without such a failure resets the count, including a graded failing solution. Reused vanilla attempts do not count. With more than one job, calls already in flight finish. The run then stops with an error that names the 3 attempts and their errors. The 3 attempts stay recorded as they are. Fix the cause, then `pluginbench run runs/<run>` codes the pending slots.

`pluginbench run evaluation.yaml` checks in the foreground, then starts the run in the background and returns once the background process owns `runs/<run>/run.lock`. It prints the `pluginbench status runs/<run> --watch` command. The lock records the process ID, process group, start time, and run directory. The background process appends one JSON line per event to `events.jsonl` and writes its output to `run.log`. Use `--foreground` to run in the terminal instead.

Progress and attempt failure messages call the no-guidance arm `vanilla`, including messages saved in `run.log` and `events.jsonl`. Frozen identities and attempt records keep the internal name `baseline`.

The background process runs in a new session. On Linux with a systemd user manager, it also runs in its own transient user scope (`systemd-run --user --scope`), so it keeps running when the terminal, agent session, or service that launched it ends, even when that launcher's whole cgroup is killed. The scope keeps the launcher's environment, including `PATH`, and the worker's process ID; `run.log` names the scope, and `systemctl --user status <pid>` shows it. Without a reachable user manager, as in most containers and on macOS, the new session survives a closed terminal but not a kill of the launcher's cgroup.

```sh
pluginbench stop runs/<run>
pluginbench run runs/<run> --dry-run
pluginbench run runs/<run>
```

`stop` sends SIGTERM only to the process group recorded in `run.lock`, and only while that process still holds the lock with the recorded start time. No new attempt starts. Attempts already coding finish and are kept for every `jobs` value, including `jobs: 1`. Each Ctrl-C while they finish prints how many calls the run is still waiting for; a second Ctrl-C does not abandon them, because their model calls are already paid for. Resume never codes complete attempts again. A stop or Ctrl-C that arrives while the run saves `evaluation.json` and its hash binding waits until both are written. If the run directory moves while the run is active, the run stops before its next attempt. A moved, stopped run is refused until you delete its `run.lock`.

Resume reads attempt states from the run's single-writer SQLite ledger, `evidence/ledger.sqlite`. Runs from before the ledger load their states from `evidence/run.json`. Complete attempts are kept. Pending attempts run. An attempt whose invocation receipt shows a started model call, but which did not complete, becomes `discarded-interrupted`. Its receipt under `evidence/discarded/` keeps its invocation and any saved usage; usage is unknown when the run was killed before it saved an execution receipt. A new attempt with a derived ID takes the same randomized slot, so discarded attempts are counted in `actual_calls` and listed in `discarded_attempts`. Replacing interrupted attempts can favour faster attempts. Before coding again, resume stops (`docker stop`, never removes) every running container whose Docker Compose project is a PluginBench trial (`pluginbench-<id>__…`) and whose `com.docker.compose.project.working_dir` label lies inside this run's `evidence/frozen/suite/`, where Harbor starts each trial; a progress line names each stopped project as left running by an attempt that a kill interrupted. With coding left, as the ledger and record receipts show, resume uses the frozen container CLI version and accepts a different host CLI version. It refuses a run saved by a different PluginBench launcher version, or by a build whose launcher source no version declares; the error names the versions, or the file whose source no version declares, and the choices are to resume with the build that started the run or to start a new run. Each coding session checks the Harbor and Docker runtime that each task was prepared on, with or without vanilla reuse; each measurement and regrade checks its task's runtime. Protocol, Harbor version, receipt and transport versions, engine platform, container limits, and the launcher version must match exactly. Measurement and regrade start no coding agent, so they skip the launcher version. They still refuse a launcher source hash that no version declares. Docker CPU and memory only have to fit, within the 80% memory budget: coding needs room for `jobs` containers at once, and measurement for the containers its inputs run at once, `jobs` or fewer, counting each input's largest container limits. The engine ID and version, endpoint, Docker CLI and plugins, and PluginBench's source hashes that a declared version stands for are recorded, not compared, and Docker commands use the ones installed now. A drift error names each changed field with its saved and current values; restore that runtime, or start a new run. A run stopped after its last attempt finished, as its ledger and record receipts show, has nothing to code. It finishes without these coding checks. Caching its finished vanilla repeats is then best effort: when the cache cannot be reserved or an entry cannot be published, a progress line names the cache and the reason, and the run still finishes. A cache location that PluginBench refuses, such as a symlinked cache directory, still stops the run. When the engine ID changed, each task's prepared images are checked before the first model call. Existing model calls are never replayed. An earlier PluginBench, such as 942f12c, refuses to continue coding a run that this version started. A run that an earlier version started keeps its saved format when this version resumes it, so the earlier version can still continue it.

Resume then completes pending measurements or starts a review phase that has not begun. Started review phases require a fresh evaluation. A run prepared before measurement profiles were saved, with measurement not yet started, is refused before any coding call. The same applies to a reviewer whose CLI, credentials, or sandbox host fail their check. Offline citation recovery can fix formatting only when the exact source supports it.

Only the process that holds `run.lock` measures. So when a resume holds the lock, a measurement revision that is not finished was left by a process that ended. A revision still marked `collecting` is recorded as `failed`. A revision directory with no `revision.json` was killed while it started; it is left as it is. A revision that failed before it saved its frozen profiles is treated the same way. Measurement then runs again as the next free revision name, such as `measurements-2`. It uses the frozen profiles of a failed revision when one saved them, and otherwise the run's prepared profiles. A progress line names the abandoned revision, why it cannot continue, and the new revision. Earlier revisions keep all their files; only the `collecting` state file is updated. To treat a revision as abandoned, a resume must hold the lock, and a plan must find the lock free or held by its own process. Otherwise each refuses.

`--notify` sends a desktop notification (macOS `osascript`, Linux `notify-send`) when preparation and readiness checks pass and coding starts, and when the run finishes, stops, or fails. A readiness failure therefore arrives before any model call. Notifications are best effort and never change the run's outcome. When the notifier is not on `PATH`, `run` prints a warning before it starts. A worker that is killed, or whose host goes down, cannot send a notification; `status` then shows `interrupted`.

## Watch a run

`pluginbench status runs/<run>` shows the run's phase, the process that owns it, its last activity, attempt states and outcomes per arm and task, coding tokens, vanilla cache reuse, discarded attempts, and the measurement and review revisions. `--watch` redraws every 2 seconds and exits when the run ends or is stopped. A redraw that catches a file mid-write is skipped. `status` only reads: it opens `evidence/ledger.sqlite` read-only and falls back to `evidence/run.json` for runs from before the ledger, or while the ledger cannot be read: busy, left with a crashed writer's journal, or not in rollback-journal mode. It works on any run directory, including stopped and old ones. For each attempt it prefers `execution-current.json` over `execution.json`, so rejected cached usage stays unavailable. It does not parse traces or re-validate historical receipts; recorded Codex or Claude figures without validation provenance are labelled `unvalidated`, with a command to rebuild the report.

`status --json` prints one JSON object for dashboards. Fields are only added within a `schema_version`; a removed or renamed field, or a new value for an existing enum, bumps it. Version 2 adds the `unvalidated` value to `tokens_status`; nothing else changed from version 1. Version 2:

| Field | Meaning |
| --- | --- |
| `schema_version` | `2` |
| `run`, `name` | Absolute run directory and evaluation name |
| `phase` | `starting`, `coding`, `measuring`, `reviewing`, `reporting`, `complete`, `stopped` (the latest operation was stopped, or an unfinished run with saved coding state and no events has no live owner), `interrupted` (no live owner, and the last event is `started` or `progress`: the worker was killed or its host went down before it could record an exit; a process killed before its `started` event is not detected), `failed` (the last event is a failure), or `not_started`. `stop` and Ctrl-C record a `stopped` event, not a failure |
| `resume_command` | The shell-quoted `pluginbench run <run>` command for a run that has work left and no owner, otherwise null. It names the command, not its result: `run` checks the saved state first and refuses, before any model call, a run it cannot continue, such as one whose first review had started |
| `active`, `owner` | Whether a live process holds `run.lock`, and its `pid`, `pgid`, and `started_at` |
| `heartbeat` | `at` and `age_seconds` of the newest write to `events.jsonl`, `run.log`, the ledger, or `run.json` |
| `last_event`, `error` | The last line of `events.jsonl` (`started`, `progress`, `stopped`, `failed`, or `finished`), and the failure message if the run failed |
| `states_source` | `ledger`, `run.json`, or `none` |
| `attempts_total`, `states`, `outcomes` | Live attempt slots, counts by state, and counts by outcome of complete attempts |
| `tokens` | Coding tokens this run spent: `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`, `lower_bound`, and `unvalidated` (whether any counted figure lacks validation). Reused vanilla attempts are excluded; discarded attempts are included |
| `reuse` | `vanilla_attempts`, `planned_hits` (from the plan's cache decisions), and `reused` (imported from the cache) |
| `discarded` | Interrupted attempts a resume replaced: `id`, `slot`, `task`, `arm`, `repeat`, `replaced_by`, `tokens`, `tokens_status` |
| `arms` | Per arm: `attempts`, `states`, `outcomes`, `reused`, and `tokens` (reused attempts included) |
| `measures` | `selected` measurements; `measurement_revisions` and `review_revisions` as `revision`, `status`, `selected`, and for reviews `reviewed` of `assigned`; `references`: task → metric → `status`, `value`, `reason`; `not_measured`: the measures `check` saved as not measured for a task, as `task`, `measure`, `reason`; empty for a run saved before that list existed |
| `report`, `log` | Paths to `report/report.html` and `run.log`, or null |
| `left_running` | Null while a live process owns the run. Otherwise `checked`, `error`, `containers` (`id`, `name`, `project` of each running container of this run's trials, found as resume finds them), and `stop_command` (`docker stop <ids>`, or null when there are none). When Docker cannot be queried, `checked` is false and `error` says why; the terminal view says it could not check. The next resume stops them |
| `attempts` | One row per live slot: `id`, `slot`, `task`, `arm`, `repeat`, `state`, `outcome`, `outcome_reason`, `reused`, `tokens`, `tokens_status`, `tokens_reason` (the rejection reason or rebuild command), `minutes`, and `measures` (metric → `status`, `value`, `unit`, `reason`, from `report/results.json` once it exists) |

States are `pending`, `running`, `complete`, and `interrupted`. Outcomes are `pass`, `overlap_pass`, `fail`, `empty` (the graded patch is empty), and `unknown`, with the verifier's reason in `outcome_reason`. A withheld result is classified as the report classifies it: `overlap_pass` or `fail` from the verifier's rewards, otherwise `unknown`. `tokens_status` is `exact`, `unvalidated` (a recorded Codex or Claude figure without validation provenance; once `pluginbench report` has re-validated it, status shows the report's verdict: `exact`, or `unknown` with the rejection reason), `lower_bound` (usage saved by an unfinished attempt, or only an observed floor), `unknown` (no usage saved yet), or `not_started`. A receipt without `total_tokens` gets input plus output, as Codex counts it; when either is missing the attempt is `lower_bound`. A total's `lower_bound` is true when any attempt it counts is `lower_bound` or `unknown`. A total can include unvalidated recorded figures; its separate `unvalidated` flag and the terminal note disclose them. Rebuild the report to obtain re-validated measurements for comparisons. The vanilla arm is `vanilla`, including in runs that recorded it as `baseline`. For a run that no process owns, the terminal view shows a `running` attempt as `interrupted`, in the summary line and in the table, and likewise a `collecting` measurement revision or a `running` review revision; the JSON keeps the recorded state.

## Review again

`pluginbench review runs/<run> --revision <name>` reviews every submission of a finished run again with the run's frozen reviewer, criteria, seed, and jobs, as review revision `<name>`. It prints the number of review calls and asks before starting unless `--yes` is set; when no answer can be read, such as an empty pipe, it exits and says to pass `--yes`. It owns `run.lock` while it works, so it refuses an active run, and it checks the reviewer's CLI, credentials, and sandbox host before the first call. Earlier revisions stay in evidence. The new revision, or `<name>-citations` when offline citation recovery repaired its formatting, becomes the selected one, and the report is rebuilt. A name that already exists is refused; choose a new one. A run with no reviewer, or whose first review has not finished, is refused.

Use exact model IDs. Harbor does not support `ultra` reasoning. Requested effort is separate from effective effort when native evidence cannot verify it.

## Compare runs

`pluginbench compare <run>... --gates gates.yaml` reads each run's `report/results.json` and prints, for each split and for all tasks, a measure table and a gate table. It makes no model calls and rebuilds nothing. Pass `DIR=ARM,ARM` to take only those packages from a run. The same package and task may come from only one input.

```yaml
count_overlap_passes: true        # required: the study decides
splits:
  climb: [task-a, task-b]
gates:
  - name: passes
    correctness: passes           # or unknown; per_task: true checks each task
    against: vanilla
    direction: higher
    tolerance: 1
  - name: tokens
    metrics: [Input tokens, Output tokens]
    statistic: mean_of_task_medians   # or total, or mean_of_task_median_ratios
    against: vanilla
    direction: lower
    factor: 1.3
  - name: cognitive ratio to reference
    metrics: [Net cognitive complexity added]
    statistic: mean_of_task_median_ratios
    reference_floor: 10           # a smaller reference makes that task missing
    against: workflow-main
    direction: lower
    factor: 1.1
```

Statistics are taken per task first, then averaged. A missing value is never 0: tasks without a value, and tasks measured on only some attempts, are listed under "Missing values" with their reasons. A metric gate is `undetermined` when either package has an assigned attempt without a value for its metrics. This applies to `total` and to both mean statistics. The detail names the metrics and the measured and assigned attempts on each side. Assigned attempts are the attempts the run kept. An interrupted attempt that a resume discarded, and its token use, are not in the report or in token gates. A task that never has a value, such as a task where the measure is not configured or not measured, therefore also makes the gate `undetermined`. To decide the gate, define a named split without that task. The `all` section still includes it, so that gate stays `undetermined` there. A ratio gate leaves out a task without a usable reference: missing, zero or negative, or below `reference_floor`. A negative reference would invert the ratio, so that adding complexity scores below removing it. That task's missing values do not make the gate `undetermined`, the gate says how many tasks it compared, and "Missing values" names each left-out task and the reason. The measure table also shows each task's value under every metric gate, and each ratio task's reference, so one task or a small reference cannot move a mean unseen. A metric gate is `undetermined` when a task it counts has attempts from only one of the two packages; the detail names those tasks. Each gate compares both sides over the tasks both have and says how many, and names tasks whose measured repeat counts differ.

A pass or unknown count gate is `undetermined` when either package has no attempts on a task in the split, or when the two packages have different numbers of attempts on a task: 3 passes of 5 would beat 2 of 3 on count at a lower rate. The detail names those tasks. Give both packages the same repeats, or define a split without those tasks. A pass count gate also checks attempts whose correctness is unknown. It counts each package's unknowns all as passes and all as fails, and decides only when every combination gives the same result. Otherwise it is `undetermined`, and the detail names how many unknowns each side has. An overlap pass that the gates file does not count is a fail, not an unknown.

Compare refuses runs it cannot compare. When a gate compares two packages on a task, and the two packages come from different runs, the saved `comparison_identity` in each run's `report/results.json` must agree on these fields:

- `runner.harness`, `runner.model`, `runner.reasoning`, `harness_version`, and `capabilities`
- the task's identity, `tasks.<task>`, and its agent timeout, `task_limits.<task>.effective.agent_timeout_seconds`
- the measurement scope: `measurement_settings` `source_scope`, `test_scope`, `coverage`, and each repository's `coverage_profiles` entry that both runs saved, and `fallow_config_sha256`
- the analyzers for that task: `effective_observer_scripts.<task>` and each measure's `tool_version` in `measurement_capabilities.<task>`
- the reviewer: `reviewer.harness`, `reviewer.model`, `reviewer.reasoning`, and `review_criteria_sha256`

The error names the two runs, the tasks, and each differing field with both values. Long values are shown as a short SHA-256. The guidance packages are the treatment under test, so they may differ, as may jobs, seed, container limits, and the Docker runtime. A field that only one run saved is not compared: a run without a reviewer, for example, has no `reviewer.model`. Compare names those fields in a `Run identity:` line. Runs saved before reports carried `comparison_identity` still compare, unchecked, and a `Run identity:` line names them.

Each split also has a paired section for every package against each comparator named in the gates file. It matches tasks across runs, including inputs selected with `DIR=ARM,ARM`. For each task, it reports:

- The candidate's pass rate minus the comparator's pass rate, using each arm's assigned repeats and the declared overlap-pass policy.
- The candidate's mean input-plus-output tokens divided by the comparator's mean. Each arm averages its own repeats before division.

Tasks have equal weight, even when their repeat counts differ. The summary reports the mean pass-rate difference and geometric-mean token ratio. It resamples whole task pairs with replacement, using 10,000 draws and seed 0. Two-sided 90% percentile intervals use linear interpolation at the 5th and 95th percentiles. The endpoints are one-sided 95% bounds. Token ratios are averaged and bootstrapped in log space, then exponentiated. Fewer than six usable task pairs produces `too few tasks` instead of an interval. These bootstrap bounds are estimates; six tasks is a reporting minimum, not a guarantee of statistical power or coverage.

An exact two-sided sign test compares positive and negative task pass-rate differences against equal probability. Ties are excluded; all ties gives p=1. It tests direction across tasks, not a non-inferiority margin.

The paired section lists excluded tasks and reasons. A task with any unknown correctness is excluded from pass-rate statistics. A task with any missing, nonfinite, or negative token total is excluded from token statistics. Both mean token totals must be positive. Missing attempts exclude the task from both statistics. Complete tasks remain visible, and exclusions are separate for correctness and tokens.

Optional bound gates use these intervals instead of the existing point statistics:

```yaml
count_overlap_passes: true
gates:
  - name: correctness non-inferiority
    correctness: passes
    against: vanilla
    direction: higher
    bound: lower
    margin: -0.05                 # lower pass-rate difference must be >= -5 percentage points
  - name: token ceiling
    metrics: [Input tokens, Output tokens]
    against: vanilla
    direction: lower
    bound: upper
    factor: 1.3                   # upper geometric-mean token ratio must be <= 1.3
```

Bound gates omit `statistic`: they always average repeats within task. They cannot use `per_task`, `tolerance`, or `reference_floor`. Correctness bounds require `bound: lower`, `direction: higher`, and `margin`. Token bounds require both token metrics, `bound: upper`, `direction: lower`, and `factor`. A bound gate is `undetermined` with fewer than six usable pairs or any excluded task in its split. Otherwise, `pass` means its endpoint meets the declared limit; `fail` means the bound does not establish that requirement. Gates without `bound` work as described above.

## Judging

`pluginbench judge` compares two arms with blind judges, one matched pair at a time: the same task and the same repeat in each arm. It makes no coding runs, and only `judge run` calls models.

```sh
pluginbench judge prepare runs/r3=workflow-r3 runs/r2=workflow-main \
  --tasks task-a,task-b --exclude holdout-a,holdout-b \
  --criteria review-standard.md --seed 11 --output judging/r3
pluginbench judge run judging/r3 --judge codex:gpt-6-astra:high \
  --judge claude:claude-opus-5-5:high --jobs 3
pluginbench judge tally judging/r3
```

`prepare` takes two `RUN_DIR=ARM` specs. They can name one run or two runs, and `vanilla` matches the arm older runs call `baseline`. It reads each run's `report/results.json` and review packets. For a submission with no review packet, it reads the attempt's saved snapshots and the frozen task instruction instead.
- It refuses any task that `--exclude` lists. Put holdout tasks there.
- Each submission's diff comes from its review packet, which must be bound to the attempt's submission and starting source. A run without a reviewer, or one that stopped before review, has no packets: its diffs are built from the saved snapshots, checked against the digests in the report, and written under numbered folders in `submissions/` in the output directory. An attempt with no saved submission, such as one that was not coded, cannot be judged. It is left out with its pair, and `prepare` names it on stderr and under `omitted` in the key.
- Both sides of a pair must start from the same source. They must also have the same instruction.
- Diff headers become `a/` and `b/` with no `index` lines, so neither side's tooling identifies it.
- The seed randomizes A and B for each pair.
- For each task, `task-N/` holds the pair diffs, the instruction, the criteria, the response schema, and a `source` link to the read-only starting source.
- `task-N.brief.md` is the prompt, and `key.json` maps each side to its arm, attempt, and correctness. Both stay outside the task folders.

`run` makes one read-only call per judge and task. It uses the same Codex or Claude launch and identity checks as reviews.

- Each call's sandbox can read only a fresh copy of `task-N/`, with the source copied in. The key, arm names, traces, final messages, and verifier results are never inside it.
- Each call atomically reserves `results/<judge>/task-N/call-K/` before launch and records evidence there directly. A killed call remains an attempt with unknown usage when its usage evidence is missing or incomplete.
- The verdict is saved to `results/<judge>/task-N.json` only after the model, the effort, the read-only trace, and the pair numbers check out.
- Each saved verdict has a `binding` of SHA-256 digests: the presented A/B diffs, starting source, brief, instruction, review standard, response schema, judge spec, prepare seed, and key. Judge specs (harness, model, effort) stay outside the workspace in `judge-specs/<judge>.json`. Inputs changed during a call prevent its verdict from being saved.
- Workspace-file binding keys start with `files/`, separate from metadata keys. Nested symlinks are refused; only prepare's own source-directory link is followed. The judge identity comes from the launch specification and must still match the saved spec before the call.
- `run` checks saved bindings before reusing a verdict. A mismatch requires a new prepared directory; it cannot silently reuse a stale verdict.
- A failed call leaves no verdict. `run` exits 2, and running it again retries only the missing calls.
- Every pair goes to the judges, including pairs that correctness will decide, so the brief reveals nothing about correctness.
- The brief asks for JSON in the final message. A hand-run prompt that asks judges to write a file needs that one change.

`tally` unblinds the verdicts with the key and writes `tally.json` and a text table.

- If exactly one side passed, that side wins without a vote. A pass is `pass` or an overlap pass. Unknown correctness does not count as a pass.
- Otherwise, the side that more than half of the judges prefer wins. Anything else, including ties, scores half to each side.
- It reports weighted wins, correctness-decided pairs, judged pairs, and per-task results.
- For each judge, it reports the side-A rate and its agreement. Agreement counts matching votes over pairs where more than half of the other judges share a verdict (`A`, `B`, or `tie`). Pairs without that majority are excluded for that judge. This rule is the same for every judge, but denominators can differ. With three judges, if two agree and one differs, that pair enters only the differing judge's agreement denominator. The table shows the rate and eligible pair count as `<rate> of <pairs>`. No eligible pairs gives JSON `null` and text `-`.
- Side-A rate is A votes divided by all pairs. Ties enter the denominator and add zero A votes. Correctness-decided pairs enter the side-A denominator and remain eligible for agreement under the same other-judges majority rule. An `unable` verdict blocks tally, so it never enters either measure.
- With at least 10 pairs, a side-A rate outside 0.2–0.8 prints `FLAG: position bias`. The endpoints are inside the band. Below 10 pairs, it prints `sample too small to tell`. JSON records `judge_stats.<judge>.position_bias` as `flagged`, `within_band`, or `too_small`.
- Per-judge token usage sums all recorded calls, including failed attempts before a successful retry. It reads each call's `host-evidence.json`, or parses saved `stdout.jsonl` and native traces for older calls. The text total adds input and output tokens; cached input and reasoning output are subsets and are not added again.
- JSON records `judge_stats.<judge>.usage`: `tokens`, `known_tokens`, `calls`, and `unknown_calls`. If any call lacks complete input/output usage, `tokens` is `null` and the text says `unknown`. `known_tokens` retains available usage, or is `null` when none is known. Optional token counters are summed only when every known call records them. A verdict without any call evidence counts as one unknown call.
- Tally refuses a changed binding and names the verdict file and mismatching field. This includes key replacement after re-preparing the same inputs.
- Tally reads each key and verdict file once. Binding checks and scoring use those same snapshots.
- Legacy verdicts without bindings remain accepted. Each prints `unbound verdict (written before binding)`. JSON counts those files in `unbound` and lists their paths in `unbound_verdicts`. Use `pluginbench judge tally judging/r3 --require-bound` to refuse them. A legacy verdict cannot prove which inputs were judged; rerun in a new prepared directory to obtain bound verdicts.
- A binding also records the judge CLI version, read from the producing call's saved native evidence. A verdict bound before this was recorded prints `needs rebind`; JSON counts those files in `needs_rebind` and lists their paths in `needs_rebind_verdicts`. `--require-bound` refuses them too. Delete the verdict and run `pluginbench judge run` to judge it again. A bound verdict whose call evidence is missing or reports a different version is a binding mismatch.
- It refuses to tally while any judge lacks a verdict for any task. That includes every judge `run` was asked for, even one that never started.

## Development

```sh
pip install -e '.[dev]'
pytest
ruff check src tests
mypy src tests
python -m build
npm ci
npm run build:charts
```

These checks make no model calls. Report projection validates fixed criteria and preserves earlier failed calls and usage; it never replays coding.

Codex runs explicitly enable Code Mode tool access in the private harness configuration. This fixed setting applies to vanilla, treatments, and review calls; host configuration remains excluded. CLI arguments and effective model/effort evidence are retained.
