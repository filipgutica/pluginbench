from decimal import Decimal


def apply_discount(amount: Decimal, discount_percent: Decimal) -> Decimal:
    return amount * (Decimal("100") - discount_percent) / Decimal("100")
