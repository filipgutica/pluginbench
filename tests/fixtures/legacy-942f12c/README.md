# Saved state from PluginBench 942f12c

Upgrade tests read these files as saved runs and caches from commit `942f12c`. `generate.py` writes them by running that commit's own code with Docker stubbed, and checks that `942f12c` itself admits the vanilla cohort and reuses the prepared entry. Every task, prompt, patch and receipt is synthetic.

Regenerate from the repository root:

```sh
python tests/fixtures/legacy-942f12c/generate.py
git add -f tests/fixtures/legacy-942f12c
```

`-f` is needed because cache entries keep files under `logs/` and `dist/`, which `.gitignore` excludes elsewhere.

| Path | What 942f12c saved |
| --- | --- |
| `runtime.json` | A Harbor runtime identity |
| `vanilla-identity.json`, `vanilla-task/`, `vanilla/` | A vanilla identity, its task catalog, and a two-repeat vanilla cache cohort |
| `prepared-task/`, `prepared-limits.json`, `prepared/`, `reference-agents/` | A prepared-image cache entry, the task and limits it was prepared from, and its oracle and nop result |
| `observer-identities.json` | The analysis and coverage observer hashes frozen into measurement profiles |
