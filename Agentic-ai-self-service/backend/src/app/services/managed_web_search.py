"""Managed Amazon Bedrock AgentCore Web Search connector contract.

The former ``duckduckgo_search`` built-in scraped DuckDuckGo's no-JavaScript
HTML endpoints. Those endpoints are neither a supported full-results API nor
reliably reachable from AgentCore Runtime egress. Web Search Tool is the AWS
managed, MCP-compliant replacement: queries stay within AWS and results carry
source URLs and citations.
"""

from __future__ import annotations

from collections.abc import Iterable

MANAGED_WEB_SEARCH_CONNECTOR_ID = "web-search"
MANAGED_WEB_SEARCH_CONNECTOR_VERSION = "1.2.0"
MANAGED_WEB_SEARCH_TARGET_NAME = "AmazonWebSearch"
MANAGED_WEB_SEARCH_TOOL_NAME = "WebSearch"
MANAGED_WEB_SEARCH_TOOL_ID = "web_search"
LEGACY_DUCKDUCKGO_TOOL_ID = "duckduckgo_search"
MANAGED_WEB_SEARCH_TOOL_IDS = frozenset({MANAGED_WEB_SEARCH_TOOL_ID, LEGACY_DUCKDUCKGO_TOOL_ID})
MANAGED_WEB_SEARCH_TEMPLATE_IDS = frozenset({"web-search-agent", "strands-gateway-agent"})
MANAGED_WEB_SEARCH_SUPPORTED_REGIONS = frozenset({"us-east-1", "eu-west-1", "ap-northeast-1"})


def managed_web_search_requested(
    template_id: str | None,
    gateway_tools: Iterable[object] | None,
) -> bool:
    """Whether this deployment requires the managed Web Search target.

    ``duckduckgo_search`` remains an accepted request ID only so saved canvases
    migrate safely. It is never routed to the retired HTML scraper.
    """

    return bool(
        template_id in MANAGED_WEB_SEARCH_TEMPLATE_IDS
        or MANAGED_WEB_SEARCH_TOOL_IDS.intersection(tool for tool in (gateway_tools or ()) if isinstance(tool, str))
    )


def without_managed_web_search_tools(gateway_tools: Iterable[object] | None) -> list:
    """Return tool IDs that still need ordinary Lambda-backed targets."""

    return [tool for tool in (gateway_tools or ()) if tool not in MANAGED_WEB_SEARCH_TOOL_IDS]


def require_managed_web_search_region(region: str) -> None:
    """Fail before an AWS side effect when Web Search is unavailable."""

    if region not in MANAGED_WEB_SEARCH_SUPPORTED_REGIONS:
        supported = ", ".join(sorted(MANAGED_WEB_SEARCH_SUPPORTED_REGIONS))
        raise ValueError(f"AgentCore Web Search is not available in {region!r}; supported Regions: {supported}.")


def managed_web_search_resource_arn(region: str) -> str:
    """Service-owned IAM resource checked for each Web Search invocation."""

    require_managed_web_search_region(region)
    return f"arn:aws:bedrock-agentcore:{region}:aws:tool/web-search.v1"


def managed_web_search_target_params(gateway_id: str) -> dict:
    """Exact ``CreateGatewayTarget`` request for Web Search Tool.

    Version 1.2.0 is pinned so request-level domain/date filters and the tool
    schema cannot drift when AWS changes the connector default.
    """

    if not gateway_id:
        raise ValueError("gateway_id is required for the managed Web Search target")
    return {
        "gatewayIdentifier": gateway_id,
        "name": MANAGED_WEB_SEARCH_TARGET_NAME,
        "description": "AWS-managed Web Search Tool with source citations",
        "targetConfiguration": {
            "mcp": {
                "connector": {
                    "source": {
                        "connectorId": MANAGED_WEB_SEARCH_CONNECTOR_ID,
                        "version": MANAGED_WEB_SEARCH_CONNECTOR_VERSION,
                    },
                    "configurations": [
                        {
                            "name": MANAGED_WEB_SEARCH_TOOL_NAME,
                            "parameterValues": {},
                        }
                    ],
                }
            }
        },
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
