# Test-driven development

Use the red and green cycle for new or corrected observable behavior. Use the refactor path for a behavior-preserving change.

## Red

1. Write one focused test for the requested behavior.
2. Name the production change that would make the test fail.
3. Run the narrowest command that executes the test.
4. Confirm that the test fails for the expected missing behavior.

Fix a test error before implementation. If the test passes, it does not prove the new behavior. Change the test or confirm that no implementation change is needed.

Use real behavior. Use a mock only when the dependency boundary makes it necessary. Do not assert only that a mock was called when an observable result exists.

## Green

1. Make the smallest code change that can pass the focused test.
2. Do not add unrequested options, abstractions, or cleanup.
3. Run the focused test again.
4. Fix the implementation before changing a correct test.

Treat existing tests as intended behavior unless repository evidence proves that a contract changed or a test is wrong.

## Behavior-preserving refactor

1. Identify focused existing coverage for the behavior that must not change.
2. Run that coverage and confirm a passing baseline.
3. Make one bounded structural change.
4. Run the focused coverage again.
5. Remove duplication or improve names only when the current change needs it.

If the behavior lacks credible coverage, add a characterization test and confirm that it passes before the refactor. Do not force a failing test for behavior that must stay unchanged.

## Exceptions

A failing automated test may not be credible for:

- prose-only documentation or skill guidance
- generated output whose source owns the change
- mechanical configuration without executable behavior
- a throwaway experiment that the user authorized as such

State the exception before implementation. Use the strongest available replacement, such as schema validation, a dry run, a focused diff, or a deterministic reproduction.

When a bug has no practical automated test, reproduce it before the fix and repeat the same reproduction after the fix. Explain why an automated regression test was not feasible.

## Completion

Run the focused test and each check affected by the change. Do not call the change complete from reasoning alone.
