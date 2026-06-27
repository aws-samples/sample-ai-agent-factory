"""Step handler: Deploy MCP Server Runtime before Gateway.

Generates FastMCP server code, uploads to S3 with dependency bundle,
creates IAM role, sets up Cognito OAuth for gateway-to-runtime auth,
creates the runtime with MCP protocol + JWT authorizer, and waits
for it to reach READY status.

Returns the runtime ARN and OAuth credentials so the gateway step
can create the MCP target with proper OAUTH credential provider.

Requirements: MCP Server as Gateway Target pattern
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os
import re
import time

import boto3

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment import generate_mcp_server_code
from app.services.deployment_state_store import DeploymentStateStore
from app.services.runtime_deployer import (
    create_agent_runtime,
    create_runtime_iam_role,
    sanitize_runtime_name,
    upload_code_to_s3,
    wait_for_default_endpoint_ready,
    wait_for_runtime_ready,
)

logger = logging.getLogger(__name__)


def _prewarm_mcp_runtime(region: str, runtime_arn: str, attempts: int = 4) -> bool:
    """Force the MCP server runtime container to fully initialize (Bug 171).

    The Gateway's MCP-target tool-discovery probe has a hard ~30s init ceiling on
    the AgentCore side; a cold MCP container (loading the strands-mcp bundle)
    exceeds it on first contact, so the target lands FAILED ("Runtime
    initialization time exceeded ... 30s") and the gateway serves 0 tools. We
    cannot change that probe limit, so we send a real MCP request HERE first —
    once the container is warm, the gateway's later probe completes well under
    30s. We POST a JSON-RPC ``initialize`` (the MCP handshake) to the runtime's
    data-plane endpoint via boto3 invoke_agent_runtime; a slow first call is
    EXPECTED (that's the cold start we're paying down), so we use a long read
    timeout and retry a few times until one returns promptly. Returns True once a
    call succeeds. Best-effort — never raises to the caller.
    """
    import json as _json

    from botocore.config import Config as _Cfg

    data = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=_Cfg(read_timeout=120, connect_timeout=10, retries={"max_attempts": 0}),
    )
    handshake = _json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agentcore-flows-prewarm", "version": "1.0"},
            },
        }
    )
    for i in range(attempts):
        try:
            data.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                payload=handshake,
                contentType="application/json",
                accept="application/json, text/event-stream",
            )
            logger.info("MCP runtime pre-warm succeeded on attempt %d", i + 1)
            return True
        except Exception as e:  # noqa: BLE001
            logger.info(
                "MCP runtime pre-warm attempt %d/%d still warming: %s",
                i + 1, attempts, str(e)[:160],
            )
            time.sleep(5)
    return False


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
            DeploymentStepName.MCP_SERVER,
            DeploymentStatusEnum.IN_PROGRESS,
        )

        mcp_server_config = event.get("mcp_server_config", {})
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        bucket = _get_env("ARTIFACTS_BUCKET_NAME", "")

        mcp_name = mcp_server_config.get("name", "mcp-server")
        mcp_tools = mcp_server_config.get("tools", [])
        mcp_system_prompt = mcp_server_config.get("systemPrompt", "")

        logger.info("Deploying MCP Server Runtime: %s (tools=%s)", mcp_name, mcp_tools)

        # 1. Generate MCP server code
        mcp_code = generate_mcp_server_code(
            server_name=mcp_name,
            tools=mcp_tools if mcp_tools else None,
            system_prompt=mcp_system_prompt,
        )
        logger.info("Generated MCP server code (%d bytes)", len(mcp_code))

        # 2. Download deps bundle and upload code zip to S3.
        # Stable prefix keyed on runtime name (Bug 61) — see codegen_step
        # for rationale (AgentCore IAM cache is keyed on (role, S3 prefix)).
        mcp_s3_key = f"deployments/by-name/{sanitize_runtime_name(mcp_name)}/mcp-server-code.zip"
        if bucket:
            s3_client = boto3.client("s3", region_name=region)

            # Bug 171: prefer the LEAN mcp-only bundle (mcp + bedrock-agentcore +
            # boto3, NO strands/otel). The generated MCP server only imports
            # FastMCP, and the heavy strands-mcp.zip made the container cold-start
            # blow past the Gateway's 30s tool-discovery probe (target FAILED, 0
            # tools). Fall back to strands-mcp.zip if the lean bundle isn't present
            # (older deploys).
            deps_bundle = None
            for _key in ("agentcore-deps/mcp-lean.zip", "agentcore-deps/strands-mcp.zip"):
                try:
                    resp = s3_client.get_object(Bucket=bucket, Key=_key)
                    deps_bundle = resp["Body"].read()
                    logger.info("Downloaded MCP deps bundle %s (%d bytes)", _key, len(deps_bundle))
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("MCP deps bundle %s unavailable: %s", _key, str(e)[:120])

            upload_code_to_s3(
                s3_client,
                bucket,
                mcp_s3_key,
                mcp_code,
                "",
                "agent.py",
                deps_bundle=deps_bundle,
            )
            logger.info("Uploaded MCP server code to s3://%s/%s", bucket, mcp_s3_key)
        else:
            raise RuntimeError("No ARTIFACTS_BUCKET_NAME set")

        # 3. Create IAM role for MCP server runtime
        sts = boto3.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        iam_client = boto3.client("iam")
        # Role name must start with "AgentCore" so the step Lambda's
        # iam:CreateRole resource scope (arn:aws:iam::*:role/AgentCore*)
        # in platform_stack.py matches. See tasks/lessons.md Bug 71.
        mcp_role_name = f"AgentCoreMCP-{sanitize_runtime_name(mcp_name)}"
        mcp_role_arn = create_runtime_iam_role(
            iam_client,
            mcp_role_name,
            account_id,
            region,
            [],  # MCP server doesn't need extra tool permissions
        )
        logger.info("Created MCP server IAM role: %s", mcp_role_arn)
        # Manifest: record the MCP exec role for generic teardown.
        store.record_resource(
            deployment_id,
            {"type": "iam_role", "name": mcp_role_name, "region": region},
        )

        # 4. Create Cognito pool for gateway-to-MCP-server OAuth auth
        gateway_name = event.get("gateway_config", {}).get("name", "mcp-gw")
        cognito = boto3.client("cognito-idp", region_name=region)

        pool_name = f"AgentCore-mcp-{gateway_name}"
        resource_id = f"agentcore-mcp-{gateway_name}"
        scope_name = "invoke"

        pool_resp = cognito.create_user_pool(
            PoolName=pool_name,
            AutoVerifiedAttributes=[],
            UsernameAttributes=["email"],
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
        )
        pool_id = pool_resp["UserPool"]["Id"]
        logger.info("Created MCP Cognito pool: %s", pool_id)
        # Manifest: record the MCP Cognito user pool for generic teardown.
        store.record_resource(
            deployment_id,
            {"type": "cognito_user_pool", "id": pool_id, "region": region},
        )

        try:
            cognito.create_resource_server(
                UserPoolId=pool_id,
                Identifier=resource_id,
                Name=f"AgentCore MCP {gateway_name}",
                Scopes=[{"ScopeName": scope_name, "ScopeDescription": "Invoke MCP server"}],
            )
        except Exception as e:
            logger.warning("MCP resource server creation: %s", e)

        domain = f"ac-mcp-{gateway_name}-{pool_id.split('_')[-1][:8]}".lower()
        domain = re.sub(r"[^a-z0-9-]", "-", domain)[:63]
        try:
            cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
        except Exception as e:
            logger.warning("MCP domain creation: %s", e)

        full_scope = f"{resource_id}/{scope_name}"
        client_resp = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"mcp-{gateway_name}-client",
            GenerateSecret=True,
            AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[full_scope],
            SupportedIdentityProviders=["COGNITO"],
        )
        mcp_client_id = client_resp["UserPoolClient"]["ClientId"]
        mcp_client_secret = client_resp["UserPoolClient"]["ClientSecret"]
        discovery_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
        logger.info("Created MCP OAuth client: %s, scope: %s", mcp_client_id, full_scope)

        # 5. Create MCP server runtime (protocol=MCP) with JWT authorizer
        agentcore_ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
        authorizer_config = {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedClients": [mcp_client_id],
            }
        }
        mcp_runtime_result = create_agent_runtime(
            agentcore_ctrl=agentcore_ctrl,
            runtime_name=sanitize_runtime_name(mcp_name),
            role_arn=mcp_role_arn,
            s3_bucket=bucket,
            s3_key=mcp_s3_key,
            entrypoint="agent.py",
            python_runtime="PYTHON_3_13",
            protocol="MCP",
            env_vars={"AWS_REGION": region},
            authorizer_config=authorizer_config,
        )
        mcp_runtime_id = mcp_runtime_result["runtime_id"]
        logger.info("Created MCP server runtime: %s", mcp_runtime_id)
        # Manifest: record the MCP server runtime for generic teardown right
        # after create (the readiness wait below can be killed mid-poll).
        store.record_resource(
            deployment_id,
            {"type": "agent_runtime", "id": mcp_runtime_id, "region": region},
        )

        # 6. Wait for MCP server runtime to be ready
        mcp_launch = wait_for_runtime_ready(agentcore_ctrl, mcp_runtime_id, timeout=300)
        if not mcp_launch.get("success"):
            raise RuntimeError(f"MCP Server Runtime failed to become ready: {mcp_launch.get('error', 'unknown')}")
        logger.info("MCP Server Runtime is READY: %s", mcp_runtime_id)

        # 6a. Gate on the DEFAULT endpoint too (Bug 166) — control-plane READY
        # does not mean the data-plane endpoint is invokable yet.
        ep = wait_for_default_endpoint_ready(agentcore_ctrl, mcp_runtime_id, timeout=180)
        if not ep.get("success"):
            logger.warning("MCP DEFAULT endpoint not READY: %s", ep.get("error"))

        # 6b. PRE-WARM the MCP runtime (Bug 171). The Gateway's MCP target
        # discovery probe ("fetch tools") has a HARD 30s init ceiling on the
        # AgentCore side; a COLD MCP container (loading the strands-mcp bundle)
        # blows past it on first contact, so the target lands FAILED with
        # "Runtime initialization time exceeded ... 30s" and the gateway serves 0
        # tools. We can't change the 30s probe limit, so we warm the container
        # FIRST by sending a real MCP request, so the gateway's probe hits an
        # already-initialized runtime. Best-effort: a warmup error is logged but
        # never fails the deploy (the gateway-target retry still applies).
        mcp_server_runtime_arn = mcp_runtime_result.get("arn", "")
        try:
            _prewarm_mcp_runtime(region, mcp_server_runtime_arn)
        except Exception as warm_exc:  # noqa: BLE001
            logger.warning("MCP runtime pre-warm skipped: %s", str(warm_exc)[:200])

        # 7. Return runtime ARN + OAuth credentials for gateway step
        logger.info("MCP Server Runtime ARN: %s", mcp_server_runtime_arn)

        return {
            **event,
            "mcp_server_runtime_arn": mcp_server_runtime_arn,
            "mcp_server_runtime_id": mcp_runtime_id,
            "mcp_oauth": {
                "discovery_url": discovery_url,
                "client_id": mcp_client_id,
                "client_secret": mcp_client_secret,
                "scope": full_scope,
                "pool_id": pool_id,
            },
        }

    except Exception:
        logger.exception("MCP Server step failed for deployment %s", deployment_id)
        raise
