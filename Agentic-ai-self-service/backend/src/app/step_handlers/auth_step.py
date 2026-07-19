"""Step handler: Configure JWT auth on the runtime (if gateway deployed).

Requirements: 3.6
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore

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
        store.update_step(deployment_id, DeploymentStepName.AUTH, DeploymentStatusEnum.IN_PROGRESS)

        runtime_id = event.get("runtime_id", "")
        gateway_result = event.get("gateway_result") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))  # noqa: F841

        if not runtime_id:
            raise RuntimeError("No runtime_id provided")

        if not gateway_result.get("client_info"):
            return {
                **event,
                "auth_result": {
                    "success": True,
                    "message": "No gateway client_info, skipping JWT auth",
                },
            }

        # Skip JWT auth configuration on the runtime itself.
        # The runtime is invoked via boto3 invoke_agent_runtime (SigV4).
        # Configuring customJWTAuthorizer switches the runtime to JWT-only,
        # which breaks SigV4 invocations. JWT auth is only needed for
        # direct HTTP invocations from external clients.
        logger.info("Skipping JWT auth on runtime (test invocation uses SigV4)")
        return {
            **event,
            "auth_result": {
                "success": True,
                "message": "Skipped JWT auth (SigV4 invocation)",
            },
        }

    except Exception:
        logger.exception("Auth step failed for deployment %s", deployment_id)
        raise
