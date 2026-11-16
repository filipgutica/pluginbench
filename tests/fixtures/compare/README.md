# Compare fixture

Reduced copies of the engineering guidance climb's saved reports (`runs/egc-20261106/results/pilot/`, 2026-11-07): the pilot's two evaluations and round 1's two.

Each `report/results.json` keeps only what `pluginbench compare` reads for `card-gates.yaml`: attempt IDs, tasks, packages, correctness with the native rewards, the gate metrics' observations, and the reference values. No prompts, diffs, review text, or paths.

`card-gates.yaml` states the card's gates 1–3. The study's `analyze.py` gives the same numbers for these runs' measures.

The reports are converted to grade contract 1 (one pass or fail per attempt): each attempt the original report graded `unknown` takes its native reward as its grade (reward 1 passes, reward 0 fails). The pass counts therefore follow that conversion, not the original study.
