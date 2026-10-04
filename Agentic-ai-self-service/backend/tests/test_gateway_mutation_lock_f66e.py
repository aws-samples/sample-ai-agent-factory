"""F-66e: one writer at a time on an existing gateway.

UpdateGateway is a full replace with no version check. A redeploy that read the
gateway's ``allowedClients`` before a teardown revoked a client, and sent its update
after, put the revoked client back -- after the teardown had read the gateway back
without it and reported the revoke done. Every writer now holds a lock keyed by the
gateway id from its read to the read-back proving its own update landed.
"""

import ast
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.services import gateway_mutation_lock as gml
from app.services.gateway_update import preserving_gateway_update
from botocore.exceptions import ClientError

KEY = "gwlock#us-east-1#gw-shared"
SRC = Path(__file__).resolve().parents[1] / "src" / "app"


class _Clock:
    """time.time and time.sleep over one fake clock, so waits cost nothing."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(gml, "time", SimpleNamespace(time=c.time, sleep=c.sleep))
    return c


class _Gateway:
    """A gateway whose update applies after ``lag`` reads, or never when ``lag`` is None."""

    def __init__(self, clients, *, lag=0, status_after=None):
        self.detail = {
            "gatewayId": "gw-shared",
            "name": "shared",
            "roleArn": "arn:aws:iam::111111111111:role/AgentCoreGateway-shared",
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://issuer/.well-known", "allowedClients": list(clients)}
            },
            "status": "READY",
        }
        self.lag = lag
        self.status_after = status_after
        self.pending = None
        self.reads_since_update = 0
        self.updates = []
        self.update_error = None

    def get_gateway(self, **_k):
        if self.pending is not None:
            self.reads_since_update += 1
            if self.status_after:
                return {**self.detail, "status": self.status_after}
            if self.lag is not None and self.reads_since_update > self.lag:
                self.detail, self.pending = self.pending, None
        return dict(self.detail)

    def update_gateway(self, **request):
        if self.update_error:
            raise self.update_error
        self.updates.append(request)
        self.reads_since_update = 0
        self.pending = {**self.detail, **{k: v for k, v in request.items() if k != "gatewayIdentifier"}}
        if self.lag == 0 and not self.status_after:
            self.detail, self.pending = self.pending, None


def _with_clients(detail, clients):
    jwt = detail["authorizerConfiguration"]["customJWTAuthorizer"]
    return preserving_gateway_update(
        detail,
        "gw-shared",
        overrides={"authorizerConfiguration": {"customJWTAuthorizer": {**jwt, "allowedClients": list(clients)}}},
    )


def _clients_are(clients):
    return lambda d: gml.allowed_clients(d) == list(clients)


# --- the lock itself --------------------------------------------------------------


def test_the_lock_is_taken_for_the_body_and_released_after(gateway_lock_table, clock):
    gw = _Gateway(["a"])
    with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
        assert gateway_lock_table.items[KEY]["lock_expires_at"] == int(clock.now) + gml.LOCK_SECONDS
        lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert KEY not in gateway_lock_table.items
    assert gml.allowed_clients(gw.detail) == ["b"]


def test_a_held_lock_makes_the_next_writer_wait_then_refuse_without_reading(gateway_lock_table, clock):
    gateway_lock_table.items[KEY] = {"claim_key": KEY, "lock_holder": "other", "lock_expires_at": clock.now + 600}
    gw = _Gateway(["a"])
    gw.get_gateway = gw.update_gateway = pytest.fail  # never reached

    with pytest.raises(gml.GatewayMutationBusy):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared"):
            pytest.fail("the body ran without the lock")
    assert clock.slept >= gml.WAIT_SECONDS
    assert gateway_lock_table.items[KEY]["lock_holder"] == "other"


def test_a_writer_that_waits_gets_the_lock_once_it_is_released(gateway_lock_table, clock):
    gateway_lock_table.items[KEY] = {"claim_key": KEY, "lock_holder": "other", "lock_expires_at": clock.now + 600}
    real_sleep = clock.sleep

    def _sleep(s):
        real_sleep(s)
        if clock.slept >= 9:
            gateway_lock_table.items.pop(KEY, None)

    clock.sleep = _sleep
    gml.time.sleep = _sleep
    with gml.gateway_mutation_lock(_Gateway(["a"]), "us-east-1", "gw-shared"):
        assert gateway_lock_table.items[KEY]["lock_holder"] != "other"
    assert 9 <= clock.slept < gml.WAIT_SECONDS


def test_an_expired_lock_is_taken_over(gateway_lock_table, clock):
    gateway_lock_table.items[KEY] = {"claim_key": KEY, "lock_holder": "crashed", "lock_expires_at": clock.now - 1}
    with gml.gateway_mutation_lock(_Gateway(["a"]), "us-east-1", "gw-shared"):
        assert gateway_lock_table.items[KEY]["lock_holder"] != "crashed"
    assert clock.slept == 0


def test_a_holder_that_outlived_its_lease_never_frees_the_next_holders_lock(gateway_lock_table, clock):
    with gml.gateway_mutation_lock(_Gateway(["a"]), "us-east-1", "gw-shared"):
        # Our lease expired and another writer took the gateway.
        gateway_lock_table.items[KEY] = {"claim_key": KEY, "lock_holder": "next", "lock_expires_at": clock.now + 900}
    assert gateway_lock_table.items[KEY]["lock_holder"] == "next"


def test_a_lock_table_error_is_not_a_lock_and_nothing_is_read(gateway_lock_table, clock):
    def _throttled(**_k):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem")

    gateway_lock_table.put_item = _throttled
    with pytest.raises(ClientError):
        with gml.gateway_mutation_lock(_Gateway(["a"]), "us-east-1", "gw-shared"):
            pytest.fail("the body ran without the lock")


@pytest.mark.real_gateway_lock
def test_without_a_claims_table_no_writer_can_take_the_lock(monkeypatch):
    monkeypatch.delenv("GATEWAY_NAME_CLAIMS_TABLE_NAME", raising=False)
    with pytest.raises(RuntimeError, match="GATEWAY_NAME_CLAIMS_TABLE_NAME is not set"):
        with gml.gateway_mutation_lock(_Gateway(["a"]), "us-east-1", "gw-shared"):
            pytest.fail("the body ran without the lock")


@pytest.mark.parametrize("region, gateway_id", [("", "gw"), ("us-east-1", ""), (None, "gw")])
def test_a_lock_needs_both_halves_of_its_key(region, gateway_id):
    with pytest.raises(ValueError):
        gml.lock_key(region, gateway_id)


def test_the_lock_key_is_region_scoped():
    assert gml.lock_key("us-east-1", "gw") != gml.lock_key("us-west-2", "gw")
    assert gml.lock_key("us-east-1", "gw").startswith("gwlock#")


# --- the postcondition --------------------------------------------------------------


def test_a_stale_ready_read_back_is_waited_through(gateway_lock_table, clock):
    gw = _Gateway(["a"], lag=3)
    with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
        out = lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert gml.allowed_clients(out) == ["b"]
    assert gw.reads_since_update == 4
    assert KEY not in gateway_lock_table.items


def test_an_update_never_read_back_keeps_the_lock_until_it_expires(gateway_lock_table, clock):
    gw = _Gateway(["a"], lag=None)
    with pytest.raises(gml.GatewayUpdateUnconfirmed):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
            lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert gw.reads_since_update == gml.CONFIRM_ATTEMPTS
    # Still in flight as far as anyone can tell: the next writer must not race it.
    assert KEY in gateway_lock_table.items


@pytest.mark.parametrize("status", ["FAILED", "UPDATE_UNSUCCESSFUL"])
def test_a_failed_update_is_final_and_releases_the_lock(gateway_lock_table, clock, status):
    gw = _Gateway(["a"], status_after=status)
    with pytest.raises(gml.GatewayUpdateFailed):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
            lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert gw.reads_since_update == 1
    assert KEY not in gateway_lock_table.items


def test_a_refused_update_releases_the_lock(gateway_lock_table, clock):
    gw = _Gateway(["a"])
    gw.update_error = ClientError({"Error": {"Code": "ValidationException"}}, "UpdateGateway")
    with pytest.raises(ClientError):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
            lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert KEY not in gateway_lock_table.items


def test_an_update_whose_outcome_is_unknown_keeps_the_lock(gateway_lock_table, clock):
    gw = _Gateway(["a"])
    gw.update_error = TimeoutError("read timeout")
    with pytest.raises(gml.GatewayUpdateUnconfirmed):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
            lk.update(_with_clients(lk.read(), ["b"]), _clients_are(["b"]))
    assert KEY in gateway_lock_table.items


def test_the_read_waits_for_an_update_still_landing(gateway_lock_table, clock):
    gw = _Gateway(["a"])
    reads = iter(["UPDATING", "UPDATING", "READY"])
    real = gw.get_gateway
    gw.get_gateway = lambda **k: {**real(**k), "status": next(reads)}
    with gml.gateway_mutation_lock(gw, "us-east-1", "gw-shared") as lk:
        assert lk.read()["status"] == "READY"
    assert clock.slept == 10


def test_the_predicates_read_what_they_claim():
    arn = "arn:aws:bedrock-agentcore:us-east-1:111111111111:policy-engine/pe"
    assert gml.engine_is(arn, "ENFORCE")({"policyEngineConfiguration": {"arn": arn, "mode": "ENFORCE"}})
    assert not gml.engine_is(arn, "ENFORCE")({"policyEngineConfiguration": {"arn": arn, "mode": "LOG_ONLY"}})
    assert not gml.engine_is(arn, "ENFORCE")({})
    assert gml.engine_detached({}) and gml.engine_detached({"policyEngineConfiguration": None})
    assert not gml.engine_detached({"policyEngineConfiguration": {"arn": arn, "mode": "ENFORCE"}})
    want = {"customJWTAuthorizer": {"discoveryUrl": "https://i", "allowedClients": ["a", "b"]}}
    assert gml.authorizer_is(want)(
        {
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://i", "allowedClients": ["b", "a"]}
            }
        }
    )
    assert not gml.authorizer_is(want)(
        {"authorizerConfiguration": {"customJWTAuthorizer": {"discoveryUrl": "https://i", "allowedClients": ["a"]}}}
    )
    assert not gml.authorizer_is(want)(
        {
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://old", "allowedClients": ["a", "b"]}
            }
        }
    )


# --- no writer can skip it ----------------------------------------------------------


def _parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node


def _enclosing(node, kind):
    while node is not None:
        node = getattr(node, "parent", None)
        if isinstance(node, kind):
            return node
    return None


def _calls(attr_or_name):
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name == attr_or_name:
                yield path.relative_to(SRC).as_posix(), node


def test_update_gateway_is_called_in_exactly_one_place_the_lock():
    sites = [(p, _enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef)).name) for p, n in _calls("update_gateway")]
    assert sites == [("services/gateway_mutation_lock.py", "update")]


def test_every_lock_is_a_with_and_every_writer_is_known():
    """A new writer is a deliberate change to this list, not a silent one."""
    uses = []
    for path, node in _calls("gateway_mutation_lock"):
        assert isinstance(node.parent, ast.withitem), f"{path}:{node.lineno} takes the lock outside a with"
        uses.append((path, _enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef)).name))
    assert sorted(uses) == sorted(
        [
            ("deployment_handler.py", "_delete_managed_resource"),
            ("deployment_handler.py", "_revoke_client_on_kept_gateways"),
            ("deployment_handler.py", "_run_delete_cleanup"),
            ("services/deployment.py", "deploy"),
            ("services/gateway_deployer.py", "cleanup_gateway_resources"),
            ("services/gateway_deployer.py", "deploy_gateway"),
            # The retry-recreate's delete of the gateway it is about to replace.
            ("services/gateway_deployer.py", "deploy_gateway"),
            ("services/policy_promoter.py", "try_promote_to_enforce"),
            ("step_handlers/policy_step.py", "handler"),
            ("step_handlers/status_update_step.py", "_cleanup_resource"),
        ]
    )


# --- the race it closes -------------------------------------------------------------


@contextlib.contextmanager
def _no_lock(ctrl, _region, gateway_id, **_k):
    """The mutant: the same read-back discipline, no mutual exclusion."""
    yield gml.GatewayLock(ctrl, gateway_id, lambda _s: None)


@pytest.mark.parametrize("locked", [True, False], ids=["with-the-lock", "mutant-without-it"])
def test_a_stale_redeploy_cannot_undo_a_revoke_the_teardown_reported(monkeypatch, gateway_lock_table, clock, locked):
    """Redeploy reads [c2, c1], teardown revokes c1, redeploy sends its stale list.

    The invariant: when the teardown says the gateway no longer allows c1, it does not.
    With the lock the teardown cannot run inside the redeploy's read-to-write window, so
    it fails honestly and its retry revokes for real. Without it, it reports a revoke
    that the redeploy's update then undoes -- the mutant run proves this test sees that.
    """
    from app import deployment_handler

    gw = _Gateway(["client-2", "client-1"])
    monkeypatch.setattr(
        deployment_handler, "assert_agentcore_resource_owned", lambda c, _t, i, _r: c.get_gateway(gatewayIdentifier=i)
    )
    if not locked:
        monkeypatch.setattr(deployment_handler, "gateway_mutation_lock", _no_lock)
    kept = [{"id": "gw-shared", "region": "us-east-1"}]

    def _teardown():
        return deployment_handler._revoke_client_on_kept_gateways("client-1", kept, lambda _g: gw)

    def _redeploy(between):
        lock = gml.gateway_mutation_lock if locked else _no_lock
        with lock(gw, "us-east-1", "gw-shared") as lk:
            detail = lk.read()
            stale = gml.allowed_clients(detail) + ["client-3"]
            outcome = between()
            lk.update(_with_clients(detail, stale), _clients_are(stale))
        return outcome

    messages, failed = _redeploy(_teardown)
    reported_revoked = any("no longer allows client client-1" in m for m in messages)
    still_listed = "client-1" in gml.allowed_clients(gw.detail)

    if locked:
        assert failed and not reported_revoked
        assert any("GatewayMutationBusy" in m for m in messages)
        # The teardown's retry, once the redeploy is done, revokes for real.
        messages, failed = _teardown()
        assert not failed and "client-1" not in gml.allowed_clients(gw.detail)
        assert gml.allowed_clients(gw.detail) == ["client-2", "client-3"]
        assert gateway_lock_table.items == {}
    else:
        # The defect itself: a revoke reported done, and the client allowed again.
        assert reported_revoked and not failed and still_listed
