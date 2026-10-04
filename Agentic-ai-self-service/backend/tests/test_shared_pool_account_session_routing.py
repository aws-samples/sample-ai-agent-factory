"""Shared-pool Cognito calls must use platform credentials, not target credentials.

The gateway itself, its tool Lambdas, and deployment-bound secrets belong in the
selected deployment account.  The warm shared Cognito pool is different: CDK
created it in the platform account, and only that account can create or remove
its per-gateway resource server and app client.

These tests deliberately model two account-distinct sessions and exercise the
real client-routing seams.  In particular, they do not replace
``gateway_deployer._create_cognito_client``: mocking that helper is what allowed
the existing region-routing tests to pass while the account half stayed broken.
"""

from __future__ import annotations

from dataclasses import dataclass

import app.deployment_handler as deployment_handler
import pytest
from app.services import deploy_target, gateway_deployer
from app.services.resource_ownership import owner_tags
from app.step_handlers import status_update_step

PLATFORM_ACCOUNT = "111111111111"
TARGET_ACCOUNT = "222222222222"
HOME_REGION = "us-east-1"
TARGET_REGION = "eu-west-1"
SHARED_POOL = "us-east-1_PLATFORM"
SHARED_DOMAIN = "agent-factory-shared-auth"
APP_CLIENT = "gateway-app-client"
RESOURCE_SERVER = "agentcore-orders"


class _Cognito:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.created_resource_servers: list[dict] = []
        self.created_clients: list[dict] = []
        self.deleted_clients: list[dict] = []
        self.deleted_resource_servers: list[dict] = []
        self.deleted_pools: list[dict] = []
        self.deleted_domains: list[dict] = []
        self.described_pools: list[dict] = []
        self.pool_tags: dict[str, str] = {}

    def create_resource_server(self, **kwargs):
        self.created_resource_servers.append(kwargs)
        return {}

    def create_user_pool_client(self, **kwargs):
        self.created_clients.append(kwargs)
        return {
            "UserPoolClient": {
                "ClientId": APP_CLIENT,
                "ClientSecret": "test-only-client-secret",
            }
        }

    def delete_user_pool_client(self, **kwargs):
        self.deleted_clients.append(kwargs)
        return {}

    def delete_resource_server(self, **kwargs):
        self.deleted_resource_servers.append(kwargs)
        return {}

    def list_user_pool_clients(self, **_kwargs):
        return {"UserPoolClients": []}

    def describe_user_pool(self, **kwargs):
        self.described_pools.append(kwargs)
        return {"UserPool": {"UserPoolTags": dict(self.pool_tags)}}

    def delete_user_pool(self, **kwargs):
        self.deleted_pools.append(kwargs)
        return {}

    def delete_user_pool_domain(self, **kwargs):
        self.deleted_domains.append(kwargs)
        return {}


class _Secrets:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.created: list[dict] = []

    def create_secret(self, **kwargs):
        self.created.append(kwargs)
        return {
            "ARN": (
                f"arn:aws:secretsmanager:{TARGET_REGION}:{self.account_id}:"
                "secret:agentcore-connector/deployment/client-AbCdEf"
            )
        }


class _UnusedClient:
    pass


class _IamExceptions:
    class EntityAlreadyExistsException(Exception):
        pass


class _Iam:
    exceptions = _IamExceptions()

    def create_role(self, **kwargs):
        return {"Role": {"Arn": (f"arn:aws:iam::{TARGET_ACCOUNT}:role/{kwargs['RoleName']}")}}

    def put_role_policy(self, **_kwargs):
        return {}


class _Sts:
    def get_caller_identity(self):
        return {"Account": TARGET_ACCOUNT}


class _Session:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.cognito = _Cognito(account_id)
        self.secrets = _Secrets(account_id)
        self.agentcore = _UnusedClient()
        self.iam = _Iam()
        self.sts = _Sts()
        self.requests: list[tuple[str, dict]] = []

    def client(self, service_name: str, **kwargs):
        self.requests.append((service_name, dict(kwargs)))
        if service_name == "cognito-idp":
            return self.cognito
        if service_name == "secretsmanager":
            return self.secrets
        if service_name == "bedrock-agentcore-control":
            return self.agentcore
        if service_name == "iam":
            return self.iam
        if service_name == "sts":
            return self.sts
        if service_name == "lambda":
            return _UnusedClient()
        raise AssertionError(f"Unexpected {service_name!r} client requested from account {self.account_id}")


@dataclass
class _Routes:
    platform: _Session
    target: _Session


@pytest.fixture
def routes(monkeypatch) -> _Routes:
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", SHARED_POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", SHARED_DOMAIN)
    monkeypatch.setenv("APP_AWS_REGION", HOME_REGION)
    monkeypatch.setenv("AWS_REGION", HOME_REGION)
    monkeypatch.setenv("PROJECT_NAME", "agent-factory")
    monkeypatch.setenv("ENVIRONMENT", "test")

    platform = _Session(PLATFORM_ACCOUNT)
    target = _Session(TARGET_ACCOUNT)

    # Default boto3 is the platform Lambda's execution-role session.  A target
    # event resolves through deploy_target.session_for_target instead.
    monkeypatch.setattr(
        gateway_deployer.boto3,
        "client",
        platform.client,
    )
    monkeypatch.setattr(
        gateway_deployer.boto3,
        "Session",
        lambda *_args, **_kwargs: platform,
    )
    monkeypatch.setattr(
        deploy_target,
        "session_for_target",
        lambda **_kwargs: target,
    )
    return _Routes(platform=platform, target=target)


def _shared_client_info() -> dict:
    return {
        "provider": "cognito",
        "user_pool_id": SHARED_POOL,
        "user_pool_region": HOME_REGION,
        "client_id": APP_CLIENT,
        "scope": f"{RESOURCE_SERVER}/invoke",
        "shared_pool": True,
    }


def _target_event() -> dict:
    return {
        "target_account_id": TARGET_ACCOUNT,
        "target_region": TARGET_REGION,
        "target_role_arn": (f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentFactoryDeploymentRole"),
    }


def _authorizer(client_id: str) -> dict:
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                f"https://cognito-idp.{HOME_REGION}.amazonaws.com/{SHARED_POOL}/.well-known/openid-configuration"
            ),
            "allowedClients": [client_id],
        }
    }


class _ConflictingGatewayControl:
    """A real redeploy path: create conflicts, then the existing gateway is repointed."""

    def __init__(self):
        self.updated_authorizers: list[dict] = []

    def create_gateway(self, **_kwargs):
        raise Exception("An error occurred (ConflictException): Gateway with name already exists")

    def list_gateways(self, **_kwargs):
        return {
            "items": [
                {
                    "name": "orders",
                    "gatewayId": "orders-existing",
                    "roleArn": (f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentCoreGateway-orders-{TARGET_REGION}"),
                }
            ]
        }

    def get_gateway(self, **_kwargs):
        return {
            "name": "orders",
            "gatewayId": "orders-existing",
            "gatewayUrl": "https://orders.example.invalid/mcp",
            "gatewayArn": (f"arn:aws:bedrock-agentcore:{TARGET_REGION}:{TARGET_ACCOUNT}:gateway/orders-existing"),
            "roleArn": (f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentCoreGateway-orders-{TARGET_REGION}"),
            "protocolType": "MCP",
            "status": "READY",
            # The last update, once one was sent: it lands (F-66e waits for that).
            "authorizerConfiguration": (self.updated_authorizers or [_authorizer("stale-client")])[-1],
        }

    def update_gateway(self, **kwargs):
        self.updated_authorizers.append(kwargs["authorizerConfiguration"])
        return {}

    def list_gateway_targets(self, **_kwargs):
        return {"items": []}


@pytest.mark.parametrize("deployment_region", [HOME_REGION, TARGET_REGION])
def test_cross_account_shared_pool_creation_uses_platform_cognito_and_target_secrets(
    routes: _Routes,
    deployment_region: str,
):
    """Both the same-region fast path and cross-region helper must switch accounts."""
    target_cognito = routes.target.client(
        "cognito-idp",
        region_name=deployment_region,
    )

    with gateway_deployer.gateway_aws_session(routes.target):
        result = gateway_deployer._create_cognito_oauth(
            target_cognito,
            "orders",
            deployment_region,
            owner_sub="tenant-a",
            deployment_id="dep-orders",
        )

    assert routes.platform.cognito.created_resource_servers == [
        {
            "UserPoolId": SHARED_POOL,
            "Identifier": RESOURCE_SERVER,
            "Name": "AgentCore Gateway orders",
            "Scopes": [
                {
                    "ScopeName": "invoke",
                    "ScopeDescription": "Invoke gateway",
                }
            ],
        }
    ]
    assert routes.platform.cognito.created_clients[0]["UserPoolId"] == SHARED_POOL
    assert routes.target.cognito.created_resource_servers == []
    assert routes.target.cognito.created_clients == []

    # The copied client secret belongs beside the target runtime, so this part
    # must continue to use the assumed target session.
    assert len(routes.target.secrets.created) == 1
    assert routes.platform.secrets.created == []
    assert result["client_info"]["client_id"] == APP_CLIENT
    assert result["client_info"]["user_pool_region"] == HOME_REGION


def test_cross_account_redeploy_retires_the_stale_client_in_the_platform_pool(
    routes: _Routes,
    monkeypatch,
):
    """Fixing create alone is insufficient: conflict adoption reuses that client later."""
    control = _ConflictingGatewayControl()
    routes.target.agentcore = control

    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway",
        lambda *_args, **_kwargs: {
            "gatewayUrl": "https://orders.example.invalid/mcp",
            "roleArn": (f"arn:aws:iam::{TARGET_ACCOUNT}:role/AgentCoreGateway-orders-{TARGET_REGION}"),
            "status": "READY",
        },
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_resolve_gateway_tool_actions",
        lambda *_args, **_kwargs: ([], 0),
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway_to_serve_tools",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_connector_targets",
        lambda *_args, **_kwargs: {
            "credential_provider_names": [],
            "secret_arns": [],
            "spec_s3_uris": [],
        },
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_external_mcp_targets",
        lambda *_args, **_kwargs: {
            "credential_provider_names": [],
            "secret_arns": [],
        },
    )
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_args: None)

    with gateway_deployer.gateway_aws_session(routes.target):
        result = gateway_deployer.deploy_gateway(
            {"name": "orders"},
            TARGET_REGION,
            deployment_id="dep-orders",
            owner_sub="tenant-a",
            # No live consumer: the plain redeploy, which is what retires the stale client.
            gateway_consumers=lambda _gateway_id, _pool_id: [],
        )

    assert result["success"] is True
    assert control.updated_authorizers == [_authorizer(APP_CLIENT)]
    assert routes.platform.cognito.deleted_clients == [{"UserPoolId": SHARED_POOL, "ClientId": "stale-client"}]
    assert routes.target.cognito.deleted_clients == []


def test_legacy_shared_pool_cleanup_uses_platform_cognito(routes: _Routes):
    with gateway_deployer.gateway_aws_session(routes.target):
        log = gateway_deployer.cleanup_gateway_resources(
            "",
            TARGET_REGION,
            {"client_info": _shared_client_info()},
        )

    assert routes.platform.cognito.deleted_clients == [{"UserPoolId": SHARED_POOL, "ClientId": APP_CLIENT}]
    assert routes.platform.cognito.deleted_resource_servers == [
        {"UserPoolId": SHARED_POOL, "Identifier": RESOURCE_SERVER}
    ]
    assert routes.target.cognito.deleted_clients == []
    assert routes.target.cognito.deleted_resource_servers == []
    assert "Shared-pool gateway app client deleted" in log


@pytest.mark.parametrize(
    ("resource", "platform_calls_attr", "expected_call"),
    [
        (
            {
                "type": "cognito_app_client",
                "id": APP_CLIENT,
                "pool_id": SHARED_POOL,
                "region": HOME_REGION,
                "account": TARGET_ACCOUNT,
            },
            "deleted_clients",
            {"UserPoolId": SHARED_POOL, "ClientId": APP_CLIENT},
        ),
        (
            {
                "type": "cognito_resource_server",
                "id": RESOURCE_SERVER,
                "pool_id": SHARED_POOL,
                "region": HOME_REGION,
                "account": TARGET_ACCOUNT,
            },
            "deleted_resource_servers",
            {"UserPoolId": SHARED_POOL, "Identifier": RESOURCE_SERVER},
        ),
    ],
)
def test_manifest_cleanup_routes_shared_pool_children_to_the_platform_account(
    routes: _Routes,
    resource: dict,
    platform_calls_attr: str,
    expected_call: dict,
):
    deployment_handler._delete_managed_resource(
        resource,
        TARGET_REGION,
        deployment_id="dep-orders",
        target_role_arn=_target_event()["target_role_arn"],
        target_session=routes.target,
    )

    assert getattr(routes.platform.cognito, platform_calls_attr) == [expected_call]
    assert getattr(routes.target.cognito, platform_calls_attr) == []


@pytest.mark.parametrize(
    ("resource", "platform_calls_attr", "expected_call"),
    [
        (
            {
                "type": "cognito_app_client",
                "id": APP_CLIENT,
                "pool_id": SHARED_POOL,
                "region": HOME_REGION,
            },
            "deleted_clients",
            {"UserPoolId": SHARED_POOL, "ClientId": APP_CLIENT},
        ),
        (
            {
                "type": "cognito_resource_server",
                "id": RESOURCE_SERVER,
                "pool_id": SHARED_POOL,
                "region": HOME_REGION,
            },
            "deleted_resource_servers",
            {"UserPoolId": SHARED_POOL, "Identifier": RESOURCE_SERVER},
        ),
    ],
)
def test_failed_deploy_cleanup_routes_shared_pool_children_to_the_platform_account(
    routes: _Routes,
    resource: dict,
    platform_calls_attr: str,
    expected_call: dict,
):
    status_update_step._cleanup_resource(
        resource,
        TARGET_REGION,
        _target_event(),
    )

    assert getattr(routes.platform.cognito, platform_calls_attr) == [expected_call]
    assert getattr(routes.target.cognito, platform_calls_attr) == []


def test_customer_owned_pool_cleanup_stays_in_the_target_account(routes: _Routes):
    """Account routing is resource-specific; not every Cognito call is platform-side."""
    target_pool = f"{TARGET_REGION}_CUSTOMER"
    routes.target.cognito.pool_tags = owner_tags(TARGET_REGION)

    deployment_handler._delete_managed_resource(
        {
            "type": "cognito_user_pool",
            "id": target_pool,
            "region": TARGET_REGION,
            "account": TARGET_ACCOUNT,
        },
        TARGET_REGION,
        target_role_arn=_target_event()["target_role_arn"],
        target_session=routes.target,
    )

    assert routes.target.cognito.described_pools == [
        {"UserPoolId": target_pool},
        {"UserPoolId": target_pool},
    ]
    assert routes.target.cognito.deleted_pools == [{"UserPoolId": target_pool}]
    assert routes.platform.cognito.described_pools == []
    assert routes.platform.cognito.deleted_pools == []
