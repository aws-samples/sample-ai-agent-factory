"""F-66e, the delete half: DeleteGateway only under the gateway's write lock.

Every UpdateGateway already held a lock keyed by the gateway id from its read to the
read-back proving it landed. DeleteGateway did not: four call sites sent it bare, so a
teardown could delete a gateway inside another writer's read-to-write window, and a
redeploy could write to a gateway already being deleted. ``GatewayLock.delete`` is now
the only caller of DeleteGateway, holds the lock from the ownership read to the proof
of absence, and keeps it whenever the outcome is unknown.

The error classifier is shared with ``update``: an allowlist of codes that prove the
service refused the request releases the lock; a 5xx, throttling, an unlisted code or
a transport failure may have landed, and keeps it.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.services import gateway_mutation_lock as gml
from app.services.deletion_confirmation import DeletionFailedAfterAccept
from app.services.resource_ownership import ResourceDeletionRefused
from botocore.exceptions import ClientError, EndpointConnectionError

KEY = "gwlock#us-east-1#gw-1"
SRC = Path(__file__).resolve().parents[1] / "src" / "app"


def _ce(code: str, op: str = "DeleteGateway", status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept += s
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(gml, "time", SimpleNamespace(time=c.time, sleep=c.sleep))
    # Every other sleep (target propagation, confirmation polls) costs nothing; the
    # lock's own wait runs on the fake clock above, so a busy lock still times out.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return c


class _Gateway:
    """A gateway with no targets that is gone ``lag`` reads after an accepted delete."""

    def __init__(self, *, delete_error=None, lag=1, status_after_delete="DELETING"):
        self.deleted_at = None
        self.reads_after = 0
        self.delete_error = delete_error
        self.lag = lag
        self.status_after_delete = status_after_delete
        self.deletes = 0
        self.updates = []
        self.on_read = None

    def get_gateway(self, **_k):
        if self.on_read:
            self.on_read()
        if self.deleted_at is not None:
            self.reads_after += 1
            if self.lag is not None and self.reads_after > self.lag:
                raise _ce("ResourceNotFoundException", "GetGateway", 404)
            return {"gatewayId": "gw-1", "status": self.status_after_delete}
        return {"gatewayId": "gw-1", "status": "READY", "authorizerConfiguration": {}}

    def delete_gateway(self, **_k):
        self.deletes += 1
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted_at = self.deletes

    def update_gateway(self, **request):
        self.updates.append(request)

    def list_gateway_targets(self, **_k):
        return {"items": []}

    def delete_gateway_target(self, **_k):
        raise AssertionError("no targets")


def _absent(gw):
    def _confirm():
        from app.services.deletion_confirmation import wait_until_absent

        wait_until_absent(
            resource_label="gateway gw-1",
            read=lambda: gw.get_gateway(gatewayIdentifier="gw-1"),
            max_attempts=4,
            delay_seconds=0,
        )

    return _confirm


# --- the classifier -----------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "AccessDeniedException",
        "ConflictException",
        "ExpiredTokenException",
        "ResourceNotFoundException",
        "ServiceQuotaExceededException",
        "UnrecognizedClientException",
        "ValidationException",
    ],
)
def test_a_listed_code_is_a_definitive_refusal(code):
    assert gml.definitive_refusal(_ce(code))


@pytest.mark.parametrize(
    "exc",
    [
        _ce("ThrottlingException", status=429),
        _ce("InternalServerException", status=500),
        _ce("ServiceUnavailableException", status=503),
        _ce("SomethingNobodyReviewed"),
        EndpointConnectionError(endpoint_url="https://x"),
        TimeoutError("read timeout"),
    ],
    ids=["throttling", "5xx", "503", "unlisted", "connection", "timeout"],
)
def test_anything_else_may_have_landed(exc):
    assert not gml.definitive_refusal(exc)


@pytest.mark.parametrize(
    "exc",
    [_ce("ThrottlingException", "UpdateGateway", 429), _ce("InternalServerException", "UpdateGateway", 500)],
    ids=["throttling", "5xx"],
)
def test_an_update_refused_ambiguously_keeps_the_lock(gateway_lock_table, clock, exc):
    """The classifier applies to update too: only a definitive refusal releases."""
    gw = _Gateway()
    gw.update_gateway = lambda **_k: (_ for _ in ()).throw(exc)
    with pytest.raises(gml.GatewayUpdateUnconfirmed) as err:
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.update({"gatewayIdentifier": "gw-1"}, lambda _d: True)
    assert KEY in gateway_lock_table.items
    # The code only: a ClientError's message can echo the request.
    assert str(err.value).endswith(exc.response["Error"]["Code"])


# --- delete ---------------------------------------------------------------------------


def test_a_confirmed_delete_releases_the_lock(gateway_lock_table, clock):
    gw = _Gateway()
    with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
        assert KEY in gateway_lock_table.items
        lk.delete(_absent(gw), terminal=(DeletionFailedAfterAccept,))
    assert gw.deletes == 1
    assert KEY not in gateway_lock_table.items


@pytest.mark.parametrize("code", ["ValidationException", "ConflictException", "ResourceNotFoundException"])
def test_a_refused_delete_propagates_unchanged_and_releases(gateway_lock_table, clock, code):
    """The callers' 'still has targets' retry and not-found handling need the ClientError."""
    gw = _Gateway(delete_error=_ce(code))
    with pytest.raises(ClientError) as err:
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(_absent(gw))
    assert err.value.response["Error"]["Code"] == code
    assert KEY not in gateway_lock_table.items


@pytest.mark.parametrize(
    "exc",
    [_ce("InternalServerException", status=500), _ce("ThrottlingException", status=429), TimeoutError("t")],
    ids=["5xx", "throttling", "timeout"],
)
def test_a_delete_whose_outcome_is_unknown_keeps_the_lock(gateway_lock_table, clock, exc):
    gw = _Gateway(delete_error=exc)
    confirmed = []
    with pytest.raises(gml.GatewayDeleteUnconfirmed):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(lambda: confirmed.append(1))
    assert confirmed == []
    assert KEY in gateway_lock_table.items


def test_an_accepted_delete_never_read_back_absent_keeps_the_lock(gateway_lock_table, clock):
    gw = _Gateway(lag=None)
    with pytest.raises(ResourceDeletionRefused):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(_absent(gw), terminal=(DeletionFailedAfterAccept,))
    assert KEY in gateway_lock_table.items


def test_a_terminal_failure_is_the_services_verdict_and_releases(gateway_lock_table, clock):
    gw = _Gateway(lag=None, status_after_delete="DELETE_FAILED")
    with pytest.raises(DeletionFailedAfterAccept):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(_absent(gw), terminal=(DeletionFailedAfterAccept,))
    assert KEY not in gateway_lock_table.items


def test_a_terminal_type_the_caller_did_not_name_keeps_the_lock(gateway_lock_table, clock):
    """``terminal`` is the caller's statement of which failures are final, not a default."""
    gw = _Gateway(lag=None, status_after_delete="DELETE_FAILED")
    with pytest.raises(DeletionFailedAfterAccept):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(_absent(gw))
    assert KEY in gateway_lock_table.items


# --- one place ------------------------------------------------------------------------


def _parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    return tree


def _enclosing(node, kinds):
    while not isinstance(node, kinds):
        node = node.parent
    return node


def _calls(attr):
    for path in sorted(SRC.rglob("*.py")):
        tree = _parents(ast.parse(path.read_text()))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == attr:
                    yield str(path.relative_to(SRC)), node


def test_delete_gateway_is_called_in_exactly_one_place_the_lock():
    sites = [(p, _enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef)).name) for p, n in _calls("delete_gateway")]
    assert sites == [("services/gateway_mutation_lock.py", "delete")]


def test_the_oracle_sees_the_sites_it_replaced():
    """Reach first: the teardown paths that used to call DeleteGateway now call the lock."""
    sites = {(p, _enclosing(n, (ast.FunctionDef, ast.AsyncFunctionDef)).name) for p, n in _calls("delete")}
    assert {
        ("deployment_handler.py", "_delete_managed_resource"),
        ("step_handlers/status_update_step.py", "_cleanup_resource"),
        ("services/gateway_deployer.py", "cleanup_gateway_resources"),
        ("services/gateway_deployer.py", "deploy_gateway"),
    } <= sites


# --- the races, at the real teardown sites --------------------------------------------


@contextlib.contextmanager
def _no_lock(ctrl, _region, gateway_id, **_k):
    """The mutant: the same delete-and-confirm discipline, no mutual exclusion."""
    yield gml.GatewayLock(ctrl, gateway_id, lambda _s: None)


def _manifest_teardown(monkeypatch, gw, locked):
    import boto3
    from app import deployment_handler as dh

    monkeypatch.setattr(dh, "assert_agentcore_resource_owned", lambda *_a, **_k: None)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: gw)
    monkeypatch.setattr("app.services.step_clients.client", lambda *_a, **_k: gw)
    if not locked:
        monkeypatch.setattr(dh, "gateway_mutation_lock", _no_lock)
    return lambda: dh._delete_managed_resource({"type": "gateway", "id": "gw-1", "region": "us-east-1"}, "us-east-1")


def _failure_cleanup(monkeypatch, gw, locked):
    from app.step_handlers import status_update_step as su

    monkeypatch.setattr(su, "assert_agentcore_resource_owned", lambda *_a, **_k: None)
    monkeypatch.setattr(su.step_clients, "client", lambda *_a, **_k: gw)
    if not locked:
        monkeypatch.setattr(su, "gateway_mutation_lock", _no_lock)
    return lambda: su._cleanup_resource({"type": "gateway", "id": "gw-1", "region": "us-east-1"}, "us-east-1", {})


def _abort_cleanup(monkeypatch, gw, locked):
    from app.services import gateway_deployer as gd

    monkeypatch.setattr(gd, "assert_agentcore_resource_owned", lambda *_a, **_k: None)
    monkeypatch.setattr(gd, "_create_agentcore_control_client", lambda _r: gw)
    if not locked:
        monkeypatch.setattr(gd, "gateway_mutation_lock", _no_lock)

    def _run():
        log = gd.cleanup_gateway_resources(
            "rt-1", "us-east-1", {"gateway_id": "gw-1", "gateway_created_by_deployment": True}
        )
        if not any("gw-1 confirmed deleted" in m for m in log):
            raise RuntimeError("; ".join(log))
        return log

    return _run


SITES = [_manifest_teardown, _failure_cleanup, _abort_cleanup]
IDS = ["manifest-teardown", "failure-cleanup", "abort-cleanup"]


@pytest.mark.parametrize("locked", [True, False], ids=["with-the-lock", "mutant-without-it"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_update_first_a_delete_inside_a_writers_window_waits_and_refuses(
    monkeypatch, gateway_lock_table, clock, site, locked
):
    """A redeploy holds the lock between its read and its update; the teardown runs then.

    The invariant: no DeleteGateway is sent while another writer's read-modify-write is
    open. With the lock the teardown waits, then fails as busy and deletes nothing; its
    retry after the writer is done deletes for real. The mutant deletes inside the window.
    """
    gw = _Gateway()
    teardown = site(monkeypatch, gw, locked)
    lock = gml.gateway_mutation_lock if locked else _no_lock
    outcome = {}
    with lock(gw, "us-east-1", "gw-1") as lk:
        lk.read()
        try:
            teardown()
            outcome["deleted"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
        deletes_inside_window = gw.deletes
    if locked:
        assert deletes_inside_window == 0
        err = outcome["error"]
        # The abort cleanup reports it in its log; the other two raise it.
        assert isinstance(err, gml.GatewayMutationBusy) or "being changed by another operation" in str(err)
        assert gateway_lock_table.items == {}
        teardown()
        assert gw.deletes == 1 and gateway_lock_table.items == {}
    else:
        assert deletes_inside_window == 1


@pytest.mark.parametrize("locked", [True, False], ids=["with-the-lock", "mutant-without-it"])
@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_delete_first_a_writer_cannot_write_while_the_delete_is_being_confirmed(
    monkeypatch, gateway_lock_table, clock, site, locked
):
    """The teardown's delete is accepted; a redeploy tries to repoint during confirmation.

    The invariant: nothing writes to a gateway between its delete and the proof it is gone.
    """
    gw = _Gateway(lag=2)
    teardown = site(monkeypatch, gw, locked)
    lock = gml.gateway_mutation_lock if locked else _no_lock
    writer = {}

    def _redeploy_during_confirmation():
        if gw.deleted_at is None or writer:
            return
        writer["tried"] = True
        try:
            with lock(gw, "us-east-1", "gw-1") as lk:
                lk._ctrl.update_gateway(gatewayIdentifier="gw-1")
        except gml.GatewayMutationBusy:
            writer["busy"] = True

    gw.on_read = _redeploy_during_confirmation
    teardown()
    assert writer.get("tried")
    if locked:
        assert writer.get("busy") and gw.updates == []
        assert gateway_lock_table.items == {}
    else:
        assert gw.updates and not writer.get("busy")


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_an_ambiguous_delete_at_a_real_site_keeps_the_lock_for_the_next_writer(
    monkeypatch, gateway_lock_table, clock, site
):
    gw = _Gateway(delete_error=_ce("InternalServerException", status=500))
    teardown = site(monkeypatch, gw, True)
    with pytest.raises(Exception):  # noqa: B017 - each site reports it its own way
        teardown()
    assert KEY in gateway_lock_table.items
    with pytest.raises(gml.GatewayMutationBusy):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1"):
            pytest.fail("a writer ran while the delete's outcome was unknown")


# --- a kept lock is collected -------------------------------------------------------


def test_a_kept_lock_is_collectable_by_the_tables_ttl_and_never_before_its_lease(gateway_lock_table, clock):
    """A kept lock is never deleted by its holder, so without the table's TTL attribute
    every ambiguous write would leave one row forever. ``gc_after`` is that attribute
    (infra's test_gateway_name_claim_table pins it on the table), and it lies past the
    lease, so the collector cannot free a lock takeover would still refuse.
    """
    from app.services.gateway_name_claim import GC_GRACE_SECONDS

    durable = {"claim_key": "claim#us-east-1#agent-gateway", "holder_deployment_id": "d-1"}
    gateway_lock_table.items[durable["claim_key"]] = dict(durable)
    gw = _Gateway(delete_error=_ce("InternalServerException", status=500))
    with pytest.raises(gml.GatewayDeleteUnconfirmed):
        with gml.gateway_mutation_lock(gw, "us-east-1", "gw-1") as lk:
            lk.delete(_absent(gw))

    row = gateway_lock_table.items[KEY]
    assert row["gc_after"] == row["lock_expires_at"] + GC_GRACE_SECONDS
    assert row["gc_after"] > row["lock_expires_at"] > clock.now
    # The lock wrote its own row only: a durable name claim carries no gc_after, so
    # the TTL never expires it, and a lock write must not give it one.
    assert gateway_lock_table.items[durable["claim_key"]] == durable
    assert {k for op, k in gateway_lock_table.calls if op == "put_item"} == {KEY}
