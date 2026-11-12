"""A stand-in `claude` that performs plugin installs on disk and replays discovery answers.

FAKE_CLAUDE_SCENARIO names a JSON file with:
- responses: the control response body for each request subtype;
- stream: events written before the first response, such as hook events;
- omit: request subtypes it answers by exiting instead;
- install_root: where plugin installs go, instead of $CLAUDE_CONFIG_DIR/plugins/cache;
- list_overrides: fields merged into every `plugin list --json` entry.
Each invocation's arguments are appended to FAKE_CLAUDE_LOG; `plugin` commands warn on stderr.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

scenario = json.loads(Path(os.environ["FAKE_CLAUDE_SCENARIO"]).read_text())
plugins = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins"
records_file = plugins / "installed_plugins.json"
markets_file = plugins / "known_marketplaces.json"
args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["plugin"]:
    print("fake claude CLI warning", file=sys.stderr)


def records() -> dict[str, list[dict[str, object]]]:
    return json.loads(records_file.read_text())["plugins"] if records_file.exists() else {}


if args[:3] == ["plugin", "marketplace", "add"]:
    source = Path(args[3])
    name = json.loads((source / ".claude-plugin/marketplace.json").read_text())["name"]
    plugins.mkdir(parents=True, exist_ok=True)
    markets_file.write_text(
        json.dumps({name: {"source": {"source": "directory", "path": str(source)}}})
    )
    print(f"Adding marketplace {name}")
    print(json.dumps({"ok": True, "marketplace": name}))
elif args[:2] == ["plugin", "install"]:
    plugin_id = args[2]
    name, market = plugin_id.split("@")
    source = Path(json.loads(markets_file.read_text())[market]["source"]["path"]) / "plugins" / name
    target = Path(scenario.get("install_root", plugins / "cache")) / market / name / "1.0.0"
    shutil.copytree(source, target)
    entry = {
        "scope": "user",
        "installPath": str(target),
        "version": "1.0.0",
        "installedAt": "2026-11-12T16:49:19.714Z",
        "lastUpdated": "2026-11-12T16:49:19.714Z",
    }
    records_file.write_text(
        json.dumps({"version": 2, "plugins": {**records(), plugin_id: [entry]}})
    )
    print(json.dumps({"ok": True, "plugin": plugin_id}))
elif args[:2] == ["plugin", "list"]:
    print(
        json.dumps(
            [
                {
                    "id": plugin_id,
                    "version": entry["version"],
                    "scope": entry["scope"],
                    "enabled": True,
                    "installPath": entry["installPath"],
                    **scenario.get("list_overrides", {}),
                }
                for plugin_id, entries in records().items()
                for entry in entries
            ],
            indent=2,
        )
    )
else:
    for event in scenario.get("stream", []):
        print(json.dumps(event), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        subtype = request["request"]["subtype"]
        if subtype in scenario.get("omit", []):
            sys.exit(0)
        print(
            json.dumps(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request["request_id"],
                        "response": scenario["responses"][subtype],
                    },
                }
            ),
            flush=True,
        )
