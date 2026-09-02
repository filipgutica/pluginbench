# pluginbench

`pluginbench` is an experimental CLI for paired evaluations of local Codex skill bundles.
It measures fixed-task completion and reports token, cost, and duration changes when available.

The POC uses Promptfoo as the execution engine. Promptfoo calls the OpenAI Codex SDK.
The baseline gets no supplied skills. The treatment gets the same tasks plus the supplied skills.

The project currently supports local fixture catalogs and pinned SWE-bench Verified tasks.
It does not measure human developer experience by itself.

Read the completed [Filip-stack capability study](reports/2026-08-31-filip-stack-evaluation.md)
and [blinded review study](reports/2026-09-01-filip-stack-blinded-review-evaluation.md).

## Install

Install Python 3.12 or later. This repository pins Node.js 26.3.0 in `.nvmrc`.

```bash
nvm install
nvm use
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,benchmark]'
npm ci
```

Promptfoo 0.122.2 and `@openai/codex-sdk` 0.151.0 are pinned in `package.json`.
The Codex SDK can use a ChatGPT subscription login or an API key.
The `benchmark` extra pins the official SWE-bench 5.0.2 harness.

Build the pinned model-trial runtime before running SWE-bench:

```bash
docker build --platform linux/arm64 -f runtime/Dockerfile \
  -t pluginbench-runtime:0.2.0 .
```

The example uses `~/.codex/auth.json`. Run `codex login` if this file does not exist.
Treat this file as a secret.

```bash
.venv/bin/pluginbench doctor examples/eval.yaml
.venv/bin/pluginbench run examples/eval.yaml --dry-run
```

The dry run shows the tasks, treatments, Promptfoo commands, trial count, and cost exposure.
It does not call a model.

## Run an evaluation

The checked-in example has one task and one attempt. A paired run starts two trials.

```bash
.venv/bin/pluginbench run examples/eval.yaml --yes
```

## Evaluate a workflow skill bundle

The workflow smoke benchmark uses three tasks. They cover debugging, review feedback, and implementation planning.
The benchmark is not tied to one skill author or repository.

The checked-in configuration points to Filip-stack because it is the first evaluation target.
It expects the `filip-stack` and `pluginbench` repositories to have the same parent directory.

Check the environment and inspect the six-trial plan before the run:

```bash
.venv/bin/pluginbench doctor benchmarks/workflow-skill-smoke/eval.yaml
.venv/bin/pluginbench run benchmarks/workflow-skill-smoke/eval.yaml --dry-run
```

The paired run starts three baseline trials and three treatment trials:

```bash
.venv/bin/pluginbench run benchmarks/workflow-skill-smoke/eval.yaml --yes
```

This is a plumbing and directional-quality check. Three tasks cannot establish a general effectiveness claim.

Use `--skill` to run the same benchmark against another local portable skill directory or bundle:

```bash
.venv/bin/pluginbench run benchmarks/workflow-skill-smoke/eval.yaml \
  --skill /path/to/superpowers/skills \
  --dry-run

.venv/bin/pluginbench run benchmarks/workflow-skill-smoke/eval.yaml \
  --skill /path/to/matt-pocock-skills \
  --dry-run
```

Run Filip-stack first to create the shared baseline. Reuse that baseline for each other skill bundle:

```bash
.venv/bin/pluginbench run benchmarks/workflow-skill-smoke/eval.yaml \
  --baseline runs/<filip-stack-run>/baseline/arm.json \
  --skill /path/to/other/skills \
  --yes
```

The baseline fingerprint excludes the treatment skill digest. It still checks every task, model, harness, timeout, and verifier field.

## Run a SWE-bench Verified task

The checked-in SWE-bench configuration runs one official Verified task.
It compares the baseline with the Filip-stack workflow skills.

The snapshot pins the dataset revision and the task row.
Pluginbench checks out the exact repository commit for each arm.
The official SWE-bench Docker harness grades each generated patch.

Run the checks and inspect the two-trial plan:

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench doctor \
  benchmarks/swe-bench-verified/eval.yaml

PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/swe-bench-verified/eval.yaml --dry-run
```

Start one baseline trial and one treatment trial:

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/swe-bench-verified/eval.yaml --yes
```

The configuration uses `gpt-5.6-luna` with high reasoning.
It uses the subscription login from `~/.codex/auth.json`.

The published task image is amd64-only.
On this ARM Mac, pluginbench pre-pulls it with `--platform linux/amd64`.
The official harness then runs the image with Docker emulation.
Track native ARM64 support in [SWE-bench issue 520](https://github.com/SWE-bench/SWE-bench/issues/520).

One task proves the benchmark path, but it cannot estimate general skill lift.
Use more fixed task IDs and repeated attempts for a decision-quality result.

### Start from an existing patch

Use a starting patch to test whether a workflow repairs a known failing implementation or
preserves a known passing implementation. Add the patch to the selected SWE-bench task:

```yaml
dataset:
  adapter: swe-bench
  # Other pinned SWE-bench fields...
  task_ids: [sympy__sympy-20590]
  starting_patches:
    sympy__sympy-20590:
      path: patches/sympy__sympy-20590.patch
      expected_score: 0
```

Set `expected_score` to `0` for a patch already confirmed to fail the official grader.
Set it to `1` for a confirmed passing control. Pluginbench snapshots and checksums the
patch, applies it identically to both arms, and tells each agent to review, preserve, fix,
and verify the proposed implementation. Starting patches cannot change `.agents/`.

The official grader still receives the complete patch relative to the SWE-bench base
commit. Each verifier directory also contains `agent-change.diff`, which isolates work
performed after the starting patch. Reports classify attempts as repaired, unchanged
failures, preserved, or regressed and compare repair and preservation rates between arms.

## Run the harder SWE-bench sample

The hard sample pins eight official SWE-bench Verified tasks across eight repositories.
It includes all three tasks labeled `>4 hours` in the pinned dataset revision and five
tasks labeled `1-4 hours`. Regenerate the checked-in snapshot from the official dataset:

```bash
.venv/bin/python benchmarks/swe-bench-verified-hard/generate_snapshot.py
```

Validate the environment and inspect the bounded run before spending any subscription
usage. The dry run must show 16 trials and a $2.40 nominal exposure estimate:

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench doctor \
  benchmarks/swe-bench-verified-hard/eval.yaml

PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/swe-bench-verified-hard/eval.yaml --dry-run
```

Start the paired run only after reviewing that output:

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/swe-bench-verified-hard/eval.yaml --yes
```

The configuration runs one attempt per arm with concurrency and batch size set to one.
It checks the best-effort $5 limit between trials. The estimate is conservative, but an
active trial can exceed the limit before pluginbench regains control.

The completed `20260831T215323Z-filip-stack-swe-bench-verified-hard` run contains one
baseline response that arrived after the configured timeout. Its canonical report marks
that task unscoreable and compares the remaining seven pairs. `strict-report.md` also
removes both arms of that pair from efficiency metrics; `raw-report.md` preserves the
original pre-timeout-guard output for provenance.

## Run the Filip-stack core ablation

The core ablation supplies only `implementation`, `minimal-code`, and
`systematic-debugging`. It reuses the compatible hard-sample baseline, so the dry run
must show eight treatment trials and a $1.20 nominal exposure estimate:

The checked-in configuration points to the archived local baseline used for the study.
That large run directory is intentionally not published. Run the hard sample first and
update `execution.baseline.result`, or pass `--baseline` with your compatible `arm.json`.

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench doctor \
  benchmarks/filip-stack-core-ablation/eval.yaml

PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/filip-stack-core-ablation/eval.yaml --dry-run

PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/filip-stack-core-ablation/eval.yaml --yes
```

Generate a three-way comparison from the saved baseline, full-bundle, and core arms:

```bash
.venv/bin/python benchmarks/filip-stack-core-ablation/generate_report.py \
  --baseline-arm runs/<full-run>/baseline/arm.json \
  --full-arm runs/<full-run>/treatment/arm.json \
  --core-arm runs/<core-run>/treatment/arm.json \
  --output runs/<core-run>
```

## Run a blinded review evaluation over the saved SWE-bench patches

The review mode reuses the saved baseline, full-workflow, and core-workflow patches. It
does not rerun implementation agents or the SWE-bench grader. Fresh Codex conversations
review opaque candidate workspaces in a read-only sandbox with no supplied workflow
skills, network access, traces, arm labels, or grader results.

`benchmarks/filip-stack-review/eval.yaml` points to the archived local arm, patch, oracle,
and repository artifacts used for the published study. Raw run directories are excluded
from Git because they contain large workspaces and host-local traces. To reproduce the
review, restore those artifacts at the configured paths or update the source paths to
compatible saved runs.

Each reviewer starts at a clean outer Git root. The candidate repository is nested under
`repository/` as untrusted review input. This keeps candidate `AGENTS.md` files outside
Codex's startup instruction chain while preserving the candidate diff exactly.

Inspect the full seven-task study before starting it:

```bash
.venv/bin/pluginbench review run \
  benchmarks/filip-stack-review/eval.yaml \
  --dry-run
```

The dry run resolves seven mutually scoreable tasks, 21 workflow candidates, and seven
optional gold-patch candidates. With two independent attempts per candidate, it reports
56 review calls, zero implementation-agent calls, zero grading calls, and the configured
token and cost exposure. Dry-run and automated tests do not call a model.

Run the three-task pilot first. It covers Django, pytest, and SymPy plus their gold
patches: 12 candidates and 24 review calls.

```bash
.venv/bin/pluginbench review run \
  benchmarks/filip-stack-review/eval.yaml \
  --task-id django__django-16560 \
  --task-id pytest-dev__pytest-5787 \
  --task-id sympy__sympy-13878 \
  --dry-run

.venv/bin/pluginbench review run \
  benchmarks/filip-stack-review/eval.yaml \
  --task-id django__django-16560 \
  --task-id pytest-dev__pytest-5787 \
  --task-id sympy__sympy-13878 \
  --yes
```

Proceed to all seven tasks only after checking structured-output validity,
infrastructure failures, gold-patch false positives, actionable evidence, and the
accepted dry-run cost:

```bash
.venv/bin/pluginbench review run \
  benchmarks/filip-stack-review/eval.yaml \
  --yes
```

Regenerate both reports without model calls:

```bash
.venv/bin/pluginbench review report runs/reviews/<review-run>
```

If a completed arm study omitted gold, append only the missing calibration reviews:

```bash
.venv/bin/pluginbench review calibrate \
  runs/reviews/<review-run> \
  benchmarks/filip-stack-review/eval.yaml \
  --dry-run

.venv/bin/pluginbench review calibrate \
  runs/reviews/<review-run> \
  benchmarks/filip-stack-review/eval.yaml \
  --yes
```

The command checks the source fingerprint, dataset, task checksums, reviewer, and tooling.
It rejects mismatches and existing calibration artifacts. It never reruns implementation agents or graders.

The command writes `gold-calibration/` under the completed run. It then regenerates the source JSON and Markdown reports.

The report keeps capability, reviewer-proxy findings, resource use, gold calibration,
and infrastructure failures separate. It does not calculate a composite quality score.
Missing attempts and usage remain explicit.

The completed calibration found that the reviewer objected to gold patches more often
than every experimental arm. Do not treat raw reviewer counts as code-quality evidence
without adjudication. See the [study report](reports/2026-09-01-filip-stack-blinded-review-evaluation.md#73-gold-calibration-results).

This study can estimate whether the saved patches attract more action-required findings,
more change requests, or more findings about over-engineering and unnecessary scope. It
can show whether those outcomes accompany the workflow's existing time and token overhead.
It cannot measure actual human correction time, long-term maintainability, follow-on task
performance, general capability effects from seven single attempts, or whether
Filip-stack's internal reviewer mechanism is effective. An automated reviewer is a proxy,
not a human reviewer.

Use these commands for the other baseline modes:

```bash
pluginbench run eval.yaml
pluginbench run eval.yaml --baseline runs/previous/baseline/arm.json
pluginbench run eval.yaml --skip-baseline
pluginbench compare baseline/arm.json treatment/arm.json
pluginbench report runs/20260831T120000Z-example
```

Baseline reuse checks a content fingerprint before Promptfoo starts the treatment.
The fingerprint includes task content, verifier commands, model settings, attempts, batch size, and tool versions.
It excludes the supplied skill because the skill is the treatment variable.

A treatment-only report shows absolute results. It does not show skill lift.

## Isolation

Each attempt gets a new copy of its task fixture.
Treatment skills exist only under that copy's `.agents/skills` directory.

Each attempt also gets temporary `HOME` and `CODEX_HOME` directories.
`pluginbench` copies only the configured `auth.json` into the temporary Codex home.
It removes these temporary directories after the batch.

For SWE-bench, Promptfoo and Codex execute inside the configured runtime container.
Pluginbench bind-mounts only the current batch config, its exact trial repositories,
and its temporary auth directories. Benchmark metadata, verifier artifacts, the run root,
and the sibling experimental arm are not available to the model container. The resolved
container image ID is included in the baseline compatibility fingerprint.

The outer container grants `SYS_ADMIN` and disables its AppArmor and seccomp profiles so
Codex can start its own bubblewrap sandbox. That inner sandbox enforces the configured
workspace-write and network restrictions. The outer container still receives only the
explicit bind mounts listed above.

This setup excludes personal Codex configuration, memory, plugins, and user skills.
Codex still supplies its built-in system instructions and system skills to both arms.

Read [SECURITY.md](SECURITY.md) before you run untrusted inputs. The SWE-bench runtime
uses a privileged outer Docker container and is not safe for hosted multi-user execution.

Promptfoo infers skill use from successful `SKILL.md` reads.
The Codex SDK does not provide a first-class skill invocation event.
See the [Promptfoo Codex SDK provider documentation](https://www.promptfoo.dev/docs/providers/openai-codex-sdk/)
and the [official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills).

## Local task catalog

The POC runs local fixture catalogs. Each task has a prompt, a fixture directory, and an external verifier script.

```yaml
schema_version: 1
tasks:
  - id: summarize-gateway-log
    prompt: Analyze events.log and write summary.json.
    fixture: fixtures/gateway-log
    verifier:
      command: [python3]
      script: verifiers/gateway_log.py
      timeout_seconds: 30
```

The verifier script must be outside the writable fixture directory.
Pluginbench includes the verifier content in the task fingerprint.
The verifier runs without a shell and receives a small environment.
Exit code zero passes the attempt. A nonzero exit code is a task failure.

## Existing benchmark sets

The architecture can use existing sets, but each set needs its native workspace and verifier adapter.

| Set | Fit for workflow skills | Current status |
| --- | --- | --- |
| Local fixture catalog | High | Runnable now |
| [SWE-bench](https://github.com/SWE-bench/SWE-bench) | High | Verified adapter runnable with pinned JSON snapshots and the official Docker harness |
| [LiveSWEBench](https://github.com/LiveBench/liveswebench) | High | Adapter reserved, not runnable |
| [LiveBench](https://github.com/LiveBench/LiveBench) question sets | Low to medium | Not planned for the first adapter |
| [LiveCodeBench](https://github.com/LiveCodeBench/LiveCodeBench) | Low for repository workflows | Deferred |

Promptfoo can load Hugging Face rows directly. That feature does not prepare a repository or grade a patch.

SWE-bench needs a repository at the task's base commit and its official Docker evaluator.
The official harness can require large Linux images and substantial local storage.

LiveSWEBench is the relevant LiveBench agentic set. Its harness prepares repositories and tests patches.
It is a better workflow-skill signal than the general LiveBench question sets.

The SWE-bench adapter keeps Promptfoo for Codex execution.
It uses the official SWE-bench CLI for grading.
The LiveSWEBench adapter remains deferred because its current harness needs manual log inspection.

## Artifacts

```text
<run>/
├── manifest.json
├── resolved-config.yaml
├── report.json
├── report.md
├── inputs/skill-bundle/...
├── inputs/task-catalog.yaml or swebench-dataset.json
├── inputs/tasks/<task>/{task.json,verifier.py,fixture/... or repository/...}
├── baseline/
│   ├── arm.json
│   ├── workspaces/<task>/attempt-01/...
│   ├── verifiers/<task>/attempt-01/{patch.diff,predictions.jsonl,logs/...}
│   └── batches/0001/{promptfooconfig.yaml,promptfoo-results.json,command.json,*.log}
└── treatment/
    ├── arm.json
    ├── workspaces/<task>/attempt-01/.agents/skills/...
    └── batches/0001/{promptfooconfig.yaml,promptfoo-results.json,command.json,*.log}
```

Reports include pass rates, paired wins, score lift, token overhead, cost overhead, and duration.
The run snapshots each selected fixture and verifier so its task digest remains inspectable.
Cost values are Promptfoo estimates. They are not billing statements.
Missing usage stays `null` in JSON and `Unavailable` in Markdown.

A review run uses a separate artifact contract:

```text
<review-run>/
├── manifest.json
├── resolved-config.yaml
├── private/candidate-map.json
├── candidates/<opaque-id>/{issue.md,candidate.patch,review-prompt.md,repository/...}
│   └── repository/.review-packet/{issue.md,candidate.patch,instructions.md}
├── reviews/<opaque-id>/attempt-01/
│   ├── promptfooconfig.yaml
│   ├── promptfoo-results.json
│   ├── result.json
│   ├── command.json
│   ├── stdout.log
│   └── stderr.log
├── gold-calibration/                  # Present after an appended calibration
│   ├── manifest.json
│   ├── private/candidate-map.json
│   ├── candidates/<opaque-id>/...
│   └── reviews/<opaque-id>/attempt-01/...
├── review-report.json
└── review-report.md
```

Only the private candidate map connects opaque IDs to experimental arms. It is outside
every reviewer-mounted workspace. The report unblinds results only after review.

Promptfoo does not stop an active eval at an aggregate token or cost limit.
`pluginbench` checks configured limits between bounded batches.
One active batch can exceed a limit.

## Verify

The automated checks do not call a model:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tests
.venv/bin/python -m build
node_modules/.bin/promptfoo validate -c tests/fixtures/promptfooconfig.yaml
```

Use the fixture test for the zero-cost orchestration check:

```bash
.venv/bin/pytest -q tests/test_run.py tests/test_promptfoo.py
```

The review-evaluation contract has a focused zero-cost suite and a real saved-artifact
dry run. Neither command makes a model call:

```bash
.venv/bin/pytest -q \
  tests/test_review_models.py \
  tests/test_review.py \
  tests/test_review_reporting.py \
  tests/test_cli.py \
  tests/test_promptfoo.py

.venv/bin/pluginbench review run \
  benchmarks/filip-stack-review/eval.yaml \
  --dry-run
```

For the paid check, run the one-task example. Confirm that the CLI shows two trials before it starts.

## POC boundary

The POC supports local portable `SKILL.md` directories only.
It does not install native plugins, hooks, MCP servers, memory, or remote skill repositories.
It does not force skill invocation.
It isolates files and Codex state, but it does not set CPU or memory limits on local processes.
