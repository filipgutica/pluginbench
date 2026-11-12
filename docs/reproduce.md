# Reproduce an evaluation

Reproduction uses the same pinned inputs and checks results against stated bounds.
Model sampling varies, so identical inputs do not guarantee identical numbers.
The seed controls attempt ordering and identities; it does not fix provider sampling.
Declare the metrics, tolerances, task set, repeats, and overlap-pass policy before comparing results.
One task and one repeat demonstrate the workflow; they do not establish statistical reproducibility.

No evaluation result is published yet.
The committed panel evidence records preparation checks, not completed model evaluations.

## Inputs and receipts

Paths below are relative to the run directory.
Keep the whole directory, including frozen inputs, evidence, receipts, and reports.

| Input | Where to find it |
| --- | --- |
| PluginBench build | Package version is not saved automatically; record `pluginbench --version` as shown below and retain the source commit or installed distribution. Component versions and source hashes appear in `evaluation.json` under `comparison_identity.isolation`, and in `inputs/vanilla-identity.json`. These identify execution components, not the complete package. |
| Harbor version | `evaluation.json`: `comparison_identity.isolation.harbor_version`; coding preflight evidence also records the runtime in `evidence/runtime-preflight.json`. New coding runs require Harbor 0.24.0. |
| Coding CLI version | `inputs/study.json`: `harness_version`; `evaluation.json`: `comparison_identity.harness_version`. Per-attempt execution receipts preserve observed CLI evidence when available. |
| Model and reasoning | `evaluation.json`: `runner`; `inputs/study.json` and `evidence/frozen/resolved-recipe.json`: `model` and `reasoning`. Requested settings are distinct from effective settings when native traces cannot verify them. |
| Dataset and task pin | `evaluation.json`: `tasks` contains resolved Git commit URLs or dataset version; `comparison_identity.task_sources` retains resolved source references for remote tasks, and `comparison_identity.tasks` binds task assets. Local tasks have asset hashes rather than an automatic Git commit pin. `inputs/tasks.json` and `inputs/<task>/task/` retain the task inventory and assets. |
| Seed and repeats | `evaluation.json`, `inputs/study.json`, and `evidence/frozen/resolved-recipe.json`: `seed` and `repeats`; `evidence/plan.json` records randomized attempt slots. |
| Container limits and timeouts | `evaluation.json`: `container_cpus`, `container_memory_mb`, `timeout_seconds`, `jobs`, and `comparison_identity.task_limits` for declared and effective budgets. |
| Prepared image IDs | `inputs/<task>/artifacts/native-preparation.json`: `image_identity.image_id` and `grader_image_identity.image_id`, alongside the preparation key and toolchain identity. The portable task identities also appear in `evaluation.json` under `comparison_identity.task_environment`. |

Each coding attempt keeps its execution evidence under `evidence/attempts/<attempt>/handoff/`,
including `execution.json`, `artifacts/`, and snapshots when available.
`evidence/run.json` binds evidence files; `evaluation-binding.json` binds the evaluation settings.
`report/results.json` supplies the saved observations read by `compare`.
Public JSON, CSV, and HTML reports call the no-guidance arm `vanilla`.
JSON schema version 2 also renames arm-specific fields; see the [report format](metrics.md#report-files) for the complete inventory.
`compare`, `status`, and `judge prepare` accept version 1 reports that use `baseline` and version 2 reports that use `vanilla`.
Evidence, `inputs/study.json`, attempt IDs, and cache identities retain their stored names, so existing runs need no migration.
Vanilla cache hits reuse verified attempts, so a new directory does not necessarily make fresh coding calls.
See [running evaluations](running-evaluations.md) for cache identities and runtime drift checks.

## One public task, vanilla only

Install Python 3.12 or newer, PluginBench, a [coding CLI](running-evaluations.md#verified-coding-clis),
and its provider credentials. Start Docker with Compose and Buildx available.
See [execution and isolation](running-evaluations.md#execution-and-isolation) for platform and sandbox requirements.
From a source checkout, create and activate a virtual environment, then install PluginBench:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Save this as `reproduce.yaml`:

```yaml
name: testem-vanilla
runner:
  harness: codex
  model: gpt-6.1-sol
  reasoning: medium
treatments: {}
tasks:
  urls:
    - https://github.com/datacurve-ai/deep-swe/tree/0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea/tasks/testem-per-launcher-reports
measurements: [tokens, lines]
repeats: 1
seed: 42
jobs: 1
```

Check preparation and inspect the planned call count:

```sh
pluginbench check reproduce.yaml --output runs/testem-check
pluginbench run reproduce.yaml --dry-run
```

Both commands need Docker. Even `--dry-run` reads `docker info` to check capacity and record runtime identity.
It can also download the pinned task. `check` prepares images and runs oracle and nop verifiers without model calls.
The plan has one coding attempt and zero reviews. Native child calls are additional; cache reuse can avoid the root attempt.

Start the evaluation after inspecting the plan:

```sh
pluginbench run reproduce.yaml --output runs/testem-vanilla --foreground
pluginbench --version > runs/testem-vanilla/pluginbench-version.txt
git rev-parse HEAD > runs/testem-vanilla/pluginbench-commit.txt
pluginbench report runs/testem-vanilla
```

`run` asks for confirmation and can make paid model calls.
The commit command records the PluginBench checkout used for the editable installation; run it from that checkout.
The version and commit text files are manual provenance notes, not hash-bound run evidence.
`report` rebuilds `report/results.json`, CSV files, and `report/report.html` from saved evidence without model calls.

Save this as `gates.yaml` to inspect vanilla correctness counts without asserting a treatment improvement:

```yaml
count_overlap_passes: false
gates: []
```

```sh
pluginbench compare runs/testem-vanilla --gates gates.yaml
```

An empty gates list prints attempt and correctness counts without gate verdicts.
For a treatment comparison, declare actual gates and run the same pinned tasks across arms.
See the [config reference](config-reference.md) and [metrics glossary](metrics.md).
Paired confidence bounds need at least six usable task pairs, so this example cannot produce them.

## Verify a published bundle

Bundle export and verification are planned and are not yet available.
There is no export or verify command to run today.
