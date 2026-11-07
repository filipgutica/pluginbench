# Compare fixture

Reduced copies of the engineering guidance climb's saved reports (`runs/egc-20261106/results/pilot/`, 2026-11-07): the pilot's two evaluations and round 1's two.

Each `report/results.json` keeps only what `pluginbench compare` reads for `card-gates.yaml`: attempt IDs, tasks, packages, correctness with the native rewards, the gate metrics' observations, and the reference values. No prompts, diffs, review text, or paths.

`card-gates.yaml` states the card's gates 1–3. The study's `analyze.py` gives the same numbers for these runs.
