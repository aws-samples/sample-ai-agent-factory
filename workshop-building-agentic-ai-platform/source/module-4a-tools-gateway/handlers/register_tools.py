# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helper to register Lambda/OpenAPI tools in the Module 3 Registry.

Used in notebooks to register tools that only AgentCore Gateway can serve
(Lambda-native targets, external APIs) — extending the Registry catalog
beyond what NGINX can proxy.
"""

import json
import logging

logger = logging.getLogger(__name__)


def register_lambda_tool(
    registry_client,
    name: str,
    description: str,
    lambda_arn: str,
    tool_schema: list[dict] | None = None,
    tags: str = "lambda,agentcore-target",
) -> dict:
    """Register a Lambda-backed tool in the Registry.

    The tool is stored with proxy_pass_url = lambda://<arn> so that the
    Sync Lambda can create a native Lambda target in AgentCore Gateway.
    NGINX cannot route lambda:// URLs, making this a Gateway-only tool.
    """
    if not tool_schema:
        tool_schema = [{
            "name": name,
            "description": description,
            "inputSchema": {"type": "object"},
        }]

    return registry_client.register_server({
        "name": name,
        "description": description,
        "path": f"/tools/{name}",
        "proxy_pass_url": f"lambda://{lambda_arn}",
        "tags": tags,
        "tool_list_json": json.dumps(tool_schema),
        "supported_transports": json.dumps(["agentcore-lambda"]),
        "status": "active",
    })


def register_openapi_tool(
    registry_client,
    name: str,
    description: str,
    api_url: str,
    tool_schema: list[dict] | None = None,
    tags: str = "openapi,agentcore-target",
) -> dict:
    """Register an external API tool in the Registry.

    The tool is stored with the actual HTTP URL. AgentCore Gateway
    creates an HTTP target for it. NGINX could also proxy it, but
    the Gateway adds guardrails and audit.
    """
    if not tool_schema:
        tool_schema = [{
            "name": name,
            "description": description,
            "inputSchema": {"type": "object"},
        }]

    return registry_client.register_server({
        "name": name,
        "description": description,
        "path": f"/tools/{name}",
        "proxy_pass_url": api_url,
        "tags": tags,
        "tool_list_json": json.dumps(tool_schema),
        "supported_transports": json.dumps(["agentcore-http"]),
        "status": "active",
    })
