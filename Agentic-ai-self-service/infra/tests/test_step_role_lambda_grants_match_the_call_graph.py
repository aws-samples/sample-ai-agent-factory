"""No step role may hold a Lambda action its own handler cannot reach.

The instance this was written for, and it was a real one. ``_create_step_role`` gated the
eleven-action Lambda mutation grant on
``step_name in {"gateway", "mcp_server", "codegen", "knowledge_base"}`` -- four steps that
all sound like they deploy code. Three of them make no Lambda API call at all. So
``StepMcpServerRole``, ``StepCodegenRole`` and ``StepKnowledgeBaseRole`` each held
``lambda:CreateFunction`` and ``lambda:UpdateFunctionCode`` on ``function:AgentCore*``,
and ARCC ``cnt_L4ZLZgjrCctfxl`` names precisely that as an escalation path: replacing a
function's code makes that function's EXECUTION role run yours. Three roles silently held
every tool Lambda's identity for a capability none of them used. ``cnt_oikES5IaGqdpqw`` is
blunter still about the codegen step in particular -- "Lambda is a form of a
terminal/compiler/interpreter ... access to lambda creation or edit should not be allowed"
on a path fed by model output, which is exactly what codegen_step is.

Fixed 2026-09-22 by splitting the block per step. This file is the part that matters more
than the fix: the next step added to ``step_configs`` gets the same audit for free, because
the step list is PARSED out of ``step_lambdas.py`` rather than transcribed here.

**What this test can and cannot conclude.** ``handler_call_graph`` matches boto3 method
names without knowing the receiver, so its reachable set over-estimates -- which is what
makes the over-reach question below sound, since an over-estimate can only make the
assertion more permissive. The opposite question ("is everything the handler needs
granted?") is NOT asked here and must not be added here: an over-estimate would demand
``lambda:GetPolicy`` for ``policy_step``'s ``bedrock-agentcore:GetPolicy``. That direction
belongs to the tests that name their call site (``test_tool_sandbox_grant``,
``test_the_tool_lambda_ownership_grant``) and to the live deploy, which is the project's
actual bar.

**If this test fires, the grant is the suspect, not the graph -- but check the graph
first.** It does not follow calls through an imported module object, a variable or a
callback, so it can under-report. Prove the call site is unreachable before deleting a
grant; the evidence recorded in ``step_lambdas.py`` for the three roles above is the shape
that proof should take. Deleting a needed grant here is an outage on a path with no
fallback, not a silent weakening.
"""

import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.handler_call_graph import reachable_lambda_actions, step_handler_refs
from tests.iam_attachment import statements_by_role

REGION = "us-east-1"
ACCOUNT = "123456789012"

_STEP_LAMBDAS_PY = pathlib.Path(__file__).resolve().parents[1] / "stacks" / "platform" / "step_lambdas.py"


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
    # Non-vacuity: an empty or tiny parse would make every assertion below skip its step.
    assert len(refs) >= 10, f"parsed only {len(refs)} step handlers out of step_lambdas.py: {refs}"
    assert refs.get("gateway") == "src/app/step_handlers/gateway_step.handler", refs.get("gateway")
    return refs


def _role_logical_id(template_json: dict, step_name: str) -> str:
    """``StepGatewayRole...`` for ``gateway`` -- the same name CDK builds in the stack.

    Derived from the step name with CDK's own transform rather than looked up in a table,
    so a renamed step cannot quietly stop being audited. Returns ``""`` when no role
    matches, which the caller treats as "nothing to check" only after asserting that at
    least one step DID match.
    """
    want = "Step" + step_name.title().replace("_", "") + "Role"
    ids = [
        lid
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::IAM::Role" and lid.startswith(want)
    ]
    assert len(ids) <= 1, f"ambiguous role for step {step_name}: {ids}"
    return ids[0] if ids else ""


def _granted_lambda_actions(statements: list[tuple[str, dict]]) -> dict[str, list[str]]:
    """``{action: [policy logical ids]}`` for every Allow on a ``lambda:`` action."""
    out: dict[str, list[str]] = {}
    for src, st in statements:
        if st.get("Effect") != "Allow":
            continue
        raw = st.get("Action")
        for action in [raw] if isinstance(raw, str) else raw or []:
            if isinstance(action, str) and action.startswith("lambda:"):
                out.setdefault(action, []).append(src)
    return out


#: ``(step, action)`` pairs allowed to reach EVERY function in the account with no
#: condition. One entry, and it has to stay one: the KB step reads tags off
#: ``kb_config["transformationLambdaArn"]``, a caller-supplied ARN that may name any
#: function, and that read IS the ownership decision -- conditioning it on
#: ``aws:ResourceTag/...=allow`` would gate the lookup on its own answer. ListTags
#: discloses tags and mutates nothing, which is what makes the trade acceptable.
_ACCOUNT_WIDE_LAMBDA_ALLOWLIST = {("knowledge_base", "lambda:ListTags")}


def _account_wide_lambda_statements(statements: list[tuple[str, dict]]) -> list[tuple[str, str, bool]]:
    """``(policy id, action, has_condition)`` for every Allow reaching ``function:*``.

    Matches the bare wildcard only. ``function:AgentCore*`` is a prefix and is governed by
    the per-action tests above; ``function:*`` is every Lambda in the account, including
    the foreign workloads this account demonstrably holds.
    """
    out: list[tuple[str, str, bool]] = []
    for src, st in statements:
        if st.get("Effect") != "Allow":
            continue
        raw_res = st.get("Resource")
        resources = [raw_res] if isinstance(raw_res, str) else raw_res or []
        if not any(isinstance(r, str) and r.endswith(":function:*") for r in resources):
            continue
        raw = st.get("Action")
        for action in [raw] if isinstance(raw, str) else raw or []:
            if isinstance(action, str) and action.startswith("lambda:"):
                out.append((src, action, bool(st.get("Condition"))))
    return out


def test_an_account_wide_lambda_grant_on_a_step_role_is_conditioned_or_allowlisted(template_json, handler_refs) -> None:
    """Resource scope, not just the action -- the dimension the tests above cannot see.

    Found by reading the LIVE policy after the 2026-09-22 deploy, not by any synth
    assertion. A narrow ``lambda:ListTags`` on ``function:AgentCore*`` had been added to
    the knowledge_base role beside a pre-existing ``lambda:ListTags`` on ``function:*``.
    CDK unions the resources of statements whose action sets are EQUAL, so the two merged
    and the deployed statement read ``["...function:*", "...function:AgentCore*"]``: the
    narrow ARN contributed nothing and the role's real scope was still account-wide.

    Every action-set assertion in this file passed through that, and would have: the
    action was granted either way. So this test asserts on ``Resource``, and demands that
    reaching every Lambda in the account be either condition-bounded (the gateway's
    bring-your-own-target statements are, on ``aws:ResourceTag/AgentCoreGatewayTarget``)
    or named in ``_ACCOUNT_WIDE_LAMBDA_ALLOWLIST`` with a reason. ARCC cnt_BBrFTwAEgWxA30:
    scope to the exact resources needed, and make a widening deliberate.
    """
    by_role = statements_by_role(template_json)
    offenders: list[tuple[str, str, str]] = []
    examined: list[tuple[str, str]] = []

    for step_name in sorted(handler_refs):
        role_lid = _role_logical_id(template_json, step_name)
        if not role_lid:
            continue
        for src, action, conditioned in _account_wide_lambda_statements(by_role.get(role_lid, [])):
            examined.append((step_name, action))
            if conditioned or (step_name, action) in _ACCOUNT_WIDE_LAMBDA_ALLOWLIST:
                continue
            offenders.append((step_name, action, src))

    # Non-vacuity, both halves. An empty sweep would pass silently, and a stale allowlist
    # entry is its own defect: if the KB grant is ever narrowed, the entry must go with it
    # so the next account-wide ListTags is not waved through by an obsolete exemption.
    assert examined, "no step role holds any function:* lambda grant -- resolver or parse failure"
    assert _ACCOUNT_WIDE_LAMBDA_ALLOWLIST <= set(examined), (
        f"allowlisted pairs no longer exist in the template: "
        f"{sorted(_ACCOUNT_WIDE_LAMBDA_ALLOWLIST - set(examined))}. Remove them from "
        "_ACCOUNT_WIDE_LAMBDA_ALLOWLIST rather than leaving a live exemption behind."
    )

    assert not offenders, (
        "these step roles reach EVERY function in the account with an unconditioned "
        f"lambda: grant (step, action, policy): {offenders}. This account holds foreign "
        "Lambdas. Either bound the statement with a condition -- the gateway step's "
        "AddPermission/GetPolicy/RemovePermission statements use "
        "aws:ResourceTag/AgentCoreGatewayTarget -- or add the pair to "
        "_ACCOUNT_WIDE_LAMBDA_ALLOWLIST with the reason it cannot be bounded."
    )


def test_no_step_role_holds_a_lambda_action_its_handler_cannot_call(template_json, handler_refs) -> None:
    by_role = statements_by_role(template_json)
    offenders: list[tuple[str, str, list[str]]] = []
    audited: list[str] = []

    for step_name, ref in sorted(handler_refs.items()):
        role_lid = _role_logical_id(template_json, step_name)
        if not role_lid:
            continue
        granted = _granted_lambda_actions(by_role.get(role_lid, []))
        if not granted:
            continue
        reachable, funcs_seen = reachable_lambda_actions(ref)
        # A handler ref that resolves to nothing would make every action look unreachable
        # and turn this test into a demand to delete the whole grant.
        assert funcs_seen > 1, f"call graph for {step_name} ({ref}) reached only {funcs_seen} functions"
        audited.append(step_name)
        for action, srcs in sorted(granted.items()):
            if action not in reachable:
                offenders.append((step_name, action, sorted(set(srcs))))

    # Non-vacuity in both halves: at least one step must have been audited, and the one
    # step known to hold a large Lambda grant must be among them. Without this a resolver
    # or parse regression that produced no roles at all would read as a clean pass.
    assert audited, "no step role with a lambda: grant was audited -- resolver or parse failure"
    assert "gateway" in audited, f"the gateway step was not audited; audited={audited}"

    assert not offenders, (
        "these step roles hold a lambda: action their handler cannot reach "
        f"(step, action, policy): {offenders}. ARCC cnt_L4ZLZgjrCctfxl: a Lambda mutation "
        "action lets the holder make that function's EXECUTION role run its code, so an "
        "unused grant is an inherited identity, not dead weight. Before deleting, confirm "
        "the call site is genuinely unreachable -- handler_call_graph does not follow calls "
        "through an imported module object, a variable or a callback."
    )


def test_the_gateway_step_is_the_only_step_role_that_can_create_or_replace_lambda_code(
    template_json, handler_refs
) -> None:
    """Pins the fix itself, not just the rule that produced it.

    The rule above is satisfied by a call graph agreeing with a grant, which is a moving
    target: adding a stray ``create_function`` to a step handler would make a broad grant
    legal again. This asserts the outcome directly -- exactly one step role may hold the
    escalation primitives -- so widening the blast radius has to be a deliberate edit to
    this line with a reason, per ARCC cnt_BBrFTwAEgWxA30 (build up from zero).
    """
    escalating = {"lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"}
    by_role = statements_by_role(template_json)
    holders = {
        step_name
        for step_name in handler_refs
        if (role_lid := _role_logical_id(template_json, step_name))
        and escalating & set(_granted_lambda_actions(by_role.get(role_lid, [])))
    }
    assert holders == {"gateway"}, (
        f"expected only the gateway step role to hold {sorted(escalating)}, found {sorted(holders)}. "
        "mcp_server, codegen and knowledge_base held these until 2026-09-22 and none of them "
        "makes a Lambda API call."
    )


def test_no_step_role_can_invoke_a_lambda(template_json, handler_refs) -> None:
    """``lambda:InvokeFunction`` was granted to four step roles and used by none.

    Kept as its own test because the reason is structural rather than incidental: a tool
    Lambda is invoked by the AgentCore GATEWAY's role (``AgentCoreGateway-<name>``, which
    is what the ``lambda:AddPermission`` grant exists to write into the function's resource
    policy), and the deployment Lambda's self-invokes have their own exact-ARN statement.
    No step role sits on either path, so a future InvokeFunction grant on one of these
    roles is a design error worth naming rather than a call-graph mismatch.
    """
    by_role = statements_by_role(template_json)
    holders = {
        step_name
        for step_name in handler_refs
        if (role_lid := _role_logical_id(template_json, step_name))
        and "lambda:InvokeFunction" in _granted_lambda_actions(by_role.get(role_lid, []))
    }
    assert not holders, (
        f"these step roles hold lambda:InvokeFunction and no step handler invokes a Lambda: {sorted(holders)}. "
        "Tool Lambdas are invoked by the gateway's own role via the resource policy."
    )
