"""Step handler: Deploy MCP Gateway via boto3.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import logging
import os

import app.services._otel_platform  # noqa: F401
from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import _SHARED_TOOL_LAMBDAS, _put_connector_secret, deploy_gateway
from app.services.litellm_gateway_deployer import deploy_litellm_gateway, resolve_gateway_provider

logger = logging.getLogger(__name__)


def _get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_deployment_store() -> DeploymentStateStore:
    return DeploymentStateStore(
        table_name=_get_env("DEPLOYMENT_TABLE_NAME", "DeploymentState"),
        region=_get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1")),
    )


def _record_gateway_resources(
    store: DeploymentStateStore, deployment_id: str, region: str, gateway_result: dict
) -> None:
    """Append every AWS sub-resource ``deploy_gateway`` created to the manifest.

    All best-effort (record_resource swallows its own errors). Recorded TYPE
    strings match the _delete_managed_resource dispatcher exactly:
    gateway / cognito_user_pool / lambda / iam_role / secret /
    api_key_credential_provider / oauth2_credential_provider /
    litellm_gateway (informational; deletes nothing).
    """

    def _rec(resource: dict) -> None:
        resource["region"] = region
        store.record_resource(deployment_id, resource)

    gw_id = gateway_result.get("gateway_id")
    if gw_id:
        _rec({"type": "gateway", "id": gw_id})

    gw_name = gateway_result.get("gateway_name")

    # A LiteLLM gateway is the CUSTOMER's own proxy, so record one informational
    # row naming what the deploy pointed at. It deletes nothing — see the
    # "litellm_gateway" arm in _delete_managed_resource. Crucially this REPLACES
    # the AgentCoreGateway-<name> role row: that role only exists on the AgentCore
    # path, and recording it here would make every LiteLLM teardown issue a delete
    # for an IAM role that was never created. The rows further down are already
    # no-ops for LiteLLM (no pool_id, no tool Lambdas), except the virtual-key
    # secret, which is a normal "secret" row we do still want — hence no early
    # return.
    if gateway_result.get("gateway_provider") == "litellm":
        base_url = gateway_result.get("litellm_base_url")
        if base_url:
            entry = {"type": "litellm_gateway", "id": base_url}
            if gw_name:
                entry["name"] = gw_name
            _rec(entry)
    elif gw_name:
        # The gateway's own execution role (AgentCoreGateway-<gateway_name>).
        _rec({"type": "iam_role", "name": f"AgentCoreGateway-{gw_name}"})

    # The Cognito pool fronting the gateway's CUSTOM_JWT auth.
    client_info = gateway_result.get("client_info") or {}
    pool_id = client_info.get("user_pool_id")
    if pool_id:
        _rec({"type": "cognito_user_pool", "id": pool_id})

    # Tool Lambdas + their exec roles (built-in dynamic-tools / customer-support,
    # KB query tool, and per-custom-tool lambdas/roles). SHARED singleton tool
    # Lambdas (AgentCoreDynamicTools / AgentCoreCustomerSupportTools) are reused
    # by every gateway, so their manifest entry also records WHICH gateway role
    # owns this deployment's invoke grant — the teardown dispatchers use it to
    # release the Lambda by reference count instead of hard-deleting it out from
    # under other live gateways (Defect C, manifest path).
    for fn in [gateway_result.get("lambda_function_name"), gateway_result.get("kb_lambda_name")]:
        if fn:
            entry = {"type": "lambda", "name": fn}
            if fn in _SHARED_TOOL_LAMBDAS and gw_name:
                entry["gateway_role"] = f"AgentCoreGateway-{gw_name}"
            _rec(entry)
    for fn in gateway_result.get("custom_tool_lambdas") or []:
        if fn:
            _rec({"type": "lambda", "name": fn})
    for role_name in gateway_result.get("custom_tool_roles") or []:
        if role_name:
            _rec({"type": "iam_role", "name": role_name})

    # Per-connector Secrets Manager secrets (hold the raw credential).
    for secret_arn in gateway_result.get("connector_secret_arns") or []:
        if secret_arn:
            _rec({"type": "secret", "id": secret_arn})

    # Per-connector credential providers. deploy_gateway records each as
    # "TYPE:name" (TYPE in {OAUTH, API_KEY}) so we route to the correct deleter.
    for entry in gateway_result.get("connector_credential_providers") or []:
        if not entry:
            continue
        kind, _, prov_name = str(entry).partition(":")
        if not prov_name:
            # Legacy bare name (no type prefix). The recorded type is a hint only:
            # teardown purges BOTH namespaces for either row type, because the two
            # deleters silently no-op on each other's providers.
            kind, prov_name = "OAUTH", str(entry)
        res_type = "api_key_credential_provider" if kind.upper() == "API_KEY" else "oauth2_credential_provider"
        _rec({"type": res_type, "name": prov_name})

    # Staged OpenAPI spec objects (large connector specs routed to S3, not inline).
    for uri in gateway_result.get("connector_spec_s3_uris") or []:
        if uri:
            _rec({"type": "s3_object", "id": uri})


def handler(event: dict, context) -> dict:
    deployment_id = event.get("deployment_id", "")

    try:
        store = _get_deployment_store()
        store.update_step(deployment_id, DeploymentStepName.GATEWAY, DeploymentStatusEnum.IN_PROGRESS)

        gateway_config = event.get("gateway_config") or {}
        region = _get_env("APP_AWS_REGION", _get_env("AWS_REGION", "us-east-1"))
        template_id = event.get("template_id")
        gateway_tools = event.get("gateway_tools") or []
        identity_config = event.get("identity_config") or {}
        custom_tools = event.get("custom_tools") or []
        connectors = event.get("connectors") or []
        external_mcp_servers = event.get("external_mcp_servers") or []
        owner_sub = event.get("owner_sub") or ""

        # SaaS connectors carrying a raw secret_value: mint a Secrets Manager
        # secret NOW (SFN path) so the raw value is dropped as early as
        # possible, and hand deploy_gateway only the resulting ARN. Secrets
        # never go to logs, the canvas, or the deployment record. See the
        # secret-hygiene HARD RULE. The payload key MUST match the jsonKey the
        # credential provider reads (apiKey for api_key, clientSecret for
        # oauth2_cc) — same shape deploy_gateway uses on the direct path.
        for connector in connectors:
            raw = connector.get("secret_value")
            if raw and not connector.get("secret_arn"):
                payload_key = (
                    "clientSecret"
                    if (connector.get("auth_method") or connector.get("authMethod")) == "oauth2_cc"
                    else "apiKey"
                )
                connector["secret_arn"] = _put_connector_secret(region, owner_sub, {payload_key: raw})
            # ALWAYS drop the raw value once we've passed the mint point — even in
            # the edge case where BOTH secret_arn and secret_value arrived on the
            # input — so the plaintext never survives the step into the re-emitted
            # SFN event ({**event} below) or down into deploy_gateway.
            connector.pop("secret_value", None)
            connector.pop("secretValue", None)

        # External MCP catalog selections carrying a raw API key / OAuth client
        # secret: mint the Secrets Manager secret NOW (same hygiene as connectors)
        # so the raw value is dropped before the SFN event is re-emitted. Tier-2
        # api_key → {apiKey}; Tier-3 oauth client secret → handled by the deployer
        # (it needs client_id + discovery too), so only the api_key raw is pre-minted.
        for _mcp in external_mcp_servers:
            _raw = _mcp.get("secret_value") or _mcp.get("secretValue")
            if _raw and not (_mcp.get("secret_arn") or _mcp.get("secretArn")):
                _mcp["secret_arn"] = _put_connector_secret(region, owner_sub, {"apiKey": _raw})
            _mcp.pop("secret_value", None)
            _mcp.pop("secretValue", None)

        mcp_server_runtime_arn = event.get("mcp_server_runtime_arn")
        mcp_oauth = event.get("mcp_oauth")

        knowledge_base_result = event.get("knowledge_base_result") or {}

        # Provider dispatch (Workstream A). AgentCore is the default and its call
        # below is untouched. A LiteLLM gateway is a customer-run proxy: nothing
        # is created in AWS beyond the virtual-key secret, and the return contract
        # is identical, which is why the state machine needs no new branch.
        gateway_provider = resolve_gateway_provider(gateway_config)
        if gateway_provider == "litellm":
            # Same secret hygiene as connectors: mint the key into Secrets Manager
            # and DROP the raw value before {**event} re-emits the payload to SFN.
            # The deployer needs the raw key for its readiness probe, so hand it
            # over explicitly rather than leaving it on the shared config dict.
            # Pop BOTH spellings unconditionally — `a.pop() or b.pop()` would skip
            # the second when the first hit, leaving plaintext on the dict that
            # {**event} re-emits. Same edge case the connector block calls out.
            _snake = gateway_config.pop("litellm_api_key", None)
            _camel = gateway_config.pop("litellmApiKey", None)
            raw_key = _snake or _camel
            litellm_result = deploy_litellm_gateway(
                gateway_config={**gateway_config, "litellm_api_key": raw_key},
                region=region,
                owner_sub=owner_sub,
                deployment_id=deployment_id if deployment_id else None,
            )
            del raw_key
            if not litellm_result.get("success"):
                raise RuntimeError(f"Gateway deployment failed: {litellm_result.get('error', 'unknown error')}")

            # Persist only the ARN back onto the config so a redeploy reuses it.
            key_ref = (litellm_result.get("client_info") or {}).get("api_key_ref")
            if key_ref:
                gateway_config["litellm_api_key_ref"] = key_ref
                # Key MUST be "id", not "arn": _delete_managed_resource derives its
                # target as ``res.get("id") or res.get("name")`` and would otherwise
                # call delete_secret(SecretId="").
                store.record_resource(deployment_id, {"type": "secret", "id": key_ref, "region": region})

            return {
                **event,
                "gateway_config": gateway_config,
                "gateway_result": litellm_result,
            }

        gateway_result = deploy_gateway(
            gateway_config=gateway_config,
            region=region,
            template_id=template_id,
            gateway_tools=gateway_tools,
            identity_config=identity_config,
            custom_tools=custom_tools,
            connectors=connectors,
            external_mcp_servers=external_mcp_servers,
            owner_sub=owner_sub,
            mcp_server_runtime_arn=mcp_server_runtime_arn,
            mcp_oauth=mcp_oauth,
            knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
            deployment_id=deployment_id if deployment_id else None,
        )

        if not gateway_result.get("success"):
            # Record the partial inventory BEFORE raising. deploy_gateway tries its
            # own abort cleanup, but that is best-effort and returns per-resource
            # failures it used to discard; when it leaves something behind there is
            # no runtime, so nothing else ever names those resources and they are
            # orphaned permanently. Verified live: a deploy that failed at
            # CreateApiKeyCredentialProvider left an orphan gateway + Cognito pool
            # with created_resources still null. With rows written, the normal
            # manifest-driven teardown cleans them up on a later delete (which
            # accepts a deployment_id precisely for this partial-failure case).
            _record_gateway_resources(store, deployment_id, region, gateway_result)
            raise RuntimeError(f"Gateway deployment failed: {gateway_result.get('error', 'unknown error')}")

        # Manifest: record every AWS sub-resource deploy_gateway created so the
        # generic teardown path can destroy them even if a later step fails
        # before *_result lands. Best-effort: record_resource never raises into
        # the deploy. Types MUST match _delete_managed_resource's dispatcher.
        _record_gateway_resources(store, deployment_id, region, gateway_result)

        # Persist connector cleanup handles (provider NAMES + secret ARNs) into
        # the gateway_result that gets written to the deployment record so
        # cleanup.sh can tear down credential providers and secrets later.
        gateway_result["connector_credential_providers"] = gateway_result.get("connector_credential_providers", [])
        gateway_result["connector_secret_arns"] = gateway_result.get("connector_secret_arns", [])
        gateway_result["connector_spec_s3_uris"] = gateway_result.get("connector_spec_s3_uris", [])

        return {
            **event,
            "gateway_result": gateway_result,
        }

    except Exception:
        logger.exception("Gateway step failed for deployment %s", deployment_id)
        raise
