"""Customer-support demo data + handlers (DEPLOYED-CODE TEMPLATE, not app code).

CUSTOMERS/ORDERS/REFUNDS mock data and the get_order / get_customer /
list_orders / process_refund tool handlers used by the Gateway dynamic-tools
Lambda and the CFN-exported customer-support tools Lambda.
"""

import json

CUSTOMERS = {
    "CUST-001": {
        "customer_id": "CUST-001",
        "name": "John Doe",
        "email": "john@example.com",
        "member_since": "2023-06-01",
    },
    "CUST-002": {
        "customer_id": "CUST-002",
        "name": "Jane Smith",
        "email": "jane@example.com",
        "member_since": "2024-01-15",
    },
}

ORDERS = {
    "ORD-12345": {
        "order_id": "ORD-12345",
        "customer_id": "CUST-001",
        "status": "delivered",
        "items": [{"name": "Wireless Headphones", "quantity": 1, "price": 79.99}],
        "total": 79.99,
        "order_date": "2025-01-15",
        "delivery_date": "2025-01-20",
    },
    "ORD-12300": {
        "order_id": "ORD-12300",
        "customer_id": "CUST-001",
        "status": "delivered",
        "items": [{"name": "Running Shoes", "quantity": 1, "price": 249.00}],
        "total": 249.00,
        "order_date": "2025-01-02",
        "delivery_date": "2025-01-08",
    },
    "ORD-12400": {
        "order_id": "ORD-12400",
        "customer_id": "CUST-001",
        "status": "delivered",
        "items": [{"name": "USB-C Charging Cable", "quantity": 2, "price": 12.99}],
        "total": 25.98,
        "order_date": "2025-01-20",
        "delivery_date": "2025-01-23",
    },
    "ORD-99000": {
        "order_id": "ORD-99000",
        "customer_id": "CUST-002",
        "status": "delivered",
        "items": [{"name": "Premium Laptop", "quantity": 1, "price": 1299.00}],
        "total": 1299.00,
        "order_date": "2025-01-10",
        "delivery_date": "2025-01-15",
    },
    "ORD-99010": {
        "order_id": "ORD-99010",
        "customer_id": "CUST-002",
        "status": "delivered",
        "items": [{"name": "Yoga Mat", "quantity": 1, "price": 45.00}],
        "total": 45.00,
        "order_date": "2025-01-18",
        "delivery_date": "2025-01-21",
    },
}

REFUNDS = {}


def _do_get_order(event):
    order_id = event.get("order_id", "")
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found. Valid IDs: {', '.join(ORDERS.keys())}"})
    return json.dumps(order)


def _do_get_customer(event):
    customer_id = event.get("customer_id", "")
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return json.dumps({"error": f"Customer {customer_id} not found. Valid IDs: {', '.join(CUSTOMERS.keys())}"})
    customer_orders = [o for o in ORDERS.values() if o["customer_id"] == customer_id]
    return json.dumps(
        {
            **customer,
            "total_orders": len(customer_orders),
            "total_spent": round(sum(o["total"] for o in customer_orders), 2),
        }
    )


def _do_list_orders(event):
    customer_id = event.get("customer_id", "")
    limit = event.get("limit", 10)
    if customer_id not in CUSTOMERS:
        return json.dumps({"error": f"Customer {customer_id} not found"})
    orders = [
        {"order_id": o["order_id"], "total": o["total"], "status": o["status"], "order_date": o["order_date"]}
        for o in ORDERS.values()
        if o["customer_id"] == customer_id
    ]
    orders.sort(key=lambda x: x["order_date"], reverse=True)
    return json.dumps({"customer_id": customer_id, "orders": orders[:limit]})


def _do_process_refund(event):
    import uuid as _uuid

    order_id = event.get("order_id", "")
    amount = event.get("amount")
    reason = event.get("reason", "")
    order = ORDERS.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found"})
    if amount is None or amount <= 0:
        return json.dumps({"error": "Refund amount must be positive"})
    if amount > order["total"]:
        return json.dumps({"error": f"Refund amount ${amount} exceeds order total ${order['total']}"})
    refund_id = f"REF-{_uuid.uuid4().hex[:5].upper()}"
    return json.dumps(
        {
            "success": True,
            "refund_id": refund_id,
            "order_id": order_id,
            "amount": amount,
            "reason": reason,
            "status": "processed",
            "message": f"Refund of ${amount:.2f} processed. Customer will receive funds in 3-5 business days.",
        }
    )
