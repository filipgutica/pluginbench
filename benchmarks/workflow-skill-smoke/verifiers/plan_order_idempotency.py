from pathlib import Path


workspace = Path.cwd()
fixture = Path(__file__).resolve().parents[1] / "fixtures" / "plan-order-idempotency"


def ignored_generated_file(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


for original in fixture.rglob("*"):
    if not original.is_file():
        continue
    relative = original.relative_to(fixture)
    candidate = workspace / relative
    if not candidate.is_file() or candidate.read_bytes() != original.read_bytes():
        raise SystemExit(f"existing file changed: {relative.as_posix()}")

allowed_roots = {path.name for path in fixture.iterdir()}
for candidate in workspace.rglob("*"):
    if not candidate.is_file():
        continue
    relative = candidate.relative_to(workspace)
    if (
        relative == Path("PLAN.md")
        or relative.parts[0] == ".agents"
        or ignored_generated_file(relative)
    ):
        continue
    if relative.parts[0] not in allowed_roots or not (fixture / relative).is_file():
        raise SystemExit(f"unexpected file created: {relative.as_posix()}")

plan_path = workspace / "PLAN.md"
if not plan_path.is_file():
    raise SystemExit("PLAN.md does not exist")
plan = plan_path.read_text()

required_sections = (
    "## Context",
    "## Goal",
    "## Non-goals",
    "## Success criteria",
    "## Bounded subtasks",
    "## Files touched",
    "## Verification commands",
    "## Risks / assumptions / open questions",
)
positions = [plan.find(section) for section in required_sections]
if any(position < 0 for position in positions) or positions != sorted(positions):
    raise SystemExit("PLAN.md lacks the required ordered sections")
if "Source: [REQUEST.md](REQUEST.md)" not in plan:
    raise SystemExit("PLAN.md does not identify REQUEST.md as its source")

required_evidence = (
    "src/orders/api.py",
    "src/orders/service.py",
    "src/orders/repository.py",
    "tests/test_orders.py",
    "PYTHONPATH=src python3 -m unittest discover -s tests -v",
)
missing = [value for value in required_evidence if value not in plan]
if missing:
    raise SystemExit("PLAN.md lacks repository evidence: " + ", ".join(missing))
if str(Path.home()) in plan:
    raise SystemExit("PLAN.md contains a machine-local home path")
