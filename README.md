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
| Codex CLI | Logged in; verified trace versions: 0.162.1, 0.167.0, 0.167.1 | Coding agent for `runner.harness: codex`. PluginBench copies its login from `~/.codex/auth.json` to the pinned container CLI. Unverified versions warn and continue; incompatible traces or unverified inherited history leave tokens unavailable. |
| Claude Code CLI | Only for `runner.harness: claude`; qualified version: 2.1.322 | Coding agent. Set `CLAUDE_CODE_OAUTH_TOKEN` to your subscription token, such as one from `claude setup-token`; API keys are refused for coding, reviews, and judges. Other versions warn and continue. The host CLI is needed only when `runner.cli_version` is omitted. |

Set `runner.cli_version: "0.167.0"` (Codex) or `"2.1.322"` (Claude Code) to select the exact coding CLI version installed in task containers.
When omitted, preparation pins the host CLI's version. Resume keeps that pin across host CLI updates.
Independent reviews use the host CLI. See [verified coding CLIs](docs/running-evaluations.md#verified-coding-clis) for credential compatibility, and [Claude Code coding](docs/running-evaluations.md#claude-code-coding) for the Claude launch and token rules.

Some measures and commands need more:

| Needed for | Tool |
| --- | --- |
| JS/TS complexity and dead code | Node (this repository pins 22.20), Corepack, and Fallow 3.18.0 |
| Python, Go, Rust analysis | Pinned analyzers; see [Measurements](docs/running-evaluations.md#measurements) |
| `coverage` | A coverage command that the task image supports |
| Analysis measures that run on the host, and `review` | Bubblewrap (`bwrap` on `PATH`) on Linux, or `sandbox-exec` on macOS |
| `pluginbench tasks pick` | `fzf`, and for `--copy` a clipboard tool (`pbcopy`, `wl-copy`, `xclip`, or `xsel`) |

## Install

```sh
pipx install pluginbench
pluginbench --version
```

`uv tool install pluginbench` works too. Either one also installs Harbor 0.24.0. If your default Python is older than 3.12, add `--python python3.12`.

To change PluginBench itself, install from a clone instead:

```sh
git clone https://github.com/filipgutica/pluginbench.git
cd pluginbench
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
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

   `run` prints the plan and asks `Start evaluation?` (`--yes` skips the question; in a script with no answer to give, pass `--yes`).
   It then runs the same checks as `check`, starts the run in the background, and prints the run directory, for example `runs/20261110T101500123456Z`.

5. Follow it:

   ```sh
   pluginbench status runs/<run> --watch
   ```

   `status` shows the phase, attempts and outcomes per arm, tokens, vanilla reuse, and measures. It never writes.

6. Open `runs/<run>/report/report.html` when the phase is `complete`.

Paths under `runs/` are relative to the directory where you run the command. For a fuller template, `pluginbench init template.yaml` writes a starter config with `/path/to` placeholders to replace.

## Test your own guidance

A *treatment* is the guidance you want to test. It can be instruction files, skills, plugins, or a mix. Every treatment runs on the same tasks as vanilla, so you can see what each one changes.

1. Put the guidance next to your config. Paths in the config are relative to the config file.

   ```text
   my-eval/
   ├── evaluation.yaml
   ├── AGENTS.md                     # instructions: UTF-8 .md or .txt files, 32 KiB in all
   ├── skills/
   │   └── write-tests/
   │       └── SKILL.md              # a skill
   └── review-plugin/
       ├── .codex-plugin/
       │   └── plugin.json           # a Codex plugin
       └── skills/
           └── self-review/
               └── SKILL.md
   ```

   Each `SKILL.md` starts with YAML front matter that has a kebab-case `name` and a `description`:

   ```markdown
   ---
   name: write-tests
   description: Add focused tests for every behaviour you change.
   ---

   Write one test per changed behaviour. Run it and confirm it fails without your change.
   ```

   A plugin needs a manifest with a kebab-case `name`. Its skills go in `skills/`:

   ```json
   {
     "name": "review-plugin",
     "version": "0.1.0",
     "description": "Review your own diff before you finish."
   }
   ```

2. Declare one treatment per thing you want to compare:

   ```yaml
   name: Guidance comparison
   runner:
     harness: codex
     model: gpt-6.1-sol
     reasoning: medium
   treatments:
     instructions:
       guidance: [./AGENTS.md]
     test-skill:
       skills: [./skills]            # one skill folder, or a folder of skills
     review-plugin:
       plugins: [./review-plugin]
   tasks:
     urls:
       - https://github.com/datacurve-ai/deep-swe/tree/0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea/tasks/testem-per-launcher-reports
   measurements: [tokens, lines]
   repeats: 1
   ```

   One treatment can also combine them, for example `guidance` and `skills` together.

3. Check it, then look at the plan:

   ```sh
   pluginbench check evaluation.yaml
   pluginbench run evaluation.yaml --dry-run
   ```

   The plan lists the arms `vanilla, instructions, test-skill, review-plugin` and 4 coding attempts: one per arm, task, and repeat. Add tasks and `repeats: 3` or more before you trust a difference.

4. Run it with `pluginbench run evaluation.yaml`, as in the quickstart. The report compares each treatment with vanilla.

Treatments work with both harnesses. Instruction files become the agent's private `AGENTS.md` (Codex) or `CLAUDE.md` (Claude Code); they are never added to the task prompt or to the repository. Skills and plugins are copied whole, including their scripts, except Git internals; symlinks, special files, and `.env` files are refused. Skill dependencies, MCP servers, apps, custom agents, commands, plugin dependencies, and hooks other than synchronous command hooks are not supported yet, and a treatment that declares one stops the run before any model call. Claude Code refuses more plugin parts, such as `agents/` and `commands/`; see [Claude Code treatments](docs/running-evaluations.md#claude-code-treatments). Before coding, PluginBench checks that the coding CLI found every declared skill. That proves the skill was available, not that the agent used it. [Native treatments](docs/running-evaluations.md#native-treatments).

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

**Treatments.** Each treatment lists `guidance` (instruction files), `skills`, or `plugins`; see [Test your own guidance](#test-your-own-guidance). The task prompt is the same for every arm.

**Measurements.** Before any paid call, PluginBench runs each selected collector twice on the starting code. A measure with no collector for a task is `not_configured`, and one whose collector fails its probe is `broken`. That task is then not measured for it, and the run continues, as long as another task can measure it. A measure that is ready on no task stops the run. With `measurement_settings.require_ready: true`, any such task stops the run. Selecting `review` requires a `reviewer` block. [Measurements](docs/running-evaluations.md#measurements).

**Concurrency.** `jobs` defaults to 2 and accepts 1 to 4. A value above Docker capacity fails the check; PluginBench does not lower it. [Concurrency and resume](docs/running-evaluations.md#concurrency-and-resume).

## Stop, resume, and reuse

- `pluginbench stop runs/<run>` stops new attempts. Attempts already coding finish and are kept, including with `jobs: 1`. Resume never codes complete attempts again.
- `pluginbench run runs/<run>` resumes. A run that was killed, for example by a host shutdown, shows `interrupted`, and `status` prints this command. Complete attempts are kept and never coded again. An attempt cut off mid-call is recorded as `discarded-interrupted` and a new attempt takes its slot. Containers the killed attempt left running are stopped before coding resumes, and `status` names them until then. Resume cannot repair a failing solution.
- If Codex fails before its first model turn, for example during a network outage, the run stops instead of scoring the attempt. Resume when Codex is reachable.
- Vanilla attempts are cached in `$XDG_CACHE_HOME/pluginbench` (default `~/.cache/pluginbench`) and reused by any later run with the same task, model, and settings. Grading and measures run again. Reports mark reused attempts. Claude Code attempts are cached under their own key, for Claude Code 2.1.322 only. [Baselines and reports](docs/running-evaluations.md#baselines-and-reports).

## Compare and judge

- `pluginbench compare` reads saved reports and applies gates you define, such as "no fewer passes than vanilla" or "at most 1.3 times the tokens". [Compare runs](docs/running-evaluations.md#compare-runs).
- `pluginbench judge` asks blind judges to choose between two arms' submissions for the same task and repeat. [Judging](docs/running-evaluations.md#judging).

## Limits

- A pass means the task's verifier passed. Task checks do not prove that every behavior is preserved.
- Discovery proves that a skill was available, not that the model used it.
- Smaller numbers alone do not prove better work. Coverage does not prove that assertions are useful.
- Unknown values are never shown as zero.
- Reused vanilla attempts are not fresh independent samples.
- Claude Code token rules are calibrated on CLI 2.1.322 only. Other versions get a weaker check, and a timed-out Claude attempt has no token lower bound. See [Claude Code coding](docs/running-evaluations.md#claude-code-coding).
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
