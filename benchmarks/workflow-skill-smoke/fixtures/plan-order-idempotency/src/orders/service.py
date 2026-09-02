from uuid import uuid4

from orders.repository import OrderRepository


class OrderService:
    def __init__(self, repository: OrderRepository) -> None:
        self.repository = repository

    def create(self, payload: dict[str, str]) -> dict[str, str]:
        return self.repository.save({"id": str(uuid4()), **payload})
