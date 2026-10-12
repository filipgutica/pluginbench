"""Run untouched, oracle, and three spec-grounded negative controls (no model)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from pluginbench.task_data.deepswe.prepare import TASK, command, suite_root, validate_suite
from pluginbench import isolation


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args()
    generated = args.prepared.resolve()
    entry = json.loads((generated / "tasks.json").read_text())["tasks"][0]
    task = entry["id"]
    suite = suite_root(args.suite, task)
    validate_suite(suite, task)
    fixture = generated / entry["fixture"]
    verifier = generated / entry["verifier"]["script"]
    oracle = suite / "tasks" / task / "solution/solution.patch"
    if task == TASK:
        mutations = {
            "late-threshold": (
                "src/circuit.ts",
                "state.consecutiveFailures >= reservation.options.threshold",
                "state.consecutiveFailures > reservation.options.threshold",
                1,
            ),
            "hostname-only": (
                "src/circuit.ts",
                "return new URL(url).origin;",
                "return new URL(url).hostname;",
                1,
            ),
            "no-cooldown": (
                "src/circuit.ts",
                "state.openedAt + options.cooldown",
                "state.openedAt + 0",
                1,
            ),
        }
    elif task == "happy-dom-abort-pending-body-reads":
        mutations = {
            "request-read-not-cancelled": (
                "packages/happy-dom/src/fetch/Request.ts",
                "void this[PropertySymbol.abortBodyConsumption](error);",
                "void 0;",
                1,
            ),
            "response-wrong-error-name": (
                "packages/happy-dom/src/fetch/Response.ts",
                "DOMExceptionNameEnum.abortError",
                "DOMExceptionNameEnum.invalidStateError",
                2,
            ),
            "multipart-read-not-cancelled": (
                "packages/happy-dom/src/fetch/multipart/MultipartFormDataParser.ts",
                "requestOrResponse[PropertySymbol.abortBodyConsumption] = abort;",
                "requestOrResponse[PropertySymbol.abortBodyConsumption] = null;",
                1,
            ),
        }
    elif task == "testem-per-launcher-reports":
        mutations = {
            "launcher-whitespace-not-sanitized": (
                "lib/launcher.js",
                "replace(/\\s+/g, '_')",
                "replace(/\\s+/g, '-')",
                1,
            ),
            "tap-summary-disabled": (
                "lib/reporters/tap_reporter.js",
                "this.showLauncherSummary = !!config.get('tap_show_launcher_summary');",
                "this.showLauncherSummary = false;",
                1,
            ),
            "xunit-properties-disabled": (
                "lib/reporters/xunit_reporter.js",
                "this.includeLauncherProperties = config.get('xunit_include_launcher_properties');",
                "this.includeLauncherProperties = false;",
                1,
            ),
        }
    elif task == "true-myth-iterable-collection-combinators":
        mutations = {
            "maybe-sequence-failure-lost": (
                "src/maybe.ts",
                "if (m.isNothing) return nothing();",
                "if (m.isNothing) return just(values);",
                2,
            ),
            "result-sequence-error-lost": (
                "src/result.ts",
                "if (r.isErr) return err(r.error);",
                "if (r.isErr) return ok(values);",
                2,
            ),
            "retry-count-ignored": (
                "src/task.ts",
                "return attempt(n);",
                "return attempt(0);",
                1,
            ),
        }
    elif task == "happy-dom-deterministic-intersectionobserver":
        mutations = {
            "thresholds-wrong-order": (
                "packages/happy-dom/src/intersection-observer/utilities/IntersectionObserverThresholdParser.ts",
                "normalizedValues.sort((a, b) => a - b)",
                "normalizedValues.sort((a, b) => b - a)",
                1,
            ),
            "root-margin-not-normalized": (
                "packages/happy-dom/src/intersection-observer/IntersectionObserver.ts",
                "return this.#rootMarginValue;",
                "return '0px';",
                1,
            ),
            "threshold-crossings-disabled": (
                "packages/happy-dom/src/intersection-observer/utilities/IntersectionObserverGeometryUtility.ts",
                "if (crossedFromBelow || crossedFromAbove)",
                "if (false)",
                1,
            ),
        }
    else:
        raise ValueError(f"no calibrated controls for {task}")
    controls = {
        "base": None,
        "oracle": None,
        **mutations,
    }
    receipts = {}
    for name, mutation in controls.items():
        with tempfile.TemporaryDirectory(prefix="pluginbench-ofetch-calibration-") as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(fixture, workspace)
            if name != "base":
                command(["git", "apply", str(oracle)], workspace)
            if mutation:
                filename, needle, replacement, occurrences = mutation
                source = workspace / filename
                content = source.read_text()
                if content.count(needle) != occurrences:
                    raise ValueError(
                        f"negative control no longer targets exactly one expression: {name}"
                    )
                source.write_text(content.replace(needle, replacement))
            before = source_digest(workspace)
            isolation_receipt = Path(temporary) / "runtime-isolation.json"
            execution = isolation.run(
                ["python3", str(verifier)],
                cwd=workspace,
                env={"PATH": os.environ["PATH"]},
                timeout=600,
                readable=isolation.verifier_paths(verifier),
                receipt=isolation_receipt,
            )
            report = json.loads(execution.stdout)
            after = source_digest(workspace)
            receipt = {
                "expected_pass": name == "oracle",
                "exit_code": execution.returncode,
                "source_before": before,
                "source_after": after,
                "source_immutable": before == after,
                "report": report,
                "stderr": execution.stderr,
                "mutation": mutation,
                "filesystem": json.loads(isolation_receipt.read_text()),
            }
            receipts[name] = receipt
            (generated / "calibration.json").write_text(json.dumps(receipts, indent=2) + "\n")
            print(
                f"{name}: pass={report['pass']} f2p={report.get('groups', {}).get('f2p_node_ids')}"[
                    :200
                ]
            )
            if report["pass"] is not receipt["expected_pass"] or before != after:
                raise ValueError(f"calibration expectation failed: {name}")
    print(generated / "calibration.json")


if __name__ == "__main__":
    main()
