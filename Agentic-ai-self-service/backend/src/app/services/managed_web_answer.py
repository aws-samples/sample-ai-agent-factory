"""Managed Amazon Bedrock AgentCore Web Answer connector contract.

Web Answer is the managed sibling of Web Search: one tool call returns a synthesized,
grounded answer with citations to current web sources. The connector plans its own
searches, runs them in parallel and decides when it has enough evidence, so an agent
needs no search-retrieve-synthesize-cite pipeline of its own. Queries stay within AWS.

Contract (AgentCore developer guide, "Add a Connector target with Web Answer" and the
Web Answer launch post, read 2026-11-12):

* ``targetConfiguration.mcp.connector.source.connectorId`` is ``web-answer``. AWS documents
  Web Answer on the connector's *current default version* and names no pin, unlike Web
  Search where this platform pins ``1.2.0``; so no ``version`` is sent, and the version the
  service resolved is read back from ``GetGatewayTarget`` as deployment evidence (observed
  ``1.1.0`` in us-east-1 on 2026-11-12; the tool is published as ``AmazonWebAnswer___WebAnswer``).
* The single configuration is named ``WebAnswer``; the tool is published on the Gateway
  under that name. Target-level ``parameterValues.domainFilter.include/exclude`` (up to 50
  domains each, connector 1.1.0+) are enforced server-side and an agent can only narrow
  them. They are not exposed yet (follow-up); the configuration ships empty.
* Outbound credentials: ``GATEWAY_IAM_ROLE``. The Gateway service role needs
  ``bedrock-agentcore:InvokeWebAnswer`` on the service-owned ARN
  ``arn:<partition>:bedrock-agentcore:<region>:aws:tool/web-answer.v1``; it does not need
  ``bedrock:InvokeModel``, Web Answer runs its own model.
* Available in US East (N. Virginia) only; see ``region_capabilities``. Every other Region
  is refused before any AWS side effect.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.region_capabilities import (
    WEB_ANSWER_SUPPORTED_REGIONS,
    feature_unavailable_message,
)

MANAGED_WEB_ANSWER_CONNECTOR_ID = "web-answer"
MANAGED_WEB_ANSWER_TARGET_NAME = "AmazonWebAnswer"
MANAGED_WEB_ANSWER_TOOL_NAME = "WebAnswer"
MANAGED_WEB_ANSWER_TOOL_ID = "web_answer"
MANAGED_WEB_ANSWER_TOOL_IDS = frozenset({MANAGED_WEB_ANSWER_TOOL_ID})
MANAGED_WEB_ANSWER_DESCRIPTION = "AWS-managed Web Answer: synthesized, cited answers from current web sources"

# No gallery template implies Web Answer yet; it is requested explicitly through its
# tool id. A future "research agent" template is added here, mirroring Web Search.
MANAGED_WEB_ANSWER_TEMPLATE_IDS: frozenset[str] = frozenset()


def managed_web_answer_requested(
    template_id: str | None,
    gateway_tools: Iterable[object] | None,
) -> bool:
    """Whether this deployment requires the managed Web Answer target."""

    return bool(
        template_id in MANAGED_WEB_ANSWER_TEMPLATE_IDS
        or MANAGED_WEB_ANSWER_TOOL_IDS.intersection(tool for tool in (gateway_tools or ()) if isinstance(tool, str))
    )


def without_managed_web_answer_tools(gateway_tools: Iterable[object] | None) -> list:
    """Return tool IDs that still need ordinary Lambda-backed targets."""

    return [tool for tool in (gateway_tools or ()) if tool not in MANAGED_WEB_ANSWER_TOOL_IDS]


def require_web_answer_region(region: str) -> None:
    """Fail before an AWS side effect when Web Answer is unavailable."""

    if region not in WEB_ANSWER_SUPPORTED_REGIONS:
        raise ValueError(feature_unavailable_message("web_answer", region))


def managed_web_answer_resource_arn(region: str, partition: str = "aws") -> str:
    """Service-owned IAM resource checked for each Web Answer invocation."""

    require_web_answer_region(region)
    return f"arn:{partition}:bedrock-agentcore:{region}:aws:tool/web-answer.v1"


def managed_web_answer_target_params(gateway_id: str) -> dict:
    """Exact ``CreateGatewayTarget`` request for the Web Answer connector.

    No connector ``version`` is sent on purpose: AWS documents Web Answer on the
    connector's current default version. ``GetGatewayTarget`` reports what the service
    resolved; the deploy records that as evidence instead of asserting a pin.
    """

    if not gateway_id:
        raise ValueError("gateway_id is required for the managed Web Answer target")
    return {
        "gatewayIdentifier": gateway_id,
        "name": MANAGED_WEB_ANSWER_TARGET_NAME,
        "description": MANAGED_WEB_ANSWER_DESCRIPTION,
        "targetConfiguration": {
            "mcp": {
                "connector": {
                    "source": {"connectorId": MANAGED_WEB_ANSWER_CONNECTOR_ID},
                    "configurations": [
                        {
                            "name": MANAGED_WEB_ANSWER_TOOL_NAME,
                            "parameterValues": {},
                        }
                    ],
                }
            }
        },
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
