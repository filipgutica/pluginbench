# PluginBench

**Are these text skills or instructions worth using?**

Run the same coding tasks with **vanilla**, **package A**, **package B**, and any other guidance you choose. Keep the harness, model, and reasoning fixed. Every package is compared with vanilla.

The report opens with metrics by package: tokens in/out, lines added/removed, complexity, dead code, coverage, test overlap, and review findings. Drill into individual tasks to see repeated runs. Correctness and missing measurements stay visible.

## Run

Requires Python 3.12+, Git, Node 22.20.0, Corepack, and a logged-in Codex or Claude CLI. Complexity/dead-code measurements also require Fallow 3.18.0.

```sh
pip install -e .
git clone https://github.com/datacurve-ai/deep-swe /tmp/deep-swe
git -C /tmp/deep-swe checkout 0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea
cp examples/evaluation.yaml evaluation.yaml
# Edit evaluation.yaml: replace the guidance and review paths.
pluginbench run --config evaluation.yaml --dry-run
pluginbench run --config evaluation.yaml
```

Choose **runner**, **guidance packages**, **tasks**, **measurements**, and optional **reviewer** in [the config](examples/evaluation.yaml), or use CLI flags (`pluginbench run --help`). Preparation and measurement happen automatically.

Independent jobs run concurrently by default, up to four at once. Set `--jobs 2` or `jobs: 2` in your config to change the limit.

Open `runs/<run>/report/report.html`. `results.json` contains metrics and baseline deltas; `results.csv` contains individual measurements. `pluginbench report runs/<run>` rebuilds them without model calls.

Resume interrupted measurement work with `pluginbench run --resume runs/<run>`. It reuses saved submissions. Interrupted coding or review phases require a fresh evaluation; resume cannot repair a failing solution.

Supports five [DeepSWE/Harbor tasks](docs/running-evaluations.md). Text is supplied directly; plugin hooks, tools, and automatic skill discovery are outside this evaluation. Smaller numbers alone do not prove better work.

[Details and limits](docs/running-evaluations.md) · [Security](SECURITY.md)
