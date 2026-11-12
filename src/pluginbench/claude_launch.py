"""Launch a Claude coding attempt through Harbor's ClaudeCode agent under the pinned contract.

Harbor's agent builds the coding command and its environment. PluginBench wraps the agent's
`exec_as_agent`, as it wraps Codex's: it accepts only the exact command Harbor builds for the
run, runs the treatment's discovery with the coding command's own environment, and then runs
the argv from `claude.coding_launch` in place of Harbor's flags, with stderr kept apart.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pluginbench import claude, claude_treatments
from pluginbench.native_treatments import NativeCapabilities
from pluginbench.treatments import Treatment

TRANSPORT_VERSION = "claude-pinned-launch-v1"
_HARBOR_VERSION = "0.24.0"
# ClaudeCode.run in Harbor 0.24.0. The contract matches the command this source builds.
_HARBOR_RUN_SHA256 = "a1cf6f922478cac2c8501fafc8575a924e726bd213f44d787034c0be81e2703f"
# A command that starts the CLI: `claude` as a word, not a path such as ~/.claude/skills.
_INVOKES_CLI = re.compile(r"(?:^|[\s;|&(])claude(?:\s|$)")
# Codex limits Claude Code ignores (council C3). A recipe that sets one is refused, and a
# Claude run records them as not enforced instead of numbers it never used.
UNENFORCED_LIMITS = ("max_threads", "max_depth")
NOT_ENFORCED = "not_enforced"


def transport() -> dict[str, str]:
    """The pinned Harbor run source, for the runtime identity of a Claude run."""
    if importlib.metadata.version("harbor") != _HARBOR_VERSION:
        raise ValueError("Claude transport requires the pinned Harbor version")
    agent = importlib.import_module("harbor.agents.installed.claude_code").ClaudeCode
    digest = hashlib.sha256(inspect.getsource(agent.run).encode()).hexdigest()
    if digest != _HARBOR_RUN_SHA256:
        raise ValueError("Harbor ClaudeCode.run changed; refusing the Claude launch contract")
    return {"version": TRANSPORT_VERSION, "harbor_run_sha256": digest}


def refuse_unenforced_limits(capabilities: NativeCapabilities) -> None:
    """Refuse a Claude recipe that sets a limit Claude would silently ignore.

    Call it on the recipe as written: a saved recipe lists every default.
    """
    declared = sorted(set(UNENFORCED_LIMITS) & capabilities.model_fields_set)
    if declared:
        raise ValueError(
            f"Claude Code does not enforce {', '.join(declared)}; remove them from capabilities"
        )


def capabilities(subagents: bool) -> dict[str, object]:
    """What a Claude run records of the capability policy."""
    return {"subagents": subagents, **dict.fromkeys(UNENFORCED_LIMITS, NOT_ENFORCED)}


def pin_exec(agent: object, boundary: dict[str, Any], *, qualification: str) -> None:
    """Replace the agent's `exec_as_agent` with the contract.

    Every command is refused when its environment carries a refused variable, so an API key
    stops the attempt before Harbor's setup runs. `qualification` is the version guard, the
    only other command allowed to start the CLI.
    """
    original: Callable[..., Awaitable[object]] = getattr(agent, "exec_as_agent")
    run = claude.CodingRun(**boundary["coding_run"])
    coded = False

    async def pinned_exec(
        environment: object,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> object:
        nonlocal coded
        # What the environment will actually run with: its own, this call's and Harbor's
        # scoped agent env, which outranks both.
        merged = dict(getattr(environment, "_merge_env")(env) or {})
        claude.refuse_env(merged)
        if not _INVOKES_CLI.search(command) or command == qualification:
            return await original(environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec)
        if coded:
            raise ValueError("a Claude attempt runs one coding command; use a fresh attempt")
        coded = True
        pinned = claude.contract_command(command, run)
        claude.check_coding_env(merged, run)
        # Discovery and coding get this same dict, so discovery sees what coding will.
        await claude_treatments.install(
            environment=environment,
            treatment=Treatment.model_validate(boundary["treatment"]),
            env=merged,
            cwd=str(boundary["image"]["workdir"]),
            artifacts=Path(str(boundary["area"])) / "artifacts",
            run=run,
            cli_version=str(boundary["version"]),
        )
        return await original(environment, pinned, env=merged, cwd=cwd, timeout_sec=timeout_sec)

    setattr(agent, "exec_as_agent", pinned_exec)
