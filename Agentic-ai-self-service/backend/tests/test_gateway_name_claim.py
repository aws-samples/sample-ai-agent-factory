"""The gateway-name claim: one conditional write, taken before any Cognito or IAM call.

Two layers. The table semantics run against moto's DynamoDB, so the condition
expressions are evaluated by a DynamoDB implementation, not by a fake written to agree
with them. The race runs two real ``deploy_gateway`` calls released by one barrier, and
counts what each made before Step 1: exactly one may reach Cognito.
"""

from __future__ import annotations

import threading

import boto3
import pytest
from app.services import gateway_deployer
from app.services import gateway_name_claim as gnc
from moto import mock_aws

# Every test here runs against moto's table, never the in-memory default.
pytestmark = pytest.mark.real_gateway_lock

REGION = "us-east-1"
ACCOUNT = "111122223333"
OWNER_A = "owner-a"
OWNER_B = "owner-b"
NAME = "shared-gw"


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName="claims",
            KeySchema=[{"AttributeName": "claim_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "claim_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv(gnc.CLAIM_TABLE_ENV, "claims")
        monkeypatch.setenv("APP_AWS_REGION", REGION)
        yield t


def _acquire(claims, *, owner=OWNER_A, dep="d-1", name=NAME, account=ACCOUNT, region=REGION, token=None):
    """One invocation of deployment *dep*: its token is its own unless *token* says."""
    return claims.acquire(
        account=account, region=region, name=name, owner_sub=owner, deployment_id=dep, token=token or f"tok-{dep}"
    )


def _where(name=NAME, account=ACCOUNT, region=REGION) -> dict:
    return {"account": account, "region": region, "name": name}


# ---------------------------------------------------------------------------
# Table semantics
# ---------------------------------------------------------------------------


def _durable(claims, *, owner=OWNER_A, dep="d-1", name=NAME, account=ACCOUNT, region=REGION):
    """What a deploy that recorded its gateway leaves: a claim promoted to durable."""
    assert _acquire(claims, owner=owner, dep=dep, name=name, account=account, region=region) is True
    assert claims.promote(**_where(name, account, region), owner_sub=owner, token=f"tok-{dep}") is True


def _free(table, name=NAME, account=ACCOUNT, region=REGION) -> bool:
    """No claim, or a provisional one given up: either way anyone may take it now."""
    item = table.get_item(Key={"claim_key": gnc.claim_key(account, region, name)}).get("Item")
    return item is None or (item.get("provisional") is True and int(item["holder_expires_at"]) == 0)


def _claim_item(table, name=NAME) -> dict:
    return table.get_item(Key={"claim_key": gnc.claim_key(ACCOUNT, REGION, name)}).get("Item") or {}


def test_a_fresh_name_is_claimed_provisionally_and_the_invocation_can_retake_it(table):
    claims = gnc.GatewayNameClaims(table)
    assert _acquire(claims) is True
    # deploy_gateway's own retry recursion calls it again, in the same invocation.
    assert _acquire(claims) is True
    item = _claim_item(table)
    assert item["owner_sub"] == OWNER_A and item["holder_deployment_id"] == "d-1"
    assert item["holder_token"] == "tok-d-1" and item["provisional"] is True
    assert int(item["gc_after"]) == int(item["holder_expires_at"]) + gnc.GC_GRACE_SECONDS


def test_another_owner_is_refused_while_the_lease_is_held(table):
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another owner"):
        _acquire(claims, owner=OWNER_B, dep="d-b")


def test_a_second_invocation_of_the_same_deployment_is_refused_while_the_first_holds_it(table):
    """F-66f/5d: a Step Functions retry or a duplicate delivery reuses the deployment
    id while the first invocation may still be running. The fence is the token."""
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _acquire(claims, dep="d-1", token="tok-first")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another of your deployments"):
        _acquire(claims, dep="d-1", token="tok-retry")
    _durable(claims, dep="d-2", name="durable-gw")
    _acquire(claims, dep="d-2", name="durable-gw", token="tok-live")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another of your deployments"):
        _acquire(claims, dep="d-2", name="durable-gw", token="tok-retry")
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert _acquire(claims, dep="d-1", token="tok-retry") is True
    assert _acquire(claims, dep="d-2", name="durable-gw", token="tok-retry") is False


def test_a_replaced_token_can_never_settle_the_claim_that_replaced_it(table):
    """5d(b): after token B replaces expired token A, neither A.promote nor A.abandon
    (nor A.release, nor A's erase exemption) touches B's claim."""
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _acquire(claims, dep="d-1", token="tok-a")
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert _acquire(claims, dep="d-1", token="tok-b") is True
    before = _claim_item(table)
    for op in (claims.promote, claims.abandon):
        assert op(**_where(), owner_sub=OWNER_A, token="tok-a") is False
    assert claims.release(**_where(), token="tok-a") is False
    assert claims.erase(**_where(), owner_sub=OWNER_A, token="tok-a") is False
    assert _claim_item(table) == before
    # The same holds for a durable claim B re-took.
    claims.promote(**_where(), owner_sub=OWNER_A, token="tok-b")
    assert _acquire(claims, dep="d-1", token="tok-c") is False
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert _acquire(claims, dep="d-1", token="tok-d") is False
    before = _claim_item(table)
    assert claims.release(**_where(), token="tok-c") is False
    assert claims.erase(**_where(), owner_sub=OWNER_A, token="tok-c") is False
    assert _claim_item(table) == before


def test_another_owner_presenting_a_live_token_cannot_take_the_claim(table):
    """A token fences one invocation's writes; it is not an ownership credential."""
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims, dep="d-1", token="shared-tok")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another owner right now"):
        _acquire(claims, owner=OWNER_B, dep="d-b", token="shared-tok")
    for op in (claims.promote, claims.abandon):
        assert op(**_where(), owner_sub=OWNER_B, token="shared-tok") is False
    assert _claim_item(table)["owner_sub"] == OWNER_A and _claim_item(table)["provisional"] is True


def test_a_stale_invocation_cannot_promote_after_its_lease_expires(table):
    """Nobody replaced it yet, but from its lease end on anyone may: it stays provisional."""
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _acquire(claims)
    clock.now += gnc.HOLDER_LEASE_SECONDS
    assert claims.promote(**_where(), owner_sub=OWNER_A, token="tok-d-1") is True
    _acquire(claims, name="late")
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert claims.promote(**_where("late"), owner_sub=OWNER_A, token="tok-d-1") is False
    assert _claim_item(table, "late")["provisional"] is True
    assert _acquire(claims, owner=OWNER_B, dep="d-b", name="late") is True


def test_an_expired_provisional_claim_is_anyones_at_once(table):
    """A hard-killed step, a lost response, a throttled cleanup: none can squat a name."""
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _acquire(claims)
    clock.now += gnc.HOLDER_LEASE_SECONDS
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another owner"):
        _acquire(claims, owner=OWNER_B, dep="d-b")
    clock.now += 1
    assert _acquire(claims, owner=OWNER_B, dep="d-b") is True
    assert _claim_item(table)["owner_sub"] == OWNER_B
    assert claims.promote(**_where(), owner_sub=OWNER_A, token="tok-d-1") is False


def test_a_promoted_owner_is_persistent_past_expiry(table):
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _durable(claims)
    item = _claim_item(table)
    assert not {"provisional", "gc_after", "holder_deployment_id", "holder_token", "holder_expires_at"} & set(item)
    clock.now += gnc.HOLDER_LEASE_SECONDS * 10
    with pytest.raises(gnc.GatewayNameClaimRefused, match="already in use by another owner"):
        _acquire(claims, owner=OWNER_B, dep="d-b")
    assert _claim_item(table)["owner_sub"] == OWNER_A


def test_promote_can_record_the_gateway_the_claim_is_the_only_handle_for(table):
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    recovery = {"recovery_gateway_id": "gw-1", "recovery_deployment_id": "d-1"}
    assert claims.promote(**_where(), owner_sub=OWNER_A, token="tok-d-1", recovery=recovery) is True
    item = _claim_item(table)
    assert "provisional" not in item and {k: item[k] for k in recovery} == recovery
    with pytest.raises(ValueError):
        claims.promote(**_where(), owner_sub=OWNER_A, token="t", recovery={"owner_sub": "x"})


def test_a_durable_claim_is_retaken_by_its_owner_and_stays_durable(table):
    claims = gnc.GatewayNameClaims(table)
    _durable(claims)
    assert _acquire(claims, dep="d-2") is False
    item = _claim_item(table)
    assert item["holder_deployment_id"] == "d-2" and "provisional" not in item
    assert claims.release(**_where(), token="tok-d-2") is True
    assert not {"holder_deployment_id", "holder_token", "holder_expires_at"} & set(_claim_item(table))


def test_a_second_deploy_of_the_same_owner_waits_for_the_lease(table):
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _durable(claims, dep="d-0")
    _acquire(claims, dep="d-1")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another of your deployments"):
        _acquire(claims, dep="d-2")
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert _acquire(claims, dep="d-2") is False
    assert _claim_item(table)["holder_deployment_id"] == "d-2"


def test_abandon_gives_a_provisional_claim_up_at_once(table):
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    assert claims.abandon(**_where(), owner_sub=OWNER_A, token="tok-d-1") is True
    assert _free(table)
    assert _acquire(claims, owner=OWNER_B, dep="d-b") is True


def test_abandon_and_promote_touch_only_their_own_provisional_claim(table):
    claims = gnc.GatewayNameClaims(table)
    _durable(claims)
    for op in (claims.abandon, claims.promote):
        assert op(**_where(), owner_sub=OWNER_A, token="tok-d-1") is False
    assert "provisional" not in _claim_item(table)
    _acquire(claims, name="other", dep="d-o")
    for owner, token in ((OWNER_B, "tok-d-o"), (OWNER_A, "tok-d-x")):
        for op in (claims.abandon, claims.promote):
            assert op(**_where("other"), owner_sub=owner, token=token) is False
    assert _claim_item(table, "other")["provisional"] is True


def test_release_never_frees_a_provisional_claim(table):
    """A provisional claim is settled by promote or abandon; a release that dropped its
    lease would leave an owner and no expiry."""
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    assert claims.release(**_where(), token="tok-d-1") is False
    assert _claim_item(table)["holder_token"] == "tok-d-1"


def test_release_by_an_invocation_that_does_not_hold_it_changes_nothing(table):
    claims = gnc.GatewayNameClaims(table)
    _durable(claims, dep="d-0")
    _acquire(claims, dep="d-1")
    assert claims.release(**_where(), token="tok-d-2") is False
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(claims, dep="d-2")


@pytest.mark.parametrize(
    "other",
    [
        {"account": "444455556666"},
        {"region": "us-west-2"},
        {"name": "other-gw"},
    ],
)
def test_account_region_and_name_are_separate_claims(table, other):
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    assert _acquire(claims, owner=OWNER_B, dep="d-b", **other) is True


def test_the_name_is_claimed_case_insensitively(table):
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims, name="Shared-GW")
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(claims, owner=OWNER_B, dep="d-b", name="shared-gw")


@pytest.mark.parametrize(
    "owner,dep,token", [("", "d-1", "t"), (OWNER_A, "", "t"), (OWNER_A, "d-1", "")], ids=["owner", "dep", "token"]
)
def test_an_empty_owner_deployment_or_token_is_refused_before_any_write(table, owner, dep, token):
    claims = gnc.GatewayNameClaims(table)
    with pytest.raises(gnc.GatewayNameClaimRefused):
        claims.acquire(**_where(), owner_sub=owner, deployment_id=dep, token=token)
    assert table.scan()["Items"] == []


def test_erase_is_refused_for_another_owner_and_under_a_live_lease(table):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _durable(claims, dep="d-0")
    _acquire(claims)
    assert claims.erase(**_where(), owner_sub=OWNER_A) is False
    claims.release(**_where(), token="tok-d-1")
    assert claims.erase(**_where(), owner_sub=OWNER_B) is False
    assert claims.erase(**_where(), owner_sub=OWNER_A) is True
    assert _acquire(claims, owner=OWNER_B, dep="d-b") is True


def test_erase_after_an_expired_lease_and_by_the_lease_holder(table):
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _durable(claims, dep="d-0")
    _acquire(claims)
    assert claims.erase(**_where(), owner_sub=OWNER_A, token="tok-d-1") is True
    _durable(claims, dep="d-0")
    _acquire(claims)
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    assert claims.erase(**_where(), owner_sub=OWNER_A) is True


def test_the_fake_agrees_with_dynamodb(table, gateway_lock_table):
    """The in-memory table the dispatcher and race tests use, against moto, over one
    script of every write: each outcome and each resulting item must be the same."""
    from decimal import Decimal

    from botocore.exceptions import ClientError

    clock = _Clock()
    lease = gnc.HOLDER_LEASE_SECONDS
    script = [
        ("acquire", OWNER_A, "a1", 0),
        ("acquire", OWNER_A, "a1", 0),
        ("acquire", OWNER_A, "a2", 0),
        ("acquire", OWNER_B, "b1", 0),
        ("release", OWNER_A, "a1", 0),
        ("promote", OWNER_B, "a1", 0),
        ("promote", OWNER_A, "a2", 0),
        ("abandon", OWNER_A, "a2", 0),
        ("acquire", OWNER_B, "b1", lease + 1),
        ("promote", OWNER_A, "a1", 0),
        ("abandon", OWNER_A, "a1", 0),
        ("abandon", OWNER_B, "b1", 0),
        ("acquire", OWNER_A, "a3", 0),
        ("recover", OWNER_A, "a3", 0),
        ("reclaim", OWNER_A, "a3", 0),
        ("abandon", OWNER_A, "a3", 0),
        ("acquire", OWNER_B, "b2", lease * 3),
        ("acquire", OWNER_A, "a4", 0),
        # A release naming another gateway keeps the evidence; one naming it clears it.
        ("release_other", OWNER_A, "a4", 0),
        ("acquire", OWNER_A, "a4", 0),
        ("release_recorded", OWNER_A, "a4", 0),
        ("reclaim", OWNER_A, "a3", 0),
        ("acquire", OWNER_A, "a4", 0),
        ("acquire", OWNER_A, "a5", 0),
        ("release", OWNER_A, "a5", 0),
        ("erase", OWNER_A, None, 0),
        ("erase", OWNER_A, "a5", 0),
        ("release", OWNER_A, "a4", 0),
        ("acquire", OWNER_A, "a5", lease + 1),
        ("erase", OWNER_B, None, 0),
        ("erase", OWNER_A, "a4", 0),
        ("erase", OWNER_A, "a5", 0),
        ("acquire", OWNER_B, "b3", 0),
        ("erase", OWNER_B, "b3", 0),
        # A token is a fence, not a credential: another owner presenting a live one
        # cannot take the claim over.
        ("acquire", OWNER_B, "b4", lease * 3),
        ("acquire", OWNER_A, "b4", 0),
        ("promote", OWNER_B, "b4", 0),
        ("erase", OWNER_B, None, 0),
        # A stale token: its lease expired, nobody replaced it, and it still cannot promote.
        ("acquire", OWNER_A, "a6", 0),
        ("promote", OWNER_A, "a6", lease + 1),
        ("abandon", OWNER_A, "a6", 0),
    ]

    def plain(v):
        return int(v) if isinstance(v, Decimal) or (isinstance(v, int) and not isinstance(v, bool)) else v

    def run(t, read):
        claims, out = gnc.GatewayNameClaims(t, clock=clock), []
        clock.now = 1_000_000.0
        for op, owner, tok, advance in script:
            clock.now += advance
            try:
                if op == "acquire":
                    got = claims.acquire(**_where(), owner_sub=owner, deployment_id=f"d-{tok}", token=tok)
                elif op == "release":
                    got = claims.release(**_where(), token=tok)
                elif op == "erase":
                    got = claims.erase(**_where(), owner_sub=owner, token=tok)
                elif op == "recover":
                    got = claims.promote(
                        **_where(),
                        owner_sub=owner,
                        token=tok,
                        recovery={"recovery_gateway_id": "gw-9", "recovery_deployment_id": f"d-{tok}"},
                    )
                elif op == "reclaim":
                    got = gnc.reclaim_recovery_pointer(deployment_id=f"d-{tok}", claims=claims)
                elif op.startswith("release_"):
                    gateways = ("gw-9", "gw-8") if op == "release_recorded" else ("gw-other",)
                    got = claims.release(**_where(), token=tok, recorded_gateways=gateways)
                else:
                    got = getattr(claims, op)(**_where(), owner_sub=owner, token=tok)
            except gnc.GatewayNameClaimRefused as exc:
                got = str(exc)
            except ClientError as exc:
                got = exc.response["Error"]["Code"]
            out.append((op, owner, tok, got, {k: plain(v) for k, v in (read() or {}).items()}))
        return out

    key = gnc.claim_key(ACCOUNT, REGION, NAME)
    faked = run(gateway_lock_table, lambda: gateway_lock_table.items.get(key))
    real = run(table, lambda: table.get_item(Key={"claim_key": key}).get("Item"))
    assert faked == real
    pointer = gnc.recovery_pointer_key("d-a3")
    assert gateway_lock_table.items[pointer] == table.get_item(Key={"claim_key": pointer})["Item"]
    assert table.get_item(Key={"claim_key": pointer})["Item"]["claim_keys"] == {key}
    # Evidence present at the first reclaim, cleared by the second.
    assert [got for op, _o, _t, got, _i in real if op == "reclaim"] == [gnc.POINTER_ACTIVE, gnc.POINTER_MARKED]
    assert (
        table.get_item(Key={"claim_key": pointer})["Item"]["gc_after"] == gateway_lock_table.items[pointer]["gc_after"]
    )
    cleared = [i for op, _o, _t, got, i in real if op == "release_recorded"]
    kept = [i for op, _o, _t, got, i in real if op == "release_other"]
    assert kept[0]["recovery_gateway_id"] == "gw-9" and "recovery_gateway_id" not in cleared[0]
    assert "recovery_deployment_id" not in cleared[0] and "holder_token" not in cleared[0]
    # And the script reaches every branch: both kinds of acquire, and every refusal.
    outcomes = {(op, str(got)[:40]) for op, _o, _t, got, _i in real}
    assert {("acquire", "True"), ("acquire", "False"), ("promote", "True"), ("abandon", "True")} <= outcomes
    assert {("release", "True"), ("erase", "True"), ("erase", "False"), ("recover", "True")} <= outcomes
    assert any(op == "acquire" and "already in use" in str(g) for op, _o, _t, g, _i in real)
    assert any(op == "acquire" and "another owner right now" in str(g) for op, _o, _t, g, _i in real)
    assert any(op == "acquire" and "another of your" in str(g) for op, _o, _t, g, _i in real)


def test_the_table_is_required(monkeypatch):
    monkeypatch.delenv(gnc.CLAIM_TABLE_ENV, raising=False)
    with pytest.raises(RuntimeError, match=gnc.CLAIM_TABLE_ENV):
        gnc.claims_from_env()


# ---------------------------------------------------------------------------
# The teardown hold (F-66f): taken before teardown decides anything
# ---------------------------------------------------------------------------


class _Sts:
    def __init__(self):
        self.calls = 0

    def get_caller_identity(self):
        self.calls += 1
        return {"Account": ACCOUNT}


class _GetGateway:
    """GetGateway by id: a name, or an error, and a count of reads."""

    def __init__(self, name=None, error=None):
        self.name, self.error, self.reads = name, error, []

    def __call__(self, region):
        self.reads.append(region)
        return self

    def get_gateway(self, gatewayIdentifier):  # noqa: N803
        if self.error:
            raise self.error
        return {"gatewayId": gatewayIdentifier, "name": self.name}


def _client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "arn:aws:secret-looking detail"}}, "GetGateway")


def _hold(table, rows, *, owner=OWNER_A, dep="d-down", ctrl=None, fallback=None, sts=None, clock=None, token=None):
    return gnc.hold_gateway_names_for_teardown(
        rows,
        owner_sub=owner,
        deployment_id=dep,
        default_account=None,
        default_region=REGION,
        sts_client_for=lambda: sts or _Sts(),
        ctrl_for=ctrl,
        fallback=fallback,
        claims=gnc.GatewayNameClaims(table, clock=clock or _Clock()) if table is not None else None,
        token=token,
    )


GW_ROW = {"type": "gateway", "id": "gw-1", "name": NAME, "region": REGION}


def test_the_hold_takes_the_lease_and_a_deploy_of_the_name_is_then_refused(table):
    clock = _Clock()
    hold = _hold(table, [GW_ROW], clock=clock)
    assert _claim_item(table)["holder_deployment_id"] == "d-down"
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another of your deployments"):
        _acquire(gnc.GatewayNameClaims(table, clock=clock), dep="d-new")
    hold.settle(erase=True)
    # Never proven ours, so the claim it created is given up, not kept.
    assert _free(table)
    assert _acquire(gnc.GatewayNameClaims(table, clock=clock), owner=OWNER_B, dep="d-b") is True


def test_a_deploy_holding_the_name_makes_the_teardown_refuse_with_nothing_held(table):
    _acquire(gnc.GatewayNameClaims(table, clock=_Clock()), dep="d-new")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="nothing was deleted"):
        _hold(table, [GW_ROW])
    assert _claim_item(table)["holder_deployment_id"] == "d-new"


def test_another_owners_claim_refuses_the_teardown(table):
    _durable(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="claimed by another owner"):
        _hold(table, [GW_ROW])
    assert _claim_item(table)["owner_sub"] == OWNER_B


def test_a_live_lease_of_the_same_deployment_blocks_its_teardown_until_it_expires(table):
    """Fenced by token, not deployment id: a gateway step of this deployment still
    running (or hard-killed before it settled) holds a lease no teardown takes over."""
    clock = _Clock()
    _acquire(gnc.GatewayNameClaims(table, clock=clock), dep="d-down", token="step-token")
    with pytest.raises(gnc.GatewayNameClaimRefused, match="held by another of your deployments"):
        _hold(table, [GW_ROW], clock=clock)
    assert _claim_item(table)["holder_token"] == "step-token"
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    _hold(table, [GW_ROW], clock=clock).settle(erase=True)
    assert _free(table)


def test_a_handed_over_token_lets_failure_cleanup_take_the_lease(table):
    """The failed step's own token, carried out through its Catch, is the one lease
    the cleanup may take over: the name never comes free in between."""
    clock = _Clock()
    _acquire(gnc.GatewayNameClaims(table, clock=clock), dep="d-down", token="step-token")
    hold = _hold(table, [GW_ROW], clock=clock, token="step-token")
    assert hold.held == [(ACCOUNT, REGION, NAME)]
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(gnc.GatewayNameClaims(table, clock=clock), owner=OWNER_B, dep="d-b")
    hold.settle(erase=True)
    assert _free(table)


def test_settling_without_erase_keeps_an_existing_owner_and_frees_the_lease(table):
    claims = gnc.GatewayNameClaims(table)
    _durable(claims, dep="d-deploy")
    _hold(table, [GW_ROW]).settle(erase=False)
    item = _claim_item(table)
    assert item["owner_sub"] == OWNER_A and "holder_deployment_id" not in item and "provisional" not in item
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another owner"):
        _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")


@pytest.mark.parametrize(
    "row, ctrl",
    [
        pytest.param(
            {"type": "gateway", "id": "foreign-gw", "region": REGION},
            _GetGateway(name="foreign-orders"),
            id="id-only-row-named-by-GetGateway",
        ),
        pytest.param(
            {"type": "gateway", "id": "foreign-gw", "name": "foreign-orders", "region": REGION},
            None,
            id="row-that-records-a-name",
        ),
    ],
)
def test_a_foreign_row_leaves_no_durable_claim_and_its_real_owner_deploys_at_once(table, row, ctrl):
    """A manifest row is not proof of ownership. A claim the hold CREATED from that
    row, kept, would put the manifest's owner on the name for good, and the name's
    real owner would be refused as "another owner"."""
    hold = _hold(table, [row], owner="manifest-owner", ctrl=ctrl)
    assert hold.held == [(ACCOUNT, REGION, "foreign-orders")]
    hold.settle(erase=False)
    assert _free(table, "foreign-orders")
    assert _acquire(gnc.GatewayNameClaims(table), owner="real-owner", name="foreign-orders", dep="d-real") is True


def _proof(result=None, live_name=NAME):
    """The dispatchers' ownership check: the live GetGateway detail, or its refusal."""
    seen = []

    def prove(region, gateway_id):
        seen.append((region, gateway_id))
        if isinstance(result, Exception):
            raise result
        return {"gatewayId": gateway_id, "name": live_name}

    prove.seen = seen
    return prove


def _bound(table, rows, *, prove, owner=OWNER_A, account=ACCOUNT, clock=None, token=None):
    return gnc.hold_gateway_names_for_teardown(
        rows,
        owner_sub=owner,
        deployment_id="d-down",
        default_account=account,
        default_region=REGION,
        sts_client_for=lambda: pytest.fail("the account is on record"),
        prove_owned=prove,
        claims=gnc.GatewayNameClaims(table, clock=clock or _Clock()),
        token=token,
    )


def test_a_legacy_gateway_proven_ours_keeps_its_new_claim_while_the_graph_stands(table):
    """A pre-claim deployment: the teardown creates the claim, proves the gateway is
    ours, then fails part-way (a coupled role or resource server survives). The claim
    is the one guard left on that graph, so it is kept and another owner is refused."""
    prove = _proof()
    hold = _bound(table, [GW_ROW], prove=prove)
    assert prove.seen == [(REGION, "gw-1")]
    hold.settle(erase=False)
    item = _claim_item(table)
    assert item["owner_sub"] == OWNER_A and not {"holder_deployment_id", "provisional"} & set(item)
    assert not [k for k in item if k.startswith("recovery_")], "a recorded gateway needs no recovery handle"
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another owner"):
        _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")
    # The retry finds the claim already there, finishes, and erases it.
    _bound(table, [GW_ROW], prove=_proof()).settle(erase=True)
    assert table.scan()["Items"] == []


def test_a_proven_gateway_no_durable_row_names_keeps_its_claim_only_while_it_stands(table):
    """38/a4: a gateway known only in memory. Gone: its claim could never be erased,
    so it is given up. Still standing: its name must not pass to another owner (whose
    deploy would adopt it), so the claim is kept and records the gateway."""
    gone = _bound(table, [GW_ROW], prove=_proof())
    gone.only_durable(set(), standing=set())
    gone.settle(erase=False)
    assert _free(table)

    standing = _bound(table, [GW_ROW], prove=_proof())
    standing.only_durable(set(), standing={"gw-1"})
    standing.settle(erase=False)
    item = _claim_item(table)
    assert "provisional" not in item and item["owner_sub"] == OWNER_A
    assert item["recovery_gateway_id"] == "gw-1" and item["recovery_deployment_id"] == "d-down"
    with pytest.raises(gnc.GatewayNameClaimRefused, match="already in use"):
        _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")


def test_standing_does_not_make_an_unproven_gateway_keep_a_claim(table):
    from app.services.resource_ownership import ResourceDeletionRefused

    hold = _bound(table, [GW_ROW], prove=_proof(ResourceDeletionRefused("not ours")), owner="manifest-owner")
    hold.only_durable(set(), standing={"gw-1"})
    hold.settle(erase=False)
    assert _free(table)


def test_a_gateway_the_proof_refuses_stays_provisional_and_is_given_up(table):
    from app.services.resource_ownership import ResourceDeletionRefused

    hold = _bound(table, [GW_ROW], prove=_proof(ResourceDeletionRefused("not ours")), owner="manifest-owner")
    hold.settle(erase=False)
    assert _free(table)
    assert _acquire(gnc.GatewayNameClaims(table), owner="real-owner", dep="d-real") is True


def test_an_existing_claim_is_bound_to_the_live_name_too(table):
    """An existing claim needs no promotion, but its row still has to name the gateway."""
    _durable(gnc.GatewayNameClaims(table), dep="d-deploy")
    prove = _proof()
    _bound(table, [GW_ROW], prove=prove).settle(erase=False)
    assert prove.seen == [(REGION, "gw-1")]
    assert _claim_item(table)["owner_sub"] == OWNER_A


@pytest.mark.parametrize("live_name", ["our-real-live-name", None, ""], ids=["another-name", "no-name", "empty"])
def test_an_owned_gateway_live_under_another_name_refuses_the_whole_hold(table, live_name):
    """Ownership proves the id, not the name. A row recording a victim's name would
    otherwise hold, and promote, a durable claim on the victim's name."""
    rows = [
        {"type": "gateway", "id": "owned-gw", "name": "someone-elses-name", "region": REGION},
        {**GW_ROW, "id": "gw-fine"},
    ]
    with pytest.raises(gnc.GatewayNameClaimRefused, match="nothing was deleted"):
        _bound(table, rows, prove=_proof(live_name=live_name), owner="manifest-owner")
    assert _free(table, "someone-elses-name") and _free(table)
    assert _acquire(gnc.GatewayNameClaims(table), owner="real-owner", name="someone-elses-name", dep="d-r") is True


def test_a_case_different_live_name_still_binds(table):
    """claim_key lower-cases; so does the binding."""
    hold = _bound(table, [GW_ROW], prove=_proof(live_name=NAME.upper()))
    hold.settle(erase=False)
    assert _claim_item(table)["owner_sub"] == OWNER_A and "provisional" not in _claim_item(table)


def test_a_row_recorded_in_another_account_refuses_the_hold(table):
    """The claim namespace is the deployment's target account: the one every proof and
    delete reads through. A row naming another account cannot pick the namespace."""
    victim = "999900001111"
    prove = _proof()
    with pytest.raises(gnc.GatewayNameClaimRefused, match="another account"):
        _bound(table, [{**GW_ROW, "account": victim}], prove=prove)
    assert table.scan()["Items"] == [] and prove.seen == []
    assert _acquire(gnc.GatewayNameClaims(table), owner="real-owner", account=victim, dep="d-r") is True


def test_a_row_recorded_in_the_target_account_is_fine(table):
    _bound(table, [{**GW_ROW, "account": ACCOUNT}], prove=_proof()).settle(erase=False)
    assert _claim_item(table)["owner_sub"] == OWNER_A


def test_a_gateway_whose_arn_is_in_another_account_refuses_the_hold(table):
    def prove(region, gateway_id):
        return {"name": NAME, "gatewayArn": f"arn:aws:bedrock-agentcore:{region}:999900001111:gateway/{gateway_id}"}

    with pytest.raises(gnc.GatewayNameClaimRefused, match="another account"):
        _bound(table, [GW_ROW], prove=prove)
    assert _free(table)


@pytest.mark.parametrize(
    "error", [_client_error("ThrottlingException"), TimeoutError("read")], ids=["throttled", "transport"]
)
def test_an_unreadable_ownership_refuses_the_hold(table, error):
    with pytest.raises(gnc.GatewayNameClaimRefused, match="could not be read") as info:
        _bound(table, [GW_ROW], prove=_proof(error))
    assert "secret-looking" not in str(info.value)
    assert _free(table)


def test_a_gateway_already_gone_leaves_its_claim_provisional(table):
    _bound(table, [GW_ROW], prove=_proof(_client_error("ResourceNotFoundException"))).settle(erase=False)
    assert _free(table)


def test_a_role_only_row_leaves_no_durable_claim_either(table, monkeypatch):
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    hold = _hold(table, [{"type": "iam_role", "name": f"AgentCoreGateway-{NAME}"}], owner="manifest-owner")
    assert [n for _a, _r, n in hold.held] == [NAME]
    hold.settle(erase=False)
    assert _free(table)


def test_an_erase_the_table_refuses_falls_back_to_a_release(table):
    clock = _Clock()
    hold = _hold(table, [GW_ROW], clock=clock)
    # Our lease expired under us and the owner's next deploy took the name.
    clock.now += gnc.HOLDER_LEASE_SECONDS + 1
    _acquire(gnc.GatewayNameClaims(table, clock=clock), dep="d-new")
    before = _claim_item(table)
    hold.settle(erase=True)
    assert _claim_item(table) == before


class _FailingWrites:
    """The moto table, with the first *n* writes matching *expr* failing like a throttle."""

    def __init__(self, table, *, expr: str, n: int = 1):
        self._table, self._expr, self.left = table, expr, n

    def _maybe_fail(self, text: str) -> None:
        if self.left and text.startswith(self._expr):
            self.left -= 1
            raise _client_error("ProvisionedThroughputExceededException")

    def update_item(self, **kw):
        self._maybe_fail(kw["UpdateExpression"])
        return self._table.update_item(**kw)

    def delete_item(self, **kw):
        self._maybe_fail("DELETE")
        return self._table.delete_item(**kw)


def test_a_lost_abandon_leaves_a_claim_that_expires_with_its_lease(table):
    """37's oracle: the settle write is lost, and nothing else is. The claim holds the
    name exactly to its lease end and not one second past it."""
    clock = _Clock()
    hold = _hold(_FailingWrites(table, expr="SET holder_expires_at = :gone"), [GW_ROW], clock=clock)
    hold.settle(erase=True)  # never raises
    assert _claim_item(table)["provisional"] is True
    clock.now = 1_000_900
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(gnc.GatewayNameClaims(table, clock=clock), owner="real-owner", dep="d-real")
    clock.now = 1_000_901
    assert _acquire(gnc.GatewayNameClaims(table, clock=clock), owner="real-owner", dep="d-real") is True


def test_a_lost_erase_keeps_the_owner_and_a_retry_erases(table):
    """A durable claim whose final erase is lost stays this owner's (no other owner
    gains the name) and the next teardown erases it: never a stuck claim."""
    clock = _Clock()
    _durable(gnc.GatewayNameClaims(table, clock=clock), dep="d-deploy")
    hold = _hold(_FailingWrites(table, expr="DELETE", n=1), [GW_ROW], clock=clock)
    hold.settle(erase=True)
    item = _claim_item(table)
    assert item["owner_sub"] == OWNER_A and "provisional" not in item
    _hold(table, [GW_ROW], clock=clock).settle(erase=True)
    assert table.scan()["Items"] == []


def test_a_refused_second_name_gives_up_the_claim_the_first_created_and_keeps_an_old_one(table):
    """Nothing was deleted, so a claim this take created would only squat the name."""
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _durable(claims, name="kept-gw", dep="d-old")
    _acquire(claims, name="busy-gw", dep="d-new")
    rows = [
        {**GW_ROW, "id": "gw-old", "name": "kept-gw"},
        {**GW_ROW, "id": "gw-fresh", "name": "fresh-gw"},
        {**GW_ROW, "id": "gw-busy", "name": "busy-gw"},
    ]
    with pytest.raises(gnc.GatewayNameClaimRefused, match="busy-gw"):
        _hold(table, rows)
    assert _free(table, "fresh-gw")
    old = _claim_item(table, "kept-gw")
    assert old["owner_sub"] == OWNER_A and not {"holder_deployment_id", "provisional"} & set(old)
    assert _claim_item(table, "busy-gw")["holder_deployment_id"] == "d-new"


def test_nothing_name_coupled_holds_nothing_and_calls_nothing(monkeypatch):
    monkeypatch.delenv(gnc.CLAIM_TABLE_ENV, raising=False)
    sts, ctrl = _Sts(), _GetGateway(error=AssertionError("read"))
    rows = [
        {"type": "agent_runtime", "id": "rt-1"},
        {"type": "secret", "id": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x"},
        {"type": "iam_role", "name": "AgentCoreRuntime-x"},
        {"type": "cognito_resource_server", "id": "other-rs"},
    ]
    hold = _hold(None, rows, sts=sts, ctrl=ctrl)
    assert hold.held == [] and sts.calls == 0 and ctrl.reads == []
    hold.settle(erase=True)


def test_no_owner_on_record_refuses_rather_than_holding_nothing(table):
    with pytest.raises(gnc.GatewayNameClaimRefused, match="no owner"):
        _hold(table, [GW_ROW], owner="")
    assert table.scan()["Items"] == []


# An id-only row: the shape recorded before rows carried the name, still in the table.
ID_ONLY = {"type": "gateway", "id": "gw-1", "region": "us-west-2"}


def _claim_item_in(table, region) -> dict:
    return table.get_item(Key={"claim_key": gnc.claim_key(ACCOUNT, region, NAME)}).get("Item") or {}


def test_an_id_only_row_takes_the_name_from_the_result_for_the_same_id(table):
    ctrl = _GetGateway(error=AssertionError("no read needed"))
    _hold(table, [ID_ONLY], ctrl=ctrl, fallback={"gateway_id": "gw-1", "gateway_name": NAME})
    assert _claim_item_in(table, "us-west-2")["holder_deployment_id"] == "d-down"
    assert ctrl.reads == []


def test_a_result_for_another_gateway_is_not_this_rows_name(table):
    ctrl = _GetGateway(name="real-name")
    hold = _hold(table, [ID_ONLY], ctrl=ctrl, fallback={"gateway_id": "gw-other", "gateway_name": NAME})
    assert hold.held == [(ACCOUNT, "us-west-2", "real-name")]
    assert ctrl.reads == ["us-west-2"], "GetGateway must read the row's region"


def test_an_id_only_row_is_named_by_get_gateway(table):
    hold = _hold(table, [ID_ONLY], ctrl=_GetGateway(name=NAME))
    assert hold.held == [(ACCOUNT, "us-west-2", NAME)]


def test_an_id_only_gateway_confirmed_gone_holds_nothing_by_itself(table):
    hold = _hold(table, [ID_ONLY], ctrl=_GetGateway(error=_client_error("ResourceNotFoundException")))
    assert hold.held == [] and table.scan()["Items"] == []


def test_a_gone_gateway_still_holds_the_name_its_resource_server_carries(table):
    # The resource server's row carries the shared pool's home region, not the
    # gateway's: the name is keyed where the deployment's gateway lives.
    rs = {"type": "cognito_resource_server", "id": f"agentcore-{NAME}", "region": REGION, "pool_id": "p"}
    hold = gnc.hold_gateway_names_for_teardown(
        [ID_ONLY, rs],
        owner_sub=OWNER_A,
        deployment_id="d-down",
        default_account=ACCOUNT,
        default_region="us-west-2",
        sts_client_for=lambda: pytest.fail("the account is on record"),
        ctrl_for=_GetGateway(error=_client_error("ResourceNotFoundException")),
        claims=gnc.GatewayNameClaims(table, clock=_Clock()),
    )
    assert hold.held == [(ACCOUNT, "us-west-2", NAME)]


@pytest.mark.parametrize(
    "error",
    [_client_error("AccessDeniedException"), _client_error("ThrottlingException"), TimeoutError("read timeout")],
    ids=["access-denied", "throttled", "transport"],
)
def test_an_unreadable_id_only_gateway_fails_closed_with_nothing_written(table, error):
    with pytest.raises(gnc.GatewayNameClaimRefused, match="nothing was deleted") as exc:
        _hold(table, [ID_ONLY], ctrl=_GetGateway(error=error))
    assert "secret-looking" not in str(exc.value) and "read timeout" not in str(exc.value)
    assert table.scan()["Items"] == []


def test_a_gateway_that_reports_no_name_fails_closed(table):
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _hold(table, [ID_ONLY], ctrl=_GetGateway(name=""))
    assert table.scan()["Items"] == []


def test_an_id_only_row_with_no_way_to_read_it_fails_closed(table):
    with pytest.raises(gnc.GatewayNameClaimRefused, match="records no name"):
        _hold(table, [ID_ONLY], ctrl=None)


@pytest.mark.parametrize(
    "role, region, expected",
    [
        (f"AgentCoreGateway-{NAME}", REGION, [NAME]),
        (f"AgentCoreGateway-{NAME}-us-west-2", "us-west-2", [NAME]),
    ],
    ids=["home-region", "suffixed-region"],
)
def test_a_gateway_role_alone_holds_the_name_it_spells(table, monkeypatch, role, region, expected):
    monkeypatch.setenv("APP_AWS_REGION", REGION)
    hold = gnc.hold_gateway_names_for_teardown(
        [{"type": "iam_role", "name": role}],
        owner_sub=OWNER_A,
        deployment_id="d-down",
        default_account=ACCOUNT,
        default_region=region,
        sts_client_for=lambda: pytest.fail("the account is on record"),
        claims=gnc.GatewayNameClaims(table, clock=_Clock()),
    )
    assert hold.held == [(ACCOUNT, region, n) for n in expected]


def test_a_digested_gateway_role_resolves_only_against_a_recorded_name(table, monkeypatch):
    from app.services.naming import regional_iam_role_name

    monkeypatch.setenv("APP_AWS_REGION", REGION)
    long_name = "a-very-long-gateway-name-that-is-cut-by-iam-and-digested"
    role = regional_iam_role_name(f"AgentCoreGateway-{long_name}", REGION)
    assert long_name not in role, "the fixture must exercise the digest"
    with pytest.raises(gnc.GatewayNameClaimRefused, match="does not spell"):
        _hold(table, [{"type": "iam_role", "name": role}])
    assert table.scan()["Items"] == []
    hold = _hold(table, [{"type": "iam_role", "name": role}, {**GW_ROW, "name": long_name}])
    assert [n for _a, _r, n in hold.held] == [long_name]


# ---------------------------------------------------------------------------
# The race, through the real deploy_gateway
# ---------------------------------------------------------------------------


class _Step1Reached(Exception):
    """Raised by the fake Cognito setup: this deploy got past the claim."""


class _Ctrl:
    def list_gateways(self, **kw):
        return {"items": []}


class _Recorder:
    def __init__(self, calls: list, label: str):
        self._calls = calls
        self._label = label

    def __getattr__(self, op):
        def _call(*a, **kw):
            self._calls.append((self._label, op))
            raise AssertionError(f"{self._label}.{op} called")

        return _call


def _install_race(monkeypatch, calls: list):
    lock = threading.Lock()

    def _oauth(*a, **kw):
        with lock:
            calls.append(("cognito", "_create_cognito_oauth", threading.current_thread().name))
        raise _Step1Reached()

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: _Ctrl())
    monkeypatch.setattr(gateway_deployer, "_create_cognito_client", lambda region: _Recorder(calls, "cognito"))
    monkeypatch.setattr(gateway_deployer, "_create_iam_client", lambda: _Recorder(calls, "iam"))
    monkeypatch.setattr(gateway_deployer, "_create_cognito_oauth", _oauth)
    monkeypatch.setattr(
        gateway_deployer,
        "cleanup_gateway_resources",
        lambda *a, **kw: calls.append(("cleanup", threading.current_thread().name)) or [],
    )


def _race(atomic_table, monkeypatch, owners: tuple[str, str], *, one_deployment=False) -> tuple[dict, list]:
    # The atomic in-memory table, not moto: moto's conditional UpdateItem is not
    # atomic under threads, so two acquires could both "win" on moto alone. The
    # condition expressions themselves are pinned against moto sequentially above.
    calls: list = []
    _install_race(monkeypatch, calls)
    claims = gnc.GatewayNameClaims(atomic_table)
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def _deploy(label: str, owner: str):
        # A Step Functions retry, or a duplicate delivery, of ONE deployment: the
        # same id, and an invocation token of its own.
        dep = "d-same" if one_deployment else f"d-{label}"

        def _claim(name: str) -> None:
            barrier.wait(timeout=10)
            claims.acquire(**_where(name), owner_sub=owner, deployment_id=dep, token=f"tok-{label}")

        results[label] = gateway_deployer.deploy_gateway(
            {"name": NAME},
            REGION,
            deployment_id=dep,
            owner_sub=owner,
            gateway_consumers=lambda *_a: [],
            claim_gateway_name=_claim,
        )

    threads = [
        threading.Thread(target=_deploy, args=(label, owner), name=label)
        for label, owner in zip("ab", owners, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, calls


@pytest.mark.parametrize(
    "owners, one_deployment",
    [((OWNER_A, OWNER_B), False), ((OWNER_A, OWNER_A), False), ((OWNER_A, OWNER_A), True)],
    ids=["two-owners", "one-owner", "one-deployment-two-invocations"],
)
@pytest.mark.parametrize("_round", range(10))
def test_two_concurrent_deploys_of_one_name_reach_cognito_once(
    table, gateway_lock_table, monkeypatch, owners, one_deployment, _round
):
    results, calls = _race(gateway_lock_table, monkeypatch, owners, one_deployment=one_deployment)
    assert set(results) == {"a", "b"}
    step1 = [c for c in calls if c[:2] == ("cognito", "_create_cognito_oauth")]
    assert len(step1) == 1, f"both deploys passed the claim: {calls}"
    winner = step1[0][2]
    loser = "b" if winner == "a" else "a"
    # The loser was refused BY THE CLAIM, and touched nothing.
    assert results[loser]["success"] is False
    assert "Gateway name" in results[loser]["error"]
    assert not [c for c in calls if loser in c], f"the refused deploy made calls: {calls}"
    assert all(c[0] in ("cognito", "cleanup") and c[1] != "iam" for c in calls), calls


def test_without_a_claim_both_deploys_reach_cognito(table, monkeypatch):
    """The baseline the race test depends on: the barrier really does put both in Step 1."""
    calls: list = []
    _install_race(monkeypatch, calls)
    barrier = threading.Barrier(2)

    def _deploy(label):
        barrier.wait(timeout=10)
        gateway_deployer.deploy_gateway(
            {"name": NAME}, REGION, deployment_id=f"d-{label}", owner_sub=label, gateway_consumers=lambda *_a: []
        )

    threads = [threading.Thread(target=_deploy, args=(label,), name=label) for label in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len([c for c in calls if c[:2] == ("cognito", "_create_cognito_oauth")]) == 2


def test_a_refused_pre_flight_leaves_no_claim(table, monkeypatch):
    """The claim comes AFTER the adoption pre-flight: a deploy refused there claims nothing."""
    calls: list = []
    _install_race(monkeypatch, calls)

    class _CtrlWithForeign(_Ctrl):
        def list_gateways(self, **kw):
            return {"items": [{"name": NAME, "gatewayId": "gw-foreign"}]}

        def get_gateway(self, **kw):
            return {"gatewayId": "gw-foreign", "authorizerConfiguration": {}}

    monkeypatch.setattr(gateway_deployer, "_create_agentcore_control_client", lambda region: _CtrlWithForeign())
    claimed: list = []
    out = gateway_deployer.deploy_gateway(
        {"name": NAME},
        REGION,
        deployment_id="d-1",
        owner_sub=OWNER_A,
        gateway_consumers=lambda *_a: [{"deployment_id": "d-foreign", "owner_sub": OWNER_B}],
        claim_gateway_name=claimed.append,
    )
    assert out["success"] is False
    assert "you do not own" in out["error"]
    assert claimed == []
    assert calls == []


# ---------------------------------------------------------------------------
# The gateway step: it supplies the claim, and settles it only after the manifest
# ---------------------------------------------------------------------------


class _Session:
    """The same-account target session: STS names the account the claim is keyed on."""

    def client(self, service, **kw):
        if service == "secretsmanager":
            return object()  # built up front; these events carry no secret
        assert service == "sts", service

        class _Sts:
            def get_caller_identity(self):
                return {"Account": ACCOUNT}

        return _Sts()


class _Store:
    def __init__(self, table, *, refuse_types=()):
        self._table = table
        self.rows: list[dict] = []
        self.holder_at_record: list = []
        self.refuse_types = set(refuse_types)
        self.marked = 0

    def update_step(self, *a, **kw):
        pass

    def record_resource_strict(self, _deployment_id, resource):
        if resource.get("type") in self.refuse_types:
            raise _client_error("ProvisionedThroughputExceededException")
        item = self._table.get_item(Key={"claim_key": gnc.claim_key(ACCOUNT, REGION, NAME)}).get("Item") or {}
        self.holder_at_record.append(item.get("holder_deployment_id"))
        self.rows.append(dict(resource))

    def record_resource(self, deployment_id, resource):
        try:
            self.record_resource_strict(deployment_id, resource)
        except Exception:  # noqa: BLE001 - the real store's best-effort append
            self.marked += 1

    def mark_resource_manifest_error(self, _deployment_id):
        self.marked += 1


def _run_step(table, monkeypatch, *, result: dict, store=None, claims_table=None, clock=None):
    from app.step_handlers import gateway_step

    monkeypatch.setenv("APP_AWS_REGION", REGION)
    store = store or _Store(table)
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
    monkeypatch.setattr(gateway_step, "resolve_gateway_provider", lambda config: "agentcore")
    monkeypatch.setattr(gateway_step.step_clients, "session_for_event", lambda event: _Session())
    if claims_table is not None or clock is not None:
        monkeypatch.setattr(
            gateway_step,
            "claims_from_env",
            lambda: gnc.GatewayNameClaims(claims_table or table, clock=clock or _Clock()),
        )
    seen: dict = {}

    def _deploy_gateway(**kw):
        seen["claim"] = kw.get("claim_gateway_name")
        assert seen["claim"] is not None, "the step passed no claim to deploy_gateway"
        seen["claim"](NAME)
        seen["claim"](NAME)  # deploy_gateway's own retry: the same invocation, the same token
        return dict(result)

    monkeypatch.setattr(gateway_step, "deploy_gateway", _deploy_gateway)
    event = {
        "deployment_id": "d-1",
        "owner_sub": OWNER_A,
        "gateway_config": {"name": NAME},
    }
    return gateway_step, store, event


_OK = {"success": True, "gateway_id": "gw-1", "gateway_name": NAME, "client_info": {}}


def test_the_step_claims_and_promotes_after_recording(table, monkeypatch):
    step, store, event = _run_step(table, monkeypatch, result=_OK)
    step.handler(event, None)
    item = _claim_item(table)
    assert item["owner_sub"] == OWNER_A
    assert not {"holder_deployment_id", "holder_token", "provisional"} & set(item), item
    assert not [k for k in item if k.startswith("recovery_")]
    # Every manifest row was written while this deployment still held the lease.
    assert store.holder_at_record and set(store.holder_at_record) == {"d-1"}
    gw_rows = [r for r in store.rows if r["type"] == "gateway"]
    assert gw_rows == [{**gw_rows[0], "id": "gw-1", "name": NAME}]


def test_a_live_gateway_whose_row_never_landed_keeps_a_claim_that_records_it(table, monkeypatch):
    """A successful deploy whose gateway row DynamoDB refused: abandoning the claim
    would let another owner's deploy adopt a gateway nothing records."""
    step, store, event = _run_step(table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}))
    step.handler(event, None)
    item = _claim_item(table)
    assert "provisional" not in item and item["recovery_gateway_id"] == "gw-1"
    assert item["recovery_deployment_id"] == "d-1"
    with pytest.raises(gnc.GatewayNameClaimRefused, match="already in use"):
        _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")


def _pointer(table, deployment_id="d-1") -> dict | None:
    return table.get_item(Key={"claim_key": gnc.recovery_pointer_key(deployment_id)}).get("Item")


def _recovered(table, deployment_id="d-1"):
    return gnc.recovered_gateway_rows(
        deployment_id=deployment_id,
        owner_sub=OWNER_A,
        account_for=lambda: ACCOUNT,
        region=REGION,
        claims=gnc.GatewayNameClaims(table),
    )


def test_recovery_evidence_is_listed_by_its_pointer_in_the_same_write(table, monkeypatch):
    """d0: the pointer and the evidence are one transaction, so the first strongly
    consistent read of the pointer finds the claim: there is no lag to wait out."""
    step, _store, event = _run_step(table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}))
    step.handler(event, None)
    assert _pointer(table)["claim_keys"] == {gnc.claim_key(ACCOUNT, REGION, NAME)}
    assert _recovered(table) == [{"type": "gateway", "id": "gw-1", "name": NAME, "region": REGION}]


def test_a_failed_recovery_transaction_fails_the_step_and_carries_the_gateway(table, monkeypatch):
    """d0 #1: neither write lands, so no durable claim holds evidence no teardown can
    find, and the step does not return success: it carries the gateway, and its lease,
    to failure cleanup."""
    from app.services.failure_inventory import StepFailedWithUnrecordedRows

    def _down(**_kw):
        raise _client_error("InternalServerError")

    monkeypatch.setattr(table.meta.client, "transact_write_items", _down)
    step, _store, event = _run_step(
        table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}), claims_table=table
    )
    with pytest.raises(StepFailedWithUnrecordedRows) as caught:
        step.handler(event, None)
    assert {"type": "gateway", "id": "gw-1"}.items() <= caught.value.rows[0].items()
    assert caught.value.claim_token and caught.value.claim_token in str(caught.value)
    item = _claim_item(table)
    assert item["provisional"] is True and not [k for k in item if k.startswith("recovery_")]
    assert _pointer(table) is None


def test_a_recovery_promote_refused_by_the_claim_writes_no_pointer(table):
    """d0 #2: the claim's own condition fails (a stale token), so the transaction
    writes nothing: no pointer, no evidence, and False rather than an error."""
    claims = gnc.GatewayNameClaims(table)
    _acquire(claims)
    recovery = {"recovery_gateway_id": "gw-1", "recovery_deployment_id": "d-1"}
    assert claims.promote(**_where(), owner_sub=OWNER_A, token="tok-stale", recovery=recovery) is False
    assert _pointer(table) is None
    assert _claim_item(table)["provisional"] is True


def test_a_later_deployment_that_records_the_gateway_clears_the_old_evidence(table, monkeypatch):
    """e4 #2 end to end: d-1's gateway row never landed, so its claim records gw-1.
    d-2 of the same owner then deploys on the name and its gateway row IS acknowledged:
    gw-1 is d-2's record now, and d-1's teardown must not find it."""
    step, _store, event = _run_step(table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}))
    step.handler(event, None)
    assert [r["id"] for r in _recovered(table)] == ["gw-1"]

    step, store, event = _run_step(table, monkeypatch, result=_OK)
    step.handler({**event, "deployment_id": "d-2"}, None)
    assert [r["id"] for r in store.rows if r["type"] == "gateway"] == ["gw-1"]
    item = _claim_item(table)
    assert not [k for k in item if k.startswith("recovery_")] and "holder_token" not in item
    assert _recovered(table) == [], "the stale pointer entry is skipped: the evidence is the claim's"


def test_old_evidence_stays_until_a_later_deployment_records_that_gateway(table, monkeypatch):
    """Cleared only on an acknowledged row naming the same gateway: a later deploy
    whose row was refused, or whose row names another gateway, leaves it."""
    step, _store, event = _run_step(table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}))
    step.handler(event, None)

    step, _store, event = _run_step(table, monkeypatch, result=_OK, store=_Store(table, refuse_types={"gateway"}))
    step.handler({**event, "deployment_id": "d-2"}, None)
    assert [r["id"] for r in _recovered(table)] == ["gw-1"]

    step, _store, event = _run_step(table, monkeypatch, result={**_OK, "gateway_id": "gw-2"})
    step.handler({**event, "deployment_id": "d-3"}, None)
    assert [r["id"] for r in _recovered(table)] == ["gw-1"]
    assert "holder_token" not in _claim_item(table), "the lease is still released"


# --- reclaiming a recovery pointer (Codex 80) ------------------------------------


def _promote_recovery(claims, *, name, dep="d-1", gateway="gw-1"):
    tok = f"tok-{dep}-{name}"
    assert claims.acquire(**_where(name), owner_sub=OWNER_A, deployment_id=dep, token=tok)
    assert claims.promote(
        **_where(name),
        owner_sub=OWNER_A,
        token=tok,
        recovery={"recovery_gateway_id": gateway, "recovery_deployment_id": dep},
    )
    return gnc.claim_key(ACCOUNT, REGION, name)


def _reclaim(claims, dep="d-1"):
    return gnc.reclaim_recovery_pointer(deployment_id=dep, claims=claims)


def test_a_pointer_listing_live_evidence_is_never_marked(table):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _promote_recovery(claims, name="orders")
    assert _reclaim(claims) == gnc.POINTER_ACTIVE
    assert "gc_after" not in _pointer(table)
    assert _pointer(table)["pointer_generation"] == 1


def test_a_pointer_whose_evidence_was_erased_is_left_to_a_bounded_ttl(table):
    clock = _Clock()
    claims = gnc.GatewayNameClaims(table, clock=clock)
    _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    assert _reclaim(claims) == gnc.POINTER_MARKED
    assert _pointer(table)["gc_after"] == int(clock.now) + gnc.GC_GRACE_SECONDS


def test_a_pointer_whose_evidence_a_later_deployment_cleared_is_marked(table):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _promote_recovery(claims, name="orders")
    assert _acquire(claims, dep="d-2", name="orders") is False  # re-leases the durable claim
    assert claims.release(**_where("orders"), token="tok-d-2", recorded_gateways=("gw-1",))
    assert _reclaim(claims) == gnc.POINTER_MARKED


def test_any_listed_claim_still_carrying_evidence_keeps_the_pointer(table):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _promote_recovery(claims, name="orders")
    _promote_recovery(claims, name="payments", gateway="gw-2")
    assert _pointer(table)["claim_keys"] == {
        gnc.claim_key(ACCOUNT, REGION, "orders"),
        gnc.claim_key(ACCOUNT, REGION, "payments"),
    }
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    assert _reclaim(claims) == gnc.POINTER_ACTIVE and "gc_after" not in _pointer(table)
    assert claims.erase(**_where("payments"), owner_sub=OWNER_A)
    assert _reclaim(claims) == gnc.POINTER_MARKED


def test_a_promote_racing_the_check_fails_the_mark(table, monkeypatch):
    """The late promote lands between the claim reads and the mark: an ADD of a new
    key, and a new generation, so the conditional mark refuses."""
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    orders = _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    real, raced = gnc._read_projected, []

    def _racing(t, key):
        got = real(t, key)
        if key == orders and not raced:
            raced.append(_promote_recovery(claims, name="payments", gateway="gw-2"))
        return got

    monkeypatch.setattr(gnc, "_read_projected", _racing)
    assert _reclaim(claims) == gnc.POINTER_RACED
    monkeypatch.setattr(gnc, "_read_projected", real)
    assert raced and "gc_after" not in _pointer(table)
    assert _recovered(table) == [{"type": "gateway", "id": "gw-2", "name": "payments", "region": REGION}]


def test_a_promote_racing_the_check_on_a_listed_key_fails_the_mark(table, monkeypatch):
    """An ADD of a key already listed leaves claim_keys unchanged; the generation is
    what the mark is conditioned on, and it still moves."""
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    orders = _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    real, raced = gnc._read_projected, []

    def _racing(t, key):
        got = real(t, key)
        if key == orders and not raced:
            raced.append(_promote_recovery(claims, name="orders", gateway="gw-3"))
        return got

    monkeypatch.setattr(gnc, "_read_projected", _racing)
    assert _reclaim(claims) == gnc.POINTER_RACED
    monkeypatch.setattr(gnc, "_read_projected", real)
    assert _pointer(table)["claim_keys"] == {orders} and "gc_after" not in _pointer(table)
    assert [r["id"] for r in _recovered(table)] == ["gw-3"]


def test_a_promote_after_the_mark_unmarks_the_pointer(table):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    assert _reclaim(claims) == gnc.POINTER_MARKED
    _promote_recovery(claims, name="payments", gateway="gw-2")
    pointer = _pointer(table)
    assert "gc_after" not in pointer and pointer["pointer_generation"] == 2
    assert [r["id"] for r in _recovered(table)] == ["gw-2"]
    assert _reclaim(claims) == gnc.POINTER_ACTIVE


def test_a_pointer_the_ttl_removed_mid_check_is_not_recreated(table, monkeypatch):
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    orders = _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    real = gnc._read_projected

    def _expiring(t, key):
        got = real(t, key)
        if key == orders:
            table.delete_item(Key={"claim_key": gnc.recovery_pointer_key("d-1")})
        return got

    monkeypatch.setattr(gnc, "_read_projected", _expiring)
    assert _reclaim(claims) == gnc.POINTER_RACED
    assert _pointer(table) is None


def _pre_generation(table):
    """A pointer written before pointer_generation existed: claim_keys alone."""
    table.update_item(Key={"claim_key": gnc.recovery_pointer_key("d-1")}, UpdateExpression="REMOVE pointer_generation")


def test_a_pointer_from_before_generations_is_still_marked(table):
    """Rollout: the pointers already written carry no generation, and must still
    reach a bounded lifetime, not read as corrupt and leave every teardown failed."""
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    _pre_generation(table)
    assert _reclaim(claims) == gnc.POINTER_MARKED
    assert "gc_after" in _pointer(table) and "pointer_generation" not in _pointer(table)


def test_a_promote_racing_the_check_of_a_pre_generation_pointer_fails_the_mark(table, monkeypatch):
    """Its first promote gives it a generation, which is what the mark is conditioned on
    not existing."""
    claims = gnc.GatewayNameClaims(table, clock=_Clock())
    orders = _promote_recovery(claims, name="orders")
    assert claims.erase(**_where("orders"), owner_sub=OWNER_A)
    _pre_generation(table)
    real, raced = gnc._read_projected, []

    def _racing(t, key):
        got = real(t, key)
        if key == orders and not raced:
            raced.append(_promote_recovery(claims, name="orders", gateway="gw-3"))
        return got

    monkeypatch.setattr(gnc, "_read_projected", _racing)
    assert _reclaim(claims) == gnc.POINTER_RACED
    monkeypatch.setattr(gnc, "_read_projected", real)
    assert raced and "gc_after" not in _pointer(table)
    assert [r["id"] for r in _recovered(table)] == ["gw-3"]


@pytest.mark.parametrize(
    ("case", "pointer"),
    [
        ("a string generation", {"claim_keys": {"k"}, "pointer_generation": "1"}),
        ("no keys", {"pointer_generation": 1}),
    ],
)
def test_a_malformed_pointer_is_never_marked(table, case, pointer):
    table.put_item(Item={"claim_key": gnc.recovery_pointer_key("d-1"), **pointer})
    assert _reclaim(gnc.GatewayNameClaims(table)) == gnc.POINTER_MALFORMED
    assert "gc_after" not in _pointer(table)


def test_no_pointer_is_absent_and_creates_none(table):
    assert _reclaim(gnc.GatewayNameClaims(table)) == gnc.POINTER_ABSENT
    assert _pointer(table) is None


def test_a_failed_deploy_whose_rows_landed_keeps_its_claim(table, monkeypatch):
    step, store, event = _run_step(
        table,
        monkeypatch,
        result={"success": False, "error": "boom", "gateway_id": "gw-1", "gateway_name": NAME},
    )
    with pytest.raises(RuntimeError, match="boom"):
        step.handler(event, None)
    item = _claim_item(table)
    assert not {"holder_deployment_id", "provisional"} & set(item)
    assert [r["id"] for r in store.rows if r["type"] == "gateway"] == ["gw-1"]
    assert set(store.holder_at_record) == {"d-1"}


def test_a_failed_deploy_that_created_nothing_frees_the_name_at_once(table, monkeypatch):
    """a0: a zero-inventory failure has nothing to guard and no row a DELETE could
    erase a claim by, so the name is anyone's the moment the step fails."""
    step, _store, event = _run_step(table, monkeypatch, result={"success": False, "error": "validation"})
    with pytest.raises(RuntimeError, match="validation"):
        step.handler(event, None)
    assert _free(table)
    assert _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b") is True


def test_a_failed_deploy_whose_abandon_is_lost_frees_the_name_at_its_lease_end(table, monkeypatch):
    """37's oracle through the step: the one lost write is the abandon."""
    clock = _Clock()
    step, _store, event = _run_step(
        table,
        monkeypatch,
        result={"success": False, "error": "validation"},
        claims_table=_FailingWrites(table, expr="SET holder_expires_at = :gone"),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="validation"):
        step.handler(event, None)
    assert _claim_item(table)["provisional"] is True
    clock.now = 1_000_900
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(gnc.GatewayNameClaims(table, clock=clock), owner=OWNER_B, dep="d-b")
    clock.now = 1_000_901
    assert _acquire(gnc.GatewayNameClaims(table, clock=clock), owner=OWNER_B, dep="d-b") is True


def test_unrecorded_graph_rows_hand_the_live_lease_to_failure_cleanup(table, monkeypatch):
    """a4 + 38: rows that never landed do not promote the claim, and do not free it
    either: the lease is carried out with them, and only its token re-takes it."""
    from app.services.failure_inventory import StepFailedWithUnrecordedRows, claim_token_from_error_info

    step, store, event = _run_step(
        table,
        monkeypatch,
        result={"success": False, "error": "boom", "gateway_id": "gw-1", "gateway_name": NAME},
        store=_Store(table, refuse_types={"gateway", "iam_role", "cognito_resource_server"}),
    )
    with pytest.raises(StepFailedWithUnrecordedRows) as info:
        step.handler(event, None)
    token = info.value.claim_token
    assert token and claim_token_from_error_info({"Cause": str(info.value)}) == token
    item = _claim_item(table)
    assert item["provisional"] is True and item["holder_token"] == token
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _acquire(gnc.GatewayNameClaims(table), owner=OWNER_B, dep="d-b")
    with pytest.raises(gnc.GatewayNameClaimRefused):
        _hold(table, [GW_ROW])  # any other teardown waits
    hold = _hold(table, [GW_ROW], token=token)
    assert hold.held == [(ACCOUNT, REGION, NAME)]


def test_a_step_with_no_claim_table_fails_before_deploying(monkeypatch):
    """Fail closed: the real claim callback raises, and deploy_gateway turns that into
    a failure before Step 1 (see test_two_concurrent_deploys_...)."""
    from app.step_handlers import gateway_step

    monkeypatch.delenv(gnc.CLAIM_TABLE_ENV, raising=False)
    monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: _Store(None))
    monkeypatch.setattr(gateway_step, "resolve_gateway_provider", lambda config: "agentcore")
    monkeypatch.setattr(gateway_step.step_clients, "session_for_event", lambda event: _Session())

    def _deploy_gateway(**kw):
        kw["claim_gateway_name"](NAME)
        pytest.fail("the claim did not raise")

    monkeypatch.setattr(gateway_step, "deploy_gateway", _deploy_gateway)
    with pytest.raises(RuntimeError, match=gnc.CLAIM_TABLE_ENV):
        gateway_step.handler({"deployment_id": "d-1", "owner_sub": OWNER_A, "gateway_config": {"name": NAME}}, None)
