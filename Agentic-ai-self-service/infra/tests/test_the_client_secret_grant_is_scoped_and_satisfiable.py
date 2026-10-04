"""Every ``DescribeUserPoolClient`` grant in the stack, checked over the template.

WHY OVER THE WHOLE TEMPLATE AND NOT PER ROLE. Three roles needed this action and each
was written separately, so each got the scoping wrong in a DIFFERENT direction:

* the shared AgentCore runtime role scoped ``userpool/*`` with a condition on
  ``aws:ResourceTag/AgentCoreStack`` -- correct for a pool the gateway step creates at
  deploy time, and UNSATISFIABLE for the shared gateway-auth pool, which is a CDK
  construct and therefore carries CloudFormation's tags plus Project/Environment but
  never AgentCoreStack. Measured on the live pool ``us-east-1_qiYLOs3Ij``: tags were
  exactly Environment, Project and three ``aws:cloudformation:*`` keys. So in the
  DEFAULT ``shared`` identity mode an agent could not resolve its gateway client secret
  and exposed no tools -- a green deploy with a dead tool plane;
* the gateway and harness step roles scoped bare ``userpool/*`` with NO condition, which
  let either read the app-client secret of every Cognito pool in the account.

Reading one role would not have caught the other, so the invariant is asserted over
EVERY statement carrying the action, and a fourth role added later cannot reintroduce
either mistake.

THE THIRD MISTAKE, which is the reason for ``test_the_tenant_runtime_role_...`` below:
the obvious repair for the unsatisfiable condition is to ALSO grant the shared runtime
role the shared pool's exact ARN. That is a cross-tenant credential break, measured
live rather than argued -- Cognito IAM has no granularity below the pool, the same role
already holds ListGateways/GetGateway on ``*`` (so it can read any gateway's
``allowedClients``), and ``describe-user-pool-client`` returns ``ClientSecret`` for
whatever client id it is handed. So that specific combination is asserted ABSENT.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.cognito_client_secret_grant import ACTION, OWNER_TAG_KEY, owner_tag_value
from stacks.platform_stack import PlatformStack

PROJECT = "acfe2e"
ENVIRONMENT = "p0920"
REGION = "us-east-1"
ACCOUNT = "123456789012"


@pytest.fixture(scope="module")
def synth():
    app = cdk.App()
    stack = PlatformStack(
        app,
        f"{PROJECT}-{ENVIRONMENT}",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    return stack, Template.from_stack(stack)


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _client_secret_statements(template: Template) -> list[tuple[str, dict]]:
    """Every statement in every IAM policy that carries the action, with its policy id."""
    found: list[tuple[str, dict]] = []
    for res_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"):
        for logical_id, res in template.find_resources(res_type).items():
            if logical_id.startswith("AgentCoreRoleBoundary"):
                # AgentCoreRoleBoundary is the permissions boundary for roles the backend mints (F-06,
                # stacks/platform/role_boundary.py). It is attached to no principal, so it grants nothing:
                # it is the cap on what a CREATED role may be granted, and its wildcards are that ceiling.
                # test_f06_role_permissions_boundary.py pins that nothing references it and that it is the
                # only unattached managed policy, so this exemption cannot hide a real grant.
                continue
            props = res.get("Properties", {})
            docs = []
            if "PolicyDocument" in props:
                docs.append(props["PolicyDocument"])
            for inline in props.get("Policies", []) or []:
                if isinstance(inline, dict) and "PolicyDocument" in inline:
                    docs.append(inline["PolicyDocument"])
            for doc in docs:
                for stmt in doc.get("Statement", []) or []:
                    if ACTION in _as_list(stmt.get("Action")):
                        found.append((logical_id, stmt))
    return found


def test_the_action_is_granted_at_all(synth):
    """Vacuity guard. Every assertion below is satisfied by a template that grants the
    action nowhere -- which is exactly the failure mode being prevented (the feature
    silently dead), so the count is pinned first."""
    _stack, template = synth
    stmts = _client_secret_statements(template)
    assert stmts, (
        f"no statement in the template grants {ACTION}. The gateway and harness step "
        "roles must both have it, or the gateway step cannot read the secret of the app "
        "client it just created and every gateway deploy fails closed."
    )


def test_every_grant_is_either_an_exact_pool_or_owner_tag_conditioned(synth):
    """The invariant. A bare ``userpool/*`` with no condition is the bug the step roles
    had; it reaches every Cognito pool in the account, including other products'."""
    _stack, template = synth
    offenders = []
    for logical_id, stmt in _client_secret_statements(template):
        resources = _as_list(stmt.get("Resource"))
        cond = stmt.get("Condition") or {}
        tag_cond = (cond.get("StringEquals") or {}).get(f"aws:ResourceTag/{OWNER_TAG_KEY}")
        # A wildcard pool segment is only acceptable with the owner-tag condition.
        wildcard = any(isinstance(r, str) and r.endswith("userpool/*") for r in resources)
        if wildcard and not tag_cond:
            offenders.append((logical_id, resources, cond))
        # And "*" is never acceptable, conditioned or not.
        if any(r == "*" for r in resources):
            offenders.append((logical_id, resources, cond))
    assert not offenders, (
        f"these {ACTION} statements are scoped too widely: {offenders}. "
        "DescribeUserPoolClient returns the app client's SECRET, so an unconditioned "
        "userpool/* grants the credential of every Cognito app client in the account."
    )


def test_the_owner_tag_value_matches_what_the_backend_actually_stamps(synth):
    """If the CDK condition value and ``resource_ownership.stack_id()`` ever disagree,
    the condition matches nothing, every client-secret read fails closed, and the deploy
    still goes green. So the two are compared directly rather than trusted to stay in
    sync by convention."""
    stack, _template = synth
    import os
    import pathlib
    import sys

    backend_src = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src"
    sys.path.insert(0, str(backend_src))
    try:
        # stack_id reads the same three env vars the Lambdas resolve at runtime.
        old = {k: os.environ.get(k) for k in ("PROJECT_NAME", "ENVIRONMENT", "APP_AWS_REGION")}
        os.environ["PROJECT_NAME"] = PROJECT
        os.environ["ENVIRONMENT"] = ENVIRONMENT
        os.environ["APP_AWS_REGION"] = REGION
        try:
            from app.services.resource_ownership import OWNER_TAG_KEY as BACKEND_KEY
            from app.services.resource_ownership import stack_id

            assert owner_tag_value(stack, _Cfg()) == stack_id(REGION), (
                "the CDK condition value and the tag the backend stamps have drifted"
            )
            assert OWNER_TAG_KEY == BACKEND_KEY, "the owner tag KEY has drifted"
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        sys.path.remove(str(backend_src))


class _Cfg:
    """Minimal stand-in for PlatformConfig — owner_tag_value reads only these two."""

    project = PROJECT
    env = ENVIRONMENT


def test_the_shared_pool_is_covered_by_at_least_one_grant(synth):
    """The satisfiability half. The bug was a grant that parsed, synthesized, deployed
    and authorized NOTHING, because the only pool the default identity mode uses could
    not satisfy its condition. So at least one statement must name the shared pool's
    own ARN -- the control-plane step roles carry it."""
    _stack, template = synth
    pools = template.find_resources("AWS::Cognito::UserPool")
    gw_pool_ids = [lid for lid in pools if "GatewayAuth" in lid]
    assert len(gw_pool_ids) == 1, f"expected exactly one gateway-auth pool construct, got {gw_pool_ids}"

    def _names_the_pool(resources) -> bool:
        for r in resources:
            # CDK renders a construct's ARN as a Fn::GetAtt / Fn::Join over its
            # logical id, so the check is structural: does the rendered ARN mention it?
            if gw_pool_ids[0] in repr(r):
                return True
        return False

    naming = [
        lid for lid, stmt in _client_secret_statements(template) if _names_the_pool(_as_list(stmt.get("Resource")))
    ]
    assert naming, (
        "no statement grants DescribeUserPoolClient on the shared gateway-auth pool's "
        "exact ARN. The gateway step creates the app client in that pool and must read "
        "its secret once; without this the gateway deploy fails closed with "
        "AccessDeniedException on a green-looking deploy."
    )


def test_the_tenant_runtime_role_cannot_read_the_shared_pools_client_secrets(synth):
    """The cross-tenant break, asserted absent.

    MEASURED LIVE, not reasoned: the shared runtime role holds
    ``bedrock-agentcore:ListGateways`` + ``GetGateway`` on ``Resource=*`` with no
    condition; ``get-gateway`` on the live gateway ``agent-gateway-1kjpgafkwg`` returned
    ``customJWTAuthorizer.allowedClients`` (the app client id) and the pool id in its
    discoveryUrl; and ``describe-user-pool-client`` returned a 51-character
    ``ClientSecret`` for that id. Cognito IAM has no resource granularity below the
    pool, and every gateway in the shared pool shares one token endpoint.

    So a DescribeUserPoolClient grant naming the shared pool on this role lets agent A
    read agent B's gateway client secret and mint a token for B's scope. Because the
    role is shared, no deploy-time scoping can fix it -- and identity mode 'per_agent'
    cannot either, since a per-agent role naming the same pool inherits the same
    pool-wide read. The runtime therefore reads its secret from a per-deployment Secrets
    Manager reference (OAUTH_CLIENT_SECRET_REF) instead, which IS scopeable per ARN.
    """
    _stack, template = synth
    pools = template.find_resources("AWS::Cognito::UserPool")
    gw_pool_id = next(lid for lid in pools if "GatewayAuth" in lid)

    runtime_role_ids = [lid for lid in template.find_resources("AWS::IAM::Role") if "SharedRuntimeExecRole" in lid]
    assert len(runtime_role_ids) == 1, f"shared runtime role logical id drifted: {runtime_role_ids}"
    runtime_role = runtime_role_ids[0]

    # Find the policies attached to that role, then the offending statements.
    offenders = []
    for logical_id, stmt in _client_secret_statements(template):
        if runtime_role not in logical_id and not logical_id.startswith("SharedRuntimeExecRole"):
            continue
        if gw_pool_id in repr(_as_list(stmt.get("Resource"))):
            offenders.append((logical_id, stmt))
    assert not offenders, (
        "the SHARED, tenant-facing AgentCore runtime role was granted "
        f"{ACTION} on the shared gateway-auth pool: {offenders}. Cognito IAM cannot "
        "scope below the pool, so this authorizes reading EVERY deployed agent's "
        "gateway client secret, and the same role already has GetGateway on * to "
        "discover the client ids. Move the secret to a per-deployment Secrets Manager "
        "reference instead of widening this role."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
