# PluginBench

**Are these skills, plugins, or instructions worth using?**

Run the same coding tasks with **vanilla**, **package A**, **package B**, and any other guidance you choose. Keep the harness, model, and reasoning fixed. Every package is compared with vanilla.

Select tokens in/out, lines added/removed, complexity, dead code, coverage, and review findings. Code measures count what each attempt added, beside the same measures for the task's reference solution. The report compares packages, with individual tasks one click away. Correctness and missing measurements stay visible. [What each measure means](docs/measurement-spec.md).

## Run

Requires Python 3.12+, Git, Docker with Linux containers, Compose, Buildx, and a logged-in Codex CLI. Local collectors and reviews require macOS `sandbox-exec` or Linux Bubblewrap. JavaScript/TypeScript analysis requires Node, Corepack, and Fallow 3.18.0.

```sh
pip install -e .
pluginbench init                                    # write evaluation.yaml to edit
pluginbench check evaluation.yaml                   # every check that needs no model
pluginbench run evaluation.yaml                     # check, then run in the background
pluginbench status runs/<run> --watch               # follow it
pluginbench stop runs/<run>                         # stop it; `run runs/<run>` resumes it
pluginbench compare runs/<run> --gates gates.yaml   # gate table
pluginbench report runs/<run>                       # rebuild the report
pluginbench review runs/<run> --revision review-2   # review again with the same reviewer
```

`init` writes a starter config; replace its `/path/to` placeholders. `check` makes no model calls. It prepares each task and exits 2 when the run would fail or lose data. `run` runs the same checks in the foreground, then continues in the background and prints the run directory and the `status` command. `run --dry-run` shows the plan without preparing anything. `status` shows the phase, outcomes, tokens, vanilla reuse, and measures; `--json` prints them for dashboards. `stop` interrupts only the process group recorded in the run's `run.lock`. `run runs/<run>` resumes a stopped or crashed run. On Linux with a systemd user manager, a background run gets its own user scope, so it outlives the terminal or agent session that started it. Add `--foreground` to keep a run in the terminal.

Choose **runner**, **treatments**, **tasks**, **measurements**, and optional **reviewer** in the config. Every setting lives there; the command line takes none. Preparation and measurement happen automatically.

To choose tasks interactively, install `fzf` and run:

```sh
pluginbench tasks pick --dataset datacurve/deep-swe-1-1 --copy
```

Search task names, preview the prompt, and press Tab to select multiple tasks. Enter prints a pinned `tasks:` YAML block. `--copy` also copies it; paste the block into your evaluation config. Escape cancels. Configs also accept [GitHub task-folder links](docs/running-evaluations.md#choose-inputs). Downloads are cached automatically.

Harbor runs each attempt and the task's own correctness checks. PluginBench freezes the starting code and submission, then measures both with the same settings. Each attempt gets fresh isolation. Concurrency defaults to two jobs, with at most four. A `jobs` value above Docker capacity fails the check instead of being lowered. Set `jobs: 1` in the config to run one at a time.

Compatible vanilla submissions are reused automatically from `$XDG_CACHE_HOME/pluginbench` (default `~/.cache/pluginbench`), so any output folder can reuse them. `pluginbench check` shows a hit or miss for every vanilla attempt. Reports identify reuse; grading and selected measurements run again.

Open `runs/<run>/report/report.html`. `results.json` contains metrics and baseline deltas; `results.csv` contains individual measurements. `pluginbench report runs/<run>` rebuilds them without model calls.

Resume with `pluginbench run runs/<run>`. Complete attempts are kept and never coded again. Pending attempts run. An attempt whose model call started but did not finish is recorded as `discarded-interrupted`, with any usage it saved, and a new attempt takes its place in the same randomized slot. A Codex session that fails before its first model turn, as in a network outage, stops the run instead of being scored or cached; resume codes it again. Measurement and reviews then continue. Resume cannot repair a failing solution.

Accepts standard [Harbor tasks](docs/running-evaluations.md) with Linux CPU environments and separate verifiers. There is no task-name allowlist. Measurement support is reported separately. Pinned analyzers cover JS/TS, Python, Go, and Rust complexity; unused-code support depends on the analyzer and build environment. Coverage accepts LCOV, coverage.py JSON, and Go profiles, with each unit labeled separately. It is reported for the lines each attempt added. Python per-test coverage also flags redundant added tests. Tasks that ship `solution/solution.patch` are measured as a reference baseline. Preparation probes selected collectors twice before coding. A broken collector stops the run before paid calls. A measure with no collector for a task, such as coverage on an image without a coverage tool, is reported as not configured. `measurement_settings.require_ready: true` stops on those too.

Treatments use native Codex instructions, skill discovery, and plugin installation. The task prompt stays unchanged. Complete package resources and synchronous command hooks are preserved; unsupported components stop admission. Discovery proves availability, not that the model used a skill. Smaller numbers alone do not prove better work.

[Details and limits](docs/running-evaluations.md) · [Security](SECURITY.md)
