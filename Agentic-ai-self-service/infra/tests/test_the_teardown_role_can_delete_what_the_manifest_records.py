"""The failure-path teardown role must be allowed to make every call its own code makes.

``status_update_step._cleanup_resource`` walks the deployment manifest and deletes
each recorded resource. Every one of those deletes is inside a best-effort
``except``, so an ``AccessDeniedException`` is logged and then the resource is
*counted as handled*. The result is a teardown that reports success while leaving
real, billable, credential-bearing resources in the account.

Measured live, not theorised. Deployment ``959b2c60`` (2026-09-21) minted a
connector-credential secret, failed in a later state, and logged::

    AccessDeniedException ... is not authorized to perform: secretsmanager:DeleteSecret

The raw customer credential stayed in Secrets Manager, tagged with the failed
deployment id. Six such secrets were sitting in the test account. Auditing the rest
of the dispatcher the same way found the role held 6 of the 16 delete verbs its own
code issues -- including neither ``ListGatewayTargets`` nor ``DeleteGatewayTarget``,
without which ``DeleteGateway`` is *rejected* for any gateway that got as far as
having a target, so every such failed deploy leaked its gateway permanently.

The invariant is derived from the source, never from a list, because a hand-kept
list of required actions is the exact artefact that drifted:

  1. AST-extract every ``(service, method)`` pair ``_cleanup_resource`` invokes --
     both ``c = step_clients.client(event, "svc"); c.delete_x()`` and the inline
     ``step_clients.client(event, "svc").delete_x()`` shape.
  2. Map each to ``<iam-prefix>:<Pascal>`` and assert the synthesized
     ``StepStatusUpdateRole`` grants it (honouring wildcard grants like
     ``s3:DeleteObject*``).

And one negative: the teardown role must NOT hold ``secretsmanager:GetSecretValue``.
Deleting a secret never requires reading it, and a role that can read every
connector credential in the account would be a worse outcome than the leak this
fixes.

ARCC guidance: ``cnt_LuG2TKuO0errRp`` (least-privilege access to secrets -- the
reason the fix is Delete/Describe and not ``secretsmanager:*``),
``cnt_ua0cTwldOsODs8`` (dangling resources: a resource must die with what created
it), ``cnt_iBtBQFlDruF4B1`` (CWE-923, release of a resource before intended /
deployment-manifest verification).
"""

import ast
import json
import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.gateway_lock_calls import calls_through_locks
from tests.iam_attachment import statements_for_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
ENVIRONMENT = "test"

ROLE_PREFIX = "StepStatusUpdateRole"

REPO = pathlib.Path(__file__).resolve().parents[2]
CLEANUP_SRC = REPO / "backend" / "src" / "app" / "step_handlers" / "status_update_step.py"
CLEANUP_FUNC = "_cleanup_resource"
HARNESS_SRC = REPO / "backend" / "src" / "app" / "services" / "harness_deployer.py"
GATEWAY_SRC = REPO / "backend" / "src" / "app" / "services" / "gateway_deployer.py"

# These helpers deliberately own the destructive AWS call so the same
# ownership/confirmation logic is shared by user-initiated and failure-path
# teardown. The cleanup oracle follows the client into them instead of forcing
# the dispatcher to duplicate that logic merely to keep an AST test green.
DELEGATED_CLIENT_HELPERS = {
    "destroy_harness": (
        HARNESS_SRC,
        "destroy_harness",
        "agentcore_ctrl",
        "bedrock-agentcore-control",
    ),
    "delete_deployment_bound_secret": (
        GATEWAY_SRC,
        "delete_deployment_bound_secret",
        "secrets_client",
        "secretsmanager",
    ),
}

# boto3 service name -> IAM action prefix. They differ often enough that guessing
# is wrong: opensearchserverless authorizes as `aoss`, and every AgentCore
# control-plane client authorizes as `bedrock-agentcore`.
IAM_PREFIX = {
    "bedrock": "bedrock",
    "bedrock-agent": "bedrock",
    "bedrock-agentcore": "bedrock-agentcore",
    "bedrock-agentcore-control": "bedrock-agentcore",
    "cognito-idp": "cognito-idp",
    "iam": "iam",
    "lambda": "lambda",
    # CloudWatch Logs authorizes as `logs`; the online-evaluation arm deletes the per-config
    # eval-results log group, and an unmapped service here silently skipped that call.
    "logs": "logs",
    "opensearchserverless": "aoss",
    "s3": "s3",
    "s3vectors": "s3vectors",
    "secretsmanager": "secretsmanager",
}

# (service, method) -> why the derived action is not required. Narrow by design:
# anything not listed here must be granted, so adding an AWS call to the teardown
# dispatcher forces an explicit decision about the grant.
CALL_WAIVERS: dict[tuple[str, str], str] = {}

# Calls that MUST still be derived, or this test is measuring nothing. Each is a
# regression anchor for a specific leak found live.
REQUIRED_ANCHORS = {
    ("secretsmanager", "delete_secret"): "the F-30 live leak: a raw customer credential left behind",
    ("bedrock-agentcore-control", "delete_gateway_target"): (
        "DeleteGateway is rejected while targets remain, so without this the gateway leaks too"
    ),
    ("opensearchserverless", "delete_collection"): (
        "a standing OpenSearch Serverless collection is the most expensive orphan this platform makes"
    ),
}


@pytest.fixture(scope="module")
def granted_actions() -> set[str]:
    """Every action the synthesized teardown role holds.

    Read off the SYNTHESIZED template, so a refactor that stops attaching a
    statement cannot make this pass.
    """
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    resources = Template.from_stack(stack).to_json()["Resources"]

    role_ids = [
        lid for lid, res in resources.items() if res["Type"] == "AWS::IAM::Role" and lid.startswith(ROLE_PREFIX)
    ]
    assert len(role_ids) == 1, f"expected exactly one {ROLE_PREFIX}* role, found {role_ids}"
    role_id = role_ids[0]

    actions: set[str] = set()

    def _collect(statements):
        for st in statements or []:
            act = st.get("Action")
            for a in [act] if isinstance(act, str) else (act or []):
                if isinstance(a, str):
                    actions.add(a)

    # Resolved by attachment through tests/iam_attachment.py rather than by scanning
    # AWS::IAM::Policy here. This role is the widest of the fifteen (it reads ownership
    # on every AgentCore type the manifest can record), so it is the likeliest to cross
    # the inline-policy size limit -- at which point CDK moves statements into a
    # <Role>OverflowPolicy<N> MANAGED policy by itself. A scan that reads only
    # AWS::IAM::Policy then answers "which actions does the teardown role hold?" from a
    # subset, and every action that moved reads as ungranted.
    _collect([st for _src, st in statements_for_role({"Resources": resources}, role_id)])

    assert actions, "the teardown role has no actions at all — the lookup is broken, not the policy"
    return actions


def _service_of(node: ast.AST) -> str | None:
    """The service string of a ``step_clients.client(event, "svc")`` call, if any."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "client"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    ):
        return node.args[1].value
    return None


def _preserves_alias(node: ast.AST, aliases: set[str]) -> bool:
    """Whether assigning *node* preserves one of the client objects.

    This intentionally accepts only identity-preserving expressions such as
    ``client`` and ``client or make_client()``. A method result that merely
    references the client must not become another client alias.
    """
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.BoolOp):
        return any(_preserves_alias(value, aliases) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _preserves_alias(node.body, aliases) or _preserves_alias(
            node.orelse,
            aliases,
        )
    return False


def _calls_on_client_parameter(
    source: pathlib.Path,
    function_name: str,
    parameter_name: str,
    service: str,
) -> set[tuple[str, str]]:
    """Derive calls made through one client parameter in a delegated helper."""
    tree = ast.parse(source.read_text())
    fn = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function_name),
        None,
    )
    assert fn is not None, (
        f"{function_name} not found in {source.name} — the teardown oracle cannot follow its delegated AWS calls"
    )

    aliases = {parameter_name}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or not _preserves_alias(
                node.value,
                aliases,
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True

    out: set[tuple[str, str]] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in aliases:
            out.add((service, node.func.attr))
    return out


def _calls_in_cleanup() -> set[tuple[str, str]]:
    """Every ``(service, method)`` pair cleanup invokes, including delegations."""
    tree = ast.parse(CLEANUP_SRC.read_text())
    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == CLEANUP_FUNC),
        None,
    )
    assert fn is not None, f"{CLEANUP_FUNC} not found in {CLEANUP_SRC.name} — this test is measuring nothing"

    # Local name -> service, for the `c = step_clients.client(event, "svc")` shape.
    bound: dict[str, str] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            svc = _service_of(node.value)
            if svc:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id] = svc

    out: set[tuple[str, str]] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        receiver = node.func.value
        inline = _service_of(receiver)
        if inline:
            out.add((inline, node.func.attr))
        elif isinstance(receiver, ast.Name) and receiver.id in bound:
            out.add((bound[receiver.id], node.func.attr))

    # F-66e: `gw_lock.delete()` under `with gateway_mutation_lock(ctrl, ...)` is the
    # DeleteGateway on ctrl's service; the lock is the only place that call is written.
    for client, method in calls_through_locks(fn):
        assert client in bound, f"the gateway lock in {CLEANUP_FUNC} wraps {client!r}, not a step client"
        out.add((bound[client], method))

    called_helpers = {
        node.func.id for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for helper_name, (
        source,
        function_name,
        parameter_name,
        service,
    ) in DELEGATED_CLIENT_HELPERS.items():
        if helper_name in called_helpers:
            out.update(
                _calls_on_client_parameter(
                    source,
                    function_name,
                    parameter_name,
                    service,
                )
            )
    return out


def _snake_to_pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_"))


def _covered(action: str, granted: set[str]) -> bool:
    """Honour wildcard grants — ``s3:DeleteObject*`` covers ``s3:DeleteObject``."""
    return any(g == action or (g.endswith("*") and action.startswith(g[:-1])) for g in granted)


def test_the_oracle_still_finds_the_calls_it_is_meant_to_find():
    """An extractor that silently stops matching turns this whole file green.

    The ``client(event, "svc")`` shape is the one thing the AST walk depends on; if
    the dispatcher is refactored to another accessor, every assertion below passes
    vacuously. Anchor on the specific calls whose absence was a real live leak.
    """
    calls = _calls_in_cleanup()
    assert len(calls) >= 20, f"only {len(calls)} AWS calls derived from {CLEANUP_FUNC} — the extractor stopped matching"
    for anchor, why in REQUIRED_ANCHORS.items():
        assert anchor in calls, f"{anchor[0]}.{anchor[1]} is no longer derived, and it is the anchor for: {why}"


def test_every_service_used_by_the_teardown_has_an_iam_prefix_mapped():
    """A service missing from IAM_PREFIX would be skipped, not flagged."""
    unmapped = sorted({svc for svc, _ in _calls_in_cleanup() if svc not in IAM_PREFIX})
    assert not unmapped, (
        f"{CLEANUP_FUNC} calls services with no IAM action prefix mapped in this test: {unmapped}. "
        "Add them to IAM_PREFIX — the boto3 service name is frequently NOT the IAM prefix "
        "(opensearchserverless authorizes as 'aoss'), so an unmapped service means its calls "
        "go unchecked."
    )


def test_the_teardown_role_is_granted_every_call_the_dispatcher_makes(granted_actions):
    """The generalization of the live F-30 leak: the code called DeleteSecret, the
    role did not allow it, and the call site swallowed the 403."""
    missing = []
    for svc, method in sorted(_calls_in_cleanup()):
        if (svc, method) in CALL_WAIVERS or svc not in IAM_PREFIX:
            continue
        action = f"{IAM_PREFIX[svc]}:{_snake_to_pascal(method)}"
        if not _covered(action, granted_actions):
            missing.append(f"{svc}.{method} -> {action}")

    assert not missing, (
        f"{CLEANUP_FUNC} makes these AWS calls but the teardown role is not granted them: "
        f"{missing}. Every call site is inside a best-effort except, so the AccessDenied is "
        "logged and the resource is then COUNTED AS CLEANED while it is still running in the "
        'account. Add the action to the `if step_name == "status_update":` block in '
        "infra/stacks/platform/step_lambdas.py, or add a reasoned CALL_WAIVERS entry here."
    )


def test_the_waivers_all_still_apply():
    """A waiver for a call the dispatcher no longer makes is dead documentation that
    would hide the next regression."""
    calls = _calls_in_cleanup()
    stale = [f"{svc}.{method}" for (svc, method) in CALL_WAIVERS if (svc, method) not in calls]
    assert not stale, f"stale CALL_WAIVERS entries — {CLEANUP_FUNC} no longer calls: {stale}"
    for key, reason in CALL_WAIVERS.items():
        assert reason.strip(), f"{key} has an empty waiver reason"


def test_the_teardown_role_cannot_read_a_secret(granted_actions):
    """Teardown deletes credentials; it never needs to read one.

    The fix for F-30 was a hair away from being ``secretsmanager:*`` on the same
    resource list. That would have handed the failure path the ability to read every
    connector credential and every provider API key in the account — a strictly
    worse outcome than the leak it repairs, and invisible because nothing would fail.
    ARCC ``cnt_LuG2TKuO0errRp``.
    """
    forbidden = sorted(
        a
        for a in granted_actions
        if a
        in {
            "secretsmanager:GetSecretValue",
            "secretsmanager:BatchGetSecretValue",
            "secretsmanager:*",
        }
        or a == "*"
    )
    assert not forbidden, (
        f"the teardown role can READ secrets: {forbidden}. Deleting a secret does not "
        "require its value. Keep this role to DeleteSecret/DescribeSecret."
    )
    assert _covered("secretsmanager:DeleteSecret", granted_actions), (
        "secretsmanager:DeleteSecret is not granted — this is the exact grant whose absence "
        "left six raw customer credentials in the account (deployment 959b2c60)."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# The permissions the dispatcher's source can never reveal
# ---------------------------------------------------------------------------

# Actions the teardown role needs that NO call in `_cleanup_resource` names, because
# the service makes them on the caller's behalf in a forward-access session. The AST
# oracle above is blind to these by construction: it derives the invariant from the
# calls the source makes, and the source never makes this one.
#
# Keyed by the call that triggers it, so the entry dies with the call. A bare list of
# "extra actions" is the artefact that drifts — this one cannot outlive its trigger,
# because test_the_fas_triggers_are_still_called below fails if the trigger goes away.
#
# The value is a TUPLE of actions, not one action, and that is load-bearing: the harness
# needed two companions and the single-action shape is what made the second one
# unrepresentable. A trigger that needs two grants and can only declare one declares the
# one somebody already thought of.
FAS_COMPANIONS: dict[tuple[str, str], tuple[tuple[str, ...], str]] = {
    ("bedrock-agentcore-control", "delete_gateway"): (
        ("bedrock-agentcore:DeleteWorkloadIdentity",),
        "MEASURED live 2026-09-21, async/silent shape: CreateGateway provisions a "
        "workload-identity record; DeleteGateway returns SUCCESS (200, status DELETING) and "
        "the control plane then calls DeleteWorkloadIdentity back out under the caller's "
        "credentials. Without the grant the gateway parks in status FAILED with statusReasons "
        "naming DeleteWorkloadIdentity (Service: AgentCredentialProvider, 403) — and "
        "_cleanup_resource, having seen a clean return, counted it cleaned. Nothing reaches "
        "the except.",
    ),
    ("bedrock-agentcore-control", "delete_agent_runtime"): (
        ("bedrock-agentcore:DeleteWorkloadIdentity",),
        "MEASURED live 2026-09-21, and NOT the same shape as the gateway: "
        "DeleteAgentRuntime raises AccessDeniedException SYNCHRONOUSLY naming "
        "DeleteWorkloadIdentity on the child ARN, and the runtime stays READY rather than "
        "parking in FAILED. The exception does reach _cleanup_resource's except, which logs "
        "it and counts the resource handled anyway — so it still leaks, just loudly. Same "
        "grant, opposite observability.",
    ),
    ("bedrock-agentcore-control", "delete_harness"): (
        (
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
        ),
        "MEASURED live 2026-09-24 (F-80), and it matched NEITHER of the other two shapes. It "
        "is async and parks in DELETE_FAILED like the gateway rather than raising like the "
        "runtime, AND the 403 named an action neither of the others needs: "
        "DeleteAgentRuntimeEndpoint on runtime/harness_<name>-<id>, because a harness's "
        "backing runtime has an endpoint a bare DeleteAgentRuntime never reaches. Harness "
        "p0bharn1790231300_302f262f-uh37DKSpQX, read off its own failureReason; the harness, "
        "its backing runtime, that runtime's workload identity and the auto-provisioned "
        "Memory holding the conversation all leaked permanently, since a delete is never "
        "retried. The previous entry here predicted 'the synchronous runtime shape' and was "
        "listed UNMEASURED for exactly that reason — the prediction was right that a FAS hop "
        "existed and wrong about which grant closes it, so the statement did not cover it.",
    ),
}


def test_the_fas_triggers_are_still_called():
    """Each companion grant is justified by a call. If the call goes, so must the entry —
    otherwise this file accumulates grants nobody can account for, which is how a
    least-privilege policy quietly becomes a wildcard."""
    calls = _calls_in_cleanup()
    stale = [f"{svc}.{method}" for (svc, method) in FAS_COMPANIONS if (svc, method) not in calls]
    assert not stale, f"stale FAS_COMPANIONS entries — {CLEANUP_FUNC} no longer calls: {stale}"


def test_the_teardown_role_holds_the_grants_the_service_needs_on_its_behalf(granted_actions):
    """The class of bug the AST invariant above cannot reach.

    Every other assertion in this file compares the role against the calls the code
    makes. This one covers a permission the code never mentions: the service takes the
    caller's credentials and makes a further call with them. Measured live 2026-09-21,
    and the consequences differ per resource type — see each FAS_COMPANIONS reason. For
    the gateway the service answers 200 FIRST and fails afterwards, so:

      * the delete appears to succeed, no exception reaches the ``except``, and the
        resource is counted as cleaned;
      * the resource is not deleted — it parks in ``FAILED`` and stays, with its IAM
        role deleted out from under it (``iam_role`` is priority 9, ``gateway`` is 2).

    The runtime instead raises synchronously and stays READY, so it leaks loudly rather
    than silently. Both need this grant; only one is invisible.

    What no amount of simulation reaches, in either shape: on the real role
    ``simulate-principal-policy`` answers ``DeleteGateway`` and ``DeleteAgentRuntime``
    ``allowed`` and ``DeleteWorkloadIdentity`` ``implicitDeny`` — the obvious action is
    genuinely permitted, the denial is on a different resource type, and the simulator
    cannot model the FAS hop at all. Only a live 403 or a grant like this one closes it.

    Note that every role in step_lambdas.py that CREATES one of these resources already
    pairs Create/DeleteWorkloadIdentity with its lifecycle verbs; the role that cleans up
    after a failed deploy held the pairing for none of the three types it deletes.
    """
    missing = []
    for (svc, method), (actions, why) in sorted(FAS_COMPANIONS.items()):
        for action in actions:
            if not _covered(action, granted_actions):
                missing.append(f"{svc}.{method} needs {action} — {why}")

    assert not missing, (
        "the teardown role is missing a grant the SERVICE makes on its behalf, so the "
        "delete will report success and leave the resource in the account:\n" + "\n".join(missing)
    )


def _render_arn(entry) -> str:
    """An ``Fn::Join``ed ARN as a comparable string, with refs as ``${...}``.

    The synthesized resources are ``{"Fn::Join": ["", ["arn:...:", {"Ref": ...}, ":..."]]}``,
    so a substring check against ``json.dumps`` of the raw structure is checking the
    JSON encoding rather than the ARN. Flatten it first.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and "Fn::Join" in entry:
        sep, parts = entry["Fn::Join"]
        return sep.join(_render_arn(p) for p in parts)
    if isinstance(entry, dict) and "Fn::Sub" in entry:
        sub = entry["Fn::Sub"]
        return sub if isinstance(sub, str) else str(sub[0])
    if isinstance(entry, dict) and "Ref" in entry:
        return "${" + str(entry["Ref"]) + "}"
    return str(entry)


def test_the_workload_identity_grant_names_both_authorized_arns(granted_actions):
    """Both ARNs, and not the easy ``resources=["*"]``.

    Two assertions in one because they are the same mistake at opposite ends: too broad
    fails least-privilege, too narrow fails to work at all.

    **Both ARNs are a measured requirement, not belt-and-braces.** AgentCore performs two
    separate authorizations for one logical delete — once against the directory, once
    against the per-identity child — and the 403 names whichever check it reached first.
    Live, on five gateways under five roles differing in one statement: directory-only
    left the gateway FAILED with a 403 naming the CHILD; child-only left it FAILED with a
    403 naming the DIRECTORY; only the pair actually deleted it. So the original error
    message understated its own requirement, and a grant written from that message alone
    would fail identically to no grant at all.

    That is also why this assertion is per-ARN rather than a substring search over the
    rendered statement, which is how it was first written: ``"workload-identity-directory
    /default" in rendered`` is *also* true of the child ARN on its own, so the check
    passed for two of the three configurations measured to leak. A test that cannot
    distinguish a working grant from a broken one is worse than no test, because it
    reports the distinction as verified.

    Asserted against the synthesized template rather than the CDK source so a refactor
    cannot widen or narrow it silently.
    """
    app = cdk.App()
    stack = PlatformStack(
        app,
        "ScopeStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    resources = Template.from_stack(stack).to_json()["Resources"]
    role_ids = [
        lid for lid, res in resources.items() if res["Type"] == "AWS::IAM::Role" and lid.startswith(ROLE_PREFIX)
    ]
    role_id = role_ids[0]

    # By attachment, for the same reason as above: an overflow managed policy is a third
    # attachment shape and the grant looked for here can live in it.
    found = []
    for _src, st in statements_for_role({"Resources": resources}, role_id):
        act = st.get("Action")
        acts = [act] if isinstance(act, str) else (act or [])
        if "bedrock-agentcore:DeleteWorkloadIdentity" in acts:
            found.append(st)

    assert found, "DeleteWorkloadIdentity is not granted to the teardown role at all"

    # Pooled across statements: the two ARNs may legitimately be split, and what has to
    # hold is that the role ends up authorized for both, not that one statement carries
    # them together.
    rendered_arns: list[str] = []
    for st in found:
        res_list = st.get("Resource")
        res_list = [res_list] if not isinstance(res_list, list) else res_list
        assert "*" not in res_list, (
            "DeleteWorkloadIdentity is granted on Resource '*'. Both required ARNs are "
            "expressible at synth time (the directory path is fixed, the child needs only "
            "a trailing wildcard) — so name them instead."
        )
        rendered_arns.extend(_render_arn(r) for r in res_list)

    directory = [a for a in rendered_arns if a.endswith("workload-identity-directory/default")]
    child = [a for a in rendered_arns if a.endswith("workload-identity-directory/default/workload-identity/*")]

    assert directory, (
        "the grant does not name the workload-identity DIRECTORY arn "
        "(.../workload-identity-directory/default). Measured live: with the child arn "
        "alone, DeleteGateway returns 200 and the gateway parks in FAILED with a 403 "
        f"naming the directory. Granted: {json.dumps(rendered_arns)}"
    )
    assert child, (
        "the grant does not name the per-identity CHILD arn "
        "(.../workload-identity-directory/default/workload-identity/*). Measured live: "
        "with the directory arn alone, DeleteGateway returns 200 and the gateway parks in "
        f"FAILED with a 403 naming the child. Granted: {json.dumps(rendered_arns)}"
    )
