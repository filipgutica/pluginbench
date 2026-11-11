# DeepSWE panel qualification

This panel prepares a vanilla ×2 screen for `gpt-6.1-sol`, reasoning `high`.
No coding run or paid model call is authorized by these files.

The source is DeepSWE commit `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea`.
The manifest records 39 candidates, including nine replacements for rejected contracts.
Thirty-two verifier contracts passed static review. Seven candidates failed static review or were
deferred; six reviewed contracts failed runtime preparation because their source contains symlinks.
All 25 selected tasks pass oracle reward 1 and nop reward 0, with canonical patch
reconstruction and cleanup complete. The manifest records qualification and check readiness per task.

## Selection

The screen candidate pool has 25 tasks from 24 repositories: 5 JavaScript/TypeScript tasks,
7 Python tasks, 9 Go tasks, and 4 Rust tasks. Anko contributes two tasks; every other
repository contributes one.
Adaptix aliases and CSSTree shorthand support are included as requested.

Selection favors library changes with explicit public behavior and local regression tests:
parsing, transformations, structured data, diagnostics, formatting, and state restoration.
The Rust tasks include larger engine changes to retain language diversity.
This is a candidate pool for empirical screening; static review does not establish its difficulty.
The later screen should select 12–16 tasks where vanilla passes 30–90% of attempts.

The manifest records each reviewed contract's checks and limitations.
Three candidates have concrete contract risks: contradictory scope instructions,
real-clock expiry, or object-ID reuse across discarded values.
Four broader contracts are deferred because complete review was not completed.
Their verifier digests are not admitted. Task graph export, Valibot, Optique, Query, Pest, and Participle are
also excluded because their source trees contain symlinks rejected by canonical export.
PSD Tools has the expected reference rewards but its grader starting source differs from the
frozen coding fixture, so canonical reconstruction is unavailable and it is also excluded.
This change preserves both source qualification and export policies.

## Verifier review

Each admitted digest binds the complete `tests/` tree, including executable modes:
`Dockerfile`, `grader.py`, `test.sh`, `config.json`, and `test.patch`.
All candidate graders match `_GRADER_SHA256`.
The verifier Dockerfiles match the reviewed copy-only template over each task image.
Configuration validation checks the base commit, test-ID arrays, and report format/paths.
Review covers the entrypoint's complete task-specific commands and report normalization,
the shared fail-closed grader, and the hidden test patch against the instruction.

The new entries in `src/pluginbench/submissions.py` name their task and pinned dataset commit.
Existing admission still verifies the exact patch-collection command, separate verifier,
no-network mode, and absence of verifier environment overrides.
Runtime qualification additionally checks the image source, base tree, trusted tests,
commit capability, stopped export, and canonical patch reconstruction.

The etree verifier removes authored root test files before its hidden suite.
Its native reward therefore does not prove that the submission's own tests pass.
Collectors still measure the canonical submitted patch, as for the existing contracts.

## Reproduce

Prepared tasks are under:

```text
.panel/tasks
```

`prepare-tasks.sh <new output directory>` fetches only the manifest's candidates at the pin.
It sets `umask 022`, checks verifier hashes/modes, and repairs Git identity only through
`[environment.env]`: `Task Developer <developer@example.invalid>`.
It does not change the verifier or hidden tests.
The output must not exist; the script will not overwrite a previous preparation.

`vanilla-x2.yaml` resolves its relative task path to the directory above from the repository root. Change only `tasks.source` if you prepare elsewhere.
It has no treatments or reviewer, two repeats, two jobs, and the existing 1,800-second agent budget.
For this correctness screen, selected measurements are tokens, lines, and coverage.
The CSSTree and Adaptix coverage profiles use task-specific commands.
Other tasks report coverage as `not_configured` with `require_ready: false`.
Complexity and unused-code collection are outside this screen's selected measurements.

Use a scratch cache and new output directories, outside the existing study runs:

```sh
umask 022
export PYTHONPATH="$PWD/src"
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
export XDG_CACHE_HOME="$PWD/.panel/cache"
python -W ignore -m pluginbench check docs/evidence/deepswe-panel-20261109/vanilla-x2.yaml --output .panel/check-panel-qualified
python -W ignore -m pluginbench run docs/evidence/deepswe-panel-20261109/vanilla-x2.yaml --dry-run
```

Both commands make no model calls. Do not remove `--dry-run` without the study owner's approval.
Raw task preparation and oracle/nop logs remain in `.panel/qualification/` and `.panel/qualification-mode-fixed/`.
The checked manifest and dry-run output are the compact evidence committed here.

## Vanilla-only configuration

The evaluation schema and frozen `StudyRecipe` schema previously rejected `treatments: {}`.
Both now accept an empty map; validation of named treatments is unchanged.
One regression test covers the two-attempt CLI plan and frozen recipe loading.
Coverage admission was already optional and has no implementation change.

Cache-key compatibility was checked using a private configuration against baseline main commit `01597a8`
and this branch, with scratch outputs and a scratch cache.
The saved comparison records task keys and attempt decisions from both `check` outputs.

## Final readiness

The final `check` reports ready for all 25 selected tasks with zero model calls.
Coverage is ready for Adaptix and CSSTree; the other 23 tasks report `not_configured`.
Every other readiness row is ready. The dry run plans 50 vanilla attempts and zero reviews.
`verification.json` binds this check to the configuration and exact reference preparations.

## Repository checks

`verification.json` records Ruff passing, the same 17 mypy diagnostics as baseline main commit `01597a8`,
and 1,207 passing tests with the three failures also reproduced on that baseline.
The focused CLI, submission, and measurement-profile tests pass: 159 passed, four
skipped because saved diagnostic cohorts are unavailable.
The one new regression failed before removing the schema constraints and passed afterward.
The preparation script reproduced all 39 verifier trees with identical bytes and modes.

## Privacy edits

Machine-specific paths in `manifest.json` and `verification.json` are now relative to the repository root.
`cache-keys.json` replaces the private configuration path with `<private-config-redacted>`.
`vanilla-x2.yaml` uses a config-relative task source and a neutral temporary coverage filename.
The commands above use a repository-local virtual environment.
Historical hashes and readiness results retain their original values; they describe the original inputs,
not the edited YAML bytes. No evidence files were removed.
