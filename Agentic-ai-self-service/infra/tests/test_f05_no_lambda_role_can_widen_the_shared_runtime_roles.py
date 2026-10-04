"""F-05: the admin chain through the shared runtime role, closed at three points.

1. ``iam:PutRolePolicy`` on ``role/AgentCore*`` matches ``AgentCoreRuntime-{project}-{env}-shared``,
   the role every shared-mode tenant's agent runs as. Every platform Lambda role that holds any
   role-mutating verb on ``role/AgentCore*`` now also carries an explicit Deny of those verbs on
   the two shared roles (exact ARNs) and on the ``AgentCoreRuntime-*-shared`` convention
   (``shared_role_guard.py``).
2. ``iam:PassRole`` on the same prefix had no ``iam:PassedToService``. Every PassRole grant in the
   template now names the service, and the set of services is exactly the ones the backend
   passes roles to: AgentCore (CreateAgentRuntime / UpdateAgentRuntime / CreateGateway /
   CreateMemory / CreateHarness / CreateOnlineEvaluationConfig), Lambda (create_function) and
   Bedrock (CreateKnowledgeBase). The deployment Lambda may pass a role to Lambda ONLY on the
   sandbox role prefix ``AgentCore-ToolSandbox-*`` (tool_tester.SANDBOX_ROLE_PREFIX), the one
   role it ever hands to ``lambda:CreateFunction``.
3. The deployment Lambda's second ``iam:PutRolePolicy`` (justified by a harness direct-deploy
   path this Lambda does not import: ``create_harness_iam_role`` is reached only from
   ``harness_step``) is gone; the JIT approver's is the one PutRolePolicy it holds, and its
   ``iam:CreateRole`` is scoped to the sandbox prefix, the only role it creates.

The JIT router's acceptance of ``Resource: ["*"]`` (routers/permissions.py) is backend scope and
is recorded in the ledger, not fixed here. ARCC cnt_SFJJhkOueCPRkd, cnt_dwzZ05hLnqhYXQ.
"""

from __future__ import annotations

import json

import pytest
from stacks.platform.shared_role_guard import SHARED_ROLE_MUTATING_ACTIONS, SHARED_ROLE_NAME_PATTERNS

from tests.iam_attachment import role_logical_id, statements_by_role, statements_for_role
from tests.p1_synth import actions, all_statements, lambda_roles, resources_text, synth

PASS = "iam:PassRole"
KNOWN_PASS_TARGETS = {"bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com", "bedrock.amazonaws.com"}
SANDBOX_ROLE_PREFIX = "role/AgentCore-ToolSandbox-"


@pytest.fixture(scope="module")
def tpl() -> dict:
    return synth("F05SharedRoleChainStack")


def _shared_role_lids(tpl) -> tuple[str, str]:
    return role_logical_id(tpl, "SharedRuntimeExecRole"), role_logical_id(tpl, "SharedMcpRuntimeExecRole")


def _mutates_agentcore_roles(st: dict) -> bool:
    return (
        st.get("Effect") == "Allow"
        and any(a in SHARED_ROLE_MUTATING_ACTIONS for a in actions(st))
        and any("role/AgentCore" in r for r in resources_text(st))
    )


def test_the_walk_finds_roles_that_can_mutate_agentcore_roles(tpl):
    holders = [
        lid for lid, sts in statements_by_role(tpl).items() if any(_mutates_agentcore_roles(st) for _p, st in sts)
    ]
    assert len(holders) >= 8, holders  # deployment Lambda + 7 step roles + status_update at least


def test_every_role_that_can_mutate_an_agentcore_role_is_denied_the_shared_ones(tpl):
    """The assertion that fails on the pre-fix tree."""
    model_lid, mcp_lid = _shared_role_lids(tpl)
    for lid, sts in statements_by_role(tpl).items():
        if not any(_mutates_agentcore_roles(st) for _p, st in sts):
            continue
        denies = [
            st for _p, st in sts if st.get("Effect") == "Deny" and set(SHARED_ROLE_MUTATING_ACTIONS) <= set(actions(st))
        ]
        assert denies, f"{lid}: can mutate role/AgentCore* but carries no Deny for the shared runtime roles"
        text = " ".join(" ".join(resources_text(st)) for st in denies)
        assert model_lid in text and mcp_lid in text, f"{lid}: the Deny does not name both shared roles' ARNs: {text}"
        for pattern in SHARED_ROLE_NAME_PATTERNS:
            assert pattern in text, f"{lid}: the Deny does not cover the naming convention {pattern}"


def test_every_pass_role_in_the_template_names_the_service_it_passes_to(tpl):
    seen: set[str] = set()
    for pid, st in all_statements(tpl):
        if st.get("Effect") != "Allow" or PASS not in actions(st):
            continue
        svc = (st.get("Condition") or {}).get("StringEquals", {}).get("iam:PassedToService")
        assert svc, f"{pid}: iam:PassRole without iam:PassedToService"
        seen |= set([svc] if isinstance(svc, str) else svc)
    assert seen, "no iam:PassRole grant found at all; walk broken"
    assert seen <= KNOWN_PASS_TARGETS, seen


def test_the_deployment_lambda_passes_to_lambda_only_on_the_sandbox_role(tpl):
    lid = role_logical_id(tpl, "DeploymentLambdaRole")
    to_lambda = []
    to_agentcore = []
    for pid, st in statements_for_role(tpl, lid):
        if st.get("Effect") != "Allow" or PASS not in actions(st):
            continue
        svc = (st.get("Condition") or {}).get("StringEquals", {}).get("iam:PassedToService")
        svcs = set([svc] if isinstance(svc, str) else (svc or []))
        if "lambda.amazonaws.com" in svcs:
            to_lambda.append((pid, st))
            assert svcs == {"lambda.amazonaws.com"}, f"{pid}: the Lambda pass shares a statement with {svcs}"
            for r in resources_text(st):
                assert SANDBOX_ROLE_PREFIX in r, f"{pid}: passes to Lambda on {r}, wider than the sandbox role"
        if "bedrock-agentcore.amazonaws.com" in svcs:
            to_agentcore.append(pid)
    assert to_lambda, "the deployment Lambda cannot pass the sandbox role to Lambda; every tool test would fail"
    assert to_agentcore, "the deployment Lambda cannot re-pass a runtime role to AgentCore; UpdateAgentRuntime dies"


def test_the_deployment_lambda_holds_exactly_the_jit_put_role_policy_and_creates_only_the_sandbox_role(tpl):
    lid = role_logical_id(tpl, "DeploymentLambdaRole")
    sts = statements_for_role(tpl, lid)
    puts = [st for _p, st in sts if st.get("Effect") == "Allow" and "iam:PutRolePolicy" in actions(st)]
    assert len(puts) == 1, (
        f"expected the JIT approver's PutRolePolicy alone, found {len(puts)}: {[actions(s) for s in puts]}"
    )
    assert actions(puts[0]) == ["iam:PutRolePolicy"], (
        "PutRolePolicy must stand alone so the boundary condition can be put on it: " + str(actions(puts[0]))
    )
    # The JIT approver's read half is still granted (routers/permissions.py reads before it widens).
    assert any(st.get("Effect") == "Allow" and "iam:GetRolePolicy" in actions(st) for _p, st in sts)
    creates = [
        st
        for _p, st in statements_for_role(tpl, lid)
        if st.get("Effect") == "Allow" and "iam:CreateRole" in actions(st)
    ]
    assert creates, "the deployment Lambda lost iam:CreateRole; the tool sandbox cannot be created"
    for st in creates:
        for r in resources_text(st):
            assert SANDBOX_ROLE_PREFIX in r, f"iam:CreateRole wider than the sandbox role: {r}"


def test_the_deny_reaches_every_lambda_role_that_can_touch_iam(tpl):
    """Template-wide over-reach guard in the other direction: no Lambda role holds an iam:
    write verb on role/AgentCore* without the Deny. Roles that hold no such verb are exempt --
    the Deny is not sprinkled where it protects nothing."""
    for lid in lambda_roles(tpl):
        sts = statements_by_role(tpl).get(lid, [])
        if not any(_mutates_agentcore_roles(st) for _p, st in sts):
            continue
        assert any(st.get("Effect") == "Deny" and "iam:PutRolePolicy" in actions(st) for _p, st in sts), lid


def test_the_deny_statement_is_well_formed(tpl):
    """A Deny that CloudFormation rejects would fail the deploy, not the attacker."""
    lid = role_logical_id(tpl, "DeploymentLambdaRole")
    denies = [st for _p, st in statements_for_role(tpl, lid) if st.get("Effect") == "Deny"]
    assert denies
    for st in denies:
        json.dumps(st)  # serialisable
        assert st.get("Resource"), st
