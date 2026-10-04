"""A gateway reuses the platform's warm Cognito domain, and can never delete it.

MEASURED, not assumed (us-east-1, throwaway pool ``us-east-1_6Tw1v3AG7``, since torn
down): ``create_user_pool_domain`` returns at once, ``describe_user_pool_domain``
reports ``Status=ACTIVE`` after **4 seconds** and kept reporting ACTIVE on all 104
subsequent polls, and the DNS name ``<domain>.auth.us-east-1.amazoncognito.com`` first
resolved — and the ``client_credentials`` mint first returned 200 — at **t+727s**.

Twelve minutes, against a 90s deploy-time ``tools/list`` probe window inside a 300s
Lambda. So a domain created during a deploy cannot be reachable in time: the probe was
never flaky, it was impossible, and ``Status=ACTIVE`` is not a readiness oracle for
it. The "empty tool plane" cure then deleted the gateway and recreated it, minting yet
another pool and another cold domain, so every retry was strictly worse than the last
(live: deploy ``3ef480e2`` / run ``df698a37``, pools ``v8OiJanup`` -> ``QU487tO1L`` ->
``yvymML5Pm``, ``CreateGateway`` returning ConflictException on attempts 2 and 3).

The fix moves pool+domain ownership to the platform stack. That makes the pool SHARED,
which creates the risk these tests exist to pin: a single agent's teardown must never
be able to delete the pool that holds every other agent's gateway credentials.

ARCC ``cnt_PQjUx2msVXY1wU``: "If credentials are shared, please create unique
credentials per entity." Only the pool and its public hosted domain are shared — the
app client (the credential) and the resource server (the scope) stay per-gateway.
ARCC ``cnt_vtSS0S3iwKjSuk``: the client secret stays server-side and off the result.
"""

import json

import pytest
from app.services import gateway_deployer

POOL = "us-east-1_SHAREDPOOL"
DOMAIN = "acf-test-gw-0123456789"
REGION = "us-east-1"


class _FakeCognito:
    """A Cognito double that models POOL TAGS, because teardown now keys on them.

    ``owned=True`` is the default because it is what the backend actually creates:
    ``create_user_pool(UserPoolTags=owner_tags(region))`` stamps ``AgentCoreStack`` on
    every pool the gateway step mints. A double that returned no tags would model a
    pool this platform never produces, and would make the protective behaviour of
    ``classify_user_pool`` look like a regression in the teardown.

    The tags are derived by calling ``owner_tags`` rather than hardcoded, so the test
    cannot drift from the value production stamps — the mismatch that made the CDK
    grant unsatisfiable against the live pool in the first place.
    """

    def __init__(
        self,
        *,
        resource_server_raises: Exception | None = None,
        owned: bool = True,
        tags: dict[str, str] | None = None,
        describe_raises: Exception | None = None,
        existing_resource_server: bool = False,
    ):
        self.resource_server_raises = resource_server_raises
        self.existing_resource_server = existing_resource_server
        self.owned = owned
        self.tags_override = tags
        self.describe_raises = describe_raises
        self.describe_calls: list[str] = []
        self.created_pools: list[str] = []
        self.created_resource_servers: list[tuple[str, str]] = []
        self.created_clients: list[dict] = []
        self.deleted_pools: list[str] = []
        self.deleted_domains: list[str] = []
        self.deleted_clients: list[tuple[str, str]] = []
        self.deleted_resource_servers: list[tuple[str, str]] = []

    # --- creation ---
    def create_user_pool(self, **kw):
        self.created_pools.append(kw["PoolName"])
        return {"UserPool": {"Id": "us-east-1_FRESHPOOL"}}

    def create_resource_server(self, **kw):
        if self.resource_server_raises is not None:
            raise self.resource_server_raises
        self.created_resource_servers.append((kw["UserPoolId"], kw["Identifier"]))
        return {}

    def describe_resource_server(self, **kw):
        # A failed create is reused only when this read proves the scope exists.
        if not self.existing_resource_server:
            raise Exception("ResourceNotFoundException")
        return {"ResourceServer": {"Identifier": kw["Identifier"], "Scopes": [{"ScopeName": "invoke"}]}}

    def create_user_pool_domain(self, **kw):
        return {}

    def create_user_pool_client(self, **kw):
        self.created_clients.append(kw)
        return {
            "UserPoolClient": {
                "ClientId": "sharedclient0000",
                "ClientSecret": "notarealclientsecret-0000000000",
            }
        }

    # --- teardown ---
    def list_user_pool_clients(self, **kw):
        # Created minus deleted, by name: resource_server_is_unused keys on client
        # names, so a fake that always answered [] would authorize every delete.
        gone = {cid for _pool, cid in self.deleted_clients}
        return {
            "UserPoolClients": [
                {"ClientId": "sharedclient0000", "ClientName": c["ClientName"]}
                for c in self.created_clients
                if "sharedclient0000" not in gone
            ]
        }

    def describe_user_pool(self, **kw):
        # A pool with a domain still attached CANNOT be deleted, so the teardown
        # deletes the domain and then POLLS this call until the Domain is gone
        # (Bug 175). Reporting a Domain forever would make that loop run its whole
        # 12x5s window on every test — so model the state change, which is also what
        # Cognito actually does.
        self.describe_calls.append(kw.get("UserPoolId", ""))
        if self.describe_raises is not None:
            raise self.describe_raises
        from app.services.resource_ownership import owner_tags

        if self.tags_override is not None:
            pool_tags = dict(self.tags_override)
        elif self.owned:
            pool_tags = owner_tags(REGION)
        else:
            pool_tags = {}
        pool: dict = {} if self.deleted_domains else {"Domain": DOMAIN}
        pool["UserPoolTags"] = pool_tags
        return {"UserPool": pool}

    def delete_user_pool(self, **kw):
        # Cognito refuses this while a hosted domain is still attached, and a fake
        # that accepts it would let "deletes the pool but orphans the domain" pass.
        if not self.deleted_domains:
            raise Exception(
                "An error occurred (InvalidParameterException) when calling the "
                "DeleteUserPool operation: User pool cannot be deleted. It has a "
                "domain configured that should be deleted first."
            )
        self.deleted_pools.append(kw["UserPoolId"])
        return {}

    def delete_user_pool_domain(self, **kw):
        self.deleted_domains.append(kw["Domain"])
        return {}

    def delete_user_pool_client(self, **kw):
        self.deleted_clients.append((kw["UserPoolId"], kw["ClientId"]))
        return {}

    def delete_resource_server(self, **kw):
        self.deleted_resource_servers.append((kw["UserPoolId"], kw["Identifier"]))
        return {}


class _FakeSecrets:
    """Records what the gateway step writes to Secrets Manager.

    Both Cognito paths now move the app client secret OUT of Cognito and into a
    per-deployment secret (``gateway_deployer._mint_client_secret_ref``), because
    Cognito's IAM resource type is ``userpool`` with no granularity below it — so any
    grant that lets an agent read its own client secret also reads every other
    client's secret in the same pool. A double is required here rather than optional:
    without it these tests reach a real ``create_secret`` call, and the resulting
    ``NoCredentialsError`` is indistinguishable from a bug in the pool logic they are
    actually about.
    """

    def __init__(self):
        self.created: list[dict] = []

    def create_secret(self, **kw):
        self.created.append(kw)
        name = kw["Name"]
        return {"ARN": f"arn:aws:secretsmanager:{REGION}:123456789012:secret:{name}-AbCdEf"}


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    """Autouse, because EVERY ``_create_cognito_oauth`` call now mints a secret.

    Left opt-in, a test added later would silently start making a real AWS call, and
    the failure would look like an unrelated credentials problem in CI.
    """
    sm = _FakeSecrets()
    monkeypatch.setattr(gateway_deployer, "_create_secrets_client", lambda region: sm)
    return sm


class _NoPlatformCognito:
    def __getattr__(self, name):
        raise AssertionError(f"shared-pool test reached the platform Cognito client ({name}) without installing a fake")


@pytest.fixture
def shared_env(monkeypatch):
    """Configure the shared pool; call the result with a fake to make it the platform client.

    The shared pool lives in the PLATFORM account, so ``_create_cognito_oauth`` reaches
    it through ``_create_platform_cognito_client`` and never through the target client
    it is passed (F-42). A test that exercises the pool installs its fake there.
    """
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", DOMAIN)
    monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", lambda region: _NoPlatformCognito())

    def _platform(cog):
        monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", lambda region: cog)
        return cog

    return _platform


# ---------------------------------------------------------------------------
# Using the warm domain
# ---------------------------------------------------------------------------


def test_the_shared_pool_is_reused_and_no_cold_domain_is_created(shared_env):
    """The fix. No new pool, no new domain — so the token endpoint is reachable at
    once instead of 727s from now."""
    cog = shared_env(_FakeCognito())
    out = gateway_deployer._create_cognito_oauth(cog, "agent-gateway", REGION)

    assert cog.created_pools == [], "creating a pool here re-introduces the 727s cold domain"
    assert out["client_info"]["user_pool_id"] == POOL
    assert out["client_info"]["token_endpoint"] == (f"https://{DOMAIN}.auth.{REGION}.amazoncognito.com/oauth2/token"), (
        "the token endpoint must be built from the PLATFORM's warm domain prefix"
    )
    assert out["client_info"]["shared_pool"] is True


def test_without_the_env_vars_it_still_creates_its_own_pool(monkeypatch):
    """An older platform stack sets neither variable. The fallback must keep working,
    or upgrading the backend without upgrading the stack breaks every gateway."""
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)
    cog = _FakeCognito()
    out = gateway_deployer._create_cognito_oauth(cog, "agent-gateway", REGION)

    assert cog.created_pools == ["AgentCore-agent-gateway"]
    assert out["client_info"]["user_pool_id"] == "us-east-1_FRESHPOOL"
    assert not out["client_info"].get("shared_pool")


def test_a_half_configured_platform_does_not_build_a_broken_token_endpoint(monkeypatch):
    """A pool id with no domain prefix would yield
    ``https://.auth.us-east-1.amazoncognito.com/oauth2/token`` — a URL that never
    resolves and whose failure looks exactly like the cold-domain bug. Both variables
    are required, so a partial rollout falls back instead."""
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", POOL)
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)
    cog = _FakeCognito()
    out = gateway_deployer._create_cognito_oauth(cog, "agent-gateway", REGION)
    assert cog.created_pools == ["AgentCore-agent-gateway"]
    assert ".auth." in out["client_info"]["token_endpoint"]
    assert "https://.auth" not in out["client_info"]["token_endpoint"]


# ---------------------------------------------------------------------------
# Per-gateway isolation inside the shared pool (ARCC cnt_PQjUx2msVXY1wU)
# ---------------------------------------------------------------------------


def test_each_gateway_gets_its_own_client_and_its_own_scope(shared_env):
    """Sharing the pool must not share the credential. Two gateways in one pool get
    separate app clients and separate resource servers, and each client is granted
    ONLY its own scope — Cognito refuses a client_credentials request for a scope the
    client is not configured for, so gateway A's credential cannot mint a token for
    gateway B."""
    cog = shared_env(_FakeCognito())
    a = gateway_deployer._create_cognito_oauth(cog, "gw-alpha", REGION)
    b = gateway_deployer._create_cognito_oauth(cog, "gw-beta", REGION)

    assert cog.created_resource_servers == [(POOL, "agentcore-gw-alpha"), (POOL, "agentcore-gw-beta")]
    assert a["client_info"]["scope"] == "agentcore-gw-alpha/invoke"
    assert b["client_info"]["scope"] == "agentcore-gw-beta/invoke"
    for kw in cog.created_clients:
        assert len(kw["AllowedOAuthScopes"]) == 1, (
            "a client granted more than its own scope could mint a token another "
            f"gateway's authorizer accepts: {kw['AllowedOAuthScopes']}"
        )
        assert kw["AllowedOAuthFlows"] == ["client_credentials"]
        assert kw["GenerateSecret"] is True


def test_the_authorizer_is_pinned_to_this_gateways_client(shared_env):
    """The property that makes sharing a pool safe. Every gateway's authorizer trusts
    the same ISSUER now, so allowedClients is the boundary: a token minted by another
    client in the same pool must be rejected even if it carried the right scope.
    Widening this to more than one client id would dissolve that boundary."""
    cog = shared_env(_FakeCognito())
    out = gateway_deployer._create_cognito_oauth(cog, "gw-alpha", REGION)
    jwt_cfg = out["authorizer_config"]["customJWTAuthorizer"]
    assert jwt_cfg["allowedClients"] == ["sharedclient0000"]
    assert jwt_cfg["discoveryUrl"] == (
        f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}/.well-known/openid-configuration"
    )


def test_the_client_secret_never_leaves_the_function(shared_env):
    """Same rule as the dedicated-pool path: the result is re-emitted into the Step
    Functions execution history at every state, written to DynamoDB, and returned by
    GET /api/deploy/{id}. resolve_client_secret re-reads it on demand instead.
    ARCC cnt_vtSS0S3iwKjSuk."""
    cog = shared_env(_FakeCognito())
    out = gateway_deployer._create_cognito_oauth(cog, "gw-alpha", REGION)
    assert "client_secret" not in out["client_info"]
    assert "notarealclientsecret" not in repr(out)


# ---------------------------------------------------------------------------
# The client secret moves to Secrets Manager (F-1b)
# ---------------------------------------------------------------------------
#
# The runtime used to resolve this secret with cognito-idp:DescribeUserPoolClient.
# That action's only IAM resource type is ``userpool``, with NOTHING below it, so the
# grant necessarily also read every other gateway's client secret in the same pool —
# and in the dedicated-pool mode the tag condition that looked like per-deployment
# scoping (``aws:ResourceTag/AgentCoreStack``) carries the STACK id, which every
# deployment in the stack stamps identically. There is no narrower Cognito grant to
# retreat to, so the credential moved to Secrets Manager, which does scope per ARN.


@pytest.mark.parametrize(
    "env,label",
    [(True, "shared pool"), (False, "dedicated pool")],
)
def test_both_pool_paths_mint_a_secret_reference(monkeypatch, fake_secrets, env, label):
    """BOTH paths, because the dedicated path had the same cross-tenant read. A fix
    applied only to the shared path would leave the other one resolving its secret
    through a pool-wide grant."""
    if env:
        monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", POOL)
        monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", DOMAIN)
        monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", lambda region: _FakeCognito())
    else:
        monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
        monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)

    out = gateway_deployer._create_cognito_oauth(_FakeCognito(), "gw-alpha", REGION)

    ref = out["client_info"].get("client_secret_ref")
    assert ref, f"the {label} path emitted no client_secret_ref, so the runtime falls back to the Cognito grant"
    assert ref.startswith("arn:aws:secretsmanager:"), ref
    assert len(fake_secrets.created) == 1, f"{label}: expected exactly one minted secret"
    written = json.loads(fake_secrets.created[0]["SecretString"])
    assert written == {"clientSecret": "notarealclientsecret-0000000000"}, (
        "the payload key must be one resolve_client_secret dereferences, or the "
        "reference resolves to nothing at the moment of use"
    )
    assert fake_secrets.created[0]["Name"].startswith("agentcore-connector/"), (
        "the runtime role's GetSecretValue grant is scoped to this namespace; a secret "
        "written outside it is unreadable by the agent that needs it"
    )


def test_the_minted_secret_is_bound_to_the_owner_and_the_deploy(fake_secrets, shared_env):
    """A prefix is not an owner. ``agentcore-connector/`` marks the PRODUCT and
    ``AgentCoreStack`` marks the STACK, so neither tells two co-resident tenants'
    secrets apart. The binding tags are what a per-tenant ownership check can read."""
    gateway_deployer._create_cognito_oauth(
        shared_env(_FakeCognito()), "gw-alpha", REGION, owner_sub="user-sub-abc", deployment_id="dep-123"
    )
    tags = {t["Key"]: t["Value"] for t in fake_secrets.created[0]["Tags"]}
    assert tags.get("DeploymentId") == "dep-123"
    assert "OwnerSubHash" in tags, f"the minted secret carries no owner binding: {tags}"
    assert tags["OwnerSubHash"] != "user-sub-abc", (
        "the sub must be HASHED — the binding only needs to be compared, and a raw sub "
        "would then appear in ListSecrets, Config history and every cost report"
    )
    assert "user-sub-abc" not in json.dumps(fake_secrets.created[0]["Tags"])


def test_a_pool_id_is_not_emitted_alongside_the_reference(shared_env):
    """Mutual exclusion has to be structural. Emitting both would leave the pool id in
    the runtime environment as a standing invitation to re-add the grant, and
    ``runtime_configure_step``/``deployment.py`` both branch on which key is present."""
    from app.services.runtime_deployer import client_secret_grant_targets

    out = gateway_deployer._create_cognito_oauth(shared_env(_FakeCognito()), "gw-alpha", REGION)
    pool_arn, secret_arn = client_secret_grant_targets(out["client_info"], REGION, "123456789012")
    assert secret_arn, "the per-agent role has no way to read the secret it was given a ref to"
    assert pool_arn is None, (
        "a per-agent role was handed cognito-idp:DescribeUserPoolClient it does not "
        f"need, and that grant cannot be narrowed to the one client it owns: {pool_arn}"
    )


def test_a_cognito_client_with_no_secret_fails_the_deploy(shared_env):
    """Never fall back to the pool id. A fallback here is a GREEN deploy with a dead
    tool plane — the exact failure mode this area already produced once, when an
    unsatisfiable tag condition meant nothing failed and no tools appeared."""

    class _NoSecret(_FakeCognito):
        def create_user_pool_client(self, **kw):
            self.created_clients.append(kw)
            return {"UserPoolClient": {"ClientId": "sharedclient0000"}}

    with pytest.raises(RuntimeError, match="no ClientSecret"):
        gateway_deployer._create_cognito_oauth(shared_env(_NoSecret()), "gw-alpha", REGION)


def test_an_existing_resource_server_is_not_a_failure(shared_env):
    """A redeploy of the same gateway name hits an already-existing resource server.
    Cognito has no upsert, so AlreadyExists is the success case — and the log must not
    echo the botocore message, which names the pool and the request parameters."""
    boom = Exception(
        "An error occurred (InvalidParameterException) when calling the "
        f"CreateResourceServer operation: {POOL} already has identifier agentcore-gw-alpha"
    )
    cog = shared_env(_FakeCognito(resource_server_raises=boom, existing_resource_server=True))
    out = gateway_deployer._create_cognito_oauth(cog, "gw-alpha", REGION)
    assert out["client_info"]["scope"] == "agentcore-gw-alpha/invoke"
    assert out["client_info"]["user_pool_id"] == POOL


def test_a_create_error_with_no_proven_resource_server_is_a_failure(shared_env):
    """The pair of the test above: an AccessDenied is not "already exists", and a
    client must not be minted for a scope no read proves is defined."""
    boom = Exception("An error occurred (AccessDeniedException) when calling the CreateResourceServer operation")
    fake = _FakeCognito(resource_server_raises=boom)
    cog = shared_env(fake)
    with pytest.raises(Exception, match="AccessDenied"):
        gateway_deployer._create_cognito_oauth(cog, "gw-alpha", REGION)
    assert fake.created_clients == []


# ---------------------------------------------------------------------------
# The shared pool must be undeletable by any single agent's teardown
# ---------------------------------------------------------------------------


def test_the_predicate_only_matches_the_configured_pool(shared_env):
    assert gateway_deployer.is_platform_owned_user_pool(POOL) is True
    assert gateway_deployer.is_platform_owned_user_pool(f"  {POOL}  ") is True
    assert gateway_deployer.is_platform_owned_user_pool("us-east-1_SOMEONEELSE") is False
    assert gateway_deployer.is_platform_owned_user_pool("") is False


def test_with_no_shared_pool_configured_nothing_is_protected(monkeypatch):
    """An empty env var must not make every pool look platform-owned — that would
    silently disable pool teardown for every deployment and leak a pool per deploy."""
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", "")
    assert gateway_deployer.is_platform_owned_user_pool("") is False
    assert gateway_deployer.is_platform_owned_user_pool(POOL) is False


def test_readopting_a_gateway_does_not_delete_the_shared_pool(shared_env):
    """_cleanup_old_cognito_pool parses a pool id out of the EXISTING gateway's
    discoveryUrl and deletes it. Once every gateway's authorizer points at the shared
    pool, that would delete the shared pool on the first re-adoption — taking every
    other deployed agent's gateway credentials and a 727s domain with it."""
    cog = _FakeCognito()
    gw_detail = {
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": (f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}/.well-known/openid-configuration"),
                "allowedClients": ["sharedclient0000"],
            }
        }
    }
    gateway_deployer._cleanup_old_cognito_pool(gw_detail, cog)

    assert cog.deleted_pools == [], f"the shared pool must survive; deleted: {cog.deleted_pools}"
    assert cog.deleted_domains == [], "and so must its warm domain — 727s to rebuild"


def test_readopting_still_deletes_a_gateways_own_old_pool(shared_env):
    """The guard must not disable the cleanup it is narrowing. A pool this deployment
    created is still torn down, or every gateway redeploy leaks one."""
    cog = _FakeCognito()
    own = "us-east-1_OWNPOOL00"
    gw_detail = {
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": (f"https://cognito-idp.{REGION}.amazonaws.com/{own}/.well-known/openid-configuration")
            }
        }
    }
    gateway_deployer._cleanup_old_cognito_pool(gw_detail, cog)
    assert cog.deleted_pools == [own]
    assert cog.deleted_domains == [DOMAIN]


def test_teardown_deletes_only_this_gateways_client_and_scope(shared_env, monkeypatch):
    """cleanup_gateway_resources on a shared-pool gateway: the app client and the
    resource server go, the pool and the domain stay."""
    cog = shared_env(_FakeCognito())
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: cog)

    class _Ctrl:
        def __init__(self):
            self.deleted = False

        def get_gateway(self, **kw):
            if self.deleted:
                raise RuntimeError("ResourceNotFoundException: gateway is gone")
            return {"gatewayArn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/agent-gateway-abc")}

        def list_tags_for_resource(self, **kw):
            from app.services.resource_ownership import owner_tags

            return {"tags": owner_tags(REGION)}

        def list_gateway_targets(self, **kw):
            return {"items": []}

        def delete_gateway(self, **kw):
            self.deleted = True
            return {}

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: _Ctrl())
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda *a, **kw: None, raising=False)

    log = gateway_deployer.cleanup_gateway_resources(
        "rt-1",
        REGION,
        {
            "gateway_id": "agent-gateway-abc",
            "client_info": {
                "user_pool_id": POOL,
                "client_id": "sharedclient0000",
                "scope": "agentcore-gw-alpha/invoke",
                "shared_pool": True,
            },
        },
    )

    assert cog.deleted_clients == [(POOL, "sharedclient0000")]
    assert cog.deleted_resource_servers == [(POOL, "agentcore-gw-alpha")], (
        "without this the per-gateway scope accumulates in the shared pool forever"
    )
    assert cog.deleted_pools == [], f"the shared pool must survive teardown; log: {log}"
    assert cog.deleted_domains == []


def test_the_shared_pool_is_never_recorded_as_a_deletable_resource(shared_env):
    """Defence in depth, and the primary guard: if the pool never enters the teardown
    manifest, the generic _delete_managed_resource dispatcher can never see it."""
    from app.step_handlers import gateway_step

    recorded: list[dict] = []

    class _Store:
        def record_resource(self, deployment_id, res):
            recorded.append(res)

    gateway_step._record_gateway_resources(
        _Store(),
        "d-1",
        REGION,
        {
            "gateway_id": "agent-gateway-abc",
            "gateway_name": "gw-alpha",
            "client_info": {"user_pool_id": POOL, "client_id": "c", "shared_pool": True},
        },
    )
    pools = [r for r in recorded if r["type"] == "cognito_user_pool"]
    assert pools == [], f"the shared pool must not be recorded as deletable: {pools}"


def test_a_gateways_own_pool_is_still_recorded(shared_env):
    """The guard is on `shared_pool`, not on recording pools at all — a pool the
    deploy created must still be torn down."""
    from app.step_handlers import gateway_step

    recorded: list[dict] = []

    class _Store:
        def record_resource(self, deployment_id, res):
            recorded.append(res)

    gateway_step._record_gateway_resources(
        _Store(),
        "d-2",
        REGION,
        {
            "gateway_id": "agent-gateway-abc",
            "gateway_name": "gw-alpha",
            "client_info": {"user_pool_id": "us-east-1_OWNPOOL00", "client_id": "c"},
        },
    )
    pools = [r for r in recorded if r["type"] == "cognito_user_pool"]
    assert [p["id"] for p in pools] == ["us-east-1_OWNPOOL00"]


def test_a_stale_manifest_row_cannot_delete_the_shared_pool(shared_env, monkeypatch):
    """The last line of defence. Even if a row naming the shared pool exists — written
    by an older build, or a future recorder that forgets the guard — the teardown
    dispatcher must refuse it."""
    from app.step_handlers import status_update_step

    cog = _FakeCognito()
    monkeypatch.setattr(status_update_step.step_clients, "client", lambda *a, **kw: cog)

    with pytest.raises(status_update_step._ResourceRetained):
        status_update_step._cleanup_resource(
            {"type": "cognito_user_pool", "id": POOL},
            REGION,
            {},
        )

    assert cog.deleted_pools == []
    assert cog.deleted_domains == []


def test_the_other_manifest_teardown_path_also_refuses_it(shared_env, monkeypatch):
    """There are TWO manifest teardown dispatchers — status_update_step._cleanup_resource
    (the Step Functions path) and deployment_handler._delete_managed_resource (the
    direct DELETE path). A guard on only one of them leaves the other able to delete
    the shared pool, and which one runs depends on how the deployment was made."""
    from app import deployment_handler
    from app.services import step_clients

    # _delete_managed_resource rebinds `boto3` to a local shim that routes through
    # step_clients.client for cross-account teardown, so THAT is the seam. Patching
    # deployment_handler.boto3 does nothing and lets the call reach real AWS.
    called: list[str] = []

    def _no_aws(event, service, **kwargs):
        called.append(service)
        return _FakeCognito()

    monkeypatch.setattr(step_clients, "client", _no_aws)

    line = deployment_handler._delete_managed_resource({"type": "cognito_user_pool", "id": POOL}, REGION)

    assert called == [], "it must refuse BEFORE constructing a cognito client"
    assert "shared platform gateway-auth pool" in line, line
    assert "skipped" in line


def test_a_gateways_own_pool_still_reaches_the_teardown(shared_env, monkeypatch):
    """The companion: the guard must be narrow. A pool this deployment created must
    still be deleted by the same dispatcher, or every deploy leaks a pool and the
    guard has traded one bug for a worse one."""
    from app import deployment_handler
    from app.services import step_clients

    cog = _FakeCognito()
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kw: cog)

    deployment_handler._delete_managed_resource({"type": "cognito_user_pool", "id": "us-east-1_OWNPOOL00"}, REGION)
    assert cog.deleted_pools == ["us-east-1_OWNPOOL00"]


def test_every_place_that_records_a_cognito_pool_checks_shared_pool():
    """Structural, because the risk is a THIRD recorder.

    Two code paths record the teardown manifest — gateway_step (Step Functions) and
    deployment.py (direct deploy) — and the bug they both had was recording the
    shared pool as deletable. A future fourth path would reintroduce it, and no
    behavioural test can cover a call site that does not exist yet. So assert the
    invariant over the source: near every ``"type": "cognito_user_pool"`` record
    site there is a ``shared_pool`` check.

    The teardown dispatchers are excluded deliberately — they guard with
    ``is_platform_owned_user_pool`` instead, which the tests above cover.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "app"
    recorders = [root / "step_handlers" / "gateway_step.py", root / "services" / "deployment.py"]
    unguarded: list[str] = []
    for path in recorders:
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if '"type": "cognito_user_pool"' not in line:
                continue
            window = "\n".join(lines[max(0, i - 8) : i + 1])
            if "shared_pool" not in window:
                unguarded.append(f"{path.name}:{i + 1}")
    assert not unguarded, (
        "these sites record a cognito_user_pool into the teardown manifest without "
        f"checking client_info['shared_pool'] first: {unguarded}. Recording the "
        "platform's shared gateway-auth pool makes it deletable by one agent's "
        "teardown, which revokes every other deployed gateway's credentials."
    )
    assert len(recorders) == 2 and all(p.exists() for p in recorders), "recorder list drifted"


# ---------------------------------------------------------------------------
# A pool we cannot prove we created
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST. The shared-pool guard was first written as ONE boolean —
# "is this the shared pool?" — and the teardown was `if shared: … elif pool_id: delete`.
# Two separate bugs live in that shape:
#
#   1. `False` is the DESTRUCTIVE branch. So when GATEWAY_SHARED_USER_POOL_ID was
#      missing from a Lambda's environment (measured: the deployment Lambda had no
#      GATEWAY_* keys at all), the guard did not degrade to "skip the check" — it
#      routed the shared pool into delete_user_pool_domain + delete_user_pool.
#   2. "Not the shared pool" was treated as "ours to delete". A pool id reaching a
#      teardown from a foreign gateway's discoveryUrl, or from a stale manifest row,
#      was deleted on the strength of not matching one id.
#
# The fix inverted the question to "can we PROVE we created this?" via the
# AgentCoreStack owner tag, with three outcomes instead of two. An attempt to fix it
# by making the boolean return True for unprovable pools was worse, not better: the
# True branch deletes the app client and resource server INSIDE the pool, so a foreign
# pool would have lost its app client. Hence three states, and hence these tests.


def _foreign_gw_detail(pool_id: str) -> dict:
    return {
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": (
                    f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
                )
            }
        }
    }


@pytest.mark.parametrize(
    "kwargs,label",
    [
        ({"owned": False}, "untagged (an older build, or somebody else's pool)"),
        (
            {"tags": {"AgentCoreStack": "someoneelse-prod-us-east-1", "ManagedBy": "other"}},
            "tagged by a DIFFERENT deployment",
        ),
        ({"tags": {"Project": "acfe2e", "Environment": "p0920"}}, "CDK-style tags but no owner tag"),
        ({"describe_raises": Exception("AccessDeniedException")}, "tags unreadable (AccessDenied)"),
        ({"describe_raises": Exception("TooManyRequestsException")}, "tags unreadable (throttled)"),
    ],
)
def test_a_pool_we_cannot_prove_we_created_is_never_touched(shared_env, kwargs, label):
    """Zero mutating calls. Not the pool, not the domain, and — the trap in the first
    attempted fix — not the app client or resource server inside it either.

    AccessDenied and a throttle are in this list deliberately: a transient failure to
    READ tags must not become permission to delete. The cost is a leaked pool an
    operator removes by hand; the cost of the other direction is unrecoverable.
    """
    cog = _FakeCognito(**kwargs)
    foreign = "us-east-1_FOREIGN001"

    assert gateway_deployer.classify_user_pool(foreign, cognito_client=cog) == (
        gateway_deployer.POOL_FOREIGN_OR_UNKNOWN
    ), label

    gateway_deployer._cleanup_old_cognito_pool(_foreign_gw_detail(foreign), cog)

    assert cog.deleted_pools == [], f"{label}: deleted a pool it could not prove it created"
    assert cog.deleted_domains == [], f"{label}: deleted a foreign hosted domain"
    assert cog.deleted_clients == [], f"{label}: deleted an app client inside a foreign pool"
    assert cog.deleted_resource_servers == [], f"{label}: deleted a resource server in a foreign pool"


def test_the_shared_pool_is_classified_without_any_aws_call(shared_env):
    """The one pool that must never be touched is recognised from configuration alone.

    If classification needed a describe_user_pool to protect it, then an AccessDenied
    or a throttle on that call would decide the fate of the pool holding every
    deployed gateway's credentials. It must not be reachable that way at all.
    """
    cog = _FakeCognito(describe_raises=AssertionError("must not be called for the shared pool"))
    assert gateway_deployer.classify_user_pool(POOL, cognito_client=cog) == gateway_deployer.POOL_SHARED_EXACT
    assert cog.describe_calls == []


def test_a_foreign_pool_reaches_neither_branch_of_the_gateway_teardown(shared_env, monkeypatch):
    """The full cleanup_gateway_resources path, because the if/elif is where the
    original bug lived. A foreign pool must fall out of BOTH branches."""
    cog = _FakeCognito(owned=False)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: cog)

    class _Ctrl:
        def list_gateway_targets(self, **kw):
            return {"items": []}

        def delete_gateway(self, **kw):
            return {}

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: _Ctrl())
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(gateway_deployer, "_create_lambda_client", lambda *a, **kw: None, raising=False)

    log = gateway_deployer.cleanup_gateway_resources(
        "rt-1",
        REGION,
        {
            "gateway_id": "agent-gateway-abc",
            "client_info": {
                "user_pool_id": "us-east-1_FOREIGN001",
                "client_id": "someoneelsesclient",
                "scope": "agentcore-theirgateway/invoke",
            },
        },
    )

    assert cog.deleted_pools == []
    assert cog.deleted_domains == []
    assert cog.deleted_clients == [], "the SHARED branch would have deleted this app client"
    assert cog.deleted_resource_servers == []
    assert any("protected" in line for line in log), f"the skip must be reported, not silent: {log}"


def test_ownership_is_compared_against_the_pools_own_region(monkeypatch):
    """A pool is stamped with owner_tags(region) for the region it is created IN, and
    stack_id embeds that region. So a teardown running in one region against a pool
    this same deployment created in ANOTHER region must still recognise it.

    Comparing against the caller's region instead would leak exactly one pool per
    cross-region deploy — and this platform deploys the same {project}-{env} to two
    regions deliberately.
    """
    from app.services.resource_ownership import owner_tags

    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
    monkeypatch.setenv("APP_AWS_REGION", "us-east-1")

    frankfurt = "eu-central-1_OURPOOL01"
    cog = _FakeCognito(tags=owner_tags("eu-central-1"))

    assert gateway_deployer.classify_user_pool(frankfurt, cognito_client=cog) == (
        gateway_deployer.POOL_OWNED_BY_STACK
    ), "our own Frankfurt pool was classified foreign by a us-east-1 teardown"


def test_the_predicate_and_the_classifier_do_not_answer_the_same_question(shared_env):
    """Vacuity guard. These two must stay distinguishable, because the bug being
    prevented is a future edit collapsing them back into one boolean.

    is_platform_owned_user_pool answers only "is this the shared pool", and a caller
    that used it to decide DELETABILITY would read False for a foreign pool and delete
    it. classify_user_pool is the only thing allowed to gate a delete.
    """
    foreign = "us-east-1_FOREIGN001"
    cog = _FakeCognito(owned=False)

    assert gateway_deployer.is_platform_owned_user_pool(foreign) is False
    assert gateway_deployer.classify_user_pool(foreign, cognito_client=cog) != (gateway_deployer.POOL_OWNED_BY_STACK), (
        "a False from the predicate must NOT imply the pool is ours"
    )

    assert gateway_deployer.is_platform_owned_user_pool(POOL) is True
    assert gateway_deployer.classify_user_pool(POOL, cognito_client=cog) == gateway_deployer.POOL_SHARED_EXACT

    assert (
        len(
            {
                gateway_deployer.POOL_SHARED_EXACT,
                gateway_deployer.POOL_OWNED_BY_STACK,
                gateway_deployer.POOL_FOREIGN_OR_UNKNOWN,
            }
        )
        == 3
    ), "the three states collapsed"


def test_a_foreign_pool_in_a_stale_manifest_row_is_refused_by_both_dispatchers(shared_env, monkeypatch):
    """Both manifest teardown dispatchers, against a pool that is neither the shared
    pool nor ours. A manifest row is PERSISTED DATA — it can name a pool written by an
    older build, or by another deployment — so it is re-verified against the live
    resource rather than trusted."""
    from app import deployment_handler
    from app.services import step_clients
    from app.step_handlers import status_update_step

    foreign = "us-east-1_FOREIGN001"
    cog = _FakeCognito(owned=False)
    monkeypatch.setattr(step_clients, "client", lambda event, service, **kw: cog)

    line = deployment_handler._delete_managed_resource({"type": "cognito_user_pool", "id": foreign}, REGION)
    assert "protected" in line, line
    assert cog.deleted_pools == []

    with pytest.raises(status_update_step._ResourceRetained):
        status_update_step._cleanup_resource(
            {"type": "cognito_user_pool", "id": foreign},
            REGION,
            {},
        )
    assert cog.deleted_pools == []
    assert cog.deleted_domains == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
