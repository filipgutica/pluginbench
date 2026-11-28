# Agent runbook

## Purpose

Compare coding with the workflow plugin against vanilla, with no plugin, on Claude Code and Codex.

For each harness, use the same task, prompt, coding model, effort, CLI, seed, resources, and review protocol in both arms.

Use `claude-opus-5-5` at medium for Claude Code coding.

Use `gpt-6.2-sol` at high to review Claude Code attempts.

Use `claude-opus-5-5` at high to review Codex attempts.

## Setup and checks

Read the private launch manifest named by the owner before starting.

Use it to resolve the exact Codex coding model and effort, reviewer CLI versions, workflow package, rendered guidance, tool paths, run directories, and credential handoff.

Work in a separate worktree.

Assign one writer per area when delegating.

Set `PLUGINBENCH_VENV` to a fresh scratch environment path.

Run these commands from the worktree root.

```sh
python3.12 -m venv "$PLUGINBENCH_VENV"
```

```sh
. "$PLUGINBENCH_VENV/bin/activate"
```

```sh
python -m pip install -e '.[dev]'
```

```sh
python -c 'import pluginbench; print(pluginbench.__file__)'
```

Expect the import path to resolve inside this worktree.

If the scratch setup uses a `.pth` file, point it at this worktree's `src` directory and repeat the import check.

Run the issue's focused checks and the Python CI checks.

```sh
ruff check src tests
```

```sh
mypy src tests
```

```sh
pytest
```

For process lifecycle changes, run the real systemd tests on a suitable host or in CI.

```sh
PLUGINBENCH_REQUIRE_SYSTEMD_TESTS=1 pytest -m systemd
```

For Go observer checks, put the Go bin directory specified in the launch manifest on PATH before checking the version.

```sh
go version
```

Expect the pinned Go version, 1.26.4.

Commands for features introduced in an issue apply once that feature is implemented.

## Host safety

Use a scratch cache and fresh output directories from the launch manifest.

Set `XDG_CACHE_HOME` to the scratch cache before starting.

Follow the launch manifest's limits on which paths you can read or write.

Check the manifest's host workload guard before Docker-heavy work.

Wait while the protected workload is active.

Limit coding and run jobs to two concurrent Docker jobs.

Measure one submission at a time in the separate mutation pass.

A measurement run may use up to four mutant containers at 2 CPU and 2048 MiB each.

The runner may clean up its own labelled trial containers and images.

Manual `docker rm`, `docker rmi`, and prune are forbidden.

Set the mutation deadline to at least `HEADROOM_PERCENT / 100` times the worst projected reference time.

The pinned value of 200 means 2×.

Use isolated test fixtures for coordinator tests.

A different cache or output path still shares the production account's coordinator and host lock.

## Paid runs and approvals

Use subscription logins only.

Use the credential handoff in the private launch manifest.

Keep credentials private and out of logs and evidence.

Before launch, record the certified set, exact configuration revision, workflow and guidance digests, expected coding and review slots, approved root-invocation ceiling, and allowed native subagent use.

Dry runs count planned root slots.

Retries and native subagent turns can add usage.

A dry run or fake-worker check grants no paid approval.

The Small pilot has 12 coding slots and 12 review slots.

Use the pilot's approved ceiling.

Obtain separate approval for Medium, Large, any extra paid smoke, and any increase in approved scope.

On interruption, use the saved run, measurement, or job revision.

Keep completed work and check the remaining cap before retrying.

## PRs and review

Use small stacked PRs with a reproducible check for each change.

Assign a Reviewer who did not author the change to inspect the exact head and its evidence.

Use a Critic for qualification, spending limits, result integrity, concurrency, authenticated writes, and network exposure.

The orchestrator checks review findings, verifies the final head, and keeps any unresolved checks visible.

Get owner approval for each public or irreversible action or a defined batch of those actions.

Squash-merge after applicable CI passes and actionable findings are resolved.

Keep public evidence to summary counts and safe identifiers.

Exclude prompts, traces, diffs, employer names, and AI attribution.

Use release-please for versioning.

Each PyPI publish needs separate owner approval.

## Handoff record

Keep the handoff record private.

Record the issue, exact head or run revision, safe log locations, commands, expected and actual results, approvals, and the next safe action.

The worker supplies evidence.

The orchestrator checks that evidence before accepting the result.

## Where to hand back to the owner

- A required private input, credential handoff, configuration pin, or approval is missing.
- A qualification threshold is unspecified or the selected tasks fall below the issue's stated floor.
- A paid batch exceeds its approved scope, reaches its invocation ceiling, or encounters an unresolved provider, auth, evidence, or cleanup blocker.
- A design needs acceptance, a private deployment needs authorization, or an actual phone check requires the owner's device.
- A public or irreversible action still needs approval, including a PyPI publish.
