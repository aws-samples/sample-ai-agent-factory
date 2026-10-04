"""A Cognito auth helper that fails part-way deletes what it created, and nothing else.

Both helpers raise before they return, so deploy_gateway's abort handler never has a
``cognito_response`` to read and cannot name what they already made. Before the
rollback, a failed ``create_user_pool_client`` leaked the resource server, a failed
secret copy leaked a live app client (a credential nobody names), and the dedicated
path swallowed a failed domain create and RETURNED SUCCESS advertising a token endpoint
that did not exist. Every test asserts the delete calls that happened, in order, not
only that the error surfaced.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.services import gateway_deployer as gd

POOL = "us-east-1_SHAREDPOOL"
DOMAIN = "acf-test-gw-0123456789"
GW = "orders"
RS = f"agentcore-{GW}"
CLIENT = "client-orders-1"
DEDICATED = "us-east-1_DEDICATED1"


class _Cognito:
    """Stateful: lists live clients by name (resource_server_is_unused keys on names),
    describes resource servers that exist, and detaches a deleted domain only after
    ``domain_lag`` further DescribeUserPool reads, as Cognito does."""

    def __init__(
        self,
        *,
        fail: str = "",
        rs_exists: bool = False,
        foreign_client: bool = False,
        no_secret: bool = False,
        domain_lag: int = 1,
    ):
        self.fail = fail
        self.no_secret = no_secret
        self.domain_lag = domain_lag
        self.calls: list[tuple] = []
        self.resource_servers: set[tuple[str, str]] = {(POOL, RS)} if rs_exists else set()
        self.clients: dict[str, str] = {"someone-else": f"{GW}-client"} if foreign_client else {}
        self.domains: dict[str, str] = {}
        self._detach_in: int | None = None

    def _maybe_fail(self, op: str):
        if self.fail == op:
            raise RuntimeError(f"injected {op} failure")

    def create_user_pool(self, **kw):
        self.calls.append(("create_user_pool",))
        return {"UserPool": {"Id": DEDICATED}}

    def create_resource_server(self, **kw):
        self._maybe_fail("create_resource_server")
        key = (kw["UserPoolId"], kw["Identifier"])
        if key in self.resource_servers:
            raise RuntimeError("InvalidParameterException: resource server already exists")
        self.calls.append(("create_resource_server", *key))
        self.resource_servers.add(key)

    def describe_resource_server(self, **kw):
        self.calls.append(("describe_resource_server", kw["UserPoolId"], kw["Identifier"]))
        if (kw["UserPoolId"], kw["Identifier"]) not in self.resource_servers:
            raise RuntimeError("ResourceNotFoundException")
        return {"ResourceServer": {"Identifier": kw["Identifier"], "Scopes": [{"ScopeName": "invoke"}]}}

    def create_user_pool_domain(self, **kw):
        self._maybe_fail("create_user_pool_domain")
        self.calls.append(("create_user_pool_domain", kw["UserPoolId"], kw["Domain"]))
        self.domains[kw["UserPoolId"]] = kw["Domain"]

    def create_user_pool_client(self, **kw):
        self._maybe_fail("create_user_pool_client")
        self.calls.append(("create_user_pool_client", kw["UserPoolId"]))
        self.clients[CLIENT] = kw["ClientName"]
        client = {"ClientId": CLIENT}
        if not self.no_secret:
            client["ClientSecret"] = "notarealsecret-0000"  # pragma: allowlist secret
        return {"UserPoolClient": client}

    def list_user_pool_clients(self, **kw):
        return {"UserPoolClients": [{"ClientId": c, "ClientName": n} for c, n in self.clients.items()]}

    def delete_user_pool_client(self, **kw):
        self._maybe_fail("delete_user_pool_client")
        self.calls.append(("delete_user_pool_client", kw["UserPoolId"], kw["ClientId"]))
        self.clients.pop(kw["ClientId"], None)

    def delete_resource_server(self, **kw):
        self.calls.append(("delete_resource_server", kw["UserPoolId"], kw["Identifier"]))
        self.resource_servers.discard((kw["UserPoolId"], kw["Identifier"]))

    def delete_user_pool_domain(self, **kw):
        self.calls.append(("delete_user_pool_domain", kw["UserPoolId"], kw["Domain"]))
        self._detach_in = self.domain_lag

    def describe_user_pool(self, **kw):
        if self._detach_in is not None:
            if self._detach_in <= 0:
                self.domains.pop(kw["UserPoolId"], None)
            self._detach_in -= 1
        domain = self.domains.get(kw["UserPoolId"])
        return {"UserPool": {"Domain": domain} if domain else {}}

    def delete_user_pool(self, **kw):
        if self.domains.get(kw["UserPoolId"]):
            raise RuntimeError("InvalidParameterException: a domain is still configured")
        self.calls.append(("delete_user_pool", kw["UserPoolId"]))

    def deletes(self) -> list[tuple]:
        return [c for c in self.calls if c[0].startswith("delete_")]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gd.time, "sleep", lambda *_a: None)


@pytest.fixture
def store(monkeypatch):
    """The Secrets Manager write under _mint_client_secret_ref, which stays real."""
    state = {"fail": False, "calls": 0}

    # ``resource_tags`` is accepted EXPLICITLY, not via ``**kwargs``, and the fake records it.
    # The real ``_put_connector_secret`` grew the parameter with P0-B governance tagging, and
    # this fake's fixed signature is what caught it: the call raised TypeError, which the
    # rollback test then read as "the injected failure did not happen". A ``**_`` here would
    # have made both tests pass while proving nothing about whether the tags arrive -- the same
    # silent swallow that ``deploy_litellm_gateway``'s ``**_ignored`` would have performed on
    # the real path. Keeping it named means a future parameter fails loudly here too.
    def _put(region, owner_sub, payload, deployment_id, *, resource_tags=None):
        state["calls"] += 1
        state["resource_tags"] = resource_tags
        if state["fail"]:
            raise RuntimeError("injected secret store failure")
        return "arn:aws:secretsmanager:us-east-1:123456789012:secret:agentcore-connector/dep/client-AbCdEf"

    monkeypatch.setattr(gd, "_put_connector_secret", _put)
    return state


def _shared(cog):
    return gd._create_cognito_oauth_in_shared_pool(cog, GW, "us-east-1", POOL, DOMAIN, "sub-a", "dep-1")


# --- shared pool -------------------------------------------------------------------


def test_shared_success_deletes_nothing(store):
    # The happy path: without it every rollback below is compatible with a helper
    # that deletes on every call.
    cog = _Cognito()
    out = _shared(cog)
    assert out["client_info"]["client_id"] == CLIENT
    assert out["resource_server_created"] is True
    assert cog.deletes() == []


def test_shared_resource_server_access_denied_is_raised_not_reused(store):
    cog = _Cognito(fail="create_resource_server")
    with pytest.raises(RuntimeError, match="injected create_resource_server failure"):
        _shared(cog)
    assert not any(c[0] == "create_user_pool_client" for c in cog.calls)
    assert cog.deletes() == []
    assert store["calls"] == 0


def test_shared_existing_resource_server_is_reused_and_never_deleted(store):
    store["fail"] = True
    cog = _Cognito(rs_exists=True)
    with pytest.raises(RuntimeError, match="injected secret store failure"):
        _shared(cog)
    assert ("describe_resource_server", POOL, RS) in cog.calls
    assert cog.deletes() == [("delete_user_pool_client", POOL, CLIENT)]
    assert (POOL, RS) in cog.resource_servers


def test_shared_client_create_failure_deletes_the_resource_server_it_created(store):
    cog = _Cognito(fail="create_user_pool_client")
    with pytest.raises(RuntimeError, match="injected create_user_pool_client failure"):
        _shared(cog)
    assert cog.deletes() == [("delete_resource_server", POOL, RS)]


def test_shared_missing_client_secret_deletes_the_client_then_the_resource_server(store):
    cog = _Cognito(no_secret=True)
    with pytest.raises(RuntimeError, match="no ClientSecret"):
        _shared(cog)
    assert cog.deletes() == [
        ("delete_user_pool_client", POOL, CLIENT),
        ("delete_resource_server", POOL, RS),
    ]
    assert store["calls"] == 0


def test_shared_secret_store_failure_deletes_the_client_then_the_resource_server(store):
    store["fail"] = True
    cog = _Cognito()
    with pytest.raises(RuntimeError, match="injected secret store failure"):
        _shared(cog)
    assert cog.deletes() == [
        ("delete_user_pool_client", POOL, CLIENT),
        ("delete_resource_server", POOL, RS),
    ]


def test_shared_resource_server_a_concurrent_deploy_joined_is_kept(store):
    store["fail"] = True
    cog = _Cognito(foreign_client=True)
    with pytest.raises(RuntimeError, match="injected secret store failure") as exc:
        _shared(cog)
    assert cog.deletes() == [("delete_user_pool_client", POOL, CLIENT)]
    assert "resource server: left in place" in "".join(exc.value.__notes__)


def test_shared_failed_cleanup_keeps_the_original_error_and_reports_itself(store):
    store["fail"] = True
    cog = _Cognito(fail="delete_user_pool_client")
    with pytest.raises(RuntimeError, match="injected secret store failure") as exc:
        _shared(cog)
    notes = "".join(exc.value.__notes__)
    assert "app client: RuntimeError" in notes
    # Our own client could not be deleted, so the resource server is still in use.
    assert "resource server: left in place" in notes
    assert cog.deletes() == []
    assert "notarealsecret" not in notes


# --- dedicated pool ----------------------------------------------------------------


def _dedicated(cog, monkeypatch):
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_ID", raising=False)
    monkeypatch.delenv("GATEWAY_SHARED_USER_POOL_DOMAIN", raising=False)
    return gd._create_cognito_oauth(cog, GW, "us-east-1", "sub-a", "dep-1")


def test_dedicated_success_deletes_nothing(store, monkeypatch):
    cog = _Cognito()
    out = _dedicated(cog, monkeypatch)
    assert out["client_info"]["user_pool_id"] == DEDICATED
    assert cog.deletes() == []


def test_dedicated_resource_server_failure_deletes_the_pool(store, monkeypatch):
    cog = _Cognito(fail="create_resource_server")
    with pytest.raises(RuntimeError, match="injected create_resource_server failure"):
        _dedicated(cog, monkeypatch)
    assert cog.deletes() == [("delete_user_pool", DEDICATED)]
    assert not any(c[0] == "create_user_pool_client" for c in cog.calls)


def test_dedicated_domain_failure_fails_and_deletes_only_the_pool(store, monkeypatch):
    """It used to RETURN SUCCESS advertising a token endpoint on a domain that was
    never created. A collided domain belongs to someone else, so it is not deleted."""
    cog = _Cognito(fail="create_user_pool_domain")
    with pytest.raises(RuntimeError, match="injected create_user_pool_domain failure"):
        _dedicated(cog, monkeypatch)
    assert cog.deletes() == [("delete_user_pool", DEDICATED)]
    assert not any(c[0] == "create_user_pool_client" for c in cog.calls)
    assert store["calls"] == 0


@pytest.mark.parametrize("fault", ["create_user_pool_client", "no_secret", "store"])
def test_dedicated_later_failure_detaches_the_domain_then_deletes_the_pool(store, monkeypatch, fault):
    cog = _Cognito(fail=fault, no_secret=fault == "no_secret", domain_lag=2)
    store["fail"] = fault == "store"
    with pytest.raises(RuntimeError):
        _dedicated(cog, monkeypatch)
    domain = next(c[2] for c in cog.calls if c[0] == "create_user_pool_domain")
    assert cog.deletes() == [
        ("delete_user_pool_domain", DEDICATED, domain),
        ("delete_user_pool", DEDICATED),
    ]


def test_dedicated_domain_that_never_detaches_is_reported_with_the_original_error(store, monkeypatch):
    store["fail"] = True
    cog = _Cognito(domain_lag=10_000)
    with pytest.raises(RuntimeError, match="injected secret store failure") as exc:
        _dedicated(cog, monkeypatch)
    notes = "".join(exc.value.__notes__)
    assert "domain: TimeoutError" in notes
    assert "user pool: RuntimeError" in notes


# --- through deploy_gateway ----------------------------------------------------------


def test_through_deploy_gateway_a_failed_secret_store_leaks_nothing(store, monkeypatch):
    """The real abort path: deploy_gateway's own cleanup plus the helper's rollback."""
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_ID", POOL)
    monkeypatch.setenv("GATEWAY_SHARED_USER_POOL_DOMAIN", DOMAIN)
    store["fail"] = True
    cog = _Cognito()
    ctrl = MagicMock()
    ctrl.list_gateways.return_value = {"items": []}
    iam = MagicMock()
    iam.exceptions.EntityAlreadyExistsException = type("E", (Exception,), {})
    monkeypatch.setattr(gd, "_create_platform_cognito_client", lambda region: cog)
    monkeypatch.setattr(gd, "_create_cognito_client", lambda region: cog)
    monkeypatch.setattr(gd, "_create_agentcore_control_client", lambda region: ctrl)
    monkeypatch.setattr(gd, "_create_iam_client", lambda *a, **k: iam)

    out = gd.deploy_gateway({"name": GW}, "us-east-1", owner_sub="sub-a", deployment_id="dep-1")

    assert out["success"] is False
    assert "injected secret store failure" in out["error"]
    assert cog.clients == {}, "the app client must not survive the failed deploy"
    assert cog.resource_servers == set(), "nor the resource server this deploy created"
    assert ctrl.create_gateway.call_count == 0
    assert iam.create_role.call_count == 0
