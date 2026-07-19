"""Customer-support tools Lambda dispatcher (DEPLOYED-CODE TEMPLATE, not app code).

Routes ``bedrockAgentCoreToolName`` to the canonical customer-support handlers
(get_order / get_customer / list_orders / process_refund). Composed after
``customer_support_impl`` into a standalone Lambda module for CFN bundles
(see ``codegen_templates.customer_support_tools_lambda_source``).
"""

# --- template-only imports (stripped when rendered) ---
import json

from app.services.codegen_templates.customer_support_impl import (
    _do_get_customer,
    _do_get_order,
    _do_list_orders,
    _do_process_refund,
)

# --- end template-only imports ---


def lambda_handler(event, context):
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "unknown")
    try:
        if "get_order" in tool_name and "list_orders" not in tool_name:
            return {"statusCode": 200, "body": _do_get_order(event)}
        elif "get_customer" in tool_name:
            return {"statusCode": 200, "body": _do_get_customer(event)}
        elif "list_orders" in tool_name:
            return {"statusCode": 200, "body": _do_list_orders(event)}
        elif "process_refund" in tool_name:
            return {"statusCode": 200, "body": _do_process_refund(event)}
        else:
            return {"statusCode": 200, "body": json.dumps({"error": f"Unknown tool: {tool_name}"})}
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"error": str(e)})}
