"""``iam:AttachRolePolicy`` must never be grantable for an arbitrary managed policy.

Two roles in this platform can attach a managed policy to any ``AgentCore*`` role:
the per-step roles built in ``platform/step_lambdas.py`` and the deployment
Lambda's role in ``platform/lambdas.py``. Both also hold ``iam:PassRole`` for
AgentCore roles, and the deployment role is the one that creates the sandbox role
that AI-generated tool code then executes as. An unconditioned attach in that
position is the shortest path from "a generated tool did something unexpected" to
"a generated tool ran as an administrator": attach ``AdministratorAccess`` to the
sandbox role, then run any tool.

ARCC guidance applied. ``cnt_pXauQr9E6bKwke`` states that an incorrectly scoped
managed policy is a privilege-escalation path and that managed policies must not
surprise the customer. ``cnt_SFJJhkOueCPRkd`` and ``cnt_L4ZLZgjrCctfxl`` require
condition keys to reach least privilege rather than action-plus-resource alone,
and specifically warn against relying on a broad managed policy.
``cnt_AGx9pUNpmdOVZB`` is why the statement is defined in CDK rather than added
by hand.

``iam:PolicyARN`` was confirmed to exist as a condition key for both
``AttachRolePolicy`` and ``DetachRolePolicy`` against the AWS Service Reference
feed (https://servicereference.us-east-1.amazonaws.com/v1/iam/iam.json), not the
docs. The same lookup is why ``iam:PutRolePolicy`` is left unconditioned: the feed
lists only ``iam:PermissionsBoundary`` and ``iam:RoleTemplateARN`` for it, so
there is no way to constrain an inline policy's *content* through a condition
key. Constraining inline policies means requiring a permissions boundary at
``CreateRole``, which is a separate and much larger change; it is recorded as an
observation rather than silently left implied by this file.

Three invariants.

**Every attach is conditioned.** Any policy statement in the synthesized template
that grants ``iam:AttachRolePolicy`` must carry an ``iam:PolicyARN`` condition.
Asserted over the whole template, not over the two statements this change edited,
so a third grant added later fails here.

**The allow-list is exactly what the application attaches.** The ARNs are read out
of the backend source. A new attach site — or an existing one switched to a
different managed policy — fails here rather than at runtime, where it would
surface as an ``AccessDenied`` that names a policy ARN and no cause. This is the
half that matters most: a condition narrower than the code is a feature outage,
which is how the ``lambda:TagResource`` regression in
``test_tool_sandbox_grant.py`` reached production.

**Detach is deliberately NOT conditioned.** Pinned as an assertion because it
looks like an oversight. The delete paths iterate whatever
``list_attached_role_policies`` returns, which on a role created by an older build
can include a policy no longer in the allow-list; conditioning detach would turn
that into a role that can never be deleted — trading a real escalation path for a
guaranteed leak. Detach also cannot grant anything, since no AWS-managed policy
carries a ``Deny``.
"""

import ast
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.config import ATTACHABLE_MANAGED_POLICIES

BACKEND_SRC = Path(__file__).resolve().parents[2] / "backend" / "src" / "app"

# Any literal that names an AWS-managed policy. Customer-managed ARNs contain the
# account id instead of ``aws`` and are not attachable by these grants at all.
_AWS_MANAGED_ARN = re.compile(r"^arn:aws:iam::aws:policy/")


@pytest.fixture(scope="module")
def template() -> Template:
    from stacks.platform_stack import PlatformStack

    app = cdk.App()
    stack = PlatformStack(
        app,
        "AttachAllowlistTestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _statements(template: Template) -> list[dict]:
    """Every statement in every IAM policy and managed policy in the template."""
    out: list[dict] = []
    for type_name in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"):
        for resource in template.find_resources(type_name).values():
            props = resource.get("Properties", {})
            documents = []
            if "PolicyDocument" in props:
                documents.append(props["PolicyDocument"])
            for inline in props.get("Policies", []) or []:
                if isinstance(inline, dict) and "PolicyDocument" in inline:
                    documents.append(inline["PolicyDocument"])
            for document in documents:
                statements = document.get("Statement", [])
                if isinstance(statements, list):
                    out.extend(s for s in statements if isinstance(s, dict))
    return out


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    if isinstance(action, str):
        return [action]
    return [a for a in action if isinstance(a, str)]


def _reachable_strings(
    expr: ast.AST,
    names: dict[str, set[str]],
) -> set[str]:
    """Return literal strings reachable through the current name bindings."""
    out: set[str] = set()
    for leaf in ast.walk(expr):
        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
            out.add(leaf.value)
        elif isinstance(leaf, ast.Name):
            out.update(names.get(leaf.id, ()))
    return out


def _managed_policy_arns_the_backend_attaches() -> set[str]:
    """Every AWS-managed policy ARN the application passes to attach_role_policy.

    Two real shapes, both resolved. ``gateway_deployer`` passes the ARN literally.
    ``tool_tester`` loops -- ``for policy_arn in [BASIC_EXECUTION_POLICY] +
    ([VPC_ACCESS_POLICY] if need_vpc else [])`` -- so a loop target is resolved by
    reading every string constant reachable in the iterable, which over-approximates
    on purpose: the VPC policy is attached only when a VPC is in use, and a
    condition that allowed it only on that branch would be a condition on the
    wrong thing.

    Anything else -- a dict lookup, a function call, an f-string -- fails the
    assertion below rather than being skipped, because an unresolvable ARN is
    exactly the case where a human has to check the allow-list by hand.
    """
    found: set[str] = set()
    unresolved: list[str] = []

    for path in sorted(BACKEND_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        # Every name bound to a string, or to anything a string is reachable
        # through. Two passes, because the real shape is a local built from module
        # constants: `wanted = [BASIC_EXECUTION_POLICY] + ([VPC_ACCESS_POLICY] ...)`
        # followed by `for policy_arn in wanted`.
        names: dict[str, set[str]] = {}

        assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For) and isinstance(n.target, ast.Name)]
        for _ in range(2):
            for node in assignments:
                strings = _reachable_strings(node.value, names)
                if not strings:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.setdefault(target.id, set()).update(strings)
            for node in loops:
                strings = _reachable_strings(node.iter, names)
                if strings:
                    names.setdefault(node.target.id, set()).update(strings)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "attach_role_policy":
                continue
            arg = next((kw.value for kw in node.keywords if kw.arg == "PolicyArn"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                values = {arg.value}
            elif isinstance(arg, ast.Name) and arg.id in names:
                values = names[arg.id]
            else:
                where = f"{path.relative_to(BACKEND_SRC.parents[2])}:{node.lineno}"
                unresolved.append(where)
                continue
            found.update(v for v in values if _AWS_MANAGED_ARN.match(v))

    assert not unresolved, (
        "attach_role_policy is called with a PolicyArn this test cannot resolve at "
        f"{', '.join(unresolved)}. The iam:PolicyARN condition in "
        "stacks/platform/config.py ATTACHABLE_MANAGED_POLICIES is a closed set, so an "
        "ARN nobody has checked against it will fail in production with AccessDenied "
        "and no cause. Either use a module-level constant, or add the ARN to the "
        "allow-list and teach this test how to see it."
    )
    return found


def test_every_attach_role_policy_grant_is_conditioned(template):
    """No statement may grant AttachRolePolicy without an iam:PolicyARN condition."""
    offenders = []
    for statement in _statements(template):
        if statement.get("Effect") == "Deny":
            # An explicit Deny of AttachRolePolicy attaches nothing; it is the F-05 guard that
            # keeps every Lambda role away from the shared runtime roles (shared_role_guard.py).
            continue
        actions = _actions(statement)
        if not any(a == "iam:AttachRolePolicy" or a == "iam:*" or a == "*" for a in actions):
            continue
        conditions = statement.get("Condition") or {}
        keys = {key for operator in conditions.values() if isinstance(operator, dict) for key in operator}
        if not any(k.lower() == "iam:policyarn" for k in keys):
            offenders.append({"Action": actions, "Resource": statement.get("Resource")})

    assert not offenders, (
        "These statements can attach ANY managed policy to the role they name, "
        "including AdministratorAccess: "
        f"{offenders}. Split iam:AttachRolePolicy into its own statement with "
        'conditions={"ArnEquals": {"iam:PolicyARN": list(ATTACHABLE_MANAGED_POLICIES)}}. '
        "Per ARCC cnt_pXauQr9E6bKwke an incorrectly scoped managed policy is a "
        "privilege-escalation path, and cnt_SFJJhkOueCPRkd requires a condition key "
        "rather than action-plus-resource alone."
    )


def test_the_allowlist_covers_every_policy_the_application_attaches():
    """A narrower condition than the code is a feature outage, not hardening."""
    attached = _managed_policy_arns_the_backend_attaches()

    assert attached, (
        "No attach_role_policy call site was found in the backend at all. Either the "
        "sandbox/gateway role creation stopped attaching managed policies -- in which "
        "case the grant and this test should go -- or this test has stopped looking "
        "where the code lives."
    )

    missing = sorted(attached - set(ATTACHABLE_MANAGED_POLICIES))
    assert not missing, (
        f"The application attaches {missing}, which the iam:PolicyARN condition does "
        "not allow. Deployed, this fails at attach_role_policy with an AccessDenied "
        "naming the policy ARN and no cause, and the feature that needed the policy "
        "stops working. Add it to ATTACHABLE_MANAGED_POLICIES in "
        "stacks/platform/config.py, or stop attaching it."
    )


def test_the_allowlist_grants_nothing_the_application_does_not_attach():
    """The other direction: an entry nobody uses is an unexplained grant."""
    attached = _managed_policy_arns_the_backend_attaches()
    unused = sorted(set(ATTACHABLE_MANAGED_POLICIES) - attached)
    assert not unused, (
        f"ATTACHABLE_MANAGED_POLICIES allows {unused}, which no attach_role_policy "
        "call site uses. Remove it. An allow-list that is wider than the code is how "
        "a condition key stops being evidence of anything."
    )


def test_detach_is_deliberately_not_conditioned(template):
    """Pinned because it reads like an oversight, and it is not.

    If a future change adds an iam:PolicyARN condition to DetachRolePolicy, this
    fails and the reason is right here: the delete paths detach whatever is
    attached, including a policy an older build attached, and a detach that is
    denied leaves a role that can never be deleted.
    """
    detach_statements = [s for s in _statements(template) if "iam:DetachRolePolicy" in _actions(s)]
    assert detach_statements, "No DetachRolePolicy grant at all; cleanup cannot delete a role it created."

    for statement in detach_statements:
        conditions = statement.get("Condition") or {}
        keys = {key for operator in conditions.values() if isinstance(operator, dict) for key in operator}
        assert not any(k.lower() == "iam:policyarn" for k in keys), (
            "DetachRolePolicy now carries an iam:PolicyARN condition. The delete paths "
            "iterate list_attached_role_policies and detach whatever is there, which on "
            "a role created by an older build can include a policy outside the "
            "allow-list. Conditioning detach converts that into a role that can never "
            "be deleted. Detach cannot grant permission either -- no AWS-managed policy "
            "carries a Deny -- so there is nothing to gain. Revert, or delete this test "
            "and say why in the message."
        )
