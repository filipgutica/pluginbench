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
import shlex
from collections import Counter
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
# The setup command of ClaudeCode.run in Harbor 0.24.0 for PluginBench's agent config: no
# settings file, memory or MCP servers. Harbor appends the skills copy when it was given skills.
_HARBOR_SETUP = (
    "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
    "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
    "$CLAUDE_CONFIG_DIR/todos $CLAUDE_CONFIG_DIR/skills && "
    "if [ -d ~/.claude/skills ]; then "
    "cp -r ~/.claude/skills/. $CLAUDE_CONFIG_DIR/skills/ 2>/dev/null || true; "
    "fi"
)
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


def harbor_setup(skills_dir: str | None) -> str:
    """The setup command Harbor runs before coding, given the skills directory it was handed."""
    if not skills_dir:
        return _HARBOR_SETUP
    return (
        f"{_HARBOR_SETUP} && (cp -r {shlex.quote(skills_dir)}/* "
        "$CLAUDE_CONFIG_DIR/skills/ 2>/dev/null || true)"
    )


def pin_exec(agent: object, boundary: dict[str, Any], *, qualification: str) -> None:
    """Replace the agent's `exec_as_agent` with the contract, an allowlist of three commands.

    The agent may run the version guard `qualification`, Harbor's setup, and Harbor's coding
    command, which runs as the pinned argv; any other command is refused. Every command is
    refused when its environment carries a refused variable, so an API key stops the attempt
    before Harbor's setup runs.

    Harbor passes the subscription token in each command's env; no command gets it. The
    coding command first stores it as the CLI's credentials file, which discovery and coding
    read, and removes the file when it ends, before Harbor downloads /logs.
    """
    original: Callable[..., Awaitable[object]] = getattr(agent, "exec_as_agent")
    upload: Callable[..., Awaitable[None]] = getattr(agent, "_upload_config_text")
    run = claude.CodingRun(**boundary["coding_run"])
    allowed = {qualification, harbor_setup(getattr(agent, "skills_dir", None))}
    coded = False

    async def pinned_exec(
        environment: object,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> object:
        nonlocal coded
        env = dict(env or {})
        token = env.pop(claude.TOKEN_ENV, "").strip()
        # What the environment will actually run with: its own, this call's and Harbor's
        # scoped agent env, which outranks both.
        merged = dict(getattr(environment, "_merge_env")(env) or {})
        claude.refuse_container_env(merged)
        if command in allowed:
            return await original(environment, command, env=env, cwd=cwd, timeout_sec=timeout_sec)
        # Anything else must be the coding command.
        pinned = claude.contract_command(command, run)
        if coded:
            raise ValueError("a Claude attempt runs one coding command; use a fresh attempt")
        coded = True
        claude.check_coding_env(merged, run)
        if not token:
            raise ValueError(f"Harbor passed no {claude.TOKEN_ENV} for the Claude coding run")
        secret = f"{claude.SECRETS_DIR}/{claude.CREDENTIALS_FILE}"
        link = f"{claude_treatments.CONFIG_DIR}/{claude.CREDENTIALS_FILE}"
        try:
            await original(
                environment, f"mkdir -p -m 700 {claude.SECRETS_DIR}", env=merged, cwd=cwd,
                timeout_sec=30,
            )
            # Harbor's private upload: owned by the agent user, mode 600.
            await upload(
                environment, content=claude.credentials(token), remote_path=secret,
                filename=claude.CREDENTIALS_FILE,
            )
            await original(
                environment, f"chmod 600 {secret} && ln -s {secret} {link}", env=merged,
                cwd=cwd, timeout_sec=30,
            )
            return await _discover_and_code(environment, pinned, merged, cwd, timeout_sec)
        finally:
            await original(environment, f"rm -f {link} {secret}", env=merged, cwd=cwd, timeout_sec=30)

    async def _discover_and_code(
        environment: object,
        pinned: str,
        merged: dict[str, str],
        cwd: str | None,
        timeout_sec: int | None,
    ) -> object:
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


def refuse_leaked_token(saved: list[Path], token: str) -> None:
    """Refuse an attempt whose saved artifacts or exported workspace hold the token anywhere."""
    token = token.strip()
    if not token:
        raise ValueError(f"no {claude.TOKEN_ENV} to check the saved artifacts against")
    leaked = sorted(
        str(path.relative_to(root.parent))
        for root in saved
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and token.encode() in path.read_bytes()
    )
    if leaked:
        raise ValueError(
            "the Claude subscription token reached saved artifacts: " + ", ".join(leaked)
        )


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
    stream: str, receipt: dict[str, Any], *, model: str, subagents: bool, transcript: str
) -> list[str]:
    """How a coding session differs from the discovery receipt it was launched after (D2).

    The session's `system/init` must report what discovery found, the pinned permission mode
    and the pinned tools, and its result must record no permission denial. Its hook events must be the listed hooks:
    each SessionStart hook that matches `startup` runs once, by its command in the root
    `transcript`, and exits 0 before the first assistant message; other listed hooks exit 0
    or 2 (a deliberate block), and no other hook event appears.
    """
    events = _events(stream)
    inits = _inits(events)
    if len(inits) != 1:
        return ["the coding stream needs exactly one system/init"]
    init = inits[0]
    try:
        builtin = set(receipt["builtins"]["commands"])
        # system/init names a skill as the dialog does (a loose one by its folder) and, like
        # discovery's commands, leaves out a `user-invocable: false` one (CLI 2.1.322).
        commands = set(receipt["commands"])
        skills = {skill["name"] for skill in receipt["skills"] if skill["display_name"] in commands}
        plugins = sorted((item["id"], item["loaded_from"]) for item in receipt["plugin_installs"])
        listing = [(hook["event"], hook.get("matcher") or "") for hook in receipt["hooks"]["hooks"]]
        at_startup = Counter(
            str(hook["commandText"])
            for hook in receipt["hooks"]["hooks"]
            if hook["event"] == "SessionStart" and _matches(hook.get("matcher") or "", "startup")
        )
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
    if init.get("permissionMode") != claude.PERMISSION_MODE:
        errors.append(f"system/init permissionMode is not {claude.PERMISSION_MODE}")
    tools = init.get("tools")
    refused = set(claude.DISALLOWED_TOOLS) | (set() if subagents else {"Agent", claude.SUBAGENT_TOOL})
    qualified = claude.QUALIFIED_TOOLS.get(str(receipt["cli_version"]))
    if not isinstance(tools, list) or refused & set(tools):
        errors.append("system/init offers a disallowed tool")
    elif qualified is not None and sorted(tools) != sorted(
        qualified - (set() if subagents else {claude.SUBAGENT_TOOL})
    ):
        errors.append("system/init tools differ from the pinned tool set")
    # bypassPermissions allows every tool, so a denial is something it still refuses.
    if any(event.get("permission_denials") != [] for event in events if event.get("type") == "result"):
        errors.append("the run records permission denials")
    return errors + _hook_errors(events, listing) + _startup_hook_errors(transcript, at_startup)


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
        # Exit 2 is a hook's deliberate block; a session must start cleanly.
        session = event.get("hook_event") == "SessionStart"
        if len(answer) != 1 or (
            (answer[0].get("exit_code"), answer[0].get("outcome")) != (0, "success")
            if session
            else answer[0].get("exit_code") not in (0, 2)
        ):
            errors.append(f"hook {event.get('hook_name')} did not exit as allowed exactly once")
    if responses:
        errors.append("a hook responded without starting")
    at_startup = sum(
        1 for name, matcher in listing if name == "SessionStart" and _matches(matcher, "startup")
    )
    startup = [index for index, event in started if event.get("hook_name") == "SessionStart:startup"]
    # CLI 2.1.322 names a hook event only by event and trigger, so hooks are counted, not
    # told apart: one that ran twice and one that never ran look like two runs.
    if any(index > first_reply for index in startup):
        errors.append("a SessionStart startup hook ran after the first reply")
    if len(startup) != at_startup:
        errors.append(f"{len(startup)} SessionStart startup hooks ran; {at_startup} are listed")
    return errors


def _startup_hook_errors(transcript: str, listed: Counter[str]) -> list[str]:
    """The root transcript names each hook run by its command as declared, with
    `${CLAUDE_PLUGIN_ROOT}` unexpanded, as discovery lists it (verified on CLI 2.1.322)."""
    entries = _events(transcript)
    first_reply = next(
        (index for index, entry in enumerate(entries) if entry.get("type") == "assistant"),
        len(entries),
    )
    runs = [
        (index, attachment)
        for index, entry in enumerate(entries)
        if isinstance(attachment := entry.get("attachment"), dict)
        and attachment.get("hookName") == "SessionStart:startup"
    ]
    errors = []
    if Counter(str(run.get("command")) for _, run in runs) != listed:
        errors.append("the transcript's SessionStart startup hooks are not each listed hook once")
    if any(
        index > first_reply or (run.get("type"), run.get("exitCode")) != ("hook_success", 0)
        for index, run in runs
    ):
        errors.append("a SessionStart startup hook failed or ran after the first reply")
    return errors


def root_transcript(sessions: Path, stream: str) -> str:
    """The text of the coding session's root transcript, or "" without exactly one."""
    inits = _inits(_events(stream))
    session = inits[0].get("session_id") if len(inits) == 1 else None
    found = list((sessions / "projects").glob(f"*/{session}.jsonl")) if session else []
    return found[0].read_text(errors="replace") if len(found) == 1 else ""


def _matches(matcher: str, trigger: str) -> bool:
    """Claude's matcher in the grammar admission allows: empty or `*` matches all; otherwise
    `|`-separated exact names (verified on CLI 2.1.322: `start` does not match `startup`)."""
    return matcher in ("", "*") or trigger in matcher.split("|")


def _sorted(value: object) -> list[str]:
    return sorted(str(item) for item in value) if isinstance(value, list) else []


def init_tools(stream: str) -> list[str] | None:
    """The tools a coding session's one `system/init` offered, for the attempt's record."""
    inits = _inits(_events(stream))
    return _sorted(inits[0].get("tools")) if len(inits) == 1 else None


def permission_denials(stream: str) -> list[str]:
    """The tools the CLI denied during the run, for the attempt's record; any denial also
    fails the attempt (`session_errors`)."""
    return [
        str(denial.get("tool_name"))
        for event in _events(stream)
        if event.get("type") == "result" and isinstance(event.get("permission_denials"), list)
        for denial in event["permission_denials"]
        if isinstance(denial, dict)
    ]


def _events(stream: str) -> list[dict[str, Any]]:
    return [
        event
        for line in stream.splitlines()
        if line.startswith("{") and isinstance(event := _json(line), dict)
    ]


def _inits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]


def _json(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None
