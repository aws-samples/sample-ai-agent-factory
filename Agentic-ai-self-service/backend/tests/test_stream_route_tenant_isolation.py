"""``POST /api/test-runtime-stream`` had no tenant isolation, and no tests at all.

Found live on 2026-09-20 against the acfe2e-p0920 deployment. ``e2e-user-a`` POSTed
``{"runtimeId": "web_search_agent_88a6981e-qE8xnm3uaM", "input": "..."}`` to this route and
received that agent's real answer in SSE token frames. The runtime is owned by a different
tenant (``user_id=b458d4f8-...``). The sync route refused the identical request.

Two routes invoke a runtime, and the wrong one was hardened:

    deployment_handler.handle_test_runtime         (request, raw_request: Request)  -> checks
    deployment_handler.handle_test_runtime_stream  (request)                        -> could not

The streaming route took no ``Request``, so it had no caller identity to compare an owner
against -- the omission was in the signature, which is why reading the body of the route never
looked like it was missing a check. ``stream_handler._stream_invoke`` (the Lambda Function URL
twin) *does* enforce the rule, and its comment claims the rule is "identical to
handle_test_runtime / delete"; the Function URL is AWS_IAM-authed and, per
``infra/stacks/platform/lambdas.py``, "provisioned but NOT yet wired to the browser". So the
enforced copy was the unreachable one, and the reachable one -- called by
``DeployPanel.tsx:420`` and ``services/api/chat.ts:51`` for every chat turn, i.e. the primary
invoke path in the product -- enforced nothing.

A second defect, same probe: the sync route's refusal did not arrive as a 404. ``HTTPException``
is an ``Exception``, and the route's own broad ``except Exception`` caught the 404 it had just
raised and returned HTTP 200 with ``{"success": false, "error": "An internal error occurred.
Check server logs for details."}`` -- while ``logger.exception`` recorded a routine
authorization denial at ERROR level with a traceback. Covered here too, because "the refusal
happened" and "the refusal is reported as a refusal" are different claims.

Three kinds of assertion, because the obvious one is satisfied by a route that refuses
everybody:
  * the refusal (both routes, both deployment modes),
  * the happy path and the documented pre-tenancy carve-out (the vacuity guards),
  * a structural guard -- any route that resolves a deployment from a caller-supplied id must
    take a ``Request``, so the next route written this way fails this suite instead of shipping.
"""

from __future__ import annotations

import inspect
import json
import sys

sys.path.insert(0, "src")

import pytest
from app import deployment_handler as dh
from app.services import step_clients
from fastapi import Request
from fastapi.testclient import TestClient

OWNER = "b458d4f8-60e1-70fa-98bd-fb664f6e307c"
OTHER = "54381418-7021-708e-4f3b-30505a2b82ec"
RUNTIME = "web_search_agent_88a6981e-qE8xnm3uaM"
ANSWER = "the owner agent answered"
HARNESS_ANSWER = "the harness answered"


def _client(sub: str | None) -> TestClient:
    """A TestClient whose requests carry ``sub`` the way API Gateway delivers it.

    ``_get_user_id`` reads ``request.scope["aws.event"]``, which Mangum populates from the
    HTTP API's JWT authorizer. Injecting it at the ASGI layer (rather than calling the route
    function directly) keeps FastAPI's routing and parameter binding in the loop -- so a
    ``raw_request`` that FastAPI mis-binds as a body field fails these tests instead of
    passing them.
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
    pass


class _Store:
    _table = _Table()


@pytest.fixture
def runtime_mode(monkeypatch):
    """A RUNTIME-mode deployment record owned by ``OWNER``, with the invoke stubbed."""
    record = {
        "deployment_id": "5bb2084b-d586-46d6-a5f3-494cd24cfc89",
        "runtime_id": RUNTIME,
        "runtime_arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{RUNTIME}",
        "user_id": OWNER,
        "deployment_mode": "runtime",
        "status": "succeeded",
    }
    invoked: list[dict] = []

    class _Agentcore:
        def invoke_agent_runtime(self, **kwargs):
            invoked.append(kwargs)
            return {"runtimeSessionId": "sess-1", "response": json.dumps({"response": ANSWER}).encode()}

    class _Session:
        def client(self, service, **_kwargs):
            assert service == "bedrock-agentcore"
            return _Agentcore()

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(record) if rid == RUNTIME else None)
    monkeypatch.setattr(step_clients, "session_for_event", lambda _event: _Session())
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: False)
    return record, invoked


@pytest.fixture
def harness_mode(monkeypatch):
    """A HARNESS-mode record owned by ``OWNER``. ``invoke_harness`` records its calls."""
    record = {
        "deployment_id": "c377daa5-77a1-49d7-a6a1-82d8d9481791",
        "runtime_id": RUNTIME,
        "harness_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:harness/h-1",
        "user_id": OWNER,
        "deployment_mode": "harness",
        "status": "succeeded",
    }
    invoked: list[tuple] = []

    data_client = object()

    class _Session:
        def client(self, service, **_kwargs):
            assert service == "bedrock-agentcore"
            return data_client

    def _invoke_harness(region, arn, prompt, session, *, agentcore_data_client=None):
        invoked.append((region, arn, prompt, session, agentcore_data_client))
        return {"success": True, "output": HARNESS_ANSWER, "trace_id": "1-abc"}

    monkeypatch.setattr(dh, "_get_state_store", lambda: _Store())
    monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(record) if rid == RUNTIME else None)
    monkeypatch.setattr(step_clients, "session_for_event", lambda _event: _Session())
    monkeypatch.setattr(dh, "invoke_harness", _invoke_harness)
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: False)
    return record, invoked


def _frames(body: str) -> tuple[list[str], list[str]]:
    """(token strings, error strings) parsed out of an SSE body."""
    tokens, errors = [], []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        evt = json.loads(line[6:])
        if evt.get("type") == "token":
            tokens.append(evt.get("token", ""))
        elif evt.get("type") == "error":
            errors.append(str(evt.get("error")))
    return tokens, errors


def _post(sub: str | None, path: str = "/api/test-runtime-stream", **extra):
    body = {"runtimeId": RUNTIME, "input": "Reply with the single word OK.", **extra}
    return _client(sub).post(path, json=body)


class TestTheStreamingRouteEnforcesOwnership:
    def test_a_non_owner_gets_an_error_frame_and_no_tokens(self, runtime_mode):
        resp = _post(OTHER)
        tokens, errors = _frames(resp.text)
        assert errors == ["Runtime not found"], resp.text
        assert tokens == []

    def test_a_non_owner_never_reaches_the_invoke(self, runtime_mode):
        """The check must precede the invoke, not just filter the output.

        A route that invoked first and dropped the answer would still bill the model, run
        the agent's tools, and leave the other tenant's traces in their logs.
        """
        _, invoked = runtime_mode
        _post(OTHER)
        assert invoked == []

    def test_the_refusal_discloses_nothing(self, runtime_mode):
        """The refusal is deliberately the same answer a missing runtime gets.

        A distinct "not yours" would turn the route into an existence oracle for other
        tenants' runtime ids, which are guessable from the agent-name prefix.
        """
        body = _post(OTHER).text
        for leak in (OWNER, "111122223333", "arn:aws", "user_id", "5bb2084b"):
            assert leak not in body, f"the refusal disclosed {leak!r}: {body}"

    def test_the_owner_still_gets_the_answer(self, runtime_mode):
        """Vacuity guard. Every assertion above is satisfied by refusing everyone."""
        resp = _post(OWNER)
        tokens, errors = _frames(resp.text)
        assert errors == []
        assert "".join(tokens) == ANSWER
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_a_pre_tenancy_record_stays_readable(self, runtime_mode, monkeypatch):
        """``user_id=None`` rows predate tenancy and stay accessible until a backfill.

        This is the sync route's documented carve-out (tasks/lessons.md Bug 37); the
        streaming route has to make the SAME choice or the two disagree about one record.
        """
        record, _ = runtime_mode
        legacy = {**record, "user_id": None}
        monkeypatch.setattr(dh, "_scan_for_runtime", lambda table, rid: dict(legacy))
        tokens, errors = _frames(_post(OTHER).text)
        assert errors == []
        assert "".join(tokens) == ANSWER

    def test_the_harness_branch_refuses_a_non_owner_before_invoking(self, harness_mode):
        """HARNESS mode is a second invoke path through the same route (Bug 190)."""
        _, invoked = harness_mode
        tokens, errors = _frames(_post(OTHER).text)
        assert errors == ["Runtime not found"]
        assert tokens == []
        assert invoked == []

    def test_the_harness_branch_still_answers_its_owner(self, harness_mode):
        tokens, errors = _frames(_post(OWNER).text)
        assert errors == []
        assert "".join(tokens) == HARNESS_ANSWER

    def test_the_simulated_shortcut_needs_no_record(self, runtime_mode):
        """``simulated: true`` returns before any lookup, so it must not have regressed."""
        tokens, errors = _frames(_post(OTHER, simulated=True).text)
        assert errors == []
        assert "[Simulated]" in "".join(tokens)


class TestTheSyncRouteReportsARefusalAsARefusal:
    def test_a_non_owner_gets_404_not_a_generic_internal_error(self, runtime_mode):
        resp = _post(OTHER, path="/api/test-runtime")
        assert resp.status_code == 404, resp.text
        assert "An internal error occurred" not in resp.text
        assert "Runtime not found" in resp.text

    def test_a_non_owner_never_reaches_the_invoke(self, runtime_mode):
        _, invoked = runtime_mode
        _post(OTHER, path="/api/test-runtime")
        assert invoked == []

    def test_the_owner_still_gets_the_answer(self, runtime_mode):
        resp = _post(OWNER, path="/api/test-runtime")
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["success"] is True
        assert doc["response"] == ANSWER


class TestTheStatusRouteDoesNotServeAnotherTenantsRecord:
    """``GET /api/deploy/{deployment_id}`` had no owner check either.

    Found by the structural guard below rather than by reading, and then confirmed live on
    2026-09-20: e2e-user-a fetched a deployment owned by another tenant and received the whole
    32-field record -- their Cognito ``sub`` included, which is the handle every other
    ownership check in this module compares against. A uuid4 id was the only thing in front of
    it. This is the route the UI polls all through a deploy, so the happy path matters as much
    as the refusal.
    """

    @pytest.fixture
    def stored(self, monkeypatch):
        state = dh.DeploymentState(
            deployment_id="5bb2084b-d586-46d6-a5f3-494cd24cfc89",
            workflow_id="wf-1",
            started_at="2026-09-20T12:00:00Z",
            status=dh.DeploymentStatusEnum.SUCCEEDED,
            user_id=OWNER,
            runtime_id=RUNTIME,
        )

        class _S:
            def get(self, did):
                return state if did == state.deployment_id else None

        monkeypatch.setattr(dh, "_get_state_store", lambda: _S())
        monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: False)
        return state

    def _get(self, sub, did="5bb2084b-d586-46d6-a5f3-494cd24cfc89"):
        return _client(sub).get(f"/api/deploy/{did}")

    def test_a_non_owner_gets_404(self, stored):
        resp = self._get(OTHER)
        assert resp.status_code == 404, resp.text

    def test_the_refusal_leaks_neither_the_owner_nor_the_record(self, stored):
        body = self._get(OTHER).text
        for leak in (OWNER, RUNTIME, "succeeded"):
            assert leak not in body, f"the refusal disclosed {leak!r}: {body}"

    def test_the_refusal_is_the_same_as_for_a_missing_deployment(self, stored):
        """Not an existence oracle: "not yours" and "no such id" must be indistinguishable."""
        mine_but_not = self._get(OTHER)
        absent = self._get(OTHER, "00000000-0000-4000-8000-000000000000")
        assert absent.status_code == mine_but_not.status_code
        assert absent.json()["detail"].split("'")[0] == mine_but_not.json()["detail"].split("'")[0]

    def test_the_owner_still_gets_the_record(self, stored):
        """Vacuity guard. The UI polls this route for the whole duration of a deploy."""
        resp = self._get(OWNER)
        assert resp.status_code == 200, resp.text
        assert resp.json()["runtime_id"] == RUNTIME

    def test_a_pre_tenancy_record_stays_readable(self, stored):
        stored.user_id = None
        assert self._get(OTHER).status_code == 200


class TestARouteThatResolvesADeploymentCanIdentifyItsCaller:
    """The structural guard for the class of bug, not the instance.

    The defect was a missing parameter, so the cheapest durable oracle is the signature:
    a route that turns a caller-supplied id into a stored deployment record has to be able
    to ask who the caller is. Any new route written the way the streaming one was fails
    here, at the point where someone still has to decide whether it needs an owner check.
    """

    #: The deployment-record lookups that take a caller-supplied id and can return ANOTHER
    #: tenant's row. Deliberately only the three defined in deployment_handler itself --
    #: other routers mounted on this app own different stores and authenticate through
    #: their own dependencies, so matching a bare ``store.get(`` produced 17 false hits.
    LOOKUPS = ("_scan_for_runtime", "_lookup_deployment_record", "_get_state_store().get(", "store.get(deployment_id")

    #: A route may identify its caller with a ``Request`` (this module's idiom, via
    #: ``_get_user_id``) or with a resolved-caller dependency (the routers' idiom). Either
    #: satisfies the guard; having neither is the defect.
    CALLER_DEPENDENCIES = ("get_caller_sub", "require_scope", "get_caller_role")

    def _routes(self):
        """(path, endpoint, source) for every route defined in deployment_handler itself."""
        for route in dh.deployment_app.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None or not hasattr(route, "path"):
                continue
            if getattr(endpoint, "__module__", "") != dh.__name__:
                continue
            try:
                source = inspect.getsource(endpoint)
            except (OSError, TypeError):  # pragma: no cover - defensive
                continue
            yield route.path, endpoint, source

    def _matching(self):
        return [(p, e, s) for p, e, s in self._routes() if any(k in s for k in self.LOOKUPS)]

    def test_the_routes_it_covers_are_the_ones_expected(self):
        """Vacuity guard, twice over.

        An empty iteration -- a renamed lookup helper, a moved route, a changed module name
        -- would make the assertion below pass while checking nothing. Naming the paths also
        records which routes were reasoned about, so a NEW one shows up here as a failure
        that a human has to classify rather than as silent coverage.
        """
        covered = sorted({p for p, _, _ in self._matching()})
        assert covered == [
            "/api/deploy/{deployment_id}",
            "/api/runtime/import",
            "/api/runtime/{runtime_id}",
            "/api/test-runtime",
            "/api/test-runtime-stream",
        ], covered

    def test_every_such_route_can_identify_its_caller(self):
        offenders = []
        for path, endpoint, _source in self._matching():
            params = inspect.signature(endpoint).parameters.values()
            has_request = any(p.annotation is Request for p in params)
            has_dependency = any(dep in str(p.default) for p in params for dep in self.CALLER_DEPENDENCIES)
            if not (has_request or has_dependency):
                offenders.append(f"{path} ({endpoint.__name__})")
        assert offenders == [], (
            "these routes resolve a deployment record from a caller-supplied id but cannot "
            f"identify the caller to compare an owner against: {offenders}"
        )
