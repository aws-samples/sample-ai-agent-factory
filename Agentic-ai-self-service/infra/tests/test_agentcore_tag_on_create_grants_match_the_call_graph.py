"""A ``tags=`` on a create is a second authorization, and forgetting it is an outage.

F-47. Every ``bedrock-agentcore`` create this platform makes now passes ``tags=``, and
passing tags is authorized as a separate ``bedrock-agentcore:TagResource`` on each resource
the create tags -- not as part of the Create action. The stack held exactly ONE such grant
(``runtime/*``, for ``runtime_configure`` and ``mcp_server``) while six step roles needed
eight resource patterns between them. Proven live, not inferred: ``create_gateway`` was
denied on ``gateway/*`` and ``create_agent_runtime`` on
``workload-identity-directory/default/workload-identity/*``, the latter a resource nothing
in this codebase creates -- AgentCore mints a workload identity for the runtime and tags
that too. The gateway, memory, policy and harness deploy paths were all dead.

**Why no existing test saw it.** The suite's IAM tests audit whether a granted action is
reachable (the over-reach direction). This is the opposite question -- is everything the
handler needs granted -- and the module docstring of ``handler_call_graph`` is explicit that
its over-estimating graph is not sound for it. That is still true, and it is why this file
asserts a TABLE in ``step_lambdas.py`` rather than deriving the grant from the graph: the
graph is the auditor, the table is the product, and where they disagree the disagreement has
to be written down as a named blind spot with the call site that settles it.

**Direction of each assertion, stated because they point opposite ways.** The subset check
(graph types must be granted) fails toward an outage and must never be relaxed by widening
the blind-spot set without a call-site proof. The staleness check (granted types must be
graph-confirmed or named) fails toward over-grant, and a stale entry here is a live
tag-forging capability on a resource type nothing creates any more.
"""

import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import governance_tag_key_globs
from stacks.platform.step_lambdas import AGENTCORE_TAG_ON_CREATE_TYPES, AGENTCORE_TYPE_ARN_TAIL
from stacks.platform_stack import PlatformStack

from tests.handler_call_graph import (
    AGENTCORE_CREATE_TO_TAGGED_TYPES,
    reachable_agentcore_tagged_types,
    step_handler_refs,
)
from tests.iam_attachment import statements_by_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
TAG_ACTION = "bedrock-agentcore:TagResource"

_STEP_LAMBDAS_PY = pathlib.Path(__file__).resolve().parents[1] / "stacks" / "platform" / "step_lambdas.py"

#: ``(step, resource type)`` granted in the table but invisible to the call graph, each with
#: the call site that proves it is needed. These are NOT exemptions from needing the grant --
#: they are exemptions from the graph being able to confirm it, and every one must name a
#: file and line. ``handler_call_graph`` does not follow a call made on an imported MODULE
#: object, which is precisely the shape here: ``harness_step.py:22`` does
#: ``from app.services import harness_deployer`` and ``harness_step.py:146`` calls
#: ``harness_deployer.ensure_gateway_outbound_provider(...)``, whose body reaches
#: ``create_oauth2_credential_provider`` at ``harness_deployer.py:464``. Read as a warning,
#: not as bookkeeping: the graph missing an edge is how a create stays ungranted, so a new
#: entry here should be the trigger to check whether the grant is right at all.
_GRAPH_BLIND_SPOTS = {
    ("harness", "oauth2credentialprovider"): (
        "harness_step.py:146 -> harness_deployer.ensure_gateway_outbound_provider "
        "-> harness_deployer.py:464 create_oauth2_credential_provider"
    ),
}


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="acf",
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def handler_refs():
    refs = step_handler_refs(_STEP_LAMBDAS_PY)
    assert len(refs) >= 10, f"parsed only {len(refs)} step handlers: {refs}"
    return refs


def _role_logical_id(template_json: dict, step_name: str) -> str:
    want = "Step" + step_name.title().replace("_", "") + "Role"
    ids = [
        lid
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::IAM::Role" and lid.startswith(want)
    ]
    assert len(ids) <= 1, f"ambiguous role for step {step_name}: {ids}"
    return ids[0] if ids else ""


def _tag_statements(statements: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    out = []
    for src, st in statements:
        if st.get("Effect") != "Allow":
            continue
        raw = st.get("Action")
        if TAG_ACTION in ([raw] if isinstance(raw, str) else raw or []):
            out.append((src, st))
    return out


def test_every_tagging_step_role_grants_exactly_the_resources_its_creates_tag(template_json) -> None:
    """One statement per role, resources equal to the table -- not a superset, not a subset.

    Equality rather than containment because CloudFormation merges statements whose action
    sets are equal by unioning their RESOURCES (F-46). A second
    ``["bedrock-agentcore:TagResource"]`` statement added anywhere in this role's policy
    would therefore fuse into this one and widen it invisibly; asserting equality is what
    detects that, and asserting a single statement is what keeps the conditions meaningful
    (two merged statements cannot keep two different condition blocks).
    """
    by_role = statements_by_role(template_json)
    checked = []
    for step_name, types in sorted(AGENTCORE_TAG_ON_CREATE_TYPES.items()):
        role_lid = _role_logical_id(template_json, step_name)
        assert role_lid, f"step {step_name} is in AGENTCORE_TAG_ON_CREATE_TYPES but has no role in the template"
        stmts = _tag_statements(by_role.get(role_lid, []))
        assert len(stmts) == 1, (
            f"step {step_name} has {len(stmts)} {TAG_ACTION} statements, expected exactly 1. "
            "Two statements with this action set will be merged by CloudFormation into one "
            "with the union of their resources; keep it to a single statement built from "
            "AGENTCORE_TAG_ON_CREATE_TYPES."
        )
        src, st = stmts[0]
        want = {f"arn:aws:bedrock-agentcore:*:{ACCOUNT}:{tail}" for t in types for tail in AGENTCORE_TYPE_ARN_TAIL[t]}
        raw_res = st.get("Resource")
        got = set([raw_res] if isinstance(raw_res, str) else raw_res or [])
        assert got == want, f"step {step_name} ({src}) tags {sorted(got)}, table says {sorted(want)}"

        # The resources are all wildcards, so the conditions ARE the scope. Asserted here
        # rather than in its own test because a resource list without them is not a
        # narrower grant, it is an account-wide one: TagResource on `memory/*` with no
        # condition lets this role relabel every memory in the account, foreign included.
        cond = st.get("Condition") or {}
        eq = cond.get("StringEquals") or {}
        assert eq.get("aws:RequestTag/ManagedBy") == "agentcore-flows", (step_name, cond)
        # Both ownership keys, and AgentCoreStack's region as a POLICY VARIABLE rather than
        # the literal home region -- a literal would deny every allowlisted cross-region
        # deploy at create time, which is the outage this grant exists to prevent.
        assert eq.get("aws:RequestTag/AgentCoreStack") == "acf-test-${aws:RequestedRegion}", (step_name, cond)
        expected_keys = ["ManagedBy", "AgentCoreStack"]
        if step_name == "memory":
            expected_keys.extend(["OwnerSubHash", "DeploymentId"])
        # Plus the governance namespaces, under StringLike because a namespace cannot be
        # expressed with StringEquals. Derived from the constant the stack uses, never retyped:
        # the two halves disagreeing is a denied deploy, which is how this was found (see
        # config.GOVERNANCE_TAG_KEY_PREFIXES and the live AccessDenied it records).
        expected_keys.extend(governance_tag_key_globs())
        assert cond.get("ForAllValues:StringLike", {}).get("aws:TagKeys") == expected_keys, (step_name, cond)
        # StringEquals on aws:TagKeys cannot express a namespace, so its presence here would
        # mean a second, narrower allowlist that denies every governance key -- the outage this
        # replaced. An empty key allowlist is not a stricter grant either; both are failures.
        assert "ForAllValues:StringEquals" not in cond, (
            f"{step_name} carries a ForAllValues:StringEquals allowlist alongside the namespace "
            f"one; IAM requires EVERY condition to pass, so this denies governance keys: {cond}"
        )

        shapes = cond.get("StringLike") or {}
        if step_name == "memory":
            assert shapes == {
                "aws:RequestTag/OwnerSubHash": "?" * 32,
                "aws:RequestTag/DeploymentId": "????????-????-????-????-????????????",
            }, (step_name, cond)
        else:
            assert not shapes, (
                f"only StepMemoryRole sends caller/deployment binding tags; "
                f"{step_name} unexpectedly gained value-shape conditions: {shapes}"
            )
        # Plain StringEquals, never StringEqualsIfExists: the IfExists variant passes when
        # the key is absent, so a request sending no tags at all would satisfy it.
        assert "StringEqualsIfExists" not in cond, (step_name, cond)
        checked.append(step_name)

    assert len(checked) == len(AGENTCORE_TAG_ON_CREATE_TYPES) >= 6, checked


def test_a_step_that_creates_nothing_taggable_holds_no_tagresource(template_json, handler_refs) -> None:
    """The nine steps absent from the table must hold none of this action.

    ``codegen`` is the one that matters most and the reason this is its own test rather than
    an implication of the table: it runs model-authored code (ARCC cnt_oikES5IaGqdpqw), so a
    tagging primitive on that role lets generated code claim this deployment's ownership of
    an arbitrary AgentCore resource and hand it to teardown.
    """
    by_role = statements_by_role(template_json)
    untabled = sorted(set(handler_refs) - set(AGENTCORE_TAG_ON_CREATE_TYPES))
    assert "codegen" in untabled, (
        f"codegen is expected to create nothing taggable; table={AGENTCORE_TAG_ON_CREATE_TYPES}"
    )
    offenders = []
    for step_name in untabled:
        role_lid = _role_logical_id(template_json, step_name)
        if role_lid and _tag_statements(by_role.get(role_lid, [])):
            offenders.append(step_name)
    assert not offenders, (
        f"these steps create nothing that gets tagged yet hold {TAG_ACTION}: {offenders}. "
        "Either the step now makes a tagged create -- add it to AGENTCORE_TAG_ON_CREATE_TYPES "
        "with the call site -- or the grant is unused authority and should go."
    )


def test_every_tagged_create_a_handler_can_reach_is_granted(handler_refs) -> None:
    """The outage direction: a create the graph can see must be in the table.

    This is the test that would have caught F-47 before the deploy. It is the direction the
    ``handler_call_graph`` docstring calls unsound for its Lambda-action use, and that is
    accounted for rather than ignored: an over-estimate here demands a grant that may not be
    needed, whose worst case is a role able to stamp THIS deployment's own ownership tags on
    a resource of that type, versus a dead deploy path for the under-grant. Over-granting is
    the correct side to err on, so the over-estimate is a feature here.
    """
    missing = []
    seen_any = 0
    for step_name, ref in sorted(handler_refs.items()):
        types, reached, sites = reachable_agentcore_tagged_types(ref)
        if types:
            seen_any += 1
            assert reached > 1, f"call graph for {step_name} ({ref}) reached only {reached} functions"
        granted = set(AGENTCORE_TAG_ON_CREATE_TYPES.get(step_name, ()))
        for t in sorted(types - granted):
            missing.append((step_name, t, sorted(sites.get(t, set()))[:1] or sorted(sites.items())[:1]))

    assert seen_any >= 6, f"only {seen_any} steps reached a tagged create -- parse or resolver failure"
    assert not missing, (
        f"these steps can reach a create that tags a resource type their role cannot tag "
        f"(step, type, call site): {missing}. Every create on these paths sends "
        "owner_tags() unconditionally, so this is a hard AccessDeniedException on that "
        "deploy path, not a missing label. Add the type to AGENTCORE_TAG_ON_CREATE_TYPES."
    )


def test_no_granted_resource_type_has_outlived_the_create_that_needed_it(handler_refs) -> None:
    """The over-grant direction, and the reason the blind-spot set needs call sites.

    A table entry whose create is gone is a live tag-forging capability on a resource type
    this platform no longer makes. The blind-spot set is the only escape hatch and it is
    audited too: an entry that the graph CAN now confirm is removed from it, so the set
    cannot quietly accumulate into a blanket exemption.
    """
    confirmed: set[tuple[str, str]] = set()
    for step_name, ref in sorted(handler_refs.items()):
        types, _, _ = reachable_agentcore_tagged_types(ref)
        confirmed.update((step_name, t) for t in types)

    tabled = {(s, t) for s, ts in AGENTCORE_TAG_ON_CREATE_TYPES.items() for t in ts}
    unconfirmed = tabled - confirmed
    assert unconfirmed <= set(_GRAPH_BLIND_SPOTS), (
        f"granted but no reachable create tags it, and not a declared blind spot: "
        f"{sorted(unconfirmed - set(_GRAPH_BLIND_SPOTS))}. If the create is gone, remove the "
        "type from AGENTCORE_TAG_ON_CREATE_TYPES. If the graph simply cannot see the call, "
        "add it to _GRAPH_BLIND_SPOTS with the file:line that proves it."
    )
    stale_blind_spots = set(_GRAPH_BLIND_SPOTS) & confirmed
    assert not stale_blind_spots, (
        f"the call graph can now see these, so they are no longer blind spots: "
        f"{sorted(stale_blind_spots)}. Remove them from _GRAPH_BLIND_SPOTS -- an entry that "
        "no longer describes a blind spot is an unaudited exemption for the next one."
    )
    assert set(_GRAPH_BLIND_SPOTS) <= tabled, (
        f"_GRAPH_BLIND_SPOTS names pairs that are not granted at all: {sorted(set(_GRAPH_BLIND_SPOTS) - tabled)}"
    )


def test_the_arn_table_and_the_create_map_cover_each_other() -> None:
    """Non-vacuity for the two tables themselves, which every other test here trusts.

    A type in the grant table with no ARN tail is a synth-time ``KeyError`` rather than a
    silent pass, so that direction is already loud. The quiet one is the reverse: an ARN tail
    or a create-map entry left behind after its type stopped being granted reads as coverage
    that does not exist.
    """
    tabled_types = {t for ts in AGENTCORE_TAG_ON_CREATE_TYPES.values() for t in ts}
    assert tabled_types <= set(AGENTCORE_TYPE_ARN_TAIL), sorted(tabled_types - set(AGENTCORE_TYPE_ARN_TAIL))
    assert set(AGENTCORE_TYPE_ARN_TAIL) == tabled_types, (
        f"AGENTCORE_TYPE_ARN_TAIL has entries no step is granted: "
        f"{sorted(set(AGENTCORE_TYPE_ARN_TAIL) - tabled_types)}. Harmless today, but it is "
        "how a shape nobody verified gets picked up by the next grant."
    )
    mapped_types = {t for ts in AGENTCORE_CREATE_TO_TAGGED_TYPES.values() for t in ts}
    assert tabled_types <= mapped_types, (
        f"these types are granted but no create in AGENTCORE_CREATE_TO_TAGGED_TYPES produces "
        f"them, so the call-graph audit above cannot ever confirm them: "
        f"{sorted(tabled_types - mapped_types)}"
    )
    # Every ARN tail must start with its own type name or be a documented nested shape --
    # a transposed table would otherwise grant `memory/*` where `harness/*` was meant.
    #
    # A nested shape must ALSO carry its container ARN, because AgentCore authorizes one
    # logical call against both and the 403 names whichever it checked first. Asserted here
    # rather than left to the live deploy: a child-only grant is an outage that looks like a
    # correctly scoped policy, and it cost a whole AgentCore runtime deploy on 2026-09-22.
    nested = {"workload-identity", "oauth2credentialprovider", "apikeycredentialprovider"}
    for t, tails in AGENTCORE_TYPE_ARN_TAIL.items():
        assert isinstance(tails, tuple), (t, tails)
        if t in nested:
            child = [x for x in tails if x.endswith(f"{t}/*")]
            assert len(child) == 1, (t, tails)
            # The container is the child's ARN minus its last two segments, derived from the
            # child rather than written out again so the two cannot drift apart.
            container = child[0].rsplit("/", 2)[0]
            assert container.endswith("/default"), (t, container)
            assert set(tails) == {container, child[0]}, (
                f"nested shape {t} must grant exactly its container and its child, got "
                f"{sorted(tails)}; expected {sorted({container, child[0]})}"
            )
        else:
            assert tails == (f"{t}/*",), (t, tails)
