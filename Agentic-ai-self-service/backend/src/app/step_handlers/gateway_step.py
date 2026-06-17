"""Step handler: Deploy MCP Gateway via boto3.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import deploy_gateway

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.GATEWAY, DeploymentStatusEnum.IN_PROGRESS)

        gateway_config = event.get("gateway_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        template_id = event.get("template_id")
        gateway_tools = event.get("gateway_tools") or []
        identity_config = event.get("identity_config") or {}
        custom_tools = event.get("custom_tools") or []

        mcp_server_runtime_arn = event.get("mcp_server_runtime_arn")
        mcp_oauth = event.get("mcp_oauth")

        knowledge_base_result = event.get("knowledge_base_result") or {}

        gateway_result = deploy_gateway(
            gateway_config=gateway_config,
            region=region,
            template_id=template_id,
            gateway_tools=gateway_tools,
            identity_config=identity_config,
            custom_tools=custom_tools,
            mcp_server_runtime_arn=mcp_server_runtime_arn,
            mcp_oauth=mcp_oauth,
            knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
            deployment_id=deployment_id if deployment_id else None,
        )

        if not gateway_result.get("success"):
            raise RuntimeError(f"Gateway deployment failed: {gateway_result.get('error', 'unknown error')}")

        return {
            **event,
            "gateway_result": gateway_result,
        }

    except Exception:
        logger.exception("Gateway step failed for deployment %s", deployment_id)
        raise
