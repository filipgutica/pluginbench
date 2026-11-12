# Claude Code 2.1.322 discovery receipts and coding starts

Each arm has the receipt the real discovery runtime wrote (`receipt.json`) and the events the real CLI streamed before its first model request (`session-start.jsonl`), from the same private configuration. `tests/test_claude_launch.py` uses them.

They were captured offline on 2026-11-12 with a fake subscription token, so no model turn ran. The coding run used the pinned launch with `--input-format stream-json` and stopped at `system/init`.

| Arm | Treatment |
| --- | --- |
| `vanilla` | None |
| `treated` | The `treatment()` of `tests/test_claude_launch.py`: guidance, a loose skill, and a plugin with a skill and a SessionStart `startup` hook |

Paths are replaced with the container's: `/tmp/pluginbench-treatments` (staged treatment), `/logs/agent/sessions` (`CLAUDE_CONFIG_DIR`), `/app` (task source) and `/SCRATCH` (anything else).
