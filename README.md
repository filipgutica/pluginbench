# PluginBench

**Are these text skills or instructions worth using?**

Run the same coding tasks with **vanilla**, **package A**, **package B**, and any other guidance you choose. Keep the harness, model, and reasoning fixed. Every package is compared with vanilla.

The report shows tokens in/out, lines added/removed, complexity, dead code, coverage, and review findings by package. Drill into tasks for repeated runs. Correctness and missing measurements stay visible. Test overlap is optional.

## Run

Requires Python 3.12+, Git, Docker with Linux containers, Compose, and Buildx, Node 22.20.0, Corepack, and a logged-in Codex or Claude CLI. Grading and measurement require macOS `sandbox-exec` or Linux Bubblewrap. Complexity/dead-code measurements require Fallow 3.18.0.

```sh
pip install -e '.[harbor]'
git clone https://github.com/datacurve-ai/deep-swe /tmp/deep-swe
git -C /tmp/deep-swe checkout 0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea
cp examples/evaluation.yaml evaluation.yaml
# Edit evaluation.yaml: replace the guidance and review paths.
pluginbench run --config evaluation.yaml --dry-run
pluginbench run --config evaluation.yaml
```

Choose **runner**, **guidance packages**, **tasks**, **measurements**, and optional **reviewer** in [the config](examples/evaluation.yaml), or use CLI flags (`pluginbench run --help`). Preparation and measurement happen automatically.

PluginBench prepares a frozen agent image; Harbor runs each coding attempt in a fresh container. PluginBench freezes its submission before separate grading, measurement, and review. Concurrency defaults to at most four jobs and is limited by Docker capacity. Use `--jobs 2` or `jobs: 2` to request fewer.

Compatible vanilla submissions are reused automatically from `.baseline-cache` beside your run directories. Reports identify reused runs; grading and selected measurements run again. Test-overlap analysis runs only when selected.

Open `runs/<run>/report/report.html`. `results.json` contains metrics and baseline deltas; `results.csv` contains individual measurements. `pluginbench report runs/<run>` rebuilds them without model calls.

Resume interrupted measurement work with `pluginbench run --resume runs/<run>`. It reuses saved submissions. Interrupted coding or review phases require a fresh evaluation; resume cannot repair a failing solution.

Supports five [DeepSWE/Harbor tasks](docs/running-evaluations.md). Guidance is text supplied directly; plugin hooks, tools, and automatic skill discovery are outside this evaluation. Smaller numbers alone do not prove better work.

[Details and limits](docs/running-evaluations.md) · [Security](SECURITY.md)
