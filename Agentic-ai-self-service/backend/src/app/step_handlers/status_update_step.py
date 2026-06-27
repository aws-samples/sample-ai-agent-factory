"""Step handler: Write final deployment status.

Receives deployment_id + results, writes final state (succeeded/failed)
to the Deployment_State_Table.

Requirements: 3.6
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
from datetime import datetime, timezone

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.agent_versions_store import (
    RuntimeSlots,
    get_slots_store,
    get_versions_store,
)
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    """Read an environment variable with a fallback."""
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    """Create a DeploymentStateStore from environment variables."""
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def handler(event: dict, context) -> dict:
    """Lambda handler for the final status update step.

    Writes the terminal deployment state (succeeded or failed) to DynamoDB,
    including runtime outputs and error details.

    Args:
        event: Step Functions event with ``deployment_id``, ``runtime_id``,
            ``runtime_endpoint``, ``gateway_result``, and optionally
            ``error`` for failure cases.
        context: Lambda context (unused).

    Returns:
        Dict with ``status`` and ``deployment_id``.
    """
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.STATUS_UPDATE,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        # Detect errors from both direct invocation ("error" key) and
        # Step Functions Catch handler ("error_info" key with Error/Cause).
        error_details = event.get("error")
        error_info = event.get("error_info")
        if not error_details and error_info:
            if isinstance(error_info, dict):
                error_details = error_info.get("Cause") or error_info.get("Error") or str(error_info)
            else:
                error_details = str(error_info)
        now = datetime.now(timezone.utc)

        # Collect outputs from previous steps (available even on partial failure)
        runtime_id = event.get("runtime_id")
        runtime_arn = event.get("runtime_arn")
        runtime_endpoint = event.get("runtime_endpoint")
        gateway_result = event.get("gateway_result") or {}
        gateway_url = gateway_result.get("gateway_url")
        policy_result = event.get("policy_result") or {}
        memory_result = event.get("memory_result") or {}
        knowledge_base_result = event.get("knowledge_base_result") or {}
        guardrails_result = event.get("guardrails_result") or {}
        mcp_server_runtime_id = event.get("mcp_server_runtime_id")
        # Phase B — HARNESS mode. The harness_step puts harness_id/harness_arn/
        # deployment_mode on the event INSTEAD of runtime_id/runtime_arn. Persist
        # them so the DELETE / test-runtime handlers can route to harness_deployer
        # (and find the ARN to invoke). deployment_mode is also written at create
        # time in deployment_handler, but we re-affirm it here for completeness.
        harness_id = event.get("harness_id")
        harness_arn = event.get("harness_arn")
        deployment_mode = event.get("deployment_mode")

        if error_details:
            # Save partial results so delete handler can clean up
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                completed_at=now,
                error_details=str(error_details),
                runtime_id=runtime_id,
                runtime_arn=runtime_arn,
                gateway_result=gateway_result if gateway_result else None,
                policy_result=policy_result if policy_result else None,
                memory_result=memory_result if memory_result else None,
                knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
                guardrails_result=guardrails_result if guardrails_result else None,
                mcp_server_runtime_id=mcp_server_runtime_id,
                harness_id=harness_id,
                harness_arn=harness_arn,
                deployment_mode=deployment_mode,
            )
            # Best-effort: flip the AgentVersion row to failed so version
            # history reflects the partial deploy.
            version_id = event.get("version_id")
            friendly_runtime_name = event.get("friendly_runtime_name")
            if version_id and friendly_runtime_name:
                try:
                    get_versions_store().update_status(
                        runtime_name=friendly_runtime_name,
                        version_id=version_id,
                        status="failed",
                        runtime_id=runtime_id,
                        runtime_arn=runtime_arn,
                    )
                except Exception:
                    logger.exception(
                        "Failed to mark AgentVersion %s/%s failed",
                        friendly_runtime_name,
                        version_id,
                    )
            return {
                "deployment_id": deployment_id,
                "status": DeploymentStatusEnum.FAILED.value,
                "error_details": str(error_details),
                "version_id": version_id,
            }

        store.update_status(
            deployment_id,
            DeploymentStatusEnum.SUCCEEDED,
            completed_at=now,
            runtime_endpoint=runtime_endpoint,
            runtime_id=runtime_id,
            runtime_arn=runtime_arn,
            gateway_url=gateway_url,
            gateway_result=gateway_result if gateway_result else None,
            policy_result=policy_result if policy_result else None,
            memory_result=memory_result if memory_result else None,
            knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
            mcp_server_runtime_id=mcp_server_runtime_id,
            harness_id=harness_id,
            harness_arn=harness_arn,
            deployment_mode=deployment_mode,
        )

        # Phase 1 Gap 1A — flip the AgentVersion row to succeeded and update
        # the runtime's production slot if this deploy targeted production.
        # Both writes are best-effort; a failure here doesn't fail the deploy
        # because the deployment record itself is already marked succeeded.
        version_id = event.get("version_id")
        friendly_runtime_name = event.get("friendly_runtime_name")
        deployment_slot = (event.get("deployment_slot") or "production").lower()
        owner_sub = event.get("owner_sub") or ""
        if version_id and friendly_runtime_name:
            try:
                get_versions_store().update_status(
                    runtime_name=friendly_runtime_name,
                    version_id=version_id,
                    status="succeeded",
                    runtime_id=runtime_id,
                    runtime_arn=runtime_arn,
                    runtime_endpoint=runtime_endpoint,
                    code_s3_key=event.get("s3_key"),
                )
            except Exception:
                logger.exception(
                    "Failed to mark AgentVersion %s/%s succeeded",
                    friendly_runtime_name,
                    version_id,
                )
            try:
                slots_store = get_slots_store()
                existing = slots_store.get(friendly_runtime_name)
                # SECURITY (H-1, security review 2026-05-28): defense-in-depth
                # against cross-tenant slot hijack. deployment_handler already
                # rejects mismatched-owner deploys at the API boundary, but in
                # case a bug or race lets one through, refuse to overwrite a
                # slot row owned by a different sub. See lessons.md Bug 122.
                if (
                    existing is not None
                    and existing.owner_sub
                    and existing.owner_sub != owner_sub
                ):
                    logger.warning(
                        "Refusing to update RuntimeSlots for %s/%s: existing "
                        "slot owned by %s, deploy caller is %s. This should "
                        "have been caught at the API boundary (Bug 122).",
                        friendly_runtime_name,
                        version_id,
                        existing.owner_sub,
                        owner_sub,
                    )
                else:
                    # On the very first deploy of a friendly name, create the slot
                    # row. On subsequent deploys, preserve the previous-production
                    # pointer so rollback() can flip back without bookkeeping.
                    if existing is None:
                        new_slots = RuntimeSlots(
                            runtime_name=friendly_runtime_name,
                            owner_sub=owner_sub,
                            production_version_id=(
                                version_id if deployment_slot == "production" else None
                            ),
                            staging_version_id=(
                                version_id if deployment_slot == "staging" else None
                            ),
                            last_promoted_at=now.isoformat(),
                        )
                    else:
                        new_slots = existing
                        if deployment_slot == "production":
                            new_slots.previous_production_version_id = (
                                existing.production_version_id
                            )
                            new_slots.production_version_id = version_id
                            new_slots.last_promoted_at = now.isoformat()
                        elif deployment_slot == "staging":
                            new_slots.staging_version_id = version_id
                    slots_store.upsert(new_slots)
            except Exception:
                logger.exception(
                    "Failed to update RuntimeSlots for %s/%s",
                    friendly_runtime_name,
                    version_id,
                )

        return {
            "deployment_id": deployment_id,
            "status": DeploymentStatusEnum.SUCCEEDED.value,
            "runtime_id": runtime_id,
            "runtime_endpoint": runtime_endpoint,
            "gateway_url": gateway_url,
            "version_id": version_id,
            "deployment_slot": deployment_slot,
        }

    except Exception as exc:
        logger.exception("Status update step failed for deployment %s", deployment_id)
        # Last-resort: try to mark as failed
        try:
            store = _get_deployment_store()
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.FAILED,
                completed_at=datetime.now(timezone.utc),
                error_details=f"Status update step error: {exc}",
            )
        except Exception:
            logger.exception("Failed to write error state for deployment %s", deployment_id)

        return {
            "deployment_id": deployment_id,
            "status": DeploymentStatusEnum.FAILED.value,
            "error_details": str(exc),
        }
