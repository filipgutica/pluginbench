# PluginBench

**Are these skills, plugins, or instructions worth using?**

PluginBench gives a coding agent the same tasks in several arms: one with no added guidance ("vanilla"), and one for each set of guidance you want to test.
The harness, model, reasoning effort, and tasks stay fixed, so the guidance is the only difference.
It then compares correctness, tokens, and the code each attempt added.

## What you get

- **Arms.** Vanilla is always included. Each *treatment* is a named set of instruction files, skills, or plugins that you declare in the config. Every treatment is compared with vanilla.
- **Attempts.** Each task runs once per arm and repeat, in a fresh Docker container, through Harbor 0.24.0. The task's own verifier decides pass or fail.
- **Measures.** You select them in the config:

| Measurement | What it counts | Extra requirement |
| --- | --- | --- |
| `tokens` | Input, cached, output, and reasoning tokens, and coding time | None |
| `lines` | Source and test lines added and removed | None |
| `cyclomatic`, `cognitive` | Complexity the attempt added | An analyzer for the task's language |
| `dead_code` | Unused code the attempt introduced | An analyzer for the task's language |
| `coverage` | How much of the added code the attempt's own tests run | A coverage command for the task image |
| `review` | Findings from an independent AI review with fixed criteria | A `reviewer` block in the config |

- **Report.** `runs/<run>/report/report.html` compares the arms, with each task one click away. `results.json` holds the metrics and deltas against vanilla. `results.csv` holds each measurement. Failed attempts and missing measurements stay visible.

When a task ships a reference solution (`solution/solution.patch`), code measures are also shown as a ratio to that reference. [What each measure means](docs/measurement-spec.md).

## Requirements

PluginBench runs on Linux or macOS. Every run needs:

| Tool | Version | Why |
| --- | --- | --- |
| Python | 3.12 or later | Runs PluginBench |
| Git | Any recent | Task sources and patches |
| Docker | Linux containers on a local Unix socket, with Compose and Buildx | Each attempt and its verifier run in containers |
| Codex CLI | Exactly 0.162.1 or 0.167.0, logged in | Coding agent. PluginBench checks the `codex` on your `PATH` and copies its login from `~/.codex/auth.json`. Other versions fail `check`. |

Some measures and commands need more:

| Needed for | Tool |
| --- | --- |
| JS/TS complexity and dead code | Node (this repository pins 22.20), Corepack, and Fallow 3.18.0 |
| Python, Go, Rust analysis | Pinned analyzers; see [Measurements](docs/running-evaluations.md#measurements) |
| `coverage` | A coverage command that the task image supports |
| Analysis measures that run on the host, and `review` | Bubblewrap (`bwrap` on `PATH`) on Linux, or `sandbox-exec` on macOS |
| `pluginbench tasks pick` | `fzf`, and for `--copy` a clipboard tool (`pbcopy`, `wl-copy`, `xclip`, or `xsel`) |

## Install

There is no registry release yet. Install from a clone:

```sh
git clone https://github.com/filipgutica/pluginbench.git
cd pluginbench
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pluginbench --version
```

## Quickstart

This run uses one public DeepSWE task, vanilla only, and one repeat. It needs no private input.

1. Save this as `evaluation.yaml`:

   ```yaml
   name: First run
   runner:
     harness: codex
     model: gpt-6.1-sol
     reasoning: medium
   treatments: {}        # vanilla only
   tasks:
     urls:
       - https://github.com/datacurve-ai/deep-swe/tree/0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea/tasks/testem-per-launcher-reports
   measurements: [tokens, lines]
   repeats: 1
   ```

2. Run every check that needs no model:

   ```sh
   pluginbench check evaluation.yaml
   ```

   This downloads and prepares the task, checks Docker capacity, the Codex version, and the verifier, then prints a readiness table.
   It shows whether each vanilla attempt is already cached, writes `runs/check-<timestamp>/check.json`, and exits with code 2 if the run would fail or lose data.
   If it reports that `jobs` exceeds Docker capacity, add `jobs: 1` to the config or give Docker more CPU and memory.

3. See the plan and the call count:

   ```sh
   pluginbench run evaluation.yaml --dry-run
   ```

   The `Coding attempts` line shows one attempt for this config. It counts attempts, not model requests: subagents are on by default, so one attempt can make several requests.

4. Start the run. **This makes paid model calls with your Codex login.**

   ```sh
   pluginbench run evaluation.yaml
   ```

   `run` prints the plan and asks `Start evaluation?` (`--yes` skips the question).
   It then runs the same checks as `check`, starts the run in the background, and prints the run directory, for example `runs/20261110T101500123456Z`.

5. Follow it:

   ```sh
   pluginbench status runs/<run> --watch
   ```

   `status` shows the phase, attempts and outcomes per arm, tokens, vanilla reuse, and measures. It never writes.

6. Open `runs/<run>/report/report.html` when the phase is `complete`.

Paths under `runs/` are relative to the directory where you run the command. For a fuller template, `pluginbench init template.yaml` writes a starter config with `/path/to` placeholders to replace.

## Commands

Every setting lives in the config. Flags only control how a command runs.

| Command | What it does | Model calls |
| --- | --- | --- |
| `init [path]` | Write a starter config; never overwrites | No |
| `check <config>` | Prepare tasks and run every check that needs no model | No |
| `run <config>` | Start a run; `--dry-run` shows the plan, `--foreground` stays in the terminal, `--notify` sends desktop notifications | Yes |
| `run <run-dir>` | Resume a stopped or crashed run | Yes |
| `status <run-dir>` | Show progress; `--watch` redraws, `--json` prints one object | No |
| `stop <run-dir>` | Stop the run's own process group | No |
| `report <run-dir>` | Rebuild the report from saved evidence | No |
| `review <run-dir> --revision <name>` | Review every submission again with the same reviewer | Yes |
| `compare <run-dir>... --gates <file>` | Print measure and gate tables across runs | No |
| `tasks pick --dataset <name>` | Choose tasks with fzf and print a pinned `tasks:` block | No |
| `judge prepare`, `judge run`, `judge tally` | Blind pairwise judging of two arms | `judge run` only |

## Configure an evaluation

**Tasks.** Use one source: a Harbor `dataset` with `version` and `ids`, GitHub task-folder `urls`, or a local `source` with `ids`. PluginBench accepts single-step Harbor tasks with a Linux CPU environment and a separate verifier. To browse DeepSWE, run `pluginbench tasks pick --dataset datacurve/deep-swe-1-1 --copy`, press Tab to select several tasks, and paste the YAML it prints. [Choose inputs](docs/running-evaluations.md#choose-inputs).

**Treatments.** Each treatment lists `guidance` (instruction files), `skills`, or `plugins`. Coding with treatments needs the Codex harness. The task prompt is the same for every arm. Unsupported components, such as MCP servers or custom agents, stop the run before any model call. [Native treatments](docs/running-evaluations.md#native-treatments).

```yaml
treatments:
  my-guidance:
    guidance: [./AGENTS.md]
```

**Measurements.** Before any paid call, PluginBench runs each selected collector twice on the starting code. A measure with no collector for a task is `not_configured`, and one whose collector fails its probe is `broken`. That task is then not measured for it, and the run continues, as long as another task can measure it. A measure that is ready on no task stops the run. With `measurement_settings.require_ready: true`, any such task stops the run. Selecting `review` requires a `reviewer` block. [Measurements](docs/running-evaluations.md#measurements).

**Concurrency.** `jobs` defaults to 2 and accepts 1 to 4. A value above Docker capacity fails the check; PluginBench does not lower it. [Concurrency and resume](docs/running-evaluations.md#concurrency-and-resume).

## Stop, resume, and reuse

- `pluginbench stop runs/<run>` stops new attempts. With `jobs` above 1, attempts already coding finish and are kept. With `jobs: 1`, the attempt in progress is interrupted, and a resume codes its slot again with a new attempt.
- `pluginbench run runs/<run>` resumes. A run that was killed, for example by a host shutdown, shows `interrupted`, and `status` prints this command. Complete attempts are kept and never coded again. An attempt cut off mid-call is recorded as `discarded-interrupted` and a new attempt takes its slot. Resume cannot repair a failing solution.
- If Codex fails before its first model turn, for example during a network outage, the run stops instead of scoring the attempt. Resume when Codex is reachable.
- Vanilla attempts are cached in `$XDG_CACHE_HOME/pluginbench` (default `~/.cache/pluginbench`) and reused by any later run with the same task, model, and settings. Grading and measures run again. Reports mark reused attempts. [Baselines and reports](docs/running-evaluations.md#baselines-and-reports).

## Compare and judge

- `pluginbench compare` reads saved reports and applies gates you define, such as "no fewer passes than vanilla" or "at most 1.3 times the tokens". [Compare runs](docs/running-evaluations.md#compare-runs).
- `pluginbench judge` asks blind judges to choose between two arms' submissions for the same task and repeat. [Judging](docs/running-evaluations.md#judging).

## Limits

- A pass means the task's verifier passed. Task checks do not prove that every behavior is preserved.
- Discovery proves that a skill was available, not that the model used it.
- Smaller numbers alone do not prove better work. Coverage does not prove that assertions are useful.
- Unknown values are never shown as zero.
- Reused vanilla attempts are not fresh independent samples.
- A measure that is `broken` on a task is not measured for that task. `check` warns, and the report shows it as unavailable there. With `measurement_settings.require_ready: true`, the check stops instead. A measure that is ready on no task always stops the run.
- PluginBench executes model-generated code. Use trusted tasks, images, and treatments on a trusted machine. See [Security](SECURITY.md).

## More

- [Evaluation details](docs/running-evaluations.md): every setting, check, and output
- [Config reference](docs/config-reference.md): every config key, its type, default, and meaning
- [Metrics](docs/metrics.md): what each measure means and its name in a gates file
- [Reproduce a result](docs/reproduce.md): what a run pins, and a worked example
- [Measurement specification](docs/measurement-spec.md)
- [Security](SECURITY.md)
- [License: Apache-2.0](LICENSE)
