# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Register the AgentCore Gateway itself in the Module 3 Registry.

The gateway becomes a discoverable MCP server — other agents can find it
via semantic search and choose to route tool calls through the governed path.
This is the composability payoff: the gateway is infrastructure AND a service.
"""

import json
import logging

logger = logging.getLogger(__name__)


def register_gateway_in_registry(
    registry_client,
    gateway_url: str,
    gateway_id: str,
    name: str = "agentcore-gateway",
    description: str = (
        "Governed MCP endpoint with enterprise guardrails, audit logging, "
        "and Lambda-native tool targets. Routes through AgentCore Gateway "
        "for policy enforcement on tool calls."
    ),
) -> dict:
    """Register the AgentCore Gateway as an MCP server in the Registry.

    After this registration:
    - The gateway appears in the Registry UI alongside other MCP servers
    - Semantic search for "governed endpoint" or "guardrails" finds it
    - Module 5 agents can discover and choose it as their tool endpoint
    """
    return registry_client.register_server({
        "name": name,
        "description": description,
        "path": f"/mcp/{name}",
        "proxy_pass_url": gateway_url,
        "tags": "agentcore,gateway,governed,enterprise,guardrails",
        "tool_list_json": json.dumps([{
            "name": "tools_via_gateway",
            "description": (
                "Access all registered tools through the governed AgentCore Gateway. "
                "Includes Bedrock Guardrails on outputs, CloudWatch audit trail, "
                "and Lambda-native tool dispatch."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Name of the tool to invoke"},
                    "arguments": {"type": "object", "description": "Tool arguments"},
                },
                "required": ["tool_name"],
            },
        }]),
        "supported_transports": json.dumps(["mcp-streamable-http"]),
        "status": "active",
        "mcp_endpoint": gateway_url,
        "metadata": json.dumps({
            "gateway_id": gateway_id,
            "auth_type": "CUSTOM_JWT",
            "features": ["guardrails", "audit", "lambda-native", "semantic-search"],
        }),
    })
