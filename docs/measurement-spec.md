# Measurement specification

Status: Ready, except JavaScript/TypeScript test redundancy (gate G4).

## Context

PluginBench compares coding runs with and without supplied guidance (global instructions, skills, plugins). It must answer two questions:

1. Does a guidance package make the agent write code that is correct, appropriately small and simple, and tested?
2. Is any improvement worth its resource cost?

Current measurements answer these questions poorly:

- Complexity is reported as the highest score in the whole snapshot, or among functions that touch a changed line. Pre-existing code dominates both. A higher score can mean "edited a complex existing function", not "wrote complex code".
- Coverage is a whole-suite rate over the whole source tree. A change of a few hundred lines barely moves it.
- Nothing compares a submission with how much code the task actually needed.

The 2026-10-16 measurement addendum showed these limits on four DeepSWE tasks.

## Goal

For every attempt, report measures of correctness, the code it added, the tests it added, and its cost. Report code measures beside the same measures for the task's reference solution. A reader can then see whether a guidance package makes agents write more, less, or similarly complex and sized code than the task required, and what it costs.

## Non-goals

- A composite quality score. Each measure is reported separately.
- An automatic "over-engineered" verdict. PluginBench reports ratios to the reference. The reader judges.
- New coding or review model calls. All measures in this specification are offline observations of saved submissions.
- Measuring code style or naming.
- Changing correctness grading or review criteria.

## Requirements

### R1. Correctness (unchanged)

Report confirmed pass, fail, unknown, and execution failure per attempt, as today. All other measures are reported for all attempts and, separately labelled, for passing attempts.

### R2. Code added (source only)

Measure only declared source files. Exclude test files, using the existing test classification and `test_scope`.

| Measure | Definition |
| --- | --- |
| Source lines added, removed, net | Existing line-change counts. Net = added − removed. |
| Net complexity added (cognitive, cyclomatic) | Sum of scores of all source functions in files the attempt changed, after the edit, minus the same sum before. Unchanged functions cancel. New functions count in full; deleted functions subtract. Go closure rows inside a declaration are excluded because that declaration's score already includes them. The value can be negative. |
| Functions added | After-side functions whose `(path, name)` does not exist before. |
| Source files added | Source paths absent from the before snapshot that contain at least one analyzed function. Files with no functions, such as constants-only modules, and out-of-scope scripts are not counted. |
| Complexity hotspots | Count of added functions, or functions whose score grew, with a cognitive score above 15. |
| Unused code introduced | Existing "introduced dead findings". |
| Duplication introduced | Existing "introduced clone groups" (JavaScript/TypeScript only). |

Complexity scores come from the language's pinned analyzer. Never pool raw scores across analyzers.

### R3. Reference baseline

- When a task ships `solution/solution.patch`, PluginBench applies it to the frozen before snapshot. It measures the result with the same collectors and settings as the submissions.
- The reference is not a coding attempt. It has no token, time, or review measures. It does not count in assigned, invocation, or baseline totals.
- For every R2 measure, report the reference value and each attempt's ratio to it (attempt ÷ reference). When the reference value is zero or negative, report the difference instead and label it as a difference.
- Ratios make tasks and languages comparable. Package summaries average per-task median ratios with equal task weight.
- If a task has no reference patch, or the patch does not apply, report the reference measures as unknown with the reason. Never substitute zero.

### R4. Tests added

| Measure | Definition |
| --- | --- |
| Test lines added | Existing count. |
| Changed-line coverage | Share of the attempt's added executable source lines that its submitted visible test suite executes. Branch coverage uses the same population when the report format supplies it. |
| Redundant added tests | Added test cases whose covered added source lines are all covered by other test cases. One suite run with per-test coverage tracking supplies the data. Equal coverage does not prove equal assertions, so this flags candidates only. |
| Duplicated test code | Added test lines, in test source files, that belong to a six-line block repeated elsewhere in the added test code. Copies of pre-existing tests are not detected. |

Per-test coverage is available through coverage.py dynamic contexts. JavaScript/TypeScript support depends on G4; until then, report redundant added tests as unknown with a reason for those tasks.

Reference solutions add no tests, so R4 has no reference baseline. Compare attempts with vanilla.

### R5. Cost

- Report coding input, cached input, uncached input, output, and reasoning-output tokens, root plus descendants.
- Report coding elapsed time.
- Report review tokens separately.
- Report dollars only from an optional, dated per-model price table in `prices.json` beside the saved evaluation. It is reporting input, not a measurement setting, so it can be added to saved evaluations. Without it, dollars are unknown.

### R6. Removed measures

Stop emitting these derived metrics from new reports. Saved raw evidence is unchanged.

- Reported cognitive and cyclomatic maximum (whole snapshot).
- Changed-function cognitive and cyclomatic maximum.
- New-file function cognitive and cyclomatic maximum.
- Whole-suite line, statement, branch, and function coverage percentages, from charts and summary tables. Keep them in the CSV as diagnostics; a large drop can flag broken existing tests.
- Reported and resolved dead findings and clone groups. Introduced counts remain.
- The leave-one-out test-overlap measure (`test_overlap`, pytest `test_contribution` settings). It needs one container run per added test case. Redundant added tests replace it.

### R7. Failure handling

- An unavailable collector, missing reference, or empty population produces "unknown" with a reason. It is never zero.
- An empty population that is complete, such as a no-op submission, reports zero for counts and sums. It reports "undefined" for ratios with a zero denominator.

## Design and ownership

| Behavior | Current owner |
| --- | --- |
| Per-function scores and changed-line overlap | `assessment_metrics.py` (`_function_changes`) |
| Coverage populations per line | `coverage_report.py`, `assessment_metrics.py` (`_coverage`) |
| Observation catalog and report rows | `assessment_charts.py`, `evaluation_report.py` |
| Offline measurement of saved submissions | `remeasure.py` |
| Report presentation | `assessment-charts.html` |

- R2 and R4 derive from evidence that existing collectors already record: function rows for both sides, changed-line ranges, and per-line coverage populations. No new analyzer is required.
- R3 adds one measured input per task, the reference snapshot. It flows through the same observer revision as submissions, so saved evaluations can gain it offline.
- The report's summary tables show each R2 measure with its "× reference" ratio beside the raw value. Task views also show the reference value.

## Verification

- Unit tests on fixture snapshots with known functions establish R2:
  - Adding a function adds its full score.
  - Growing a function adds only its growth.
  - Deleting a function subtracts its score.
  - Test files are excluded.
- A fixture with known covered and uncovered added lines establishes changed-line coverage.
- A fixture with per-test coverage, where one added test duplicates another's covered lines, flags exactly that test as redundant.
- Selecting `test_overlap` or supplying `test_contribution` settings fails validation with a clear message, before any coding call.
- Reference baseline:
  - Applying the reference patch to its task's before snapshot reproduces the patch's changed paths.
  - A missing patch yields unknown with a reason.
- Regression: a saved evaluation re-measured offline keeps every correctness, token, line, and review observation byte-identical. It makes zero model calls.
- Acceptance: re-measure `native-workflow-evaluation-20261016-v1` and report R2–R5 for all 60 attempts and four references.

## Risks, assumptions, and open questions

### Decisions

- Whole-suite coverage: CSV diagnostics only.
- Hotspot threshold: cognitive score above 15 for every language.
- Price table: optional, dated, per model.
- Test redundancy: per-test coverage in one run replaces leave-one-out test overlap.

### Gate

- **G4. JavaScript/TypeScript per-test coverage.** Confirm whether the pinned Vitest and c8 setups can attribute coverage to individual tests in one run. This blocks only JavaScript/TypeScript redundant added tests.

### Assumptions

- `(path, name)`, with Go line numbers removed from names, identifies a function well enough for "added" and "grew". Nested functions, overloads, and renames can misclassify. Net complexity added does not depend on matching.
- Reference patches are source-only for the current tasks. A future reference that adds tests is measured on source only for R2.
- Fallow (JavaScript/TypeScript) and rust-code-analysis (Python) define complexity differently. Ratios to a same-analyzer reference remove this difference within a task.

### Non-blocking questions

- Whether to add public surface added (new exported symbols) to R2. Fallow reports exports; Python has no equivalent collector today.
- Whether to show the AI review's "scope" and "maintainability" findings beside R2 as auxiliary over-engineering judgments.
