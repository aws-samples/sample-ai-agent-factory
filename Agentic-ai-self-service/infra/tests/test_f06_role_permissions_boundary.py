"""F-06: a platform permissions boundary exists, reaches every Lambda, caps what created roles
can do, and -- behind the ``enforce_role_permissions_boundary`` context switch -- is REQUIRED on
every CreateRole / PutRolePolicy / UpdateAssumeRolePolicy / AttachRolePolicy a Lambda role holds.

Why the switch defaults off is in ``role_boundary.py``; what the backend must do before it is
flipped is in the P1 ledger. Both modes are asserted here so the default is a decision the
tests can see, not an omission: off means NO ``iam:PermissionsBoundary`` condition anywhere (the
grants are unchanged), on means EVERY such grant carries it and nothing else does.

The allow-list is checked against the backend source rather than against a hand-typed set: the
test scans the modules that write inline policies onto created roles for ``"svc:Action"``
literals and requires each to be inside the boundary's Allow list. A created role the boundary
would starve is an AccessDenied on a live deploy; this makes it a red test instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from stacks.platform.config import ATTACHABLE_MANAGED_POLICIES
from stacks.platform.role_boundary import (
    BOUNDARY_ARN_ENV,
    BOUNDARY_DENIED_ACTIONS,
    ENFORCE_CONTEXT_KEY,
    PASS_ROLE_SERVICES,
)

from tests.iam_attachment import statements_by_role
from tests.p1_synth import actions, all_statements, lambda_roles, resources_text, synth

RETROFIT = "iam:PutRolePermissionsBoundary"
BOUNDARY_CONDITIONED_ACTIONS = {
    "iam:CreateRole",
    "iam:PutRolePolicy",
    "iam:UpdateAssumeRolePolicy",
    "iam:AttachRolePolicy",
}
_BACKEND = Path(__file__).resolve().parents[2] / "backend" / "src" / "app"
#: Modules that write policy documents onto roles the backend creates.
POLICY_WRITERS = (
    "services/runtime_deployer.py",
    "services/per_agent_identity.py",
    "services/harness_deployer.py",
    "services/gateway_deployer.py",
    "services/iam_manager.py",
    "services/tool_tester.py",
    "step_handlers/iam_step.py",
    "step_handlers/memory_step.py",
    "step_handlers/evaluation_step.py",
    "step_handlers/knowledge_base_step.py",
)
_ACTION_RE = re.compile(r'"([a-z0-9-]+):([A-Za-z*]+)"')
#: Namespaces whose literals in those files are not actions granted to a created role: IAM
#: verbs the platform's OWN roles call, trust-policy sts:AssumeRole, and condition-key prefixes.
_NOT_CREATED_ROLE_GRANTS = {"iam", "sts", "aws", "arn"}


@pytest.fixture(scope="module")
def tpl_default() -> dict:
    return synth("F06BoundaryDefaultStack")


@pytest.fixture(scope="module")
def tpl_enforced() -> dict:
    return synth("F06BoundaryEnforcedStack", context={ENFORCE_CONTEXT_KEY: "true"})


def _boundary(tpl: dict) -> tuple[str, dict]:
    found = [
        (lid, res)
        for lid, res in tpl["Resources"].items()
        if res["Type"] == "AWS::IAM::ManagedPolicy" and lid.startswith("AgentCoreRoleBoundary")
    ]
    assert len(found) == 1, f"expected exactly one AgentCoreRoleBoundary managed policy, found {[f[0] for f in found]}"
    return found[0]


def _allowed(tpl: dict) -> set[str]:
    _lid, res = _boundary(tpl)
    out: set[str] = set()
    for st in res["Properties"]["PolicyDocument"]["Statement"]:
        if st.get("Effect") == "Allow":
            out |= set(actions(st))
    return out


def _covers(allowed: set[str], action: str) -> bool:
    if action in allowed:
        return True
    svc, _verb = action.split(":", 1)
    return f"{svc}:*" in allowed


def test_the_boundary_exists_with_an_account_global_name(tpl_default):
    _lid, res = _boundary(tpl_default)
    assert res["Properties"]["ManagedPolicyName"] == "acf-test-agentcore-role-boundary"
    assert "*" not in _allowed(tpl_default), "the boundary must never be Allow *"


def test_the_boundary_denies_identity_management_role_assumption_and_account_shaping(tpl_default):
    _lid, res = _boundary(tpl_default)
    denied: set[str] = set()
    for st in res["Properties"]["PolicyDocument"]["Statement"]:
        if st.get("Effect") == "Deny":
            denied |= set(actions(st))
    assert set(BOUNDARY_DENIED_ACTIONS) <= denied
    for must in (
        "iam:Create*",
        "iam:Put*",
        "iam:Attach*",
        "iam:Update*",
        "sts:AssumeRole",
        "organizations:*",
        "account:*",
    ):
        assert must in denied, must
    allowed = _allowed(tpl_default)
    assert {a for a in allowed if a.startswith("iam:")} == {"iam:PassRole"}, allowed


def test_the_boundary_pass_role_is_conditioned_to_known_services(tpl_default):
    _lid, res = _boundary(tpl_default)
    passes = [st for st in res["Properties"]["PolicyDocument"]["Statement"] if "iam:PassRole" in actions(st)]
    assert len(passes) == 1
    assert set(passes[0]["Condition"]["StringEquals"]["iam:PassedToService"]) == set(PASS_ROLE_SERVICES)


def test_the_boundary_allows_every_action_the_backend_writes_onto_a_created_role(tpl_default):
    """Derived from source: a starved created role is a live AccessDenied, so it fails here."""
    allowed = _allowed(tpl_default)
    missing: dict[str, set[str]] = {}
    scanned = 0
    for rel in POLICY_WRITERS:
        text = (_BACKEND / rel).read_text()
        for svc, verb in _ACTION_RE.findall(text):
            if svc in _NOT_CREATED_ROLE_GRANTS:
                continue
            scanned += 1
            action = f"{svc}:{verb}"
            if not _covers(allowed, action):
                missing.setdefault(rel, set()).add(action)
    assert scanned > 100, f"only {scanned} action literals scanned; the source walk is broken"
    assert not missing, f"created roles are granted actions the boundary would deny: {missing}"
    # The two AWS-managed policies the platform may attach to a created role.
    for arn in ATTACHABLE_MANAGED_POLICIES:
        assert arn.endswith(("AWSLambdaBasicExecutionRole", "AWSLambdaVPCAccessExecutionRole")), arn
    for action in (
        "logs:CreateLogGroup",
        "logs:PutLogEvents",
        "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface",
    ):
        assert _covers(allowed, action), action


def test_every_platform_lambda_receives_the_boundary_arn(tpl_default):
    lid, _res = _boundary(tpl_default)
    fns = {k: v for k, v in tpl_default["Resources"].items() if v["Type"] == "AWS::Lambda::Function"}
    carriers = 0
    for flid, fn in fns.items():
        env = fn["Properties"].get("Environment", {}).get("Variables", {})
        if (
            flid.startswith(("DeploymentLambda", "Step"))
            and "Handler" in fn["Properties"]
            and not flid.startswith("StepFunctions")
        ):
            assert env.get(BOUNDARY_ARN_ENV) == {"Ref": lid}, f"{flid}: {BOUNDARY_ARN_ENV} missing or not the boundary"
            carriers += 1
    assert carriers >= 15, f"only {carriers} Lambdas carry the boundary ARN (deployment + 15 steps expected)"


def test_by_default_no_grant_is_conditioned_on_the_boundary(tpl_default):
    """The documented default: enforcement is a switch, not a side effect. The one grant that
    ALWAYS carries the key is the retrofit verb itself (PutRolePermissionsBoundary sets the
    boundary, so the key is always in its request context); it is pinned separately below."""
    for pid, st in all_statements(tpl_default):
        if set(actions(st)) == {RETROFIT}:
            continue
        cond = st.get("Condition") or {}
        assert "iam:PermissionsBoundary" not in (cond.get("StringEquals") or {}), pid


def test_when_enforced_every_role_minting_grant_requires_the_boundary(tpl_enforced):
    """The assertion that fails on the pre-fix tree (no boundary to name, no switch to flip)."""
    lid, _res = _boundary(tpl_enforced)
    expected = {"Ref": lid}
    checked = 0
    for role_lid in lambda_roles(tpl_enforced):
        for pid, st in statements_by_role(tpl_enforced).get(role_lid, []):
            if st.get("Effect") != "Allow":
                continue
            hit = BOUNDARY_CONDITIONED_ACTIONS & set(actions(st))
            if not hit:
                continue
            checked += 1
            cond = (st.get("Condition") or {}).get("StringEquals") or {}
            assert cond.get("iam:PermissionsBoundary") == expected, (
                f"{role_lid}/{pid}: {sorted(hit)} granted without iam:PermissionsBoundary while enforcement is on"
            )
            # The condition key is absent from every OTHER verb's request context; a mixed
            # statement would deny its reads and deletes.
            assert set(actions(st)) <= BOUNDARY_CONDITIONED_ACTIONS, (role_lid, pid, actions(st))
    assert checked >= 8, f"only {checked} role-minting grants inspected; the walk is broken"


def _role_of_lambda(fn: dict) -> str | None:
    r = fn["Properties"].get("Role")
    if isinstance(r, dict) and "Fn::GetAtt" in r:
        return r["Fn::GetAtt"][0]
    return None


def _minting_roles(tpl: dict) -> set[str]:
    """Lambda roles that hold iam:CreateRole or iam:PutRolePolicy (the roles that mint or widen)."""
    out = set()
    for role_lid in lambda_roles(tpl):
        for _pid, st in statements_by_role(tpl).get(role_lid, []):
            if st.get("Effect") == "Allow" and {"iam:CreateRole", "iam:PutRolePolicy"} & set(actions(st)):
                out.add(role_lid)
    return out


def test_every_lambda_that_mints_roles_carries_the_boundary_arn(tpl_default):
    """Derived, not enumerated: whichever Lambda's role holds CreateRole/PutRolePolicy is a
    Lambda whose code must pass PermissionsBoundary=, so its environment must carry the ARN.
    Today that is the deployment/API Lambda (which is also the tool-tester path: tool_tester
    runs inside it via self-invoke) and the seven minting steps; a new minting Lambda that
    forgets the variable fails here, not in a live AccessDenied under enforcement."""
    lid, _res = _boundary(tpl_default)
    minting = _minting_roles(tpl_default)
    assert len(minting) >= 8, f"only {len(minting)} minting roles found; the walk is broken: {sorted(minting)}"
    fns = {k: v for k, v in tpl_default["Resources"].items() if v["Type"] == "AWS::Lambda::Function"}
    seen = set()
    for flid, fn in fns.items():
        role_lid = _role_of_lambda(fn)
        if role_lid not in minting:
            continue
        seen.add(role_lid)
        env = fn["Properties"].get("Environment", {}).get("Variables", {})
        assert env.get(BOUNDARY_ARN_ENV) == {"Ref": lid}, (
            f"{flid} (role {role_lid}) mints roles without {BOUNDARY_ARN_ENV}"
        )
    assert seen == minting, f"minting roles with no Lambda function attached: {sorted(minting - seen)}"
    names = {tpl_default["Resources"][r]["Properties"].get("FunctionName", r) for r in seen}
    assert any(str(n).endswith("-deployment") or "Deployment" in str(n) for n in names), names


def test_the_retrofit_verb_is_granted_to_exactly_the_minting_roles_and_only_for_this_boundary(
    tpl_default, tpl_enforced
):
    """A(ii): iam:PutRolePermissionsBoundary on role/AgentCore*, always conditioned on
    iam:PermissionsBoundary=<the platform boundary>, alone in its statement, for the same
    roles that hold CreateRole -- in BOTH enforcement modes (the retrofit must run before the
    flip). Unconditioned, the verb would swap any AgentCore* role's cap for an empty one."""
    for tpl in (tpl_default, tpl_enforced):
        lid, _res = _boundary(tpl)
        minting = _minting_roles(tpl)
        holders = set()
        for role_lid in lambda_roles(tpl):
            for pid, st in statements_by_role(tpl).get(role_lid, []):
                if st.get("Effect") != "Allow" or RETROFIT not in actions(st):
                    continue
                holders.add(role_lid)
                assert actions(st) == [RETROFIT], (role_lid, pid, actions(st))
                cond = (st.get("Condition") or {}).get("StringEquals") or {}
                assert cond.get("iam:PermissionsBoundary") == {"Ref": lid}, (
                    f"{role_lid}/{pid}: PutRolePermissionsBoundary not pinned to the platform boundary: {st.get('Condition')}"
                )
                for r in resources_text(st):
                    assert "role/AgentCore" in r and "role/AgentCore*" in r.replace('"', ""), (role_lid, pid, r)
        assert holders == minting, f"retrofit holders {sorted(holders)} != minting roles {sorted(minting)}"


def test_no_role_may_ever_remove_a_boundary(tpl_default, tpl_enforced):
    """DeleteRolePermissionsBoundary has no caller and must never be granted; and the retrofit
    verb never appears outside the pinned statements checked above (template-wide, all roles)."""
    for tpl in (tpl_default, tpl_enforced):
        lid, _res = _boundary(tpl)
        for pid, st in all_statements(tpl):
            if st.get("Effect") != "Allow":
                continue
            assert "iam:DeleteRolePermissionsBoundary" not in actions(st), pid
            if RETROFIT in actions(st):
                cond = (st.get("Condition") or {}).get("StringEquals") or {}
                assert cond.get("iam:PermissionsBoundary") == {"Ref": lid}, pid


def test_the_boundary_is_the_only_managed_policy_nothing_attaches_to(tpl_default):
    """Four template-wide over-reach oracles exempt AgentCoreRoleBoundary because it is a cap
    attached to no principal. That exemption is only sound while it is the ONE unattached
    managed policy: a future policy nobody attaches could otherwise hide a real wildcard grant
    behind the same name-prefix test. Attachment has exactly two shapes in this template -- a
    ``Roles`` list on the policy (CDK's overflow policies) or a role's ``ManagedPolicyArns``
    naming it; the Lambda environment's ``Ref`` to the boundary is a reference, not an attachment.
    """
    roles = [res for res in tpl_default["Resources"].values() if res["Type"] == "AWS::IAM::Role"]
    attached_via_roles = {
        m["Ref"]
        for res in roles
        for m in res["Properties"].get("ManagedPolicyArns", [])
        if isinstance(m, dict) and "Ref" in m
    }
    unattached = [
        lid
        for lid, res in tpl_default["Resources"].items()
        if res["Type"] == "AWS::IAM::ManagedPolicy"
        and not res["Properties"].get("Roles")
        and lid not in attached_via_roles
    ]
    assert unattached == [_boundary(tpl_default)[0]], unattached


def test_the_boundary_is_not_attached_to_any_platform_role(tpl_default):
    """It caps roles the backend creates; the platform's own CDK roles are governed by their
    exact policies, and attaching the cap to them would silently widen nothing and hide drift."""
    lid, _res = _boundary(tpl_default)
    for rlid, res in tpl_default["Resources"].items():
        if res["Type"] != "AWS::IAM::Role":
            continue
        props = res["Properties"]
        assert props.get("PermissionsBoundary") != {"Ref": lid}, rlid
        assert not any(isinstance(m, dict) and m.get("Ref") == lid for m in props.get("ManagedPolicyArns", [])), rlid
    for _p, st in all_statements(tpl_default):
        for r in resources_text(st):
            assert lid not in r or st.get("Effect") == "Deny", (
                "a platform role is granted something ON the boundary policy"
            )
