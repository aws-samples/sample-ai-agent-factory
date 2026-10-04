"""A gateway `lambda` target may name a function the platform did not create, and
granting the gateway invoke on it must stay gated on the OWNER's opt-in tag.

The live failure that started this: a gateway target naming
``acfe2e-llstub-22add474`` — a real function, in the same account, just not one the
platform built — failed the whole deploy with
``not authorized to perform: lambda:AddPermission``, because the step roles hold that
action only on ``function:AgentCore*``. That prefix is correct and must not be widened:
``lambda:AddPermission`` is permission management, so ``function:*`` would let any
tenant's canvas make this platform rewrite the resource policy of ANY function in the
account, and naming a function is not authority over it (F-7).

So the capability exists in exactly one shape — ``function:*`` CONDITIONED on the
function carrying ``AgentCoreGatewayTarget=allow``, which only someone who can already
tag that function can set. These tests exist because that condition is the entire
security property: a future "simplify the policy" change that drops it would leave a
statement that still works, still passes every deploy test, and quietly hands the
platform account-wide permission-management reach.

Both the tag key on the resource and ``lambda:Principal`` on the action were confirmed
supported against the AWS Service Reference feed for ``lambda``
(https://servicereference.us-east-1.amazonaws.com/v1/lambda/lambda.json) rather than
the docs — a condition key that is not actually supported is silently ignored on some
services and fails closed on others, and neither is something to guess at.

ARCC guidance applied: ``cnt_BBrFTwAEgWxA30`` (scope to the exact resources needed),
``cnt_dIF0SRA5SUuWSk`` (enumerate the actions; prefer StringEquals, keep wildcards to
the ones that are forced), ``cnt_AGx9pUNpmdOVZB`` (least privilege over convenience).
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

REGION = "us-east-1"
ACCOUNT = "123456789012"

#: Mirrored in backend gateway_deployer.GATEWAY_TARGET_OPT_IN_TAG / _VALUE.
TAG_CONDITION_KEY = "aws:ResourceTag/AgentCoreGatewayTarget"
TAG_CONDITION_VALUE = "allow"

#: Verbs that write to a function's resource policy. Read-only GetPolicy is not here:
#: it reveals who may invoke a function, which is not nothing, but it cannot change it.
PERMISSION_MANAGEMENT = {"lambda:AddPermission", "lambda:RemovePermission"}


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


def _statements(template_json: dict) -> list[tuple[str, dict]]:
    """(logical id, statement) for every statement in every IAM policy/role."""
    out: list[tuple[str, dict]] = []
    for lid, res in template_json["Resources"].items():
        if res["Type"] == "AWS::IAM::Policy":
            docs = [res["Properties"].get("PolicyDocument", {})]
        elif res["Type"] == "AWS::IAM::ManagedPolicy":
            docs = [res["Properties"].get("PolicyDocument", {})]
        elif res["Type"] == "AWS::IAM::Role":
            docs = [p.get("PolicyDocument", {}) for p in res["Properties"].get("Policies", []) or []]
        else:
            continue
        for doc in docs:
            for st in doc.get("Statement", []) or []:
                out.append((lid, st))
    return out


def _actions(st: dict) -> list[str]:
    act = st.get("Action")
    if isinstance(act, str):
        return [act]
    return [a for a in (act or []) if isinstance(a, str)]


def _resource_strings(st: dict) -> list[str]:
    """Resource entries flattened to strings, resolving Fn::Join of literals.

    The synthesized ARNs are ``Fn::Join`` over literals plus ``AWS::Partition``, so a
    naive string check would see no resource at all and every assertion would pass
    vacuously.
    """
    res = st.get("Resource")
    entries = [res] if not isinstance(res, list) else res
    out: list[str] = []
    for e in entries:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict) and "Fn::Join" in e:
            _sep, parts = e["Fn::Join"]
            out.append(_sep.join(p if isinstance(p, str) else "<ref>" for p in parts))
        else:
            out.append(repr(e))
    return out


def _is_account_wide_function(resource: str) -> bool:
    """A resource string reaching every function in the account/region."""
    return resource.endswith(":function:*") or resource.endswith(":function:")


def test_no_statement_writes_a_function_resource_policy_account_wide_without_the_tag(template_json):
    """The core invariant, stated over the WHOLE synthesized template rather than the
    statement we happen to have written: if any principal can AddPermission or
    RemovePermission on every function in the account, the opt-in tag must be what
    bounds it."""
    offenders = []
    for lid, st in _statements(template_json):
        if st.get("Effect") != "Allow":
            continue
        acts = set(_actions(st)) & PERMISSION_MANAGEMENT
        if not acts:
            continue
        if not any(_is_account_wide_function(r) for r in _resource_strings(st)):
            continue
        cond = st.get("Condition", {}) or {}
        if cond.get("StringEquals", {}).get(TAG_CONDITION_KEY) != TAG_CONDITION_VALUE:
            offenders.append((lid, sorted(acts), _resource_strings(st), cond))
    assert not offenders, (
        "account-wide Lambda permission-management without the owner opt-in tag "
        f"{TAG_CONDITION_KEY}={TAG_CONDITION_VALUE}: {offenders}"
    )


def test_a_wildcard_lambda_star_grant_never_carries_delete_or_code_write(template_json):
    """Tagging a function to allow an invoke grant is not consent to have it deleted or
    rewritten. Whatever reaches ``function:*`` must carry only policy verbs —
    DeleteFunction / UpdateFunctionCode there would turn an opt-in tag into a hand-over
    of the function itself (ARCC cnt_pXauQr9E6bKwke: UpdateFunctionCode on a function is
    privilege escalation to everything that function can do)."""
    forbidden = {
        "lambda:DeleteFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:CreateFunction",
        "lambda:*",
    }
    offenders = []
    for lid, st in _statements(template_json):
        if st.get("Effect") != "Allow":
            continue
        if not any(_is_account_wide_function(r) for r in _resource_strings(st)):
            continue
        bad = sorted(set(_actions(st)) & forbidden)
        if bad:
            offenders.append((lid, bad))
    assert not offenders, f"account-wide function:* statements carrying destructive verbs: {offenders}"


def test_the_gateway_step_can_actually_grant_an_opted_in_function(template_json):
    """The other half: the capability must EXIST, or the bring-your-own-Lambda target
    is still undeployable and the actionable error message is a dead end. Pinned on the
    gateway step's own role, since that is the one that calls add_permission."""
    role_ids = [
        lid
        for lid, res in template_json["Resources"].items()
        if res["Type"] == "AWS::IAM::Role" and lid.startswith("StepGatewayRole")
    ]
    assert len(role_ids) == 1, role_ids
    role_id = role_ids[0]

    found = []
    for lid, st in _statements(template_json):
        res = template_json["Resources"][lid]
        # CDK can move this statement into an overflow managed policy as the
        # gateway role grows. Both forms attach through Roles and are equally live.
        if res["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        roles = res["Properties"].get("Roles", []) or []
        if not any(isinstance(r, dict) and r.get("Ref") == role_id for r in roles):
            continue
        if "lambda:AddPermission" not in _actions(st):
            continue
        if any(_is_account_wide_function(r) for r in _resource_strings(st)):
            found.append(st)
    assert found, "the gateway step cannot grant invoke on ANY function outside function:AgentCore*"
    st = found[0]
    cond = st.get("Condition", {})
    assert cond["StringEquals"][TAG_CONDITION_KEY] == TAG_CONDITION_VALUE
    # ...and the only principal it may ever name is an AgentCore gateway role, so even
    # a tagged function cannot be opened up to an arbitrary account or to "*".
    principal = cond["StringLike"]["lambda:Principal"]
    assert principal.endswith(":role/AgentCoreGateway-*"), principal


def test_teardown_can_take_its_own_grant_back_off_an_opted_in_function(template_json):
    """A grant the platform can write and cannot remove is a permanent trust
    relationship on a customer's function (ARCC cnt_ua0cTwldOsODs8: a trust
    relationship must die with the resource that needed it). Both teardown paths —
    the failure-path auto-cleanup in the status-update step and the manifest delete in
    the deployment Lambda — need RemovePermission under the same tag."""
    wanted = {"StepStatusUpdateRole": False, "DeploymentLambdaRole": False}
    for prefix in list(wanted):
        role_ids = [
            lid
            for lid, res in template_json["Resources"].items()
            if res["Type"] == "AWS::IAM::Role" and lid.startswith(prefix)
        ]
        assert len(role_ids) == 1, (prefix, role_ids)
        role_id = role_ids[0]
        for lid, st in _statements(template_json):
            res = template_json["Resources"][lid]
            if res["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
                continue
            roles = res["Properties"].get("Roles", []) or []
            if not any(isinstance(r, dict) and r.get("Ref") == role_id for r in roles):
                continue
            if "lambda:RemovePermission" not in _actions(st):
                continue
            if not any(_is_account_wide_function(r) for r in _resource_strings(st)):
                continue
            if (st.get("Condition", {}).get("StringEquals", {}) or {}).get(TAG_CONDITION_KEY) == TAG_CONDITION_VALUE:
                wanted[prefix] = True
    assert all(wanted.values()), f"teardown cannot release its grant on an opted-in function: {wanted}"


def test_the_tag_key_matches_the_one_the_backend_tells_users_to_set(template_json):
    """The policy and the error message must name the SAME tag. They live in different
    files (infra/stacks/platform/*.py and backend gateway_deployer), and a mismatch is
    invisible: the deploy still fails with AccessDenied and the remedy in the message
    still does nothing."""
    import pathlib
    import re

    src = (
        pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app" / "services" / "gateway_deployer.py"
    ).read_text()
    key = re.search(r'GATEWAY_TARGET_OPT_IN_TAG\s*=\s*"([^"]+)"', src)
    val = re.search(r'GATEWAY_TARGET_OPT_IN_VALUE\s*=\s*"([^"]+)"', src)
    assert key and val, "the backend constants were renamed — update the policy conditions too"
    assert f"aws:ResourceTag/{key.group(1)}" == TAG_CONDITION_KEY
    assert val.group(1) == TAG_CONDITION_VALUE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
