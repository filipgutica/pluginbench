"""Native CLI discovery proofs; no credentials, provider turns, or Docker required."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pluginbench.native_treatments import NativeCapabilities, REMOTE_ROOT, admit, install, stage
from pluginbench.treatments import Treatment


def skill(root: Path, name: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Native qualification fixture.\n---\n"
        "Read resources/example.txt when requested.\n"
    )
    (root / "resources").mkdir()
    (root / "resources/example.txt").write_text("complete resource\n")
    (root / "check.sh").write_text("#!/bin/sh\nprintf 'resource-script-proof'\n")
    (root / "check.sh").chmod(0o755)
    return root


def native_fixture(tmp_path: Path) -> tuple[Treatment, dict[str, str]]:
    if shutil.which("codex") is None or shutil.which("node") is None:
        pytest.skip("Native CLI qualification needs installed Codex and Node")
    for name in ("home", "codex", "workspace"):
        (tmp_path / name).mkdir()
    (tmp_path / "codex/config.toml").write_text("[features]\nplugins=true\nhooks=true\n")
    guide = tmp_path / "instructions.md"
    guide.write_text("Keep the implementation in the existing module.\n")
    standalone = skill(tmp_path / "standalone", "standalone-probe")
    plugin = tmp_path / "plugin"
    skill(plugin / "skills/packaged", "packaged-probe")
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
        "name": "fixture",
        "version": "1.0.0",
        "skills": "./skills",
        "hooks": {"hooks": {"SessionStart": [{"hooks": [{
            "type": "command", "command": "echo native-hook-fixture",
        }]}]}},
    }))
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "XDG_CONFIG_HOME": str(tmp_path / "home/.config"),
    }
    return Treatment(guidance=[guide], skills=[standalone], plugins=[plugin]), env


def run_installer(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(root / "staged/install.cjs"), str(root / "staged"), str(root / "workspace")],
        cwd=root / "workspace", env=env, capture_output=True, text=True, timeout=90,
        check=False,
    )


def test_real_cli_discovers_complete_skills_plugins_hooks_and_global_guidance(tmp_path: Path) -> None:
    treatment, env = native_fixture(tmp_path)
    stage(treatment, tmp_path / "staged")
    # Harbor owns this copy; the installer must verify its resulting bytes/modes.
    installed_skill = tmp_path / "home/.agents/skills/standalone"
    shutil.copytree(treatment.skills[0], installed_skill)
    result = run_installer(tmp_path, env)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "verified"
    custom = [item for item in receipt["skills"]["skills"] if item["scope"] != "system"]
    assert {item["name"] for item in custom} == {"standalone-probe", "fixture:packaged-probe"}
    assert all(item["enabled"] for item in custom)
    assert len(receipt["hooks"]["hooks"]) == 1
    hook = receipt["hooks"]["hooks"][0]
    assert hook["eventName"] == "sessionStart" and hook["source"] == "plugin"
    assert hook["command"] == "echo native-hook-fixture"
    packaged = next(item for item in custom if item.get("pluginId"))
    installed = Path(packaged["path"]).parent
    assert (installed / "resources/example.txt").read_text() == "complete resource\n"
    assert (installed / "check.sh").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "codex/AGENTS.md").read_bytes() == treatment.guidance[0].read_bytes()
    assert not list((tmp_path / "workspace").iterdir())
    assert not list((tmp_path / "codex").rglob("auth.json"))


@pytest.mark.parametrize("defect", ["missing_resource", "ambient_skill"])
def test_installer_refuses_missing_assets_and_undeclared_native_discovery(
    tmp_path: Path, defect: str,
) -> None:
    treatment, env = native_fixture(tmp_path)
    treatment.plugins = []
    stage(treatment, tmp_path / "staged")
    installed = tmp_path / "home/.agents/skills/standalone"
    shutil.copytree(treatment.skills[0], installed)
    if defect == "missing_resource":
        (installed / "resources/example.txt").unlink()
    else:
        skill(tmp_path / "home/.agents/skills/rogue", "undeclared-skill")
    result = run_installer(tmp_path, env)
    assert result.returncode != 0
    assert ("inventory differs" if defect == "missing_resource" else "Undeclared native skill") in result.stderr
    assert not list((tmp_path / "workspace").iterdir())


def test_guidance_limits_prevent_native_truncation_and_other_harness_is_not_emulated(
    tmp_path: Path,
) -> None:
    guidance = tmp_path / "guide.md"
    guidance.write_bytes(b"x" * 32769)
    with pytest.raises(ValueError, match="silent truncation"):
        admit(Treatment(guidance=[guidance]), "codex")
    with pytest.raises(ValueError, match="Codex only"):
        admit(Treatment(), "claude")


@pytest.mark.parametrize("subagents", [False, True])
def test_common_capability_limits_survive_real_harbor_option_parsing(
    tmp_path: Path, subagents: bool,
) -> None:
    from pluginbench import harbor_backend

    sdk = pytest.importorskip("harbor.agents.installed.codex")
    policy = NativeCapabilities(subagents=subagents, max_threads=3, max_depth=1)
    configurations: list[dict[str, Any]] = []
    guidance = tmp_path / "guide.md"
    guidance.write_text("Use the existing module.\n")
    for treatment in (Treatment(), Treatment(guidance=[guidance])):
        config = harbor_backend._native_config(
            task_dir=tmp_path, area=tmp_path, owner="fixture", harness="codex",
            version="0.162.1", limits={"cpus": 1, "memory_mb": 1024}, timeout=30,
            model="gpt-6.1-sol", effort="medium", treatment=treatment, capabilities=policy,
        )
        agent_config = config["agent"]
        assert isinstance(agent_config, dict)
        kwargs = agent_config["kwargs"]
        agent = sdk.Codex(logs_dir=tmp_path, model_name="gpt-6.1-sol", **kwargs)
        assert "--dangerously-bypass-hook-trust" in agent.build_cli_flags()
        effective = agent._build_effective_config()
        configurations.append(effective)
        assert config["extra_instructions"] == []
    assert configurations[0] == configurations[1]
    assert configurations[0]["agents"]["max_threads"] == 3
    assert configurations[0]["agents"]["enabled"] is subagents
    assert "multi_agent_v2" not in configurations[0]["features"]
    assert configurations[0]["features"]["remote_plugin"] is False
    assert configurations[0]["features"]["apps"] is False


@pytest.mark.parametrize("missing_asset", [False, True])
def test_native_install_separates_pty_stderr_and_preserves_failure(
    tmp_path: Path, missing_asset: bool,
) -> None:
    treatment, env = native_fixture(tmp_path)
    installed = tmp_path / "home/.agents/skills/standalone"
    shutil.copytree(treatment.skills[0], installed)
    if missing_asset:
        (installed / "resources/example.txt").unlink()
    logs = tmp_path / "logs"
    logs.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    codex = shutil.which("codex")
    assert codex is not None
    wrapper = binaries / "codex"
    wrapper.write_text(f'#!/bin/sh\nprintf "native CLI warning\\n" >&2\nexec "{codex}" "$@"\n')
    wrapper.chmod(0o755)
    env["PATH"] = str(binaries) + os.pathsep + env["PATH"]

    class Environment:
        async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
            assert target_dir == REMOTE_ROOT
            shutil.copytree(source_dir, tmp_path / "staged")

        async def exec(self, command: str, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            assert kwargs["env"]["CODEX_HOME"] == "/tmp/codex-home"
            mapped = command.replace(REMOTE_ROOT, str(tmp_path / "staged"))
            mapped = mapped.replace("/logs/agent", str(logs))
            mapped = mapped.replace("/workspace", str(tmp_path / "workspace"))
            result = subprocess.run(
                ["bash", "-c", mapped], cwd=tmp_path / "workspace", env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90,
            )
            return SimpleNamespace(stdout=result.stdout, return_code=result.returncode)

        async def download_file(self, source_path: str, target_path: Path) -> None:
            shutil.copyfile(logs / Path(source_path).name, target_path)

    invocation = install(
        environment=Environment(), treatment=treatment,
        env={**env, "CODEX_HOME": "/tmp/codex-home"}, cwd="/workspace", artifacts=artifacts,
    )
    if missing_asset:
        with pytest.raises(ValueError, match="model call refused"):
            asyncio.run(invocation)
        receipt = json.loads((artifacts / "native-treatment.json").read_text())
        assert receipt["status"] == "failed"
        assert "inventory differs" in (artifacts / "native-treatment.stderr").read_text()
    else:
        receipt = asyncio.run(invocation)
        assert receipt["status"] == "verified"
        assert "native CLI warning" in (artifacts / "native-treatment.stderr").read_text()
        assert "native CLI warning" not in (artifacts / "native-treatment.stdout").read_text()


def test_signaled_discovery_process_fails_promptly(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("Native installer needs Node")
    for name in ("home", "codex", "workspace", "bin"):
        (tmp_path / name).mkdir()
    (tmp_path / "codex/config.toml").write_text("")
    binary = tmp_path / "bin/codex"
    binary.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        'plugin) printf \'{"installed": []}\\n\' ;;\n'
        'app-server) IFS= read -r request; kill -TERM "$$" ;;\n'
        '*) exit 1 ;;\nesac\n'
    )
    binary.chmod(0o755)
    stage(Treatment(), tmp_path / "staged")
    result = subprocess.run(
        ["node", str(tmp_path / "staged/install.cjs"), str(tmp_path / "staged"),
         str(tmp_path / "workspace")],
        cwd=tmp_path / "workspace",
        env={
            "PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", os.defpath),
            "HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "codex"),
        },
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode != 0
    assert "Native discovery exited: null" in result.stderr
