"""The governance tag namespaces: one constant, two consumers, and they must agree.

P0-B resolves a governance tag set per deploy from admin-created tag policies, and the live
path now applies it to the AWS resources a deployment creates. That broke every governed deploy
on ``acfe2e-p0920`` the moment it shipped -- measured live, CreateAgentRuntime:

    AccessDeniedException ... not authorized to perform: bedrock-agentcore:TagResource on
    resource: arn:aws:bedrock-agentcore:us-east-1:...:runtime/*

with the action granted and the resource matching. What denied it was
``ForAllValues:StringEquals aws:TagKeys: [ManagedBy, AgentCoreStack]`` -- a deliberate tripwire
whose comment says so. A governance key is another key. ARCC ``cnt_SaTYaDCgBBJTcv`` describes
this exact shape: a stack that cannot tag the resources it manages starts failing "even though
the customer has not made any changes to their code/stack".

The fix has TWO halves that cannot be allowed to drift:

1. the ``aws:TagKeys`` allowlist admits the governance NAMESPACES (an enumeration is impossible
   -- an admin creates keys at runtime through ``POST /api/settings/tags``, so enumerating them
   would mean a platform redeploy per tag policy; and dropping the bound is the escalation the
   condition exists to prevent, ARCC ``cnt_L4ZLZgjrCctfxl`` listing create/update tags among
   the powerful operations and ``cnt_6gBImtb08AJqCB`` giving the mechanism: tags carry ABAC
   decisions);
2. the backend refuses an out-of-namespace key at the API boundary, from the same value handed
   to it in the environment, so the operator reads a sentence instead of watching a
   half-created deployment fail.

Half 1 without half 2 is the original outage in slower motion. Half 2 without half 1 refuses
keys AWS would have accepted. Two halves with DIFFERENT values is worse than either: the API
accepts a key the deployed policy denies. So the agreement is the test, not the individual
values.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import (
    GOVERNANCE_TAG_KEY_PREFIXES,
    GOVERNANCE_TAG_KEY_PREFIXES_ENV,
    governance_tag_key_globs,
    governance_tag_key_prefixes_env_value,
)
from stacks.platform_stack import PlatformStack

TAG_ACTION = "bedrock-agentcore:TagResource"


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "GovernanceTagNamespaceStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


def _lambda_envs(template_json: dict) -> dict[str, dict]:
    return {
        lid: (res.get("Properties", {}).get("Environment", {}) or {}).get("Variables", {}) or {}
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::Lambda::Function"
    }


def test_the_namespaces_are_namespaces():
    """A bound that is not a bound. ``("",)`` or ``("*",)`` would satisfy every agreement
    assertion in this file while authorizing every tag key in the account, which is the
    privilege-escalation surface the allowlist exists to close. Asserted against the VALUE's
    shape rather than against a copy of it, so widening the constant for a legitimate second
    namespace stays a one-line edit and widening it to everything does not."""
    assert GOVERNANCE_TAG_KEY_PREFIXES, "an empty namespace tuple refuses every governance tag"
    for prefix in GOVERNANCE_TAG_KEY_PREFIXES:
        assert prefix.endswith(":"), f"{prefix!r} must end with ':' so it names a namespace, not a key prefix"
        assert len(prefix) > 1, f"{prefix!r} bounds nothing"
        assert "*" not in prefix and "?" not in prefix, (
            f"{prefix!r} contains an IAM wildcard character, so the glob built from it would "
            "match more than the namespace it appears to name"
        )
    assert governance_tag_key_globs() == [f"{p}*" for p in GOVERNANCE_TAG_KEY_PREFIXES]


def test_every_tag_on_create_grant_admits_exactly_these_namespaces(template_json):
    """The IAM half. Every ``bedrock-agentcore:TagResource`` statement in the stack, not just
    the runtime's: the same denial was waiting for memory, gateway, harness and the policy
    engine, each behind its own step role's copy of the condition."""
    globs = governance_tag_key_globs()
    checked = 0
    for lid, res in template_json["Resources"].items():
        if res["Type"] not in {"AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"}:
            continue
        for statement in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = [actions] if isinstance(actions, str) else actions or []
            if TAG_ACTION not in actions or statement.get("Effect") != "Allow":
                continue
            checked += 1
            cond = statement.get("Condition") or {}
            allowed = (cond.get("ForAllValues:StringLike") or {}).get("aws:TagKeys")
            assert allowed, f"{lid}: {TAG_ACTION} with no aws:TagKeys namespace allowlist: {cond}"
            missing = [g for g in globs if g not in allowed]
            assert not missing, (
                f"{lid}: the tag-key allowlist {allowed!r} omits {missing!r}, so every deploy "
                "carrying a governance tag is denied at create time -- the live regression this "
                "file documents"
            )
            # And the namespaces are the ONLY wildcards. A bare `*` alongside them would pass
            # the assertion above while making the whole condition vacuous.
            wild = [k for k in allowed if ("*" in k or "?" in k) and k not in globs]
            assert not wild, (
                f"{lid}: the allowlist carries wildcard entries {wild!r} that are not declared "
                "namespaces; a pattern outside GOVERNANCE_TAG_KEY_PREFIXES bounds nothing"
            )

    # Reach before verdict: a template walk that matched nothing would report every grant
    # compliant. Six steps tag on create (runtime_configure, mcp_server, gateway, harness,
    # memory, policy), so anything below that is a broken walk rather than a clean stack.
    assert checked >= 6, f"only {checked} {TAG_ACTION} statements were inspected; the walk is probably broken"


def test_the_backend_is_told_the_same_namespaces_it_is_bounded_by(template_json):
    """The other half. Both the API Lambda (which refuses at the boundary) and the step Lambdas
    (which build the merged tag set) must carry the value, or the refusal happens where it
    cannot be explained -- inside a deployment that has already created resources."""
    expected = governance_tag_key_prefixes_env_value()
    assert expected == ",".join(GOVERNANCE_TAG_KEY_PREFIXES)

    carriers, wrong = [], []
    for lid, env in _lambda_envs(template_json).items():
        # The deploy-path Lambdas are the ones that resolve or apply a governance tag set; a
        # front-door Lambda with no tag code does not need the value. TAG_POLICY_TABLE_NAME is
        # the honest marker for "this function reads tag policies", and it is set by the same
        # two builders that set this variable.
        if "TAG_POLICY_TABLE_NAME" not in env:
            continue
        carriers.append(lid)
        if env.get(GOVERNANCE_TAG_KEY_PREFIXES_ENV) != expected:
            wrong.append((lid, env.get(GOVERNANCE_TAG_KEY_PREFIXES_ENV)))

    assert carriers, "no Lambda reads the tag-policy table, so this agreement check is vacuous"
    assert not wrong, (
        f"these tag-policy-reading Lambdas disagree with the IAM allowlist: {wrong}. The API "
        "would then accept a governance tag key the deployed policy denies, which is the "
        "original outage with an extra step."
    )


def test_the_backend_default_matches_the_deployed_value():
    """The backend's fallback, for a Lambda from a stack that predates the variable.

    A default that did not match would make an older step Lambda refuse tags the running policy
    allows (or, worse, accept ones it denies) with nothing in the template to show why. Read out
    of the backend rather than restated, so the two constants cannot drift silently.
    """
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "resource_tagging.py"
    ).read_text(encoding="utf-8")
    match = re.search(r"_DEFAULT_GOVERNANCE_TAG_KEY_PREFIXES = \(([^)]*)\)", source)
    assert match, "the backend no longer declares _DEFAULT_GOVERNANCE_TAG_KEY_PREFIXES"
    default = tuple(part.strip().strip("\"'") for part in match.group(1).split(",") if part.strip())
    assert default == GOVERNANCE_TAG_KEY_PREFIXES, (
        f"the backend defaults to {default} while this stack deploys {GOVERNANCE_TAG_KEY_PREFIXES}"
    )
    env_match = re.search(r'GOVERNANCE_TAG_KEY_PREFIXES_ENV = "([^"]+)"', source)
    assert env_match, "the backend no longer declares GOVERNANCE_TAG_KEY_PREFIXES_ENV"
    assert env_match.group(1) == GOVERNANCE_TAG_KEY_PREFIXES_ENV, (
        f"the backend reads {env_match.group(1)!r} while this stack sets "
        f"{GOVERNANCE_TAG_KEY_PREFIXES_ENV!r}; an unread variable is silently the default"
    )
