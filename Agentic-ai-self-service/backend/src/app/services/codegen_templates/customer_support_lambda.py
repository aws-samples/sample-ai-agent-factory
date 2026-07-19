"""Customer-support demo Lambda (DEPLOYED-CODE TEMPLATE, not app code).

Standalone Gateway Lambda for the customer-support tools schema
(check_order_status / lookup_customer / search_knowledge_base /
get_return_policy). Deployed verbatim by gateway_deployer.
"""

import json


def lambda_handler(event, context):
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "unknown")

    if "check_order_status" in tool_name:
        order_id = event.get("order_id", "").upper()
        orders = {
            "ORD-12345": {
                "order_id": "ORD-12345",
                "status": "Shipped",
                "tracking_number": "1Z999AA10123456784",
                "estimated_delivery": "2025-02-10",
                "items": [
                    {"name": "Laptop Pro 15", "qty": 1, "price": "$1,299.00"},
                    {"name": "USB-C Charger", "qty": 1, "price": "$49.99"},
                ],
                "total": "$1,348.99",
            },
            "ORD-67890": {
                "order_id": "ORD-67890",
                "status": "Processing",
                "tracking_number": None,
                "estimated_delivery": "2025-02-15",
                "items": [
                    {"name": "Wireless Mouse", "qty": 1, "price": "$29.99"},
                    {"name": "Mechanical Keyboard", "qty": 1, "price": "$89.99"},
                ],
                "total": "$119.98",
            },
            "ORD-11111": {
                "order_id": "ORD-11111",
                "status": "Delivered",
                "tracking_number": "1Z999AA10123456785",
                "delivered_date": "2025-01-28",
                "items": [{"name": "27 inch 4K Monitor", "qty": 1, "price": "$449.00"}],
                "total": "$449.00",
            },
            "ORD-22222": {
                "order_id": "ORD-22222",
                "status": "Cancelled",
                "reason": "Customer requested cancellation",
                "refund_status": "Refund processed",
                "items": [{"name": "Noise-Cancelling Headphones", "qty": 1, "price": "$199.00"}],
                "total": "$199.00",
            },
        }
        order = orders.get(order_id)
        if not order:
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "error": f"Order {order_id} not found. Valid demo order IDs: ORD-12345, ORD-67890, ORD-11111, ORD-22222"
                    }
                ),
            }
        return {"statusCode": 200, "body": json.dumps(order)}

    elif "lookup_customer" in tool_name:
        email = event.get("email", "").lower()
        customers = {
            "john@example.com": {
                "name": "John Smith",
                "customer_id": "CUST-001",
                "email": "john@example.com",
                "membership_tier": "Gold",
                "member_since": "2022-03-15",
                "orders": ["ORD-12345", "ORD-11111"],
                "total_spent": "$1,797.99",
            },
            "jane@example.com": {
                "name": "Jane Doe",
                "customer_id": "CUST-002",
                "email": "jane@example.com",
                "membership_tier": "Silver",
                "member_since": "2023-06-20",
                "orders": ["ORD-67890"],
                "total_spent": "$119.98",
            },
            "bob@example.com": {
                "name": "Bob Wilson",
                "customer_id": "CUST-003",
                "email": "bob@example.com",
                "membership_tier": "Platinum",
                "member_since": "2021-01-10",
                "orders": ["ORD-22222"],
                "total_spent": "$4,599.00",
            },
        }
        customer = customers.get(email)
        if not customer:
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "error": f"No customer found with email {email}. Try: john@example.com, jane@example.com, bob@example.com"
                    }
                ),
            }
        return {"statusCode": 200, "body": json.dumps(customer)}

    elif "search_knowledge_base" in tool_name:
        query = event.get("query", "").lower()
        articles = [
            {
                "id": "KB-001",
                "title": "How to Reset Your Account Password",
                "summary": "Go to Settings > Security > Reset Password.",
            },
            {
                "id": "KB-002",
                "title": "Return and Refund Policy",
                "summary": "Items can be returned within 30 days of delivery.",
            },
            {
                "id": "KB-003",
                "title": "Shipping and Delivery Information",
                "summary": "Standard shipping: 5-7 business days.",
            },
            {
                "id": "KB-004",
                "title": "Warranty Coverage Details",
                "summary": "All electronics include 1-year manufacturer warranty.",
            },
            {
                "id": "KB-005",
                "title": "Troubleshooting Blue Screen Errors",
                "summary": "Common causes: outdated drivers, hardware failure.",
            },
            {"id": "KB-006", "title": "How to Track Your Order", "summary": "Log in to your account > My Orders."},
            {
                "id": "KB-007",
                "title": "Membership Tiers and Benefits",
                "summary": "Silver: free standard shipping. Gold: free express.",
            },
        ]
        matches = [a for a in articles if query in a["title"].lower() or query in a["summary"].lower()]
        if not matches:
            matches = articles[:3]
        return {"statusCode": 200, "body": json.dumps({"results": matches, "total_found": len(matches)})}

    elif "get_return_policy" in tool_name:
        category = event.get("product_category", "general").lower()
        policies = {
            "electronics": {
                "category": "Electronics",
                "return_window": "30 days",
                "condition": "Must be in original packaging",
            },
            "accessories": {"category": "Accessories", "return_window": "60 days", "condition": "Must be unused"},
            "software": {"category": "Software", "return_window": "14 days", "condition": "Physical media only"},
            "general": {
                "category": "General",
                "return_window": "30 days",
                "condition": "Item must be in resalable condition",
            },
        }
        policy = policies.get(category, policies["general"])
        return {"statusCode": 200, "body": json.dumps(policy)}

    return {"statusCode": 200, "body": json.dumps({"message": f"Unknown tool: {tool_name}"})}
