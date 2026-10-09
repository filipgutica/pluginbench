# Workflow Pytest canary

This benchmark is a fast iteration pilot for engineering workflow skills.
It is not a general effectiveness study.

## Run the repair pilot

Pass the exact skill tree under test. Do not rely on the config's fallback path.

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/workflow-pytest-canary/repair.yaml \
  --skill /path/to/plugins/workflow/skills \
  --output /absolute/path/to/run-root \
  --yes
```

Before a paid run, replace `run` with `run ... --dry-run` and confirm:

- the intended skill names for the supplied bundle
- the intended skill digest
- the repair seed digest `sha256:5e7174f7973d6313c492aa3285ce46fa827ea61bff073a5a80f284375aa5d74c`
- `expected_score: 0`
- three attempts per arm

The checked-in repair config uses three attempts for a fast pilot. It cannot
satisfy the notebook's final promotion rule of four repairs in five attempts.
Freeze a five-attempt config before a promotion study.

PluginBench's current machine decision uses task pass-rate lift. It does not
enforce the per-attempt repair or preservation gates. Treat `Decision` as a
pilot filter only. Read the starting-patch outcome table and apply the notebook's
4-of-5 repair and 3-of-3 preservation gates separately.

## Run the preservation control

Use the same skill snapshot that produced the repair result.

```bash
PATH=/opt/homebrew/bin:$PATH .venv/bin/pluginbench run \
  benchmarks/workflow-pytest-canary/preservation.yaml \
  --skill /path/to/plugins/workflow/skills \
  --output /absolute/path/to/run-root \
  --yes
```

Confirm the preservation seed digest
`sha256:038019d0a9fdc60a87a4ac6fb65cc4398f655e1bc1a299037fcc56fe59a259e8`
and `expected_score: 1` in the dry-run output.

## Preserve evidence

PluginBench ignores raw `runs/` directories because they contain large
workspaces and host-specific traces. Do not treat an ignored run directory as a
durable research record.

After each accepted experiment:

1. Record the resolved config, skill digest, report metrics, decision, and run path in the lab notebook.
2. Preserve the report, arm manifests, verifier reports, and relevant `agent-change.diff` artifacts in a tracked or external archive.
3. Record checksums for archived artifacts.
4. Do not delete the raw run until the archive has been verified.

See the [filip-stack lab notebook](https://github.com/filipgutica/filip-stack/blob/main/docs/workflow-skill-evaluation-lab-notebook.md).
