"""Step handler: Wait for AgentCore runtime to become READY.

Requirements: 3.5
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.observability_dashboard import put_dashboard_for_runtime
from app.services.runtime_deployer import (
    wait_for_default_endpoint_ready,
    wait_for_runtime_ready,
)

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
        store.update_step(
            deployment_id,
            DeploymentStepName.RUNTIME_LAUNCH,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        runtime_id = event.get("runtime_id", "")
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

        if not runtime_id:
            raise RuntimeError("No runtime_id provided from configure step")

        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")

        # Manifest: re-record the runtime for generic teardown (idempotent with
        # runtime_configure_step; covers any caller that reaches launch without
        # the configure manifest write). Best-effort: never fails the deploy.
        store.record_resource(
            deployment_id,
            {"type": "agent_runtime", "id": runtime_id, "region": region},
        )

        result = wait_for_runtime_ready(agentcore_ctrl, runtime_id, timeout=540)

        if not result.get("success"):
            raise RuntimeError(f"Runtime launch failed: {result.get('error', 'unknown')}")

        # Bug 166: the runtime being READY is NOT enough to invoke — the DEFAULT
        # endpoint qualifier the data plane invokes against is provisioned
        # asynchronously and can lag the runtime's own READY. Gate on the
        # endpoint here so a deploy never reports success while the agent is
        # still uninvokable (the old code silently fell back to the bare runtime
        # ARN, which surfaced to users as "Runtime not found." on first invoke).
        ep_result = wait_for_default_endpoint_ready(agentcore_ctrl, runtime_id, timeout=180)
        if not ep_result.get("success"):
            raise RuntimeError(f"Runtime launch failed: {ep_result.get('error', 'DEFAULT endpoint not READY')}")
        endpoint_url = ep_result.get("endpoint_arn") or result.get("arn", "")

        # Phase 1 Gap 1D — every successful runtime gets a CloudWatch
        # dashboard with widgets for invocations / latency / tokens / errors
        # / tool calls. Best-effort: a put_dashboard failure is logged but
        # doesn't fail the deploy. The dashboard is upserted on the runtime
        # ID so re-deploys of the same version overwrite in place.
        dashboard_name = ""
        dashboard_url = ""
        try:
            friendly_name = event.get("friendly_runtime_name") or runtime_id
            dashboard_name, dashboard_url = put_dashboard_for_runtime(
                runtime_id=runtime_id,
                runtime_name=friendly_name,
                region=region,
            )
        except Exception:
            logger.exception(
                "put_dashboard failed for runtime %s — continuing without dashboard",
                runtime_id,
            )

        return {
            **event,
            "runtime_id": runtime_id,
            "runtime_arn": result.get("arn", ""),
            "runtime_endpoint": endpoint_url,
            "dashboard_name": dashboard_name,
            "dashboard_url": dashboard_url,
            "launch_result": {
                "success": True,
                "runtime_id": runtime_id,
                "endpoint": endpoint_url,
                "dashboard_name": dashboard_name,
                "dashboard_url": dashboard_url,
            },
        }

    except Exception:
        logger.exception("Runtime launch step failed for deployment %s", deployment_id)
        raise
