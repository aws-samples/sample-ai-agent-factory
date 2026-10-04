"""Step handler: Create IAM execution role for the runtime.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import json
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.iam_boundary import create_role_kwargs, ensure_role_boundary
from app.services.naming import regional_iam_role_name
from app.services.observability import (
    _validate_user_otel_secret_arn,
)
from app.services.observability import (
    get_platform_observability_defaults_lenient as get_platform_observability_defaults,
)
from app.services.resource_ownership import assert_this_deployment_may_mutate
from app.services.resource_tagging import governed_tag_list
from app.services.runtime_deployer import (
    client_secret_grant_targets,
    create_runtime_iam_role,
    runtime_key_grant_targets,
    sanitize_runtime_name,
)

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
    has_frozen_platform_defaults = "platform_observability_defaults" in event
    platform_defaults = (
        event.get("platform_observability_defaults")
        if has_frozen_platform_defaults
        else get_platform_observability_defaults()
    )
    trusted_secret_arns = {str(arn) for arn in (event.get("recorded_secret_arns") or []) if arn}
    if platform_defaults and platform_defaults.get("auth_header_secret_arn"):
        platform_secret_arn = platform_defaults["auth_header_secret_arn"]
        if not has_frozen_platform_defaults or platform_secret_arn in trusted_secret_arns:
            return platform_secret_arn
        try:
            _validate_user_otel_secret_arn(platform_secret_arn)
        except ValueError as e:
            logger.warning(
                "Frozen platform OTEL authentication reference was not deployment-bound "
                "(%s); disabling OTEL auth for this runtime.",
                type(e).__name__,
            )
            return None
        return platform_secret_arn

    obs_cfg = event.get("observability_config") or {}
    otel_secret_arn = obs_cfg.get("auth_header_secret_arn") or obs_cfg.get("authHeaderSecretArn")
    if otel_secret_arn and otel_secret_arn not in trusted_secret_arns:
        try:
            _validate_user_otel_secret_arn(otel_secret_arn)
        except ValueError as e:
            logger.warning(
                "Per-canvas OTEL authentication reference rejected (%s); disabling OTEL auth for this runtime.",
                type(e).__name__,
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
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )

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
        if event.get("target_account_id") and identity_mode == "per_agent":
            raise RuntimeError(
                "Cross-account deployment cannot mint and immediately use a "
                "per-agent execution role. Register and use the stable target "
                "Runtime role, or deploy per-agent identity in the platform account."
            )
        if identity_mode == "per_agent":
            import time as _time

            from app.services import per_agent_identity

            account_id = step_clients.account_id_for_event(event)
            iam_client = step_clients.client(event, "iam")
            agentcore_runtime_name = event.get("agentcore_runtime_name") or sanitize_runtime_name(
                config.get("name", "agent")
            )
            pa_role_name = per_agent_identity.build_per_agent_role_name(
                agentcore_runtime_name,
                region=region,
            )

            # Construct resource ARNs from the step results already on the
            # event (gateway/memory/kb expose IDs, not ARNs). Missing id ->
            # None -> the policy builder falls back to '*' for that one tool.
            gw_id = (event.get("gateway_result") or {}).get("gateway_id")
            gateway_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gw_id}" if gw_id else None
            mem_id = (event.get("memory_result") or {}).get("memory_id")
            memory_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{mem_id}" if mem_id else None
            kb_result = event.get("knowledge_base_result") or {}
            kb_id = kb_result.get("kb_id") or kb_result.get("knowledge_base_id")
            kb_arn = f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/{kb_id}" if kb_id else None

            otel_secret_arn = _resolve_otel_secret_arn(event)
            artifacts_bucket = _get_env("ARTIFACTS_BUCKET_NAME", "") or None

            # The gateway's OAuth client secret is never injected as an env var
            # (GetAgentRuntime returns those in plaintext), so the agent resolves it
            # at the moment of use. Grant exactly the one read that needs: the pool
            # for a Cognito gateway, or the one secret for an external IDP. Both come
            # off client_info, which the gateway step has already put on the event.
            user_pool_arn, client_secret_arn = client_secret_grant_targets(
                (event.get("gateway_result") or {}).get("client_info"), region, account_id
            )
            # Same contract for the model-provider API key and a LiteLLM gateway's
            # virtual key. runtime_configure_step injects these two ARNs from the
            # SAME helper, so the injection and the grant cannot drift — and an
            # injection without a grant is an agent that deploys green and then
            # raises AccessDeniedException on its first model call.
            provider_key_arn, gateway_key_arn = runtime_key_grant_targets(config, event.get("gateway_result"))

            # Tag the per-agent role ManagedBy=agentcore-flows (same as the
            # shared/runtime roles in runtime_deployer.create_runtime_iam_role) so
            # the tag-scoped delete grant can clean it up on teardown. Without the
            # tag, a future tightening of the role/AgentCore* grant would orphan
            # per-agent roles on deletion. (PR #3 review — mNemlaghi.)
            # Also carries AgentCoreStack={project}-{env}-{region}; see
            # services/resource_ownership.py. ManagedBy names the product, so it
            # cannot distinguish two deployments sharing this account, and IAM
            # role names are account-global.
            _managed_tag = governed_tag_list(region, event.get("resource_tags"))
            _existing_role: dict | None = None
            _role_created = True
            try:
                iam_client.create_role(
                    RoleName=pa_role_name,
                    AssumeRolePolicyDocument=json.dumps(per_agent_identity.build_trust_policy()),
                    Description=(f"Per-agent least-privilege role for {agentcore_runtime_name}"),
                    Tags=_managed_tag,
                    **create_role_kwargs(),
                )
            except iam_client.exceptions.EntityAlreadyExistsException:
                _role_created = False
                # The role name collided, and the exception cannot say why: this is
                # either our own redeploy of this agent or something else in the
                # account already holding the name (role names are account-global and
                # this one is derived from a user-chosen agent name). So read the tags
                # off the role BEFORE touching it -- `get_role` returns `Role.Tags`
                # and is already granted on `role/AgentCore*`, so proving ownership
                # costs no new IAM permission and no extra call. Tagging first, as
                # this did, stamped our tag on a role we had not yet shown was ours.
                _existing_role = iam_client.get_role(RoleName=pa_role_name)["Role"]
                assert_this_deployment_may_mutate(f"IAM role {pa_role_name}", _existing_role.get("Tags"), region)
                # F-06: a per-agent role minted before the boundary existed is retrofitted here,
                # after ownership is proven and before the inline policy below is rewritten.
                ensure_role_boundary(iam_client, pa_role_name, role=_existing_role)
                # Ours -- ensure the tag is present (idempotent).
                try:
                    iam_client.tag_role(RoleName=pa_role_name, Tags=_managed_tag)
                except Exception as _tag_err:  # noqa: BLE001
                    logger.warning(
                        "Could not tag reused per-agent role %s: %s",
                        pa_role_name,
                        _tag_err,
                    )
            pa_role_arn = (_existing_role or iam_client.get_role(RoleName=pa_role_name)["Role"])["Arn"]
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
                        user_pool_arn=user_pool_arn,
                        client_secret_arn=client_secret_arn,
                        provider_key_secret_arn=provider_key_arn,
                        gateway_key_secret_arn=gateway_key_arn,
                        model_free=(event.get("runtime_artifact_kind") == "mcp"),
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
                "role_created_by_deployment": _role_created,
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
            from app.services.deploy_target import (
                DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
                DEFAULT_TARGET_RUNTIME_ROLE_NAME,
                target_execution_role_arn,
            )

            _tgt_acct = event["target_account_id"]
            # A protocol-only FastMCP server gets the model-free MCP runtime role,
            # never the model-capable Runtime role. Anything else (Strands agents)
            # keeps the model-capable role. This mirrors the home-account model-free
            # runtime contract into the cross-account path.
            _is_mcp = event.get("runtime_artifact_kind") == "mcp"
            if _is_mcp:
                _xacct_role_arn = target_execution_role_arn(
                    _tgt_acct,
                    role_arn=event.get("target_mcp_runtime_role_arn"),
                    default_role_name=DEFAULT_TARGET_MCP_RUNTIME_ROLE_NAME,
                )
            else:
                _xacct_role_arn = target_execution_role_arn(
                    _tgt_acct,
                    role_arn=event.get("target_runtime_role_arn"),
                    default_role_name=DEFAULT_TARGET_RUNTIME_ROLE_NAME,
                )
            _rt_role_name = _xacct_role_arn.rsplit("/", 1)[-1]
            logger.info("Cross-account: using pre-provisioned target runtime role %s", _xacct_role_arn)
            return {
                **event,
                "role_name": _rt_role_name,
                "role_arn": _xacct_role_arn,
                "role_created_by_deployment": False,
                "iam_result": {
                    "success": True,
                    "message": f"Using pre-provisioned target-account runtime role {_rt_role_name}",
                },
            }

        # A protocol-only FastMCP server gets the model-free shared role when the
        # platform provisions a distinct one; the model-capable shared role would
        # let a tool-only runtime invoke arbitrary models, breaking the model-free
        # runtime contract. Falls back to the model-capable role only if no
        # dedicated MCP role is injected, so existing single-role stacks are
        # unaffected.
        _is_mcp_artifact = event.get("runtime_artifact_kind") == "mcp"
        shared_mcp_role_arn = _get_env("SHARED_MCP_RUNTIME_ROLE_ARN", "").strip()
        shared_role_arn = _get_env("SHARED_RUNTIME_ROLE_ARN", "").strip()
        if _is_mcp_artifact and shared_mcp_role_arn:
            shared_role_arn = shared_mcp_role_arn
        if shared_role_arn:
            logger.info(
                "Using shared %sruntime exec role %s (Bug 60)",
                "model-free MCP " if (_is_mcp_artifact and shared_mcp_role_arn) else "",
                shared_role_arn,
            )
            shared_role_name = shared_role_arn.rsplit("/", 1)[-1]
            return {
                **event,
                "role_name": shared_role_name,
                "role_arn": shared_role_arn,
                "role_created_by_deployment": False,
                "iam_result": {
                    "success": True,
                    "message": f"Using shared runtime role {shared_role_name}",
                },
            }

        # Legacy per-deploy role path (kept for backward compat with stacks
        # that don't have SHARED_RUNTIME_ROLE_ARN injected).
        role_name = regional_iam_role_name(
            f"AgentCoreRuntime-{runtime_name}",
            region,
        )
        iam_client = step_clients.client(event, "iam")
        account_id = step_clients.account_id_for_event(event)

        # Pass through the OTEL auth secret ARN so the role can resolve
        # OTLP headers at agent boot via secretsmanager:GetSecretValue.
        otel_secret_arn = _resolve_otel_secret_arn(event)
        # Same client-secret grant as the per-agent path above: the secret is no
        # longer injected as an env var, so the role must be able to resolve it.
        _legacy_pool_arn, _legacy_secret_arn = client_secret_grant_targets(
            (event.get("gateway_result") or {}).get("client_info"), region, account_id
        )
        _legacy_provider_key_arn, _legacy_gateway_key_arn = runtime_key_grant_targets(
            config, event.get("gateway_result")
        )

        _role_result = create_runtime_iam_role(
            iam_client=iam_client,
            role_name=role_name,
            account_id=account_id,
            region=region,
            connected_tools=connected_tools,
            otel_secret_arn=otel_secret_arn,
            user_pool_arn=_legacy_pool_arn,
            client_secret_arn=_legacy_secret_arn,
            provider_key_secret_arn=_legacy_provider_key_arn,
            gateway_key_secret_arn=_legacy_gateway_key_arn,
            # Phase 2 (Loom) governance tagging — resolved at deploy start and
            # threaded through the SFN input; applied to the runtime exec role.
            resource_tags=event.get("resource_tags") or {},
            return_provenance=True,
        )
        if isinstance(_role_result, tuple):
            role_arn, role_created = _role_result
        else:
            # Rolling/mocked callers predating provenance return only the ARN.
            # Unknown provenance must fail closed: retaining an orphan is
            # recoverable; deleting a reused role is not.
            role_arn, role_created = _role_result, False

        return {
            **event,
            "role_name": role_name,
            "role_arn": role_arn,
            "role_created_by_deployment": role_created,
            "iam_result": {"success": True, "message": f"Role {role_name} ready"},
        }

    except Exception:
        logger.exception("IAM step failed for deployment %s", deployment_id)
        raise
