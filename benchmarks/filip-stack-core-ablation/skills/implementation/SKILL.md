---
name: implementation
description: "Use for an explicitly authorized feature, bug fix, refactor, or behavior change that needs the smallest correct implementation and focused test evidence."
---

# Implementation

Implement one bounded change with evidence. The coordinator owns authority, scope, delegation, review, commits, and publishing.

## Workflow

1. Confirm implementation authority and the bounded outcome.
2. Confirm the active slice for meaningful work.
3. Read the relevant code, tests, contracts, repository guidance, and [engineering constraints](references/engineering-constraints.md).
4. For new or corrected behavior, select the smallest targeted test that observes the requested outcome.
5. For a behavior-preserving refactor, run focused existing coverage before the change.
6. Apply the correct path in [test-driven-development.md](references/test-driven-development.md).
7. Use `$workflow:minimal-code` for the implementation.
8. Run the focused test and checks affected by the change.
9. Review the implementation against the requested outcome before independent review.

## Active slice boundary

Stop and return the boundary to the coordinator when the work requires:

- an independent conceptual change
- a change to the commit or verification boundary
- a material product or architecture decision

Defer adjacent cleanup, new abstractions, and follow-up work that the active slice does not require. Do not expand the slice silently.

Do not widen scope, change a public contract, add a dependency, or change unrelated tests without explicit need and authority.

## Evidence

Record the items that apply:

- the observed failing test or reproduction for new or corrected behavior
- why that failure represents the requested behavior
- the passing pre-change baseline for a behavior-preserving refactor
- the focused passing result after implementation
- any broader check required by the changed surface
- any test-first exception and its replacement evidence

This skill does not grant commit, branch, push, pull-request, or publishing authority.

Read [upstream.md](references/upstream.md) for source pins and local adaptation decisions.
