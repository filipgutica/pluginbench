from orders.repository import OrderRepository
from orders.service import OrderService


def create_order(payload: dict[str, str], repository: OrderRepository) -> dict[str, str]:
    return OrderService(repository).create(payload)
