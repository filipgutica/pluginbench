"""Declared versions of PluginBench's own components, and the source hashes saved runs hold.

Saved runs and caches bind each component by its declared version, not by its source bytes,
so a fix that leaves a component's behaviour unchanged keeps them usable. The hashes are
still saved as provenance. `tests/test_components.py` pins each file's current hash to its
component's version: changing a file fails that test until someone decides whether the
change needs a new version.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

VERSIONS: dict[str, int] = {
    "launcher": 1,
    "analysis_observer": 1,
    "coverage_observer": 1,
    # Version 2 denies writes to / and /tmp in the Linux sandbox (b05b459).
    "local_sandbox": 2,
}

# The source files each component's version stands for. These are the only hashed files.
FILES: dict[str, tuple[str, ...]] = {
    # How a coding attempt is launched. Session parsing lives in codex_events.py, outside
    # this list, so a parser-only change keeps every vanilla cache entry. claude.py counts
    # only when Claude is the harness.
    "launcher": (
        "codex.py",
        "harbor_backend.py",
        "processes.py",
        "native_treatments.py",
        "native_treatment_runtime.js",
        "treatments.py",
        "claude.py",
    ),
    "analysis_observer": ("language_analysis.py",),
    "coverage_observer": ("coverage_report.py",),
    "local_sandbox": ("isolation.py",),
}

# Every content each file had on main from 942f12c up to 34386c5, before versions were
# saved. Runs and caches from that range record only these hashes. This table is fixed:
# later changes are saved with their version and never added here.
_SAVED_HASHES: dict[str, dict[str, int]] = {
    "codex.py": {"6b1d892b37ea5e33337a469923762fe71130e22c840859771367cf34fa66b21f": 1},
    "harbor_backend.py": {"066f9a3641d85ae8d109357b545e82973d4eb8a7bb7c5fbed4eed29c96df62b8": 1},
    "processes.py": {"0170e0a4902fc610d19b50ed5656c601474adad97b9b8d6fb5b08c6ab6ad00d1": 1},
    "native_treatments.py": {"dcb11553bc9bd2ea7a15ff3293d3ec74617565f89c3c2d156f714acade8b73a8": 1},
    "native_treatment_runtime.js": {
        "229d01b78fd893b8e502139507b74d18f0147079c58f15f90b0548b5520e67c5": 1
    },
    "treatments.py": {"e807503ac1809dd1030a6805702fe23da0d4cd07eb2d9f52d12069ae85f1926f": 1},
    "claude.py": {"87951a73f68414d49253815480c683695e586b0c205adb6cbdb61101e887abd7": 1},
    "language_analysis.py": {
        # 942f12c, then the Rust dep-info fix (959d5f6, 9474664): same results where the
        # earlier file returned one.
        "bc09200c57bf78d403d9f5b473d16af5ea653059dc06559ae3c86ba4652b9744": 1,
        "9d3e0281732cf43504eee765d32a95d73909dfc202ce0528a910403f6e5bec2e": 1,
        "d80e0593e2e33acdf415b36bc5a24ff8e096281fa5b6d8d3d3ef5c8a7a29f03a": 1,
    },
    "coverage_report.py": {"1a097f128f99b6ce57b23d903a52a199061a00039f7662767d8e6777821186cc": 1},
    "isolation.py": {
        "cee7b89e2ce9887733a2d6cda2010885b2c6e9c388939c110b3592bb8f193135": 1,
        "992f724c3c12dbd8b1950261edd980f88b677ccd962d73782ef4241ba0114984": 2,
    },
}


class UnknownSourceError(ValueError):
    """A saved run records source bytes that no PluginBench version declares."""


class ObserverError(ValueError):
    """A measurement profile cannot be measured by the observer this build runs."""


# What to do with a measurement profile that another observer version froze.
OBSERVER_NEXT_STEP = (
    "measure it with the PluginBench build that froze it, or prepare a new measurement revision"
)


def source_sha256(name: str) -> str:
    return hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()


def launcher_files(harness: str) -> tuple[str, ...]:
    """The launcher files a coding run hashes: claude.py only when Claude is the harness."""
    return tuple(name for name in FILES["launcher"] if name != "claude.py" or harness == "claude")


def saved_launcher_version(harness: object, hashes: object) -> int:
    """The version of a saved launcher hash set, which must name every file of its harness."""
    expected = launcher_files(harness) if harness in ("codex", "claude") else ()
    saved = sorted(hashes) if isinstance(hashes, Mapping) else []
    if not expected or saved != sorted(expected):
        raise UnknownSourceError(
            f"the saved PluginBench launcher hashes {', '.join(saved) or 'no file'}, not the "
            f"files of a {harness} launcher"
        )
    assert isinstance(hashes, Mapping)
    return saved_version("launcher", hashes)


def saved_version(component: str, hashes: object) -> int:
    """The version a run saved before versions existed used, read from its source hashes."""
    label = component.replace("_", " ")
    if not isinstance(hashes, Mapping):
        raise UnknownSourceError(f"the saved PluginBench {label} source hashes are not a mapping")
    if not hashes:
        raise UnknownSourceError(f"the saved PluginBench {label} records no source hash")
    versions = set()
    for name, digest in sorted(hashes.items()):
        version = (
            _SAVED_HASHES.get(name, {}).get(digest)
            if name in FILES[component] and isinstance(digest, str)
            else None
        )
        if version is None:
            raise UnknownSourceError(
                f"{name} has sha256 {str(digest)[:12]}, which no PluginBench version declares "
                f"for its {label}; a build this version cannot vouch for saved it"
            )
        versions.add(version)
    if len(versions) > 1:
        raise UnknownSourceError(f"the saved PluginBench {label} mixes source versions")
    return versions.pop()
