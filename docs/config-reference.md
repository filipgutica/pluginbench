# Configuration reference

Regenerate with `PYTHONPATH=src python scripts/gen_docs.py` from the repository root.

Use YAML or JSON; paths are relative to the config file.
Unknown fields are rejected. `<name>` denotes a mapping key; `[]` denotes a list item.
Required applies within the containing object, including optional objects when supplied.
Defaults are JSON values; `—` means no default. Validators also enforce combinations of fields.
See [running evaluations](running-evaluations.md) for valid combinations and execution requirements.

## Evaluation file

| Key path | Type | Default | Required | Description |
| --- | --- | --- | --- | --- |
| `name` | `str` | `"evaluation"` | no | Names the evaluation in plans and reports. |
| `runner` | `Runner` | `—` | yes | Selects the coding harness, model, and reasoning effort. |
| `runner.harness` | `Literal['codex', 'claude']` | `—` | yes | Selects the coding or review CLI. |
| `runner.model` | `str` | `—` | yes | Selects the model ID, with an exact claude-* ID required for Claude. |
| `runner.reasoning` | `Literal['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']` | `—` | yes | Selects reasoning effort, subject to harness and evaluation validation. |
| `treatments` | `dict[str, Treatment]` | `—` | yes | Maps treatment names to nonempty native packages, with vanilla included automatically. |
| `treatments.<name>.guidance` | `list[Path]` | `[]` | no | Selects UTF-8 .md or .txt instruction files relative to the config file. |
| `treatments.<name>.skills` | `list[Path]` | `[]` | no | Selects standalone skill roots or collections relative to the config file. |
| `treatments.<name>.plugins` | `list[Path]` | `[]` | no | Selects complete native plugin directories relative to the config file. |
| `capabilities` | `NativeCapabilities` | `{"subagents":true,"max_threads":4,"max_depth":1}` | no | Sets the same native agent capabilities for vanilla and every treatment. |
| `capabilities.subagents` | `bool` | `true` | no | Enables native agents for vanilla and every treatment. |
| `capabilities.max_threads` | `int` | `4` | no | Limits concurrent native children to between one and sixteen. |
| `capabilities.max_depth` | `int` | `1` | no | Native V1 depth preference; ignored by model-selected V2 agents. |
| `tasks` | `Tasks` | `—` | yes | Selects exactly one task source and a unique nonempty task inventory. |
| `tasks.source` | `Path \| None` | `null` | no | Selects a local task directory instead of a dataset or GitHub URLs. |
| `tasks.dataset` | `str \| None` | `null` | no | Selects a Harbor dataset instead of a local source or GitHub URLs. |
| `tasks.version` | `str \| None` | `null` | no | Selects the dataset version, which resolves to an immutable content hash. |
| `tasks.ids` | `list[str]` | `[]` | no | Selects unique task IDs from the local source or dataset. |
| `tasks.urls` | `list[str]` | `[]` | no | Selects unique GitHub task-folder URLs whose refs resolve to commits. |
| `measurements` | `list[Literal['tokens', 'lines', 'cyclomatic', 'cognitive', 'dead_code', 'coverage', 'review']]` | `—` | yes | Selects unique measurements, with review requiring reviewer settings. |
| `measurement_settings` | `MeasurementSettings` | `{"source_scope":null,"test_scope":null,"fallow_config":null,"coverage":null,"coverage_profiles":{},"test_contribution":null,"require_ready":false}` | no | Configures measurement scopes, tools, coverage profiles, and readiness requirements. |
| `measurement_settings.source_scope` | `list[str] \| None` | `null` | no | Selects repository-relative source patterns for measurements. |
| `measurement_settings.test_scope` | `list[str] \| None` | `null` | no | Selects repository-relative test patterns for measurements. |
| `measurement_settings.fallow_config` | `Path \| None` | `null` | no | Selects a Fallow configuration file relative to the evaluation config. |
| `measurement_settings.coverage` | `CoverageSettings \| None` | `null` | no | Provides the default coverage profile when no repository or language profile matches. |
| `measurement_settings.coverage.command` | `list[str]` | `—` | yes | Provides the coverage command as a nonempty argument list. |
| `measurement_settings.coverage.report_path` | `str` | `—` | yes | Names the coverage report relative to the repository without parent traversal. |
| `measurement_settings.coverage.tool_version` | `str` | `—` | yes | Records the nonempty coverage tool version in the measurement profile. |
| `measurement_settings.coverage.format` | `Literal['lcov', 'coverage-py-json', 'go-coverprofile']` | `"lcov"` | no | Selects the parser for the coverage report. |
| `measurement_settings.coverage.timeout_seconds` | `int` | `600` | no | Sets the coverage command timeout in seconds from one to 7200. |
| `measurement_settings.coverage.source_scope` | `list[str] \| None` | `null` | no | Selects repository-relative source patterns for this coverage profile. |
| `measurement_settings.coverage_profiles` | `dict[str, CoverageSettings]` | `{}` | no | Maps repository URLs or language names to coverage profiles, with repository matches taking precedence. |
| `measurement_settings.coverage_profiles.<name>.command` | `list[str]` | `—` | yes | Provides the coverage command as a nonempty argument list. |
| `measurement_settings.coverage_profiles.<name>.report_path` | `str` | `—` | yes | Names the coverage report relative to the repository without parent traversal. |
| `measurement_settings.coverage_profiles.<name>.tool_version` | `str` | `—` | yes | Records the nonempty coverage tool version in the measurement profile. |
| `measurement_settings.coverage_profiles.<name>.format` | `Literal['lcov', 'coverage-py-json', 'go-coverprofile']` | `"lcov"` | no | Selects the parser for the coverage report. |
| `measurement_settings.coverage_profiles.<name>.timeout_seconds` | `int` | `600` | no | Sets the coverage command timeout in seconds from one to 7200. |
| `measurement_settings.coverage_profiles.<name>.source_scope` | `list[str] \| None` | `null` | no | Selects repository-relative source patterns for this coverage profile. |
| `measurement_settings.test_contribution` | `NoneType` | `null` | no | Retains the removed test contribution setting as null for saved evaluations. |
| `measurement_settings.require_ready` | `bool` | `false` | no | Requires every selected measurement to be ready on every task before coding. |
| `reviewer` | `Reviewer \| None` | `null` | no | Configures the independent reviewer exactly when review is selected. |
| `reviewer.harness` | `Literal['codex', 'claude']` | `—` | yes | Selects the coding or review CLI. |
| `reviewer.model` | `str` | `—` | yes | Selects the model ID, with an exact claude-* ID required for Claude. |
| `reviewer.reasoning` | `Literal['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']` | `—` | yes | Selects reasoning effort, subject to harness and evaluation validation. |
| `reviewer.timeout_seconds` | `int \| None` | `null` | no | Sets the review timeout in seconds, or inherits the evaluation timeout when null. |
| `reviewer.guidance` | `list[Path]` | `[]` | no | Selects files or directories of review criteria relative to the config file. |
| `repeats` | `int` | `3` | no | Sets the number of attempts for each task and arm. |
| `seed` | `int` | `42` | no | Seeds attempt ordering and identities without guaranteeing deterministic model sampling. |
| `timeout_seconds` | `int` | `1800` | no | Sets the coding agent timeout in seconds, including child work. |
| `jobs` | `int` | `2` | no | Sets concurrent jobs from one to four, subject to Docker capacity. |
| `container_cpus` | `int \| None` | `null` | no | Sets a common CPU limit covering all selected task requirements, or uses their common declaration. |
| `container_memory_mb` | `int \| None` | `null` | no | Sets a common memory limit in MB covering all selected task requirements, or uses their common declaration. |

## Gates file

| Key path | Type | Default | Required | Description |
| --- | --- | --- | --- | --- |
| `count_overlap_passes` | `bool` | `—` | yes | Declares whether native overlap passes count as passes. |
| `splits` | `dict[str, list[str]]` | `{}` | no | Maps split names to task IDs, with all tasks also compared automatically. |
| `gates` | `list[Gate]` | `—` | yes | Lists uniquely named rules for comparing each candidate with its comparator. |
| `gates[].name` | `str` | `—` | yes | Names the gate uniquely within the gates file. |
| `gates[].against` | `str` | `—` | yes | Names the comparator arm as it appears in the comparison report. |
| `gates[].direction` | `Literal['higher', 'lower']` | `—` | yes | Selects whether values must meet a lower ceiling or a higher floor. |
| `gates[].correctness` | `Optional[Literal['passes', 'unknown']]` | `null` | no | Selects pass or unknown counts instead of metric values. |
| `gates[].per_task` | `bool` | `false` | no | Checks correctness separately for each task instead of the aggregate. |
| `gates[].metrics` | `list[str] \| None` | `null` | no | Names report metrics to sum for a metric gate instead of correctness. |
| `gates[].statistic` | `Optional[Literal['total', 'mean_of_task_medians', 'mean_of_task_median_ratios']]` | `null` | no | Selects the point statistic for metric gates without a confidence bound. |
| `gates[].reference_floor` | `float \| None` | `null` | no | Excludes tasks whose reference is below this floor when computing reference ratios. |
| `gates[].tolerance` | `float \| None` | `null` | no | Sets nonnegative additive slack instead of a factor for a point gate. |
| `gates[].factor` | `float \| None` | `null` | no | Sets multiplicative slack for a point gate or the ceiling for a token-ratio bound. |
| `gates[].bound` | `Optional[Literal['lower', 'upper']]` | `null` | no | Selects a paired confidence endpoint for pass-rate differences or input-plus-output token ratios. |
| `gates[].margin` | `float \| None` | `null` | no | Sets the required lower pass-rate difference from minus one to one for a correctness bound. |
