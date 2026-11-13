# Claude Code 2.1.322 discovery receipts and coding starts

Each arm has three files from the same private configuration:

- `receipt.json`: the receipt the real discovery runtime wrote.
- `session-start.jsonl`: the events the real CLI streamed up to `system/init`.
- `transcript-hooks.jsonl`: the hook entries of the root transcript, with each attachment's `content`, `stdout` and `stderr` removed.

`tests/test_claude_launch.py` uses them.

They were captured offline on 2026-11-12 with a fake subscription token and a dead proxy, so no model turn ran. The token was in the CLI's credentials file only, as `claude.credentials` writes it, and not in the environment. The coding run used the pinned launch (`bypassPermissions`, no `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`) and stopped after its first failed model request.

| Arm | Treatment |
| --- | --- |
| `vanilla` | None |
| `treated` | The `treatment()` of `tests/test_claude_launch.py`: guidance, a loose skill, and a plugin with a skill and a SessionStart `startup` hook |
| `superpowers` | `treated` with Superpowers' hook matcher, `startup\|clear\|compact`. The CLI still names the event `SessionStart:startup`. |
| `two-hooks` | `treated` with two SessionStart hooks: `${CLAUDE_PLUGIN_ROOT}/hooks/a.sh` (`startup`) and `echo B` (`startup\|clear`) |

The stream names a hook run only by event and trigger. The transcript names it by its command as declared, with `${CLAUDE_PLUGIN_ROOT}` unexpanded, and by its exit code. Discovery lists the same command text.

Paths are replaced with the container's: `/tmp/pluginbench-treatments` (staged treatment), `/logs/agent/sessions` (`CLAUDE_CONFIG_DIR`), `/app` (task source) and `/SCRATCH` (anything else).

`../init-tools.json` holds the `system/init` tools with subagents on. `offline` comes from these captures. `signed_in` comes from the paid probe and adds WebFetch.
