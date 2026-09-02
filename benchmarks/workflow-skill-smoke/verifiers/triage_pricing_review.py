import importlib.util
import sys
from decimal import Decimal
from pathlib import Path


sys.dont_write_bytecode = True
workspace = Path.cwd()
module_path = workspace / "pricing.py"
spec = importlib.util.spec_from_file_location("pricing_under_test", module_path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load pricing.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

result = module.apply_discount(Decimal("0.10"), Decimal("33"))
if not isinstance(result, Decimal) or result != Decimal("0.067"):
    raise SystemExit("pricing no longer uses exact Decimal arithmetic")
for invalid in (Decimal("-1"), Decimal("101")):
    try:
        module.apply_discount(Decimal("10"), invalid)
    except ValueError:
        pass
    else:
        raise SystemExit(f"discount {invalid} must raise ValueError")
if module.apply_discount(Decimal("10"), Decimal("0")) != Decimal("10"):
    raise SystemExit("the zero-percent boundary is invalid")
if module.apply_discount(Decimal("10"), Decimal("100")) != Decimal("0"):
    raise SystemExit("the 100-percent boundary is invalid")

expected_contract = (
    "# Pricing contract\n\n"
    "Public pricing functions use `Decimal` and exact decimal arithmetic.\n\n"
    "A discount percentage must be between 0 and 100, inclusive.\n"
)
if (workspace / "CONTRACT.md").read_text() != expected_contract:
    raise SystemExit("the pricing contract changed")
