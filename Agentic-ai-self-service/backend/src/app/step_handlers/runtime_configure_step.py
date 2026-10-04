"""Step handler: Create AgentCore runtime via boto3 API.

Requirements: 3.5
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
from app.services import step_clients
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import validate_token_endpoint_shape
from app.services.observability import (
    build_otel_env_vars,
)
from app.services.observability import (
    get_platform_observability_defaults_lenient as get_platform_observability_defaults,
)
from app.services.region_models import (
    region_inference_prefix,
    to_regional_model_id,
    to_regional_model_id_for_provider,
)
from app.services.runtime_deployer import (
    canvas_model_providers,
    create_agent_runtime,
    govern_default_runtime_log_group,
    needs_provider_api_key,
    runtime_key_grant_targets,
    sanitize_runtime_name,
)

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# The prefix rule lives in app.services.region_models so codegen and this
# handler cannot drift: whatever MODEL_ID we set here is what the deployed agent
# actually invokes, and a `us.` inference profile does not exist in eu-central-1.
#
# These two are re-exports kept for the tests that pin this handler against that
# module. The handler itself calls `to_regional_model_id_for_provider`, because the
# rule is Bedrock-only — see the MODEL_ID assignment below.
_region_inference_prefix = region_inference_prefix
_to_cross_region_model_id = to_regional_model_id


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
        region = event.get("target_region") or _get_env(
            "APP_AWS_REGION",
            _get_env("AWS_REGION", "us-east-1"),
        )

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
        # A protocol-only FastMCP server never invokes a model. It must NOT carry
        # MODEL_ID, a provider API-key reference, or a provider base URL — those are
        # model-runtime concerns, and leaking them onto a tool-only runtime is the
        # same model-free contract violation the MCP execution role guards against.
        _is_mcp_runtime = event.get("runtime_artifact_kind") == "mcp"
        model_cfg = None if _is_mcp_runtime else config.model
        if model_cfg:
            # model_cfg may be a Pydantic model or a plain dict depending on serialization
            if hasattr(model_cfg, "modelId"):
                raw_model_id = model_cfg.modelId or ""
            elif isinstance(model_cfg, dict):
                raw_model_id = model_cfg.get("modelId", model_cfg.get("model_id", ""))
            else:
                raw_model_id = ""
            # Provider-aware on purpose. This line used to prefix unconditionally, and
            # an OpenAI agent deployed through the real API came up with
            # MODEL_ID = us.gpt-4o-mini — the Bedrock cross-region inference-profile
            # namespace pasted onto a model in OpenAI's catalog, where it is not a
            # namespace but part of the name. Only the Bedrock and SageMaker branches of
            # _get_model_init_code read MODEL_ID at all, so for a foreign catalog this
            # is an env var the agent ignores; it still has to be right, because it is
            # what an operator reads out of GetAgentRuntime when an agent misbehaves.
            env_vars["MODEL_ID"] = to_regional_model_id_for_provider(
                raw_model_id, canvas_model_providers(config)[0], region
            )

        # Non-Bedrock providers (openai/anthropic/gemini/litellm/mistral/…) need a
        # model-provider API key. Hand over the REFERENCE, never the value:
        # PROVIDER_API_KEY_SECRET_ARN is the agent's provider_api_key_ref, and the
        # generated agent dereferences it inside the container (_provider_api_key in
        # services/code_generator.py).
        #
        # This used to resolve the secret here and inject the plaintext as
        # PROVIDER_API_KEY. That is not a place a secret can live: GetAgentRuntime
        # returns a runtime's environment variables verbatim, so the key was readable
        # by any principal holding that one describe call, and every Task in this
        # state machine re-emits the whole event into the execution history. Same
        # reasoning, and the same fix, as OAUTH_CLIENT_SECRET_REF below.
        # ARCC cnt_dAiE0OyXKvfeow / cnt_n8LpZcqYi2t3I2 / cnt_77BHvX7WzuG1X8.
        #
        # The reference is safe to hand over because POST /api/deploy already
        # live-validated the long-lived `agentcore-provider/` source, copied its raw
        # value into this deployment's `agentcore-connector/` secret in the target
        # account, and replaced provider_api_key_ref with that copied ARN. The runtime
        # can read deployment-bound connector secrets, but not another tenant's
        # long-lived provider source. Kept in sync with build_shared_runtime_role
        # (CDK), per_agent_identity.build_scoped_runtime_policy and
        # create_runtime_iam_role.
        #
        # Without a key at all, a non-Bedrock provider deploys an agent whose model
        # calls 401 (provider_api_key_ref was once consumed NOWHERE). PROVIDER_BASE_URL
        # supports OpenAI-compatible gateways / a LiteLLM proxy and is not a secret.
        #
        # runtime_key_grant_targets is THE shared decision: iam_step calls the same
        # function to grant the read. Deciding here and granting there independently is
        # how an agent ends up deploying green and raising AccessDeniedException on its
        # first model call. It also asks a wider question than the old gate did — the
        # old one read only the parent's provider, so a Bedrock parent with one OpenAI
        # sub-agent was handed no key at all and that sub-agent's first call 401'd.
        _provider_key_arn, _gateway_key_arn = (
            (None, None) if _is_mcp_runtime else runtime_key_grant_targets(config, event.get("gateway_result"))
        )
        if _provider_key_arn:
            env_vars["PROVIDER_API_KEY_SECRET_ARN"] = _provider_key_arn
        if not _is_mcp_runtime and needs_provider_api_key(config):
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

        if idp_provider == "litellm":
            # Workstream A: a LiteLLM MCP Gateway authenticates with a STATIC
            # virtual key, not an OAuth2 client-credentials exchange. Tell the
            # generated agent to skip the token endpoint entirely and send the key
            # on every MCP request.
            #
            # The key travels BY REFERENCE. This branch used to resolve it here and
            # inject the value as GATEWAY_API_KEY, on the reasoning that "the runtime
            # has no secretsmanager grant of its own" — which was wrong twice over:
            # the runtime role does hold GetSecretValue on the `agentcore-connector/`
            # namespace this ref lives in, and an env var is not a place a secret can
            # live, because GetAgentRuntime returns runtime environment variables in
            # plaintext and every Task here re-emits the whole event into the
            # execution history. The generated agent already preferred the reference
            # (_resolve_gateway_key in services/code_generator.py) — only the CFN
            # export was using it. ARCC cnt_dAiE0OyXKvfeow / cnt_n8LpZcqYi2t3I2.
            env_vars["GATEWAY_AUTH_MODE"] = "static_bearer"
            # Pinned MCP server aliases ride the x-mcp-servers header. Only set
            # when the canvas pinned some — empty means "every server the key sees".
            _litellm_servers = [str(s) for s in (gateway_result.get("litellm_servers") or []) if str(s).strip()]
            if _litellm_servers:
                env_vars["GATEWAY_MCP_SERVERS"] = ",".join(_litellm_servers)
            if _gateway_key_arn:
                # From runtime_key_grant_targets above, the same call iam_step makes to
                # grant the read. litellm_gateway_deployer guarantees this is a
                # platform-minted `agentcore-connector/` secret (it validates a
                # round-tripped litellm_api_key_ref and mints any inline key itself),
                # which is exactly the namespace the runtime role is scoped to. Nothing
                # to resolve and nothing to log.
                env_vars["GATEWAY_API_KEY_SECRET_ARN"] = _gateway_key_arn
            else:
                logger.warning("LiteLLM gateway produced no api_key_ref; the agent will have no gateway key")
        elif idp_provider == "cognito" or not idp_provider:
            # Cognito env vars
            if client_info.get("client_id"):
                env_vars["COGNITO_CLIENT_ID"] = client_info["client_id"]
            # COGNITO_USER_POOL_ID, *not* COGNITO_CLIENT_SECRET. GetAgentRuntime
            # returns a runtime's environment variables in plaintext, so an env var
            # is not a place a secret can live; the generated agent's
            # _resolve_client_secret() re-reads it with DescribeUserPoolClient from
            # exactly these two values (code_generator.py). This is what the
            # CloudFormation export already did — the live path was the one holding
            # the plaintext. ARCC cnt_n8LpZcqYi2t3I2 / cnt_77BHvX7WzuG1X8.
            #
            # A legacy `client_secret` still present in client_info (a deployment
            # created before this change, re-configured now) is deliberately NOT
            # forwarded: there is nothing it would enable that the pool id does not.
            #
            # PREFER the Secrets Manager reference. Handing over the pool id requires
            # the runtime to hold cognito-idp:DescribeUserPoolClient, and Cognito IAM
            # has no granularity below the pool — so that grant necessarily also reads
            # every OTHER gateway's client secret in the same pool (shared mode) or in
            # any pool carrying this stack's owner tag (dedicated mode). The reference
            # is scopeable to one ARN, which is what makes per_agent mode isolating.
            # The pool id is still emitted as a FALLBACK only when no ref exists, i.e.
            # for a deployment created before the ref did. See
            # gateway_deployer._mint_client_secret_ref.
            _cognito_ref = client_info.get("client_secret_ref") or client_info.get("clientSecretRef")
            if _cognito_ref:
                env_vars["OAUTH_CLIENT_SECRET_REF"] = _cognito_ref
            elif client_info.get("user_pool_id"):
                env_vars["COGNITO_USER_POOL_ID"] = client_info["user_pool_id"]
            if client_info.get("token_endpoint"):
                # Validated before it is handed to the agent, because the agent POSTs
                # its own client secret here from inside the runtime where none of the
                # platform's guards run (F-6). Shape-only, no DNS — see
                # validate_token_endpoint_shape for why resolving at deploy time would
                # prove nothing about the runtime's request and would make a resolver
                # hiccup fail a deploy. Fail-closed: a runtime that would send its
                # credential over cleartext http, or to a link-local address, must not
                # be created.
                env_vars["COGNITO_TOKEN_ENDPOINT"] = validate_token_endpoint_shape(
                    client_info["token_endpoint"], label="COGNITO_TOKEN_ENDPOINT"
                )
            if client_info.get("scope"):
                env_vars["COGNITO_SCOPE"] = client_info["scope"]
        else:
            # External IDP env vars (Okta, Azure AD, Auth0, custom)
            env_vars["AUTH_PROVIDER"] = idp_provider
            if client_info.get("client_id"):
                env_vars["OAUTH_CLIENT_ID"] = client_info["client_id"]
            # The REFERENCE, never the secret — same reason as the Cognito branch
            # above. For an external IDP there is no DescribeUserPoolClient to fall
            # back on, so the agent dereferences this Secrets Manager name itself
            # (_resolve_client_secret in the generated code). Before this, the
            # reference was injected under the name OAUTH_CLIENT_SECRET and sent
            # verbatim as the OAuth client_secret, which no IDP would accept.
            _external_ref = client_info.get("client_secret_ref") or client_info.get("clientSecretRef")
            if _external_ref:
                env_vars["OAUTH_CLIENT_SECRET_REF"] = _external_ref
            if client_info.get("token_endpoint"):
                # Same guard as the Cognito branch above, and this is the branch that
                # needs it: for an external IDP the endpoint was chosen by the OIDC
                # discovery document, not derived by this platform.
                env_vars["OAUTH_TOKEN_ENDPOINT"] = validate_token_endpoint_shape(
                    client_info["token_endpoint"], label="OAUTH_TOKEN_ENDPOINT"
                )
            if client_info.get("scope"):
                env_vars["OAUTH_SCOPE"] = client_info["scope"]

        # Inject OTLP observability env vars. Single source of truth shared
        # with the direct-deploy and CFN paths. When platform-level OTEL is
        # configured (SSM /agentcore-workflow/{env}/otel/*), per-canvas values
        # for endpoint/secret/sample are dropped and platform values win.
        platform_observability_defaults = (
            event.get("platform_observability_defaults")
            if "platform_observability_defaults" in event
            else get_platform_observability_defaults()
        )
        otel_env = build_otel_env_vars(
            event.get("observability_config")
            or (config.observability.model_dump() if getattr(config, "observability", None) else None),
            runtime_name=runtime_name,
            deployment_id=deployment_id,
            enable_otel_legacy=bool(getattr(config, "enable_otel", False)),
            platform_defaults=platform_observability_defaults,
            trusted_auth_secret_arns=event.get("recorded_secret_arns") or (),
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
                _policies = ApprovalPolicyStore(_pol_table, _region).list(event.get("owner_org") or "default")
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
        is_a2a = (config.protocol or "HTTP").upper() == "A2A" or "a2a" in (event.get("connected_tools") or [])
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
            region=region,
            # P0-B governance tags. Verified present in THIS state's input on a live
            # execution (get-execution-history, ConfigureRuntime TaskStateEntered) before
            # this read was added: no SFN Task in this machine sets `Parameters`, so every
            # state receives the whole state document, and every handler propagates it. A
            # read that silently resolved to {} would have been indistinguishable from the
            # defect it fixes.
            resource_tags=event.get("resource_tags") or {},
        )

        # Manifest: record the runtime for generic teardown right after create
        # succeeds (runtime_launch's readiness wait can be killed mid-poll,
        # otherwise leaking the runtime). Best-effort: never fails the deploy.
        store.record_resource(
            deployment_id,
            {
                "type": "agent_runtime",
                "id": runtime_result["runtime_id"],
                "name": event.get("friendly_runtime_name") or runtime_name,
                "region": region,
                "created_by_deployment": (runtime_result.get("created_by_deployment") is True),
            },
        )
        # AgentCore creates the DEFAULT endpoint's CloudWatch group outside this
        # stack and otherwise leaves conversation content there forever.  The
        # runtime row above is written first so a governance failure still leaves
        # teardown an exact runtime handle.
        govern_default_runtime_log_group(
            step_clients.client(event, "logs", region_name=region),
            runtime_result["runtime_id"],
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
                    {
                        "type": "iam_role",
                        "name": role_name,
                        "region": region,
                        "created_by_deployment": (event.get("role_created_by_deployment") is True),
                    },
                )

        return {
            **event,
            "runtime_id": runtime_result["runtime_id"],
            "runtime_arn": runtime_result.get("arn", ""),
            "configure_result": {
                "success": True,
                "runtime_id": runtime_result["runtime_id"],
                "created_by_deployment": (runtime_result.get("created_by_deployment") is True),
            },
        }

    except Exception:
        logger.exception("Runtime configure step failed for deployment %s", deployment_id)
        raise
