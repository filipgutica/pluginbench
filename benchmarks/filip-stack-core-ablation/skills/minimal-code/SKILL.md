---
name: minimal-code
description: "Use when work involves coding, debugging, refactoring, test changes, documentation edits, generated-code cleanup, utility extraction, or dependency decisions."
---

# Minimal code

Use this skill as the default standard for implementation work. Reduce code, ceremony, abstractions, dependencies, and unnecessary explanation without reducing correctness.

Prefer the smallest correct, readable change. Add complexity only when the current task shows that simpler options are insufficient.

This skill expands the global engineering principles in AGENTS.md and CLAUDE.md.

It does not replace user instructions, task-specific skills, debugging, tests, or the review cycle.

## Decision order

Apply these decisions in order:

1. **Do nothing when no change is needed.** Explain briefly and stop.
2. **Reuse local code.** Prefer existing helpers, components, scripts, config, and patterns over new surfaces.
3. **Use the platform.** Prefer language, runtime, browser, framework, and standard-library features before custom code.
4. **Avoid dependencies.** Do not add a dependency unless existing tools are insufficient and the benefit justifies the maintenance cost.
5. **Keep the shape direct.** Prefer one clear function, branch, or data transform over factories, registries, adapters, or configuration layers.
6. **Write the smallest causal fix.** Test the smallest causal behavior change first. Keep the fix understandable, testable, and correct.

## Implementation rules

- Make the narrowest change that satisfies the user's goal.
- For meaningful code work, consult relevant guidance through `$workflow:field-guide` when available.
- Apply current instructions and repository contracts before field-guide guidance.
- Prefer existing source state and declarative or framework-native invalidation.
- Add mirrored state or timing coordination only when focused evidence shows that direct approaches fail.
- Avoid future-proofing, broad configurability, generic abstractions, and extra extension points unless the current task proves they are needed.
- Prefer deleting or reusing code over adding code when behavior stays correct.
- Keep prose concise: state what changed, how it was verified, and any real limitation. Do not include feature tours or generic explanations.
- Preserve existing public contracts unless the user explicitly asks to change them.
- Do not hide failures, weaken assertions, skip meaningful validation, or remove error handling to make the diff smaller.
- After a behavior-preserving refactor, normalize data and control flow. Do not retain transitional variants without a current need.
- Name stable domain contracts at their producer. Use `ReturnType` or `NonNullable` only for local, incidental shapes. Do not reconstruct producer-owned contracts at consumers.
- Put cancellation and freshness checks at the stateful or externally visible boundary they protect. Add earlier guards only before expensive, stateful, or unsafe work.
- Simplify or reorder prerequisites and guards before you explain unusual sequencing. Comments must explain unavoidable constraints, not preserve avoidable structure.

## Utility comments

Add JSDoc to shared, exported, or non-obvious utilities when it explains intent that the signature does not show.

Use JSDoc to clarify:

- why the utility exists
- non-obvious inputs or return values
- important edge cases, invariants, or failure behavior

Do not add JSDoc to trivial local helpers where the name and types already explain the behavior. Avoid comments that restate the implementation.

## Safety boundaries

Minimal code must not reduce:

- correctness or data integrity
- type safety and runtime validation
- security checks and trust-boundary handling
- accessibility requirements
- meaningful tests for non-trivial behavior
- explicit user requirements
- clear errors and useful failure modes

If the smallest implementation conflicts with these boundaries, choose the smallest implementation that keeps them intact.

## Related workflow skills

- Use `$workflow:coordinator` to route planning and implementation.
- Use this skill as the default implementation lens inside the coordinator's Implementation flow.
- Use `$workflow:simplification-review` to audit minimality, reuse, ownership, complexity, and cleanup.
- Use `$workflow:review-cycle` as the final acceptance gate after meaningful edits.
