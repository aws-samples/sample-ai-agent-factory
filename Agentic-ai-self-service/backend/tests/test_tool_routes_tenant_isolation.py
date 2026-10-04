"""The four async tool routes: who owns a row, and what a failure may say.

None of ``POST /api/generate-tool``, ``GET /api/generate-tool/{job_id}``,
``POST /api/test-tool`` or ``GET /api/test-tool/{test_id}`` had a single test
before this file, which is how all four shipped with:

1. no tenant recorded on the row they create, so the ownership question was
   unanswerable as well as unasked;
2. no ownership check on either poll route, so any authenticated caller holding
   an id read another tenant's prompt, generated tool source and test output --
   and an id is not a credential;
3. no TTL, so every tool anyone ever generated stayed in the deployments table
   permanently;
4. raw exception text published to the caller through the row's ``error`` field
   and through the POST's own 500.

ARCC cnt_Yq9sVcaZyQniIv ("Prevent data leakage in generative AI systems") names
(2) in its threat -- "users may be able to view other users' content and session
histories" -- and its manual verification step is exactly this file: *test that
the session state of a given user is not accessible to another user and that IDOR
is not possible*. ARCC cnt_94E30Xo4RZHtSJ ("Handle all errors and return generic
error messages") covers (4), and its existence-oracle rule is why every refusal
below is a 404 whose body is byte-identical to the genuine not-found body.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")

import pytest
from app import deployment_handler as dh
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
OTHER = "54381418-7021-708e-4f3b-30505a2b82ec"

# Benign: this file never asks the platform to run code, it only checks who may
# read the row. The safety of the code itself is test_tool_sandbox_boundary.py.
CODE = "def lambda_handler(event, context):\n    return {'ok': True}\n"


def _client(sub: str | None) -> TestClient:
    """A TestClient whose requests carry ``sub`` the way API Gateway delivers it.

    ``_get_user_id`` reads ``request.scope["aws.event"]``, which Mangum populates
    from the HTTP API's JWT authorizer. Injecting at the ASGI layer rather than
    calling the route function directly keeps FastAPI's parameter binding in the
    loop, so a ``raw_request`` that FastAPI mis-binds as a body field fails these
    tests instead of passing them.
    """
    # A standard user's groups even with no sub, so a missing identity is refused by the
    # ownership check under test rather than by the scope check in front of it.
    claims = {"cognito:groups": ["g-users-default"], **({"sub": sub} if sub else {})}
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}

    async def _inject(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "aws.event": event}
        await dh.deployment_app(scope, receive, send)

    return TestClient(_inject)


class _Table:
    """Just enough DynamoDB table for these routes."""

    def __init__(self, item: dict | None = None):
        self.item = item
        self.puts: list[dict] = []
        self.updates: list[dict] = []

    def put_item(self, Item):  # noqa: N803 - boto3's parameter name
        self.puts.append(Item)

    def get_item(self, Key):  # noqa: N803 - boto3's parameter name
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def table(monkeypatch):
    """Route every handler in the module at one fake table."""
    t = _Table()
    monkeypatch.setattr(dh, "_get_deploy_table", lambda: t)
    return t


@pytest.fixture
def no_async_invoke(monkeypatch):
    """Stub the self-invoke so POSTing does not require Lambda."""
    calls: list[dict] = []

    class _Lambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(dh.boto3, "client", lambda *a, **k: _Lambda())
    return calls


def _test_row(owner: str | None, **extra) -> dict:
    row = {"deployment_id": "test-abc123", "status": "completed", "success": True, "results_json": "[]"}
    if owner:
        row["user_id"] = owner
    row.update(extra)
    return row


def _job_row(owner: str | None, **extra) -> dict:
    row = {
        "deployment_id": "gen-abc123",
        "status": "completed",
        "success": True,
        "tool_json": json.dumps({"toolName": "secret_tool", "lambdaCode": CODE}),
        "message": "done",
    }
    if owner:
        row["user_id"] = owner
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# The rows now name their owner
# ---------------------------------------------------------------------------


class TestTheRowRecordsWhoAskedForIt:
    def test_test_tool_stamps_the_caller(self, table, no_async_invoke):
        resp = _client(OWNER).post(
            "/api/test-tool",
            json={"lambdaCode": CODE, "testCases": [{"name": "t", "input": {}}]},
        )
        assert resp.status_code == 200
        assert table.puts[0]["user_id"] == OWNER

    def test_generate_tool_stamps_the_caller(self, table, no_async_invoke):
        # conversation_history non-empty selects generation (async) mode; the
        # clarification path is synchronous and writes no row at all.
        resp = _client(OWNER).post(
            "/api/generate-tool",
            json={"prompt": "a tool", "conversationHistory": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert table.puts[0]["user_id"] == OWNER

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/test-tool", {"lambdaCode": CODE, "testCases": []}),
            ("/api/generate-tool", {"prompt": "x", "conversationHistory": [{"role": "user", "content": "hi"}]}),
        ],
    )
    def test_the_row_expires(self, table, no_async_invoke, path, body):
        """A scratch row with no TTL is a permanent copy of customer content."""
        import time

        _client(OWNER).post(path, json=body)
        ttl = table.puts[0]["ttl"]
        assert isinstance(ttl, int)
        # Roughly a day out: far enough for the client's poll, not indefinite.
        assert 0 < ttl - int(time.time()) <= 86400 + 60

    def test_an_unauthenticated_caller_does_not_get_a_null_owner(self, table, no_async_invoke):
        """Omit the attribute rather than writing None.

        ``user_id`` backs a GSI, and a NULL key value is rejected outright (Bug
        111). Writing None would make the POST fail rather than the read.
        """
        _client(None).post("/api/test-tool", json={"lambdaCode": CODE, "testCases": []})
        assert "user_id" not in table.puts[0]


# ---------------------------------------------------------------------------
# The poll routes refuse another tenant
# ---------------------------------------------------------------------------


class TestAnotherTenantCannotReadTheResult:
    def test_test_result_is_refused(self, table):
        table.item = _test_row(OWNER)
        resp = _client(OTHER).get("/api/test-tool/test-abc123")
        assert resp.status_code == 404

    def test_generate_result_is_refused(self, table):
        table.item = _job_row(OWNER)
        resp = _client(OTHER).get("/api/generate-tool/gen-abc123")
        assert resp.status_code == 404
        # The generated source is the payload that must not cross tenants.
        assert "secret_tool" not in resp.text

    def test_an_unauthenticated_caller_is_refused(self, table):
        """``_get_user_id`` returning None must not compare equal to an owner."""
        table.item = _test_row(OWNER)
        assert _client(None).get("/api/test-tool/test-abc123").status_code == 404

    @pytest.mark.parametrize(
        ("path", "row", "missing_id"),
        [
            ("/api/test-tool/{}", _test_row(OWNER), "test-doesnotexist"),
            ("/api/generate-tool/{}", _job_row(OWNER), "gen-doesnotexist"),
        ],
    )
    def test_the_refusal_is_not_an_existence_oracle(self, monkeypatch, path, row, missing_id):
        """A 403, or a distinguishable 404 body, would let a caller enumerate ids.

        ARCC cnt_94E30Xo4RZHtSJ: an unauthorized caller must not be able to tell
        "exists but not yours" from "does not exist".
        """
        forbidden = _Table(row)
        monkeypatch.setattr(dh, "_get_deploy_table", lambda: forbidden)
        denied = _client(OTHER).get(path.format(row["deployment_id"]))

        absent = _Table(None)
        monkeypatch.setattr(dh, "_get_deploy_table", lambda: absent)
        notfound = _client(OTHER).get(path.format(missing_id))

        assert denied.status_code == notfound.status_code == 404
        assert denied.json() == notfound.json()

    def test_the_owner_still_reads_their_own_result(self, table):
        """The happy path. A check that refuses everyone is a feature outage."""
        table.item = _test_row(OWNER)
        resp = _client(OWNER).get("/api/test-tool/test-abc123")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_the_owner_still_reads_their_own_job(self, table):
        table.item = _job_row(OWNER)
        resp = _client(OWNER).get("/api/generate-tool/gen-abc123")
        assert resp.status_code == 200
        assert resp.json()["tool"]["toolName"] == "secret_tool"

    def test_a_running_row_is_still_pollable_by_its_owner(self, table):
        table.item = {"deployment_id": "test-abc123", "status": "running", "user_id": OWNER}
        assert _client(OWNER).get("/api/test-tool/test-abc123").json()["status"] == "running"

    def test_a_running_row_is_refused_to_another_tenant(self, table):
        table.item = {"deployment_id": "test-abc123", "status": "running", "user_id": OWNER}
        assert _client(OTHER).get("/api/test-tool/test-abc123").status_code == 404

    def test_a_row_with_no_owner_is_refused(self, table):
        """This route fails CLOSED on an ownerless row, unlike handle_deploy_status.

        The first version of the check copied that route's pre-tenancy carve-out and
        justified it as "narrow by construction: these rows expire within a day".
        The justification was false and a reviewer caught it: the TTL is written by
        the new code, so a row created *before* the fix has no ``ttl`` attribute,
        DynamoDB never expires it, and an absent ``user_id`` left it readable by
        every tenant indefinitely.

        Failing closed costs one retry for whatever was in flight during the deploy,
        because a scratch row lives for seconds. A deployment *state* row is
        long-lived, which is why the carve-out is right there and wrong here.
        """
        table.item = _test_row(None)
        assert _client(OTHER).get("/api/test-tool/test-abc123").status_code == 404

    def test_a_job_with_no_owner_is_refused(self, table):
        table.item = _job_row(None)
        resp = _client(OTHER).get("/api/generate-tool/gen-abc123")
        assert resp.status_code == 404
        assert "secret_tool" not in resp.text

    def test_an_ownerless_row_is_refused_even_to_its_likely_creator(self, table):
        """No caller is privileged over an ownerless row, not even the right one.

        There is no way to tell from the row who created it -- that is the entire
        defect -- so "the owner can still read it" cannot be implemented, only
        guessed at.
        """
        table.item = _test_row(None)
        assert _client(OWNER).get("/api/test-tool/test-abc123").status_code == 404

    def test_an_unauthenticated_caller_cannot_read_an_ownerless_row(self, table):
        """The ``None == None`` trap, asserted directly.

        With no caller identity and no owner on the row, a single ``!=`` comparison
        is False and lets an unauthenticated caller read exactly the rows this
        change exists to refuse. An absent value must never compare equal to a
        missing one.
        """
        table.item = _test_row(None)
        assert _client(None).get("/api/test-tool/test-abc123").status_code == 404
        table.item = _job_row(None)
        assert _client(None).get("/api/generate-tool/gen-abc123").status_code == 404

    def test_an_empty_string_owner_is_not_a_match(self, table):
        """A falsy-but-present owner must not be satisfiable by a falsy caller."""
        table.item = _test_row(None)
        table.item["user_id"] = ""
        assert _client(None).get("/api/test-tool/test-abc123").status_code == 404
        assert _client(OWNER).get("/api/test-tool/test-abc123").status_code == 404


# ---------------------------------------------------------------------------
# What a failure is allowed to tell the caller
# ---------------------------------------------------------------------------


class TestAFailureDoesNotDescribeTheInternals:
    def test_the_post_500_carries_no_exception_text(self, monkeypatch, table):
        """This returned f"{type(e).__name__}: {e}" to the client."""

        def _boom(*a, **k):
            raise RuntimeError("arn:aws:iam::166827918465:role/AgentCore-Secret-Role does not exist")

        monkeypatch.setattr(table, "put_item", _boom)
        resp = _client(OWNER).post("/api/test-tool", json={"lambdaCode": CODE, "testCases": []})
        assert resp.status_code == 500
        assert "RuntimeError" not in resp.text
        assert "166827918465" not in resp.text
        assert "AgentCore-Secret-Role" not in resp.text

    def test_the_async_test_failure_is_generic(self, monkeypatch, table):
        """The poll route republishes the row's ``error`` verbatim, so it must be."""
        monkeypatch.setattr(
            dh,
            "test_tool",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("AccessDenied for arn:aws:iam::166827918465:role/x")),
        )
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})
        stored = table.updates[-1]["ExpressionAttributeValues"][":e"]
        assert "166827918465" not in stored
        assert "AccessDenied" not in stored
        assert stored

    def test_the_async_generate_failure_is_generic(self, monkeypatch, table):
        monkeypatch.setattr(
            dh,
            "generate_tool",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("bedrock:InvokeModel denied on model arn:aws:x")),
        )
        dh._handle_async_generate({"job_id": "gen-abc123", "prompt": "x", "region": "us-east-1"})
        stored = table.updates[-1]["ExpressionAttributeValues"][":e"]
        assert "InvokeModel" not in stored
        assert "arn:aws" not in stored
        assert stored

    def test_a_refusal_of_the_submitted_code_still_reaches_the_caller(self, monkeypatch, table):
        """The genericization must not swallow the user's own actionable error.

        A blocked import is a result, not an exception, and the reason names the
        caller's own code -- not our internals. If this ever stops being returned,
        the tool tester becomes a silent "failed" with no way to fix it.
        """
        monkeypatch.setattr(
            dh,
            "test_tool",
            lambda **kw: {"success": False, "error": "Code safety validation failed: Blocked import: os"},
        )
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})
        stored = table.updates[-1]["ExpressionAttributeValues"][":e"]
        assert stored == "Code safety validation failed: Blocked import: os"


class TestALegacyRowAcquiresAnExpiry:
    """A row created before the TTL shipped has none, and would live forever.

    Refusing to read it (above) stops the cross-tenant leak but does not stop the
    platform from keeping the prompt and the generated source indefinitely. Every
    completion update therefore stamps ``ttl`` as well, so any legacy row still in
    flight acquires an expiry.
    """

    @pytest.mark.parametrize("failing", [False, True])
    def test_the_test_completion_stamps_a_ttl(self, monkeypatch, table, failing):
        if failing:
            monkeypatch.setattr(dh, "test_tool", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        else:
            monkeypatch.setattr(dh, "test_tool", lambda **kw: {"success": True, "allPassed": True})
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})
        upd = table.updates[-1]
        assert upd["ExpressionAttributeNames"]["#ttl"] == "ttl"
        assert "#ttl = :ttl" in upd["UpdateExpression"]
        assert isinstance(upd["ExpressionAttributeValues"][":ttl"], int)

    @pytest.mark.parametrize("failing", [False, True])
    def test_the_generate_completion_stamps_a_ttl(self, monkeypatch, table, failing):
        if failing:
            monkeypatch.setattr(dh, "generate_tool", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        else:
            monkeypatch.setattr(dh, "generate_tool", lambda **kw: {"success": True, "tool": {"toolName": "t"}})
        dh._handle_async_generate({"job_id": "gen-abc123", "prompt": "x", "region": "us-east-1"})
        upd = table.updates[-1]
        assert upd["ExpressionAttributeNames"]["#ttl"] == "ttl"
        assert "#ttl = :ttl" in upd["UpdateExpression"]
        assert isinstance(upd["ExpressionAttributeValues"][":ttl"], int)


# ---------------------------------------------------------------------------
# The sandbox posture has to survive the round trip
# ---------------------------------------------------------------------------


class TestTheSandboxPostureReachesTheCaller:
    """``sandboxIsolated`` and ``note`` are produced by ``test_tool`` and consumed
    by the UI two HTTP requests later, so every link in that chain is load-bearing.

    The test runs in a Lambda, the result is written to a row, and the browser
    reads the row on a later poll. A value that only exists in ``test_tool``'s
    return is a value nobody ever sees -- and the thing it explains (an isolated
    sandbox failing a correct HTTP tool, ARCC cnt_MSVB0Kk8WMwmmW) is exactly the
    case where an unexplained result makes a user rewrite working code.
    """

    def test_the_completion_persists_both_fields(self, monkeypatch, table):
        monkeypatch.setattr(
            dh,
            "test_tool",
            lambda **kw: {
                "success": True,
                "allPassed": False,
                "results": [],
                "sandboxIsolated": True,
                "note": "the sandbox has no internet access",
            },
        )
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})

        upd = table.updates[-1]
        assert upd["ExpressionAttributeValues"][":iso"] is True
        assert upd["ExpressionAttributeValues"][":note"] == "the sandbox has no internet access"
        assert "sandbox_isolated = :iso" in upd["UpdateExpression"]

    def test_note_is_written_through_an_expression_attribute_name(self, monkeypatch, table):
        """A literal ``note = :note`` risks a ValidationException on this one update.

        The DynamoDB reserved-word list is long, and the failure mode is silent in
        the worst way: the test would run, the Lambda would finish, and the row
        would stay ``running`` forever because the only write that could complete
        it was rejected.
        """
        monkeypatch.setattr(dh, "test_tool", lambda **kw: {"success": True, "allPassed": True})
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})

        upd = table.updates[-1]
        assert upd["ExpressionAttributeNames"]["#note"] == "note"
        assert "#note = :note" in upd["UpdateExpression"]
        # No bare attribute name anywhere in the expression.
        assert " note = " not in upd["UpdateExpression"]

    def test_a_result_without_the_fields_stores_none_rather_than_failing(self, monkeypatch, table):
        """``test_tool``'s failure paths return neither field on purpose.

        They never reached the sandbox, so "was it isolated?" has no answer. The
        completion update must still succeed, and must store the absence rather
        than inventing ``False``.
        """
        monkeypatch.setattr(
            dh, "test_tool", lambda **kw: {"success": False, "error": "Tool testing is disabled until ..."}
        )
        dh._handle_async_test({"test_id": "test-abc123", "lambda_code": CODE, "test_cases": [], "region": "us-east-1"})

        vals = table.updates[-1]["ExpressionAttributeValues"]
        assert vals[":iso"] is None
        assert vals[":note"] is None

    def test_the_poll_route_publishes_both(self, table):
        table.item = _test_row(OWNER, sandbox_isolated=True, note="no internet access by design")
        body = _client(OWNER).get("/api/test-tool/test-abc123").json()
        assert body["sandboxIsolated"] is True
        assert body["note"] == "no internet access by design"

    def test_an_un_isolated_run_is_reported_as_such_not_omitted(self, table):
        """``False`` and absent mean different things to the UI, so both must survive."""
        table.item = _test_row(OWNER, sandbox_isolated=False)
        body = _client(OWNER).get("/api/test-tool/test-abc123").json()
        assert body["sandboxIsolated"] is False
        assert body["note"] is None

    def test_a_legacy_row_reports_an_unknown_posture(self, table):
        """A row written before the field existed must not read as "not isolated".

        ``None`` travels to the frontend as ``undefined``, which the UI renders as
        nothing at all. Defaulting it to ``False`` here would claim, of a run whose
        posture we do not know, that it had full egress.
        """
        table.item = _test_row(OWNER)
        body = _client(OWNER).get("/api/test-tool/test-abc123").json()
        assert body["sandboxIsolated"] is None
        assert body["note"] is None

    def test_the_posture_is_not_leaked_to_another_tenant(self, table):
        """Two new published fields are two new things the ownership check must cover."""
        table.item = _test_row(OTHER, sandbox_isolated=True, note="no internet access by design")
        resp = _client(OWNER).get("/api/test-tool/test-abc123")
        assert resp.status_code == 404
        assert "internet" not in resp.text
