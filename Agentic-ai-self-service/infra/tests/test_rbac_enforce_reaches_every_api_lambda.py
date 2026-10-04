"""Every Lambda behind an API route must receive ``RBAC_ENFORCE``.

``services.rbac.rbac_enforcing()`` once read an **absent** variable as ``"false"``,
so it silently meant advisory. That made a missing variable invisible: nothing
failed, nothing logged, the API just kept allowing requests it was configured to
deny. (An absent value now enforces, and the stack ships ``"true"``; the variable
is still required, so the one flag reaches every API Lambda.)

The workflow Lambda had the variable; the deployment Lambda did not. Measured live
on throwaway stack ``acfe2e-p0920`` (us-east-1), with a caller in ``t-user`` and no
``g-*`` group, therefore holding zero scopes:

    RBAC_ENFORCE set on the workflow Lambda only (what deploy.sh did):
      GET /api/workflows     (workflow   Lambda) -> 403   enforced
      GET /api/admin/audit   (deployment Lambda) -> 200   NOT enforced
      GET /api/cost/budgets  (deployment Lambda) -> 200   NOT enforced

    RBAC_ENFORCE set on both (after the fix):
      GET /api/workflows                          -> 403
      GET /api/admin/audit                        -> 403
      GET /api/cost/budgets                       -> 403

    A caller in g-admins-super kept 200 on all three in every configuration, so
    enforcing is not simply "deny everything".

51 of the 66 API operations route to the deployment Lambda, including every
scope-guarded ``/api/admin``, ``/api/cost``, ``/api/registry``,
``/api/permissions``, ``/api/prompts``, ``/api/tags``, ``/api/triggers``,
``/api/connectors``, ``/api/hitl``, ``/api/approvals``, ``/api/evaluations``,
``/api/versions``, ``/api/identity``, ``/api/models``, ``/api/mcp-servers`` and
``/api/vpc-profiles`` route. So an operator following docs/RBAC_ROLLOUT.md step 5
flipped 15 operations to fail-closed, left 51 advisory, and had no signal telling
them apart. Note the doc was already correct — it names "the workflow + deployment
Lambdas" in steps 1 and 5; only the stack was wrong.

Asserted against the integration targets in the synthesized template rather than a
hard-coded pair of function names, so adding a third API Lambda without the
variable fails here instead of shipping a hole.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name="test",
        project_name="agentcore-workflow",
        env=cdk.Environment(region="us-east-1", account="123456789012"),
    )
    return Template.from_stack(stack).to_json()


def _api_backed_lambda_logical_ids(tpl: dict) -> set[str]:
    """Logical ids of Lambda functions that are HTTP API integration targets.

    An integration's ``IntegrationUri`` is a ``Fn::GetAtt``/``Fn::Sub`` referencing
    the function, so walk the structure for any Ref/GetAtt that names a Lambda.
    """
    resources = tpl["Resources"]
    lambda_ids = {lid for lid, r in resources.items() if r["Type"] == "AWS::Lambda::Function"}

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "Fn::GetAtt" and isinstance(val, list) and val and val[0] in lambda_ids:
                    found.add(val[0])
                elif key == "Ref" and isinstance(val, str) and val in lambda_ids:
                    found.add(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for res in resources.values():
        if res["Type"] == "AWS::ApiGatewayV2::Integration":
            walk(res.get("Properties", {}).get("IntegrationUri"))
    return found


def test_every_api_backed_lambda_receives_rbac_enforce(template_json: dict) -> None:
    api_lambdas = _api_backed_lambda_logical_ids(template_json)
    assert len(api_lambdas) >= 2, (
        f"expected at least the workflow + deployment Lambdas behind the API, found {api_lambdas} — "
        "if the integration wiring changed, this helper needs updating, not deleting"
    )

    missing = []
    for logical_id in sorted(api_lambdas):
        env = template_json["Resources"][logical_id].get("Properties", {}).get("Environment", {})
        if "RBAC_ENFORCE" not in env.get("Variables", {}):
            missing.append(logical_id)

    assert not missing, (
        f"API-backed Lambda(s) {missing} have no RBAC_ENFORCE variable. An absent value reads as "
        "advisory, so RBAC_ENFORCE=true ./scripts/deploy.sh would leave their routes allowing "
        "requests while the operator believes the control plane is fail-closed."
    )


def test_rbac_enforce_is_enforcing_by_default_on_every_api_lambda(template_json: dict) -> None:
    """Every route declares a scope (backend test_rbac_route_coverage), and the user
    provisioner grants a new user g-users-default, so the shipped default is fail-closed.
    Advisory is the explicit ``-c rbac_enforce=false`` escape hatch for an upgrade."""
    for logical_id in sorted(_api_backed_lambda_logical_ids(template_json)):
        variables = template_json["Resources"][logical_id]["Properties"]["Environment"]["Variables"]
        assert variables["RBAC_ENFORCE"] == "true", (
            f"{logical_id} must default to enforcing 'true'; advisory by default leaves every "
            "scope guard logging instead of denying"
        )


def test_rbac_enforce_is_context_driven_on_every_api_lambda(template_json: dict) -> None:
    """A hardcoded ``"true"`` would pass the two tests above and still be unflippable.

    Asserted on the source rather than the template because ``try_get_context``
    resolves before synth, so the rendered template cannot show where the value
    came from.
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "stacks" / "platform" / "lambdas.py").read_text()
    occurrences = src.count('"RBAC_ENFORCE": stack.node.try_get_context("rbac_enforce") or "true"')
    assert occurrences >= 2, (
        f"expected the context-driven RBAC_ENFORCE assignment on every API Lambda builder, found "
        f"{occurrences}; a literal value cannot be flipped by deploy.sh"
    )
