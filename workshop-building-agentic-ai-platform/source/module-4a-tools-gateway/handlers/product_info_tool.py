# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP-compatible Lambda: workshop-product-info-tool.

Returns product details by ID. Used in Module 3 (Register MCP Server step)
and Module 4 (Sync + Gateway target testing).

Handles two MCP methods:
  - tools/list → returns the get_product_info tool definition
  - tools/call → returns product data for the given product_id
"""

import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel("INFO")

# Sample product catalog
PRODUCTS = {
    "PROD-001": {
        "product_id": "PROD-001",
        "name": "Enterprise AI Platform License",
        "description": "Annual license for the Enterprise Agentic AI Platform, includes all modules.",
        "price": 49999.99,
        "currency": "USD",
        "stock_level": 500,
        "category": "Software",
    },
    "PROD-002": {
        "product_id": "PROD-002",
        "name": "GPU Compute Credits (1000 hours)",
        "description": "Pre-paid GPU compute credits for model training and inference workloads.",
        "price": 2500.00,
        "currency": "USD",
        "stock_level": 10000,
        "category": "Compute",
    },
    "PROD-003": {
        "product_id": "PROD-003",
        "name": "Professional Services - Platform Setup",
        "description": "Expert-led deployment and configuration of the agentic AI platform.",
        "price": 15000.00,
        "currency": "USD",
        "stock_level": 20,
        "category": "Services",
    },
    "PROD-004": {
        "product_id": "PROD-004",
        "name": "Data Connector Pack",
        "description": "Pre-built connectors for S3, RDS, DynamoDB, and OpenSearch data sources.",
        "price": 999.99,
        "currency": "USD",
        "stock_level": 1000,
        "category": "Software",
    },
}

TOOL_DEFINITION = {
    "name": "get_product_info",
    "description": "Returns product details for a given product ID including name, description, price, and stock level.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "product_id": {
                "type": "string",
                "description": "The product identifier (e.g., PROD-001)",
            }
        },
        "required": ["product_id"],
    },
}


def _unwrap_event(event):
    """Extract the MCP JSON-RPC body from either gateway envelope or direct invocation.

    The AgentCore Gateway sends tool arguments directly (e.g. {"product_id": "PROD-001"})
    rather than a full MCP JSON-RPC envelope.  We detect this by checking for the
    absence of "method" / "jsonrpc" keys and synthesise a tools/call body so the
    rest of the handler can use a single code-path.
    """
    if isinstance(event, dict) and "mcp" in event and "gatewayRequest" in event.get("mcp", {}):
        body = event["mcp"]["gatewayRequest"].get("body", {})
        if isinstance(body, str):
            body = json.loads(body)
        return body
    # API Gateway / ALB wraps in {"body": "..."}
    if isinstance(event, dict) and isinstance(event.get("body"), str):
        return json.loads(event["body"])
    return event


def handler(event, context):
    """MCP-compatible Lambda handler for product info lookups."""
    # Log method and tool name only — never log full event (may contain auth headers/tokens)
    safe_info = {"method": event.get("method", ""), "has_mcp": "mcp" in event}
    if isinstance(event, dict) and "params" in event:
        safe_info["tool"] = event.get("params", {}).get("name", "")
    logger.info("Request: %s", json.dumps(safe_info))

    body = _unwrap_event(event)

    method = body.get("method", "")
    request_id = body.get("id", 1)

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [TOOL_DEFINITION]},
        }

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "get_product_info":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        product_id = arguments.get("product_id", "")
        product = PRODUCTS.get(product_id)

        if product is None:
            text = f"Product '{product_id}' not found. Available: {', '.join(PRODUCTS.keys())}"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": True},
            }

        text = json.dumps(product, indent=2)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not supported: {method}"},
    }
