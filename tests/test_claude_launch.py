"""Claude coding attempts through Harbor's real ClaudeCode agent and a fake container.

No Docker, credentials or model call: the container runs commands on the host, and the
`claude` it finds on its PATH replays a calibrated 2.1.322 coding fixture.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pluginbench import claude, claude_launch, claude_treatments, evaluation, saved_usage
from pluginbench import harbor_backend as harbor
from pluginbench.native_treatments import REMOTE_ROOT, NativeCapabilities, admit
from pluginbench.processes import run_command
from pluginbench.treatments import Treatment
from test_claude_treatments import HOOK, skill
from test_native_harbor import native_task

FIXTURES = Path(__file__).parent / "fixtures/claude-2.1.322"
VERSION = "2.1.322"
MODEL = "claude-opus-5-5"
TOKEN = "sk-ant-oat01-fake-subscription-token"
DIAGNOSTIC = "claude diagnostic on stderr"


def treatment(root: Path) -> Treatment:
    """A guidance file, a loose skill and a plugin with a skill and a SessionStart hook."""
    guide = root / "guide.md"
    guide.write_text("Keep the implementation in the existing module.\n")
    plugin = root / "demo"
    skill(plugin / "skills/demo-skill", "demo-skill")
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "demo"}))
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": HOOK}]},
    ]}}))
    return Treatment(
        guidance=[guide], skills=[skill(root / "loose-folder", "loose-skill")], plugins=[plugin]
    )


@dataclass
class Container:
    """The attempt's fake container: it records each command with the env it would run with."""

    root: Path
    case: str
    arm: str
    subagents: bool
    persistent: dict[str, str] = field(default_factory=dict)
    harbor_config: dict[str, Any] = field(default_factory=dict)
    commands: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    installs: list[dict[str, Any]] = field(default_factory=list)
    # Shell run after the sessions are written, to damage them.
    after_sessions: str = ""
    # Changes to the captured events the CLI streams before its first model request.
    edit_start: Callable[[list[dict[str, Any]]], None] = lambda events: None
    agent_root: Path | None = None

    @property
    def remote(self) -> Path:
        """Where the container's staged treatment directory lives on the host."""
        return self.root / "remote"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def argv_log(self) -> Path:
        return self.root / "claude-argv.txt"

    def agent_dir(self, config: dict[str, Any]) -> Path:
        return Path(str(config["trials_dir"])) / str(config["trial_name"]) / "agent"

    def stream(self) -> str:
        """The captured start of a real session for this arm, then a calibrated coding run."""
        start = [
            json.loads(line)
            for line in (FIXTURES / "session-start" / self.arm / "session-start.jsonl")
            .read_text()
            .splitlines()
        ]
        coding = [
            json.loads(line)
            for line in (FIXTURES / "coding" / self.case / "stream.jsonl").read_text().splitlines()
        ]
        (init,) = [event for event in coding if event.get("subtype") == "init"]
        for event in start:
            event["session_id"] = init["session_id"]
        _as_probed(start, subagents=self.subagents)
        self.edit_start(start)
        return "".join(json.dumps(event) + "\n" for event in start + coding if event is not init)

    def install_cli(self) -> None:
        """The CLI Harbor's PATH export finds first: it replays one coding fixture."""
        fixture = FIXTURES / "coding" / self.case
        (self.root / "stream.jsonl").write_text(self.stream())
        binary = self.home / ".local/bin/claude"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "2.1.322 (Claude Code)"; exit 0; fi\n'
            f'printf "%s\\n" "$@" > {shlex.quote(str(self.argv_log))}\n'
            f"cat > {shlex.quote(str(self.root / 'prompt.txt'))}\n"
            f'cp -R {shlex.quote(str(fixture / "sessions"))}/. "$CLAUDE_CONFIG_DIR/"\n'
            f"{self.after_sessions}\n"
            f"cat {shlex.quote(str(self.root / 'stream.jsonl'))}\n"
            f"echo {shlex.quote(DIAGNOSTIC)} >&2\n"
        )
        binary.chmod(0o755)
        # The version guard also requires the image's base tools.
        for tool in ("curl", "node", "npm", "ps"):
            stub = binary.with_name(tool)
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)

    def local(self, text: str) -> str:
        """Container paths in a command or env value, as their host directories."""
        assert self.agent_root is not None
        return (
            text.replace("/logs/agent/", f"{self.agent_root}/")
            .replace(REMOTE_ROOT, str(self.remote))
            .replace("/tmp/claude-code-settings", str(self.root / "claude-code-settings"))
        )

    def run_trial(self, config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> object:
        """Harbor's trial, with its real ClaudeCode agent, against this container."""
        container = self
        agent_dir = self.agent_dir(config)
        agent_dir.mkdir(parents=True)
        self.agent_root = agent_dir
        result_type = importlib.import_module("harbor.environments.base").ExecResult
        context = importlib.import_module("harbor.models.agent.context").AgentContext
        agent_type = importlib.import_module("harbor.agents.installed.claude_code").ClaudeCode

        class Environment:
            session_id = config["trial_name"] + "__env"
            default_user = None
            scoped: dict[str, str] = {}

            def _merge_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
                # Harbor's precedence: persistent < per-command < the trial's scoped agent env.
                return {**container.persistent, **(env or {}), **self.scoped} or None

            async def upload_file(self, *args: object, **kwargs: object) -> None:
                pass

            async def exec(
                self,
                command: str,
                user: str | int | None = None,
                env: dict[str, str] | None = None,
                cwd: str | None = None,
                timeout_sec: int | None = None,
            ) -> object:
                merged = self._merge_env(env) or {}
                container.commands.append((command, env if env is not None else {}))
                result = run_command(
                    ["bash", "-c", container.local(command)],
                    cwd=container.root,
                    timeout=30,
                    env={
                        "PATH": os.environ["PATH"],
                        "HOME": str(container.home),
                        **{key: container.local(value) for key, value in merged.items()},
                    },
                )
                return result_type(
                    stdout=result.stdout, stderr=result.stderr, return_code=result.returncode
                )

        class Trial:
            agent_environment = Environment()

            def __init__(self) -> None:
                agent = config["agent"]
                self.agent = agent_type(
                    logs_dir=agent_dir,
                    model_name=agent["model_name"],
                    extra_env=agent.get("env"),
                    **{**agent["kwargs"], **container.harbor_config},
                )
                # Trial scopes the agent's own env over each of its commands.
                Environment.scoped = dict(self.agent.extra_env)

            @classmethod
            async def create(cls, trial_config: object) -> Trial:
                return cls()

            async def run(self) -> object:
                await self.agent.exec_as_agent(
                    self.agent_environment, harbor._native_cli_qualification("claude", VERSION)
                )
                await self.agent.run("Fix the bug.", self.agent_environment, context())
                return SimpleNamespace(
                    model_dump=lambda mode: {"exception_info": None, "verifier_result": None}
                )

        monkeypatch.setattr(importlib.import_module("harbor.trial.trial"), "Trial", Trial)
        monkeypatch.setattr(
            importlib.import_module("harbor.models.trial.config"),
            "TrialConfig",
            SimpleNamespace(model_validate=lambda value: value),
        )
        # Source export and the frozen-image guard are proven elsewhere.
        monkeypatch.setattr(harbor, "install_native_hooks", lambda *args: None)
        return asyncio.run(harbor.run_trial(config))


@dataclass
class Attempt:
    area: Path
    task_dir: Path
    container: Container
    treatment: Treatment
    subagents: bool

    def invoke(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        self.container.install_cli()
        identity: dict[str, object] = {
            "task_assets_sha256": harbor.native_task_assets(self.task_dir),
            "source_digest": "prepared-source",
            "workdir": "/app",
            "image_identity": {"image": "coding", "image_id": "coding", "workdir": "/app"},
            "grader_image_identity": {"image": "grader", "image_id": "grader"},
            "toolchain_identity": {"harness": "claude", "harness_version": VERSION},
            "transport_policy": {
                "name": harbor.NATIVE_TRANSPORT_POLICY,
                "provider_hosts": harbor.NATIVE_PROVIDER_HOSTS["claude"],
            },
        }
        protocol: dict[str, object] = {
            "native_task_identity": identity,
            "isolation": {"resources": {"cpus": 1, "memory_mb": 256}},
            "harness_version": VERSION,
            "reasoning": "high",
            "models": {"claude": MODEL},
            "treatment": self.treatment.model_dump(mode="json"),
            "capabilities": {"subagents": self.subagents},
        }
        monkeypatch.setattr(harbor, "assert_runtime", lambda expected, **kwargs: expected)
        monkeypatch.setattr(harbor, "_assert_native_image", lambda image, expected: None)
        monkeypatch.setattr(
            harbor,
            "_native_process",
            lambda **kwargs: self.container.run_trial(kwargs["config"], monkeypatch),
        )
        monkeypatch.setattr(
            harbor,
            "_native_submission",
            lambda **kwargs: {"status": "complete", "snapshot_digest": "frozen-submission"},
        )
        monkeypatch.setattr(harbor, "_native_cleanup_projects", lambda **kwargs: {"status": "complete"})
        return harbor.invoke_native(
            area=self.area,
            task_dir=self.task_dir,
            entry={"host": "claude"},
            protocol=protocol,
            prompt="",
            timeout=1800,
        )


@pytest.fixture
def attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Attempt]:
    for name in claude.REFUSED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)

    def make(
        *, treated: bool, case: str = "plain", subagents: bool = True, after_sessions: str = ""
    ) -> Attempt:
        arm = "treated" if treated else "vanilla"
        root = tmp_path / arm
        root.mkdir()
        container = Container(root, case, arm, subagents, after_sessions=after_sessions)
        area = root / "area"
        area.mkdir()
        made = Attempt(
            area=area,
            task_dir=native_task(root),
            container=container,
            treatment=treatment(root) if treated else Treatment(),
            subagents=subagents,
        )

        async def install(**kwargs: Any) -> dict[str, Any]:
            # Discovery runs before the coding command, with the env the coding command gets.
            assert not container.argv_log.exists()
            container.installs.append(kwargs)
            # What the real runtime leaves in the container: the staged treatment, and the
            # loose skills Harbor copies into the private configuration.
            claude_treatments.stage(
                made.treatment, container.remote, run=kwargs["run"], cli_version=VERSION
            )
            for source in made.treatment.skills:
                shutil.copytree(
                    source,
                    Path(container.local(claude_treatments.CONFIG_DIR)) / "skills" / source.name,
                    copy_function=shutil.copy2,
                )
            receipt: dict[str, Any] = json.loads((FIXTURES / "session-start" / arm / "receipt.json").read_text())
            receipt["inventory"] = admit(made.treatment, "claude")
            (kwargs["artifacts"] / "native-treatment.json").write_text(json.dumps(receipt))
            return receipt

        monkeypatch.setattr(claude_treatments, "install", install)
        return made

    return make


def coding_command(container: Container) -> tuple[str, dict[str, str]]:
    (coding,) = [
        (command, env) for command, env in container.commands if "--output-format" in command
    ]
    return coding


@pytest.mark.parametrize(
    ("treated", "case", "subagents"),
    [(False, "plain", False), (True, "two-parallel", True)],
)
def test_a_claude_attempt_runs_the_pinned_command_with_the_pinned_subscription_env(
    attempt: Callable[..., Attempt],
    monkeypatch: pytest.MonkeyPatch,
    treated: bool,
    case: str,
    subagents: bool,
) -> None:
    made = attempt(treated=treated, case=case, subagents=subagents)
    execution = made.invoke(monkeypatch)
    run = claude.CodingRun(MODEL, "high", subagents, 1800)
    argv, pinned = claude.coding_launch(run)
    command, env = coding_command(made.container)
    # Harbor's prompt plumbing, then exactly the pinned argv, stderr apart from the stream.
    assert command.startswith('set -o pipefail; export PATH="$HOME/.local/bin:$PATH"; ')
    assert command.endswith(
        f"| {shlex.join(argv)} 2>/logs/agent/claude-code.stderr.log "
        "| tee /logs/agent/claude-code.txt"
    )
    assert made.container.argv_log.read_text().splitlines() == argv[1:]
    assert (made.container.root / "prompt.txt").read_text() == "Fix the bug."
    assert {name: env[name] for name in pinned} == pinned
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert env["CLAUDE_CONFIG_DIR"] == claude_treatments.CONFIG_DIR
    assert not set(env) & set(claude.REFUSED_ENV)
    # Discovery got the very dict the coding command ran with.
    (install,) = made.container.installs
    assert install["env"] is env
    assert install["run"] == run and install["cli_version"] == VERSION

    # The session matched its receipt, and the delivered files outlasted the run.
    trees = [command for command, _ in made.container.commands if " --trees " in command]
    assert len(trees) == (1 if treated else 0)
    if treated:
        assert made.container.commands.index((trees[0], env)) > made.container.commands.index(
            (command, env)
        )
    artifacts = made.area / "artifacts"
    assert DIAGNOSTIC not in (artifacts / "stdout.jsonl").read_text()
    assert (artifacts / "claude-code.stderr.log").read_text() == DIAGNOSTIC + "\n"
    saved = json.loads((artifacts / "execution.json").read_text())
    assert saved == json.loads(json.dumps(execution))
    assert saved["successful_native_completion"] is True
    assert "treatment_error" not in saved and saved["native_treatment"]["status"] == "verified"
    assert saved["claude_init_tools"] == sorted(
        claude.QUALIFIED_TOOLS[VERSION] - (set() if subagents else {"Task"})
    )
    assert saved["launch"]["delivery"] == claude_treatments.DELIVERY_POLICY
    assert saved["launch"]["argv"] == argv
    assert saved["launch"]["capabilities"] == {
        "subagents": subagents, "max_threads": "not_enforced", "max_depth": "not_enforced",
    }
    # The fresh attempt's usage is what reuse and reports revalidate to. A study keeps the
    # receipt beside the attempt's artifacts.
    (made.area / "execution.json").write_text(json.dumps(saved))
    revalidated = saved_usage.revalidate_usage(saved, made.area / "execution.json")
    if treated:
        # A command hook's subprocess is not metered: usage stays incomplete.
        assert saved["usage"] is None and saved["usage_accounting"]["status"] == "incomplete"
        assert saved["native_session_usage"] == revalidated["usage_validation"]["validated_usage"]
        assert saved["native_session_usage"] is not None
        assert revalidated["usage"] is None
    else:
        assert saved["usage"] is not None
        assert saved["usage_accounting"]["status"] == "complete"
        assert saved["usage_accounting"]["rule"] == "equal"
        assert revalidated["usage"] == saved["usage"]
        assert revalidated["usage_validation"]["status"] == "validated"


def test_a_fresh_attempt_with_unreconciled_usage_is_unvalidated_at_launch(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The subagent transcript answers a call the stream never made.
    made = attempt(
        treated=False,
        case="two-parallel",
        after_sessions='for meta in "$CLAUDE_CONFIG_DIR"/projects/*/*/subagents/*.meta.json; '
        "do echo '{\"toolUseId\": \"toolu_orphan\"}' > \"$meta\"; done",
    )
    made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is True
    assert saved["usage"] is None and saved["usage_accounting"]["status"] == "incomplete"
    assert "a subagent transcript has no matching Agent tool call" in (
        saved["usage_accounting"]["reasons"]
    )


def test_an_api_key_in_the_container_env_stops_the_attempt_before_any_command(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    made = attempt(treated=False)
    # A task's own environment can name a key; Harbor would pass it to every command.
    made.container.persistent = {"ANTHROPIC_API_KEY": "sk-ant-api-key"}
    with pytest.raises(ValueError, match="refuses ANTHROPIC_API_KEY"):
        made.invoke(monkeypatch)
    assert made.container.commands == [] and not made.container.installs
    assert not made.container.argv_log.exists()


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])
def test_a_refused_variable_in_the_operator_env_stops_the_run_before_any_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv(name, "operator-value")
    monkeypatch.setattr(
        harbor, "run_command", lambda *args, **kwargs: pytest.fail("worker started")
    )
    with pytest.raises(ValueError, match=f"CLAUDE_CODE_OAUTH_TOKEN only; unset {name}"):
        harbor._native_process(
            area=tmp_path, config={}, expected={}, harness="claude", timeout=60,
            install_only=False,
        )
    assert not (tmp_path / "private").exists()
    # The evaluation's own check refuses it too, before Docker or task preparation.
    monkeypatch.setattr(claude, "preflight", lambda: {"harness_version": "2.1.322 (Claude Code)"})
    with pytest.raises(ValueError, match=f"unset {name}"):
        evaluation._harness_version("claude", VERSION, coding=True)


@pytest.mark.parametrize(
    "harbor_config",
    [
        # PluginBench's old Claude kwargs: Harbor turns any config into its own --settings.
        {"config": {}},
        {"max_turns": 5},
        {"disable_web_search": True},
        {"permission_mode": "acceptEdits"},
    ],
)
def test_a_command_off_the_contract_is_refused_before_the_cli_starts(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch, harbor_config: dict[str, Any]
) -> None:
    made = attempt(treated=False)
    made.container.harbor_config = harbor_config
    with pytest.raises(ValueError, match="pinned launch contract"):
        made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is False
    assert "differs from the pinned launch contract" in json.dumps(saved["error"])
    assert not made.container.installs and not made.container.argv_log.exists()
    # Neither did Harbor's setup or cleanup of the settings folder a config adds.
    assert not any("claude-code-settings" in command for command, _ in made.container.commands)


def _pinned_agent(skills_dir: str | None = None) -> tuple[Any, list[str]]:
    """An agent whose own `exec_as_agent` records commands, behind the contract."""
    ran: list[str] = []

    async def exec_as_agent(environment: object, command: str, **kwargs: object) -> object:
        ran.append(command)
        return SimpleNamespace(stdout="", stderr="", return_code=0)

    agent = SimpleNamespace(exec_as_agent=exec_as_agent, skills_dir=skills_dir)
    boundary = {"coding_run": {"model": MODEL, "effort": "high", "subagents": False,
                                "timeout_seconds": 1800}}
    claude_launch.pin_exec(agent, boundary, qualification="guard")
    return agent, ran


class _Environment:
    def _merge_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        return env


@pytest.mark.parametrize(
    "command",
    [
        "/root/.local/bin/claude -p hi",
        '"$HOME/.local/bin/claude" -p hi',
        "claude<<<x",
        "claude>f",
        "env",
        claude_launch.harbor_setup(None) + "; env > /tmp/env",
        claude_launch.harbor_setup(None) + " && mkdir -p /tmp/claude-code-settings",
        claude_launch.harbor_setup("/skills"),
    ],
)
def test_the_agent_runs_only_the_guard_harbors_setup_and_the_coding_command(command: str) -> None:
    agent, ran = _pinned_agent()
    with pytest.raises(ValueError, match="pinned launch contract"):
        asyncio.run(agent.exec_as_agent(_Environment(), command))
    assert ran == []


def test_harbors_setup_with_task_skills_is_allowed(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    made = attempt(treated=False)
    # Harbor's trial hands the agent a skills folder when the task or Harbor has one.
    made.container.harbor_config = {"skills_dir": "/task skills"}
    made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is True
    setup = "set -o pipefail; " + claude_launch.harbor_setup("/task skills")
    assert setup in [command for command, _ in made.container.commands]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda command: command.replace(" 2>&1 | tee", " | tee"),
        lambda command: command.replace("claude-code.txt", "other.txt"),
        lambda command: command.replace("--print", "--print --settings /tmp/s.json"),
        lambda command: command.replace("unset HARBOR", "unset OTHER"),
        lambda command: command + "; curl example.com",
    ],
)
def test_the_contract_accepts_only_the_exact_harbor_command(
    mutation: Callable[[str], str],
) -> None:
    run = claude.CodingRun(MODEL, "high", True, 1800)
    variable = "harbor_claude_code_instruction_" + "ab" * 16
    harbor_command = (
        'export PATH="$HOME/.local/bin:$PATH"; '
        f'{variable}="${variable.upper()}"; unset {variable.upper()}; '
        f'printf "%s" "${variable}" | claude --verbose --output-format=stream-json '
        "--effort high --permission-mode=bypassPermissions --print 2>&1 "
        "| tee /logs/agent/claude-code.txt"
    )
    assert claude.contract_command(harbor_command, run).endswith(
        f"{shlex.join(claude.coding_launch(run)[0])} 2>{claude.STDERR_LOG} | tee {claude.STDOUT_LOG}"
    )
    with pytest.raises(ValueError, match="pinned launch contract"):
        claude.contract_command(mutation(harbor_command), run)


def test_a_second_coding_command_in_one_attempt_is_refused(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    made = attempt(treated=False)
    original = made.container.run_trial

    def twice(config: dict[str, Any], patch: pytest.MonkeyPatch) -> object:
        agent_type = importlib.import_module("harbor.agents.installed.claude_code").ClaudeCode
        first = agent_type.run

        async def run_twice(self: Any, *args: Any) -> None:
            await first(self, *args)
            await first(self, *args)

        patch.setattr(agent_type, "run", run_twice)
        return original(config, patch)

    monkeypatch.setattr(made.container, "run_trial", twice)
    monkeypatch.setattr(claude_launch, "transport", lambda: {})
    with pytest.raises(ValueError, match="runs one coding command"):
        made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert "runs one coding command" in json.dumps(saved["error"])
    assert len(made.container.installs) == 1


def test_the_version_guard_runs_the_cli_the_coding_command_runs() -> None:
    guard = harbor._native_cli_qualification("claude", VERSION)
    assert guard.startswith('export PATH="$HOME/.local/bin:$PATH"; set -eu; ')


def test_claude_harbor_config_passes_no_settings_and_pins_the_agent_env(tmp_path: Path) -> None:
    config = harbor._native_config(
        task_dir=tmp_path, area=tmp_path, owner="o", harness="claude", version=VERSION,
        limits={"cpus": 1, "memory_mb": 256}, timeout=1800, model=MODEL, effort="high",
        treatment=Treatment(), capabilities=NativeCapabilities(subagents=False),
        agent_env={"GIT_AUTHOR_NAME": "PluginBench"},
    )
    agent = config["agent"]
    assert isinstance(agent, dict)
    assert agent["kwargs"] == {"version": VERSION, "reasoning_effort": "high"}
    pinned = claude.coding_launch(claude.CodingRun(MODEL, "high", False, 1800))[1]
    assert agent["env"] == {**pinned, "GIT_AUTHOR_NAME": "PluginBench"}


def test_the_claude_transport_pins_harbors_run_source(monkeypatch: pytest.MonkeyPatch) -> None:
    assert claude_launch.transport() == {
        "version": "claude-pinned-launch-v1",
        "harbor_run_sha256": claude_launch._HARBOR_RUN_SHA256,
    }
    monkeypatch.setattr(claude_launch, "_HARBOR_RUN_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="ClaudeCode.run changed"):
        claude_launch.transport()


def _as_probed(events: list[dict[str, Any]], *, subagents: bool) -> None:
    """Give a captured offline start the tools of a signed-in 2.1.322 session.

    The fixtures were captured with a fake token, where the CLI leaves out WebFetch; every
    signed-in probe run offered it. The real CLI drops its subagent tool, Task, for
    `--disallowed-tools Agent`.
    """
    tools = _init(events)["tools"]
    tools.append("WebFetch")
    if not subagents:
        tools.remove("Task")


def _init(events: list[dict[str, Any]]) -> dict[str, Any]:
    (init,) = [event for event in events if event.get("subtype") == "init"]
    return init


def _hook(events: list[dict[str, Any]], subtype: str) -> dict[str, Any]:
    (hook,) = [event for event in events if event.get("subtype") == subtype]
    return hook


def _repeat_hook(events: list[dict[str, Any]], *, before_reply: bool) -> None:
    """Run the start's one SessionStart hook a second time, under a new id."""
    pair = [
        {**_hook(events, subtype), "hook_id": "again"} for subtype in ("hook_started", "hook_response")
    ]
    if before_reply:
        events[:0] = pair
    else:
        events.extend([{"type": "assistant", "message": {"content": []}}, *pair])


def _drop_hooks(events: list[dict[str, Any]]) -> None:
    events[:] = [event for event in events if not event["subtype"].startswith("hook_")]


SESSION_CHANGES: dict[str, tuple[bool, Callable[[list[dict[str, Any]]], None], str]] = {
    "model": (False, lambda e: _init(e).update(model="claude-sonnet-5-5"), "model differs"),
    "version": (
        False, lambda e: _init(e).update(claude_code_version="2.1.323"),
        "claude_code_version differs",
    ),
    "api key": (
        False, lambda e: _init(e).update(apiKeySource="ANTHROPIC_API_KEY"), "apiKeySource differs",
    ),
    "agent": (False, lambda e: _init(e)["agents"].append("claude-code-guide"), "agents differs"),
    "skill": (True, lambda e: _init(e)["skills"].append("undeclared"), "skills differs"),
    "command": (
        True, lambda e: _init(e)["slash_commands"].append("undeclared"), "slash_commands differs",
    ),
    "plugin": (
        True,
        lambda e: _init(e)["plugins"].append({"name": "x", "path": "/x", "source": "x@other"}),
        "plugins differs",
    ),
    "plugin folder": (
        True,
        lambda e: _init(e)["plugins"][0].update(path="/logs/agent/sessions/plugins/cache/demo"),
        "plugins differs",
    ),
    "mcp": (False, lambda e: _init(e).update(mcp_servers=[{"name": "m"}]), "mcp_servers differs"),
    "subagent tool": (False, lambda e: _init(e)["tools"].append("Task"), "disallowed tool"),
    "tool removed": (
        False, lambda e: _init(e).update(tools=["Read"]), "tools differ from the pinned tool set",
    ),
    "tool added": (
        False, lambda e: _init(e)["tools"].append("LSP"), "tools differ from the pinned tool set",
    ),
    "remote tool": (False, lambda e: _init(e)["tools"].append("RemoteTrigger"), "disallowed tool"),
    "hook did not run": (True, lambda e: _drop_hooks(e), "0 SessionStart startup hooks ran"),
    "hook failed": (
        True, lambda e: _hook(e, "hook_response").update(exit_code=1, outcome="error"),
        "did not exit as allowed exactly once",
    ),
    "startup hook blocked": (
        True, lambda e: _hook(e, "hook_response").update(exit_code=2, outcome="error"),
        "did not exit as allowed exactly once",
    ),
    "startup hook ran twice": (
        True, lambda e: _repeat_hook(e, before_reply=True), "2 SessionStart startup hooks ran",
    ),
    "startup hook ran after the first reply": (
        True, lambda e: _repeat_hook(e, before_reply=False), "ran after the first reply",
    ),
    "unlisted hook": (
        True,
        lambda e: e.insert(0, {**_hook(e, "hook_started"), "hook_name": "PreToolUse:Bash",
                               "hook_event": "PreToolUse", "hook_id": "other"}),
        "hook event PreToolUse:Bash is not in the listing",
    ),
    "hook in a vanilla arm": (
        False,
        lambda e: e.insert(0, {"type": "system", "subtype": "hook_started", "hook_id": "h",
                               "hook_name": "SessionStart:startup", "hook_event": "SessionStart"}),
        "hook event SessionStart:startup is not in the listing",
    ),
}


@pytest.mark.parametrize("arm", ["vanilla", "treated", "superpowers"])
def test_each_captured_real_session_matches_its_real_receipt(arm: str) -> None:
    assert _errors(*_captured(arm)) == []


@pytest.mark.parametrize("change", sorted(SESSION_CHANGES))
def test_a_session_that_differs_from_its_discovery_receipt_fails_the_attempt(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    treated, edit, reason = SESSION_CHANGES[change]
    made = attempt(treated=treated, subagents=change != "subagent tool")
    made.container.edit_start = edit
    made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is False
    assert "differs from its discovery receipt" in saved["treatment_error"]
    assert reason in saved["treatment_error"]


def test_a_run_with_a_permission_denial_fails_the_attempt(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The paid probe's one-subagent run: the subagent's Bash call was denied.
    made = attempt(treated=False, case="one-subagent")
    made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is False
    assert "the run records permission denials" in saved["treatment_error"]


def _captured(arm: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    captured = FIXTURES / "session-start" / arm
    events = [json.loads(line) for line in (captured / "session-start.jsonl").read_text().splitlines()]
    _as_probed(events, subagents=True)
    return events, json.loads((captured / "receipt.json").read_text())


def _errors(events: list[dict[str, Any]], receipt: dict[str, Any]) -> list[str]:
    stream = "".join(json.dumps(event) + "\n" for event in events)
    return claude_launch.session_errors(stream, receipt, model=MODEL, subagents=True)


@pytest.mark.parametrize(("exit_code", "allowed"), [(0, True), (2, True), (1, False)])
def test_a_listed_tool_hook_may_allow_or_block(exit_code: int, allowed: bool) -> None:
    events, receipt = _captured("treated")
    receipt["hooks"]["hooks"].append({"event": "PreToolUse", "matcher": "Bash"})
    hook = {"type": "system", "hook_id": "tool", "hook_name": "PreToolUse:Bash",
            "hook_event": "PreToolUse"}
    events += [
        {"type": "assistant", "message": {"content": []}},
        {**hook, "subtype": "hook_started"},
        {**hook, "subtype": "hook_response", "exit_code": exit_code, "outcome": "error"},
    ]
    assert (_errors(events, receipt) == []) is allowed


def test_an_unqualified_version_is_held_only_to_the_disallowed_tools() -> None:
    events, receipt = _captured("vanilla")
    receipt["cli_version"] = "2.1.400"
    _init(events).update(claude_code_version="2.1.400", tools=["Read"])
    assert _errors(events, receipt) == []
    _init(events)["tools"].append("RemoteTrigger")
    assert _errors(events, receipt) == ["system/init offers a disallowed tool"]


def test_a_failed_usage_check_leaves_the_attempt_usage_unvalidated(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> dict[str, Any]:
        raise ValueError("unreadable transcript")

    monkeypatch.setattr(saved_usage, "claude_attempt_usage", broken)
    made = attempt(treated=False)
    made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["usage"] is None
    assert saved["usage_accounting"] == {
        "status": "incomplete", "reasons": ["the usage check failed: unreadable transcript"],
    }


@pytest.mark.parametrize(
    "damage",
    [
        "echo changed >> {remote}/marketplace/plugins/demo/skills/demo-skill/SKILL.md",
        "chmod 600 {remote}/marketplace/plugins/demo/.claude-plugin/plugin.json",
        "mkdir {remote}/marketplace/plugins/demo/agents",
        'echo changed >> "$CLAUDE_CONFIG_DIR/skills/loose-folder/SKILL.md"',
    ],
)
def test_treatment_files_changed_during_the_run_fail_the_attempt(
    attempt: Callable[..., Attempt], monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    made = attempt(treated=True)
    made.container.after_sessions = damage.format(remote=shlex.quote(str(made.container.remote)))
    with pytest.raises(ValueError, match="files changed during the coding run"):
        made.invoke(monkeypatch)
    saved = json.loads((made.area / "artifacts/execution.json").read_text())
    assert saved["successful_native_completion"] is False


@pytest.mark.skipif(
    os.environ.get("PLUGINBENCH_REAL_CLAUDE_TESTS") != "1",
    reason="Explicit check against the host's real Claude Code CLI 2.1.322",
)
@pytest.mark.parametrize("subagents", [False, True])
def test_the_real_cli_drops_its_subagent_tool_for_disallowed_agent(
    tmp_path: Path, subagents: bool
) -> None:
    """`system/init` names the subagent tool Task; `--disallowed-tools Agent` removes it.

    A fake token: the CLI reports init before its first model request, and the test stops it
    there.
    """
    import subprocess

    argv, pinned = claude.coding_launch(claude.CodingRun(MODEL, "low", subagents, 60))
    argv[argv.index("--print") : argv.index("--print")] = ["--input-format", "stream-json"]
    for name in ("home", "config"):
        (tmp_path / name).mkdir()
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"), "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, **pinned,
    }
    process = subprocess.Popen(
        argv, cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}))
    process.stdin.write("\n")
    process.stdin.flush()
    try:
        init = next(
            event for event in map(json.loads, process.stdout) if event.get("subtype") == "init"
        )
    finally:
        process.kill()
        process.wait()
    assert init["claude_code_version"] == VERSION
    assert ("Task" in init["tools"]) is subagents
    assert not set(claude.DISALLOWED_TOOLS) & set(init["tools"])
