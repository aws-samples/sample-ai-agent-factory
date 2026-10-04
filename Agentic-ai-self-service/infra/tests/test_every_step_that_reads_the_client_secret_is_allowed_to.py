"""A step that resolves the gateway client secret must be granted the read.

THE DEFECT THIS PINS, measured live. The gateway step role was granted
``cognito-idp:DescribeUserPool`` but NOT ``cognito-idp:DescribeUserPoolClient`` --
different IAM actions, one character apart in prose and unrelated in authorization.
``gateway_deployer.resolve_client_secret`` needs the *client* one, because the app
client's secret is deliberately never carried in ``client_info`` (it would land in the
Step Functions execution history, the DynamoDB item and ``GET /api/deploy/{id}``) and
is instead re-read at the moment of use.

Consequence, from deployment ``ad93d2d4`` on ``acfe2e-p0920``: the deploy went GREEN.
Nine tools synced onto a READY target, ``success: True``, a runtime shipped. And every
deploy-time ``tools/list`` probe logged ``AccessDeniedException``, so
``tool_plane_verified`` came back ``False`` -- the platform could not verify over MCP
what the agent would actually see. The denial was masked for a long time by an
unrelated cause with an identical symptom (a cold Cognito hosted domain also makes the
probe never answer); only once the shared warm pool removed that cause did the missing
grant become the sole remaining explanation.

WHY THIS TEST IS SHAPED THIS WAY. An equivalent test already existed for the *runtime*
role (``test_runtime_role_can_resolve_the_client_secret``) and it did its job. Nothing
covered the *step* roles, and a hand-maintained list of which steps need the grant
would have been written from the same wrong belief that caused the bug. So the required
set is DERIVED from the backend source -- which step handlers transitively reach the
secret read -- and then checked against the synthesized template. Add a new step that
resolves a client secret and this test starts demanding its grant on its own.

ARCC ``cnt_1ZPqVzeASDHlO7`` and ``cnt_dwzZ05hLnqhYXQ``: least privilege, scoped to the
specific resource rather than a wildcard -- so the tests below also assert the grant is
NOT widened to ``cognito-idp:*`` and stays inside this account and region.
"""

import ast
import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import role_logical_id, statements_for_role

REGION = "us-east-1"
PROJECT = "agentcore-workflow"
ENVIRONMENT = "test"

ACTION = "cognito-idp:DescribeUserPoolClient"

#: The boto3 call the action authorizes. Seeding on this rather than on a wrapper name
#: (``resolve_client_secret``) keeps the derivation anchored to what IAM actually checks.
SEED_CALL = "describe_user_pool_client"

_BACKEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "app"

#: The reviewed answer, as of the call-graph run that produced it. Pinned ALONGSIDE the
#: derivation, not instead of it: the derived set below is what the grant assertions use
#: (so a newly-added step that reads a secret is demanded automatically), while this pin
#: makes any change to the derived set fail loudly so a human re-reviews it. One without
#: the other is how this bug survived -- a hand-written list restates the wrong belief,
#: and a pure derivation silently changes meaning when the code moves.
_REVIEWED = {
    # handler -> gateway_deployer.resolve_client_secret, for the tools/list probe.
    "gateway_step",
    # handler -> harness_deployer.ensure_gateway_outbound_provider (harness_step.py:116)
    # -> resolve_client_secret (harness_deployer.py:385), the harness->gateway OAuth2
    # bridge.
    "harness_step",
}

#: Maps a step handler module stem to its role's logical-id prefix in the template.
#: Only the naming convention is encoded here, not the answer.
_STEP_TO_ROLE_PREFIX = {
    "auth_step": "StepAuthRole",
    "codegen_step": "StepCodegenRole",
    "evaluation_step": "StepEvaluationRole",
    "gateway_step": "StepGatewayRole",
    "guardrails_step": "StepGuardrailsRole",
    "harness_step": "StepHarnessRole",
    "iam_step": "StepIamRole",
    "knowledge_base_step": "StepKnowledgeBaseRole",
    "mcp_server_step": "StepMcpServerRole",
    "memory_step": "StepMemoryRole",
    "policy_step": "StepPolicyRole",
    "runtime_configure_step": "StepRuntimeConfigureRole",
    "runtime_launch_step": "StepRuntimeLaunchRole",
    "status_update_step": "StepStatusUpdateRole",
    "validate_step": "StepValidateRole",
}


# --------------------------------------------------------------------------------
# Derive, from the backend source, which steps reach the secret read.
# --------------------------------------------------------------------------------


def _calls_in(node) -> set[str]:
    """Every called name in *node*, whether ``f()`` or ``obj.f()``."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _function_table() -> dict[str, dict[str, set[str]]]:
    """``{module_relpath: {function_name: names it calls}}`` over the whole backend app."""
    table: dict[str, dict[str, set[str]]] = {}
    for path in sorted(_BACKEND_SRC.rglob("*.py")):
        rel = str(path.relative_to(_BACKEND_SRC))
        tree = ast.parse(path.read_text())
        table[rel] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                table[rel].setdefault(node.name, set()).update(_calls_in(node))
    return table


def _steps_needing_the_grant() -> set[str]:
    """Step handler stems that transitively reach the ``describe_user_pool_client`` call.

    A real per-symbol call graph, seeded at the boto3 call and propagated by callee name.
    Two earlier, cruder derivations were both wrong in instructive ways, so the shape
    here is deliberate:

    * Seeding on the wrapper name ``resolve_client_secret`` and then treating "imports
      the module that defines it" as reaching it marked ``status_update_step`` -- which
      imports ``gateway_deployer`` only for ``_SHARED_TOOL_LAMBDAS``,
      ``_release_shared_tool_lambda`` and ``is_platform_owned_user_pool``, and never
      touches a secret. A module-level import is not a call.
    * Grepping for the boto3 call finds five sites, but three of them
      (``deployment.py:353``, ``code_generator.py:566`` and ``:1507``) are inside
      triple-quoted strings of GENERATED agent source. Those are the deployed runtime's
      permission, not a step's -- and they are already covered by
      ``test_runtime_role_can_resolve_the_client_secret``. Parsing rather than grepping
      excludes them for free, because a string literal contains no ``ast.Call``.

    Propagation is by callee name, which over-approximates across same-named functions in
    different modules. That is the safe direction for this test: a false positive demands
    a grant a step may not need and gets caught in review, whereas a false negative is
    the bug itself.
    """
    table = _function_table()

    tainted: set[tuple[str, str]] = {
        (rel, fn) for rel, funcs in table.items() for fn, calls in funcs.items() if SEED_CALL in calls
    }

    changed = True
    while changed:
        changed = False
        tainted_names = {fn for _, fn in tainted}
        for rel, funcs in table.items():
            for fn, calls in funcs.items():
                if (rel, fn) not in tainted and (calls & tainted_names):
                    tainted.add((rel, fn))
                    changed = True

    return {stem for stem in _STEP_TO_ROLE_PREFIX if any(rel == f"step_handlers/{stem}.py" for rel, _ in tainted)}


# --------------------------------------------------------------------------------
# Template side.
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template_json():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        # Account-agnostic, matching app.py. Pinning an account here resolves the CDK
        # assets-bucket ARN to a literal and breaks unrelated suppressions.
        env=cdk.Environment(region=REGION),
    )
    return Template.from_stack(stack).to_json()


def _statements_for_role_prefix(template_json, prefix: str) -> list[dict]:
    """Every Allow/Deny statement attached to the role, however it is attached.

    The step roles carry no ``RoleName``, so they are located by logical id, then the
    statements are resolved BY ATTACHMENT through ``tests/iam_attachment.py``. This
    module used to resolve them itself and read only two of the three attachment shapes
    CDK uses, which made it report a grant that IS present as absent.

    Not hypothetically: ``cognito-idp:DescribeUserPoolClient`` for the gateway role
    synthesizes into ``StepGatewayRoleOverflowPolicy1EBBF24B3``, an
    ``AWS::IAM::ManagedPolicy`` that CDK creates by itself once the role's inline
    document nears the 10240-character limit -- and that logical id exists in the
    deployed stack too, so this is the real shape, not a synth artifact.

    The trap worth naming: WHICH role overflows depends on the total size of unrelated
    statements, and this fixture synthesizes account-agnostic (see above), so its
    pseudo-parameter ARNs are longer than a pinned account's and the gateway role
    crosses the line here before it crosses it anywhere else. Adding statements
    elsewhere in the stack can therefore move a grant out of a hand-rolled scan's view,
    which reads as a regression in a grant nobody touched.
    """
    role_lid = role_logical_id(template_json, prefix)
    return [st for _src, st in statements_for_role(template_json, role_lid)]


def _allows(statements: list[dict], action: str) -> list[dict]:
    out = []
    for st in statements:
        if st.get("Effect") != "Allow":
            continue
        actions = st.get("Action")
        actions = [actions] if isinstance(actions, str) else (actions or [])
        if action in actions:
            out.append(st)
    return out


# --------------------------------------------------------------------------------
# Tests.
# --------------------------------------------------------------------------------


def test_the_call_graph_finds_the_real_call_site():
    """Vacuity guard on the seed. If the seed is empty nothing is tainted, the derived set
    is empty, and every grant assertion below passes for free -- which is indistinguishable
    from the pre-fix codebase."""
    table = _function_table()
    seed = {(rel, fn) for rel, funcs in table.items() for fn, calls in funcs.items() if SEED_CALL in calls}
    assert seed, (
        f"no function in backend/src/app calls {SEED_CALL}(); the call-graph seed is empty, "
        "so this whole module would pass vacuously"
    )
    assert ("services/gateway_deployer.py", "resolve_client_secret") in seed, (
        "expected gateway_deployer.resolve_client_secret to be the primary call site; "
        f"found {sorted(seed)}. If it moved, update this guard deliberately."
    )


def test_the_derived_set_still_matches_what_was_reviewed():
    """The derivation drives the grant assertions, so a change to it changes what this
    module demands. Fail here so that change is a decision rather than a side effect."""
    derived = _steps_needing_the_grant()
    assert derived == _REVIEWED, (
        f"the set of steps reaching {SEED_CALL}() changed.\n"
        f"  derived:  {sorted(derived)}\n"
        f"  reviewed: {sorted(_REVIEWED)}\n"
        f"  newly reaching it:   {sorted(derived - _REVIEWED)}\n"
        f"  no longer reaching:  {sorted(_REVIEWED - derived)}\n"
        f"Grant or revoke {ACTION} in infra/stacks/platform/step_lambdas.py to match, then "
        "update _REVIEWED with a comment naming the call path."
    )


def test_every_step_that_reads_the_secret_can_read_it(template_json):
    """The regression. Fails on the pre-fix template for gateway_step."""
    missing = []
    for stem in sorted(_steps_needing_the_grant()):
        prefix = _STEP_TO_ROLE_PREFIX[stem]
        if not _allows(_statements_for_role_prefix(template_json, prefix), ACTION):
            missing.append(f"{stem} ({prefix})")
    assert not missing, (
        f"these steps resolve a gateway client secret but their role cannot call {ACTION}: "
        f"{missing}.\n"
        "The deploy will still go GREEN -- the read fails at the moment of use, so the "
        "symptom is an AccessDeniedException in the step's logs and a gateway whose tool "
        "plane was never verified over MCP. Note DescribeUserPool is a DIFFERENT action "
        "and does not authorize this."
    )


@pytest.mark.parametrize("stem", sorted(_steps_needing_the_grant()))
def test_the_grant_is_least_privilege(template_json, stem):
    """Granted narrowly: the specific action, inside this account and region, never via
    a service-wide wildcard (ARCC cnt_1ZPqVzeASDHlO7, cnt_dwzZ05hLnqhYXQ)."""
    statements = _statements_for_role_prefix(template_json, _STEP_TO_ROLE_PREFIX[stem])

    assert not _allows(statements, "cognito-idp:*"), (
        f"{stem}'s role is granted cognito-idp:* -- that satisfies the test above by "
        "giving the step full control of every user pool, including the shared "
        "gateway-auth pool that authenticates every deployed agent"
    )

    for grant in _allows(statements, ACTION):
        resources = grant.get("Resource")
        resources = [resources] if not isinstance(resources, list) else resources
        for resource in resources:
            assert resource != "*", f"{stem}'s {ACTION} grant is on '*'"
            if isinstance(resource, str):
                assert ":userpool/" in resource, (
                    f"{stem}'s {ACTION} grant should name the userpool resource type; got {resource!r}"
                )


def test_a_step_that_never_touches_the_secret_is_not_granted_the_read(template_json):
    """The other half of least privilege, and a second vacuity guard: if every role had
    the grant, the test above would pass no matter what. validate_step only inspects a
    canvas, so it has no business reading an app client's secret."""
    needed = _steps_needing_the_grant()
    over_granted = []
    for stem, prefix in sorted(_STEP_TO_ROLE_PREFIX.items()):
        if stem in needed:
            continue
        try:
            statements = _statements_for_role_prefix(template_json, prefix)
        except AssertionError:
            # No such role in this template (a step whose role is conditional). Absence
            # of a role is not an over-grant, so it is not this test's business.
            continue
        if _allows(statements, ACTION):
            over_granted.append(stem)
    assert not over_granted, f"these steps are granted {ACTION} but never resolve a client secret: {over_granted}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
