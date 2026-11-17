"""Two offline coverage distributions, pinned independently of the task image."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import zipfile

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pluginbench import components

ARTIFACTS = Path(__file__).with_name("coverage_tools")
CONTAINER_ROOT = "/opt/pluginbench-tools"
# c8 includes its complete production dependency tree, not just the npm package tarball.
CATALOG = {
    ("coverage.py", "7.6.12"): (
        "coverage-7.6.12-py3-none-any.whl",
        "eb8668cfbc279a536c633137deeb9435d2962caec279c3f8cf8b91fff6ff8953",
    ),
    ("c8", "10.1.3"): (
        "c8-10.1.3.tar.gz",
        "0a5a1754d8f1ce9f9a7b72d9bef328ab5aba560bbee4a0aac398606c77ffb27a",
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


def artifact_bytes(tool: ProvisionedTool) -> bytes:
    """Verify before runtime checks, and verify the exact bytes unpacked for Docker cp."""
    filename, expected = CATALOG[(tool.name, tool.version)]
    try:
        content = (ARTIFACTS / filename).read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"missing artifact for provisioned tool {tool.name}: {filename}") from error
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f"provisioned tool {tool.name} artifact SHA-256 mismatch")
    return content


def read_tool(value: object) -> ProvisionedTool | None:
    """Revalidate the saved pins, including when passed an already constructed model."""
    if value is None:
        return None
    return ProvisionedTool.model_validate(
        value.model_dump() if isinstance(value, ProvisionedTool) else value
    )


@contextmanager
def staged_tool(tool: ProvisionedTool) -> Iterator[Path]:
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
        yield root
