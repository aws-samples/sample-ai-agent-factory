"""A same-named gateway is adopted only when ownership can be PROVEN (F-10, part 4).

THE DEFECT. ``create_gateway`` raises ``ConflictException`` for two causes the
exception cannot distinguish: this deployment is redeploying its own gateway, or
something else in the account already holds that name. AgentCore gateway names are
account-global and ours come straight from a user-chosen canvas field, so the second is
reachable by typing a name. The conflict-recovery branch assumed the first and went
straight to ``update_gateway``, repointing the stranger's gateway at this deployment's
Cognito authorizer. That is not a cosmetic mislabel: the gateway then rejects the tokens
its real owner's clients present, and the previous ``authorizerConfiguration`` is gone,
so it cannot be put back from here. The live account holds exactly this collision —
gateway ``omargw``, pinned to the foreign pool ``AgentCore-omargw``.

WHY NOT A TAG. ``bedrock-agentcore:ListTagsForResource`` and ``TagResource`` both exist
for the ``gateway`` resource type (AWS Service Reference feed, 255 actions) and
``CreateGateway`` accepts ``tags`` — but both measure ``implicitDeny`` for the gateway
step role, and every gateway that already exists is untagged. A tag-only check would
refuse each of those on its next redeploy, which is the failure
``resource_ownership``'s own docstring warns about: fail-closed over an incomplete
ownership table removes the feature instead of securing it.

THE TWO PROOFS, both computable from data the branch already has:

1. The gateway's **IAM role**. By the time ``create_gateway`` runs, this deployment
   holds ``gw_role_arn`` by one of two routes, and both are ownership-proven: either
   ``create_role`` had just succeeded (nothing older can reference a role that did not
   exist), or the ``EntityAlreadyExists`` branch cleared
   ``assert_this_deployment_may_mutate`` against the role's tags. A pre-existing gateway
   pointing at that role was created by a principal holding ``iam:PassRole`` on our own
   role. This is the proof that keeps an **external-OAuth** redeploy working, since such
   a gateway has no Cognito authorizer for proof 2 to read.
2. The gateway's **authorizer**. Ours, when Cognito-backed, is pinned either to the
   platform's shared gateway-auth pool or to a pool this stack created and owner-tagged
   — which is exactly what ``classify_user_pool`` decides, via a ``describe_user_pool``
   grant already proven live. No new IAM action, no new API call.

WHERE THE CHECK HAS TO GO, and it is not interchangeable with anywhere else in the
branch: above the ``update_gateway`` call *and* above every assignment to the ``gateway``
local. ``deploy_gateway``'s abort handler reads ``locals().get("gateway")`` and hands its
gatewayId to ``cleanup_gateway_resources``, which DELETES it. A refusal raised after that
assignment would destroy the very gateway it exists to protect — turning "your deploy
failed" into "the stranger's gateway is gone". That ordering is asserted below, not just
documented.

ARCC ``cnt_GURZvDLm6pRn1K`` ("Prevent S3 Bucket Sniping") states the exit criterion
verbatim for a globally-unique name: *before performing actions, ensure ownership has
not changed*. Related: ``cnt_1vtvHlE7JwCaFm`` and ``cnt_vBC0kXE8PNHqrW``.
"""

import logging

import pytest
from app.services import gateway_deployer
from app.services.resource_ownership import ForeignResourceError, owner_tags

REGION = "us-east-1"
ACCOUNT = "123456789012"

SHARED_POOL = "us-east-1_SHAREDPOOL"
SHARED_DOMAIN = "acf-test-gw-0123456789"
OWN_POOL = "us-east-1_OWNPOOL00"
FOREIGN_POOL = "us-east-1_FOREIGN00"

GW_NAME = "omargw"
GW_ID = "omargw-w0ayuewvtr"
GW_URL = f"https://{GW_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"

OUR_ROLE = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreGateway-{GW_NAME}"
# Same role NAME, different account. A suffix or `in` compare would accept it.
LOOKALIKE_ROLE = f"arn:aws:iam::999988887777:role/AgentCoreGateway-{GW_NAME}"
FOREIGN_ROLE = f"arn:aws:iam::{ACCOUNT}:role/SomebodyElsesGatewayRole"

OLD_CLIENT = "staleclient00000"
NEW_CLIENT = "freshclient00000"
# Must never appear in a refusal message: it travels into an SFN failure Cause the UI
# renders (CodeQL py/clear-text-logging-sensitive-data).
SECRET = "notarealclientsecret-0000000000"  # pragma: allowlist secret


def _cognito_auth(pool_id: str, clients: list[str]) -> dict:
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
            "allowedClients": list(clients),
        }
    }


# A real external IdP, as the non-Cognito identity_config path produces. There is no
# pool id anywhere in it, which is why proof 2 cannot speak for this gateway.
EXTERNAL_AUTH = {
    "customJWTAuthorizer": {
        "discoveryUrl": "https://login.example.com/.well-known/openid-configuration",
        "allowedClients": ["externalclient0"],
    }
}


class _FakeCognito:
    """Models pool TAGS, because that is what ``classify_user_pool`` keys on."""

    def __init__(self, *, owned_pools: tuple[str, ...] = (OWN_POOL,)):
        self.owned_pools = owned_pools
        self.describe_calls: list[str] = []
        self.calls: list[str] = []
        self.deleted_clients: list[tuple[str, str]] = []
        self.deleted_pools: list[str] = []
        self.deleted_domains: list[str] = []

    def describe_user_pool(self, **kw):
        pid = kw["UserPoolId"]
        self.describe_calls.append(pid)
        self.calls.append(f"describe_user_pool:{pid}")
        pool: dict = {} if self.deleted_domains else {"Domain": SHARED_DOMAIN}
        pool["UserPoolTags"] = dict(owner_tags(REGION)) if pid in self.owned_pools else {}
        return {"UserPool": pool}

    def delete_user_pool_client(self, **kw):
        self.calls.append(f"delete_user_pool_client:{kw['ClientId']}")
        self.deleted_clients.append((kw["UserPoolId"], kw["ClientId"]))
        return {}

    def delete_user_pool_domain(self, **kw):
        self.calls.append("delete_user_pool_domain")
        self.deleted_domains.append(kw["Domain"])
        return {}

    def delete_user_pool(self, **kw):
        self.calls.append(f"delete_user_pool:{kw['UserPoolId']}")
        self.deleted_pools.append(kw["UserPoolId"])
        return {}


@pytest.fixture
def shared_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", SHARED_POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", SHARED_DOMAIN)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_a_foreign_gateway_is_refused(shared_env):
    """The defect, at its smallest. A pool we cannot prove we own means hands off."""
    cog = _FakeCognito(owned_pools=())
    with pytest.raises(ForeignResourceError) as excinfo:
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME,
            _cognito_auth(FOREIGN_POOL, [OLD_CLIENT]),
            existing_role_arn=FOREIGN_ROLE,
            owned_role_arn=OUR_ROLE,
            cognito_client=cog,
        )
    msg = str(excinfo.value)
    assert GW_NAME in msg
    assert FOREIGN_POOL in msg, "the operator's only lead on whose gateway this is"
    assert "account-global" in msg
    assert SECRET not in msg


def test_the_platforms_shared_pool_proves_ownership(shared_env):
    """The common redeploy: every gateway on the shared-pool path takes this route."""
    cog = _FakeCognito(owned_pools=())
    proof = gateway_deployer.assert_gateway_is_adoptable(
        GW_NAME,
        _cognito_auth(SHARED_POOL, [OLD_CLIENT]),
        existing_role_arn=FOREIGN_ROLE,
        owned_role_arn=OUR_ROLE,
        cognito_client=cog,
    )
    assert proof == f"AUTHORIZER_POOL:{gateway_deployer.POOL_SHARED_EXACT}"


def test_a_pool_this_stack_owner_tagged_proves_ownership(shared_env):
    """The per-gateway-pool path, whose pools carry ``owner_tags`` at creation."""
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    proof = gateway_deployer.assert_gateway_is_adoptable(
        GW_NAME,
        _cognito_auth(OWN_POOL, [OLD_CLIENT]),
        existing_role_arn=FOREIGN_ROLE,
        owned_role_arn=OUR_ROLE,
        cognito_client=cog,
    )
    assert proof == f"AUTHORIZER_POOL:{gateway_deployer.POOL_OWNED_BY_STACK}"


def test_an_external_oauth_redeploy_is_not_refused(shared_env):
    """The way a too-narrow fix breaks a supported feature.

    A gateway deployed with an external IdP (``identity_config.provider != "cognito"``)
    has no Cognito pool in its authorizer at all, so the pool proof can never speak for
    it. With only that proof, EVERY redeploy of an external-OAuth gateway would be
    refused as foreign — a fix that removes a feature. The role proof carries it.
    """
    cog = _FakeCognito(owned_pools=())
    proof = gateway_deployer.assert_gateway_is_adoptable(
        GW_NAME,
        EXTERNAL_AUTH,
        existing_role_arn=OUR_ROLE,
        owned_role_arn=OUR_ROLE,
        cognito_client=cog,
    )
    assert proof == "GATEWAY_ROLE"
    assert cog.describe_calls == [], "the role proof needs no Cognito call at all"


def test_an_external_oauth_gateway_on_a_role_that_is_not_ours_is_refused(shared_env):
    """The other half of the above: an external authorizer is not a free pass."""
    cog = _FakeCognito(owned_pools=())
    with pytest.raises(ForeignResourceError) as excinfo:
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME,
            EXTERNAL_AUTH,
            existing_role_arn=FOREIGN_ROLE,
            owned_role_arn=OUR_ROLE,
            cognito_client=cog,
        )
    assert "no Cognito sign-in configured" in str(excinfo.value)


def test_a_role_arn_from_another_account_does_not_match(shared_env):
    """Exact ARN compare, not the role name. Our naming scheme is public — the live
    account already holds ``AgentCoreGateway-omargw`` for a gateway we did not create —
    so a suffix or substring compare on the name would accept a stranger's role, and a
    cross-account reference makes that reachable."""
    cog = _FakeCognito(owned_pools=())
    with pytest.raises(ForeignResourceError):
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME,
            EXTERNAL_AUTH,
            existing_role_arn=LOOKALIKE_ROLE,
            owned_role_arn=OUR_ROLE,
            cognito_client=cog,
        )


def test_two_unknown_role_arns_do_not_prove_each_other(shared_env):
    """``"" == ""`` is the trap. ``get_gateway`` can omit ``roleArn``, and a caller that
    has not resolved its own role yet passes ``""`` too — an equality check written
    without the truthiness guard would then report GATEWAY_ROLE for a gateway about
    which nothing whatsoever is known."""
    cog = _FakeCognito(owned_pools=())
    with pytest.raises(ForeignResourceError):
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME,
            _cognito_auth(FOREIGN_POOL, [OLD_CLIENT]),
            existing_role_arn="",
            owned_role_arn="",
            cognito_client=cog,
        )


def test_a_malformed_discovery_url_is_refused(shared_env):
    """``cognito-idp.us-east-1.amazonaws.com.evil.test`` is not Cognito. The host is
    validated exactly by ``_pool_id_from_authorizer``; this pins that the gate inherits
    that validation rather than doing its own substring match."""
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    spoofed = {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                f"https://cognito-idp.{REGION}.amazonaws.com.evil.test/{OWN_POOL}/.well-known/openid-configuration"
            ),
            "allowedClients": [OLD_CLIENT],
        }
    }
    with pytest.raises(ForeignResourceError):
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME, spoofed, existing_role_arn=FOREIGN_ROLE, owned_role_arn=OUR_ROLE, cognito_client=cog
        )


def test_an_empty_authorizer_is_refused(shared_env):
    """A gateway whose authorizerConfiguration could not be read is not ours by
    default. Unreadable must fail SAFE, the same direction ``classify_user_pool`` fails."""
    cog = _FakeCognito(owned_pools=(OWN_POOL,))
    with pytest.raises(ForeignResourceError):
        gateway_deployer.assert_gateway_is_adoptable(
            GW_NAME, {}, existing_role_arn=FOREIGN_ROLE, owned_role_arn=OUR_ROLE, cognito_client=cog
        )


# ---------------------------------------------------------------------------
# Through deploy_gateway, which is where the ordering matters
# ---------------------------------------------------------------------------


class _FakeCtrl:
    """Control plane whose ``create_gateway`` always conflicts, as a real name
    collision does. Shares one ``calls`` journal with the Cognito double so ordering
    assertions read a single interleaved sequence."""

    def __init__(self, *, existing_auth: dict, existing_role_arn: str):
        self.existing_auth = existing_auth
        self.existing_role_arn = existing_role_arn
        self.calls: list[str] = []
        self.updated_authorizers: list[dict] = []
        self.deleted_gateways: list[str] = []

    def create_gateway(self, **kw):
        self.calls.append("create_gateway")
        raise _conflict()

    def list_gateways(self, **kw):
        return {"items": [{"name": GW_NAME, "gatewayId": GW_ID, "status": "READY"}]}

    def get_gateway(self, **kw):
        self.calls.append("get_gateway")
        return {
            "name": GW_NAME,
            "gatewayId": GW_ID,
            "gatewayUrl": GW_URL,
            "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{GW_ID}",
            "roleArn": self.existing_role_arn,
            "protocolType": "MCP",
            "status": "READY",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": self.existing_auth,
        }

    def update_gateway(self, **kw):
        self.calls.append("update_gateway")
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


def _conflict() -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ConflictException", "Message": f"Gateway {GW_NAME} already exists"}},
        "CreateGateway",
    )


class _FakeIamExceptions:
    class EntityAlreadyExistsException(Exception):
        pass


class _FakeIam:
    exceptions = _FakeIamExceptions()

    def __init__(self):
        self.calls: list[str] = []

    def create_role(self, **kw):
        self.calls.append("create_role")
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{kw['RoleName']}"}}

    def put_role_policy(self, **kw):
        self.calls.append("put_role_policy")
        return {}


class _FakeSts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


OWNER = "sub-owner-a"


def _no_consumers(gateway_id, pool_id):
    return []


#: What gateway_step passes: this deploy's owner and the live-consumer read (F-63).
ADOPT = {"owner_sub": OWNER, "gateway_consumers": _no_consumers}


def _install(monkeypatch, *, ctrl, cog):
    cog.calls = ctrl.calls
    cleanup_partials: list[dict] = []
    iam = _FakeIam()
    iam.calls = ctrl.calls

    def _fake_oauth(*a, **kw):
        ctrl.calls.append("create_cognito_oauth")
        return {
            "authorizer_config": _cognito_auth(SHARED_POOL, [NEW_CLIENT]),
            "client_info": {
                "provider": "cognito",
                "user_pool_id": SHARED_POOL,
                "client_id": NEW_CLIENT,
                "client_secret": SECRET,
                "token_endpoint": f"https://{SHARED_DOMAIN}.auth.{REGION}.amazoncognito.com/oauth2/token",
                "shared_pool": True,
                "scope": f"agentcore-{GW_NAME}/gateway.access",
            },
            "resource_server_created": False,
        }

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: cog)
    # The shared pool is a platform-account resource (F-42); a dedicated pool never reaches this.
    monkeypatch.setattr(gateway_deployer, "_create_platform_cognito_client", lambda region: cog)
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: iam)
    monkeypatch.setattr(gateway_deployer, "_create_cognito_oauth", _fake_oauth)
    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway",
        lambda *a, **kw: {"gatewayUrl": GW_URL, "roleArn": OUR_ROLE, "status": "READY"},
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

    def _fake_cleanup(name, region, partial, **kw):
        cleanup_partials.append(dict(partial))
        return []

    monkeypatch.setattr(gateway_deployer, "cleanup_gateway_resources", _fake_cleanup)
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(gateway_deployer.boto3, "client", lambda *a, **kw: _FakeSts())
    return cleanup_partials


def test_a_foreign_gateway_survives_the_refusal_untouched(shared_env, monkeypatch):
    """The whole point, end to end, and the part that could go wrong in the fix itself.

    Three things must all hold: the deploy fails, the foreign gateway is NOT repointed,
    and it is NOT deleted. The third is the one a careless implementation gets wrong —
    the abort handler reads ``locals().get("gateway")``, so raising one line lower down
    would hand the stranger's gatewayId to ``cleanup_gateway_resources``.
    """
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(FOREIGN_POOL, [OLD_CLIENT]), existing_role_arn=FOREIGN_ROLE)
    cog = _FakeCognito(owned_pools=())
    partials = _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-foreign", **ADOPT)

    # deploy_gateway reports failure as a dict rather than raising (its caller reads
    # `success` and fails the step), so this asserts on the contract the step handler
    # actually consumes.
    assert out["success"] is False
    assert ctrl.updated_authorizers == [], "a stranger's gateway was repointed at our authorizer"
    assert ctrl.deleted_gateways == [], "the refusal DELETED the gateway it was protecting"
    assert "update_gateway" not in ctrl.calls
    assert FOREIGN_POOL not in cog.deleted_pools
    assert cog.deleted_clients == []
    # And the abort handler was told nothing about a gateway, because there was none of
    # ours to release.
    assert all(not p.get("gateway_id") for p in partials), f"partial named a gateway: {partials}"
    assert GW_NAME in out["error"]
    assert FOREIGN_POOL in out["error"]
    assert SECRET not in out["error"], "the error travels into an SFN failure Cause the UI renders"


def test_our_own_gateway_is_still_adopted_on_redeploy(shared_env, monkeypatch):
    """The regression the gate could cause: a redeploy is the normal case, and it must
    still repoint, still retire the stale client, and still return the gateway."""
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=FOREIGN_ROLE)
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-ours", **ADOPT)

    assert out["gateway_id"] == GW_ID
    assert out["gateway_created_by_deployment"] is False
    assert out["gateway_role_created_by_deployment"] is True
    assert ctrl.updated_authorizers == [_cognito_auth(SHARED_POOL, [NEW_CLIENT])]
    assert cog.deleted_clients == [(SHARED_POOL, OLD_CLIENT)]


def test_empty_tool_recovery_never_deletes_an_adopted_gateway(
    shared_env,
    monkeypatch,
):
    """The retry loop is a second deletion lane, separate from abort cleanup.

    A valid tools/list response with too few tools may justify recreating a
    gateway created by this invocation. It never authorizes deleting a gateway
    that merely passed the redeploy ownership proof.
    """
    ctrl = _FakeCtrl(
        existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]),
        existing_role_arn=FOREIGN_ROLE,
    )
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)
    monkeypatch.setattr(
        gateway_deployer,
        "_resolve_gateway_tool_actions",
        lambda *a, **kw: (["DynamicTools___search"], 1),
    )

    probes: list[int] = []

    def _empty_live_plane(*args, probe=None, **kwargs):
        if probe is not None:
            probe["got_valid_response"] = True
        probes.append(0)
        return 0

    monkeypatch.setattr(
        gateway_deployer,
        "_wait_for_gateway_to_serve_tools",
        _empty_live_plane,
    )

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME},
        REGION,
        deployment_id="d-adopted-empty-plane",
        **ADOPT,
    )

    assert out["success"] is False
    assert out["gateway_created_by_deployment"] is False
    assert ctrl.deleted_gateways == []
    assert "Refusing to delete an adopted gateway" in out["error"]
    # Redeploy audit 2026-09-28 row 4: the adopted gateway gets the same probe budget a
    # created one gets through recreation (1 + 2 rounds), then the refusal stands.
    assert len(probes) == 1 + gateway_deployer._ADOPTED_REPROBE_ATTEMPTS == 3
    assert "after 3 probe rounds" in out["error"]


def test_an_adopted_gateway_whose_plane_converges_on_reprobe_succeeds(shared_env, monkeypatch):
    """Redeploy audit 2026-09-28 row 4. On a redeploy the just-updated target can still be
    converging when the first 90 s probe ends (UPDATING -> READY, provider repoint, a fresh
    Cognito domain). A created gateway got two more probes via recreation; the adopted one
    failed on the first. It must now re-probe in place and, once the plane serves, take the
    same success path — without ever touching delete_gateway."""
    ctrl = _FakeCtrl(
        existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]),
        existing_role_arn=FOREIGN_ROLE,
    )
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)
    monkeypatch.setattr(
        gateway_deployer,
        "_resolve_gateway_tool_actions",
        lambda *a, **kw: (["DynamicTools___search"], 1),
    )
    sequence = iter([0, 0, 1])
    probes: list[int] = []
    pauses: list[float] = []
    monkeypatch.setattr(gateway_deployer.time, "sleep", lambda secs: pauses.append(secs))

    def _converging_plane(*args, probe=None, **kwargs):
        if probe is not None:
            probe["got_valid_response"] = True
        served = next(sequence)
        probes.append(served)
        return served

    monkeypatch.setattr(gateway_deployer, "_wait_for_gateway_to_serve_tools", _converging_plane)

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME},
        REGION,
        deployment_id="d-adopted-converging-plane",
        **ADOPT,
    )

    assert out["success"] is True, out.get("error")
    assert out["gateway_created_by_deployment"] is False
    assert out["tool_plane_verified"] is True
    assert probes == [0, 0, 1]
    assert ctrl.deleted_gateways == []
    assert gateway_deployer._ADOPTED_REPROBE_PAUSE_SECONDS in pauses


def test_an_adopted_gateway_that_converges_on_the_last_round_is_not_refused(shared_env, monkeypatch):
    """The bound is inclusive: the last permitted round counts."""
    ctrl = _FakeCtrl(
        existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]),
        existing_role_arn=FOREIGN_ROLE,
    )
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)
    monkeypatch.setattr(
        gateway_deployer,
        "_resolve_gateway_tool_actions",
        lambda *a, **kw: (["DynamicTools___a", "DynamicTools___b"], 2),
    )
    sequence = iter([1, 1, 2])

    def _plane(*args, probe=None, **kwargs):
        if probe is not None:
            probe["got_valid_response"] = True
        return next(sequence)

    monkeypatch.setattr(gateway_deployer, "_wait_for_gateway_to_serve_tools", _plane)

    out = gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-last-round", **ADOPT)

    assert out["success"] is True, out.get("error")
    assert out["tool_plane_verified"] is True
    assert ctrl.deleted_gateways == []


def test_the_adoption_is_recorded_at_a_level_lambda_emits(shared_env, monkeypatch, caplog):
    """Adopting a resource this deploy did not create is a security-relevant decision,
    so the record must say which proof allowed it — ARCC ``cnt_6yTkcrHkEBKA7u`` wants
    the *success* of such an attempt logged, not only the failure. The whole backend's
    INFO was dead in Lambda until ``app.logging_config`` set the ``app`` package level;
    this asserts the record exists and carries the proof, not merely that it is INFO."""
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=FOREIGN_ROLE)
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    with caplog.at_level(logging.INFO, logger="app.services.gateway_deployer"):
        gateway_deployer.deploy_gateway({"name": GW_NAME}, REGION, deployment_id="d-log", **ADOPT)

    hits = [r for r in caplog.records if "ownership proof" in r.getMessage()]
    assert hits, "an adoption left no record of why it was permitted"
    assert any(gateway_deployer.POOL_SHARED_EXACT in r.getMessage() for r in hits)
    assert all(SECRET not in r.getMessage() for r in hits)


# ---------------------------------------------------------------------------
# F-63: live consumers and tenancy, through deploy_gateway
# ---------------------------------------------------------------------------

#: Every call that changes something: a refusal must precede all of them.
_MUTATIONS = {"create_cognito_oauth", "create_role", "put_role_policy", "create_gateway", "update_gateway"}


def test_a_same_owner_live_consumer_keeps_its_client_through_the_deploy(shared_env, monkeypatch):
    """The happy path the refusals below are measured against: the previous version's
    runtime holds OLD_CLIENT, so the adopted gateway allows both and OLD is not retired."""
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=FOREIGN_ROLE)
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)
    seen: list[tuple[str, str]] = []

    def _consumers(gid, pool):
        seen.append((gid, pool))
        return [{"deployment_id": "d-v1", "owner_sub": OWNER, "client_ids": [OLD_CLIENT]}]

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME}, REGION, deployment_id="d-v2", owner_sub=OWNER, gateway_consumers=_consumers
    )

    assert out["success"] is True, out.get("error")
    assert ctrl.updated_authorizers == [_cognito_auth(SHARED_POOL, [NEW_CLIENT, OLD_CLIENT])]
    assert cog.deleted_clients == [], "the live previous version's client was retired"
    assert seen == [(GW_ID, SHARED_POOL)] * 2, "read at the pre-flight and again above the update"


@pytest.mark.parametrize(
    "consumers",
    [
        pytest.param([{"deployment_id": "d-a", "owner_sub": "sub-owner-b", "client_ids": [OLD_CLIENT]}], id="foreign"),
        pytest.param([{"deployment_id": "d-a", "owner_sub": "", "client_ids": [OLD_CLIENT]}], id="ownerless row"),
        pytest.param("raise", id="read failure"),
        pytest.param(None, id="no callback (the legacy executor)"),
    ],
)
def test_a_refused_adoption_mutates_nothing_at_all(shared_env, monkeypatch, consumers):
    """Codex F-63 review items 6 and 7: refused BEFORE a client is minted, the existing
    gateway role's policy is re-put, or the shared resource server is touched."""
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=OUR_ROLE)
    cog = _FakeCognito(owned_pools=())
    partials = _install(monkeypatch, ctrl=ctrl, cog=cog)

    def _read(gid, pool):
        if consumers == "raise":
            raise RuntimeError("ProvisionedThroughputExceededException")
        return consumers

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME},
        REGION,
        deployment_id="d-b",
        owner_sub=OWNER,
        gateway_consumers=None if consumers is None else _read,
    )

    assert out["success"] is False
    assert _MUTATIONS.isdisjoint(ctrl.calls), ctrl.calls
    assert cog.deleted_clients == [] and cog.deleted_pools == [] and cog.deleted_domains == []
    assert ctrl.deleted_gateways == []
    assert partials == [], "nothing was created, so the abort has nothing to release"
    if consumers != "raise" and consumers is not None:
        assert "do not own" in out["error"]
        assert "sub-owner-b" not in out["error"] and "d-a" not in out["error"]


def test_a_missing_owner_is_refused_before_anything_is_created(shared_env, monkeypatch):
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=OUR_ROLE)
    cog = _FakeCognito(owned_pools=())
    _install(monkeypatch, ctrl=ctrl, cog=cog)

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME}, REGION, deployment_id="d-b", owner_sub="", gateway_consumers=_no_consumers
    )

    assert out["success"] is False
    assert _MUTATIONS.isdisjoint(ctrl.calls), ctrl.calls


def test_a_consumer_that_appears_after_the_preflight_is_still_refused(shared_env, monkeypatch):
    """The race the second read exists for. By then this deploy HAS minted a client,
    so the abort must release it -- and must not delete the resource server it reused,
    which is keyed on the gateway name and so belongs to the other deployment too."""
    ctrl = _FakeCtrl(existing_auth=_cognito_auth(SHARED_POOL, [OLD_CLIENT]), existing_role_arn=OUR_ROLE)
    cog = _FakeCognito(owned_pools=())
    partials = _install(monkeypatch, ctrl=ctrl, cog=cog)
    reads = iter([[], [{"deployment_id": "d-a", "owner_sub": "sub-owner-b", "client_ids": [OLD_CLIENT]}]])

    out = gateway_deployer.deploy_gateway(
        {"name": GW_NAME},
        REGION,
        deployment_id="d-b",
        owner_sub=OWNER,
        gateway_consumers=lambda gid, pool: next(reads),
    )

    assert out["success"] is False
    assert "update_gateway" not in ctrl.calls
    assert cog.deleted_clients == []
    [partial] = partials
    assert partial["gateway_id"] is None, "the existing gateway must never reach the abort"
    assert partial["client_info"]["client_id"] == NEW_CLIENT
    assert partial["resource_server_created_by_deployment"] is False
