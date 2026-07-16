"""Step handler: Create IAM execution role for the runtime.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import json
import logging
import os

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.observability import (
    _validate_user_otel_secret_arn,
    get_platform_observability_defaults,
)
from app.services.runtime_deployer import create_runtime_iam_role, sanitize_runtime_name

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _resolve_otel_secret_arn(event: dict) -> str | None:
    """Resolve the OTEL auth-header secret ARN for the runtime exec role.

    Single source of truth shared by the per-agent (Gap P3.3B) and legacy
    per-deploy role paths. Prefers the platform-managed secret (always in the
    ``agentcore-otel/`` namespace), then falls back to a per-canvas ARN — but
    only after ``_validate_user_otel_secret_arn`` confirms it stays inside that
    namespace (Critic Finding 1 BLOCKER: never grant ``GetSecretValue`` on a
    tenant-supplied ARN that escapes the namespace). A rejected/invalid ARN is
    dropped (warn-and-disable) rather than failing the deploy — OTEL auth is
    best-effort and must never block the runtime.
    """
    platform_defaults = get_platform_observability_defaults()
    if platform_defaults and platform_defaults.get("auth_header_secret_arn"):
        return platform_defaults["auth_header_secret_arn"]

    obs_cfg = event.get("observability_config") or {}
    otel_secret_arn = obs_cfg.get("auth_header_secret_arn") or obs_cfg.get(
        "authHeaderSecretArn"
    )
    if otel_secret_arn:
        try:
            _validate_user_otel_secret_arn(otel_secret_arn)
        except ValueError as e:
            logger.warning(
                "Per-canvas OTEL secret ARN rejected (%s); disabling OTEL "
                "auth for this runtime.",
                e,
            )
            return None
    return otel_secret_arn


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.IAM, DeploymentStatusEnum.IN_PROGRESS)

        config = event.get("config", {})
        connected_tools = event.get("connected_tools") or []
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

        runtime_name = sanitize_runtime_name(config.get("name", "agent"))

        # Bug 60 fix: prefer the platform's stable shared runtime role created
        # at CDK stack init. AgentCore's IAM cache for fresh per-deploy roles
        # took 17-20 minutes to propagate in some accounts, causing every
        # deploy to fail with `ValidationException: Access denied when trying
        # to retrieve zip file from S3`. The shared role had its IAM cache
        # propagated during stack creation, so user-deploys see no race.
        # ---- Gap P3.3B: opt-in per-agent least-privilege execution role ----
        # ONLY when the canvas Identity node sets mode == 'per_agent'. The
        # shared-role default (below) is 100% unchanged for everyone else.
        identity_config = event.get("identity_config") or {}
        identity_mode = identity_config.get("mode", "shared")
        if identity_mode == "per_agent":
            from app.services import per_agent_identity
            import time as _time

            account_id = step_clients.account_id_for_event(event)
            iam_client = step_clients.client(event, "iam")
            agentcore_runtime_name = (
                event.get("agentcore_runtime_name")
                or sanitize_runtime_name(config.get("name", "agent"))
            )
            pa_role_name = per_agent_identity.build_per_agent_role_name(
                agentcore_runtime_name
            )

            # Construct resource ARNs from the step results already on the
            # event (gateway/memory/kb expose IDs, not ARNs). Missing id ->
            # None -> the policy builder falls back to '*' for that one tool.
            gw_id = (event.get("gateway_result") or {}).get("gateway_id")
            gateway_arn = (
                f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gw_id}"
                if gw_id else None
            )
            mem_id = (event.get("memory_result") or {}).get("memory_id")
            memory_arn = (
                f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{mem_id}"
                if mem_id else None
            )
            kb_result = event.get("knowledge_base_result") or {}
            kb_id = kb_result.get("kb_id") or kb_result.get("knowledge_base_id")
            kb_arn = (
                f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/{kb_id}"
                if kb_id else None
            )

            otel_secret_arn = _resolve_otel_secret_arn(event)
            artifacts_bucket = _get_env("ARTIFACTS_BUCKET_NAME", "") or None

            # Tag the per-agent role ManagedBy=agentcore-flows (same as the
            # shared/runtime roles in runtime_deployer.create_runtime_iam_role) so
            # the tag-scoped delete grant can clean it up on teardown. Without the
            # tag, a future tightening of the role/AgentCore* grant would orphan
            # per-agent roles on deletion. (PR #3 review — mNemlaghi.)
            _managed_tag = [{"Key": "ManagedBy", "Value": "agentcore-flows"}]
            try:
                iam_client.create_role(
                    RoleName=pa_role_name,
                    AssumeRolePolicyDocument=json.dumps(
                        per_agent_identity.build_trust_policy()
                    ),
                    Description=(
                        f"Per-agent least-privilege role for {agentcore_runtime_name}"
                    ),
                    Tags=_managed_tag,
                )
            except iam_client.exceptions.EntityAlreadyExistsException:
                # Reused role — ensure the tag is present (idempotent).
                try:
                    iam_client.tag_role(RoleName=pa_role_name, Tags=_managed_tag)
                except Exception as _tag_err:  # noqa: BLE001
                    logger.warning(
                        "Could not tag reused per-agent role %s: %s",
                        pa_role_name,
                        _tag_err,
                    )
            pa_role_arn = iam_client.get_role(RoleName=pa_role_name)["Role"]["Arn"]
            iam_client.put_role_policy(
                RoleName=pa_role_name,
                PolicyName="AgentCoreRuntimePolicy",
                PolicyDocument=json.dumps(
                    per_agent_identity.build_scoped_runtime_policy(
                        connected_tools,
                        kb_arn=kb_arn,
                        gateway_arn=gateway_arn,
                        memory_arn=memory_arn,
                        otel_secret_arn=otel_secret_arn,
                        artifacts_bucket=artifacts_bucket,
                    )
                ),
            )
            # Bug 52/63: per-agent roles are minted fresh at deploy time, so
            # AgentCore's service-side IAM cache can lag (17-20 min observed).
            # create_agent_runtime's 8x5s transient-retry loop is the safety
            # net; this 15s sleep keeps the happy path one-shot. per_agent is
            # opt-in + slower-first-deploy and is NEVER the default.
            _time.sleep(15)
            logger.info("Using per-agent exec role %s (Gap P3.3B)", pa_role_arn)
            return {
                **event,
                "role_name": pa_role_name,
                "role_arn": pa_role_arn,
                "identity_mode": "per_agent",
                "iam_result": {
                    "success": True,
                    "message": f"Per-agent role {pa_role_name} ready",
                },
            }

        # Phase 7 (opt-in) cross-account — BEST PRACTICE (mirrors the home Bug-60
        # shared-role design). The platform's SHARED_RUNTIME_ROLE_ARN lives in the
        # HOME account, so CreateAgentRuntime in a TARGET account can't pass it.
        # AgentCore's guidance is to use a STABLE, PRE-PROVISIONED exec role —
        # never mint-and-immediately-use (the fresh-role IAM-propagation race that
        # CREATE_FAILs a runtime for ~17-20 min). So a cross-account deploy uses a
        # well-known role the target-account owner pre-created (+ pre-warmed) as an
        # onboarding step: `AgentCoreFlowsRuntimeRole`. We pass it by ARN (built
        # from the target account id) exactly like the home shared role — zero
        # deploy-time role creation, zero propagation race. Overridable per target
        # via a `runtime_role_name` on the deploy_target config (defaults below).
        _cross_account = bool(event.get("target_account_id"))
        if _cross_account:
            _tgt_acct = event["target_account_id"]
            _rt_role_name = event.get("target_runtime_role_name") or "AgentCoreFlowsRuntimeRole"
            _xacct_role_arn = f"arn:aws:iam::{_tgt_acct}:role/{_rt_role_name}"
            logger.info("Cross-account: using pre-provisioned target runtime role %s", _xacct_role_arn)
            return {
                **event,
                "role_name": _rt_role_name,
                "role_arn": _xacct_role_arn,
                "iam_result": {
                    "success": True,
                    "message": f"Using pre-provisioned target-account runtime role {_rt_role_name}",
                },
            }

        shared_role_arn = _get_env("SHARED_RUNTIME_ROLE_ARN", "").strip()
        if shared_role_arn:
            logger.info("Using shared runtime exec role %s (Bug 60)", shared_role_arn)
            shared_role_name = shared_role_arn.rsplit("/", 1)[-1]
            return {
                **event,
                "role_name": shared_role_name,
                "role_arn": shared_role_arn,
                "iam_result": {
                    "success": True,
                    "message": f"Using shared runtime role {shared_role_name}",
                },
            }

        # Legacy per-deploy role path (kept for backward compat with stacks
        # that don't have SHARED_RUNTIME_ROLE_ARN injected).
        role_name = f"AgentCoreRuntime-{runtime_name}"
        iam_client = step_clients.client(event, "iam")
        account_id = step_clients.account_id_for_event(event)

        # Pass through the OTEL auth secret ARN so the role can resolve
        # OTLP headers at agent boot via secretsmanager:GetSecretValue.
        otel_secret_arn = _resolve_otel_secret_arn(event)

        role_arn = create_runtime_iam_role(
            iam_client=iam_client,
            role_name=role_name,
            account_id=account_id,
            region=region,
            connected_tools=connected_tools,
            otel_secret_arn=otel_secret_arn,
            # Phase 2 (Loom) governance tagging — resolved at deploy start and
            # threaded through the SFN input; applied to the runtime exec role.
            resource_tags=event.get("resource_tags") or {},
        )

        return {
            **event,
            "role_name": role_name,
            "role_arn": role_arn,
            "iam_result": {"success": True, "message": f"Role {role_name} ready"},
        }

    except Exception:
        logger.exception("IAM step failed for deployment %s", deployment_id)
        raise
