# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Gateway target synchronization service.

Reads MCP servers from the Module 3 Registry and maps them to
AgentCore Gateway target configurations (Lambda, HTTP/MCP).
"""

import json
import logging
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# URL patterns that should never be used as gateway targets
_BLOCKED_URL_PATTERNS = [
    r"^https?://localhost",
    r"^https?://127\.",
    r"^https?://0\.0\.0\.0",
    r"^https?://169\.254\.",
    r"^https?://10\.",
    r"^https?://172\.(1[6-9]|2[0-9]|3[01])\.",
    r"^https?://192\.168\.",
    r"^https?://\[?::1\]?",           # IPv6 loopback
    r"^https?://\[?fd[0-9a-f]{2}:",   # IPv6 ULA (fd00::/8)
    r"^https?://\[?fe80:",            # IPv6 link-local
]


class GatewaySyncService:
    """Maps Registry server entries to AgentCore Gateway targets."""

    def __init__(self, region: str = "us-west-2"):
        self._client = boto3.client("bedrock-agentcore-control", region_name=region)
        self._region = region

    def build_target_config(self, server: dict) -> dict | None:
        """Map a Registry server entry to an AgentCore Gateway target config.

        Args:
            server: A server dict from the Registry API with keys like
                    server_name/name, proxy_pass_url, tags, tool_list, etc.

        Returns:
            Target connection configuration dict, or None if unknown type.

        Raises:
            ValueError: If the target config is invalid (bad ARN, blocked URL).
        """
        proxy_url = server.get("proxy_pass_url", "")
        tags = server.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        tool_list = server.get("tool_list", [])
        if isinstance(tool_list, str):
            try:
                tool_list = json.loads(tool_list)
            except (json.JSONDecodeError, TypeError):
                tool_list = []

        name = server.get("display_name") or server.get("server_name") or server.get("name", "")

        # Lambda target: proxy_pass_url = lambda://arn:aws:lambda:...
        if proxy_url.startswith("lambda://"):
            lambda_arn = proxy_url.replace("lambda://", "")
            if not lambda_arn.startswith("arn:aws:lambda:"):
                raise ValueError(f"Invalid Lambda ARN: {lambda_arn}")
            # Each inlinePayload entry requires: name, description, inputSchema.type
            _ALLOWED_SCHEMA_KEYS = {"type", "properties", "required", "items", "description"}

            def _sanitize_schema(schema):
                if not isinstance(schema, dict):
                    return {"type": "object"}
                result = {}
                for k, v in schema.items():
                    if k not in _ALLOWED_SCHEMA_KEYS:
                        continue
                    if k == "properties" and isinstance(v, dict):
                        result[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
                    elif k == "items" and isinstance(v, dict):
                        result[k] = _sanitize_schema(v)
                    else:
                        result[k] = v
                return result

            normalized_tools = []
            for t in (tool_list or [{"name": name}]):
                normalized_tools.append({
                    "name": t.get("name", name),
                    "description": t.get("description", f"Tool from {name}"),
                    "inputSchema": _sanitize_schema(t.get("inputSchema", {"type": "object"})),
                })
            if not normalized_tools:
                normalized_tools = [{
                    "name": name,
                    "description": f"Tool provided by {name}",
                    "inputSchema": {"type": "object"},
                }]

            return {
                "name": name,
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "lambdaArn": lambda_arn,
                            "toolSchema": {
                                "inlinePayload": normalized_tools,
                            }
                        }
                    }
                },
                "credentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            }

        # MCP Server target: regular URL
        if proxy_url.startswith("http://") or proxy_url.startswith("https://"):
            # Security: block internal/private URLs
            for pattern in _BLOCKED_URL_PATTERNS:
                if re.match(pattern, proxy_url, re.IGNORECASE):
                    raise ValueError(f"Blocked URL pattern: {proxy_url}")

            return {
                "name": name,
                "targetConfiguration": {
                    "mcp": {
                        "mcpServer": {
                            "endpoint": proxy_url,
                        }
                    }
                },
                "credentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            }

        return None  # Unknown type, skip

    def build_target_config_for_nginx(
        self, server: dict, cloudfront_url: str
    ) -> dict | None:
        """Build target config routing through the existing NGINX proxy.

        For internal Docker services (e.g., http://currenttime-server:8000),
        route via CloudFront → ALB → NGINX → service. This is Path A's
        infrastructure reused as Path B targets.

        Note: in-cluster HTTP intentional — these URLs are private Docker
        service hostnames that never leave the VPC; external traffic is HTTPS.
        """
        proxy_url = server.get("proxy_pass_url", "")
        name = server.get("display_name") or server.get("server_name") or server.get("name", "")
        path = server.get("path", f"/{name}")

        # Internal Docker services use http:// with non-public hostnames
        if proxy_url.startswith("http://") and not proxy_url.startswith("http://localhost"):
            external_url = f"{cloudfront_url.rstrip('/')}/mcp{path}"
            return {
                "name": name,
                "targetConfiguration": {
                    "mcp": {
                        "mcpServer": {
                            "endpoint": external_url,
                        }
                    }
                },
                "credentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            }
        return None

    def create_target(self, gateway_id: str, target_config: dict) -> str | None:
        """Create a gateway target from a pre-built config.

        Returns the target ID on success, None on failure.
        """
        name = target_config.get("name", "unknown")
        try:
            resp = self._client.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=f"tg-{name}",
                **{k: v for k, v in target_config.items() if k != "name"},
            )
            target_id = resp["targetId"]
            logger.info("Created target %s for %s", target_id, name)
            return target_id
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ConflictException":
                logger.info("Target %s already exists, skipping", name)
                return "existing"
            logger.error("Failed to create target for %s: %s", name, type(e).__name__)
            return None
        except Exception as e:
            logger.error("Failed to create target for %s: %s", name, type(e).__name__)
            return None

    def delete_target(self, gateway_id: str, target_id: str, name: str = "") -> bool:
        """Delete a gateway target. Returns True on success."""
        try:
            self._client.delete_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
            )
            logger.info("Deleted target %s (%s)", target_id, name)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info("Target %s already deleted", target_id)
                return True
            logger.error("Failed to delete target %s: %s", target_id, type(e).__name__)
            return False

    def list_targets(self, gateway_id: str) -> list[dict]:
        """List all existing targets for a gateway."""
        try:
            resp = self._client.list_gateway_targets(gatewayIdentifier=gateway_id)
            return resp.get("items", [])
        except ClientError as e:
            logger.error("Failed to list targets: %s", type(e).__name__)
            return []
