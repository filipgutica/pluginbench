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
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pluginbench import claude, claude_treatments
from pluginbench.native_treatments import REMOTE_ROOT, NativeCapabilities
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


def recorded_capabilities(policy: NativeCapabilities, harness: str) -> dict[str, object]:
    """A run's capability policy as its plans and comparison identity record it."""
    if harness == "claude":
        return capabilities(policy.subagents)
    return policy.model_dump(mode="json")


def validated_launch(execution: dict[str, Any], model: str, effort: str) -> bool:
    """Whether a coding attempt ran the pinned Claude argv for this model and effort.

    Claude does not report its effective effort, so this binds the requested one to the launch.
    """
    launch = execution.get("launch")
    if not isinstance(launch, dict) or execution.get("actual_primary_models") != [model]:
        return False
    policy = launch.get("capabilities")
    subagents = policy.get("subagents") if isinstance(policy, dict) else None
    if effort not in claude.EFFORTS or not isinstance(subagents, bool):
        return False
    # The timeout sets only the environment, so any value gives the same argv.
    argv = claude.coding_launch(claude.CodingRun(model, effort, subagents, 0))[0]
    return bool(
        launch.get("harness") == "claude-cli"
        and launch.get("delivery") == claude_treatments.DELIVERY_POLICY
        and launch.get("requested_model") == model
        and launch.get("requested_effort") == effort
        and launch.get("harness_version")
        and launch.get("argv") == argv
        and execution.get("runtime_effort_evidence") in ([], [effort])
    )


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
        receipt = await claude_treatments.install(
            environment=environment,
            treatment=Treatment.model_validate(boundary["treatment"]),
            env=merged,
            cwd=str(boundary["image"]["workdir"]),
            artifacts=Path(str(boundary["area"])) / "artifacts",
            run=run,
            cli_version=str(boundary["version"]),
        )
        result = await original(environment, pinned, env=merged, cwd=cwd, timeout_sec=timeout_sec)
        trees = _treatment_trees(receipt)
        if trees:
            # Skills and plugins load their files on use, so they must last the whole run.
            report = await original(
                environment,
                "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                f"node {REMOTE_ROOT}/install.cjs {REMOTE_ROOT} "
                f"{shlex.quote(str(boundary['image']['workdir']))} --trees "
                + shlex.join(path for path, _ in trees),
                env=merged,
                cwd=cwd,
                timeout_sec=120,
            )
            observed = json.loads(str(getattr(report, "stdout", "")))
            for (path, entries), now in zip(trees, observed, strict=True):
                if _by_path(now) != _by_path(entries):
                    raise ValueError(f"Claude treatment files changed during the coding run: {path}")
        return result

    setattr(agent, "exec_as_agent", pinned_exec)


def _treatment_trees(receipt: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Each delivered tree the CLI reads during coding, with its frozen inventory entries."""
    inventory = receipt["inventory"]
    plugins = {plugin["name"]: plugin["entries"] for plugin in inventory["plugins"]}
    return [
        *(
            (install["loaded_from"], plugins[install["id"].partition("@")[0]])
            for install in receipt["plugin_installs"]
        ),
        *(
            (f"{claude_treatments.CONFIG_DIR}/skills/{skill['directory']}", skill["entries"])
            for skill in inventory["skills"]
        ),
    ]


def _by_path(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: str(entry["path"]))


def session_errors(
    stream: str, receipt: dict[str, Any], *, model: str, subagents: bool
) -> list[str]:
    """How a coding session differs from the discovery receipt it was launched after (D2).

    Its `system/init` must report what discovery found, and its hook events must be the
    listed hooks: each SessionStart hook that matches `startup` starts once and exits 0
    before the first assistant message, and no other hook event appears.
    """
    events = [
        event
        for line in stream.splitlines()
        if line.startswith("{") and isinstance(event := _json(line), dict)
    ]
    inits = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    if len(inits) != 1:
        return ["the coding stream needs exactly one system/init"]
    init = inits[0]
    try:
        builtin = set(receipt["builtins"]["commands"])
        skills = {skill["name"] for skill in receipt["skills"]}
        plugins = sorted((item["id"], item["loaded_from"]) for item in receipt["plugin_installs"])
        listing = [(hook["event"], hook.get("matcher") or "") for hook in receipt["hooks"]["hooks"]]
        expected = {
            "claude_code_version": receipt["cli_version"],
            "model": model,
            # Discovery saw a subscription token and no API key.
            "apiKeySource": "none" if "apiKeySource" not in receipt["account"] else None,
            "mcp_servers": [],
            "agents": receipt["builtins"]["agents"],
            "slash_commands": sorted(builtin | skills),
            "skills": sorted(skills),
            "plugins": plugins,
        }
    except (KeyError, TypeError) as error:
        return [f"the discovery receipt lacks {error}"]
    observed = {
        **{key: init.get(key) for key in ("claude_code_version", "model", "apiKeySource")},
        "mcp_servers": init.get("mcp_servers"),
        "agents": _sorted(init.get("agents")),
        "slash_commands": _sorted(init.get("slash_commands")),
        # Built-in skills are built-in commands too; the rest must be the declared skills.
        "skills": sorted(set(_sorted(init.get("skills"))) - builtin),
        "plugins": sorted(
            (plugin.get("source"), plugin.get("path"))
            for plugin in init.get("plugins") or []
            if isinstance(plugin, dict) and plugin.get("path") != "builtin"
        ),
    }
    errors = [
        f"system/init {key} differs from discovery"
        for key in expected
        if observed[key] != expected[key]
    ]
    refused = set(claude.DISALLOWED_TOOLS) | (set() if subagents else {"Agent", "Task"})
    if not isinstance(init.get("tools"), list) or refused & set(init["tools"]):
        errors.append("system/init offers a disallowed tool")
    return errors + _hook_errors(events, listing)


def _hook_errors(events: list[dict[str, Any]], listing: list[tuple[str, str]]) -> list[str]:
    errors = []
    first_reply = next(
        (index for index, event in enumerate(events) if event.get("type") == "assistant"),
        len(events),
    )
    hooks = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("type") == "system" and str(event.get("subtype", "")).startswith("hook_")
    ]
    started: list[tuple[int, dict[str, Any]]] = []
    responses: dict[object, list[dict[str, Any]]] = {}
    for index, event in hooks:
        name, _, trigger = str(event.get("hook_name", "")).partition(":")
        if name != event.get("hook_event") or not any(
            listed == name and _matches(matcher, trigger) for listed, matcher in listing
        ):
            errors.append(f"hook event {event.get('hook_name')} is not in the listing")
        if event["subtype"] == "hook_started":
            started.append((index, event))
        elif event["subtype"] == "hook_response":
            responses.setdefault(event.get("hook_id"), []).append(event)
    for _, event in started:
        answer = responses.pop(event.get("hook_id"), [])
        if len(answer) != 1 or answer[0].get("exit_code") != 0 or answer[0].get("outcome") != "success":
            errors.append(f"hook {event.get('hook_name')} did not exit 0 exactly once")
    if responses:
        errors.append("a hook responded without starting")
    at_startup = sum(
        1 for name, matcher in listing if name == "SessionStart" and _matches(matcher, "startup")
    )
    ran = sum(
        1
        for index, event in started
        if event.get("hook_name") == "SessionStart:startup" and index < first_reply
    )
    if ran != at_startup:
        errors.append(
            f"{ran} SessionStart startup hooks ran before the first reply; {at_startup} are listed"
        )
    return errors


def _matches(matcher: str, trigger: str) -> bool:
    """Claude's matcher: empty or `*` matches all; otherwise `|`-separated exact names."""
    return matcher in ("", "*") or trigger in matcher.split("|")


def _sorted(value: object) -> list[str]:
    return sorted(str(item) for item in value) if isinstance(value, list) else []


def _json(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None
