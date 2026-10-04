"""A non-home deployment must not pretend the shared Cognito pool moved regions.

The platform's warm gateway-auth pool belongs to the home-region CDK stack. A
same-account deployment may create its AgentCore Gateway in another region, but
resource-server/client operations, discovery URLs, and teardown for the shared
pool must still target the region encoded in the pool id. The deployment-bound
copy of the client secret is different: it belongs beside the target runtime.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.services import gateway_deployer, harness_deployer
from app.step_handlers import gateway_step

HOME_REGION = "us-east-1"
TARGET_REGION = "eu-west-1"
SHARED_POOL = "us-east-1_SHAREDPOOL"
SHARED_DOMAIN = "acf-test-gw-0123456789"
TARGET_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:agentcore-connector/deployment/client-AbCdEf"


class _Cognito:
    def __init__(self):
        self.created_resource_servers: list[dict] = []
        self.created_clients: list[dict] = []
        self.deleted_clients: list[dict] = []
        self.deleted_resource_servers: list[dict] = []

    def create_resource_server(self, **kwargs):
        self.created_resource_servers.append(kwargs)
        return {}

    def create_user_pool_client(self, **kwargs):
        self.created_clients.append(kwargs)
        return {
            "UserPoolClient": {
                "ClientId": "regional-client",
                "ClientSecret": "regional-secret",
            }
        }

    def delete_user_pool_client(self, **kwargs):
        self.deleted_clients.append(kwargs)
        return {}

    def delete_resource_server(self, **kwargs):
        self.deleted_resource_servers.append(kwargs)
        return {}

    def list_user_pool_clients(self, **kwargs):
        gone = {c["ClientId"] for c in self.deleted_clients}
        return {
            "UserPoolClients": [
                {"ClientId": "regional-client", "ClientName": c["ClientName"]}
                for c in self.created_clients
                if "regional-client" not in gone
            ]
        }


class _Secrets:
    def __init__(self, arn: str = TARGET_SECRET_ARN):
        self.arn = arn
        self.created: list[dict] = []

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        return {"ARN": self.arn}

    def get_secret_value(self, **_kwargs):
        return {"SecretString": json.dumps({"clientSecret": "resolved-secret"})}


def _shared_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", SHARED_POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", SHARED_DOMAIN)


def test_non_home_gateway_mutates_the_shared_pool_in_its_home_region(monkeypatch):
    _shared_env(monkeypatch)
    target_cognito = _Cognito()
    home_cognito = _Cognito()
    cognito_regions: list[str] = []
    secret_regions: list[str] = []
    secrets = _Secrets()

    def _cognito(region: str):
        cognito_regions.append(region)
        return home_cognito

    def _secrets(region: str):
        secret_regions.append(region)
        return secrets

    monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", _cognito)
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", _secrets)

    result = gateway_deployer._create_cognito_oauth(
        target_cognito,
        "orders-gateway",
        TARGET_REGION,
        owner_sub="owner",
        deployment_id="dep-orders",
    )

    assert cognito_regions == [HOME_REGION]
    assert target_cognito.created_resource_servers == []
    assert target_cognito.created_clients == []
    assert home_cognito.created_resource_servers[0]["UserPoolId"] == SHARED_POOL
    assert home_cognito.created_clients[0]["UserPoolId"] == SHARED_POOL
    assert secret_regions == [TARGET_REGION]
    assert result["client_info"]["client_secret_ref"] == TARGET_SECRET_ARN
    assert result["client_info"]["user_pool_region"] == HOME_REGION
    assert result["client_info"]["token_endpoint"] == (
        f"https://{SHARED_DOMAIN}.auth.{HOME_REGION}.amazoncognito.com/oauth2/token"
    )
    assert result["authorizer_config"]["customJWTAuthorizer"]["discoveryUrl"] == (
        f"https://cognito-idp.{HOME_REGION}.amazonaws.com/{SHARED_POOL}/.well-known/openid-configuration"
    )


def test_secret_resolution_uses_the_secret_arn_region_not_the_pool_region(monkeypatch):
    requested_regions: list[str] = []
    secrets = _Secrets()

    def _secrets(region: str):
        requested_regions.append(region)
        return secrets

    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", _secrets)

    value = gateway_deployer.resolve_client_secret(
        {
            "user_pool_id": SHARED_POOL,
            "client_id": "regional-client",
            "client_secret_ref": TARGET_SECRET_ARN,
        }
    )

    assert value == "resolved-secret"
    assert requested_regions == [TARGET_REGION]


def test_runtime_jwt_configuration_uses_the_pool_region(monkeypatch):
    agentcore = MagicMock()
    agentcore.get_agent_runtime.return_value = {
        "status": "READY",
        "agentRuntimeArtifact": {"codeConfiguration": {}},
        "roleArn": "arn:aws:iam::123456789012:role/runtime",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
    }
    monkeypatch.setattr(
        gateway_deployer,
        "_create_agentcore_control_client",
        lambda _region: agentcore,
    )
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda _seconds: None)

    result = gateway_deployer.configure_jwt_auth(
        "runtime-1",
        {
            "client_info": {
                "provider": "cognito",
                "user_pool_id": SHARED_POOL,
                "client_id": "regional-client",
            }
        },
        TARGET_REGION,
    )

    assert result["success"] is True
    authorizer = agentcore.update_agent_runtime.call_args.kwargs["authorizerConfiguration"]["customJWTAuthorizer"]
    assert authorizer["discoveryUrl"] == (
        f"https://cognito-idp.{HOME_REGION}.amazonaws.com/{SHARED_POOL}/.well-known/openid-configuration"
    )


def test_manifest_routes_shared_pool_children_home_and_secret_to_target():
    rows = gateway_step._gateway_manifest_resources(
        TARGET_REGION,
        {
            "client_info": {
                "user_pool_id": SHARED_POOL,
                "user_pool_region": HOME_REGION,
                "client_id": "regional-client",
                "scope": "agentcore-orders/invoke",
                "shared_pool": True,
                "minted_client_secret_ref": TARGET_SECRET_ARN,
            }
        },
    )

    app_client = next(row for row in rows if row["type"] == "cognito_app_client")
    resource_server = next(row for row in rows if row["type"] == "cognito_resource_server")
    secret = next(row for row in rows if row["type"] == "secret")

    assert app_client["region"] == HOME_REGION
    assert resource_server["region"] == HOME_REGION
    assert secret["region"] == TARGET_REGION


def test_direct_cleanup_deletes_shared_pool_children_in_the_pool_region(monkeypatch):
    _shared_env(monkeypatch)
    cognito = _Cognito()
    requested_regions: list[str] = []

    def _cognito(region: str):
        requested_regions.append(region)
        return cognito

    monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", _cognito)
    monkeypatch.setattr(
        gateway_deployer,
        "_create_lambda_client",
        lambda _region: MagicMock(),
    )

    log = gateway_deployer.cleanup_gateway_resources(
        "",
        TARGET_REGION,
        {
            "client_info": {
                "user_pool_id": SHARED_POOL,
                "user_pool_region": HOME_REGION,
                "client_id": "regional-client",
                "scope": "agentcore-orders/invoke",
                "shared_pool": True,
            }
        },
    )

    assert requested_regions == [HOME_REGION]
    assert cognito.deleted_clients == [{"UserPoolId": SHARED_POOL, "ClientId": "regional-client"}]
    assert cognito.deleted_resource_servers == [{"UserPoolId": SHARED_POOL, "Identifier": "agentcore-orders"}]
    assert "Shared-pool gateway app client deleted" in log


def test_harness_provider_uses_the_pool_region_even_when_harness_is_elsewhere():
    agentcore = MagicMock()
    agentcore.create_oauth2_credential_provider.return_value = {
        "credentialProviderArn": (
            "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
            "token-vault/default/oauth2credentialprovider/harness-orders"
        )
    }
    secrets = _Secrets()

    harness_deployer.ensure_gateway_outbound_provider(
        agentcore,
        "orders",
        {
            "user_pool_id": SHARED_POOL,
            "client_id": "regional-client",
            "client_secret_ref": TARGET_SECRET_ARN,
            "scope": "agentcore-orders/invoke",
        },
        secrets_client=secrets,
        region=TARGET_REGION,
    )

    provider_input = agentcore.create_oauth2_credential_provider.call_args.kwargs["oauth2ProviderConfigInput"][
        "customOauth2ProviderConfig"
    ]
    assert provider_input["oauthDiscovery"]["discoveryUrl"] == (
        f"https://cognito-idp.{HOME_REGION}.amazonaws.com/{SHARED_POOL}/.well-known/openid-configuration"
    )
