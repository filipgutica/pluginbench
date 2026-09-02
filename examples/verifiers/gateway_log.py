import json
from pathlib import Path


expected = {"total": 5, "successful": 2, "errors": 2, "cancelled": 1}
path = Path("summary.json")
if not path.is_file():
    raise SystemExit("summary.json does not exist")

try:
    actual = json.loads(path.read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"summary.json is invalid: {exc}") from exc

if actual != expected:
    raise SystemExit(f"summary.json is incorrect: {actual!r}")
