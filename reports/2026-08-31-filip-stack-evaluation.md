# Filip-stack workflow evaluation

**Date:** 2026-08-31
**Status:** Exploratory evaluation report and next-study design

**Follow-up:** [Blinded reviewer-proxy evaluation](2026-09-01-filip-stack-blinded-review-evaluation.md)

## Abstract

We tested whether Filip-stack workflow skills changed Codex performance on eight pinned SWE-bench Verified tasks.

The experiment compared three arms:

1. A baseline without supplied workflow skills.
2. The full Filip-stack workflow bundle.
3. A core bundle with `implementation`, `minimal-code`, and `systematic-debugging`.

Seven tasks produced a strict three-way comparison. The baseline passed two tasks. The full bundle passed one task. The core bundle passed three tasks.

The full bundle used 30.88% more tokens than the baseline. It had 25.37% higher nominal cost. The core bundle used 4.06% more tokens than the baseline.

These results do not prove that the full workflow reduces model capability. Each task had one attempt. One changed binary result came from a narrow SymPy expression mismatch.

The core arm's patch fixed a pytest compatibility defect that both other patches introduced. This result deserves follow-up, but one sample cannot establish skill causation.

The original experiment measured task completion and resource use. It did not measure the workflow's main intended benefit: a better development experience.

The next evaluation should make review effort, correction effort, maintainability, unnecessary scope, and predictability its primary outcomes. Task completion should remain a non-regression guardrail.

## 1. Research question

The first study asked this question:

> Does a workflow skill bundle improve agent task completion enough to justify its token and cost overhead?

That question remains useful, but it is incomplete. The workflow aims to improve the development process around the agent.

The revised primary question is:

> Does the workflow reduce total human effort and improve change quality without materially reducing task completion?

We define the intended benefit as:

```text
Net development benefit
  = avoided review and correction effort
  + improved change quality
  + improved predictability
  - workflow time and token overhead
  - human interaction and cognitive overhead
```

This report treats benchmark pass rate as a capability guardrail. It does not treat pass rate as the complete product outcome.

## 2. What we built and ran

We built `pluginbench`, a local CLI for paired skill evaluations. The final proof-of-concept uses Promptfoo for orchestration and the Codex SDK provider.

The runner supplies an isolated Codex home for each trial. The baseline has no supplied skills. Treatment trials receive only their configured portable `SKILL.md` directories.

The runner records configuration, prompts, model responses, usage, patches, verifier output, and reports. It supports baseline reuse through a compatibility fingerprint.

### 2.1 Evaluation history

The project moved through several bounded checks before the final hard-task comparison.

| Stage | Engine and task | Result | Use in this report |
| --- | --- | --- | --- |
| Promptfoo plumbing check | Local `summarize-gateway-log` task | Baseline and example-skill arms both passed. | Execution and skill-discovery proof only. |
| Workflow smoke set | Three local skill-sensitive tasks | Baseline passed two. Full workflow passed three. | Directional result only. |
| First SWE-bench task | `sympy__sympy-20590` | The final isolated baseline and full arms both passed. | Valid one-task paired result. |
| Hard SWE-bench set | Eight pinned Verified tasks | Seven pairs met the strict timeout contract. | Primary capability result. |
| Core ablation | Same hard set and reused baseline | Core passed three of seven strict tasks. | Exploratory ablation result. |

The POC uses Promptfoo's Codex SDK provider. This boundary supports the current model, subscription authentication, isolated workspaces, usage capture, and saved event traces.

The pivot did not remove benchmark-native grading. Promptfoo executes the agent. The official SWE-bench harness still prepares and grades repository patches.

### 2.2 Local workflow smoke evaluation

The local smoke set contained three deterministic tasks:

| Task | Required work |
| --- | --- |
| `debug-profile-cache` | Diagnose a case-sensitive user ID collision, make the scoped fix, and run tests. |
| `triage-pricing-review` | Reject invalid review advice, preserve exact decimal arithmetic, and add valid percentage checks. |
| `plan-order-idempotency` | Produce an implementation-ready plan from repository evidence without changing code. |

The debugging and review tasks passed in both arms. The planning task failed in the baseline and passed with the full workflow.

Across the combined three tasks, the baseline passed two and the full workflow passed three. The full workflow used 934,985 input tokens against 365,071.

The full workflow's nominal cost was $0.1009 against $0.0280. Its duration was 354.8 seconds against 199.5 seconds.

This smoke set showed that natural skill discovery could change an outcome. It also showed large overhead on small tasks.

The tasks were hand-authored for workflow sensitivity. They were too small and too few for an effectiveness claim.

### 2.3 One-task SWE-bench evaluation

We next added the official SWE-bench verifier. We selected `sympy__sympy-20590` and first passed its gold-patch oracle.

Several diagnostic pairs were excluded while we tightened isolation. One pair could read host-side benchmark artifacts. Another could not start Codex's inner sandbox.

The final valid pair ran one container per model trial. It used immutable evaluator image digests and mounted no sibling arm or grader artifacts.

Both arms passed the official task. The baseline used 855,319 tokens. The full workflow used 1,044,822 tokens.

The full workflow added about 22% more tokens, 12.25% more nominal cost, and 9.2 seconds. It added no task-score lift.

This result proved the isolated benchmark path. One task could not estimate general workflow value.

### 2.4 Hard-task configuration

The evaluation used these fixed components:

| Component | Value |
| --- | --- |
| Dataset | `SWE-bench/SWE-bench_Verified` |
| Dataset revision | `78f471bf655a3137b2e8a75af1501690ec009ec3` |
| SWE-bench harness | `5.0.2` |
| Image platform | `linux/amd64` |
| Promptfoo | `0.122.2` |
| Codex SDK | `0.151.0` |
| Model | `gpt-5.6-luna` |
| Reasoning setting | `high` |
| Attempts | One per task and arm |
| Concurrency | One |
| Trial timeout | 900 seconds |
| Model network access | Disabled |
| Approval policy | Never |

SWE-bench Verified is a human-validated subset of 500 SWE-bench tasks. Each task contains an issue, repository, base commit, and grader contract.

The official harness applies a candidate patch in a task image. It then runs the task tests and reports resolved or unresolved status.

See the official [SWE-bench harness reference](https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md).

The official [SWE-bench FAQ](https://github.com/SWE-bench/SWE-bench/blob/main/docs/faq.md) explains the dataset and evaluation process.

### 2.5 Experimental arms

The baseline exposed no supplied skills.

The full arm exposed the complete Filip-stack workflow skill directory. The agent could discover and invoke those skills naturally. Invocation was not forced.

Here, `full` means that the complete skill bundle was available. Later trace analysis showed that the agent did not fully follow the review-cycle procedure.

The core arm exposed only these three skills:

- `implementation`
- `minimal-code`
- `systematic-debugging`

The core run reused the compatible baseline from the full run. It ran eight new treatment trials.

### 2.6 Cost boundary

Both paid executions stayed below the requested five-dollar limit. Promptfoo estimated the full paired run at $2.2860. It estimated the core treatment at $1.2236.

The combined nominal estimate was about $3.51. These values are provider estimates. They are not subscription invoice charges.

## 3. Tasks

The study used eight fixed SWE-bench Verified tasks from eight repositories.

| Task | Estimated difficulty | Required change |
| --- | --- | --- |
| `pydata__xarray-6992` | More than four hours | Correct index reset behavior when coordinate names and stored variables diverge. |
| `sphinx-doc__sphinx-7590` | More than four hours | Parse C++ user-defined literal suffixes in documented declarations. |
| `sympy__sympy-13878` | More than four hours | Add closed-form cumulative distribution functions where symbolic integration performs poorly. |
| `astropy__astropy-13398` | One to four hours | Add direct ITRS transformations for observed AltAz and HADec coordinates. |
| `django__django-16560` | One to four hours | Add a configurable validation error code to database constraints. |
| `pylint-dev__pylint-4551` | One to four hours | Use Python type hints when pyreverse infers UML attributes. |
| `pytest-dev__pytest-5787` | One to four hours | Preserve chained exceptions during report serialization and deserialization. |
| `scikit-learn__scikit-learn-25102` | One to four hours | Preserve heterogeneous DataFrame dtypes in selectors that do not modify values. |

The [pinned task snapshot](../benchmarks/swe-bench-verified-hard/tasks.json) contains the complete issue text, commits, images, gold patches, and test contracts.

The gold-patch oracle passed all eight tasks. This check confirmed that the local images and verifier pipeline could score the selected tasks.

## 4. Results

### 4.1 Strict three-way comparison

One baseline trial exceeded the configured timeout. Promptfoo reported 1,136.982 seconds of model latency for the scikit-learn task.

We marked that baseline result unscoreable. We excluded the task from all paired and efficiency claims.

| Metric | Baseline | Full bundle | Core bundle |
| --- | ---: | ---: | ---: |
| Tasks passed | 2 of 7 | 1 of 7 | 3 of 7 |
| Pass rate | 28.57% | 14.29% | 42.86% |
| Total tokens | 11,251,938 | 14,726,939 | 11,708,595 |
| Tokens per task | 1,607,420 | 2,103,848 | 1,672,656 |
| Nominal cost | $0.8769 | $1.0994 | $0.9196 |
| Cost per task | $0.1253 | $0.1571 | $0.1314 |
| Duration per task | 460.2 seconds | 471.3 seconds | 526.7 seconds |

### 4.2 Paired differences

The full bundle lost one task against the baseline and gained none. Six task outcomes were unchanged.

Compared with the baseline, the full bundle had:

- A 14.29 percentage-point lower pass rate.
- 30.88% more tokens.
- 25.37% higher nominal cost.
- 11.1 more seconds per task.

The core bundle gained one task against the baseline and lost none. Six task outcomes were unchanged.

Compared with the baseline, the core bundle had:

- A 14.29 percentage-point higher pass rate.
- 4.06% more tokens.
- 4.87% higher nominal cost.
- 66.5 more seconds per task.

Compared with the full bundle, the core bundle passed two more tasks. It used 20.50% fewer tokens and had 16.36% lower nominal cost.

### 4.3 Task outcomes

| Task | Baseline | Full bundle | Core bundle |
| --- | ---: | ---: | ---: |
| `astropy__astropy-13398` | Fail | Fail | Fail |
| `django__django-16560` | Pass | Pass | Pass |
| `pydata__xarray-6992` | Fail | Fail | Fail |
| `pylint-dev__pylint-4551` | Fail | Fail | Fail |
| `pytest-dev__pytest-5787` | Fail | Fail | Pass |
| `sphinx-doc__sphinx-7590` | Fail | Fail | Fail |
| `sympy__sympy-13878` | Pass | Fail | Pass |
| `scikit-learn__scikit-learn-25102` | Unscoreable | Pass | Fail |

The scikit-learn row is an absolute record only. It does not support a paired comparison.

## 5. Analysis of the full-bundle regression

The observed regression is real under the benchmark's binary contract. The evidence does not show a broad collapse in coding ability.

### 5.1 The strict score difference came from SymPy

The baseline and core arms passed the SymPy task. The full arm failed one `FAIL_TO_PASS` test named `test_arcsin`.

All 19 selected SymPy regression tests passed in the full arm.

The expected arcsine CDF used this form:

```python
2 / pi * asin(sqrt((x - a) / (b - a)))
```

The full arm used this form:

```python
S.Half + asin((2 * x - a - b) / (b - a)) / pi
```

These expressions are mathematically equivalent on the distribution support. However, the task test compared the symbolic expression structure.

The full expression did not match the expected structure. That single assertion changed the task from pass to fail.

The local trial could not run the relevant SymPy test because its workspace lacked required test dependencies. The official SWE-bench image later exposed the mismatch.

This evidence supports three conclusions:

1. The binary regression should remain visible.
2. The failure does not demonstrate a generally incorrect implementation.
3. One attempt cannot attribute the expression choice to workflow skills.

The local raw artifact contains the full SymPy patch at
`runs/20260831T215323Z-filip-stack-swe-bench-verified-hard/treatment/verifiers/sympy__sympy-13878/attempt-01/patch.diff`.

The corresponding `test_output.txt` in that verifier directory contains the failed assertion.

### 5.2 Workflow overhead is visible, but causation is not

Across the seven strict tasks, the full bundle used 3,475,001 more tokens than the baseline. This increase is substantial.

Across all eight tasks, the baseline recorded 230 visible command executions. The full arm recorded 457. The core arm recorded 437.

Skill discovery, instruction reading, review steps, and additional checks explain some of these commands. The logs show correlation, not causal impact on correctness.

Patch size does not support a simple over-engineering explanation. Across all eight tasks, the patches contained these changed lines:

| Arm | Files in patches | Added lines | Removed lines |
| --- | ---: | ---: | ---: |
| Baseline | 31 | 762 | 103 |
| Full bundle | 30 | 576 | 133 |
| Core bundle | 35 | 655 | 71 |

These counts include tests. They do not measure whether each line was necessary.

The full bundle did not produce the largest aggregate patch. A blind review must judge unnecessary abstractions and unrelated scope.

### 5.3 Why the core arm performed better in this sample

The core arm recovered the SymPy task by choosing the expression structure expected by the task test. This outcome may reflect run variance.

The core arm also passed the pytest task. This difference reflects a concrete implementation choice.

The baseline and full patches serialized exception chains. Both patches broke `test_deserialization_failure`.

That regression test changes a serialized entry type to `Unknown`. It expects deserialization to raise a `RuntimeError`.

The full patch treated every `ExceptionChainRepr` as a chain. It reconstructed data from a separate chain field and bypassed the modified top-level entry.

The core patch used chain serialization only when the chain contained more than one exception. It preserved existing behavior for single exceptions.

The core patch passed 125 existing tests and both new chained-exception tests. The other two arms passed the new tests but failed one existing test.

This is evidence of better compatibility on that task. It does not prove that the three core skills caused the improvement.

The local full pytest patch is under
`runs/20260831T215323Z-filip-stack-swe-bench-verified-hard/treatment/verifiers/pytest-dev__pytest-5787/attempt-01/patch.diff`.

The local core pytest patch is under
`runs/20260901T003052Z-filip-stack-core-ablation/treatment/verifiers/pytest-dev__pytest-5787/attempt-01/patch.diff`.

The full patch's local `test_output.txt` records the compatibility failure.

### 5.4 What the added review actually did

The full arm read the review-cycle skill and announced review work. However, every full-arm trial lacked the dependencies needed to run its repository test suite.

The agent therefore reviewed syntax, diffs, scope, style, and its own tests or stubs. The official verifier later exercised contracts that this evidence missed.

| Task | Observable review result | Official verifier result |
| --- | --- | --- |
| pytest | The agent claimed legacy reports kept their old path. | An existing deserialization test failed because one-element chains bypassed that path. |
| SymPy | The final review called the scoped diff clean. | `test_arcsin` rejected the chosen expression structure. |
| Astropy | The review accepted the direct transforms and scope. | Four target tests and five regression tests failed. |
| Xarray | The review corrected its new expected dataset. | All 12 target tests still failed. |
| Sphinx | The review fixed the suffix-specific identifier pattern. | The remaining target expression test failed. |
| Pylint | The review improved annotation compatibility and lookup order. | Ten target tests still failed. |

The pytest contrast is especially informative. The full arm's direct stub validated the representation it had just designed.

The core arm instead inspected the repository representation and noticed that ordinary exceptions use one-element `ExceptionChainRepr` values. It preserved the legacy path for that case.

This evidence supports a hypothesis about the missing lift. Independent context and strong behavioral evidence may have made the review more effective.

It mainly rechecked the implementation's existing assumptions. Hidden verifier coverage later exposed assumptions that the same thread had already accepted.

The review was not useless. It caught real local issues in the Sphinx, Xarray, Pylint, and scikit-learn patches.

The benchmark score records only final task resolution. It does not credit useful findings that failed to change the binary outcome.

We also cannot assign all full-bundle overhead to review. The full arm read and applied coordination, Field Guide, debugging, simplification, and other workflow guidance.

The local full and core run directories preserve both pytest Promptfoo traces under `batches/0007`.

### 5.5 Why no independent reviewer ran

The injected `review-cycle/SKILL.md` contract selected one standard reviewer for routine meaningful changes.

The injected `coordinator/SKILL.md` contract assigned that independent role separately from the main thread.

The observable traces show the main thread announcing, conducting, and accepting its own review. They record no reviewer identity, separate reviewer result, or independent review artifact.

This was not an obvious model limitation. A post-run inspection used the exact image digest in the local treatment `arm.json`.

```text
codex-cli 0.151.0
multi_agent                              stable             true
```

The generated local `batches/0007/promptfooconfig.yaml` did not disable multi-agent support.

Current Codex documentation says local releases enable subagents by default. It also says applicable skill instructions can trigger delegation.

See the official [Codex subagents documentation](https://developers.openai.com/codex/subagents).

Two gaps combined:

1. **Evaluation fidelity gap.** Pluginbench copied portable skills, but it did not configure a named standard-reviewer profile or verify independent review evidence.
2. **Filip-stack enforcement gap.** The skill required independence, but it gave no fail-closed outcome when delegation was unavailable or skipped.

The isolated workspace contained no Codex custom agent files under `.codex/agents`. Skill-local `agents/openai.yaml` files provided interface metadata, not reviewer profiles.

Codex supplied built-in agent roles. However, the workflow used the abstract name `standard reviewer` without a concrete spawn instruction.

Pluginbench also treated a skill-file read as skill use. That heuristic proves discovery, not procedure compliance.

Therefore, the result supports two different conclusions:

- As a natural-use test, it exposed a Workflow enforcement gap in this run. The agent did not execute the required independent review.
- As a test of independent review effectiveness, it is inconclusive. The intended review mechanism never produced observable independent evidence.

A stronger evaluation must save the patch before review, require a separate read-only reviewer, and save the reviewed patch afterward.

It must record the reviewer profile, thread, findings, corrections, and verification evidence. It should grade both patch checkpoints.

## 6. What the logs contain

The run artifacts contain more evidence than the generated Markdown reports.

For each trial, Promptfoo saved:

- The complete user prompt.
- Visible assistant messages.
- Commands and captured command output.
- File-change events.
- The final assistant response.
- Input, cached, output, and reasoning token counts.
- A heuristic record of read skill files.
- Provider latency and nominal cost.

The `response.raw` field in each `promptfoo-results.json` file contains this event stream. Its `reasoningTexts` array is empty.

The artifacts do not expose hidden chain-of-thought. We can analyze observable actions and outputs only.

For each SWE-bench verification, the harness saved:

- The submitted patch.
- The evaluation shell script.
- Container execution logs.
- Complete test output.
- A machine-readable test report.
- Passed and failed `FAIL_TO_PASS` tests.
- Passed and failed `PASS_TO_PASS` tests.

### 6.1 Artifact map

The repository excludes raw `runs/` directories because they contain large workspaces,
model traces, and host-specific paths. The published summaries preserve the aggregate results.

| Evidence | Location |
| --- | --- |
| Full strict report | [Published summary](generated/filip-stack-swe-bench-strict.md) |
| Three-way ablation report | [Published summary](generated/filip-stack-core-ablation.md) |
| Gold-patch evidence manifest | [gold-calibration.json](../benchmarks/filip-stack-review/gold-calibration.json) |
| Pinned task definitions | [tasks.json](../benchmarks/swe-bench-verified-hard/tasks.json) |

The original local workspace retains the complete configurations, patches, traces, and grader output under `runs/`.

Each arm directory also contains `batches` and `verifiers` directories. The `batches` files hold agent traces. The `verifiers` files hold patches and grader logs.

## 7. Limits of the evidence

This was an engineering smoke study, not a statistically powered benchmark publication.

The main limits are:

- Seven comparable tasks are too few for a stable effect estimate.
- Each task had one attempt per arm.
- Agent outputs vary between runs.
- The core run started after the full run and reused its baseline.
- Compatibility fingerprints cannot detect provider or model-serving drift between runs.
- One baseline task exceeded the configured timeout.
- Skill calls were detected with a file-read heuristic.
- The full bundle was exposed, but independent-review compliance was not verified.
- The isolated treatment contained no named standard-reviewer file under `.codex/agents`.
- The study did not blind a human reviewer.
- The study did not include human correction loops.
- The study did not include follow-on extension tasks.
- Nominal cost estimates do not represent subscription billing.
- Binary task scores can hide near-correct or contract-specific failures.

The present result should trigger a better experiment. It should not support a claim that the full bundle harms model capability.

## 8. Revised developer-experience evaluation

The next study should treat a complete development episode as the unit of evaluation.

Each episode should contain four phases:

1. The agent implements an initial task.
2. Blind reviewers inspect the patch.
3. A human performs a bounded correction and acceptance loop.
4. The agent completes a related extension task on the accepted change.

The baseline, core, and full arms should receive identical task information, repositories, models, settings, limits, and human decision scripts.

Every arm should receive the same verified subagent capability and registered profiles. This keeps the treatment difference limited to supplied workflow skills.

Natural-use trials should not force reviewer invocation. They should record compliance and reject any claimed independent review without a separate reviewer artifact.

A separate mechanism study can force the same independent review across all arms. It should not be mixed into the natural-use treatment estimate.

### 8.1 Primary outcomes and guardrails

| Goal | Primary measurement | Guardrail |
| --- | --- | --- |
| Less review slop | Action-required blind-review findings, severity, and review minutes | Exclude stylistic preferences. |
| Fewer human corrections | Correction turns and active human minutes before acceptance | Label product decisions separately. |
| Maintainable, reusable code | Extension success, regressions, changed surface, tokens, and findings | Do not use a static score alone. |
| Less over-engineering | Unnecessary abstractions, options, dependencies, layers, and single-use indirection | Do not reward small code by itself. |
| Fewer unnecessary changes | Unrelated files, lines, APIs, behavior, formatting, and dependency changes | Allow required tests and integration work. |
| Capability non-regression | Repeated paired task pass rate | Pre-register an acceptable regression margin. |
| Predictability | Variance in completion, review time, correction time, tokens, and wall time | Report failures and infrastructure errors separately. |

### 8.2 Review rubric

Reviewers should see the issue, repository state, patch, and test evidence. They should not see the arm, skill names, token use, or grader result.

Randomized patch identifiers should replace arm labels. The study should preserve code exactly and remove only evaluation metadata.

When independent review occurs, the evaluator should capture two patch checkpoints. One comes before review, and one comes after accepted corrections.

The reviewer must use a separate read-only context. The artifact must identify its profile, thread, findings, and inspected diff.

Grading both checkpoints will show whether review fixed defects, introduced regressions, or changed only non-scored qualities.

Reviewers should record each action-required finding with:

- Category.
- Severity.
- Affected file and line.
- Required corrective action.
- Estimated correction minutes.
- Confidence.

Useful categories include correctness, compatibility, missing tests, unnecessary scope, maintainability, API change, dependency churn, and formatting churn.

Reviewers should not record personal style preferences unless a repository rule makes them actionable.

Two reviewers should score each patch independently. An adjudicator should resolve disagreements about actionability or severity.

### 8.3 Human correction protocol

The correction phase should use a fixed interface and timer. The human should mark each turn before sending it.

Each turn should have one label:

- `product-decision`: The task lacked information that only a human could supply.
- `agent-correction`: The agent misunderstood evidence or produced a defect.
- `acceptance-check`: The human requested proof required by the study contract.
- `preference-only`: The human requested a non-required stylistic change.

Primary correction metrics should count `agent-correction` turns and active human minutes. Reports should show all labels for transparency.

### 8.4 Follow-on extension task

Each initial task should have a related extension that was not shown during the first phase.

The extension should use the accepted patch as its starting point. It should require reuse or modification of the first design.

Measure:

- Extension task success.
- New regressions.
- Files and lines changed.
- Existing code replaced or bypassed.
- New abstractions or dependencies.
- Agent tokens and wall time.
- Review findings and review minutes.
- Human correction turns and minutes.

This phase provides behavioral evidence about maintainability. It is stronger than a subjective static maintainability score.

### 8.5 Capability guardrail

Run capability tasks with repeated attempts. Use the same attempts for all arms and pair results by task and attempt index.

Randomize arm order within each task and counterbalance that order across attempts. This design reduces time and provider drift as competing explanations.

Three attempts per task are a reasonable minimum for an exploratory follow-up. More attempts improve the estimate but increase cost.

Report task-level outcomes, paired wins, confidence intervals, and failure classes. Separate wrong patches, test regressions, timeouts, and infrastructure errors.

Pre-register a non-regression margin before running the study. The acceptable margin is a product decision, not a value the data should select afterward.

The SymPy case shows why the report needs failure classes. A structural mismatch should remain a failure, but it should not look like broad inability.

### 8.6 Net development benefit

A single composite score needs explicit conversion weights. Those weights depend on the team and should not be invented after seeing results.

The first DX study should report component measures separately. It should also collect the data required for a later composite.

A practical composite can use human-equivalent minutes:

```text
Net benefit minutes
  = avoided blind-review minutes
  + avoided agent-correction minutes
  + avoided expected rework minutes
  - added workflow wall-time cost
  - added human interaction minutes
  - token cost converted to team-equivalent minutes
```

The team should pre-register the token-to-minutes conversion. The report should always retain the unconverted token and cost values.

Improved predictability should remain a separate distribution measure. Hiding variance inside an average would lose important experience information.

## 9. Recommended next studies

### 9.1 Stage 0: retrospective blind review

Start with a zero-model-cost review of the existing patches.

Prepare randomized review packets for the baseline, full, and core patches. Keep the scikit-learn patch, but label its capability result unpaired after unblinding.

Ask two blind reviewers to score action-required findings and review time. This stage directly measures review slop, scope, and over-engineering.

It cannot measure correction effort or extension maintainability. It can test the rubric and reviewer agreement before another paid run.

Expected model trial count: zero.

### 9.2 Stage 1: small interactive DX pilot

Select six tasks with clear acceptance contracts and feasible extension tasks. Use baseline, core, and full arms.

Run one initial attempt per task and arm. Then perform blind review, correction, and extension phases.

The pilot contains 18 evaluation episodes. Each episode has one initial turn and one extension turn.

The minimum exposure is 36 primary-agent calls. A compliant full arm adds 12 reviewer calls, making 48 calls the expected no-correction exposure.

The two blind reviewers are humans and add no provider calls. Cap each episode at one agent correction turn after blind review.

Cap subagent review at one provider call after each agent editing turn. Initial, correction, and extension work can each trigger that review.

The hard ceiling is 36 primary calls, 18 correction calls, and 54 reviewer calls. The total ceiling is 108 provider calls.

Publish estimated token and cost ranges for 36, 48, and 108 calls before execution.

This stage tests study operations. It should not support a final effectiveness claim.

### 9.3 Stage 2: repeated capability guardrail

Run at least three paired attempts on the fixed capability set. Keep model, prompts, limits, images, and verifier contracts identical.

For eight tasks and three arms, the expected trial count is 72. Publish the estimated token and cost exposure before execution.

Use bounded batches. Stop before scheduling a new batch when the best-effort limit is exhausted.

## 10. Decision guidance

The current evidence does not justify removing the full workflow because of task regression. It also does not show that the full workflow creates net value.

The full bundle has a demonstrated token overhead. Its only strict loss came from one exact symbolic assertion in one stochastic attempt.

The core bundle produced the best task score in this sample. It also introduced much less token overhead than the full bundle.

The reasonable current position is:

1. Keep task capability as a hard guardrail.
2. Treat the full-bundle regression as unresolved, not proven causal harm.
3. Evaluate developer experience directly.
4. Use the core bundle as an explicit ablation arm.
5. Start with retrospective blind review because it uses existing evidence and no model calls.

The strongest next result would answer a practical question:

> Would a reviewer and maintainer prefer to receive the full-workflow patch after accounting for review, correction, extension, time, and token costs?

## Appendix A. Existing reports

- [Strict baseline versus full summary](generated/filip-stack-swe-bench-strict.md)
- [Three-way core ablation summary](generated/filip-stack-core-ablation.md)
- [Blinded reviewer-proxy summary](generated/filip-stack-blinded-review.md)

## Appendix B. Reproduction notes

The resolved configuration files contain the exact dataset revision, task IDs, model, reasoning setting, image platform, limits, and skill paths.

The reports preserve the baseline compatibility decision. The core run reused the same baseline rather than generating a new one.

The strict report generator preserved the original pre-timeout report. It then marked the late baseline result unscoreable in the canonical report.

Automated project checks made no model API calls. The repository test suite, lint checks, type checks, package build, doctor command, and dry run passed before this report.
