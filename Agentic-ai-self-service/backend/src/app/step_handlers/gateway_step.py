"""Step handler: Deploy MCP Gateway via boto3.

Requirements: 3.4
"""

# Platform OTEL bootstrap — MUST be first import. See lambda_handler.py.
import app.services._otel_platform  # noqa: F401

import logging
import os

from app.models.deployment_models import DeploymentStatusEnum, DeploymentStepName
from app.services.deployment_state_store import DeploymentStateStore
from app.services.gateway_deployer import _put_connector_secret, deploy_gateway

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
    api_key_credential_provider / oauth2_credential_provider.
    """
    def _rec(resource: dict) -> None:
        resource["region"] = region
        store.record_resource(deployment_id, resource)

    gw_id = gateway_result.get("gateway_id")
    if gw_id:
        _rec({"type": "gateway", "id": gw_id})

    # The gateway's own execution role (AgentCoreGateway-<gateway_name>).
    gw_name = gateway_result.get("gateway_name")
    if gw_name:
        _rec({"type": "iam_role", "name": f"AgentCoreGateway-{gw_name}"})

    # The Cognito pool fronting the gateway's CUSTOM_JWT auth.
    client_info = gateway_result.get("client_info") or {}
    pool_id = client_info.get("user_pool_id")
    if pool_id:
        _rec({"type": "cognito_user_pool", "id": pool_id})

    # Tool Lambdas + their exec roles (built-in dynamic-tools / customer-support,
    # KB query tool, and per-custom-tool lambdas/roles).
    for fn in [gateway_result.get("lambda_function_name"), gateway_result.get("kb_lambda_name")]:
        if fn:
            _rec({"type": "lambda", "name": fn})
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
            # Legacy bare name (no type prefix) — default to oauth2 provider.
            kind, prov_name = "OAUTH", str(entry)
        res_type = (
            "api_key_credential_provider"
            if kind.upper() == "API_KEY"
            else "oauth2_credential_provider"
        )
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
                connector["secret_arn"] = _put_connector_secret(
                    region, owner_sub, {payload_key: raw}
                )
            # ALWAYS drop the raw value once we've passed the mint point — even in
            # the edge case where BOTH secret_arn and secret_value arrived on the
            # input — so the plaintext never survives the step into the re-emitted
            # SFN event ({**event} below) or down into deploy_gateway.
            connector.pop("secret_value", None)
            connector.pop("secretValue", None)

        mcp_server_runtime_arn = event.get("mcp_server_runtime_arn")
        mcp_oauth = event.get("mcp_oauth")

        knowledge_base_result = event.get("knowledge_base_result") or {}

        gateway_result = deploy_gateway(
            gateway_config=gateway_config,
            region=region,
            template_id=template_id,
            gateway_tools=gateway_tools,
            identity_config=identity_config,
            custom_tools=custom_tools,
            connectors=connectors,
            owner_sub=owner_sub,
            mcp_server_runtime_arn=mcp_server_runtime_arn,
            mcp_oauth=mcp_oauth,
            knowledge_base_result=knowledge_base_result if knowledge_base_result else None,
            deployment_id=deployment_id if deployment_id else None,
        )

        if not gateway_result.get("success"):
            raise RuntimeError(f"Gateway deployment failed: {gateway_result.get('error', 'unknown error')}")

        # Manifest: record every AWS sub-resource deploy_gateway created so the
        # generic teardown path can destroy them even if a later step fails
        # before *_result lands. Best-effort: record_resource never raises into
        # the deploy. Types MUST match _delete_managed_resource's dispatcher.
        _record_gateway_resources(store, deployment_id, region, gateway_result)

        # Persist connector cleanup handles (provider NAMES + secret ARNs) into
        # the gateway_result that gets written to the deployment record so
        # cleanup.sh can tear down credential providers and secrets later.
        gateway_result["connector_credential_providers"] = gateway_result.get(
            "connector_credential_providers", []
        )
        gateway_result["connector_secret_arns"] = gateway_result.get(
            "connector_secret_arns", []
        )
        gateway_result["connector_spec_s3_uris"] = gateway_result.get(
            "connector_spec_s3_uris", []
        )

        return {
            **event,
            "gateway_result": gateway_result,
        }

    except Exception:
        logger.exception("Gateway step failed for deployment %s", deployment_id)
        raise
