"""Gateway tool Lambda dispatcher (DEPLOYED-CODE TEMPLATE, not app code).

Routes ``bedrockAgentCoreToolName`` to the canonical tool implementations.
Composed after ``dynamic_tools_impl`` + ``customer_support_impl`` into one
self-contained Lambda module (see ``codegen_templates.dynamic_tools_lambda_source``).
"""

# --- template-only imports (stripped when rendered) ---
import json

from app.services.codegen_templates.customer_support_impl import (
    _do_get_customer,
    _do_get_order,
    _do_list_orders,
    _do_process_refund,
)
from app.services.codegen_templates.dynamic_tools_impl import (
    ToolUnavailable,
    _do_duckduckgo_search,
    _do_fetch_webpage,
    _do_weather,
    _do_wikipedia_search,
)

# --- end template-only imports ---


def lambda_handler(event, context):
    tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "unknown")
    try:
        if "duckduckgo_search" in tool_name:
            return {"statusCode": 200, "body": _do_duckduckgo_search(event.get("query", ""))}
        elif "wikipedia_search" in tool_name:
            return {"statusCode": 200, "body": _do_wikipedia_search(event.get("query", ""))}
        elif "weather_api" in tool_name or "get_weather" in tool_name:
            return {"statusCode": 200, "body": _do_weather(event.get("location", ""))}
        elif "web_page_fetcher" in tool_name or "fetch_webpage" in tool_name:
            return {"statusCode": 200, "body": _do_fetch_webpage(event.get("url", ""))}
        elif "get_order" in tool_name and "list_orders" not in tool_name:
            return {"statusCode": 200, "body": _do_get_order(event)}
        elif "get_customer" in tool_name:
            return {"statusCode": 200, "body": _do_get_customer(event)}
        elif "list_orders" in tool_name:
            return {"statusCode": 200, "body": _do_list_orders(event)}
        elif "process_refund" in tool_name:
            return {"statusCode": 200, "body": _do_process_refund(event)}
        else:
            return {"statusCode": 200, "body": json.dumps({"error": f"Unknown tool: {tool_name}"})}
    except ToolUnavailable as e:
        # External dependency unreachable after retries — return a STRUCTURED
        # error so the agent + tests can tell "tool failed" from "no results".
        return {
            "statusCode": 200,
            "body": json.dumps({"error": "tool_unavailable", "detail": str(e), "tool": tool_name}),
        }
    except Exception as e:
        return {"statusCode": 200, "body": json.dumps({"error": str(e)})}
