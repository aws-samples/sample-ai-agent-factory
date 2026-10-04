"""A step role that can create an AgentCore resource must be able to delete it.

This class of bug is invisible until a deploy fails, and then it is invisible
*again* because the delete is wrapped in a best-effort ``except``:

  * ``bedrock-agentcore:DeleteGateway`` was missing from the gateway step role.
    Bug 134's empty-tool-plane retry -- which ``gateway_deployer`` calls "the ONLY
    deterministic cure" -- deletes the gateway and recreates it from scratch. The
    delete 403'd, the ``except`` logged it "(non-fatal)", ``deploy_gateway`` then
    re-adopted the SAME gateway id by name and re-synced the same 9 tools, and all
    three "recreations" were bit-identical. Measured live: deployment ``3ef480e2``,
    SFN run ``df698a37``, 368s of billed Lambda, and the failure still reported as
    an AgentCore service-side flake.
  * The same grant is what ``deploy_gateway``'s abort cleanup needs to release the
    gateway it just created, so every failed gateway deploy also leaked one.

So the invariant is checked two ways, and both are derived rather than listed:

  1. Symmetry on the synthesized template: for every ``Create<X>`` action a step
     role holds, it also holds ``Delete<X>``, unless the pair is waived below with
     a reason. Waivers are deliberately narrow -- a shared account-level resource
     we must never delete, or a grant that exists only so a service-side
     validation passes.
  2. Coverage from the source: every ``bedrock-agentcore`` control-plane method
     ``gateway_deployer`` actually calls must be granted to the gateway step role.
     (1) alone would not have caught the original bug, because the gateway step's
     list had no ``Delete`` verb for the gateway at all -- there was no asymmetric
     pair to notice, just an absent capability.

ARCC guidance: ``cnt_ua0cTwldOsODs8`` (dangling resources -- tap into the related
system's deletion flow so a resource dies with what created it),
``cnt_dwzZ05hLnqhYXQ`` / ``cnt_AGx9pUNpmdOVZB`` (least privilege: scope by the exact
action list, which is what makes an absent action a real capability gap rather than
a formality).
"""

import ast
import pathlib
import re

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.gateway_lock_calls import calls_through_locks, lock_method_ctrl_calls

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
ENVIRONMENT = "test"

# logical-id prefix -> the step_lambdas.py step name, for readable failures.
STEP_ROLE_PREFIXES = {
    "StepGatewayRole": "gateway",
    "StepHarnessRole": "harness",
    "StepMcpServerRole": "mcp_server",
    "StepMemoryRole": "memory",
    "StepPolicyRole": "policy",
    "StepEvaluationRole": "evaluation",
    "StepRuntimeConfigureRole": "runtime_configure",
    "StepRuntimeLaunchRole": "runtime_launch",
}

# (step, resource suffix) -> why no Delete is granted. Anything NOT in here must
# have its Delete, so adding a Create* verb forces an explicit decision.
DELETE_WAIVERS = {
    ("gateway", "TokenVault"): (
        "The token vault is the account's single shared `default` vault. The first "
        "CreateOauth2CredentialProvider in an account provisions it implicitly (Bug "
        "79/153), so the grant is needed; deleting it would destroy every other "
        "deployment's credential providers. Never delete."
    ),
    ("harness", "TokenVault"): ("Same shared `default` vault as the gateway step -- see that waiver."),
    ("mcp_server", "AgentRuntime"): (
        "The MCP server runtime is recorded in the deployment manifest as "
        "type=agent_runtime at the point of creation (mcp_server_step.py), and is "
        "deleted by the teardown role via _delete_managed_resource. The step itself "
        "has no in-step cleanup path that deletes a runtime, so granting Delete here "
        "would be reach it never uses. If an in-step abort cleanup is ever added, "
        "this waiver must go."
    ),
    ("mcp_server", "AgentRuntimeEndpoint"): (
        "CreateAgentRuntime auto-creates the DEFAULT endpoint and DeleteAgentRuntime "
        "cascades to it; the step never deletes an endpoint on its own."
    ),
}

# bedrock-agentcore control-plane methods gateway_deployer calls that are NOT IAM
# actions of the same name, or are deliberately not granted.
GATEWAY_METHOD_WAIVERS: dict[str, str] = {}

GATEWAY_DEPLOYER = (
    pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "gateway_deployer.py"
)


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _actions_for_role_prefix(template_json: dict, prefix: str) -> set[str]:
    """Every action granted to the role whose logical id starts with *prefix*.

    Reads the SYNTHESIZED template, not the source dict, so a refactor that stops
    attaching a statement cannot make this pass. CDK appends a hash to the logical
    id, hence the prefix match.

    Three attachment shapes, and the third is the one that broke this file. Past a
    role's 10,240-character inline-policy limit CDK moves the excess statements into
    auto-generated ``<Role>OverflowPolicy<N>`` resources of type
    ``AWS::IAM::ManagedPolicy`` (see ``_OverflowPolicyNagSuppressor`` in
    ``stacks/platform/nag_suppressions.py``). Reading only ``AWS::IAM::Policy``
    therefore made every overflowed statement invisible: the gateway step role's
    entire ``bedrock-agentcore:*`` block had moved, and this test reported 19
    missing control-plane grants that are in fact granted. The failure direction
    was the safe one here, but the blind spot is not directional -- a role whose
    grants overflow would equally have hidden a genuinely missing one, and a
    statement crosses the threshold for reasons that have nothing to do with it.
    """
    resources = template_json["Resources"]
    role_ids = [lid for lid, res in resources.items() if res["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)]
    assert len(role_ids) == 1, f"expected exactly one {prefix}* role in the template, found {role_ids}"
    role_id = role_ids[0]

    actions: set[str] = set()

    def _collect(statements):
        for st in statements or []:
            act = st.get("Action")
            for a in [act] if isinstance(act, str) else (act or []):
                if isinstance(a, str):
                    actions.add(a)

    # Inline policies on the role itself...
    for pol in resources[role_id]["Properties"].get("Policies", []) or []:
        _collect(pol.get("PolicyDocument", {}).get("Statement"))
    # ...every AWS::IAM::Policy attached to it (what add_to_policy produces)...
    # ...and every AWS::IAM::ManagedPolicy attached to it, which is where CDK puts
    # the statements that no longer fit inline.
    for res in resources.values():
        if res["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        roles = res["Properties"].get("Roles", []) or []
        if any(isinstance(r, dict) and r.get("Ref") == role_id for r in roles):
            _collect(res["Properties"].get("PolicyDocument", {}).get("Statement"))

    assert actions, f"{prefix}* role has no actions at all — the lookup is broken, not the policy"
    return actions


def test_the_lookup_reads_the_overflow_managed_policies(template_json):
    """Pin the blind spot itself, not just its consequence.

    Every other test in this file consumes ``_actions_for_role_prefix`` and would go
    green again the moment a statement moved back inline, leaving the omission to be
    rediscovered the next time a role grew. This asserts the two facts directly: that
    CDK really is overflowing at least one role in this stack, and that an action
    living only in an overflow policy is visible to the lookup.
    """
    resources = template_json["Resources"]
    overflow = {
        lid: res
        for lid, res in resources.items()
        if res["Type"] == "AWS::IAM::ManagedPolicy" and "OverflowPolicy" in lid
    }
    assert overflow, (
        "No OverflowPolicy* managed policy in the synthesized template. Either the step "
        "roles shrank below CDK's inline-policy ceiling -- in which case this test and the "
        "ManagedPolicy branch of _actions_for_role_prefix are now dead weight and should be "
        "removed deliberately -- or the overflow mechanism changed shape and the lookup is "
        "blind again."
    )

    checked = 0
    for lid, res in overflow.items():
        role_refs = [r.get("Ref") for r in res["Properties"].get("Roles", []) or [] if isinstance(r, dict)]
        for role_id in role_refs:
            prefix = next((p for p in (*STEP_ROLE_PREFIXES, "DeploymentLambdaRole") if role_id.startswith(p)), None)
            if prefix is None:
                continue
            overflowed_actions: set[str] = set()
            for st in res["Properties"].get("PolicyDocument", {}).get("Statement") or []:
                act = st.get("Action")
                overflowed_actions.update([act] if isinstance(act, str) else (act or []))
            seen = _actions_for_role_prefix(template_json, prefix)
            missing = sorted(a for a in overflowed_actions if isinstance(a, str) and a not in seen)
            assert not missing, (
                f"{lid} grants {missing} to {role_id}, and _actions_for_role_prefix does not see "
                "them. Every grant audit in this file is then measuring a subset of the role."
            )
            checked += 1
    assert checked, "no OverflowPolicy was attached to a role this file audits; the check proved nothing"


@pytest.mark.parametrize(("prefix", "step"), sorted(STEP_ROLE_PREFIXES.items()))
def test_every_create_has_a_matching_delete(template_json, prefix, step):
    """A step that can create a resource must be able to delete it, or every
    failure path leaks one and the swallowed AccessDenied hides that it did."""
    actions = _actions_for_role_prefix(template_json, prefix)
    # AgentCore control-plane resources ONLY. Every step role also carries shared
    # statements with non-AgentCore creates that are not leak-shaped -- e.g.
    # iam:CreateServiceLinkedRole (an account singleton the service manages) and
    # logs:CreateLogStream -- and folding those in would bury the signal in waivers.
    # Deletability of the non-AgentCore resources a step creates (IAM roles, Lambdas,
    # Cognito pools, S3 objects) is covered by the manifest teardown path instead.
    agentcore = {a for a in actions if a.startswith("bedrock-agentcore:")}
    creates = {a.split(":Create", 1)[1]: a for a in agentcore if ":Create" in a}
    delete_suffixes = {a.split(":Delete", 1)[1] for a in agentcore if ":Delete" in a}

    unwaived = {
        suffix: action
        for suffix, action in creates.items()
        if suffix not in delete_suffixes and (step, suffix) not in DELETE_WAIVERS
    }
    assert not unwaived, (
        f"step '{step}' can create but not delete: {sorted(unwaived.values())}. "
        "Either grant the matching Delete* action, or add an entry to DELETE_WAIVERS "
        "in this file explaining why the step must never delete it. A create with no "
        "delete means every failure path in that step leaks a real AWS resource."
    )


def test_the_waivers_all_still_apply(template_json):
    """A waiver that no longer describes a real asymmetry is dead documentation
    that would hide the next regression. Fail when one goes stale."""
    stale = []
    for (step, suffix), reason in DELETE_WAIVERS.items():
        prefix = next(p for p, s in STEP_ROLE_PREFIXES.items() if s == step)
        agentcore = {a for a in _actions_for_role_prefix(template_json, prefix) if a.startswith("bedrock-agentcore:")}
        creates = {a.split(":Create", 1)[1] for a in agentcore if ":Create" in a}
        deletes = {a.split(":Delete", 1)[1] for a in agentcore if ":Delete" in a}
        if suffix not in creates:
            stale.append(f"{step}/{suffix}: no Create* for it is granted any more")
        elif suffix in deletes:
            stale.append(f"{step}/{suffix}: Delete* IS granted now, so the waiver is obsolete")
        assert reason.strip(), f"{step}/{suffix} has an empty reason"
    assert not stale, "stale DELETE_WAIVERS entries: " + "; ".join(stale)


def _agentcore_methods_called_in_gateway_deployer() -> set[str]:
    """Every bedrock-agentcore control-plane method the gateway step's module calls.

    Derived from the source, so adding a new control-plane call to the deploy path
    fails this test until the grant exists -- which is exactly the loop that was
    missing when DeleteGateway was added to the code but not to the role.
    """
    src = GATEWAY_DEPLOYER.read_text()
    assert src, "gateway_deployer.py not found from the infra tests"
    # The control client is always bound to a name ending in `agentcore_ctrl`
    # (`agentcore_ctrl`, `ctrl`) in this module.
    calls = set(re.findall(r"\bagentcore_ctrl\.([a-z][a-z0-9_]*)\s*\(", src))
    calls |= set(re.findall(r"\bctrl\.([a-z][a-z0-9_]*)\s*\(", src))
    # F-66e: UpdateGateway and DeleteGateway go through the gateway write lock, so a
    # `gw_lock.delete()` under `with gateway_mutation_lock(agentcore_ctrl, ...)` is a
    # DeleteGateway on that client, and the role still needs it.
    calls |= {m for client, m in calls_through_locks(ast.parse(src)) if client.endswith("ctrl")}
    return calls


def _snake_to_pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_"))


def test_the_gateway_step_is_granted_every_control_plane_call_it_makes(template_json):
    """The generalization of the original bug: the code called DeleteGateway and the
    role did not allow it, and the call site swallowed the 403."""
    granted = _actions_for_role_prefix(template_json, "StepGatewayRole")
    methods = _agentcore_methods_called_in_gateway_deployer()
    assert "delete_gateway" in methods, (
        "the regression anchor is gone: gateway_deployer no longer calls "
        "delete_gateway, so this test would pass against nothing"
    )

    missing = sorted(
        f"{m} -> bedrock-agentcore:{_snake_to_pascal(m)}"
        for m in methods
        if m not in GATEWAY_METHOD_WAIVERS and f"bedrock-agentcore:{_snake_to_pascal(m)}" not in granted
    )
    assert not missing, (
        "gateway_deployer calls these AgentCore control-plane APIs but the gateway "
        f"step role is not granted them: {missing}. Every one of these call sites is "
        "inside a best-effort except, so the 403 will be logged as non-fatal and the "
        "deploy will report some other cause. Add the action to the 'gateway' entry "
        "of agentcore_steps in infra/stacks/platform/step_lambdas.py."
    )


def test_the_lock_methods_make_exactly_the_calls_the_oracle_credits_them_with():
    """The wrapper the oracle follows: delete sends DeleteGateway and nothing else, and
    update sends UpdateGateway and reads back through GetGateway."""
    calls = lock_method_ctrl_calls()
    assert calls["delete"] == {"delete_gateway"}
    assert calls["update"] == {"update_gateway", "get_gateway"}
    assert calls["read"] == {"get_gateway"}


def test_gateway_deployer_deletes_a_gateway_only_through_the_lock():
    """No bare DeleteGateway: the anchor above holds only because every one is locked."""
    src = GATEWAY_DEPLOYER.read_text()
    assert not re.search(r"\.delete_gateway\s*\(", src)
    assert "delete_gateway" in {m for _c, m in calls_through_locks(ast.parse(src))}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
