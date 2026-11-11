"""Reference docs cover live config fields and stay in sync with their code owners."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/gen_docs.py"
SPEC = importlib.util.spec_from_file_location("gen_docs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gen_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen_docs)


def test_every_config_field_has_a_description() -> None:
    fields = [
        field
        for model in (gen_docs.Evaluation, gen_docs.Gates)
        for field in gen_docs.config_fields(model)
    ]
    missing = [
        path
        for path, owner, name, field in fields
        if not gen_docs.description(owner, name, field).strip()
    ]
    assert not missing, f"Config fields need descriptions: {missing}"
    live = {(owner.__name__, name) for _, owner, name, _ in fields}
    stale = [
        f"{model}.{name}"
        for model, descriptions in gen_docs.PINNED_DESCRIPTIONS.items()
        for name in descriptions
        if (model, name) not in live
    ]
    assert not stale, f"Pinned description keys no longer exist: {stale}"


@pytest.mark.parametrize("name", ["config-reference.md", "metrics.md"])
def test_generated_reference_is_current(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the same writer as the command, without changing committed docs.
    (tmp_path / "docs").mkdir()
    original_root = gen_docs.ROOT
    monkeypatch.setattr(gen_docs, "ROOT", tmp_path)
    gen_docs.main()
    assert (original_root / "docs" / name).read_bytes() == (tmp_path / "docs" / name).read_bytes()
