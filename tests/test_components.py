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
            "harbor_backend.py": "70b32fd5198c99e76d2b8751d40bc854eb193555f68b954658b1697da43992b8",
            "processes.py": "0170e0a4902fc610d19b50ed5656c601474adad97b9b8d6fb5b08c6ab6ad00d1",
            # Claude admission dispatch only; Codex admission is unchanged.
            "native_treatments.py": "cfe17310a7f43de42af556e98fe63380493aeaf6a419ad612d51e01d671f0f19",
            "native_treatment_runtime.js": (
                "229d01b78fd893b8e502139507b74d18f0147079c58f15f90b0548b5520e67c5"
            ),
            # Claude plugin admission rules; Codex admission and every admitted launch stay v1.
            "treatments.py": "68bc11023a601c7bcf2ababdbb5007f0515eb1c64e9a1194862cfa939541ebab",
            # Claude version admission and coding launch contract; Claude-only, so Codex stays v1.
            "claude.py": "7010fe7d5242a43138c63a09af1cb73b63f4ccc0a44a87732024791395ecdfab",
            "claude_treatments.py": "15d5d5de89f34e2b1b6982bb0f1eb1fbbcf28be2b08586500e489d2db55e2aa5",
            "claude_treatment_runtime.js": (
                "61ac207ea357796c3ed935cac8b3d1574e0c562a7651cade586aad7e5cc5731b"
            ),
            "claude_launch.py": "f06f63ca474b69e4e1db9722a96a796754f633d75676230e6fe71f541b507462",
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
            "coverage_report.py": (
                "1a097f128f99b6ce57b23d903a52a199061a00039f7662767d8e6777821186cc"
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
