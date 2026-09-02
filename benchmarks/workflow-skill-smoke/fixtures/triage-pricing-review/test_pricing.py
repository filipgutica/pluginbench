import unittest
from decimal import Decimal

from pricing import apply_discount


class PricingTest(unittest.TestCase):
    def test_uses_exact_decimal_arithmetic(self) -> None:
        result = apply_discount(Decimal("0.10"), Decimal("33"))

        self.assertIsInstance(result, Decimal)
        self.assertEqual(result, Decimal("0.067"))

    def test_rejects_out_of_range_discount(self) -> None:
        for discount in (Decimal("-1"), Decimal("101")):
            with self.subTest(discount=discount):
                with self.assertRaises(ValueError):
                    apply_discount(Decimal("10"), discount)


if __name__ == "__main__":
    unittest.main()
