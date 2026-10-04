"""F-63: what an adopted gateway's authorizer allows, and when adoption is refused.

Adoption by name used to replace ``allowedClients`` with the new deploy's client and
retire the previous one, so the previous version's runtime (every version is its own
runtime, and they share the implied gateway) got HTTP 400 at the token endpoint and
lost every tool. Measured live on f62gw-1790114295. These pin the decision function
directly; the deploy_gateway wiring is pinned in test_foreign_gateway_is_not_adopted.
"""

from __future__ import annotations

import pytest
from app.services.gateway_deployer import GatewayAdoptionRefused, _adoption_authorizer

POOL_A = "us-east-1_PoolAAAAA"
URL_A = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_A}/.well-known/openid-configuration"
URL_EXT_A = "https://idp-a.example.com/.well-known/openid-configuration"
URL_EXT_B = "https://idp-b.example.com/.well-known/openid-configuration"


def _auth(url: str, *clients: str) -> dict:
    return {"customJWTAuthorizer": {"discoveryUrl": url, "allowedClients": list(clients)}}


def _clients(auth: dict) -> list[str]:
    return auth["customJWTAuthorizer"]["allowedClients"]


def _adopt(previous, new, consumers, owner="sub-a"):
    return _adoption_authorizer(
        "gw", "gw-id", previous, new, owner_sub=owner, gateway_consumers=lambda gid, pool: consumers
    )


def test_same_owner_live_consumer_keeps_its_client():
    # The happy path: without it the refusals below are compatible with refusing all.
    out = _adopt(_auth(URL_A, "old"), _auth(URL_A, "new"), [{"owner_sub": "sub-a", "client_ids": ["old"]}])
    assert _clients(out) == ["new", "old"]
    assert out["customJWTAuthorizer"]["discoveryUrl"] == URL_A


def test_no_live_consumer_is_the_plain_redeploy():
    out = _adopt(_auth(URL_A, "old"), _auth(URL_A, "new"), [])
    assert _clients(out) == ["new"]


def test_a_missing_owner_is_refused_even_with_no_consumers():
    with pytest.raises(GatewayAdoptionRefused):
        _adopt(_auth(URL_A, "old"), _auth(URL_A, "new"), [], owner="")


def test_a_consumer_with_unknown_clients_keeps_every_previous_client():
    # A legacy or manifest-only row names no client. Dropping "old" would strand it.
    out = _adopt(_auth(URL_A, "old", "older"), _auth(URL_A, "new"), [{"owner_sub": "sub-a", "client_ids": []}])
    assert _clients(out) == ["new", "old", "older"]


def test_a_different_issuer_with_a_live_consumer_is_refused():
    # Both external IdPs have no Cognito pool id, so a pool comparison sees them equal.
    with pytest.raises(GatewayAdoptionRefused):
        _adopt(_auth(URL_EXT_A, "old"), _auth(URL_EXT_B, "new"), [{"owner_sub": "sub-a", "client_ids": ["old"]}])


def test_a_different_issuer_with_an_incomplete_consumer_is_refused():
    with pytest.raises(GatewayAdoptionRefused):
        _adopt(_auth(URL_EXT_A, "old"), _auth(URL_EXT_B, "new"), [{"owner_sub": "sub-a", "client_ids": []}])


def test_a_different_issuer_with_no_live_consumer_is_the_redeploy():
    out = _adopt(_auth(URL_EXT_A, "old"), _auth(URL_EXT_B, "new"), [])
    assert _clients(out) == ["new"]
    assert out["customJWTAuthorizer"]["discoveryUrl"] == URL_EXT_B


def test_allowed_clients_are_deduplicated_in_a_stable_order():
    out = _adopt(
        _auth(URL_A, "old", "old", "gone"),
        _auth(URL_A, "new", "new"),
        [{"owner_sub": "sub-a", "client_ids": ["old"]}, {"owner_sub": "sub-a", "client_ids": ["old"]}],
    )
    assert _clients(out) == ["new", "old"]


def test_a_foreign_owner_is_refused():
    with pytest.raises(GatewayAdoptionRefused, match="do not own"):
        _adopt(_auth(URL_A, "old"), _auth(URL_A, "new"), [{"owner_sub": "sub-b", "client_ids": ["old"]}])


def test_a_consumer_with_no_recorded_owner_is_refused():
    with pytest.raises(GatewayAdoptionRefused, match="do not own"):
        _adopt(_auth(URL_A, "old"), _auth(URL_A, "new"), [{"owner_sub": "", "client_ids": ["old"]}])


@pytest.mark.parametrize("consumers", [None, "raise"])
def test_an_unreadable_consumer_list_is_refused(consumers):
    def _read(gid, pool):
        raise RuntimeError("scan failed")

    with pytest.raises(GatewayAdoptionRefused):
        _adoption_authorizer(
            "gw",
            "gw-id",
            _auth(URL_A, "old"),
            _auth(URL_A, "new"),
            owner_sub="sub-a",
            gateway_consumers=None if consumers is None else _read,
        )
