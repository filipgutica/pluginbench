---
name: systematic-debugging
description: "Use when a bug, failing test, build failure, CI failure, performance problem, or unexpected technical behavior needs a root-cause investigation."
---

# Systematic debugging

Find the cause before proposing a fix. Keep investigation read-only unless the user grants implementation authority.

## Investigation

1. Read the full error, stack, and available logs.
2. Reproduce with the narrowest credible command or exact user steps.
3. Record whether the failure is deterministic.
4. Inspect recent changes, inputs, environment, and component boundaries.
5. Trace the incorrect value or state backward to its source.
6. Compare the failing path with a working local pattern.
7. State one falsifiable hypothesis and its evidence.
8. Run the smallest read-only experiment that can disprove it.
9. Repeat with a new hypothesis when the evidence rejects the current one.

Do not build a source-code hypothesis for a CI test failure before local reproduction. If the isolated test passes locally, investigate stale state or infrastructure before application code.

## Fix route

After the evidence identifies a cause:

1. Stop with a fix path when implementation authority is absent.
2. Route an authorized correction through `$workflow:coordinator` and `$workflow:implementation`.
3. Add focused regression coverage when feasible.
4. Fix the causal boundary instead of its downstream symptom.
5. Repeat the reproduction and affected checks.

After three failed causal fix attempts, stop and reassess the architecture or the original diagnosis with the user.

Read [upstream.md](references/upstream.md) for provenance and local adaptation decisions.
