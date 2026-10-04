"""Step handler: Create an AgentCore Harness via boto3 (Phase B authoring path).

Parallel to runtime_configure_step. The Harness is AWS's managed, config-driven
agent harness — DECLARE model + instructions + tools + memory, no code artifact.
This step runs in HARNESS mode INSTEAD of codegen/iam/runtime_configure/
runtime_launch; the shared gateway + memory steps still run before it so the
harness can wire a connected gateway + memory.

Requirements: 3.5 (Phase B)
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import (
    DeploymentStatusEnum,
    DeploymentStepName,
    RuntimeConfig,
)
from app.services import harness_deployer, step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.runtime_deployer import govern_default_runtime_log_group

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _resolve_memory_arn(memory_result: dict, region: str, event: dict) -> str:
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
        account_id = step_clients.account_id_for_event(event)
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
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )

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
        harness_name = harness_deployer.sanitize_harness_name(event.get("agentcore_runtime_name") or config.name)

        # Wire a connected gateway + memory if the shared steps deployed them.
        gateway_result = event.get("gateway_result") or {}
        gateway_arn = gateway_result.get("gateway_arn") or gateway_result.get("arn") or None

        # P0-B governance tags, resolved once per deploy by `tag_policy_store.resolve_governance`
        # and carried in the Step Functions state. This step creates a harness, its execution role
        # and (for a CUSTOM_JWT gateway) an OAuth2 credential provider -- all three are billable or
        # auditable, so cost attribution and ABAC (ARCC cnt_6gBImtb08AJqCB) apply. The `governed_*`
        # helpers validate the set against the namespaces this step's role may stamp before the
        # first create, so an unstampable key fails the step instead of half-building a harness.
        resource_tags = event.get("resource_tags")

        memory_result = event.get("memory_result") or {}
        memory_arn = _resolve_memory_arn(memory_result, region, event) or None

        # Build (or reuse the shared) harness execution role, scoped to the
        # connected model/memory/gateway ARNs for least privilege (Holmes IAM).
        iam_client = step_clients.client(event, "iam")
        cross_account = bool(event.get("target_account_id"))
        if cross_account:
            # The platform's optional SHARED_HARNESS_ROLE_ARN belongs to the
            # home account and cannot be passed to AgentCore in a target
            # account. Cross-account Harness deploys therefore mirror the
            # Runtime path: use a stable, pre-provisioned target-account role
            # whose exact ARN was validated when the target was registered.
            from app.services.deploy_target import (
                DEFAULT_TARGET_HARNESS_ROLE_NAME,
                target_execution_role_arn,
            )

            role_arn = target_execution_role_arn(
                event["target_account_id"],
                role_arn=event.get("target_harness_role_arn"),
                default_role_name=DEFAULT_TARGET_HARNESS_ROLE_NAME,
            )
            role_created = False
        else:
            _role_result = harness_deployer.get_shared_or_new_harness_role(
                iam_client,
                harness_name,
                model_id=model_id or None,
                memory_arn=memory_arn,
                gateway_arn=gateway_arn,
                region=region,
                return_provenance=True,
                resource_tags=resource_tags,
            )
            if isinstance(_role_result, tuple):
                role_arn, role_created = _role_result
            else:
                # Unknown provenance is retained, never assumed newly created.
                role_arn, role_created = _role_result, False

        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")

        # A platform gateway uses CUSTOM_JWT (Cognito) auth — the harness needs an
        # outbound OAuth credential provider to call it, or invoke fails with 401
        # (verified live). Register one from the gateway's client_info.
        gw_provider_arn = None
        gw_scopes: list = []
        gw_provider_name = ""
        gw_provider_created = False
        if gateway_arn:
            _provider_result = harness_deployer.ensure_gateway_outbound_provider(
                agentcore_ctrl,
                harness_name,
                gateway_result.get("client_info") or {},
                # These must use the deployment target. The resolver's
                # default factories point at the platform account, where a
                # target gateway's app client / staged secret does not exist.
                secrets_client=step_clients.client(event, "secretsmanager"),
                cognito_client=step_clients.client(event, "cognito-idp"),
                return_provenance=True,
                region=region,
                resource_tags=resource_tags,
            )
            if len(_provider_result) == 3:
                gw_provider_arn, gw_scopes, gw_provider_created = _provider_result
            else:
                # Backward-compatible fail-closed handling for a replaced/mock
                # helper that still exposes the older two-value contract.
                gw_provider_arn, gw_scopes = _provider_result
                gw_provider_created = False
            if gw_provider_arn:
                import re as _re

                gw_provider_name = _re.sub(r"[^a-zA-Z0-9_-]", "-", f"harness-gw-{harness_name}")[:60]
                # Record BEFORE create_harness, not after it. The provider is a
                # real AWS resource the moment the call above returns, and it is
                # reachable for teardown by exactly two routes: this manifest row,
                # or destroy_harness reconstructing the name from a harness_id. If
                # create_harness raises (or this Lambda is killed) there is no
                # harness_id, so the harness row below is never written and
                # destroy_harness never runs — leaving the provider, and the
                # gateway client credentials embedded in it, orphaned with nothing
                # pointing at it. Recording here is the same orphan-guard rule the
                # comment below states; the provider just happens to be created
                # first. Double deletion is safe: _delete_managed_resource and
                # destroy_harness both treat a missing provider as success.
                store.record_resource(
                    deployment_id,
                    {
                        "type": "oauth2_credential_provider",
                        "name": gw_provider_name,
                        "region": region,
                        "created_by_deployment": gw_provider_created,
                    },
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
            region=region,
            resource_tags=resource_tags,
        )
        harness_id = create_result.get("harness_id", "")
        if not harness_id:
            raise RuntimeError("create_harness returned no harness_id")
        early_arn = create_result.get("arn", "")

        # Manifest: record the harness + its side-resources for generic teardown
        # right after create succeeds (wait_for_harness_ready can be killed
        # mid-poll, otherwise leaking these). Types match _delete_managed_resource.
        store.record_resource(
            deployment_id,
            {
                "type": "harness",
                "id": harness_id,
                "region": region,
                "created_by_deployment": (create_result.get("created_by_deployment") is True),
            },
        )
        # Per-harness exec role only — never record the shared role (it is reused
        # across every harness and must not be torn down on a single delete).
        if not cross_account and not os.environ.get("SHARED_HARNESS_ROLE_ARN", ""):
            store.record_resource(
                deployment_id,
                {
                    "type": "iam_role",
                    "name": str(role_arn).rsplit("/", 1)[-1],
                    "region": region,
                    "created_by_deployment": role_created,
                },
            )
        # The harness->gateway outbound OAuth2 credential provider is recorded at
        # the point of creation above, which is before this line runs.

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
            raise RuntimeError(f"Harness failed to become ready: {ready.get('error', 'unknown error')}")

        harness_arn = ready.get("arn") or create_result.get("arn", "")

        # AgentCore hosts the harness on a runtime of its own, and that runtime's DEFAULT log group
        # holds every conversation the harness serves. The service creates the group without
        # retention, exactly as it does for the runtimes the platform creates, so it gets the same
        # 30-day bound under the same rule: a deployment does not report success while its
        # conversation log is unbounded (govern_default_runtime_log_group). Measured 2026-10-02:
        # all 60 harness runtime groups in the platform account were set to never expire, the one
        # from that morning's harness deploy-and-delete included. The harness row is already in the
        # manifest, so a failure here still leaves the delete path everything it needs.
        backing_runtime_id = ready.get("backing_runtime_id") or ""
        if not backing_runtime_id:
            raise RuntimeError(
                "The ready harness reports no backing runtime, so its conversation log cannot be bounded"
            )
        runtime_log_group = govern_default_runtime_log_group(
            step_clients.client(event, "logs", region_name=region),
            backing_runtime_id,
        )

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
                # The service-created runtime and its bounded conversation log group,
                # left to expire on teardown like every runtime's.
                "backing_runtime_id": backing_runtime_id,
                "runtime_log_group": runtime_log_group,
            },
        }

    except Exception:
        logger.exception("Harness step failed for deployment %s", deployment_id)
        raise
