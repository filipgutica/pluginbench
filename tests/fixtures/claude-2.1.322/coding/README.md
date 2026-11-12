# Claude Code 2.1.322 coding fixtures

Each case has the stream (`stream.jsonl`) and the session directory (`sessions/`, the CLI's `CLAUDE_CONFIG_DIR`) of one coding run. `tests/test_claude_usage.py` uses them.

The cases come from a probe on 2026-11-12 with CLI 2.1.322 and `claude-opus-5-5`. They keep:

- the event structure
- the models
- the usage numbers

They replace the following:

- Prompt, response and tool text become `<text>`.
- Session, message, tool-call and agent ids become synthetic ids. The links between files stay intact.
- Cost is removed.

| Case | Source | Status |
| --- | --- | --- |
| `plain` | One prompt with no tool calls | Calibrated: transcripts equal `modelUsage` |
| `one-subagent` | One prompt and one `Agent` call | Calibrated: transcripts equal `modelUsage` |
| `two-parallel` | One prompt and two parallel `Agent` calls | Calibrated: transcripts equal `modelUsage` |
| `compaction` | **Derived.** The probe ran a prompt and then a second `/compact` turn. That gave two inits and two results. This case drops the second init and the first result, so it has a coding attempt's one-prompt shape. | Not calibrated |

No single-prompt auto-compaction has been observed. The validation run will check:

- that it leaves one result
- that it writes a `compact_boundary` to both the stream and the root transcript
