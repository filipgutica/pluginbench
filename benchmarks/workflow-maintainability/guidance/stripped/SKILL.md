---
name: engineering
description: Use for an authorized code change, bug fix, refactor, failing test, CI failure, or review correction that needs the smallest correct implementation and focused verification. Do not use for explanation-only requests or planning without edits.
---

# Engineering

Deliver the smallest authorized change and prove the requested contract.

Inspect Git state, nearby implementations, the production owner, affected callers, and existing tests. State material assumptions. For nontrivial work, state a plan and challenge it with a simpler alternative before editing. Do nothing when no change is needed.

Run a relevant existing test as the baseline. Fix the owning production boundary; change correct tests only when evidence establishes a changed contract. Add coverage only for a credible regression existing tests miss. A new bug regression test must fail on the old code for the expected reason.

Reuse existing local code, native features, and installed dependencies. Avoid speculative abstraction, configurability, redundant state, duplicated knowledge, and adjacent cleanup. Preserve behavior, public contracts, types, validation, error handling, security, accessibility, and meaningful tests.

Rerun affected checks. Inspect the final diff and callers for accidental changes, duplication, dead code, needless indirection, and test value. Simplify within scope and rerun checks after edits. Stop when the contract and required checks and reviews pass.

Read [testing](references/testing-and-debugging.md) when verification is uncertain, [verification tools](references/verification-tools.md) when repository checks are insufficient, [hygiene](references/commit-and-pr-hygiene.md) before subtasks or publication, and [delegation and review](references/delegation-and-review.md) for delegation or more than routine bounded edits. Follow required independent review. Invoke $workflow:test-audit when test value or preservation is uncertain.

Report exact evidence and incomplete checks after [first-reader review](../technical-writing/references/technical-prose.md#first-reader-review). Stop for new authority or material user decisions. Do not commit, push, publish, deploy, or modify external work without user authority.
