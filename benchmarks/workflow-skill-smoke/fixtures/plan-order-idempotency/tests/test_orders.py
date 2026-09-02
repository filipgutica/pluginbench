import unittest

from orders.api import create_order
from orders.repository import OrderRepository


class OrderTest(unittest.TestCase):
    def test_creates_an_order(self) -> None:
        repository = OrderRepository()

        order = create_order({"item": "book"}, repository)

        self.assertEqual(order["item"], "book")
        self.assertEqual(len(repository.orders), 1)


if __name__ == "__main__":
    unittest.main()
