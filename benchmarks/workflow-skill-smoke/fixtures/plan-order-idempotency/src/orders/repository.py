class OrderRepository:
    def __init__(self) -> None:
        self.orders: list[dict[str, str]] = []

    def save(self, order: dict[str, str]) -> dict[str, str]:
        self.orders.append(order)
        return order
