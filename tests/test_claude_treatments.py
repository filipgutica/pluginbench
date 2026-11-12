"""Claude delivery and no-model discovery proofs, against a fake `claude`; no credentials."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pluginbench import claude
from pluginbench.claude_treatments import CONFIG_DIR, DELIVERY_POLICY, install, stage
from pluginbench.native_treatments import REMOTE_ROOT
from pluginbench.treatments import Treatment

FIXTURES = Path(__file__).parent / "fixtures/claude-2.1.322"
CAPTURED = json.loads((FIXTURES / "discovery.json").read_text())
RUN = claude.CodingRun(model="claude-opus-5-5", effort="high", subagents=True, timeout_seconds=1800)
VERSION = "2.1.322"
HOOK = 'echo \'{"hookSpecificOutput":{"hookEventName":"SessionStart"}}\''


@dataclass
class Attempt:
    """One attempt's private runtime: its paths, treatment and the fake CLI's scenario."""

    root: Path
    treatment: Treatment
    scenario: dict[str, Any]
    # Merged over the launch environment, as a misconfigured runtime would pass it.
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def staged(self) -> Path:
        return self.root / "staged"

    def env(self) -> dict[str, str]:
        return {
            "PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", os.defpath),
            "HOME": str(self.root / "home"),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "FAKE_CLAUDE_SCENARIO": str(self.root / "scenario.json"),
            "FAKE_CLAUDE_LOG": str(self.root / "claude-calls.jsonl"),
            **claude.discovery_launch(RUN)[1],
            **self.extra_env,
        }

    def run(self) -> subprocess.CompletedProcess[str]:
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))
        return subprocess.run(
            [
                "node",
                str(self.staged / "install.cjs"),
                str(self.staged),
                str(self.root / "workspace"),
            ],
            cwd=self.root / "workspace",
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        return [
            json.loads(line) for line in (self.root / "claude-calls.jsonl").read_text().splitlines()
        ]


def skill(root: Path, name: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Claude fixture skill.\n---\n")
    (root / "check.sh").write_text("#!/bin/sh\nprintf proof\n")
    (root / "check.sh").chmod(0o755)
    return root


def responses(attempt: Attempt) -> dict[str, Any]:
    """What CLI 2.1.322 answers for the attempt's declared treatment, from captured shapes."""
    captured: dict[str, Any] = json.loads(
        json.dumps(CAPTURED["responses"])
        .replace("/SCRATCH/cfg", str(attempt.config))
        .replace("/SCRATCH/market", str(attempt.staged / "marketplace"))
        .replace("/SCRATCH/work", str(attempt.root / "workspace"))
    )
    treatment = attempt.treatment
    # Commands and the dialog's display_name use the frontmatter name; the dialog's name is
    # the folder (CLI 2.1.322).
    loose = {
        path.name: (path / "SKILL.md").read_text().split("name: ", 1)[1].split("\n", 1)[0]
        for path in treatment.skills
    }
    packaged = [
        f"{plugin.name}:{name}"
        for plugin in treatment.plugins
        for name in sorted(path.name for path in (plugin / "skills").iterdir())
    ]
    initialize = captured["initialize"]
    initialize["commands"] = [
        *[
            {"name": name, "description": "", "argumentHint": ""}
            for name in [*loose.values(), *packaged]
        ],
        *[command for command in initialize["commands"] if command.get("builtin")],
    ]
    dialog_entry = captured["get_skills_dialog"]["skills"][1]
    captured["get_skills_dialog"]["skills"] = [
        *[
            {**dialog_entry, "name": name, "display_name": name, "source": "plugin"}
            for name in packaged
        ],
        *[
            {**dialog_entry, "name": folder, "display_name": name, "source": "user"}
            for folder, name in loose.items()
        ],
    ]
    listing = captured["get_hooks_listing"]
    listing["policy"]["allDisabled"] = True
    hook_entry = listing["hooks"][0]
    listing["hooks"] = [
        {
            **hook_entry,
            "pluginName": f"{plugin.name}@pluginbench-frozen",
            "displayText": HOOK,
            "commandText": HOOK,
        }
        for plugin in treatment.plugins
    ]
    listing["events"] = listing["events"] if treatment.plugins else []
    memory = captured["get_memory_dialog"]
    memory["files"][0]["exists"] = bool(treatment.guidance)
    for category in captured["get_context_usage"]["categories"]:
        if category["id"] == "memory_files" and not treatment.guidance:
            category["tokens"] = 0
    settings = captured["get_settings"]
    if treatment.plugins:
        settings["effective"]["disableAllHooks"] = True
    else:
        settings["effective"] = {"disableAllHooks": True}
        settings["sources"] = []
    return captured


def attempt(tmp_path: Path, *, vanilla: bool = False) -> Attempt:
    if shutil.which("node") is None:
        pytest.skip("Claude installer needs Node")
    for name in ("home", "config", "workspace", "bin"):
        (tmp_path / name).mkdir(parents=True)
    binary = tmp_path / "bin/claude"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_claude.py"}" "$@"\n')
    binary.chmod(0o755)
    treatment = Treatment()
    if not vanilla:
        guide = tmp_path / "guide.md"
        guide.write_text("Keep the implementation in the existing module.\n")
        plugin = tmp_path / "demo"
        skill(plugin / "skills/demo-skill", "demo-skill")
        (plugin / ".claude-plugin").mkdir()
        (plugin / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0"})
        )
        (plugin / "hooks").mkdir()
        (plugin / "hooks/hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"matcher": "startup", "hooks": [{"type": "command", "command": HOOK}]},
                        ]
                    }
                }
            )
        )
        treatment = Treatment(
            guidance=[guide],
            # A folder name that differs from the skill name, as Claude reports both.
            skills=[skill(tmp_path / "loose-folder", "loose-skill")],
            plugins=[plugin],
        )
        # Harbor's setup copies loose skills into the private configuration.
        shutil.copytree(treatment.skills[0], tmp_path / "config/skills/loose-folder")
    stage(treatment, tmp_path / "staged", run=RUN, cli_version=VERSION)
    result = Attempt(tmp_path, treatment, {})
    result.scenario["responses"] = responses(result)
    return result


def test_vanilla_receipt_proves_nothing_loaded_without_a_model_call(tmp_path: Path) -> None:
    vanilla = attempt(tmp_path, vanilla=True)
    result = vanilla.run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "verified" and receipt["delivery"] == DELIVERY_POLICY
    assert receipt["commands"] == [] and receipt["skills"] == [] and receipt["plugins"] == []
    assert receipt["hooks"] == {
        "hooks": [],
        "policy": {**CAPTURED["responses"]["get_hooks_listing"]["policy"], "allDisabled": True},
    }
    assert receipt["guidance"]["path"] is None and receipt["guidance"]["memory_files_tokens"] == 0
    assert receipt["builtins"] == {
        "commands": ["code-review", "debug"],
        "agents": [
            "Explore",
            "Plan",
            "claude",
            "claude-code-guide",
            "general-purpose",
            "statusline-setup",
        ],
    }
    assert not (vanilla.config / "CLAUDE.md").exists()
    # No marketplace or install; discovery ran with exactly the shared builder's argv.
    assert vanilla.calls() == [["plugin", "list", "--json"], claude.discovery_launch(RUN)[0][1:]]


def test_treatment_receipt_proves_guidance_skills_plugin_and_hook_loaded(tmp_path: Path) -> None:
    treated = attempt(tmp_path)
    result = treated.run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "verified"
    assert receipt["commands"] == ["demo:demo-skill", "loose-skill"]
    assert {(item["display_name"], item["source"]) for item in receipt["skills"]} == {
        ("demo:demo-skill", "plugin"),
        ("loose-skill", "user"),
    }
    assert [item["id"] for item in receipt["plugins"]] == ["demo@pluginbench-frozen"]
    [installed] = receipt["plugin_installs"]
    # The CLI loads a directory marketplace's plugin from the staged folder, not the cache.
    loaded_from = Path(installed["loaded_from"])
    assert loaded_from == (treated.staged / "marketplace/plugins/demo").resolve()
    assert (loaded_from / "skills/demo-skill/check.sh").stat().st_mode & 0o777 == 0o755
    assert Path(installed["cache_path"]).is_relative_to((treated.config / "plugins/cache").resolve())
    # The key the hook-incomplete usage rule reads.
    [hook] = receipt["hooks"]["hooks"]
    assert (hook["event"], hook["matcher"], hook["commandText"]) == (
        "SessionStart",
        "startup",
        HOOK,
    )
    assert receipt["hooks"]["policy"]["allDisabled"] is True
    guidance = treated.config / "CLAUDE.md"
    assert guidance.read_bytes() == treated.treatment.guidance[0].read_bytes()
    assert guidance.stat().st_mode & 0o777 == 0o600
    assert receipt["guidance"]["memory_files_tokens"] > 0
    assert treated.calls() == [
        ["plugin", "marketplace", "add", str(treated.staged / "marketplace"), "--json"],
        ["plugin", "install", "demo@pluginbench-frozen", "--json"],
        ["plugin", "list", "--json"],
        claude.discovery_launch(RUN)[0][1:],
    ]
    assert not list((tmp_path / "workspace").iterdir())


def _responses(change: Callable[[dict[str, Any]], object]) -> Callable[[Attempt], object]:
    return lambda attempt: change(attempt.scenario["responses"])


def _commands(attempt: Attempt) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = attempt.scenario["responses"]["initialize"]["commands"]
    return commands


def _extra_memory_file(attempt: Attempt) -> None:
    (attempt.root / "workspace/CLAUDE.md").write_text("Project instructions.\n")
    attempt.scenario["responses"]["get_memory_dialog"]["files"][1]["exists"] = True


def _memory_tokens(responses: dict[str, Any]) -> None:
    for category in responses["get_context_usage"]["categories"]:
        if category["id"] == "memory_files":
            category["tokens"] = 0


def _extra_install_record(attempt: Attempt) -> None:
    records = attempt.config / "plugins/installed_plugins.json"
    records.parent.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(attempt.config / "plugins/cache/x/rogue/1.0.0")}
    records.write_text(json.dumps({"version": 2, "plugins": {"rogue@x": [entry]}}))


def _user_agent(attempt: Attempt) -> None:
    # CLI 2.1.322 loads this and lists it as Explore, in place of the built-in agent.
    (attempt.config / "agents").mkdir()
    (attempt.config / "agents/Explore.md").write_text("---\nname: Explore\ndescription: Other.\n---\n")


STAGED_PLUGIN = "{staged}/marketplace/plugins/demo"

# Each defect and the refusal it must cause; {staged} is the attempt's canonical staged root.
REFUSALS: dict[str, tuple[Callable[[Attempt], object], str]] = {
    "undeclared non-builtin skill": (
        lambda attempt: _commands(attempt).append({"name": "rogue", "description": ""}),
        "Undeclared Claude command or skill discovered: rogue",
    ),
    "duplicate command": (
        lambda attempt: _commands(attempt).append({"name": "loose-skill", "description": ""}),
        "Claude discovered duplicate commands",
    ),
    "missing declared skill": (
        _responses(lambda r: r["get_skills_dialog"]["skills"].pop()),
        "Claude skills dialog differs from the declared skills",
    ),
    "skill off": (
        _responses(lambda r: r["get_skills_dialog"]["skills"][0].update(state="off")),
        "Claude skill is not on from its declared source: demo:demo-skill",
    ),
    "skill from another source": (
        _responses(lambda r: r["get_skills_dialog"]["skills"][0].update(source="user")),
        "Claude skill is not on from its declared source: demo:demo-skill",
    ),
    "undeclared agent": (
        _responses(lambda r: r["initialize"]["agents"].append({"name": "rogue", "description": ""})),
        "Undeclared Claude agent discovered: rogue",
    ),
    "user agent in the private configuration": (_user_agent, "Undeclared Claude user agents exist"),
    "unexpected discovery event": (
        lambda attempt: attempt.scenario.update(stream=[{"type": "system", "subtype": "init"}]),
        "Unexpected Claude discovery event: system/init",
    ),
    "refused environment": (
        lambda attempt: attempt.extra_env.update(ANTHROPIC_BASE_URL="https://proxy.invalid"),
        "Claude treatment installation refuses ANTHROPIC_BASE_URL",
    ),
    "unpinned environment": (
        lambda attempt: attempt.extra_env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="0"),
        "requires the pinned launch environment: CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    ),
    "marketplace add failed": (
        lambda attempt: attempt.scenario.update(outcomes={"marketplace-add": "error"}),
        "Claude plugin marketplace-add did not succeed",
    ),
    "plugin install failed": (
        lambda attempt: attempt.scenario.update(outcomes={"install": "error"}),
        "Claude plugin install did not succeed",
    ),
    "undeclared install record": (
        _extra_install_record,
        "Claude plugin install record lists missing or undeclared plugins",
    ),
    "undeclared listed plugin": (
        lambda attempt: attempt.scenario.update(
            list_extra=[{"id": "rogue@x", "scope": "user", "enabled": True}]
        ),
        "Claude plugin list contains missing or undeclared plugins",
    ),
    "plugin loaded from another folder": (
        lambda attempt: attempt.scenario.update(list_overrides={"readFromFolder": str(attempt.root)}),
        "Claude plugin is not loaded from its staged folder: demo@pluginbench-frozen",
    ),
    "staged plugin bytes differ": (
        lambda attempt: (
            attempt.staged / "marketplace/plugins/demo/skills/demo-skill/check.sh"
        ).write_text("changed\n"),
        f"Installed treatment bytes differ: {STAGED_PLUGIN}/skills/demo-skill/check.sh",
    ),
    "extra memory file": (
        _extra_memory_file,
        "Claude memory files differ from the declared guidance",
    ),
    "guidance tokens zero": (_responses(_memory_tokens), "Claude memory_files tokens (0) disagree"),
    "auto-memory on": (
        _responses(lambda r: r["get_memory_dialog"]["auto_memory"].update(enabled=True)),
        "Claude auto-memory is on",
    ),
    "hook listing mismatch": (
        _responses(lambda r: r["get_hooks_listing"]["hooks"][0].update(commandText="echo other")),
        "Claude hook listing differs from the declared hooks",
    ),
    "hooks not all disabled": (
        _responses(lambda r: r["get_hooks_listing"]["policy"].update(allDisabled=False)),
        "Claude discovery did not disable hooks",
    ),
    "hook event during discovery": (
        lambda attempt: attempt.scenario.update(stream=CAPTURED["stream"]),
        "Claude hook ran during discovery",
    ),
    "nonzero session cost": (
        _responses(
            lambda r: r["get_session_cost"].update(
                text=r["get_session_cost"]["text"].replace(" 0 input", " 12 input")
            )
        ),
        "Claude discovery reported model usage",
    ),
    "version mismatch": (
        _responses(lambda r: r["initialize"].update(claude_code_version="2.1.321")),
        "Claude CLI version 2.1.321 differs from the pinned 2.1.322",
    ),
    "API key source": (
        _responses(
            lambda r: r["initialize"].update(
                account={
                    "tokenSource": "none",
                    "apiKeySource": "ANTHROPIC_API_KEY",
                    "apiProvider": "firstParty",
                }
            )
        ),
        "Claude discovery is not authenticated by the subscription token alone",
    ),
    "API key beside the subscription token": (
        _responses(
            lambda r: r["initialize"].update(
                account={
                    "tokenSource": "CLAUDE_CODE_OAUTH_TOKEN",
                    "apiKeySource": "ANTHROPIC_API_KEY",
                    "apiProvider": "firstParty",
                }
            )
        ),
        "Claude discovery is not authenticated by the subscription token alone",
    ),
    "third-party provider": (
        _responses(
            lambda r: r["initialize"].update(
                account={"tokenSource": "CLAUDE_CODE_OAUTH_TOKEN", "apiProvider": "bedrock"}
            )
        ),
        "Claude discovery is not authenticated by the subscription token alone",
    ),
    "API key in the environment": (
        lambda attempt: attempt.extra_env.update(ANTHROPIC_API_KEY="sk-ant-unwanted"),
        "Claude treatment installation refuses ANTHROPIC_API_KEY",
    ),
    "missing get_hooks_listing response": (
        lambda attempt: attempt.scenario.update(omit=["get_hooks_listing"]),
        "Claude discovery exited before answering get_hooks_listing",
    ),
    "effective settings differ": (
        _responses(lambda r: r["get_settings"]["effective"].update(autoMemoryEnabled=True)),
        "Claude effective settings differ from the pinned settings",
    ),
    "plugin not enabled": (
        lambda attempt: attempt.scenario.update(list_overrides={"enabled": False}),
        "Claude plugin is not enabled in user scope: demo@pluginbench-frozen",
    ),
    "installPath outside cache": (
        lambda attempt: attempt.scenario.update(install_root=str(attempt.config / "elsewhere")),
        "Claude plugin installed outside the private cache: demo@pluginbench-frozen",
    ),
    "tree byte mismatch": (
        lambda attempt: (attempt.config / "skills/loose-folder/check.sh").write_text("changed\n"),
        "Installed treatment bytes differ",
    ),
}


@pytest.mark.parametrize("defect", sorted(REFUSALS))
def test_discovery_refuses_each_receipt_rule(tmp_path: Path, defect: str) -> None:
    treated = attempt(tmp_path)
    change, message = REFUSALS[defect]
    change(treated)
    result = treated.run()
    assert result.returncode != 0
    assert message.format(staged=treated.staged.resolve()) in result.stderr
    assert result.stdout == ""


def test_a_skill_hidden_from_commands_is_verified_from_the_skills_dialog(tmp_path: Path) -> None:
    # CLI 2.1.322 shows a `user-invocable: false` skill on in the dialog, unadvertised, and
    # leaves it out of initialize.commands.
    treated = attempt(tmp_path)
    responses = treated.scenario["responses"]
    responses["initialize"]["commands"] = [
        command for command in _commands(treated) if command["name"] != "loose-skill"
    ]
    for item in responses["get_skills_dialog"]["skills"]:
        item["advertised"] = item["display_name"] != "loose-skill"
    result = treated.run()
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["commands"] == ["demo:demo-skill"]
    assert {item["display_name"]: item["advertised"] for item in receipt["skills"]} == {
        "demo:demo-skill": True,
        "loose-skill": False,
    }


class Environment:
    """Harbor's environment calls, mapped onto the attempt's local directories."""

    def __init__(self, attempt: Attempt, logs: Path) -> None:
        self.attempt = attempt
        self.logs = logs

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        assert target_dir == REMOTE_ROOT
        shutil.rmtree(self.attempt.staged)
        shutil.copytree(source_dir, self.attempt.staged)
        (self.attempt.root / "scenario.json").write_text(json.dumps(self.attempt.scenario))

    async def exec(self, command: str, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == CONFIG_DIR
        mapped = command.replace(REMOTE_ROOT, str(self.attempt.staged))
        mapped = mapped.replace("/logs/agent", str(self.logs))
        mapped = mapped.replace("/workspace", str(self.attempt.root / "workspace"))
        result = subprocess.run(
            ["bash", "-c", mapped],
            cwd=self.attempt.root / "workspace",
            env=self.attempt.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
            check=False,
        )
        return SimpleNamespace(stdout=result.stdout, return_code=result.returncode)

    async def download_file(self, source_path: str, target_path: Path) -> None:
        shutil.copyfile(self.logs / Path(source_path).name, target_path)


@pytest.mark.parametrize("defect", [False, True])
def test_install_keeps_stderr_apart_and_writes_the_receipt(tmp_path: Path, defect: bool) -> None:
    treated = attempt(tmp_path / "attempt")
    if defect:
        treated.scenario["responses"]["initialize"]["claude_code_version"] = "2.1.321"
    for name in ("logs", "artifacts"):
        (tmp_path / name).mkdir()
    env = {**claude.discovery_launch(RUN)[1], "CLAUDE_CONFIG_DIR": CONFIG_DIR}
    invocation = install(
        environment=Environment(treated, tmp_path / "logs"),
        treatment=treated.treatment,
        env=env,
        cwd="/workspace",
        artifacts=tmp_path / "artifacts",
        run=RUN,
        cli_version=VERSION,
    )
    saved = tmp_path / "artifacts/native-treatment.json"
    if defect:
        with pytest.raises(ValueError, match="model call refused"):
            asyncio.run(invocation)
        assert json.loads(saved.read_text())["status"] == "failed"
        assert (
            "differs from the pinned"
            in (tmp_path / "artifacts/native-treatment.stderr").read_text()
        )
    else:
        receipt = asyncio.run(invocation)
        assert receipt["status"] == "verified"
        assert json.loads(saved.read_text()) == receipt
        assert receipt["discovery"]["argv"] == claude.discovery_launch(RUN)[0]
        assert (
            "fake claude CLI warning"
            in (tmp_path / "artifacts/native-treatment.stderr").read_text()
        )
        assert (
            "fake claude CLI warning"
            not in (tmp_path / "artifacts/native-treatment.stdout").read_text()
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"CLAUDE_CONFIG_DIR": "/root/.claude"}, "private CLAUDE_CONFIG_DIR"),
        ({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, "pinned launch environment"),
        ({"ANTHROPIC_BASE_URL": "https://proxy.invalid"}, "refuses ANTHROPIC_BASE_URL"),
        ({"ANTHROPIC_API_KEY": "sk-ant-unwanted"}, "refuses ANTHROPIC_API_KEY"),
    ],
)
def test_install_refuses_an_environment_coding_would_not_use(
    tmp_path: Path,
    change: dict[str, str],
    message: str,
) -> None:
    class Unreachable:
        async def upload_dir(self, **kwargs: object) -> None:
            raise AssertionError("refused before upload")

    env = {**claude.discovery_launch(RUN)[1], "CLAUDE_CONFIG_DIR": CONFIG_DIR, **change}
    with pytest.raises(ValueError, match=message):
        asyncio.run(
            install(
                environment=Unreachable(),
                treatment=Treatment(),
                env=env,
                cwd="/workspace",
                artifacts=tmp_path,
                run=RUN,
                cli_version=VERSION,
            )
        )
