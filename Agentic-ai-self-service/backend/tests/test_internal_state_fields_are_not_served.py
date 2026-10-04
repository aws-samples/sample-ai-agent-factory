"""The API response **is** the storage model, so every new field is published by default.

Found live. ``GET /api/deploy/{id}`` on ``acfe2e-p0920``, after a real failed deployment,
returned::

    "execution_arn": "arn:aws:states:us-east-1:166827918465:execution:
                      acfe2e-p0920-deployment:deploy-70e488d0-..."

which names the platform's account, its state machine and its region -- "Internal system
components" in ARCC ``cnt_94E30Xo4RZHtSJ``'s list of what a response must not contain. It was
the only 12-digit account id anywhere in that document, and it reached the browser on three
surfaces: this route, ``GET /api/deployments``, and the ``POST /api/deploy`` 202.

Nothing consumed it. Zero frontend references; nothing in the backend outside the two lines
that write it.

The reason a unit test could not have caught the disclosure is the reason these tests are
shaped the way they are: ``execution_arn`` was not *added* to a response, it was added to
``DeploymentState`` for the platform's own bookkeeping, and three routes that do
``state.model_dump(mode="json")`` published it with nothing to object. So the guard cannot be
"``execution_arn`` is absent" -- that only re-tests the field we already found. It has to be
"the served field set is exactly this list", which fails on the NEXT such field too.

Two oracles, because either alone is insufficient:

* the pin on the served set catches a new field on the model, but would still pass if a route
  quietly dropped its ``exclude=``;
* driving the real route functions catches that, but on its own only knows about the one field.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from app.models.deployment_models import (
    INTERNAL_ONLY_STATE_FIELDS,
    DeploymentState,
    DeploymentStatusEnum,
    DeployResponse,
)

#: A stand-in for the platform's own account. Not the real one -- the point is the shape.
PLATFORM_ACCOUNT = "111122223333"
PLATFORM_EXECUTION_ARN = (
    f"arn:aws:states:us-east-1:{PLATFORM_ACCOUNT}:execution:acfe2e-p0920-deployment:deploy-70e488d0-x"
)

#: Every field ``GET /api/deploy/{id}`` and ``GET /api/deployments`` are allowed to serve.
#:
#: Maintained BY HAND on purpose. Adding a field to :class:`DeploymentState` fails this test
#: until someone writes the name here or in ``INTERNAL_ONLY_STATE_FIELDS``, which is the whole
#: mechanism: the failure is the decision point that did not exist when ``execution_arn`` was
#: added.
PUBLIC_STATE_FIELDS = frozenset(
    {
        "deployment_id",
        "workflow_id",
        # F-55. The canvas node the caller deployed, as their own browser sent it. It is
        # the other half of ``workflow_id`` (a flow holds many nodes), carries no account,
        # ARN or platform component, and is validated to ``[A-Za-z0-9_-]`` on the way in.
        "node_id",
        # The caller's own sub. They are the only principal who can read their own records
        # (``query_by_user`` keys off the JWT claim), so this is their identifier, not a
        # third party's.
        "user_id",
        "status",
        "current_step",
        "started_at",
        "completed_at",
        "runtime_endpoint",
        "runtime_id",
        # Server-authored protocol determines which safe invocation surface the
        # caller must use (HTTP chat, MCP tools, or A2A).
        "runtime_protocol",
        "gateway_url",
        "gateway_result",
        "policy_result",
        "knowledge_base_result",
        "guardrails_result",
        "mcp_server_runtime_id",
        "memory_result",
        "runtime_arn",
        "harness_id",
        "harness_arn",
        "harness_result",
        "deployment_mode",
        # Public deliberately, and this test is what forced the decision rather than
        # letting it default. ``imported`` is a bare boolean about the caller's own
        # record -- no account, no ARN, no platform component -- and it answers a
        # question the caller genuinely has, because it changes the contract of their
        # next request: DELETE will not destroy an adopted runtime unless they also
        # pass ``?destroy=true``. Nothing reads it in the frontend today; that is an
        # argument for not *promising* it, not for hiding a flag whose whole meaning
        # is "your delete behaves differently".
        "imported",
        # Sanitized on the way in -- see services/error_sanitizer.py and
        # test_error_details_never_leak_internals.py. Served because the actionable sentence
        # is the only part of a failure an operator can act on (ARCC cnt_94E30Xo4RZHtSJ
        # explicitly permits keeping it).
        "error_details",
        "ttl",
        "version_id",
        "parent_version_id",
        "deployment_slot",
        "agentcore_runtime_name",
        "created_resources",
        # Caller-supplied on the deploy request: the account and region THEY asked to deploy
        # into. Reflecting their own input back is not disclosure of ours.
        "target_account_id",
        "target_region",
        "delete_status",
        "delete_message",
    }
)


def _state(**overrides) -> DeploymentState:
    base = {
        "deployment_id": "70e488d0-fb48-47bf-b25f-b01769b6c85f",
        "workflow_id": "93152cb2-9578-493e-a16a-c0b4438c4acb",
        "user_id": "54381418-7021-708e-4f3b-30505a2b82ec",
        "execution_arn": PLATFORM_EXECUTION_ARN,
        "status": DeploymentStatusEnum.FAILED,
        "started_at": datetime(2026, 9, 20, 19, 12, tzinfo=UTC),
        "error_details": "Knowledge Base ZZZZZZZZZZ not found",
    }
    base.update(overrides)
    return DeploymentState(**base)


class _StubStore:
    """Only the two methods the read routes call."""

    def __init__(self, state: DeploymentState) -> None:
        self._state = state

    def get(self, deployment_id: str) -> DeploymentState | None:
        return self._state if deployment_id == self._state.deployment_id else None

    def query_by_user(self, user_id: str, status_filter=None) -> list[DeploymentState]:
        return [self._state] if user_id == self._state.user_id else []

    def query_by_workflow(self, workflow_id: str, status_filter=None) -> list[DeploymentState]:
        return [self._state] if workflow_id == self._state.workflow_id else []


class _Request:
    """The shape ``_get_user_id`` reaches into: an API Gateway JWT authorizer claim."""

    def __init__(self, sub: str | None) -> None:
        claims = {"sub": sub} if sub else {}
        self.scope = {"aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}}


@pytest.fixture()
def served(monkeypatch):
    """Drive the real route functions against a stub store, return both bodies."""
    from app import deployment_handler as dh

    state = _state()
    monkeypatch.setattr(dh, "_get_state_store", lambda: _StubStore(state))
    # The status route opportunistically promotes a pending Cedar engine; it is best-effort
    # and wrapped, but stub it so no test ever reaches AWS.
    monkeypatch.setattr(dh, "_maybe_promote_policy", lambda *a, **k: None, raising=False)

    # The status route takes the raw request now: it gained a tenant-isolation check on
    # 2026-09-20 (it used to serve any deployment record to any authenticated caller). The
    # owner's own sub is what this fixture is for -- it measures what a legitimate caller is
    # served, which is a different question from who may call. See
    # tests/test_stream_route_tenant_isolation.py for the isolation itself.
    one = asyncio.run(dh.handle_deploy_status(state.deployment_id, _Request(state.user_id)))
    many = asyncio.run(dh.handle_list_deployments(_Request(state.user_id)))
    return state, one, many


class TestTheServedFieldSetIsPinned:
    def test_the_two_sets_partition_the_model(self):
        """The allow-list plus the internal list must be exactly the model's fields.

        This is what fails when a field is added: not "you leaked something", but "you have
        not said which side this belongs on".
        """
        declared = set(DeploymentState.model_fields)
        classified = PUBLIC_STATE_FIELDS | INTERNAL_ONLY_STATE_FIELDS
        unclassified = declared - classified
        assert not unclassified, (
            "new DeploymentState field(s) are being served by default: "
            f"{sorted(unclassified)}. Add each to PUBLIC_STATE_FIELDS if a caller needs it, "
            "or to INTERNAL_ONLY_STATE_FIELDS if it is the platform's own bookkeeping."
        )
        stale = classified - declared
        assert not stale, f"classified but no longer on the model: {sorted(stale)}"

    def test_the_two_sets_do_not_overlap(self):
        """Otherwise a field could be listed as public AND excluded, and the pin below would
        silently stop meaning anything."""
        assert not (PUBLIC_STATE_FIELDS & INTERNAL_ONLY_STATE_FIELDS)

    def test_the_internal_list_is_not_empty(self):
        """Vacuity guard. Every assertion here still passes if ``INTERNAL_ONLY_STATE_FIELDS``
        is emptied and everything is declared public -- which is precisely the defect."""
        assert INTERNAL_ONLY_STATE_FIELDS


class TestTheRealRoutesServeOnlyThatSet:
    def test_the_status_route_serves_exactly_the_public_set(self, served):
        _, one, _ = served
        assert set(one) == set(PUBLIC_STATE_FIELDS)

    def test_the_list_route_serves_exactly_the_public_set(self, served):
        _, _, many = served
        assert len(many) == 1
        assert set(many[0]) == set(PUBLIC_STATE_FIELDS)

    @pytest.mark.parametrize("route", ["one", "many"])
    def test_no_platform_account_id_anywhere_in_the_body(self, served, route):
        """The live finding, checked the way it was found: over the WHOLE serialized document,
        because the browser is handed the whole document, not one field."""
        state, one, many = served
        body = json.dumps(one if route == "one" else many, default=str)
        assert PLATFORM_ACCOUNT not in body, body
        assert "states:us-east-1" not in body, body
        assert "deployment:deploy-" not in body, body

    def test_the_record_still_holds_it(self, served):
        """The other direction. Removing the field from the model would pass every assertion
        above and destroy the handle an operator needs to find the execution in the console --
        which is why the fix excludes at the serialization boundary rather than deleting it."""
        state, _, _ = served
        assert state.execution_arn == PLATFORM_EXECUTION_ARN
        assert "execution_arn" in DeploymentState.model_fields

    def test_the_actionable_failure_message_survives(self, served):
        """Vacuity guard on the routes. Serving nothing would satisfy every check above."""
        _, one, _ = served
        assert one["error_details"] == "Knowledge Base ZZZZZZZZZZ not found"
        assert one["status"] == "failed"


class TestThe202DoesNotCarryIt:
    def test_deploy_response_has_no_execution_arn_field(self):
        """The third surface. It was a declared field on the 202's model, so it could not be
        excluded at a call site -- it had to come off the model."""
        assert "execution_arn" not in DeployResponse.model_fields

    def test_deploy_response_rejects_it_rather_than_ignoring_it(self):
        """If the model silently accepted an unknown key, re-adding the argument at the call
        site would be a no-op and this would look fixed while a future edit reinstated it."""
        body = DeployResponse(deployment_id="d1").model_dump(by_alias=True)
        assert set(body) == {"deploymentId", "status", "message"}
