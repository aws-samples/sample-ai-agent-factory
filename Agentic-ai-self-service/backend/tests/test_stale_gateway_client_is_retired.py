"""Redeploying a gateway must retire the app client it stops using (peer finding F-10).

THE DEFECT, in three parts, all on the gateway conflict-recovery path — the path
EVERY redeploy of an existing gateway takes:

1. ``_create_cognito_oauth_in_shared_pool`` calls ``create_user_pool_client``
   unconditionally, and Cognito happily accepts a duplicate client name. So a redeploy
   minted a *second* app client, repointed the gateway's authorizer at it, and
   overwrote the manifest row with the new client id. Nothing then named the old
   client, so no teardown ever deleted it: one live credential leaked per redeploy,
   each with its own secret and each still able to mint a token for that gateway's
   scope. ARCC ``cnt_QbLfysVKP69zGk`` is explicit that a rotation is only a rotation
   if the old credential is invalidated — *"create canary testing to continuously
   verify that old credentials are properly invalidated"*. Minting a replacement and
   leaving the original usable is duplication, not rotation.

2. The old Cognito pool was deleted BEFORE ``update_gateway`` ran, and an
   ``update_gateway`` failure was ``logger.warning``-ed and then ignored. Both halves
   failed in the same direction: the gateway was left pinned to a pool that no longer
   existed, so it could not validate any token, and the deploy reported success. That
   is the exact failure this module refuses elsewhere — a green deploy with a dead tool
   plane.

3. ``gateway`` was assigned from the adopted gateway's detail BEFORE the repoint, and
   ``deploy_gateway``'s abort handler reads ``locals().get("gateway")`` and hands its
   gatewayId to ``cleanup_gateway_resources``, which DELETES it. Turning (2) into a
   raise would therefore have destroyed a gateway that existed before this deploy
   started. The assignment now happens after the repoint succeeds.

MEASURED IAM, us-east-1, the gateway step role (``acfe2e-p0920-StepGatewayRole``),
which is what constrains the implementation:

    cognito-idp:ListUserPoolClients   implicitDeny   (against the pool ARN)
    cognito-idp:DeleteUserPoolClient  allowed        (against the pool ARN)
    cognito-idp:DescribeUserPoolClient allowed       (against the pool ARN)

So the stale client can NEVER be found by enumerating the pool. It is read instead out
of the gateway's own ``authorizerConfiguration.customJWTAuthorizer.allowedClients``,
which the conflict branch already fetched via ``get_gateway`` — no new IAM permission
and no extra API call. (Simulating ``DeleteUserPoolClient`` with no ``--resource-arns``
reports ``implicitDeny``; the same call against the pool ARN is ``allowed``. Always
simulate with the resource.)

That id comes off a resource this deploy did not necessarily create, so it is
attacker-influenced input to a delete and ownership is proven first, the F-7 way: a
client id read off a FOREIGN gateway names a client in a foreign pool, and deleting it
would break a stranger's gateway.
"""

import logging

import pytest
from app.services import gateway_deployer
from botocore.exceptions import ClientError

from tests.test_foreign_gateway_is_not_adopted import ADOPT


def _client_error(code: str, op: str, message: str) -> ClientError:
    """A real ClientError, because a bare ``Exception`` would let a weaker
    implementation pass: ``type(e).__name__`` on one is just ``Exception``, so a test
    built on bare exceptions cannot tell "reports the error code" from "reports
    nothing useful"."""
    return ClientError({"Error": {"Code": code, "Message": message}}, op)


REGION = "us-east-1"
SHARED_POOL = "us-east-1_SHAREDPOOL"
SHARED_DOMAIN = "acf-test-gw-0123456789"
OWN_POOL = "us-east-1_OWNPOOL00"
FOREIGN_POOL = "us-east-1_FOREIGN00"

OLD_CLIENT = "staleclient00000"
NEW_CLIENT = "freshclient00000"

GW_NAME = "agent-gateway"
GW_ID = "agent-gateway-fake0000"
GW_URL = f"https://{GW_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
# Must never be logged. A botocore message echoes the request parameters, and the
# DeleteUserPoolClient request names the pool and the client.
SECRET = "notarealclientsecret-0000000000"  # pragma: allowlist secret


def _auth(pool_id: str, clients: list[str]) -> dict:
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
            "allowedClients": list(clients),
        }
    }


class _FakeCognito:
    """Records deletes, and models pool TAGS because classify_user_pool keys on them.

    ``delete_user_pool`` refuses while a domain is attached, exactly as Cognito does,
    so "deletes the pool but orphans the domain" cannot pass.
    """

    def __init__(self, *, owned_pools: tuple[str, ...] = (OWN_POOL,), delete_client_raises: Exception | None = None):
        self.owned_pools = owned_pools
        self.delete_client_raises = delete_client_raises
        self.deleted_clients: list[tuple[str, str]] = []
        self.deleted_pools: list[str] = []
        self.deleted_domains: list[str] = []
        self.describe_calls: list[str] = []
        self.calls: list[str] = []

    def describe_user_pool(self, **kw):
        from app.services.resource_ownership import owner_tags

        pid = kw["UserPoolId"]
        self.describe_calls.append(pid)
        self.calls.append(f"describe_user_pool:{pid}")
        pool: dict = {} if self.deleted_domains else {"Domain": SHARED_DOMAIN}
        pool["UserPoolTags"] = dict(owner_tags(REGION)) if pid in self.owned_pools else {}
        return {"UserPool": pool}

    def delete_user_pool_client(self, **kw):
        self.calls.append(f"delete_user_pool_client:{kw['ClientId']}")
        if self.delete_client_raises is not None:
            raise self.delete_client_raises
        self.deleted_clients.append((kw["UserPoolId"], kw["ClientId"]))
        return {}

    def delete_user_pool_domain(self, **kw):
        self.calls.append("delete_user_pool_domain")
        self.deleted_domains.append(kw["Domain"])
        return {}

    def delete_user_pool(self, **kw):
        self.calls.append(f"delete_user_pool:{kw['UserPoolId']}")
        if not self.deleted_domains:
            raise Exception(
                "An error occurred (InvalidParameterException) when calling the DeleteUserPool "
                "operation: User pool cannot be deleted. It has a domain configured that should "
                "be deleted first."
            )
        self.deleted_pools.append(kw["UserPoolId"])
        return {}

    def list_user_pool_clients(self, **kw):  # pragma: no cover - must never be called
        raise AssertionError(
            "ListUserPoolClients is implicitDeny for the gateway step role. Reaching it "
            "means the implementation enumerates the pool instead of reading the "
            "gateway's own allowedClients, and it will fail closed in production."
        )


@pytest.fixture
def shared_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", SHARED_POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", SHARED_DOMAIN)


# ---------------------------------------------------------------------------
# The retirement helper itself
# ---------------------------------------------------------------------------


def test_the_stale_client_is_deleted_from_the_shared_pool(shared_env):
    """The leak. One live orphaned credential per redeploy, with its own secret."""
    cog = _FakeCognito()
    gateway_deployer._retire_stale_gateway_clients(
        _auth(SHARED_POOL, [OLD_CLIENT]),
        _auth(SHARED_POOL, [NEW_CLIENT]),
        cog,
    )
    assert cog.deleted_clients == [(SHARED_POOL, OLD_CLIENT)]
    assert cog.deleted_pools == [], "the shared pool itself must survive — it holds every other gateway's client"
    assert cog.deleted_domains == [], "and so must its warm domain (measured: 727s to become resolvable)"


def test_the_client_this_deploy_just_minted_is_never_deleted(shared_env):
    """The way this fix could destroy the deploy it is part of.

    The new authorizer is already live on the gateway by the time this runs, so
    deleting its client would leave the gateway unable to validate any token — the
    very outcome the ordering fix exists to prevent. ``allowedClients`` is a LIST and
    a redeploy can legitimately leave the previous client in it, so "delete everything
    that was there before" is not a safe rule.
    """
    cog = _FakeCognito()
    gateway_deployer._retire_stale_gateway_clients(
        _auth(SHARED_POOL, [OLD_CLIENT, NEW_CLIENT]),
        _auth(SHARED_POOL, [NEW_CLIENT]),
        cog,
    )
    assert cog.deleted_clients == [(SHARED_POOL, OLD_CLIENT)]
    assert NEW_CLIENT not in [c for _p, c in cog.deleted_clients]


def test_an_idempotent_redeploy_deletes_nothing(shared_env):
    """Same client in and out — there is nothing stale, and no API call to make."""
    cog = _FakeCognito()
    gateway_deployer._retire_stale_gateway_clients(
        _auth(SHARED_POOL, [NEW_CLIENT]),
        _auth(SHARED_POOL, [NEW_CLIENT]),
        cog,
    )
    assert cog.deleted_clients == []
    assert cog.describe_calls == [], "no stale client means the pool need not even be classified"


def test_a_foreign_pools_client_is_never_touched(shared_env, caplog):
    """The client id is read off a gateway we may not own, so it may name a stranger's
    client in a stranger's pool. Deleting it would break their gateway."""
    cog = _FakeCognito(owned_pools=())
    with caplog.at_level(logging.WARNING, logger="app.services.gateway_deployer"):
        gateway_deployer._retire_stale_gateway_clients(
            _auth(FOREIGN_POOL, [OLD_CLIENT]),
            _auth(SHARED_POOL, [NEW_CLIENT]),
            cog,
        )
    assert cog.deleted_clients == []
    assert any("not provably ours" in r.message for r in caplog.records)


def test_deleting_a_credential_is_logged_at_a_level_lambda_actually_emits(shared_env, caplog):
    """Found live, not by reading: at INFO this line did not exist in production.

    The deployed ``acfe2e-p0920-step-gateway`` log group, read straight after a real
    redeploy that provably deleted a client, held 17 WARNING records, 4 ERROR records
    and ZERO INFO. This module's logger is left at NOTSET and Lambda's root logger sits
    at WARNING, so an INFO record never reaches a handler. The effect was an audit gap
    pointing the wrong way: the *refusal* to delete (a warning) was visible while the
    deletion of a live credential was not.

    Capturing at WARNING is the whole mechanism of the test, not a detail — it sets the
    logger's threshold to WARNING, so an ``info()`` call is dropped at
    ``isEnabledFor`` and never reaches ``caplog.records``. Capturing at INFO instead
    would configure away the very defect and pass either way.
    """
    cog = _FakeCognito()
    with caplog.at_level(logging.WARNING, logger="app.services.gateway_deployer"):
        gateway_deployer._retire_stale_gateway_clients(
            _auth(SHARED_POOL, [OLD_CLIENT]), _auth(SHARED_POOL, [NEW_CLIENT]), cog
        )
    assert cog.deleted_clients == [(SHARED_POOL, OLD_CLIENT)]
    hits = [r for r in caplog.records if "Deleted stale gateway app client" in r.getMessage()]
    assert hits, "the deletion of a live credential left no record at a level Lambda emits"
    assert all(r.levelno >= logging.WARNING for r in hits)
    # ...and it still names only the pool. The client id is half of the credential.
    assert all(OLD_CLIENT not in r.getMessage() for r in hits)


def test_deleting_a_whole_user_pool_is_logged_at_that_level_too(shared_env, caplog):
    """The same defect on the larger action, and it was there before this fix: deleting
    a user pool destroys every identity in it, and at INFO that left no trace either."""
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    with caplog.at_level(logging.WARNING, logger="app.services.gateway_deployer"):
        gateway_deployer._cleanup_old_cognito_pool({"authorizerConfiguration": _auth(OWN_POOL, [OLD_CLIENT])}, cog)
    assert cog.deleted_pools == [OWN_POOL]
    hits = [r for r in caplog.records if "Cleaned up old Cognito pool" in r.getMessage()]
    assert hits, "a deleted user pool left no record at a level Lambda emits"
    assert all(r.levelno >= logging.WARNING for r in hits)


def test_a_pool_this_deploy_owns_also_has_its_stale_client_retired(shared_env):
    """Not redundant with the pool delete. ``_cleanup_old_cognito_pool`` swallows its
    own failures (and ``delete_user_pool`` really does fail while a domain is
    attached), so a pool delete that silently does not happen would otherwise leave
    the old client live with nothing naming it."""
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    gateway_deployer._retire_stale_gateway_clients(
        _auth(OWN_POOL, [OLD_CLIENT]),
        _auth(SHARED_POOL, [NEW_CLIENT]),
        cog,
    )
    assert cog.deleted_clients == [(OWN_POOL, OLD_CLIENT)]


@pytest.mark.parametrize(
    ("url", "label"),
    [
        (f"https://evil.example.com/{SHARED_POOL}/.well-known/openid-configuration", "a host that is not Cognito"),
        (
            f"https://cognito-idp.{REGION}.amazonaws.com.evil.example/{SHARED_POOL}/.well-known/x",
            "amazonaws.com as a substring, not a suffix",
        ),
        (f"https://cognito-idp.{REGION}.amazonaws.com/.well-known/openid-configuration", "no pool segment"),
        ("", "no discoveryUrl at all"),
        ("not-a-url", "unparseable"),
        # The right host, but the first path segment is not a pool id. Cognito pool ids
        # are always `<region>_<suffix>`, so this is rejected WITHOUT spending a
        # DescribeUserPool on it -- which also means classify_user_pool is never handed
        # an id whose region it cannot parse.
        (
            f"https://cognito-idp.{REGION}.amazonaws.com/.well-known/../garbage/openid-configuration",
            "a Cognito host with a non-pool first segment",
        ),
    ],
)
def test_a_discovery_url_that_is_not_cognitos_deletes_nothing(shared_env, url, label):
    """The discoveryUrl is attacker-influenced: it is whatever the adopted gateway
    says. Validating the HOST exactly (urlparse.hostname, prefix AND suffix) rather
    than substring-matching the raw URL is what stops a crafted URL steering a delete
    (py/incomplete-url-substring-sanitization)."""
    cog = _FakeCognito()
    previous = {"customJWTAuthorizer": {"discoveryUrl": url, "allowedClients": [OLD_CLIENT]}}
    gateway_deployer._retire_stale_gateway_clients(previous, _auth(SHARED_POOL, [NEW_CLIENT]), cog)
    assert cog.deleted_clients == [], label
    assert cog.describe_calls == [], label


def test_an_empty_previous_authorizer_is_a_no_op(shared_env):
    """A gateway with no CUSTOM_JWT authorizer at all, and the ``{}`` the caller
    passes when ``get_gateway`` returns no authorizerConfiguration."""
    cog = _FakeCognito()
    for previous in ({}, {"customJWTAuthorizer": {}}, _auth(SHARED_POOL, [])):
        gateway_deployer._retire_stale_gateway_clients(previous, _auth(SHARED_POOL, [NEW_CLIENT]), cog)
    assert cog.deleted_clients == []


def test_a_delete_failure_neither_fails_the_deploy_nor_echoes_the_credential(shared_env, caplog):
    """Retirement is hygiene, not the deploy's purpose: a failure here must not fail a
    gateway that is already correctly repointed. But the botocore message for this call
    echoes the request parameters, which name the pool AND the client id — so only the
    exception TYPE may be logged (ARCC ``cnt_rHmO501l15qr2W``)."""
    boom = _client_error(
        "NotAuthorizedException",
        "DeleteUserPoolClient",
        f"UserPoolId={SHARED_POOL}, ClientId={OLD_CLIENT}, secret={SECRET}",
    )
    cog = _FakeCognito(delete_client_raises=boom)
    with caplog.at_level(logging.WARNING, logger="app.services.gateway_deployer"):
        gateway_deployer._retire_stale_gateway_clients(
            _auth(SHARED_POOL, [OLD_CLIENT]),
            _auth(SHARED_POOL, [NEW_CLIENT]),
            cog,
        )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "NotAuthorizedException" in text
    assert SECRET not in text
    assert OLD_CLIENT not in text
    assert SHARED_POOL not in text


def test_a_non_client_error_still_names_something(shared_env, caplog):
    """``error_code`` returns ``''`` for anything that is not a ClientError -- a
    TypeError from a bad stub, a connection reset. Reporting an empty string there
    would produce "Could not delete stale app client: " and answer nothing."""
    cog = _FakeCognito(delete_client_raises=TimeoutError("connection reset"))
    with caplog.at_level(logging.WARNING, logger="app.services.gateway_deployer"):
        gateway_deployer._retire_stale_gateway_clients(
            _auth(SHARED_POOL, [OLD_CLIENT]),
            _auth(SHARED_POOL, [NEW_CLIENT]),
            cog,
        )
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)


def test_the_pool_id_parser_is_shared_with_the_pool_cleanup(shared_env):
    """One parser, because the host validation is the security-relevant half and two
    copies would drift. ``_cleanup_old_cognito_pool`` reads the same function."""
    assert gateway_deployer._pool_id_from_authorizer(_auth(SHARED_POOL, [])) == SHARED_POOL
    assert gateway_deployer._pool_id_from_authorizer({}) == ""


# ---------------------------------------------------------------------------
# Ordering, through the real conflict-recovery path in deploy_gateway
# ---------------------------------------------------------------------------


class _FakeCtrl:
    """A control plane whose create_gateway always conflicts, i.e. every redeploy."""

    def __init__(self, *, existing_auth: dict, update_raises: Exception | None = None):
        self.existing_auth = existing_auth
        self.update_raises = update_raises
        self.calls: list[str] = []
        self.deleted_gateways: list[str] = []
        self.updated_authorizers: list[dict] = []

    def create_gateway(self, **kw):
        self.calls.append("create_gateway")
        raise Exception("An error occurred (ConflictException): Gateway with name already exists")

    def list_gateways(self, **kw):
        self.calls.append("list_gateways")
        return {"items": [{"name": GW_NAME, "gatewayId": GW_ID, "roleArn": "arn:aws:iam::1:role/gwrole"}]}

    def get_gateway(self, **kw):
        self.calls.append("get_gateway")
        return {
            "name": GW_NAME,
            "gatewayId": GW_ID,
            "gatewayUrl": GW_URL,
            "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:1:gateway/{GW_ID}",
            "roleArn": "arn:aws:iam::1:role/gwrole",
            "protocolType": "MCP",
            "status": "READY",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": self.existing_auth,
        }

    def update_gateway(self, **kw):
        self.calls.append("update_gateway")
        if self.update_raises is not None:
            raise self.update_raises
        self.updated_authorizers.append(kw["authorizerConfiguration"])
        # It lands: the next GetGateway reads it back (F-66e waits for that).
        self.existing_auth = kw["authorizerConfiguration"]
        return {}

    def list_gateway_targets(self, **kw):
        return {"items": []}

    def create_gateway_target(self, **kw):
        return {"targetId": "T0000000"}

    def delete_gateway_target(self, **kw):
        return {}

    def delete_gateway(self, **kw):
        self.calls.append("delete_gateway")
        self.deleted_gateways.append(kw["gatewayIdentifier"])
        return {}


class _FakeIamExceptions:
    class EntityAlreadyExistsException(Exception):
        pass


class _FakeIam:
    exceptions = _FakeIamExceptions()

    def create_role(self, **kw):
        return {"Role": {"Arn": f"arn:aws:iam::166827918465:role/{kw['RoleName']}"}}

    def put_role_policy(self, **kw):
        return {}


class _FakeSts:
    def get_caller_identity(self):
        return {"Account": "166827918465"}


def _install(monkeypatch, *, ctrl, cog):
    """Stub every AWS edge of deploy_gateway, leaving the conflict branch itself real.

    The two doubles are cross-wired onto ONE ``calls`` list, which the ordering test
    then reads as a single interleaved sequence. Two separate lists would not do:
    concatenating "the control-plane calls" and "the Cognito calls" yields
    ``[update_gateway, delete_user_pool]`` whatever the real order was, so the
    assertion would hold even with the delete back in front of the update.
    """
    cog.calls = ctrl.calls
    cleanup_partials: list[dict] = []

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: cog)
    # The shared pool is a platform-account resource (F-42); a dedicated pool never reaches this.
    monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", lambda region: cog)
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: _FakeIam())
    monkeypatch.setattr(
        gateway_deployer,
        "_create_cognito_oauth",
        lambda *a, **kw: {
            "authorizer_config": _auth(SHARED_POOL, [NEW_CLIENT]),
            "client_info": {
                "provider": "cognito",
                "user_pool_id": SHARED_POOL,
                "client_id": NEW_CLIENT,
                "client_secret": SECRET,
                "token_endpoint": f"https://{SHARED_DOMAIN}.auth.{REGION}.amazoncognito.com/oauth2/token",
                "shared_pool": True,
                "scope": f"agentcore-{GW_NAME}/gateway.access",
            },
        },
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway",
        lambda *a, **kw: {"gatewayUrl": GW_URL, "roleArn": "arn:aws:iam::1:role/gwrole", "status": "READY"},
    )
    monkeypatch.setattr(gateway_deployer, "_resolve_gateway_tool_actions", lambda *a, **kw: ([], 0))
    monkeypatch.setattr(gateway_deployer, "_wait_for_gateway_to_serve_tools", lambda *a, **kw: 0)
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_connector_targets",
        lambda *a, **kw: {"credential_provider_names": [], "secret_arns": [], "spec_s3_uris": []},
    )
    monkeypatch.setattr(
        gateway_deployer,
        "_deploy_external_mcp_targets",
        lambda *a, **kw: {"credential_provider_names": [], "secret_arns": []},
    )

    def _fake_cleanup(name, region, partial):
        cleanup_partials.append(dict(partial))
        return []

    monkeypatch.setattr(gateway_deployer, "cleanup_gateway_resources", _fake_cleanup)
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(gateway_deployer.boto3, "client", lambda *a, **kw: _FakeSts())
    return cleanup_partials


def test_the_old_pool_is_deleted_only_after_the_authorizer_is_repointed(shared_env, monkeypatch):
    """The ordering bug, as a sequence. The delete used to come FIRST, which left the
    gateway pinned to a pool that no longer existed."""
    ctrl = _FakeCtrl(existing_auth=_auth(OWN_POOL, [OLD_CLIENT]))
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-order", **ADOPT)

    assert out["gateway_id"] == GW_ID
    assert ctrl.updated_authorizers == [_auth(SHARED_POOL, [NEW_CLIENT])]
    assert cog.deleted_pools == [OWN_POOL], "the gateway's own old pool must still be reclaimed"
    # cog and ctrl share one journal (see _install), so this IS the real sequence.
    order = [
        c
        for c in ctrl.calls
        if c == "update_gateway" or c.startswith(("delete_user_pool:", "delete_user_pool_client:"))
    ]
    assert order == [
        "update_gateway",
        f"delete_user_pool_client:{OLD_CLIENT}",
        f"delete_user_pool:{OWN_POOL}",
    ], f"the repoint must come first, and the credential before the pool that holds it: {order}"
    # And the stale client went with it.
    assert (OWN_POOL, OLD_CLIENT) in cog.deleted_clients


def test_a_shared_pool_redeploy_retires_the_previous_app_client(shared_env, monkeypatch):
    """The leak, end to end through the path a real redeploy takes."""
    ctrl = _FakeCtrl(existing_auth=_auth(SHARED_POOL, [OLD_CLIENT]))
    cog = _FakeCognito()
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-shared", **ADOPT)

    assert out["gateway_id"] == GW_ID
    assert cog.deleted_clients == [(SHARED_POOL, OLD_CLIENT)], (
        "before this fix the previous app client stayed live in the shared pool forever, "
        "with its own secret and nothing naming it"
    )
    assert cog.deleted_pools == []
    assert cog.deleted_domains == []


def test_a_failed_repoint_fails_the_deploy_and_leaves_everything_alone(shared_env, monkeypatch):
    """The swallowed warning. A gateway that could not be repointed cannot validate
    the tokens this deploy mints, so reporting success ships a dead tool plane.

    And the refusal has to be TRUE when it says the existing gateway was left as it
    was: the abort handler deletes ``locals()["gateway"]``, so the adopted gateway
    must not be in scope yet.
    """
    ctrl = _FakeCtrl(
        existing_auth=_auth(OWN_POOL, [OLD_CLIENT]),
        update_raises=_client_error(
            "AccessDeniedException",
            "UpdateGateway",
            # A botocore message really does echo the request, and for UpdateGateway
            # that request carries the whole authorizerConfiguration.
            f"not authorized to perform: bedrock-agentcore:UpdateGateway; "
            f"authorizerConfiguration={_auth(SHARED_POOL, [NEW_CLIENT])}",
        ),
    )
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    cleanup_partials = _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-fail", **ADOPT)

    assert out["success"] is False
    assert "could not be repointed" in out["error"]
    # The CODE is what tells an operator this is a missing UpdateGateway grant rather
    # than an AgentCore flake, so the refusal is actionable...
    assert "AccessDeniedException" in out["error"]
    # ...but the botocore message is not, and this string is rendered in the UI as a
    # Step Functions failure Cause.
    assert "authorizerConfiguration" not in out["error"]
    assert NEW_CLIENT not in out["error"]
    # Nothing retired, because nothing was replaced.
    assert cog.deleted_clients == []
    assert cog.deleted_pools == []
    assert cog.deleted_domains == []
    # And the gateway that existed before this deploy is still there.
    assert ctrl.deleted_gateways == []
    assert [p.get("gateway_id") for p in cleanup_partials] == [None] or cleanup_partials == [], (
        f"the abort must not hand the ADOPTED gateway to cleanup_gateway_resources: {cleanup_partials}"
    )
    assert out.get("gateway_id") is None


def test_the_failed_repoint_still_reports_the_client_it_minted(shared_env, monkeypatch):
    """Not an orphan of its own. The Cognito app client, resource server and minted
    secret were created BEFORE the repoint, so the failure inventory has to name them
    or this fix trades a leaked old client for a leaked new one."""
    ctrl = _FakeCtrl(
        existing_auth=_auth(OWN_POOL, [OLD_CLIENT]),
        update_raises=_client_error(
            "AccessDeniedException",
            "UpdateGateway",
            # A botocore message really does echo the request, and for UpdateGateway
            # that request carries the whole authorizerConfiguration.
            f"not authorized to perform: bedrock-agentcore:UpdateGateway; "
            f"authorizerConfiguration={_auth(SHARED_POOL, [NEW_CLIENT])}",
        ),
    )
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-fail-inv", **ADOPT)

    ci = out["client_info"]
    assert ci["user_pool_id"] == SHARED_POOL
    assert ci["client_id"] == NEW_CLIENT
    assert ci["scope"] == f"agentcore-{GW_NAME}/gateway.access"
    assert ci["shared_pool"] is True
    assert "client_secret" not in ci, "this dict travels into an SFN failure cause"
    assert SECRET not in str(out)
