# Filip-stack blinded review evaluation

**Date:** 2026-09-01

**Status:** Completed retrospective reviewer-proxy study

**Source study:** [Filip-stack workflow evaluation](2026-08-31-filip-stack-evaluation.md)

## Abstract

This study reused saved patches from seven comparable SWE-bench Verified tasks.

It compared three implementation arms:

1. A baseline without supplied workflow skills.
2. The full Filip-stack workflow bundle.
3. A core bundle with three workflow skills.

Each saved patch received two fresh, blinded reviews. The study completed 42 valid arm reviews without infrastructure failures.

An after-the-fact calibration added 14 reviews of seven blinded SWE-bench gold patches. All 14 calibration calls produced valid results.

The core arm had the best capability result. It passed three tasks, compared with two for baseline and one for full.

The core arm had the fewest arm findings. It had 10 findings, compared with 17 for baseline and 12 for full.

Gold calibration invalidated a simple quality interpretation of those counts. Gold patches received 32 findings and only one accept verdict.

Gold had 2.29 findings per review. Baseline had 1.21, full had 0.86, and core had 0.71.

The reviewer therefore judged contract-passing reference patches more harshly than every experimental arm. Raw reviewer counts do not prove that core produced better code.

The reviewer verdict matched official pass or fail in 25 of 42 reviews. Both reviewers accepted several known failing patches.

The reviewer also requested changes on some passing patches. This included both reviews of the passing baseline SymPy patch.

The study gives evidence about what this reviewer reports. It does not provide a calibrated code-quality or human-effort measure.

The official verifier still supports a limited capability conclusion. Core deserves more study, while full regressed in this small sample.

The reviewer-proxy result cannot support a development-experience ranking without human adjudication.

## 1. Research question

The intended product question is:

> Does the workflow reduce total human effort and improve change quality without materially reducing task completion?

This retrospective study answers a smaller question:

> Do independent reviewers find different action-required problems in saved baseline, full, and core patches?

The study uses an automated reviewer as a proxy. It does not use a human correction loop.

The study can show:

- How often a reviewer requests changes.
- How many action-required findings a reviewer reports.
- Which categories and severities appear.
- Whether repeated reviewers agree.
- How review outcomes align with official task results.
- Whether the implementation arms changed unrelated files or scope.

The study cannot show:

- Actual human review minutes.
- Human correction or steering effort.
- Long-term maintenance cost.
- Follow-on extension success.
- General capability effects from repeated implementation attempts.
- The effectiveness of Filip-stack's internal review procedure.

## 2. Source implementation study

The source study used one implementation attempt per task and arm.

All strict comparisons used these fixed components:

| Component | Value |
| --- | --- |
| Dataset | `SWE-bench/SWE-bench_Verified` |
| Dataset revision | `78f471bf655a3137b2e8a75af1501690ec009ec3` |
| Promptfoo | `0.122.2` |
| Codex SDK | `0.151.0` |
| Model | `gpt-5.6-luna` |
| Reasoning | `high` |
| Attempts | One per task and arm |
| Trial timeout | 900 seconds |
| Concurrency | One |

The baseline exposed no supplied skills.

The full arm exposed the complete Filip-stack workflow skill directory. The agent could discover and use skills without forced invocation.

The core arm exposed these skills:

- `implementation`
- `minimal-code`
- `systematic-debugging`

The strict implementation result was:

| Arm | Tasks passed | Pass rate | Input tokens | Output tokens | Nominal cost | Duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 2 of 7 | 28.57% | 11,133,058 | 118,880 | $0.8769 | 3,221.5 s |
| Full | 1 of 7 | 14.29% | 14,589,241 | 137,698 | $1.0994 | 3,299.0 s |
| Core | 3 of 7 | 42.86% | 11,588,061 | 120,534 | $0.9196 | 3,686.8 s |

The scikit-learn task lacked a strict three-way result. This study preserved it as excluded provenance.

See the [source report](2026-08-31-filip-stack-evaluation.md) for the implementation analysis and trace evidence.

### 2.1 Why the core bundle used three skills

The core bundle was a diagnostic ablation. It was not a claim about the final product bundle.

The selection kept three direct execution controls:

- `implementation` required a bounded change, focused tests, and implementation evidence.
- `minimal-code` constrained scope, abstraction, dependencies, and speculative design.
- `systematic-debugging` required root-cause analysis before a fix.

These skills cover the basic code-change loop. They also map directly to the intended quality guardrails.

The ablation removed coordination, memory, simplification review, and final review-cycle skills. This reduced overlapping procedure and review overhead.

The selection tested one hypothesis. Direct implementation discipline might retain benefits without the full workflow's process cost.

The result cannot assign causation to one selected skill. It can only compare the three-skill bundle with the other complete arms.

## 3. Review study design

### 3.1 Saved inputs

The study reused these saved artifacts:

- The original issue text.
- The pinned repository base commit.
- The generated patch.
- The implementation trace.
- The official verifier result.
- The implementation usage and duration.

PluginBench checked the source fingerprints, task checksums, dataset revision, model identifier, and repository snapshots.

The study selected seven tasks that were scoreable across all three arms. This produced 21 candidate patches.

### 3.2 Blinding

PluginBench assigned each candidate an opaque identifier. It stored the arm mapping under the private run directory.

Each reviewer received only:

- The issue text.
- A clean repository at the pinned base commit.
- One applied candidate patch.
- Standard review instructions.

The reviewer did not receive:

- The arm label.
- Filip-stack or skill files.
- Implementation traces.
- Official verifier results.
- Gold-patch metadata.
- Other candidate patches.

An artifact check found no private arm labels or skill identifiers in reviewer instructions. It found no private candidate map in review workspaces.

### 3.3 Review contract

Each reviewer had to report only action-required findings. The prompt excluded personal style preferences.

Each finding required:

- A category.
- A severity.
- A file and line.
- Concrete evidence.
- The smallest required action.
- A confidence value.

The reviewer returned `accept` only when it found no action-required problem.

The reviewer used a read-only sandbox. It had no workflow skills, network access, or official grader access.

### 3.4 Reviewer configuration

| Component | Value |
| --- | --- |
| Provider | `openai:codex-sdk` |
| Model | `gpt-5.6-luna` |
| Reasoning | `high` |
| Attempts | Two per candidate |
| Review timeout | 900 seconds |
| Concurrency | One |
| Promptfoo | `0.122.2` |
| Codex SDK | `0.151.0` |
| Runtime image | `pluginbench-runtime:0.2.0` |

The study ran 42 review calls. It did not rerun implementation agents or SWE-bench graders.

### 3.5 Gold calibration

The initial arm study disabled the optional gold-patch arm. This omission was not made clear before the run.

PluginBench later added an after-the-fact calibration command. It reused the completed study and scheduled only the missing gold reviews.

The calibration used the same seven tasks, reviewer, model, reasoning, timeout, and tooling. It did not rerun implementation agents or graders.

Each gold patch had passed the local SWE-bench oracle. Gold patches are contract-passing references, not perfect code examples.

The calibration produced seven candidates and 14 reviews. It stored them under the source run's `gold-calibration` directory.

## 4. Execution record

The first valid full run started after four setup failures and one successful gate.

| Run | Result | Model usage |
| --- | --- | ---: |
| `20260901T062334Z` | Git normalized gold-patch metadata during materialization. PluginBench rejected the byte difference. | None |
| `20260901T062625Z` | Docker could not mount temporary Codex auth paths under the macOS temporary directory. | None recorded |
| `20260901T062954Z` | Promptfoo could not resolve local JSON Schema references. The fail-fast guard stopped after three failures. | None recorded |
| `20260901T063300Z` | Codex rejected the invalid `agents.enabled=false` configuration. | None recorded |
| `20260901T063409Z` | One Django baseline review returned a valid structured response. | $0.0967 nominal |
| `20260901T064011Z` | The 42-call experimental-arm study completed. | $3.1894 nominal |
| `20260901T152957Z` | The after-the-fact 14-call gold calibration completed. | $1.3535 nominal |

PluginBench added these fixes before the full study:

- Stable Git patch equivalence for metadata-only patch normalization.
- Docker-visible temporary auth directories under the run parent.
- A stop after three consecutive infrastructure failures.
- Local JSON Schema reference expansion for Promptfoo.
- Removal of the invalid Codex agent configuration.

The arm study ran for 2 hours and 37 minutes. It recorded 42 valid reviews and zero infrastructure failures.

The gold calibration ran for 1 hour and 8 minutes. It recorded 14 valid reviews and zero infrastructure failures.

The total nominal exposure was $4.6397, including the successful gate and calibration. Failed setup attempts recorded no model usage.

## 5. Aggregate results

### 5.1 Reviewer outcomes

| Metric | Baseline | Full | Core |
| --- | ---: | ---: | ---: |
| Candidates | 7 | 7 | 7 |
| Valid reviews | 14 | 14 | 14 |
| Accept verdicts | 6 | 6 | 7 |
| Accept rate | 42.86% | 42.86% | 50.00% |
| Changes-required verdicts | 8 | 8 | 7 |
| Changes-required rate | 57.14% | 57.14% | 50.00% |
| Findings | 17 | 12 | 10 |
| Findings per review | 1.21 | 0.86 | 0.71 |
| Verdict agreement | 100.00% | 100.00% | 85.71% |

Core had the lowest changes-required rate. It also had the fewest findings per review.

Full had the same changes-required rate as baseline. Full had 0.36 fewer findings per review.

Core had 0.50 fewer findings per review than baseline. It had 0.14 fewer findings per review than full.

These differences describe reviewer output. They do not prove a change in code quality.

Gold calibration makes that warning material. Gold had more findings and fewer accepts than every arm.

### 5.2 Severity

| Severity | Baseline | Full | Core |
| --- | ---: | ---: | ---: |
| Blocker | 1 | 0 | 2 |
| Major | 12 | 6 | 6 |
| Minor | 4 | 6 | 2 |

The two core blocker findings describe the same Pylint defect. Both reviewers found that defect independently.

The baseline blocker was a Sphinx parsing defect. The reviewers described different evidence across the two attempts.

Finding counts include repeated reviews. They are not counts of unique defects.

### 5.3 Categories

| Category | Baseline | Full | Core |
| --- | ---: | ---: | ---: |
| Correctness | 11 | 7 | 7 |
| Compatibility | 1 | 1 | 1 |
| Testing | 4 | 1 | 0 |
| API contract | 1 | 1 | 1 |
| Unnecessary scope | 0 | 2 | 1 |
| Maintainability | 0 | 0 | 0 |
| Overengineering | 0 | 0 | 0 |
| Dependency churn | 0 | 0 | 0 |

The reviewer focused on correctness. It produced little evidence about the broader development-experience goals.

The study found three unnecessary-scope findings. All three concerned unrelated Django icon changes in skill-arm patches.

## 6. Task-paired results

This table uses the form `official pass / reviewer accepts / findings`. Each candidate had two reviews.

| Task | Baseline | Full | Core |
| --- | ---: | ---: | ---: |
| Xarray | Fail / 2 / 0 | Fail / 2 / 0 | Fail / 2 / 0 |
| Sphinx | Fail / 0 / 5 | Fail / 0 / 2 | Fail / 0 / 3 |
| SymPy | Pass / 0 / 2 | Fail / 2 / 0 | Pass / 2 / 0 |
| Astropy | Fail / 0 / 7 | Fail / 0 / 3 | Fail / 0 / 2 |
| Django | Pass / 2 / 0 | Pass / 0 / 3 | Pass / 1 / 1 |
| Pylint | Fail / 0 / 3 | Fail / 0 / 4 | Fail / 0 / 4 |
| Pytest | Fail / 2 / 0 | Fail / 2 / 0 | Pass / 2 / 0 |

### 6.1 Xarray

All three Xarray patches failed the official verifier. Both reviewers accepted every patch without a finding.

The implementation logs show that all 12 target tests still failed. The full internal review had corrected its own expected dataset.

The external reviewers also missed this problem. This task is a clear reviewer false-negative case.

### 6.2 Sphinx

All three Sphinx patches failed. Both reviewers requested changes for every patch.

The findings identified parser defects in user-defined literal suffix handling. They also identified missing prefixed and raw string support.

The baseline produced five findings. Full produced two, and core produced three.

The reviewer signal aligned with the official result on this task. It also gave concrete correction steps.

### 6.3 SymPy

The baseline and core patches passed. The full patch failed one strict expression assertion.

Both reviewers requested changes on the passing baseline patch. They found weak tests for new cumulative distribution functions.

Both reviewers accepted the failing full patch. They did not detect its expression-shape mismatch.

Both reviewers also accepted the passing core patch.

This task shows that review quality and benchmark compatibility are different signals. It also shows a reviewer false positive and false negative.

### 6.4 Astropy

All three Astropy patches failed. Both reviewers requested changes for every patch.

The reviewers found missing atmospheric refraction behavior across all arms. They also found route-test, shape, and representation risks.

The baseline produced seven findings. Full produced three, and core produced two.

The reviewers found fewer issues in the skill-arm patches. The official verifier still rejected every patch.

### 6.5 Django

All three Django patches passed the official verifier.

Both in-study reviewers accepted the baseline patch. The separate plumbing gate reviewer requested one major correction on the same patch.

Both full reviewers requested changes. They found unrelated icon symlink conversion and one positional API compatibility concern.

The core reviewers disagreed. One accepted the patch. One found the same unrelated icon conversion.

The icon changes were outside the issue's necessary impact area. This is direct evidence of unnecessary scope in both skill arms.

The gate disagreement also shows reviewer variance. The gate result is not part of the study metrics.

### 6.6 Pylint

All three Pylint patches failed. Both reviewers requested changes for every patch.

The core patch used `AssignAttr.value`. The reviewers found that `AssignAttr` stores the right side on its parent.

Both core reviewers classified this defect as a blocker. The official verifier also rejected the patch.

The baseline and full findings covered class attributes, annotated assignments, forward references, and fallback behavior.

This task produced useful, concrete reviewer evidence across all arms.

### 6.7 Pytest

The baseline and full patches failed one existing compatibility test. The core patch passed.

Both reviewers accepted all three patches without findings.

The baseline and full patches bypassed legacy deserialization behavior for one-element exception chains. The core patch preserved that path.

The reviewer did not detect this distinction. This task is another clear reviewer false-negative case.

## 7. Reviewer calibration

### 7.1 Alignment with official results

The review verdict matched official pass or fail in 25 of 42 reviews. This is 59.52% agreement.

The study contained 12 reviews of passing patches. Seven returned `accept`, for a 58.33% acceptance rate.

The study contained 30 reviews of failing patches. Eighteen requested changes, for a 60.00% detection rate.

Eight candidates had unanimous reviewer outcomes that conflicted with the official result:

- All three failing Xarray patches received two accept verdicts.
- The passing baseline SymPy patch received two changes-required verdicts.
- The failing full SymPy patch received two accept verdicts.
- The passing full Django patch received two changes-required verdicts.
- The failing baseline and full Pytest patches received two accept verdicts each.

This comparison is not a complete reviewer accuracy score. Reviewers can find valid non-grader problems in passing patches.

The comparison still shows material blind spots. The proxy cannot replace the official verifier.

### 7.2 Repeated-review agreement

Baseline and full had 100% verdict agreement across repeated reviews. Core had 85.71% agreement.

High agreement did not guarantee correctness. Reviewers agreed on several wrong accept verdicts.

Repeated reviews improved evidence depth on Sphinx, Astropy, Django, and Pylint. They did not recover the Xarray or Pytest misses.

### 7.3 Gold calibration results

| Metric | Gold |
| --- | ---: |
| Candidates | 7 |
| Valid reviews | 14 |
| Accept verdicts | 1 |
| Accept rate | 7.14% |
| Changes-required verdicts | 13 |
| Findings | 32 |
| Findings per review | 2.29 |
| Major findings | 23 |
| Minor findings | 9 |
| Verdict agreement | 85.71% |
| Infrastructure failures | 0 |

Gold received a lower acceptance rate than baseline, full, and core. Gold also received more findings per review than every arm.

The reviewers agreed on six of seven gold patches. They consistently judged the reference patches more harshly than the experimental patches.

The findings were concrete and mostly about correctness. The calibration reported 27 correctness, three compatibility, and two API-contract findings.

Concrete evidence does not make each finding in scope or correct. A gold patch can pass the benchmark contract and still have broader risks.

The result exposes a rubric-calibration problem. The proxy did not establish a sensible reference level for accepted work.

Raw changes-required and finding counts therefore cannot support arm quality rankings. Human adjudication must separate real defects from out-of-scope concerns.

The calibration does not erase task-level evidence. It weakens aggregate claims that fewer proxy findings mean less review burden.

### 7.4 Why gold scored poorly

| Task | Gold accepts | Gold findings |
| --- | ---: | ---: |
| Xarray | 0 of 2 | 5 |
| Sphinx | 0 of 2 | 4 |
| SymPy | 1 of 2 | 1 |
| Astropy | 0 of 2 | 6 |
| Django | 0 of 2 | 4 |
| Pylint | 0 of 2 | 8 |
| Pytest | 0 of 2 | 4 |

The reviewer had no grader access. It could not separate benchmark requirements from plausible concerns outside the tested contract.

The rubric also asked for all action-required findings. It did not require proof that each concern failed an official assertion.

Many findings extrapolated to edge cases. Examples included C++ literal variants, array index state, constraint APIs, and serialization compatibility.

Some concerns may describe real latent defects. Others may exceed the issue scope or reflect a different design preference.

Gold patches can also be larger or more complete than partial candidate fixes. A larger correct diff can expose more review surface.

The study did not adjudicate each gold finding. It therefore cannot label all 32 findings as false positives.

The supported conclusion is narrower. This reviewer and rubric produce too many gold objections for raw counts to measure review burden reliably.

## 8. What the logs show about Filip-stack review

### 8.1 The full internal review was not independent

The full implementation traces show that the main implementation agent reviewed its own work. The traces contain no separate reviewer identity or artifact.

The review-cycle and coordinator skills described an independent reviewer role. The runtime supported subagents.

The observed gap was procedural. The skill contract did not make delegation an explicit acceptance condition with required evidence.

The main agent therefore rechecked its own assumptions. The official verifier later exposed assumptions that the same thread had accepted.

### 8.2 Independent post-run review changed the evidence

The new reviewer used fresh context and no workflow skills. It reported concrete defects that the implementation traces had missed.

Examples include:

- Sphinx suffix and prefixed-string parser defects.
- Astropy refraction and representation defects.
- Pylint annotation and assignment defects.
- Django unrelated symlink changes.

The reviewer still missed important verifier failures. It also reported more problems on gold patches than on any arm.

Fresh context alone did not solve hidden-contract discovery or reviewer calibration.

### 8.3 Full versus baseline

Full had the same changes-required rate as baseline. Full had five fewer findings across 14 reviews.

Full also passed one task instead of two. It used more implementation tokens and nominal cost.

The lower finding count does not offset the capability result. The reviewer missed known defects in several full patches.

The full Django patch also reached review with unrelated scope changes. Both reviewers found those changes.

This study does not show that the full workflow reduced review burden. Gold calibration prevents a reliable burden comparison from proxy counts alone.

### 8.4 Core versus baseline and full

Core passed three tasks. It also had the lowest changes-required rate and finding count.

Core used 4.06% more implementation tokens than baseline. Full used 30.88% more.

Core has the strongest official capability result in this sample. The calibrated reviewer evidence does not establish a quality advantage.

Core also produced a blocker defect in Pylint. Both reviewers found it.

The aggregate result must not hide this severe task-level failure.

## 9. Developer-experience outcomes

| Goal | Evidence from this study | Result |
| --- | --- | --- |
| Less slop to review | Changes-required verdicts, findings, severity, and review duration | Not established. Gold scored worse than every arm. |
| Fewer human corrections | No human correction loop ran. | Not measured. |
| Maintainable, reusable code | No follow-on extension task ran. Reviewers reported no maintainability findings. | Not measured. |
| Less overengineering | Reviewers reported no overengineering findings. | No useful signal. |
| Fewer unnecessary changes | Reviewers found unrelated Django icon changes in full and core. | Measured on one task. |
| Capability non-regression | Official SWE-bench results remained available. | Full regressed in this sample. Core improved in this sample. |

The primary product question remains unanswered.

The study did not measure total human effort. It measured problems that an automated reviewer reported before correction.

Core has the best task completion result. Its lower proxy burden is not calibrated evidence of better review experience.

Full has the worst task completion result. The proxy cannot establish whether full changed real review burden.

Neither result supports a causal claim. One implementation attempt per task leaves substantial run variance.

## 10. Resource use

### 10.1 Reviewer resources

| Arm | Input tokens | Cached tokens | Uncached input | Output tokens | Nominal cost | Mean duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | 12,364,863 | 11,194,624 | 1,170,239 | 181,159 | $1.2420 | 258.4 s |
| Full | 9,058,655 | 8,122,368 | 936,287 | 139,400 | $0.9363 | 201.1 s |
| Core | 10,063,667 | 9,040,384 | 1,023,283 | 142,796 | $1.0112 | 210.3 s |
| Gold | 13,906,140 | 12,668,416 | 1,237,724 | 205,734 | $1.3535 | 289.9 s |
| Total | 45,393,325 | 41,025,792 | 4,367,533 | 669,089 | $4.5429 | — |

Reviewer duration and tokens are model resource measures. They are not human review minutes.

Baseline reviews used more model resources than skill-arm reviews. Repository and patch differences can explain this result.

The reviewer did not know the arm label. The resource difference therefore reflects candidate and model behavior, not treatment awareness.

Gold reviews used the most tokens and had the highest findings count. This further warns against treating reviewer effort as candidate quality without calibration.

### 10.2 Combined study exposure

The 42-call study cost $3.1894 in nominal provider estimates. The successful gate added $0.0967.

The 14-call gold calibration added $1.3535. The total nominal exposure was $4.6397.

This value is not a subscription invoice charge.

The full run stayed below the configured five-dollar ceiling. One active review could have crossed the limit slightly.

## 11. Evidence limits

The study has these main limits:

- Each implementation arm had one attempt per task.
- Seven tasks cannot represent general software development.
- The tasks are hard benchmark issues, not typical small changes.
- The reviewer used the same model family as the implementation agents.
- The reviewer could not run the official grader.
- Gold patches pass the benchmark contract but are not perfect code references.
- Gold calibration showed a high reviewer objection rate and no human adjudication.
- Reviewer findings do not measure human effort.
- Findings can repeat the same defect across attempts.
- Official pass does not prove maintainability or minimal scope.
- Official failure does not prove every reviewer finding is valid.
- The portable skill bundle does not include every installed plugin capability.

The study also reused saved patches. It did not observe corrections after reviewer feedback.

## 12. Conclusions

The new evaluation adds useful evidence, but it does not answer the full development-experience question.

The strongest supported conclusions are:

1. Core had the best official capability result in this seven-task sample.
2. Full had the worst official capability result in this sample.
3. Independent reviewers reported concrete defects and unnecessary scope that internal review missed.
4. The reviewer proxy missed several official failures and requested changes on passing patches.
5. Gold patches scored worse than every experimental arm on acceptance and findings per review.
6. Raw reviewer counts cannot support a code-quality or development-experience ranking.
7. The full workflow's internal self-review did not provide reliable independent evidence.

The current capability evidence supports more core testing. It does not support adopting the full bundle for measured efficiency gains.

The reviewer evidence needs human adjudication before it can support either arm. The next study should use a small human-reviewed correction loop.

## 13. Recommended next study

Use three tasks with different observed patterns:

1. Django, where all arms passed but skill-arm patches had unrelated scope.
2. Pytest, where core passed and both other arms failed a compatibility contract.
3. SymPy, where benchmark compatibility and reviewer quality diverged.

For each task and arm:

1. Run at least three fresh implementation attempts.
2. Use blinded human reviewers with a fixed rubric.
3. Adjudicate automated and human findings against issue scope and contract evidence.
4. Record action-required findings and severity.
5. Record review and correction minutes.
6. Record steering turns and necessary product decisions separately.
7. Apply corrections in a fresh implementation context.
8. Run the official verifier after corrections.
9. Add a small follow-on extension task.

Keep these outputs separate:

- Capability.
- Review burden.
- Correction burden.
- Scope quality.
- Follow-on success.
- Token, cost, and duration overhead.

Do not combine them into one quality score.

## Appendix A. Artifact map

The repository excludes raw `runs/` directories because they contain large workspaces,
model traces, and host-specific paths. The original local workspace retains those artifacts.

Published review artifacts:

- [Generated Markdown summary](generated/filip-stack-blinded-review.md)
- [Narrative implementation study](2026-08-31-filip-stack-evaluation.md)
- [Gold-patch evidence manifest](../benchmarks/filip-stack-review/gold-calibration.json)
- [Pinned task snapshot](../benchmarks/swe-bench-verified-hard/tasks.json)

Local-only raw artifact roots:

- `runs/20260831T215323Z-filip-stack-swe-bench-verified-hard`
- `runs/20260901T003052Z-filip-stack-core-ablation`
- `runs/reviews/20260901T064011Z-filip-stack-swe-bench-review-arms`

## Appendix B. Verification

The full implementation test suite passed after the calibration changes:

```text
85 passed
```

These checks also passed:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests benchmarks
.venv/bin/ruff format --check src tests benchmarks
.venv/bin/mypy src tests
.venv/bin/python -m build
node_modules/.bin/promptfoo validate -c tests/fixtures/promptfooconfig.yaml
```

PluginBench regenerated both reports without content changes.

| Artifact | SHA-256 |
| --- | --- |
| `review-report.json` | `5b2d30e1ecef06f417574900f5d7fd368ffe792ce535a3e220bdd3a05986710e` |
| `review-report.md` | `4f0b3db853b62774f94434c552b2d466e4e9ff468a3487472a50e0598915e707` |
