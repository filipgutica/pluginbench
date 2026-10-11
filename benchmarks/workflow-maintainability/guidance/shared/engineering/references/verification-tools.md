# Verification tools

Select checks from the changed contract. Use repository-declared tools before optional tools. Do not install tools automatically.

| Concern | Preferred evidence |
| --- | --- |
| Behavior | Focused test, then the affected suite |
| Types and contracts | Repository compiler or type checker |
| Local rules | Existing ESLint, Oxlint, Biome, Ruff, Clippy, or equivalent command |
| Formatting | Existing formatter in check mode |
| Build or packaging | The smallest affected build or package validation |
| Coverage | Existing coverage command and changed-line coverage when configured |
| Architecture | Existing dependency rules, import rules, ArchUnit, dependency-cruiser, or Fallow rules |
| Security | Existing CodeQL, Semgrep, or project security checks |
| Dependency risk | Existing SCA or OSV-based check |
| Secrets | Existing gitleaks or equivalent check |

Run focused checks first. Add a broad suite only when the change affects shared behavior, public contracts, packaging, or integration boundaries.

## Fallow

Use Fallow only for JavaScript or TypeScript repositories when it is already available or the user requests it. It complements tests, types, and lint. It does not replace them.

For changed-code risk:

```sh
fallow audit --base <base-ref> --format json --quiet --explain
```

Exit code 0 means the audit produced no fail verdict; warnings or findings can
still be present. Exit code 1 means the audit produced a fail verdict. Inspect
the JSON findings in both cases. Preserve every other exit code as a tool
failure.

Use focused commands when the question is narrower:

```sh
fallow dead-code --file <path> --format json --quiet
fallow dupes --changed-since <base-ref> --format json --quiet
fallow health --complexity --format json --quiet
fallow guard <changed-files> --format json --quiet
```

Treat findings as evidence to inspect. Dynamic plugin entry points, framework discovery, generated imports, and external consumers can produce incomplete reachability evidence. Do not delete a file, export, or dependency until its callers, manifests, runtime loading, and public contract are checked.

Use similar-code results only to find candidates. Inspect both implementations and their callers before deciding that they have the same behavior or can be consolidated.

## Verification record

For each command, record:

- the contract it checks
- whether it passed, failed, or could not run
- the relevant failure, without copying noisy output
- replacement evidence for a skipped or unavailable check

Do not report a blocked, rejected, timed-out, or unavailable check as passed.
