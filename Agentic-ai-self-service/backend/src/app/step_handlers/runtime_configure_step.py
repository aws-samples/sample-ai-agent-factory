"""Step handler: Create AgentCore runtime via boto3 API.

Requirements: 3.5
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
import re

import boto3

from app.models.deployment_models import (
    DeploymentStatusEnum,
    DeploymentStepName,
    RuntimeConfig,
)
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.observability import build_otel_env_vars, get_platform_observability_defaults
from app.services.runtime_deployer import create_agent_runtime, sanitize_runtime_name

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _to_cross_region_model_id(model_id: str) -> str:
    """Ensure model ID uses cross-region inference profile format.

    Appends the ``-v1:0`` version suffix only to LEGACY date-suffixed IDs
    (e.g. ``us.anthropic.claude-haiku-4-5-20251001``). Current-generation
    dateless IDs (``us.anthropic.claude-sonnet-5``) must pass through
    unchanged — appending ``-v1:0`` produces an invalid model identifier.
    Mirrors code_generator._to_cross_region_model_id.
    """
    if not model_id:
        return model_id
    if not model_id.startswith(("us.", "global.", "eu.", "ap.")):
        model_id = f"us.{model_id}"
    # Only legacy DATED inference profiles require a -v1:0 version suffix.
    if (
        "anthropic." in model_id
        and re.search(r"-\d{8}$", model_id)
        and not re.search(r"-v\d+:\d+$", model_id)
    ):
        model_id = f"{model_id}-v1:0"
    return model_id


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
            DeploymentStepName.RUNTIME_CONFIGURE,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        config_dict = event.get("config", {})
        config = RuntimeConfig.model_validate(config_dict)
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))

        # Phase 1 Gap 1A — versioning. Use the version-suffixed AgentCore
        # runtime name minted in deployment_handler.handle_deploy. Falls back
        # to the legacy naming for any caller bypassing the deployment handler
        # (direct deploys via services/deployment.py).
        runtime_name = event.get("agentcore_runtime_name") or sanitize_runtime_name(config.name)
        role_arn = event.get("role_arn", "")
        s3_bucket = event.get("s3_bucket", "")
        s3_key = event.get("s3_key", "")
        entrypoint = event.get("entrypoint", config.entrypoint or "agent.py")

        if not role_arn:
            raise RuntimeError("No role_arn provided from IAM step")
        if not s3_bucket:
            raise RuntimeError("No s3_bucket provided from codegen step")

        agentcore_ctrl = step_clients.client(event, "bedrock-agentcore-control")

        # Build environment variables for the runtime
        env_vars = {}
        model_cfg = config.model
        if model_cfg:
            # model_cfg may be a Pydantic model or a plain dict depending on serialization
            if hasattr(model_cfg, "modelId"):
                raw_model_id = model_cfg.modelId or ""
            elif isinstance(model_cfg, dict):
                raw_model_id = model_cfg.get("modelId", model_cfg.get("model_id", ""))
            else:
                raw_model_id = ""
            env_vars["MODEL_ID"] = _to_cross_region_model_id(raw_model_id)

        # Non-Bedrock providers (openai/anthropic/gemini/litellm/mistral/…) read
        # their credential from PROVIDER_API_KEY. Resolve it from the agent's
        # provider_api_key_ref (a Secrets Manager ARN) at deploy time and inject
        # the plaintext value as an env var — same pattern as COGNITO_CLIENT_SECRET
        # below, so no extra runtime IAM grant is needed. Without this, selecting
        # any non-Bedrock provider deploys an agent whose model calls 401
        # (provider_api_key_ref was previously consumed NOWHERE). PROVIDER_BASE_URL
        # supports OpenAI-compatible gateways / a LiteLLM proxy.
        _provider = getattr(config, "model_provider", None)
        if _provider is None and isinstance(model_cfg, dict):
            _provider = model_cfg.get("provider")
        elif _provider is None and hasattr(model_cfg, "provider"):
            _provider = getattr(model_cfg, "provider", None)
        _provider = _provider or "bedrock"
        if str(_provider).lower() not in ("bedrock", ""):
            _key_ref = getattr(config, "provider_api_key_ref", None)
            if _key_ref:
                try:
                    _sm = step_clients.client(event, "secretsmanager")
                    _val = _sm.get_secret_value(SecretId=_key_ref).get("SecretString", "")
                    if _val:
                        env_vars["PROVIDER_API_KEY"] = _val
                except Exception:  # noqa: BLE001
                    logger.warning("Could not resolve provider_api_key_ref for %s provider", _provider)
            _base_url = getattr(config, "provider_base_url", None)
            if _base_url:
                env_vars["PROVIDER_BASE_URL"] = str(_base_url)

        gateway_result = event.get("gateway_result") or {}
        memory_result = event.get("memory_result") or {}
        guardrails_result = event.get("guardrails_result") or {}
        if guardrails_result.get("guardrail_id"):
            env_vars["GUARDRAIL_ID"] = guardrails_result["guardrail_id"]
            env_vars["GUARDRAIL_VERSION"] = guardrails_result.get("guardrail_version", "DRAFT")
        if gateway_result.get("gateway_url"):
            env_vars["GATEWAY_URL"] = gateway_result["gateway_url"]
        if memory_result.get("memory_id"):
            env_vars["MEMORY_ID"] = memory_result["memory_id"]

        # Inject knowledge base id so the agent's retrieve_from_kb tool can
        # call bedrock-agent-runtime:Retrieve. See tasks/lessons.md Bug 87.
        kb_result = event.get("knowledge_base_result") or {}
        if kb_result.get("knowledge_base_id"):
            env_vars["KB_ID"] = kb_result["knowledge_base_id"]
        elif kb_result.get("kb_id"):
            env_vars["KB_ID"] = kb_result["kb_id"]
        client_info = gateway_result.get("client_info") or {}
        idp_provider = client_info.get("provider", "cognito")

        if idp_provider == "cognito" or not idp_provider:
            # Cognito env vars (existing behavior)
            if client_info.get("client_id"):
                env_vars["COGNITO_CLIENT_ID"] = client_info["client_id"]
            if client_info.get("client_secret"):
                env_vars["COGNITO_CLIENT_SECRET"] = client_info["client_secret"]
            if client_info.get("token_endpoint"):
                env_vars["COGNITO_TOKEN_ENDPOINT"] = client_info["token_endpoint"]
            if client_info.get("scope"):
                env_vars["COGNITO_SCOPE"] = client_info["scope"]
        else:
            # External IDP env vars (Okta, Azure AD, Auth0, custom)
            env_vars["AUTH_PROVIDER"] = idp_provider
            if client_info.get("client_id"):
                env_vars["OAUTH_CLIENT_ID"] = client_info["client_id"]
            if client_info.get("client_secret"):
                env_vars["OAUTH_CLIENT_SECRET"] = client_info["client_secret"]
            if client_info.get("token_endpoint"):
                env_vars["OAUTH_TOKEN_ENDPOINT"] = client_info["token_endpoint"]
            if client_info.get("scope"):
                env_vars["OAUTH_SCOPE"] = client_info["scope"]

        # Inject OTLP observability env vars. Single source of truth shared
        # with the direct-deploy and CFN paths. When platform-level OTEL is
        # configured (SSM /agentcore-workflow/{env}/otel/*), per-canvas values
        # for endpoint/secret/sample are dropped and platform values win.
        otel_env = build_otel_env_vars(
            event.get("observability_config") or (
                config.observability.model_dump() if getattr(config, "observability", None) else None
            ),
            runtime_name=runtime_name,
            deployment_id=deployment_id,
            enable_otel_legacy=bool(getattr(config, "enable_otel", False)),
            platform_defaults=get_platform_observability_defaults(),
        )
        env_vars.update(otel_env)

        # Phase 2 Gap 2D — human-in-the-loop. The injected human_approval @tool
        # writes PENDING rows keyed on the AgentCore runtime NAME (known here;
        # the canonical runtime_id does not exist until create_agent_runtime
        # returns, and env vars are fixed at create time). owner_sub rides the
        # SFN input from deployment_handler.handle_deploy so the owner_sub GSI
        # pending queue is populated for the right tenant.
        if "hitl" in (event.get("connected_tools") or []):
            hitl_table = _get_env("HITL_REQUESTS_TABLE_NAME", "")
            if hitl_table:
                env_vars["HITL_REQUESTS_TABLE_NAME"] = hitl_table
                env_vars["HITL_RUNTIME_ID"] = runtime_name
                env_vars["RUNTIME_OWNER_SUB"] = event.get("owner_sub", "")

        # Loom-study 2.2 — inject org-configured HITL approval policies so the
        # generated agent's BeforeToolInvocation hook (2.1) GUARANTEES a gate on
        # matching tools, independent of whether the model calls human_approval.
        # Also needs the HITL table (the hook records PENDING rows) even when the
        # "hitl" tool node isn't wired, so set the table when policies exist.
        try:
            from app.services.approval_policy_store import ApprovalPolicyStore, serialize_for_agent
            _pol_table = _get_env("TAG_POLICY_TABLE_NAME", "")
            if _pol_table:
                _region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
                _policies = ApprovalPolicyStore(_pol_table, _region).list(
                    event.get("owner_org") or "default"
                )
                _serialized = serialize_for_agent(_policies)
                if _serialized:
                    env_vars["LOOM_APPROVAL_POLICIES"] = _serialized
                    _hitl_table = _get_env("HITL_REQUESTS_TABLE_NAME", "")
                    if _hitl_table:
                        env_vars.setdefault("HITL_REQUESTS_TABLE_NAME", _hitl_table)
                        env_vars.setdefault("HITL_RUNTIME_ID", runtime_name)
                        env_vars.setdefault("RUNTIME_OWNER_SUB", event.get("owner_sub", ""))
        except Exception:  # noqa: BLE001 — policy injection must never fail a deploy
            logger.warning("approval-policy injection skipped")

        # Gap 3A - A2A. Inject agent-card + peer-allowlist env when the runtime
        # is A2A (by protocol OR by an 'a2a' tool node). The self-contained
        # agent reads these at runtime; absent vars fail-closed (no allowlist =>
        # all peers refused).
        is_a2a = (config.protocol or "HTTP").upper() == "A2A" or "a2a" in (
            event.get("connected_tools") or []
        )
        if is_a2a:
            a2a_cfg = event.get("a2a_config") or {}
            caps = a2a_cfg.get("capabilities") or []
            if caps:
                env_vars["A2A_CAPABILITIES"] = ",".join([str(c)[:64] for c in caps][:32])
            if a2a_cfg.get("advertised_description"):
                env_vars["A2A_ADVERTISED_DESCRIPTION"] = str(a2a_cfg["advertised_description"])[:512]
            allow = a2a_cfg.get("peer_allowlist") or []
            if allow:
                env_vars["A2A_PEER_ALLOWLIST"] = ",".join([str(u)[:512] for u in allow][:64])

        # Bug 129: the A2A agent is a SELF-CONTAINED interop layer that serves the
        # agent card + invoke over the standard BedrockAgentCoreApp HTTP entrypoint
        # (/invocations + an extra /.well-known/agent-card.json route). It does NOT
        # embed the a2a-sdk JSON-RPC server. So the control-plane serverProtocol
        # MUST be HTTP — setting it to "A2A" makes AgentCore probe for a native
        # A2A JSON-RPC server the container never starts, and every invoke fails
        # with HTTP 424 (Failed Dependency) + zero container logs. The A2A
        # behaviour is delivered by the agent-card route + env above, never by the
        # native server protocol. Any non-HTTP/MCP protocol value collapses to HTTP.
        server_protocol = (config.protocol or "HTTP").upper()
        if server_protocol not in ("HTTP", "MCP"):
            server_protocol = "HTTP"

        runtime_result = create_agent_runtime(
            agentcore_ctrl=agentcore_ctrl,
            runtime_name=runtime_name,
            role_arn=role_arn,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            entrypoint=entrypoint,
            python_runtime=config.python_runtime or "PYTHON_3_13",
            protocol=server_protocol,
            env_vars=env_vars if env_vars else None,
            vpc_config=getattr(config, "vpc_config", None),
        )

        # Manifest: record the runtime for generic teardown right after create
        # succeeds (runtime_launch's readiness wait can be killed mid-poll,
        # otherwise leaking the runtime). Best-effort: never fails the deploy.
        store.record_resource(
            deployment_id,
            {"type": "agent_runtime", "id": runtime_result["runtime_id"], "region": region},
        )
        # Per-deploy exec role minted by iam_step (mode == 'per_agent'); skip the
        # Bug-60 shared role, which is reused across every runtime in the stack.
        # Phase 7 (opt-in) cross-account: the target-account runtime role is
        # PRE-PROVISIONED by the target-account owner (the platform didn't create
        # it) — it is shared infrastructure like the home shared role and MUST
        # NOT be recorded/torn down (deleting it breaks every future deploy into
        # that account — observed live). Skip recording when cross-account.
        _cross_account = bool(event.get("target_account_id"))
        shared_role_arn = _get_env("SHARED_RUNTIME_ROLE_ARN", "")
        if role_arn and role_arn != shared_role_arn and not _cross_account:
            role_name = role_arn.rsplit("/", 1)[-1]
            if role_name and not role_name.endswith("-shared"):
                store.record_resource(
                    deployment_id,
                    {"type": "iam_role", "name": role_name, "region": region},
                )

        return {
            **event,
            "runtime_id": runtime_result["runtime_id"],
            "runtime_arn": runtime_result.get("arn", ""),
            "configure_result": {
                "success": True,
                "runtime_id": runtime_result["runtime_id"],
            },
        }

    except Exception:
        logger.exception("Runtime configure step failed for deployment %s", deployment_id)
        raise
