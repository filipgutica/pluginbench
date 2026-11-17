"""Two offline coverage distributions, pinned independently of the task image."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import zipfile
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench import components
from pluginbench.code_diff import _SOURCE_SUFFIXES, _is_source_path

ARTIFACTS = Path(__file__).with_name("coverage_tools")
CONTAINER_ROOT = "/opt/pluginbench-tools"
SUPPORT_FILES = {
    "coverage.py": ("launch-coverage.py", "coverage-config.ini"),
    "c8": ("launch-c8.cjs", "c8-config.json"),
}
# c8 includes its complete production dependency tree, not just the npm package tarball.
CATALOG = {
    ("coverage.py", "7.6.12"): (
        "coverage-7.6.12-py3-none-any.whl",
        "eb8668cfbc279a536c633137deeb9435d2962caec279c3f8cf8b91fff6ff8953",
    ),
    ("c8", "10.1.3"): (
        "c8-10.1.3.tar.gz",
        "d374743193106494d1b05b0e13eff115c66575f0652a9a876faf02359a299c1a",
    ),
}


class ProvisionedTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Selects the vendored coverage.py or c8 tool.")
    version: str = Field(description="Pins an exact release supplied by PluginBench.")
    # Operators declare name/version. Preparation fills these pins and saved profiles retain them.
    sha256: str = Field(
        default="", description="Records the verified artifact SHA-256; filled when omitted."
    )
    provisioner_version: int = Field(
        default=components.VERSIONS["measurement_tools"],
        description="Pins the PluginBench measurement_tools component that provisions the tool.",
    )

    @model_validator(mode="after")
    def pinned_artifact(self) -> ProvisionedTool:
        if (self.name, self.version) not in CATALOG:
            raise ValueError(f"unsupported provisioned tool: {self.name} {self.version}")
        _, expected = CATALOG[(self.name, self.version)]
        if self.sha256 and self.sha256 != expected:
            raise ValueError("provisioned tool SHA-256 differs from the pinned artifact")
        if self.provisioner_version != components.VERSIONS["measurement_tools"]:
            raise ValueError("provisioned tool requires another measurement_tools version")
        object.__setattr__(self, "sha256", expected)
        artifact_bytes(self)
        return self


class ProvisionedCommandOptions(TypedDict, total=False):
    provisioned_tool: ProvisionedTool
    provisioned_scope: list[str] | None


def artifact_bytes(tool: ProvisionedTool) -> bytes:
    """Verify before runtime checks, and verify the exact bytes unpacked for Docker cp."""
    filename, expected = CATALOG[(tool.name, tool.version)]
    try:
        content = (ARTIFACTS / filename).read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"missing artifact for provisioned tool {tool.name}: {filename}") from error
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f"provisioned tool {tool.name} artifact SHA-256 mismatch")
    for name in SUPPORT_FILES[tool.name]:
        if not (ARTIFACTS / name).is_file():
            raise ValueError(f"missing artifact for provisioned tool {tool.name}: {name}")
    return content


def read_tool(value: object) -> ProvisionedTool | None:
    """Revalidate the saved pins, including when passed an already constructed model."""
    if value is None:
        return None
    return ProvisionedTool.model_validate(
        value.model_dump() if isinstance(value, ProvisionedTool) else value
    )


def collection_files(directory: Path, tool: ProvisionedTool) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("coverage collection requires a regular raw data directory")
    files = sorted(directory.iterdir())
    if not files or any(
        file.is_symlink() or not file.is_file()
        or (file.name != ".coverage" if tool.name == "coverage.py" else file.suffix != ".json")
        for file in files
    ):
        raise ValueError("coverage collection requires only regular raw data files")
    return files


@contextmanager
def staged_tool(
    tool: ProvisionedTool, *, source_files: list[str] | None = None, data: Path | None = None
) -> Iterator[Path]:
    content = artifact_bytes(tool)
    with tempfile.TemporaryDirectory(prefix="pluginbench-tool-") as directory:
        root = Path(directory)
        target = root / tool.name
        target.mkdir()
        if tool.name == "coverage.py":
            with zipfile.ZipFile(io.BytesIO(content)) as wheel:
                wheel.extractall(target)
        else:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                archive.extractall(target, filter="data")
        for name in SUPPORT_FILES[tool.name]:
            (target / name).write_bytes((ARTIFACTS / name).read_bytes())
        (target / "source-files.json").write_text(json.dumps(source_files))
        extensions = _SOURCE_SUFFIXES if source_files is None else {
            Path(name).suffix for name in source_files if _is_source_path(name)
        }
        (target / "source-extensions.json").write_text(json.dumps(sorted(extensions)))
        if data is not None:
            (target / "data").mkdir()
            for file in collection_files(data, tool):
                (target / "data" / file.name).write_bytes(file.read_bytes())
        yield root
