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
            "codex.py": "6b1d892b37ea5e33337a469923762fe71130e22c840859771367cf34fa66b21f",
            "harbor_backend.py": "53b0e6cb6488e1d9adda935ad71b19aead7da71b2e87063fd6e69dc3646860cb",
            "processes.py": "0170e0a4902fc610d19b50ed5656c601474adad97b9b8d6fb5b08c6ab6ad00d1",
            "native_treatments.py": "dcb11553bc9bd2ea7a15ff3293d3ec74617565f89c3c2d156f714acade8b73a8",
            "native_treatment_runtime.js": (
                "229d01b78fd893b8e502139507b74d18f0147079c58f15f90b0548b5520e67c5"
            ),
            "treatments.py": "e807503ac1809dd1030a6805702fe23da0d4cd07eb2d9f52d12069ae85f1926f",
            "claude.py": "87951a73f68414d49253815480c683695e586b0c205adb6cbdb61101e887abd7",
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
        2,
        {"isolation.py": "992f724c3c12dbd8b1950261edd980f88b677ccd962d73782ef4241ba0114984"},
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
