# Delegation and review

Keep focused or tightly coupled work in the main thread. Delegate substantial independent implementation, discovery, or verification when parallel work, context isolation, or independent judgment justifies the full coordination cost; a cheaper model alone is not a reason to delegate. Continue independent work while agents run.

Before starting a new agent, check whether an existing agent fits the role and responsibility. Reuse it for related follow-up work, corrections, and verification. Start a new agent when the role changes, parallel work requires another agent, independent judgment is needed, or the existing context is unsuitable. Do not use an implementation agent to independently review its own changes.

## Task boundaries

A delegated task must define:

- one outcome
- owned files or a read-only responsibility
- required inputs and known constraints
- an observable completion signal
- actions that remain unauthorized

Use dependency order. Run tasks in parallel only when neither task needs the other's result and their writes cannot overlap. Keep one writer for an overlapping area. Return compact evidence instead of a transcript.

The main thread owns integration, scope, and acceptance. Validate delegated results against source and checks; a worker does not accept its own work.

## Delegated verification

Routine checks belong to the main thread or implementer. For a substantial delegated verification batch, use the implementation profile with a check-only assignment: do not change source, tests, configuration, or documentation; allow required build outputs and caches; return exact commands, outcomes, and relevant failure evidence; do not fix failures.

## Escalation and recovery

Escalate early when uncertainty could change correctness, architecture, public contracts, dependencies, security, ownership, or authority. Pause dependent work while continuing independent investigation that cannot be invalidated by the decision.

Return the evidence, unresolved decision, viable options, a recommendation, and what is blocked. Do not broaden the assignment or assume new authority to resolve the blocker.

When a delegated task fails or cannot meet its completion signal, report the failure and checks that remain unverified. The main thread chooses whether to retry, narrow the task, reassign it, or take over within existing authority. Do not repeat failed attempts without new evidence or a changed approach.

Before claiming completion, account for every required delegated task, integrate its result or complete its replacement, and confirm the required verification. Report unresolved failures and blocked checks as incomplete work, not success.

## Independent review

Scale independent review to risk and honor user or repository requirements:

- Routine changes, meaning small, local, and reversible with no public-contract, data, security, or concurrency impact, use focused checks and main-thread diff inspection.
- Every other change gets an adversarial reviewer on the final diff. When the design is uncertain or hard to reverse, have it critique the plan before editing too.

Advice and planning do not replace required review.

Give the reviewer:

- the requested outcome and non-goals
- the exact diff, branch range, or changed files
- relevant contract and test evidence
- relevant test-value and preservation evidence; include a focused test audit when one was needed
- results from available complexity, dead-code, and duplication analysis, such as `fallow audit`, run after the last edit
- known limits without coaching it toward acceptance

Ask for action-required findings only. Each finding must name the affected location, concrete evidence, impact, and smallest correction.

Review is independent only when a separate reviewer identity or context inspected the actual change. Self-review, an unscoped approval, or a claim without an artifact does not qualify.

Verify reviewer findings against live code and the task contract. Reject style preferences, stale assumptions, and scope expansion. Apply valid findings only with implementation authority, then rerun affected checks. Reuse the same reviewer for correction verification unless a fresh judgment is necessary.

If separate review is unavailable, perform a careful diff inspection and report that independent review did not run.

## Review feedback from the user

Treat supplied feedback as a hypothesis until it matches the live code and contract.

1. Locate the exact code and current behavior.
2. Check the requested correction against callers, tests, and ownership.
3. Explain whether the concern is valid, invalid, already addressed, out of scope, or blocked by missing evidence.
4. Make a correction only when the user authorized edits.
