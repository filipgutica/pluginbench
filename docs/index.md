# PluginBench

PluginBench tests whether skills, plugins, or instructions help coding agents make fewer mistakes. It runs the same coding tasks with vanilla and with each package you choose, holds the harness, model, and reasoning fixed, and compares every package with vanilla on tokens, code added, complexity, dead code, coverage, and review findings.

Install and first run: see the [README on GitHub](https://github.com/filipgutica/pluginbench#readme).

- [Running evaluations](running-evaluations.md): choose inputs, run, resume, and compare.
- [Config reference](config-reference.md): every setting in `evaluation.yaml`.
- [Metrics](metrics.md): what each measure means and how it is computed.
- [Reproduce a result](reproduce.md): the files and settings that identify a run.

The [measurement specification](measurement-spec.md) defines the measures and their known limits. To report a vulnerability, see the [security policy](https://github.com/filipgutica/pluginbench/security/policy).
