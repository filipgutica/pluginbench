---
name: engineering
description: Use for an authorized code change, bug fix, refactor, failing test, CI failure, or review correction that needs the smallest correct implementation and focused verification. Do not use for explanation-only requests or planning without edits.
---

# Engineering

Deliver the smallest authorized change and prove the requested contract.

## Work the change

1. Inspect Git state, the requested behavior, the supplied diff, nearby implementations, the production owner, and the nearest existing test.
2. Identify the owner, boundary, and verification signal before editing. For nontrivial work, state a plan and check it against a simpler alternative. Do nothing when no change is needed.
3. Run the nearest existing test that observes the requested contract when suitable coverage exists, and record the baseline.
4. Fix the production owner before changing a correct existing test.
5. Change an existing test only when repository evidence proves that its contract changed or the test is wrong.
6. Add focused coverage for a credible regression that existing tests would miss. Confirm that a new bug regression test fails on the pre-fix code for the expected reason. For reversible, low-impact changes, prefer existing checks over tests that mirror the implementation.
7. Make the smallest causal change. Avoid speculative options, abstractions, dependencies, and adjacent cleanup.
8. Rerun the same test and only the affected existing checks. Repeat or broaden verification when the changed code, an observed failure, or an unresolved risk justifies it.
9. Inspect the final diff and affected callers for accidental changes, duplication, dead code, redundant state or branches, needless indirection, and test value. Simplify changed code if useful, preserving behavior; rerun affected checks after edits. Stop when the contract passes and required checks and review gates pass.

## Keep the design small

Prefer existing local code, standard library or native framework features, and installed dependencies before bespoke code. Choose the simplest maintainable option matching the contract and repository conventions.

Keep one clear owner per responsibility. Fix behavior at that owner, preserve established boundaries, and extend existing seams. Add helpers or modules for current reuse, clearer ownership, or simpler code. Share duplicated knowledge without coupling unrelated responsibilities merely because their code looks similar.

Minimality must not remove correctness, data integrity, type safety, runtime validation, error handling, security, accessibility, or meaningful tests.

Surface a material ambiguity before it changes the solution.
Stop when the work needs new authority or a material user decision.

## Check test value

For ordinary test edits, consider:

- the observable behavior, invariant, or independent contract protected
- a credible regression that would fail the test
- the distinct risk that existing coverage misses
- whether the test exercises the owning boundary without unnecessary exposure of internals

Prefer extending existing cases over duplicating proof. Another test layer can protect a distinct risk. Dependency injection for clocks, network, or storage can support deterministic tests at real boundaries.

Before removing coverage, establish the failure it detects and the stronger proof that remains, or why its contract is obsolete. Preserve correct assertions rather than weakening them to pass.

## Load only what the route needs

Read [testing and debugging](references/testing-and-debugging.md) when reproduction, regression coverage, or refactor verification is uncertain. Use credible repository checks for mechanical or prose-only changes.

Read [verification tools](references/verification-tools.md) only when repository checks cannot cover the affected contract.

Read [hygiene](references/commit-and-pr-hygiene.md) before subtasks, commits, PRs.

Invoke $workflow:test-audit for an explicit test audit or cleanup, substantial coverage changes, or uncertainty about test value or preservation. Bound it to the affected tests and owners; routine test edits use the questions above.

Read [delegation and review](references/delegation-and-review.md) for delegation or any change beyond a routine bounded edit. Follow its review requirements and any user or repository requirements. Routine bounded changes can use focused checks and final diff inspection.

Do not use review as a substitute for a blocked or failed test.

## Finish

Map every completion claim to an exact command result or bounded direct evidence. Record the reviewed change range and any version, seed, or environment detail needed to reproduce a result. Do not report unavailable evidence as passed.

Report results, checks, and remaining risk after [first-reader review](../technical-writing/references/technical-prose.md#first-reader-review).
Do not describe self-review as independent review.
Do not commit, push, publish, deploy, or modify external work without user authority.
