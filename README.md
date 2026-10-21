# PluginBench

**Are these skills, plugins, or instructions worth using?**

Run the same coding tasks with **vanilla**, **package A**, **package B**, and any other guidance you choose. Keep the harness, model, and reasoning fixed. Every package is compared with vanilla.

Select tokens in/out, lines added/removed, complexity, dead code, coverage, and review findings. Code measures count what each attempt added, beside the same measures for the task's reference solution. The report compares packages, with individual tasks one click away. Correctness and missing measurements stay visible. [What each measure means](docs/measurement-spec.md).

## Run

Requires Python 3.12+, Git, Docker with Linux containers, Compose, Buildx, and a logged-in Codex CLI. Local collectors and reviews require macOS `sandbox-exec` or Linux Bubblewrap. JavaScript/TypeScript analysis requires Node, Corepack, and Fallow 3.18.0.

```sh
pip install -e '.[harbor]'
cp examples/evaluation.yaml evaluation.yaml
# Edit evaluation.yaml: replace the treatment and review paths.
pluginbench run --config evaluation.yaml --dry-run
pluginbench run --config evaluation.yaml
```

Choose **runner**, **treatments**, **tasks**, **measurements**, and optional **reviewer** in [the config](examples/evaluation.yaml), or use CLI flags (`pluginbench run --help`). Preparation and measurement happen automatically.

To choose tasks interactively, install `fzf` and run:

```sh
pluginbench tasks pick --dataset datacurve/deep-swe-1-1 --copy
```

Search task names, preview the prompt, and press Tab to select multiple tasks. Enter prints a pinned `tasks:` YAML block. `--copy` also copies it; paste the block into your evaluation config. Escape cancels. Configs also accept [GitHub task-folder links](docs/running-evaluations.md#choose-inputs). Downloads are cached automatically.

Harbor runs each attempt and the task's own correctness checks. PluginBench freezes the starting code and submission, then measures both with the same settings. Each attempt gets fresh isolation. Concurrency defaults to at most four jobs, limited by Docker capacity. Use `--jobs 2` to request fewer.

Compatible vanilla submissions are reused automatically from `.baseline-cache` beside your run directories. Reports identify reuse; grading and selected measurements run again.

Open `runs/<run>/report/report.html`. `results.json` contains metrics and baseline deltas; `results.csv` contains individual measurements. `pluginbench report runs/<run>` rebuilds them without model calls.

Resume interrupted measurement work with `pluginbench run --resume runs/<run>`. It reuses saved submissions. Interrupted coding or review phases require a fresh evaluation; resume cannot repair a failing solution.

Accepts standard [Harbor tasks](docs/running-evaluations.md) with Linux CPU environments and separate verifiers. There is no task-name allowlist. Measurement support is reported separately. Pinned analyzers cover JS/TS, Python, Go, and Rust complexity; unused-code support depends on the analyzer and build environment. Coverage accepts LCOV, coverage.py JSON, and Go profiles, with each unit labeled separately. It is reported for the lines each attempt added. Python per-test coverage also flags redundant added tests. Tasks that ship `solution/solution.patch` are measured as a reference baseline. Preparation probes selected collectors twice before coding; `measurement_settings.require_ready: true` stops before paid calls when measurements are unavailable.

Treatments use native Codex instructions, skill discovery, and plugin installation. The task prompt stays unchanged. Complete package resources and synchronous command hooks are preserved; unsupported components stop admission. Discovery proves availability, not that the model used a skill. Smaller numbers alone do not prove better work.

[Details and limits](docs/running-evaluations.md) · [Security](SECURITY.md)
