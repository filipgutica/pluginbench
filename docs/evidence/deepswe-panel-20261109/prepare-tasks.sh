#!/usr/bin/env bash
# Reproduce the pinned task payloads without writing PluginBench's shared cache.
set -euo pipefail
umask 022
manifest=$(cd -- "$(dirname -- "$0")" && pwd)/manifest.json
out=${1:?Usage: prepare-tasks.sh <new output directory>}
[ ! -e "$out" ] || { echo "Output already exists: $out" >&2; exit 1; }
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
git -c gc.auto=0 clone -q --filter=blob:none --no-checkout https://github.com/datacurve-ai/deep-swe "$scratch/deep-swe"
python3 - "$manifest" "$scratch/deep-swe" "$out" <<'PY'
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tomllib

manifest, source, out = map(pathlib.Path, sys.argv[1:])
spec = json.loads(manifest.read_text())
names = [row["task"] for row in spec["tasks"]]
subprocess.run(
    ["git", "-C", str(source), "-c", "gc.auto=0", "checkout", "-q",
     spec["dataset_commit"], "--", *["tasks/" + name for name in names]],
    check=True,
)
identity = '''[environment.env]
GIT_CONFIG_COUNT = "2"
GIT_CONFIG_KEY_0 = "user.name"
GIT_CONFIG_VALUE_0 = "Task Developer"
GIT_CONFIG_KEY_1 = "user.email"
GIT_CONFIG_VALUE_1 = "developer@example.invalid"
'''
for row in spec["tasks"]:
    name = row["task"]
    task = out / name
    shutil.copytree(source / "tasks" / name, task)
    for relative, expected in row["verifier_files"].items():
        file = task / "tests" / relative
        assert hashlib.sha256(file.read_bytes()).hexdigest() == expected["sha256"], file
        assert oct(file.stat().st_mode & 0o777) == expected["mode"], file
    path = task / "task.toml"
    text = path.read_text()
    assert tomllib.loads(text)["environment"].get("env") == {}, name
    assert text.count("[environment.env]\n") == 1, name
    path.write_text(text.replace("[environment.env]\n", identity))
print(f"Prepared {len(names)} candidates in {out}")
PY
