"""Step handler: Create an AgentCore Harness via boto3 (Phase B authoring path).

Parallel to runtime_configure_step. The Harness is AWS's managed, config-driven
agent harness — DECLARE model + instructions + tools + memory, no code artifact.
This step runs in HARNESS mode INSTEAD of codegen/iam/runtime_configure/
runtime_launch; the shared gateway + memory steps still run before it so the
harness can wire a connected gateway + memory.

Requirements: 3.5 (Phase B)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os

import boto3

from app.models.deployment_models import (
    DeploymentStatusEnum,
    DeploymentStepName,
    RuntimeConfig,
)
from app.services import harness_deployer
from app.services.deployment_state_store import DeploymentStateStore

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _resolve_memory_arn(memory_result: dict, region: str) -> str:
    """Resolve a memory ARN from the upstream memory_result.

    memory_step persists only ``memory_id`` (not the ARN), so when no explicit
    arn is present we reconstruct it from the id using the verified format
    ``arn:aws:bedrock-agentcore:{region}:{account}:memory/{id}`` (see iam_step).
    """
    arn = memory_result.get("memory_arn") or memory_result.get("arn")
    if arn:
        return arn
    memory_id = memory_result.get("memory_id")
    if not memory_id:
        return ""
    try:
        account_id = boto3.client("sts").get_caller_identity()["Account"]
        return f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{memory_id}"
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve memory ARN from id %s", memory_id)
        return ""


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(
            deployment_id,
            DeploymentStepName.HARNESS,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        config_dict = event.get("config", {})
        config = RuntimeConfig.model_validate(config_dict)
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

        # Resolve the model id from config.model (Pydantic model or plain dict).
        model_cfg = config.model
        model_id = ""
        if hasattr(model_cfg, "modelId"):
            model_id = model_cfg.modelId or ""
        elif isinstance(model_cfg, dict):
            model_id = model_cfg.get("modelId", model_cfg.get("model_id", "")) or ""

        system_prompt = config.system_prompt or ""

        # Use the version-suffixed AgentCore name when present (deployment_handler
        # mints it); fall back to the friendly config name for direct callers.
        harness_name = harness_deployer.sanitize_harness_name(
            event.get("agentcore_runtime_name") or config.name
        )

        # Wire a connected gateway + memory if the shared steps deployed them.
        gateway_result = event.get("gateway_result") or {}
        gateway_arn = gateway_result.get("gateway_arn") or gateway_result.get("arn") or None

        memory_result = event.get("memory_result") or {}
        memory_arn = _resolve_memory_arn(memory_result, region) or None

        # Build (or reuse the shared) harness execution role, scoped to the
        # connected model/memory/gateway ARNs for least privilege (Holmes IAM).
        iam_client = boto3.client("iam")
        role_arn = harness_deployer.get_shared_or_new_harness_role(
            iam_client,
            harness_name,
            model_id=model_id or None,
            memory_arn=memory_arn,
            gateway_arn=gateway_arn,
        )

        agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)

        # A platform gateway uses CUSTOM_JWT (Cognito) auth — the harness needs an
        # outbound OAuth credential provider to call it, or invoke fails with 401
        # (verified live). Register one from the gateway's client_info.
        gw_provider_arn = None
        gw_scopes: list = []
        gw_provider_name = ""
        if gateway_arn:
            gw_provider_arn, gw_scopes = harness_deployer.ensure_gateway_outbound_provider(
                agentcore_ctrl, harness_name, gateway_result.get("client_info") or {}
            )
            if gw_provider_arn:
                import re as _re

                gw_provider_name = (
                    _re.sub(r"[^a-zA-Z0-9_-]", "-", f"harness-gw-{harness_name}")[:60]
                )

        create_result = harness_deployer.create_harness(
            agentcore_ctrl,
            harness_name,
            role_arn,
            model_id=model_id or None,
            system_prompt=system_prompt or None,
            gateway_arn=gateway_arn,
            gateway_outbound_provider_arn=gw_provider_arn,
            gateway_scopes=gw_scopes,
            memory_arn=memory_arn,
        )
        harness_id = create_result.get("harness_id", "")
        if not harness_id:
            raise RuntimeError("create_harness returned no harness_id")
        early_arn = create_result.get("arn", "")

        # Manifest: record the harness + its side-resources for generic teardown
        # right after create succeeds (wait_for_harness_ready can be killed
        # mid-poll, otherwise leaking these). Types match _delete_managed_resource.
        store.record_resource(
            deployment_id, {"type": "harness", "id": harness_id, "region": region}
        )
        # Per-harness exec role only — never record the shared role (it is reused
        # across every harness and must not be torn down on a single delete).
        if not os.environ.get("SHARED_HARNESS_ROLE_ARN", ""):
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": f"AgentCoreHarness-{harness_name}",
                    "region": region,
                },
            )
        # The harness->gateway outbound OAuth2 credential provider.
        if gw_provider_name:
            store.record_resource(
                deployment_id,
                {
                    "type": "oauth2_credential_provider",
                    "name": gw_provider_name,
                    "region": region,
                },
            )

        # ORPHAN GUARD (Bug 153): create_harness already created the AWS resource.
        # wait_for_harness_ready may run for up to 600s, but the harness Lambda +
        # its SFN task are capped at 300s — if AWS kills us mid-poll the step
        # never returns, so status_update never sees harness_id and DELETE leaks
        # the real harness. Persist the destroyable handle onto the record NOW,
        # keeping status IN_PROGRESS, so a later timeout/failure still leaves a
        # harness_id/harness_arn (mirrored into runtime_id/runtime_arn for the
        # GSI lookup) that the delete path can clean up.
        try:
            store.update_status(
                deployment_id,
                DeploymentStatusEnum.IN_PROGRESS,
                runtime_id=harness_id,
                runtime_arn=early_arn or None,
                harness_id=harness_id,
                harness_arn=early_arn or None,
                deployment_mode="harness",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not pre-persist harness handle for %s (orphan guard)",
                deployment_id,
                exc_info=True,
            )

        ready = harness_deployer.wait_for_harness_ready(agentcore_ctrl, harness_id)
        if not ready.get("success"):
            raise RuntimeError(
                f"Harness failed to become ready: {ready.get('error', 'unknown error')}"
            )

        harness_arn = ready.get("arn") or create_result.get("arn", "")

        return {
            **event,
            "harness_id": harness_id,
            "harness_arn": harness_arn,
            # Reuse runtime_id/runtime_arn/runtime_endpoint so the shared
            # status_update step persists the harness handle into the SAME
            # fields the runtime path uses. This makes the DELETE / test-runtime
            # lookups (which resolve a record via the runtime_id GSI and then
            # branch on deployment_mode) work UNCHANGED in HARNESS mode — the
            # harness_id is the lookup key, harness_arn is the invoke handle.
            "runtime_id": harness_id,
            "runtime_arn": harness_arn,
            "runtime_endpoint": harness_arn,
            "deployment_mode": "harness",
            "harness_result": {
                "success": True,
                "harness_id": harness_id,
                "harness_arn": harness_arn,
                "role_arn": role_arn,
                # OAuth2 credential provider registered for the harness->gateway
                # outbound call; persisted so DELETE can tear it down (no orphan).
                "gateway_outbound_provider_name": gw_provider_name,
            },
        }

    except Exception:
        logger.exception("Harness step failed for deployment %s", deployment_id)
        raise
