# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AgentCore Gateway Lambda Target: Documentation server.

Runtime contract (AgentCore Lambda target):
  - ``event`` is the FLAT tool-arguments dict (e.g. {"pageId": "arch-overview"}),
    NOT a JSON-RPC envelope.
  - The invoked tool name is provided via
    ``context.client_context.custom['bedrockAgentCoreToolName']`` formatted as
    ``DocsAPI___<tool_name>`` -- split on the triple underscore to recover it.
  - Return the result DIRECTLY (e.g. {"content": [{"type": "text", ...}]}) with
    NO statusCode/body wrapper.
  - ``tools/list`` is served by the gateway from the tool schema and never
    reaches this Lambda.
"""
import json

SAMPLE_PAGES = {
    "arch-overview": {
        "title": "Architecture Overview",
        "content": "System uses microservices behind an API gateway with async messaging.",
    },
    "runbook-deploy": {
        "title": "Deployment Runbook",
        "content": "Step 1: Run pipeline. Step 2: Canary 10%. Step 3: Full rollout.",
    },
    "api-reference": {
        "title": "API Reference",
        "content": "GET /users - returns user list. POST /users - creates a user.",
    },
}

SAMPLE_SPACES = ["engineering", "data-science", "platform"]


def _text(text: str) -> dict:
    """Wrap a string in the MCP text-content result envelope."""
    return {"content": [{"type": "text", "text": text}]}


def _resolve_tool_name(context) -> str:
    """Extract the bare tool name from the AgentCore client context."""
    custom = {}
    client_context = getattr(context, "client_context", None)
    if client_context is not None:
        raw = getattr(client_context, "custom", None)
        if isinstance(raw, str):
            try:
                custom = json.loads(raw or "{}")
            except json.JSONDecodeError:
                custom = {}
        elif isinstance(raw, dict):
            custom = raw
    full_tool_name = custom.get("bedrockAgentCoreToolName", "___unknown")
    return full_tool_name.split("___", 1)[-1]


def handler(event, context):
    tool_name = _resolve_tool_name(context)
    args = event if isinstance(event, dict) else {}

    if tool_name == "get_page":
        page = SAMPLE_PAGES.get(
            args.get("pageId", ""), {"title": "Not Found", "content": ""}
        )
        return _text(f"# {page['title']}\n\n{page['content']}")

    if tool_name == "search_pages":
        query = str(args.get("query", "")).lower()
        results = [
            {"title": p["title"]}
            for p in SAMPLE_PAGES.values()
            if query in p["title"].lower() or query in p["content"].lower()
        ]
        return _text(json.dumps(results))

    if tool_name == "list_spaces":
        return _text(json.dumps(SAMPLE_SPACES))

    if tool_name == "create_page":
        return _text(f"Created page: {args.get('title', 'Untitled')}")

    if tool_name == "update_page":
        return _text(f"Updated page: {args.get('pageId', 'unknown')}")

    if tool_name == "delete_page":
        return _text(f"Deleted page: {args.get('pageId', 'unknown')}")

    return _text(f"Unknown tool: {tool_name}")
