"""Each hashed source file is pinned to its component's declared version.

When a pin fails, decide whether the change alters what the component does. If it does, raise
the component's version in `components.VERSIONS`: saved runs of the old version are then
refused where they would mix two behaviours. If it does not, keep the version. Either way,
update the hash below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pluginbench import collectors, components, evaluation, harbor_backend, isolation
from pluginbench import language_analysis, mutation, measurement_tools

PINNED: dict[str, tuple[int, dict[str, str]]] = {
    "launcher": (
        1,
        {
            # The optional artifact destination is judge-only; coding launch behavior stays v1.
            # The optional spawn callback reports the reviewer's PID to the invocation ledger;
            # no launch changes.
            # The reviewer's failed cleanup is told to the host lock owner; launches are
            # unchanged.
            "codex.py": "9017df04931b2817beeb281f6153d3820d3461e3fa5ed17563fdd274ef390ff6",
            # The Claude launch contract; tests/test_codex_identity.py proves Codex launches equal.
            # Measurement and regrade skip launch-only runtime fields; coding checks are unchanged.
            # Audited command hooks keep native usage; with no audited_hooks every launch is
            # unchanged, and vanilla has no hooks, so this stays v1.
            # Artifact normalization drops only redundant children and preserves collected
            # payload bytes and bindings; coding launch behavior stays v1.
            # The optional spawn callback only reports the worker's PID to the invocation
            # ledger; what is launched, and how, is unchanged, so this stays v1.
            # The daemon check takes an optional timeout, which only cleanup after a crash
            # passes; launches are unchanged. A failed trial cleanup now also tells the host
            # lock owner, which records it for the next owner to check.
            # Inside a scoped operation, its containers carry the invocation label, a build
            # uses the engine's built-in builder, and only the Docker commands PluginBench
            # issues run; none changes a launch, and outside an operation nothing changes.
            # A native task that sets Docker controls (DOCKER_*, BUILDX_*, BUILDKIT_*,
            # COMPOSE_*) in its environment is now refused; every task still admitted launches
            # exactly as before, so this stays v1.
            # Containers, builds and trials now start only inside an operation, on the daemon
            # it recorded; a launch inside one is unchanged, so this stays v1.
            # Optional measurement provisioning leaves coding launches unchanged (v1).
            "harbor_backend.py": "69e113f4ce3e91a98a782352684fc9e24e8f614304eb17159950cce215fc7330",
            # The same spawn callback; launches are unchanged. A failed process-group cleanup
            # or output drain now also tells the host lock owner, which records it for the next
            # owner to check. Launches are unchanged.
            "processes.py": "a86b3c57bd280f01d5f0c9dbc0676186d9c0e1d2bc07e2e6aa2cd2564291ea66",
            # Claude admission dispatch, and the Codex config's V2 spawn-override hide. The
            # vanilla cache refuses a cohort saved without the hide by its config, so this stays v1.
            "native_treatments.py": "3c9a617c292e0fbc93bc07b46e012ab79bd5589efc6a3e9267e4652c71ba89b3",
            # Admits a hook whose command Codex registers with its plugin-root placeholders
            # expanded. Such hooks were always refused, so every admitted launch stays v1.
            "native_treatment_runtime.js": (
                "ca133df5f2d24e0a869e85ee063bdffd61cbb75be54112fb3a0a90fec1940a88"
            ),
            # Claude plugin admission rules; Codex admission and every admitted launch stay v1.
            # Claude plugin agents admit, and Claude plugin skills may set `paths`; a Codex
            # inventory and an agent-free Claude one are unchanged.
            "treatments.py": "7b3f9e19379b716c12c18ed393dc44b3ba240d5efdf8d551a7bba5e67e4097e0",
            # Claude version admission, coding launch contract and the subscription-only refusal
            # every Claude launch shares; a token-only launch is unchanged, so this stays v1.
            # The forced subagent model joins the coding env, which the Claude vanilla
            # identity binds by value, so the launcher stays v1 and Codex is untouched.
            # The unqualified-version warning names plugin-skill `paths`: text only.
            "claude.py": "551a620c3645f727102458dbe3fcda4b8a15c60585fad03b81b908326265c4f6",
            "claude_treatments.py": "0149a2dd1c0f0db6f2e29b030885bacbad2e0569e43cdf45c25764181bc9ea97",
            # Discovery proves plugin agents exactly; an agent-free receipt is unchanged and
            # verifies as before unless an agent loads, so the launcher stays v1.
            "claude_treatment_runtime.js": (
                "3fb19c0a255775fe1df4f6bb04db4dba0615be016bb67d6109fc5db15e26a656"
            ),
            # The launch check expects the receipt's plugin agents beside the built-ins.
            # A startup hook with a statusMessage is matched by it; such attempts always failed
            # before, and a hook-free vanilla launch is unchanged, so the launcher stays v1.
            "claude_launch.py": "7fd4bf8ee86bb27b9ba67cde210dee6f0212ff3bf65f4aec28be397a7cc1ca01",
        },
    ),
    "analysis_observer": (
        2,
        {
            # prepare_go_observer also builds mutation's standard-library site finder, which
            # ships no go.sum. The analysis observer's files, cache and binary are unchanged.
            # v2: rust-code-analysis files the parser rejects are listed in a complexity
            # partition instead of making complexity unavailable for the whole repository, so a
            # profile v1 froze would now report complexity it reported unavailable.
            # v2 also partitions Go dead code by the host build. Version 2 is unreleased, so it
            # carries both partitions.
            "language_analysis.py": (
                "0c3a0a4b5c34e2e85265ed83255f8c44135a0cfa5d31784e96b72ff64ea0baac"
            ),
        },
    ),
    "coverage_observer": (
        2,
        {
            # Optional Go `go list` and `go tool cover` sidecars narrow completeness to
            # compiled files with blocks, and a zero-width Go block with no statements parses
            # instead of aborting. An optional LCOV instrumented-counts sidecar likewise lets
            # files Jest's istanbul found nothing in, such as TypeScript type modules, leave
            # completeness. A profile that writes no sidecar and was not refused parses as
            # before, so this stays v1.
            # coverage.py 7.2.x JSON, which has no meta.format, reads as format 2. Such reports
            # were always refused, and every other report parses as before, so this stays v1.
            # Blocks Go names after a `//line` target that is no frozen Go file count for the
            # one file naming it instead of aborting. Only refused reports change: stays v1.
            # v2: Go reports add a `lines` population, which leaves out blank and comment-only
            # lines and any file a line directive is in or names. A profile v1 froze would now
            # report Go line coverage and compare executed lines across readiness repeats.
            "coverage_report.py": (
                "7915e6742224d11a7994c3bac44042581085b44928a96ea0d95a937429796bf0"
            ),
        },
    ),
    "mutation_observer": (
        1,
        {
            # Only report derivation changes; collection, sites, sampling and outcomes stay v1.
            # Optional provisioning runs previously unsupported profiles; existing runs stay v1.
            "mutation.py": "1daa2608157db47654b293c5ec74ed175b8cfa3c6d7042820d339cfb865e69cd",
            "mutation_sites.mjs": (
                "f7a602774f3e6f2415c1745127134862a9a6f42e8e9807b4c28368e7a3c27c88"
            ),
            "observer_data/go-mutation-v1/main.go": (
                "ee3f96b8d281dc135ccfe706aadc250444ff686af0acfc10339510504c8e6eca"
            ),
        },
    ),
    "measurement_tools": (
        4,
        {
            # v4 reports in fresh containers, validates maps, and disables exclusion hints.
            "measurement_tools.py": "57d5bffdd4badcbbe9d37f44a149679e5de02ae321eb429ca5d73665bccc16bf",
            "coverage_tools/launch-coverage.py": "297461d2032e34f917392c9eba9b45c9b08769c496565fb37ad989ba86a39cfa",
            "coverage_tools/launch-c8.cjs": "4f2507a5afc9d92685c166856612f5c47ffa81f188b14d7f0283188424d88b2f",
            "coverage_tools/c8-config.json": "2ee2372e0af85263890e3e052453e08a091d47225ffa7678ca8a6c963fcbb177",
            "coverage_tools/coverage-config.ini": "1ff3036ad9b40af6c1b0c195c4cb5b87ab2f308a9ac4287c58778639f13ba2a4",
        },
    ),
    "local_sandbox": (
        3,
        {"isolation.py": "482538e42f57cc5eb3ccd60bd9d10ed9c690863cb111a0d7518420bccc2b8bd4"},
    ),
}


@pytest.mark.parametrize("component", sorted(components.VERSIONS))
def test_each_hashed_file_is_pinned_to_its_component_version(component: str) -> None:
    version, hashes = PINNED[component]
    # A file dropped from the declared list would stop being hashed without anyone deciding.
    assert sorted(components.FILES[component]) == sorted(hashes)
    actual = {name: components.source_sha256(name) for name in components.FILES[component]}
    changed = sorted(name for name in actual if actual[name] != hashes.get(name))
    assert not changed, (
        f"{', '.join(changed)} changed without a version decision for the {component}; "
        "see this file's docstring"
    )
    assert components.VERSIONS[component] == version


def test_the_pinned_files_are_the_files_the_code_hashes() -> None:
    hashed = {
        "launcher": set(evaluation._launcher_sha256("claude")),
        "analysis_observer": {Path(language_analysis.__file__).name},
        "coverage_observer": set(collectors.observer_identity()),
        "mutation_observer": set(mutation.observer_identity()),
        "local_sandbox": {Path(isolation.__file__).name},
        "measurement_tools": {
            Path(measurement_tools.__file__).name,
            "coverage_tools/launch-coverage.py", "coverage_tools/launch-c8.cjs",
            "coverage_tools/c8-config.json", "coverage_tools/coverage-config.ini",
        },
    }
    assert {name: set(files) for name, files in components.FILES.items()} == hashed
    assert Path(harbor_backend.__file__).name in hashed["launcher"]
    assert set(PINNED) == set(components.VERSIONS) == set(components.FILES)


CODEX_LAUNCHER = (
    "codex.py", "harbor_backend.py", "processes.py", "native_treatments.py",
    "native_treatment_runtime.js", "treatments.py",
)


def test_claude_only_launcher_files_stay_out_of_the_codex_launcher() -> None:
    # Codex runs and caches bind exactly these files, so a Claude-only change never moves them.
    assert components.launcher_files("codex") == CODEX_LAUNCHER
    assert set(evaluation._launcher_sha256("codex")) == set(CODEX_LAUNCHER)
    assert components.launcher_files("claude") == components.FILES["launcher"]


def test_a_launcher_saved_before_versions_keeps_its_version() -> None:
    # Those runs predate the later Claude-only files, so their sets never name them.
    legacy = {name: next(iter(components._SAVED_HASHES[name])) for name in CODEX_LAUNCHER}
    assert components.saved_launcher_version("codex", legacy) == 1
    claude = {**legacy, "claude.py": next(iter(components._SAVED_HASHES["claude.py"]))}
    assert components.saved_launcher_version("claude", claude) == 1


def test_an_unknown_saved_hash_is_refused_and_names_the_file() -> None:
    known = next(iter(components._SAVED_HASHES["harbor_backend.py"]))
    with pytest.raises(components.UnknownSourceError, match="codex.py has sha256 ffffffffffff"):
        components.saved_version("launcher", {"harbor_backend.py": known, "codex.py": "f" * 64})


@pytest.mark.parametrize(
    ("digest", "version"),
    [
        ("cee7b89e2ce9887733a2d6cda2010885b2c6e9c388939c110b3592bb8f193135", 1),
        ("992f724c3c12dbd8b1950261edd980f88b677ccd962d73782ef4241ba0114984", 2),
    ],
)
def test_the_sandbox_write_boundary_change_is_its_own_version(digest: str, version: int) -> None:
    # b05b459 made / and /tmp read-only in the Linux sandbox: a behaviour change.
    assert components.saved_version("local_sandbox", {"isolation.py": digest}) == version
