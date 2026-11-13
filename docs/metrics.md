# Metrics glossary

Regenerate with `PYTHONPATH=src python scripts/gen_docs.py` from the repository root.

Metric names below are the exact strings accepted in a gate's `metrics` list.
Definitions and units come from `METRIC_GUIDE` in `src/pluginbench/assessment_charts.py`.
Point gates choose their direction; the code defines no universal better direction for each metric.
Paired token bounds require both Input tokens and Output tokens, with `direction: lower`.
Saved Codex and Claude Code usage is re-validated on cache reuse and report generation.
Invalid, disagreeing, or legacy stdout-only evidence makes tokens unavailable; token gates treat it as unknown.
Token provenance retains original figures and recording time in `usage_validation`.
Compare treats tokens without validated provenance as unknown in every report schema.
Higher coverage or lower cost alone does not establish a better solution.
Correctness gates use `correctness: passes` or `correctness: unknown` instead of metric names.
See [running evaluations](running-evaluations.md#compare-runs) for gate statistics and bounds.

| Metric | What it measures | Unit | Gates-file name | Direction |
| --- | --- | --- | --- | --- |
| Line coverage | Executable source lines reached by the selected tests. Unsupported units remain unknown; coverage alone does not establish useful assertions. | % covered | `Line coverage %` | Gate chooses |
| Statement coverage | Source statements reached by the selected tests. Go counts statements within measured blocks; this is not branch or function coverage. | % covered | `Statement coverage %` | Gate chooses |
| Input tokens | Tokens the implementation model read: the task, guidance, context, and tool results. Repeated context can be counted again on later turns; fewer tokens means less input used. | tokens | `Input tokens` | Lower for paired token bounds |
| Output tokens | Tokens the implementation model generated. Reasoning is included when the harness counts it in output; fewer tokens means less output used, not necessarily a better solution. | tokens | `Output tokens` | Lower for paired token bounds |
| Review input tokens | Tokens the independent reviewer read while judging the submitted code. These are review costs, separate from the coding model's input. | tokens | `Judge input tokens` | Gate chooses |
| Review output tokens | Tokens the independent reviewer generated while judging the submitted code. These are review costs, separate from the coding model's output. | tokens | `Judge output tokens` | Gate chooses |
| Branch coverage | Percentage of code branches exercised by the selected tests. Higher means a larger share ran; it does not prove their behavior was checked correctly. | % covered | `Branch coverage %` | Gate chooses |
| Function coverage | Percentage of functions called by the selected tests. Higher means a larger share ran; it does not prove every path or result was checked. | % covered | `Function coverage %` | Gate chooses |
| Source lines added | Source lines added by the agent's edits, including comments and blank lines. A rewritten line counts once as removed and once as added; these are not net changes. | lines | `Source lines added` | Gate chooses |
| Source lines removed | Source lines removed by the agent's edits, including comments and blank lines. A rewritten line counts once as removed and once as added; these are not net changes. | lines | `Source lines removed` | Gate chooses |
| Test lines added | Test lines added by the agent's edits, including comments and blank lines. A rewritten line counts once as removed and once as added; these are not net changes. | lines | `Test lines added` | Gate chooses |
| Test lines removed | Test lines removed by the agent's edits, including comments and blank lines. A rewritten line counts once as removed and once as added; these are not net changes. | lines | `Test lines removed` | Gate chooses |
| New unused-code warnings | Unused-code warnings present after the task that were absent from the starting scan. Public APIs and runtime calls can produce false positives. Zero means no new warnings. | warnings | `Introduced dead findings` | Gate chooses |
| New duplicate-code groups | Groups of matching code blocks detected after the task but absent from the starting scan. Lower means fewer new groups; this counts groups, not duplicated lines. | groups | `Introduced clone groups` | Gate chooses |
| Tests with no unique branch coverage | Supplied tests whose covered branches are also covered by other supplied tests. Shared coverage does not mean shared assertions or unnecessary tests. | tests | `Branch contribution` | Gate chooses |
| Cached input tokens | Input tokens served from the provider's prompt cache. Usually priced far below fresh input. | tokens | `Cached input tokens` | Gate chooses |
| Uncached input tokens | Input tokens that were not served from cache: input minus cached input. The better input-cost signal. | tokens | `Uncached input tokens` | Gate chooses |
| Reasoning output tokens | Output tokens spent on hidden reasoning. A subset of output tokens, not added to them. | tokens | `Reasoning output tokens` | Gate chooses |
| Source lines net | Source lines added minus source lines removed. Negative means the submission shrank the code. | lines | `Source lines net` | Gate chooses |
| Coding time | Elapsed time of the coding run, including subagents. Shared machine load can affect it. | seconds | `Coding time` | Gate chooses |
| Cognitive complexity added | Branching-and-nesting the agent added to source code: changed functions after the edit minus the same functions before. New functions count in full; deleted ones subtract. | score | `Net cognitive complexity added` | Gate chooses |
| Cyclomatic complexity added | Independent paths the agent added to source code, measured like cognitive complexity added. Editing an already-complex function counts only its growth. | paths | `Net cyclomatic complexity added` | Gate chooses |
| Functions added | Source functions after the edit whose path and name did not exist before. Renames count as added. | functions | `Functions added` | Gate chooses |
| Source files added | New non-test source files. More files is not automatically worse; compare with the reference. | files | `Source files added` | Gate chooses |
| Complexity hotspots added | New or grown source functions whose cognitive score is above 15, a common review threshold. | functions | `Complexity hotspots added` | Gate chooses |
| Changed-line coverage | Share of the agent's added executable source lines that its submitted tests run. Running a line does not prove its behavior is checked. | % covered | `Changed-line coverage %` | Gate chooses |
| Changed-branch coverage | Share of branches on the agent's added source lines that its submitted tests take. | % covered | `Changed-branch coverage %` | Gate chooses |
| Redundant added tests | Added tests whose covered added lines are all covered by other tests. Candidates only: equal coverage does not mean equal assertions. | tests | `Redundant added tests` | Gate chooses |
| Duplicated added test lines | Added test lines inside a six-line block that repeats elsewhere in the added tests. | lines | `Duplicated added test lines` | Gate chooses |
| Review findings | Actionable issues reported for each submission by an isolated AI reviewer using the same criteria for vanilla and guided code. Fewer means fewer reported issues, not proof of fewer defects. | findings | `Total review findings` | Gate chooses |
| P0 · Critical · immediate blocker | Issues assigned this severity by the independent reviewer. The same rubric applies to both groups; severity is a judgment, not a measured defect count. | findings | `P0 review findings` | Gate chooses |
| P1 · High · fix before landing | Issues assigned this severity by the independent reviewer. The same rubric applies to both groups; severity is a judgment, not a measured defect count. | findings | `P1 review findings` | Gate chooses |
| P2 · Medium · should fix | Issues assigned this severity by the independent reviewer. The same rubric applies to both groups; severity is a judgment, not a measured defect count. | findings | `P2 review findings` | Gate chooses |
| P3 · Low · concrete improvement | Issues assigned this severity by the independent reviewer. The same rubric applies to both groups; severity is a judgment, not a measured defect count. | findings | `P3 review findings` | Gate chooses |
| Correctness findings | Incorrect behavior or a missed requirement, as reported by the independent reviewer. Each issue has one category; fewer means fewer reported issues of this kind. | findings | `Correctness review findings` | Gate chooses |
| Scope findings | Unnecessary changes beyond the task, as reported by the independent reviewer. Each issue has one category; fewer means fewer reported issues of this kind. | findings | `Scope review findings` | Gate chooses |
| Maintainability findings | Complexity, duplication, or unclear responsibilities, as reported by the independent reviewer. Each issue has one category; fewer means fewer reported issues of this kind. | findings | `Maintainability review findings` | Gate chooses |
| Tests findings | Missing, misleading, or low-value tests, as reported by the independent reviewer. Each issue has one category; fewer means fewer reported issues of this kind. | findings | `Tests review findings` | Gate chooses |

## Report files

`report/results.json`, `report/results.csv`, and `report/report.html` name the no-guidance arm `vanilla`.
CSV column names are unchanged; the `arm` column now contains `vanilla` instead of `baseline`.
The JSON format is `schema_version: 2`. Version 1 called this arm `baseline`.
`compare`, `status`, and `judge prepare` accept both report versions without rewriting them.

| Version 1 | Version 2 | Location |
| --- | --- | --- |
| `baseline` | `vanilla` | `arm` and `guidance` values; `completeness_matrix[].cells` keys |
| `baseline_median` | `vanilla_median` | `comparisons[]` |
| `baseline_assigned` | `vanilla_assigned` | `comparisons[]` |
| `baseline_measured` | `vanilla_measured` | `medians[]`, `raw_medians[]`, `comparisons[]` |
| `baseline_value` | `vanilla_value` | `aggregates[]` |
| `baseline_origin` | `vanilla_origin` | `attempts[]` |
| `reused_baseline_attempts` | `reused_vanilla_attempts` | Top-level field |

Arm values change in attempts, observations, medians, comparisons, aggregates, reference comparisons and aggregates,
passing medians and aggregates, review findings, cost rows, and measurement completeness rows.
HTML uses the same public data, labels, and vanilla-first ordering.
Opaque attempt IDs, evidence paths, provenance, and comparison identities keep their stored values.
Evidence files, `inputs/study.json`, run identities, and cache keys still use `baseline` internally.
Existing runs and vanilla caches can resume and reuse without migration.
