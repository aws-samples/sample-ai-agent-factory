"""Peer finding F-9: a missing deployment record was a free pass to any runtime.

THE DEFECT. Four places took `runtime_id` from the caller, looked up the deployment
record, and then wrote the tenant check as `if deployment_state: ...`. When the lookup
found nothing the check was skipped entirely and the code went on to *synthesize* the ARN
from the caller-supplied id:

    runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"

So any authenticated caller could name any runtime in the account and have the platform
invoke it -- another tenant's, or one this platform never created (this account really
does hold foreign runtimes). The delete path was worse: it called `destroy_runtime` on
the id with no record and no owner to compare against, which is unauthenticated-by-record
deletion of someone else's runtime.

And the bypass did not even need a runtime with no record: each site set the record to
None inside `except Exception`, so anything that made the lookup fail turned the check
off. A check you can disable by breaking its input is not a check.

THE FIX, and why it is shaped this way. A record is now REQUIRED. No record -> 404 with
the same opaque body the cross-tenant case returns, so a caller cannot use the status code
to tell "exists but is not yours" from "not recorded here". A lookup that *failed* is a
503, not a 404, because "we could not tell" must not be reported as "it does not exist" --
and, more importantly, must not be reported as success.

The ARN synthesis itself is kept. Once the record is present and owned, a deploy that died
before persisting `runtime_arn` is a real case, and deriving the ARN for an id we have
just authorized is fine. What was wrong was authorizing nothing at all.

IMPORTED RUNTIMES. `POST /api/runtime/import` adopts an externally-built runtime, and its
docstring claimed "teardown of an imported runtime is the same DELETE path (the caller
opts in there)". There was no opt-in -- DELETE destroyed it like anything else, so
import-then-delete let a caller destroy a runtime this platform never created. The opt-in
now exists (`?destroy=true`), defaults to off, and the record carries `imported`.

The happy paths are asserted here as loudly as the refusals, because a gate tested only by
what it rejects is compatible with rejecting everything.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

import app.deployment_handler as dh  # noqa: E402
import app.stream_handler as sh  # noqa: E402
from app.services import step_clients  # noqa: E402

RUNTIME = "victim_runtime_abc123"
OWNER = "owner-sub-1111"
ATTACKER = "attacker-sub-2222"
ACCOUNT = "123456789012"
# Read the region off config rather than hardcoding one: the handler synthesizes the ARN
# with config.aws_region, which follows the ambient AWS_REGION (us-west-2 on this
# machine, us-east-1 in the deployed stack). Hardcoding it made this assert a property of
# the developer's shell.
REGION = dh.config.aws_region
SYNTHESIZED_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME}"


def _record(**over) -> dict:
    rec = {
        "deployment_id": "dep-1",
        "workflow_id": "wf-1",
        "user_id": OWNER,
        "runtime_id": RUNTIME,
        "status": "succeeded",
    }
    rec.update(over)
    return rec


class _Store:
    def __init__(self):
        self._table = object()

    def get(self, _id):
        return None


def _fake_boto3_client(_service, *a, **k):
    m = MagicMock()
    m.get_caller_identity.return_value = {"Account": ACCOUNT}
    return m


# The fail-closed body for "the lookup itself broke". Asserted literally rather than by
# substring: the point of the 503 is that it is NOT the 404 text, so a test that would
# also pass on "Runtime not found" would not be testing anything.
UNVERIFIABLE = "Could not verify this runtime right now. Try again shortly."


@pytest.fixture
def invoker(monkeypatch):
    """TestClient for /api/test-runtime with the AWS edge captured, not called."""
    calls: list[dict] = []
    client_mock = MagicMock()

    def _invoke(**kwargs):
        calls.append(kwargs)
        return {"response": "ok", "runtimeSessionId": "s1", "statusCode": 200}

    client_mock.invoke_agent_runtime.side_effect = _invoke

    class _Session:
        def client(self, service, **_kwargs):
            if service == "bedrock-agentcore":
                return client_mock
            if service == "sts":
                sts = MagicMock()
                sts.get_caller_identity.return_value = {"Account": ACCOUNT}
                return sts
            raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(step_clients, "session_for_event", lambda _event: _Session())
    monkeypatch.setattr(dh.boto3, "client", _fake_boto3_client)
    c = TestClient(dh.deployment_app)
    c._invokes = calls  # type: ignore[attr-defined]
    return c


def _post(c: TestClient, sub: str):
    with patch.object(dh, "_get_user_id", lambda req: sub):
        return c.post("/api/test-runtime", json={"input": "hi", "runtimeId": RUNTIME})


# ---------------------------------------------------------------------------
# /api/test-runtime — the invoke sink
# ---------------------------------------------------------------------------


def test_invoke_with_no_deployment_record_is_refused(invoker, monkeypatch):
    """The defect itself: no record used to mean "synthesize the ARN and invoke"."""
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    r = _post(invoker, ATTACKER)
    assert r.status_code == 404, r.text
    assert invoker._invokes == [], "a runtime with no record was invoked"


def test_the_refusal_does_not_reveal_whether_the_runtime_exists(invoker, monkeypatch):
    """A 404 for "not recorded" must be indistinguishable from "not yours"."""
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    missing = _post(invoker, ATTACKER)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record())
    not_mine = _post(invoker, ATTACKER)
    assert missing.status_code == not_mine.status_code == 404
    assert missing.json() == not_mine.json(), "the bodies differ, so the id is enumerable"


def test_a_failed_lookup_does_not_turn_the_check_off(invoker, monkeypatch):
    """The bypass that needed no unrecorded runtime: break the lookup instead.

    Fail CLOSED and say so with a 503 -- reporting "not found" would be a lie, and
    reporting success would be the original defect.
    """

    def _boom(table, rid):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(dh, "_scan_for_runtime", _boom)
    r = _post(invoker, ATTACKER)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == UNVERIFIABLE
    assert invoker._invokes == []


def test_another_tenants_runtime_is_still_refused(invoker, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record())
    assert _post(invoker, ATTACKER).status_code == 404
    assert invoker._invokes == []


def test_the_owner_can_still_invoke_a_record_that_has_no_arn_yet(invoker, monkeypatch):
    """The happy path the fix must not break, and the reason ARN synthesis stays.

    A deploy that failed after creating the runtime but before persisting
    ``runtime_arn`` is a real record. Once ownership is established, deriving the ARN
    for that id is fine -- what was wrong was deriving it for nobody.
    """
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(runtime_arn=""))
    r = _post(invoker, OWNER)
    assert r.status_code == 200, r.text
    assert len(invoker._invokes) == 1
    assert invoker._invokes[0]["agentRuntimeArn"] == SYNTHESIZED_ARN


def test_the_owner_can_still_invoke_a_record_that_has_an_arn(invoker, monkeypatch):
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME}"
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(runtime_arn=arn))
    assert _post(invoker, OWNER).status_code == 200
    assert invoker._invokes[0]["agentRuntimeArn"] == arn


def test_a_legacy_record_with_no_owner_is_still_usable(invoker, monkeypatch):
    """Pre-tenancy records carry user_id=None and must keep working -- that carve-out
    is deliberate and documented at the call site. It is the *absence of a record*
    that stops being a free pass, not the absence of an owner on one."""
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(user_id=None))
    assert _post(invoker, ATTACKER).status_code == 200


# ---------------------------------------------------------------------------
# The Function URL streaming path (stream_handler)
# ---------------------------------------------------------------------------


def _stream(sub: str, record, monkeypatch) -> tuple[str, list]:
    """Drive _stream_invoke and return (everything written, invoke calls).

    ``_stream_invoke`` builds its bedrock-agentcore client inline (it needs a long
    read timeout), so the only seam is ``boto3.client`` — hand back the recording mock
    for that service and a plain mock for STS. Getting this wrong would make every
    refusal test pass vacuously, which is why the owner test asserts a call HAPPENED.
    """
    written: list[str] = []
    calls: list[dict] = []
    client_mock = MagicMock()

    def _invoke(**kwargs):
        calls.append(kwargs)
        return {"response": "ok", "runtimeSessionId": "s1"}

    client_mock.invoke_agent_runtime.side_effect = _invoke

    class _Session:
        def client(self, service, **_kwargs):
            if service == "bedrock-agentcore":
                return client_mock
            if service == "sts":
                return _fake_boto3_client(service)
            raise AssertionError(f"unexpected service: {service}")

    monkeypatch.setattr(sh, "_get_state_store", lambda: _Store())
    if isinstance(record, Exception):

        def _scan(_t, _r):
            raise record

        monkeypatch.setattr(sh, "_scan_for_runtime", _scan)
    else:
        monkeypatch.setattr(sh, "_scan_for_runtime", lambda t, r: record)
    monkeypatch.setattr(sh.step_clients, "session_for_event", lambda _event: _Session())
    sh._stream_invoke(written.append, {"input": "hi", "runtimeId": RUNTIME}, sub)
    # ``_sse`` writes bytes (the Function URL streams raw frames), so decode rather
    # than assuming str — joining the wrong type raises instead of asserting.
    return b"".join(written).decode(), calls


def test_the_stream_route_also_requires_a_record(monkeypatch):
    out, calls = _stream(ATTACKER, None, monkeypatch)
    assert "Runtime not found" in out
    assert calls == [], "the streaming path invoked a runtime with no record"


def test_the_stream_route_refuses_a_sigv4_caller_with_no_record(monkeypatch):
    """The IAM carve-out is about OWNERSHIP, not existence.

    A SigV4 caller cannot match a Cognito sub, so the owner compare is skipped for it
    -- deliberately. That must not also skip *having a record at all*, or the trusted
    boundary becomes "any signed principal may invoke any runtime in the account".
    """
    out, calls = _stream("iam:AIDAEXAMPLE", None, monkeypatch)
    assert "Runtime not found" in out
    assert calls == []


def test_the_stream_route_fails_closed_on_a_broken_lookup(monkeypatch):
    out, calls = _stream(ATTACKER, RuntimeError("dynamodb unavailable"), monkeypatch)
    assert calls == []
    assert UNVERIFIABLE in out
    assert "dynamodb" not in out, "the internal failure text was echoed to the caller"


def test_the_stream_route_still_serves_the_owner(monkeypatch):
    out, calls = _stream(OWNER, _record(), monkeypatch)
    assert len(calls) == 1, out
    assert calls[0]["agentRuntimeArn"].endswith(f"runtime/{RUNTIME}")


# ---------------------------------------------------------------------------
# /api/test-runtime-stream — the route the browser actually uses
# ---------------------------------------------------------------------------
#
# This is the third sink and the one that matters most in practice: every chat turn in
# the UI goes through it (DeployPanel.tsx:420), whereas the hardened Function URL twin
# above is not wired to the browser at all. It refuses with an SSE error frame rather
# than an HTTP status because that is what the browser's reader consumes, so these
# tests read the frame, not the code.


def _post_stream(c: TestClient, sub: str):
    with patch.object(dh, "_get_user_id", lambda req: sub):
        return c.post("/api/test-runtime-stream", json={"input": "hi", "runtimeId": RUNTIME})


def test_the_browser_stream_route_requires_a_record(invoker, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    r = _post_stream(invoker, ATTACKER)
    assert "Runtime not found" in r.text
    assert invoker._invokes == [], "the UI's own invoke path ran with no record"


def test_the_browser_stream_route_refusals_are_indistinguishable(invoker, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    missing = _post_stream(invoker, ATTACKER).text
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record())
    not_mine = _post_stream(invoker, ATTACKER).text
    assert missing == not_mine, "the frames differ, so the id is enumerable"


def test_the_browser_stream_route_fails_closed_on_a_broken_lookup(invoker, monkeypatch):
    def _boom(table, rid):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(dh, "_scan_for_runtime", _boom)
    r = _post_stream(invoker, ATTACKER)
    assert UNVERIFIABLE in r.text
    assert invoker._invokes == []


def test_the_browser_stream_route_still_serves_the_owner(invoker, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record(runtime_arn=""))
    r = _post_stream(invoker, OWNER)
    assert r.status_code == 200, r.text
    assert len(invoker._invokes) == 1, r.text
    assert invoker._invokes[0]["agentRuntimeArn"].endswith(f"runtime/{RUNTIME}")


# ---------------------------------------------------------------------------
# DELETE /api/runtime/{id} — the destructive sink
# ---------------------------------------------------------------------------


@pytest.fixture
def deleter(monkeypatch):
    destroyed: list[str] = []
    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(dh, "_claim_delete_status", lambda deployment_id: True)
    # This suite owns the deployment-record/tenant authorization boundary.
    # The newer manifest co-residency gate is covered by its dedicated
    # deletion-authority suites; without a neutral result here, this fixture's
    # deliberately tiny table double cannot enumerate peer deployments and the
    # runtime is retained before the mocked destructive sink is reached.
    monkeypatch.setattr(dh, "manifest_delete_refusal", lambda *args, **kwargs: None)
    monkeypatch.setattr(dh, "destroy_runtime", lambda rid, region: destroyed.append(rid) or {"success": True})
    monkeypatch.setattr(dh.boto3, "client", _fake_boto3_client)
    monkeypatch.setattr(dh, "_is_slow_delete", lambda rec: False)
    c = TestClient(dh.deployment_app)
    c._destroyed = destroyed  # type: ignore[attr-defined]
    return c


def _delete(c: TestClient, sub: str, query: str = ""):
    with patch.object(dh, "_get_user_id", lambda req: sub):
        return c.delete(f"/api/runtime/{RUNTIME}{query}")


def test_delete_with_no_record_destroys_nothing(deleter, monkeypatch):
    """The worst instance: a destructive call authorized by nothing."""
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: None)
    r = _delete(deleter, ATTACKER)
    assert r.status_code == 404, r.text
    assert deleter._destroyed == [], "a runtime with no record was DESTROYED"


def test_delete_of_another_tenants_runtime_destroys_nothing(deleter, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record())
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: _record())
    assert _delete(deleter, ATTACKER).status_code == 404
    assert deleter._destroyed == []


def test_delete_fails_closed_when_the_lookup_breaks(deleter, monkeypatch):
    """The destructive sink must never treat "we could not tell" as "go ahead"."""

    def _boom(rid):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(dh, "_lookup_deployment_record", _boom)
    r = _delete(deleter, OWNER)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == UNVERIFIABLE
    assert deleter._destroyed == []


def test_the_owner_can_still_delete(deleter, monkeypatch):
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: _record())
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: _record())
    r = _delete(deleter, OWNER)
    assert r.status_code == 200, r.text
    assert deleter._destroyed == [RUNTIME]


# ---------------------------------------------------------------------------
# Imported runtimes — the opt-in the docstring promised
# ---------------------------------------------------------------------------


def test_deleting_an_imported_runtime_does_not_destroy_it_by_default(deleter, monkeypatch):
    rec = _record(imported=True)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(rec))
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: dict(rec))
    r = _delete(deleter, OWNER)
    assert r.status_code == 200, r.text
    assert deleter._destroyed == [], "an imported runtime was destroyed without opt-in"
    assert "import" in r.json()["message"].lower()


def test_an_imported_runtime_is_destroyed_when_the_caller_opts_in(deleter, monkeypatch):
    rec = _record(imported=True)
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(rec))
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: dict(rec))
    r = _delete(deleter, OWNER, "?destroy=true")
    assert r.status_code == 200, r.text
    assert deleter._destroyed == [RUNTIME]


def test_a_legacy_imported_record_is_recognised_by_its_workflow_id(deleter, monkeypatch):
    """Records written before the `imported` field existed only carry the
    ``imported-<id>`` workflow_id, and they are exactly the ones at risk."""
    rec = _record(workflow_id=f"imported-{RUNTIME}")
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(rec))
    monkeypatch.setattr(dh, "_lookup_deployment_record", lambda rid: dict(rec))
    assert _delete(deleter, OWNER).status_code == 200
    assert deleter._destroyed == []


def test_import_marks_the_record_so_delete_can_see_it(monkeypatch):
    created = {}

    class _ImportStore(_Store):
        def create(self, state):
            created["state"] = state
            return state

    class _Ctrl:
        def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
            return {"agentRuntimeName": agentRuntimeId, "status": "READY"}

    monkeypatch.setattr(dh, "_get_state_store", lambda: _ImportStore())
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: None)
    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: _Ctrl())
    monkeypatch.setattr(dh, "_get_user_id", lambda req: OWNER)
    c = TestClient(dh.deployment_app)
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{RUNTIME}"
    r = c.post("/api/runtime/import", json={"runtimeArn": arn})
    assert r.status_code == 201, r.text
    assert created["state"].imported is True
