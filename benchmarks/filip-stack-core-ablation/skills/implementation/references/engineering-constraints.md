# Engineering constraints

Apply the constraints that match the changed surface.

## Tests

- Treat existing tests as intended behavior. Fix the implementation before changing a correct test.
- Change tests only when the behavior or contract changed, a fixture or mock must reflect real behavior, or repository evidence proves the test is wrong.
- Do not weaken assertions, remove edge cases, blindly update snapshots, or hide failures.
- Add deterministic regression coverage for a bug fix when feasible.

## Contracts and types

- Preserve public APIs, exported types, and externally visible behavior unless the user authorized a contract change.
- When a contract must change, update its call sites and tests, explain migration impact, and prefer backward compatibility.
- Avoid `any`, unsafe casts, and type-error suppression. Use narrowing, type guards, generics, or correct inference so types match runtime behavior.
- Do not add local type shims or compatibility aliases to hide dependency or version mismatches. Identify and report the upstream cause.

## Dependencies and toolchains

- Add a dependency only when existing tools are insufficient. State why it is needed and prefer a small maintained package.
- Never edit a lockfile by hand. Change the manifest and regenerate the lockfile with the repository's package manager.
- Use the package manager and version declared by the repository, including the root `packageManager` field when present.

## Errors

- Do not swallow errors or remove error handling to make tests pass.
- When intentionally ignoring an error, make the reason explicit.
