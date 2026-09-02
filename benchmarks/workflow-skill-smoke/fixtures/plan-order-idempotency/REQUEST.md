# Order idempotency request

Add an optional `Idempotency-Key` header to the create-order endpoint.

Store the key with each order. A repeated key must return the original order and must not create another order.

Keep the current behavior when the header is absent. Do not add a dependency.
