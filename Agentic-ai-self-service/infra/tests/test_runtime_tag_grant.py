"""The runtime-creating step roles must be able to tag the runtime they create.

Found live, not by reading: a deploy through ``POST /api/deploy`` failed in
``DeployMCPServer`` with

    AccessDeniedException ... is not authorized to perform:
    bedrock-agentcore:TagResource on resource: .../runtime/*
    because no identity-based policy allows the bedrock-agentcore:TagResource action

raised from ``CreateAgentRuntime`` with ``runtime_id`` still null.  ``tags`` on a
create call is authorized as ``TagResource`` on the resource being created, never
as part of the create action, and ``create_agent_runtime`` always sends
``owner_tags(region)`` with no untagged fallback.  So the absence of this grant is
not a hardening gap, it is a total outage of every runtime-creating deployment.

The grant is deliberately conditioned rather than plain: the runtime id does not
exist at synth time, so the resource ids have to stay globbed, and this account
holds foreign runtimes that an unconditioned tag write could relabel.

``runtime/*`` alone is NOT the whole resource set, and this file asserted that it
was until a second live denial proved otherwise.  ``CreateAgentRuntime`` also mints
a workload identity for the runtime and tags that too, under the caller's identity,
so the same create call is authorized against a resource nothing in this codebase
names:

    AccessDeniedException ... not authorized to perform: bedrock-agentcore:TagResource
    on resource: .../workload-identity-directory/default/workload-identity/*

The permitted resource set is therefore derived from
``AGENTCORE_TAG_ON_CREATE_TYPES`` and ``AGENTCORE_TYPE_ARN_TAIL`` below rather than
written out here, because a hand-written list is exactly what was wrong: it looked
like the stricter assertion while forbidding a grant the runtime path cannot deploy
without.

What these tests DO pin, stated exactly so no reader infers more: the only tag pair
this role may write is *this deployment's own*, in the region the call is made in.
What they do NOT pin, because the policy cannot deliver it: the grant still permits
stamping our ownership onto a foreign runtime this platform did not create.  The AWS
Service Reference feed gives ``bedrock-agentcore:TagResource`` only
``aws:RequestTag/${TagKey}`` and ``aws:TagKeys``, with no create-only condition key,
so a dependent tag-on-create is indistinguishable in policy from a standalone retag.
That residual is named in the grant's own comment and in the ownership docs rather
than being asserted away by a test name here.
"""

import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform.step_lambdas import (
    AGENTCORE_TAG_ON_CREATE_TYPES,
    AGENTCORE_TYPE_ARN_TAIL,
)
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

RUNTIME_CREATING_STEPS = ("RuntimeConfigure", "McpServer")

# The synth inputs, named so the expected tag value can be DERIVED from them below
# instead of hardcoded -- a hardcoded expectation passes even when the policy stops
# tracking the stack it is generated for.
PROBE_PROJECT = "tagprobe"
PROBE_ENV = "t0922"
PROBE_ACCOUNT = "123456789012"
PROBE_HOME_REGION = "us-east-1"


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"{PROBE_PROJECT}-{PROBE_ENV}",
        environment_name=PROBE_ENV,
        project_name=PROBE_PROJECT,
        env=cdk.Environment(account=PROBE_ACCOUNT, region=PROBE_HOME_REGION),
    )
    return Template.from_stack(stack)


def _statements_for_role(template: Template, role_fragment: str) -> list[dict]:
    """Every policy statement attached to the role whose logical id matches, any shape.

    Resolved through ``tests/iam_attachment.py`` rather than by scanning
    ``AWS::IAM::Policy`` here, because that misses CDK's ``<Role>OverflowPolicy<N>``
    managed policy -- the third attachment shape, which CDK creates on its own once a
    role's inline document nears the 10240-character limit. The tag statements happen to
    be inline for these two roles today, so the old scan passed; it would have started
    reporting the grant as absent the moment an unrelated statement pushed either role
    over the line, which is exactly what happened to the gateway role next door.
    """
    template_json = template.to_json()
    roles = [
        lid
        for lid in template_json["Resources"]
        if role_fragment in lid and template_json["Resources"][lid]["Type"] == "AWS::IAM::Role"
    ]
    assert roles, f"no IAM role logical id contains {role_fragment!r} -- the scan found nothing to check"
    assert len(roles) == 1, (
        f"{role_fragment!r} matches {roles}; an ambiguous role makes every assertion below unattributable"
    )
    return [st for _src, st in statements_for_role(template_json, roles[0])]


def _tag_statements(statements: list[dict], *, require: bool = True) -> list[dict]:
    """The TagResource statements, refusing to return an empty list by default.

    Every assertion below loops over these statements, and a loop over nothing
    passes. That is the shape that lets a grant disappear while its conditions are
    still reported as correct, so absence is an error here rather than a vacuous
    pass -- only the presence test opts out.
    """
    out = []
    for s in statements:
        actions = s.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if "bedrock-agentcore:TagResource" in actions:
            out.append(s)
    if require:
        assert out, (
            "no bedrock-agentcore:TagResource statement exists, so this test has nothing "
            "to check -- treat it as a failure, not a pass"
        )
    return out


@pytest.mark.parametrize("step", RUNTIME_CREATING_STEPS)
def test_each_runtime_creating_step_role_may_tag_the_runtime_it_creates(template, step):
    """Without this the deploy fails at CreateAgentRuntime with runtime_id null."""
    found = _tag_statements(_statements_for_role(template, step), require=False)
    assert found, (
        f"the {step} step role cannot call bedrock-agentcore:TagResource, so "
        "CreateAgentRuntime's tags= argument will be denied and every deployment "
        "that creates a runtime fails (observed live before this grant existed)"
    )


@pytest.mark.parametrize("step", RUNTIME_CREATING_STEPS)
def test_the_runtime_tag_grant_writes_only_this_deployments_own_ownership_pair(template, step):
    """Both ownership VALUES are pinned, not just the key list and ManagedBy.

    The narrower claim is the true one. Pinning ``aws:TagKeys`` to the two keys plus
    ``aws:RequestTag/ManagedBy`` to our product value still authorizes TagResource
    against any existing runtime, because a request carrying an ARBITRARY
    ``AgentCoreStack`` value satisfies it -- and ``AgentCoreStack`` is the value
    teardown actually matches on, so that gap would let this role forge or strip
    another deployment's ownership. With both values pinned the only pair it can write
    is this deployment's own. It can still write that pair onto a runtime we did not
    create; see the module docstring.
    """
    expected_owner = f"{PROBE_PROJECT}-{PROBE_ENV}-${{aws:RequestedRegion}}"
    for statement in _tag_statements(_statements_for_role(template, step)):
        equals = statement.get("Condition", {}).get("StringEquals", {})
        assert equals.get("aws:RequestTag/ManagedBy") == "agentcore-flows", (
            f"{step}: bedrock-agentcore:TagResource on runtime/* is granted without pinning "
            "aws:RequestTag/ManagedBy, so this role could stamp arbitrary tags onto a "
            "foreign runtime in this account"
        )
        assert equals.get("aws:RequestTag/AgentCoreStack") == expected_owner, (
            f"{step}: aws:RequestTag/AgentCoreStack is "
            f"{equals.get('aws:RequestTag/AgentCoreStack')!r}, expected {expected_owner!r}. "
            "Leaving this key unpinned authorizes relabelling any runtime in the account "
            "under an arbitrary owner; pinning it to a value that is not the one "
            "owner_tags() sends denies every runtime create instead"
        )
        # The allowlist is the two ownership keys plus the governance NAMESPACES, and the
        # namespaces are read from the same constant the stack builds them from rather than
        # retyped here -- a literal would pass while the deployed policy denied the very keys
        # the product resolves. ``create_agent_runtime`` now sends ManagedBy, AgentCoreStack and
        # whatever P0-B tag policies an admin has created; those keys are unknowable at synth
        # time, which is why this is a namespace rather than an enumeration (see
        # config.GOVERNANCE_TAG_KEY_PREFIXES).
        allowed = statement.get("Condition", {}).get("ForAllValues:StringLike", {}).get("aws:TagKeys")
        assert sorted(allowed or []) == sorted(["AgentCoreStack", "ManagedBy", *governance_tag_key_globs()]), (
            f"{step}: the tag-key allow-list is {allowed!r}. It must be the two ownership keys "
            "plus exactly the governance namespaces config.GOVERNANCE_TAG_KEY_PREFIXES declares: "
            "anything narrower denies a governed deploy at CreateAgentRuntime (measured live on "
            "acfe2e-p0920), and anything wider lets this role write tag keys that other policies "
            "in this account may authorize on"
        )
        # A namespace is a bound only while it IS one. `platform` without the colon, or a bare
        # `*`, would satisfy the equality above only if someone edited the constant to match --
        # so the shape is asserted against the pattern itself, not against the constant.
        for glob in allowed:
            assert "*" not in glob or glob.endswith(":*"), (
                f"{step}: {glob!r} is not a namespace. A tag-key pattern that is not "
                "`<namespace>:*` bounds nothing useful -- `*` alone authorizes every key, which "
                "is the privilege-escalation surface the allowlist exists to close"
            )


@pytest.mark.parametrize("step", RUNTIME_CREATING_STEPS)
def test_the_runtime_tag_grant_survives_a_non_home_target_region(template, step):
    """A literal home region in either the ARN or the tag value is a regression.

    ``target_region`` with no ``target_account_id`` is a supported, admin-gated,
    allowlisted path: ``handle_deploy`` persists it (deployment_handler.py:1249-1277)
    and ``step_clients.session_for_event`` then uses THIS role's own credentials in
    that other region, so ``owner_tags(region)`` emits
    ``{project}-{env}-<target_region>``. Pinning either the resource region or the tag
    value's region component to the home region therefore denies TagResource and fails
    the deploy at CreateAgentRuntime -- the exact outage this grant exists to fix,
    reintroduced for every non-home region. ``${aws:RequestedRegion}`` keeps the value
    exact in every region instead of exact only at home.
    """
    for statement in _tag_statements(_statements_for_role(template, step)):
        owner = statement.get("Condition", {}).get("StringEquals", {}).get("aws:RequestTag/AgentCoreStack")
        assert isinstance(owner, str), (
            f"{step}: aws:RequestTag/AgentCoreStack rendered as {owner!r}, not a plain string. "
            "A CDK token here would emit Fn::Sub/Fn::Join and CloudFormation would consume "
            "the ${aws:...} variable instead of IAM"
        )
        assert owner.endswith("-${aws:RequestedRegion}"), (
            f"{step}: the owner tag value is {owner!r}. Its region component must be the IAM "
            "policy variable, not a synth-time literal"
        )
        assert PROBE_HOME_REGION not in owner, (
            f"{step}: the owner tag value {owner!r} contains the home region literally, so a "
            "same-account deploy to an allowlisted non-home region is denied"
        )
        resources = statement.get("Resource", [])
        for arn in [resources] if isinstance(resources, str) else resources:
            assert isinstance(arn, str) and arn.startswith("arn:aws:bedrock-agentcore:*:"), (
                f"{step}: the tag statement's resource is {arn!r}. The region segment must be "
                "* for the same-account multi-region path; the account segment must stay pinned "
                "and the request-tag conditions carry the safety"
            )
            assert f":{PROBE_ACCOUNT}:" in arn, (
                f"{step}: the tag statement's resource {arn!r} does not pin this account"
            )


# Resource shapes no runtime-creating step may ever be able to tag. Stated as a
# deny-list rather than as "must contain :runtime/*" because that stricter-looking
# form was WRONG: it forbade the workload-identity ARNs that CreateAgentRuntime
# provably needs (see the module docstring), so it failed on the correct policy.
# A deny-list keeps the property the test exists for -- no sprawl onto the other
# AgentCore types -- without asserting a resource count the service decides.
TYPES_NO_RUNTIME_STEP_MAY_TAG = (
    ":gateway/",
    ":memory/",
    ":oauth2credentialprovider/",
    ":apikeycredentialprovider/",
    ":policy-engine/",
    ":harness/",
    ":runtime-endpoint/",
)


@pytest.mark.parametrize("step", RUNTIME_CREATING_STEPS)
def test_the_grant_stays_off_every_other_agentcore_resource_type(template, step):
    """These steps tag the runtime and the identity it mints -- nothing else.

    The permitted set is DERIVED from the same two tables the policy is built
    from, so a type added to the grant without being added to the table fails
    here instead of being silently blessed by a hand-written expectation.
    """
    step_key = re.sub(r"(?<!^)(?=[A-Z])", "_", step).lower()
    assert step_key in AGENTCORE_TAG_ON_CREATE_TYPES, (
        f"{step}: no AGENTCORE_TAG_ON_CREATE_TYPES row named {step_key!r}; the step naming "
        "convention changed, so this test is no longer checking the step it names"
    )
    allowed = {
        tail for type_name in AGENTCORE_TAG_ON_CREATE_TYPES[step_key] for tail in AGENTCORE_TYPE_ARN_TAIL[type_name]
    }
    assert allowed, f"{step}: the derived allowlist is empty, so every assertion below is vacuous"

    for statement in _tag_statements(_statements_for_role(template, step)):
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, (str, dict)) else resources
        rendered = [str(r) for r in resources]
        assert rendered, f"{step}: the tag statement names no resource at all"
        for arn in rendered:
            for forbidden in TYPES_NO_RUNTIME_STEP_MAY_TAG:
                assert forbidden not in arn, (
                    f"{step}: bedrock-agentcore:TagResource is granted on {arn!r}, which is a "
                    f"{forbidden.strip(':/')} -- no runtime-creating step handler creates one, "
                    "so the grant is a capability nothing uses"
                )
            assert any(tail.split("/")[0] in arn for tail in allowed), (
                f"{step}: bedrock-agentcore:TagResource is granted on {arn!r}, which matches no "
                f"tail derived from AGENTCORE_TAG_ON_CREATE_TYPES[{step_key!r}] = "
                f"{AGENTCORE_TAG_ON_CREATE_TYPES[step_key]}. Add the type to the table with the "
                "call site that proves it, or drop the resource from the grant"
            )
