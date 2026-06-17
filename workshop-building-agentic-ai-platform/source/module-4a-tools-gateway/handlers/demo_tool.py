# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Demo MCP tool Lambda: search-knowledge-base.

A simple Lambda that responds to MCP protocol calls (tools/list and tools/call)
via the AgentCore Gateway. Used by Notebook 05 to demonstrate Lambda-backed
tools accessible only through Path B.

In Module 5, this placeholder is replaced by a real Bedrock Knowledge Base
Retrieve API integration.
"""

import json
import logging

logger = logging.getLogger(__name__)

# Sample product knowledge base (replaced by real KB in Module 5)
_KNOWLEDGE_BASE = [
    {
        "title": "Return Policy",
        "content": "Items can be returned within 30 days of purchase with original receipt. "
        "Electronics must be unopened. Clothing can be tried on but must have tags attached.",
        "tags": ["returns", "policy", "refund"],
    },
    {
        "title": "Shipping Information",
        "content": "Standard shipping takes 5-7 business days. Express shipping takes 1-2 business days. "
        "Free shipping on orders over $50. International shipping available to select countries.",
        "tags": ["shipping", "delivery", "international"],
    },
    {
        "title": "Product Warranty",
        "content": "All electronics come with a 1-year manufacturer warranty. Extended warranty "
        "available for purchase. Warranty covers defects in materials and workmanship.",
        "tags": ["warranty", "electronics", "coverage"],
    },
    {
        "title": "Account Management",
        "content": "Customers can manage their account at account.example.com. Password resets "
        "are available via email. Two-factor authentication is recommended for all accounts.",
        "tags": ["account", "security", "password"],
    },
]


def _search(query: str, max_results: int = 3) -> list[dict]:
    """Simple keyword search over the sample knowledge base."""
    query_lower = query.lower()
    scored = []
    for entry in _KNOWLEDGE_BASE:
        score = 0
        for word in query_lower.split():
            if word in entry["title"].lower():
                score += 2
            if word in entry["content"].lower():
                score += 1
            if word in " ".join(entry["tags"]):
                score += 3
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:max_results]]


def _unwrap_event(event):
    """Extract the MCP JSON-RPC body from either gateway envelope or direct invocation.

    The AgentCore Gateway sends tool arguments directly (e.g. {"query": "..."})
    rather than a full MCP JSON-RPC envelope.  We detect this by checking for the
    absence of "method" / "jsonrpc" keys and synthesise a tools/call body.
    """
    if isinstance(event, str):
        event = json.loads(event)
    # AgentCore Gateway wraps requests in {"mcp": {"gatewayRequest": {"body": ...}}}
    if "mcp" in event and "gatewayRequest" in event.get("mcp", {}):
        body = event["mcp"]["gatewayRequest"].get("body", {})
        if isinstance(body, str):
            body = json.loads(body)
        return body
    return event


def handler(event, context):
    """MCP protocol handler for AgentCore Gateway Lambda targets.

    Handles:
    - tools/list: Returns available tool definitions
    - tools/call: Executes the search-knowledge-base tool
    """
    body = _unwrap_event(event)
    method = body.get("method", "")
    req_id = body.get("id", "")

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "search-knowledge-base",
                        "description": "Search the enterprise knowledge base for relevant information. "
                        "Returns matching documents based on keyword relevance.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Search query (keywords or natural language)",
                                },
                                "max_results": {
                                    "type": "integer",
                                    "description": "Maximum number of results to return (default: 3)",
                                    "default": 3,
                                },
                            },
                            "required": ["query"],
                        },
                    }
                ]
            },
        }

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "search-knowledge-base":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        query = arguments.get("query", "")
        max_results = arguments.get("max_results", 3)

        if not query:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Missing required parameter: query"},
            }

        results = _search(query, max_results)

        if not results:
            text = f"No results found for '{query}'."
        else:
            parts = [f"Found {len(results)} result(s) for '{query}':\n"]
            for i, r in enumerate(results, 1):
                parts.append(f"{i}. **{r['title']}**\n   {r['content']}\n")
            text = "\n".join(parts)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }
