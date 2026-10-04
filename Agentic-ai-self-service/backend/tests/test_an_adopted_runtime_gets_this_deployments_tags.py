"""An ADOPTED runtime carries this deployment's governance tags, or the deploy fails.

``create_agent_runtime`` falls back to ``update_agent_runtime`` when a runtime of the same
name already exists and passes the exact-owner-tag check. ``update_agent_runtime`` has no
``tags`` parameter, so that path used to drop ``create_params["tags"]`` on the floor: the
adopted runtime kept whatever tag set the deploy that FIRST created it sent.

That is invisible in every way that matters. The deploy reports success, the code really is
updated, and the tag set the operator chose is present in the deployment record and in every
Step Functions state input -- just not on the resource that bills and that ABAC reads. And
because a redeploy is the only occasion on which an existing runtime's tags are written at
all, this was simultaneously the only path by which a stale tag could ever be corrected.

The tests here are about the second call, not the first: a happy-path create already proves
tagging works, and would keep passing with the whole adopt branch untagged.
"""

from __future__ import annotations

import pytest
from app.services.resource_ownership import stack_id
from app.services.resource_tagging import GovernanceTagError
from app.services.runtime_deployer import create_agent_runtime

RUNTIME = "acfe2e-p0920-agent"
RID = "acfe2e_p0920_agent-AbCdEf1234"
ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RID}"
GOV = {"platform:application": "checkout", "org:cost-center": "cc-42"}


class _Ctrl:
    """Enough of the bedrock-agentcore control plane to reach the adopt branch.

    Every call is recorded in ``self.calls`` in order. The order is the assertion in
    ``test_tagging_precedes_the_update``: a list of which calls happened cannot distinguish
    "tagged then updated" from "updated then tagged", and those two differ in what is left
    behind when the tag call is denied.
    """

    def __init__(self, *, list_arn: str | None = None, tag_fails: bool = False, present: bool = True):
        # The ARN is read TWICE from two different calls, and the fake keeps them separate
        # because the product does: ``found_arn`` comes from the list summary, while the
        # ownership assertion resolves its own ARN from ``get_agent_runtime``. A single field
        # here would make the "no ARN" test unreachable -- an empty ARN on the GET refuses
        # inside the ownership check, several steps earlier and for a different reason.
        self.arn = ARN
        self.list_arn = ARN if list_arn is None else list_arn
        self.tag_fails = tag_fails
        self.present = present
        self.calls: list[str] = []
        self.tagged: dict[str, str] | None = None
        self.update_params: dict | None = None

    def create_agent_runtime(self, **kw):
        self.calls.append("create")
        self.created = kw
        raise RuntimeError("ConflictException: agent runtime already exists")

    def list_agent_runtimes(self, **kw):
        self.calls.append("list")
        if not self.present:
            return {"agentRuntimes": []}
        summary = {"agentRuntimeName": RUNTIME, "agentRuntimeId": RID, "agentRuntimeArn": self.list_arn}
        return {"agentRuntimes": [summary]}

    # --- the ownership re-read the adopt branch performs before it touches anything ---
    def get_agent_runtime(self, **kw):
        self.calls.append("get")
        return {"agentRuntimeArn": self.arn}

    def list_tags_for_resource(self, **kw):
        self.calls.append("list_tags")
        # The live owner tags of the runtime being adopted. They must match exactly or the
        # branch refuses before this test's subject is reached.
        return {"tags": {"ManagedBy": "agentcore-flows", "AgentCoreStack": stack_id("us-east-1")}}

    def tag_resource(self, **kw):
        self.calls.append("tag")
        if self.tag_fails:
            raise RuntimeError("AccessDeniedException: not authorized to perform tag_resource")
        self.tagged = dict(kw["tags"])
        self.tag_arn = kw["resourceArn"]

    def update_agent_runtime(self, **kw):
        self.calls.append("update")
        self.update_params = kw
        return {"agentRuntimeId": RID}


def _adopt(ctrl):
    return create_agent_runtime(
        ctrl,
        runtime_name=RUNTIME,
        role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-x",
        s3_bucket="b",
        s3_key="k/code.zip",
        region="us-east-1",
        resource_tags=GOV,
    )


def test_the_adopted_runtime_is_tagged_with_this_deployments_set():
    ctrl = _Ctrl()
    out = _adopt(ctrl)
    assert out["created_by_deployment"] is False, "adoption must still be recorded as adoption"
    assert ctrl.tagged is not None, (
        "the adopted runtime was never tagged. update_agent_runtime takes no tags parameter, "
        "so unless tag_resource is called the governance set reaches nothing."
    )
    assert ctrl.tag_arn == ARN
    # Both halves: the operator's governance keys AND the ownership pair.
    assert ctrl.tagged["platform:application"] == "checkout"
    assert ctrl.tagged["org:cost-center"] == "cc-42"
    assert ctrl.tagged["ManagedBy"] == "agentcore-flows"
    assert ctrl.tagged["AgentCoreStack"] == stack_id("us-east-1")


def test_the_retag_sends_exactly_what_the_create_would_have():
    """The two paths must not be able to disagree about what this deployment stamps.

    Asserting the four keys above would still pass if the retag built its own set from
    ``resource_tags`` and, say, omitted a key the create adds. It would also pass if the
    retag sent ONLY the governance keys -- which is the shape the step roles' IAM grant
    denies: ``aws:RequestTag/ManagedBy`` and ``aws:RequestTag/AgentCoreStack`` are pinned
    with StringEquals, and StringEquals against an absent request tag does not match. So the
    set has to be identical to the create's, and that identity is the assertion.
    """
    ctrl = _Ctrl()
    _adopt(ctrl)
    assert ctrl.tagged == ctrl.created["tags"]


def test_the_update_carries_the_authorizer_exactly_as_the_create_would():
    """UpdateAgentRuntime replaces configuration; an authorizer left out is removed.

    Live (2026-09-28): the customJWTAuthorizer MCP server runtime was redeployed through this
    branch, came back with authorizerConfiguration = null, accepted plain SigV4 and rejected the
    bearer token the gateway target and the pre-warm use, so the deployment failed on every
    pre-warm attempt. The create path set the authorizer; the update path did not.
    """
    authorizer = {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x/.well-known/openid-configuration",
            "allowedClients": ["client-1"],
        }
    }
    ctrl = _Ctrl()
    create_agent_runtime(
        ctrl,
        runtime_name=RUNTIME,
        role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-x",
        s3_bucket="b",
        s3_key="k/code.zip",
        region="us-east-1",
        resource_tags=GOV,
        protocol="MCP",
        authorizer_config=authorizer,
    )
    assert ctrl.update_params is not None
    assert ctrl.update_params["authorizerConfiguration"] == authorizer
    assert ctrl.update_params["authorizerConfiguration"] == ctrl.created["authorizerConfiguration"]
    assert ctrl.update_params["protocolConfiguration"] == {"serverProtocol": "MCP"}


def test_an_update_without_an_authorizer_sends_none_so_an_iam_only_runtime_stays_iam_only():
    ctrl = _Ctrl()
    _adopt(ctrl)
    assert ctrl.update_params is not None
    assert "authorizerConfiguration" not in ctrl.update_params
    assert "authorizerConfiguration" not in ctrl.created


def test_tagging_precedes_the_update():
    """Order, because the two orders differ in what a denial leaves behind.

    Tag-then-update means a denied tag call fails the deploy having mutated nothing.
    Update-then-tag would leave the NEW code live on a runtime still carrying the previous
    deployment's cost and access-control tags -- a state no operator can see from the UI.
    """
    ctrl = _Ctrl()
    _adopt(ctrl)
    assert ctrl.calls.index("tag") < ctrl.calls.index("update")
    # And after the ownership proof, not before it: tagging a resource whose owner tag has
    # not been re-read would stamp this deployment's ownership onto someone else's runtime.
    assert ctrl.calls.index("list_tags") < ctrl.calls.index("tag")


def test_a_denied_retag_refuses_and_updates_nothing():
    ctrl = _Ctrl(tag_fails=True)
    with pytest.raises(RuntimeError) as err:
        _adopt(ctrl)
    assert "update" not in ctrl.calls, (
        "the runtime was updated anyway after the tag call failed. New code is now live under "
        "the previous deployment's tags, which is the state tagging-first exists to prevent."
    )
    assert ctrl.update_params is None
    # The exception TYPE is reported, never the botocore message: an AccessDenied message
    # echoes request parameters, and a tag VALUE is caller data.
    assert "RuntimeError" in str(err.value)
    assert "cc-42" not in str(err.value)


def test_an_unnamespaced_governance_key_never_reaches_a_single_api_call():
    """The refusal is at set construction, so it costs nothing -- not even the create attempt.

    This is what makes tagging-first safe to raise from: the only failure left by the time
    ``tag_resource`` is reached is an AWS-side one. A validation failure has already happened
    before the first call.
    """
    ctrl = _Ctrl()
    with pytest.raises(GovernanceTagError):
        create_agent_runtime(
            ctrl,
            runtime_name=RUNTIME,
            role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-x",
            s3_bucket="b",
            s3_key="k/code.zip",
            region="us-east-1",
            resource_tags={"cost-center": "cc-42"},
        )
    assert ctrl.calls == [], f"calls were made before the tag set was validated: {ctrl.calls}"


def test_an_adopted_runtime_with_no_arn_is_refused_rather_than_deployed():
    """A summary carrying an id but no ARN cannot be tagged -- and could never be torn down.

    The previous code returned that empty string as the deployment's runtime ARN, so this is
    not a new failure mode being invented; it is an already-broken state being named at the
    point it is detectable. Note the ownership check passes here -- it resolves its own ARN
    from ``get_agent_runtime`` -- so this really is the list summary's gap and not a
    re-test of the ownership refusal.
    """
    ctrl = _Ctrl(list_arn="")
    with pytest.raises(RuntimeError, match="no agentRuntimeArn"):
        _adopt(ctrl)
    assert "tag" not in ctrl.calls
    assert "update" not in ctrl.calls
