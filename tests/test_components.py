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
from pluginbench import language_analysis

PINNED: dict[str, tuple[int, dict[str, str]]] = {
    "launcher": (
        1,
        {
            # The optional artifact destination is judge-only; coding launch behavior stays v1.
            "codex.py": "185d0c33e1389051ddfb97a85c08978a6f65773c2378d601cdf608f4b0c17ccf",
            # The Claude launch contract; tests/test_codex_identity.py proves Codex launches equal.
            # Measurement and regrade skip launch-only runtime fields; coding checks are unchanged.
            "harbor_backend.py": "a0279b9c8733f5097fe8160bfac1f7e125a2f82703ede84d8b5c68b6d69b34de",
            "processes.py": "0170e0a4902fc610d19b50ed5656c601474adad97b9b8d6fb5b08c6ab6ad00d1",
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
            "claude_launch.py": "5a2c4eb9e16ef7763fa972241cd3e82101a3f5ae46b7e67e649446aba7b2648a",
        },
    ),
    "analysis_observer": (
        1,
        {
            "language_analysis.py": (
                "df4f76cff23df1e011e5a75bc79c3754d9e8893eb207512cd25f05a664eb8545"
            ),
        },
    ),
    "coverage_observer": (
        1,
        {
            # An optional Go `go list` sidecar narrows completeness to compiled files. A
            # profile that writes none parses as before, so v1-frozen profiles keep v1.
            "coverage_report.py": (
                "a8bf3c15a0f54c1b26e270b9d964312f2e2f594ebdeed27e55344c0c2770ae6d"
            ),
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
        "local_sandbox": {Path(isolation.__file__).name},
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
