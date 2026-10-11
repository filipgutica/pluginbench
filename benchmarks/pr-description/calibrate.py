"""Exercise the prepared PR verifier without model calls."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

GOOD_DRAFT = """## Description
Start the devps picker in browse mode so normal navigation does not edit the search filter.

## Changes
- Use `/` to search; Enter keeps the filter and Esc clears it before returning to browse mode.
- Keep action shortcuts available in both modes and fall back to the table with unsupported fzf versions.

## Verification
1. On macOS with Node 22+ and fzf 0.66+, check out `bcfda8a340ab17e277b541f85d7e612b30f05d77`, run `npm ci` and `npm run build`, and start two disposable local dev servers. Run `node dist/cli.js` in a terminal. Confirm the picker starts in browse mode with its search input hidden.
2. Press `/`, enter a project filter and press Enter. Confirm browsing resumes, the filtered rows remain and the header shows the active filter.
3. Press `/` again and then Esc. Confirm the filter clears and all rows return. Press q from browse mode to quit.
"""


def load_verifier(path: Path):
    spec = importlib.util.spec_from_file_location("prepared_pr_verifier", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calibrate(catalog: Path) -> dict[str, object]:
    task = json.loads(catalog.read_text())["tasks"][0]
    verifier = load_verifier(catalog.parent / task["verifier"]["script"])
    fixture = catalog.parent / task["fixture"]
    cases = {
        "compliant": (GOOD_DRAFT, None),
        "wrong-headings": (
            GOOD_DRAFT.replace("## Changes", "## Implementation"),
            "required_headings",
        ),
        "ci-echo": (
            GOOD_DRAFT
            + "\nAll checks passed. `npm test`, `npm run typecheck`, and `npm run smoke` passed.\n",
            "no_routine_ci_echo",
        ),
        "fabricated-local": (
            GOOD_DRAFT + "\nI ran the picker and verified its behavior locally.\n",
            "no_claimed_local_execution",
        ),
        "placeholder": (
            GOOD_DRAFT.replace("Start the devps", "TODO: Start the devps"),
            "no_placeholders",
        ),
        "source-edit": (GOOD_DRAFT, "captured_inputs_unchanged"),
    }
    results = {}
    for name, (draft, failed_criterion) in cases.items():
        with tempfile.TemporaryDirectory(prefix="pluginbench-pr-calibration-") as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(fixture, workspace)
            (workspace / "PR.md").write_text(draft)
            if name == "source-edit":
                (workspace / "context.md").write_text("altered")
            result = verifier.evaluate(workspace)
            flat = {
                key: value for group in result["criteria"].values() for key, value in group.items()
            }
            valid = (
                result["pass"]
                if failed_criterion is None
                else result["pass"] is False and flat[failed_criterion] is False
            )
            results[name] = {
                "expected_failure": failed_criterion,
                "calibration_pass": valid,
                "result": result,
            }
    return {"pass": all(case["calibration_pass"] for case in results.values()), "cases": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    args = parser.parse_args()
    result = calibrate(args.catalog.resolve())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["pass"] else 1)
