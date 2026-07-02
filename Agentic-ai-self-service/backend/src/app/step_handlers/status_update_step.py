"""Step handler: Write final deployment status.

Receives deployment_id + results, writes final state (succeeded/failed)
to the Deployment_State_Table.

Requirements: 3.6
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
import time
from datetime import datetime, timezone

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.agent_versions_store import (
    RuntimeSlots,
    get_slots_store,
    get_versions_store,
)
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _gone(exc: Exception) -> bool:
    """True if the exception signals the resource is already deleted."""
    msg = str(exc).lower()
    return any(x in msg for x in ("notfound", "not found", "does not exist", "no longer exists"))


def _auto_cleanup_on_failure(store: DeploymentStateStore, deployment_id: str) -> None:
    """Best-effort cleanup of created_resources on deploy failure.

    Iterates the manifest in dependency order (primary resources before
    backing stores) and deletes each. Mirrors the logic in
    deployment_handler._delete_managed_resource but runs inline in the
    status_update step so failed deployments don't orphan AWS resources.

    All exceptions are logged but swallowed — this is a best-effort cleanup
    and must not change the deployment's failure status.
    """
    region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
    try:
        state = store.get(deployment_id)
        record = state.model_dump() if state else None
        resources = (record or {}).get("created_resources") or []
        if not resources:
            logger.info("No created_resources to clean up for %s", deployment_id)
            return

        # Priority order: delete primary resources first, then backing stores
        priority = {
            "knowledge_base": 0, "agent_runtime": 1, "harness": 1, "gateway": 2,
            "policy_engine": 2, "lambda": 5, "memory": 6, "guardrail": 6,
            "oauth2_credential_provider": 7, "api_key_credential_provider": 7,
            "s3_vectors_bucket": 8, "iam_role": 9, "cognito_user_pool": 9,
            "secret": 9, "s3_object": 9,
        }
        ordered = sorted(resources, key=lambda r: priority.get(str(r.get("type")), 4))
        cleaned = 0
        for res in ordered:
            try:
                _cleanup_resource(res, region)
                cleaned += 1
            except Exception as e:
                if _gone(e):
                    cleaned += 1
                else:
                    logger.warning("Auto-cleanup failed for %s: %s", res, str(e)[:200])
        logger.info("Auto-cleanup completed for %s: %d/%d resources", deployment_id, cleaned, len(resources))
    except Exception as e:
        logger.warning("Auto-cleanup error for %s: %s", deployment_id, str(e)[:200])


def _cleanup_resource(res: dict, region: str) -> None:
    """Delete a single resource from the manifest. Raises on failure."""
    rtype = str(res.get("type", ""))
    rid = res.get("id") or ""
    rname = res.get("name") or ""
    res_region = res.get("region") or region

    if rtype == "gateway":
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        ctrl.delete_gateway(gatewayIdentifier=rid)
    elif rtype == "agent_runtime":
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        ctrl.delete_agent_runtime(agentRuntimeId=rid)
    elif rtype == "harness":
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        ctrl.delete_harness(harnessId=rid)
    elif rtype == "policy_engine":
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        # Delete policies first, then engine
        try:
            pols = ctrl.list_policies(policyEngineId=rid, maxResults=100).get("policies", [])
            for p in pols:
                try:
                    ctrl.delete_policy(policyEngineId=rid, policyId=p.get("policyId"))
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(2)
        ctrl.delete_policy_engine(policyEngineId=rid)
    elif rtype == "guardrail":
        boto3.client("bedrock", region_name=res_region).delete_guardrail(guardrailIdentifier=rid)
    elif rtype == "knowledge_base":
        ba = boto3.client("bedrock-agent", region_name=res_region)
        # Delete data sources first
        try:
            for ds in ba.list_data_sources(knowledgeBaseId=rid).get("dataSourceSummaries", []):
                try:
                    ba.delete_data_source(knowledgeBaseId=rid, dataSourceId=ds["dataSourceId"])
                except Exception:
                    pass
        except Exception:
            pass
        ba.delete_knowledge_base(knowledgeBaseId=rid)
        # Poll until deleted
        for _ in range(24):
            try:
                ba.get_knowledge_base(knowledgeBaseId=rid)
            except Exception as ge:
                if _gone(ge):
                    break
            time.sleep(5)
    elif rtype == "s3_vectors_bucket":
        s3v = boto3.client("s3vectors", region_name=res_region)
        bname = rname or rid
        try:
            for ix in s3v.list_indexes(vectorBucketName=bname).get("indexes", []):
                try:
                    s3v.delete_index(vectorBucketName=bname, indexName=ix["indexName"])
                except Exception:
                    pass
        except Exception:
            pass
        s3v.delete_vector_bucket(vectorBucketName=bname)
    elif rtype == "cognito_user_pool":
        cog = boto3.client("cognito-idp", region_name=res_region)
        # Delete domain first (required)
        try:
            dom = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
            if dom:
                cog.delete_user_pool_domain(UserPoolId=rid, Domain=dom)
                for _ in range(12):
                    try:
                        still = cog.describe_user_pool(UserPoolId=rid).get("UserPool", {}).get("Domain")
                    except Exception:
                        still = None
                    if not still:
                        break
                    time.sleep(5)
        except Exception:
            pass
        # Retry pool delete in case domain teardown is still settling
        for _attempt in range(6):
            try:
                cog.delete_user_pool(UserPoolId=rid)
                break
            except Exception as ce:
                if "domain" in str(ce).lower() and _attempt < 5:
                    time.sleep(5)
                    continue
                raise
    elif rtype == "memory":
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        ctrl.delete_memory(memoryId=rid)
    elif rtype == "lambda":
        boto3.client("lambda", region_name=res_region).delete_function(FunctionName=rname or rid)
    elif rtype == "iam_role":
        iam = boto3.client("iam")
        role_name = rname or rid
        # Detach/delete policies first
        try:
            for pol in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=role_name, PolicyArn=pol["PolicyArn"])
        except Exception:
            pass
        try:
            for pol in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role_name, PolicyName=pol)
        except Exception:
            pass
        iam.delete_role(RoleName=role_name)
    elif rtype == "secret":
        boto3.client("secretsmanager", region_name=res_region).delete_secret(
            SecretId=rid, ForceDeleteWithoutRecovery=True
        )
    elif rtype in ("oauth2_credential_provider", "api_key_credential_provider"):
        ctrl = boto3.client("bedrock-agentcore-control", region_name=res_region)
        if rtype == "oauth2_credential_provider":
            ctrl.delete_oauth2_credential_provider(name=rname)
        else:
            ctrl.delete_api_key_credential_provider(name=rname)


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

            # Auto-cleanup on failure: delete created resources from the manifest
            # so failed deployments don't leave orphaned AWS resources (KB, Cognito
            # pools, gateways, etc.). Best-effort — cleanup errors are logged but
            # don't change the failure status.
            _auto_cleanup_on_failure(store, deployment_id)

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
