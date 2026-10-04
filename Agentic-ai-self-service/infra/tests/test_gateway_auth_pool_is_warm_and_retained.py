"""The shared gateway-auth pool must exist at PLATFORM deploy time, and survive.

Why this construct exists, measured rather than assumed (us-east-1, throwaway pool):

  * ``create_user_pool_domain`` returns immediately.
  * ``describe_user_pool_domain`` reports ``Status=ACTIVE`` after **4 seconds**, and
    kept reporting ACTIVE on all 104 subsequent polls.
  * The DNS name ``<domain>.auth.us-east-1.amazoncognito.com`` first resolved, and
    the ``client_credentials`` token mint first returned 200, at **t+727s**.

Twelve minutes. The deploy-time MCP ``tools/list`` probe window is 90 seconds and the
gateway step Lambda is capped at 300s, so a domain created during a deploy can never
be reachable in time — the probe was not flaky, it was impossible. And the "empty
tool plane" cure (delete the gateway, recreate it) created ANOTHER pool with ANOTHER
cold domain, so each retry was strictly less likely to work than the last. Live:
deployment ``3ef480e2`` / run ``df698a37`` burned 368s over three attempts on pools
``v8OiJanup`` -> ``QU487tO1L`` -> ``yvymML5Pm``, with CloudTrail showing
``CreateGateway`` returning ConflictException on the last two.

So the domain must already be warm, which means the platform stack must own it.

ARCC ``cnt_PQjUx2msVXY1wU`` (unique credentials per entity) is why only the pool and
the public hosted domain are shared: each gateway still gets its own app client and
its own resource server. ``cnt_h02wszR9St529D`` (deletion protection on
storage/identity resources) is why both carry retention in prod-like environments:
DeletionPolicy RetainExceptOnCreate + UpdateReplacePolicy Retain -- they survive a stack
delete, a replacing update and any rollback AFTER a successful create, but not the rollback
of the operation that first creates them (2026-09-25: plain Retain persisted the pool through
a failed first create and blocked the retry). Destroy envs (dev/test/sandbox/preview) delete
them with everything else; see test_stateful_resources_retain_except_on_create.py.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.gateway_auth_pool import MAX_PREFIX_LEN, gateway_domain_prefix_pattern
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "acf"
# A prod-like environment: "test" is in platform_stack's _destroy_envs and would (correctly)
# synthesize Delete/Delete, making every retention assertion below vacuous.
ENVIRONMENT = "staging"


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _resources(template, type_name):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == type_name}


def test_the_platform_creates_the_gateway_auth_domain_itself(template):
    """The whole point: the domain is provisioned by the platform stack, not by a
    deploy. If this resource is gone, every gateway deploy is back to minting a cold
    domain and the tool plane becomes unverifiable again."""
    domains = _resources(template, "AWS::Cognito::UserPoolDomain")
    gw_domains = [
        lid
        for lid, res in domains.items()
        if isinstance(res["Properties"].get("UserPoolId"), dict)
        and res["Properties"]["UserPoolId"].get("Ref", "").startswith("GatewayAuthUserPool")
    ]
    assert len(gw_domains) == 1, (
        "expected exactly one hosted domain on the gateway-auth pool, found "
        f"{gw_domains}. Without it the token endpoint does not exist and "
        "gateway_deployer falls back to creating a cold domain per deploy (727s)."
    )


def test_the_pool_and_domain_are_retained(template):
    """A replaced pool revokes every deployed agent's gateway credentials at once; a
    replaced domain is a ~12-minute token-mint outage for all of them. Neither is a
    fast rebuild, so both must survive a stack update or rollback."""
    pools = {k: v for k, v in _resources(template, "AWS::Cognito::UserPool").items() if k.startswith("GatewayAuth")}
    assert len(pools) == 1, f"expected one GatewayAuth* pool, found {sorted(pools)}"
    for kind, resources in (
        ("pool", pools),
        (
            "domain",
            {
                k: v
                for k, v in _resources(template, "AWS::Cognito::UserPoolDomain").items()
                if isinstance(v["Properties"].get("UserPoolId"), dict)
                and v["Properties"]["UserPoolId"].get("Ref", "").startswith("GatewayAuthUserPool")
            },
        ),
    ):
        for lid, res in resources.items():
            # RetainExceptOnCreate, not Retain: the pool must survive a stack delete or a
            # replacing update, but NOT the rollback of a failed FIRST create -- plain Retain
            # stranded this very pool (us-east-1_wPnNWjU4U) as DELETE_SKIPPED on 2026-09-25
            # when a fresh stack died at its VPC, and the retry then collided on the name.
            assert res.get("DeletionPolicy") == "RetainExceptOnCreate", (
                f"{kind} {lid} DeletionPolicy is {res.get('DeletionPolicy')!r}, expected RetainExceptOnCreate"
            )
            assert res.get("UpdateReplacePolicy") == "Retain", (
                f"{kind} {lid} has no UpdateReplacePolicy: Retain — a property change that forces "
                "replacement would silently destroy it, which is the same outage as a delete"
            )


def test_the_gateway_auth_pool_has_self_signup_disabled(template):
    """It issues machine-to-machine tokens only. An open sign-up on a pool whose
    clients are trusted by every gateway authorizer would let anyone create an
    identity inside the trust boundary."""
    pools = {k: v for k, v in _resources(template, "AWS::Cognito::UserPool").items() if k.startswith("GatewayAuth")}
    props = next(iter(pools.values()))["Properties"]
    admin_only = (props.get("AdminCreateUserConfig") or {}).get("AllowAdminCreateUserOnly")
    assert admin_only is True, (
        f"self_sign_up must stay disabled on the shared gateway-auth pool; AllowAdminCreateUserOnly={admin_only!r}"
    )


#: Lambdas that run ``gateway_deployer`` code and therefore MUST learn the warm pool.
#: Matched by logical-id prefix, not by a name substring: the previous version of this
#: test filtered on ``"-step-" in FunctionName``, which silently excluded
#: ``DeploymentLambda`` — the one that runs ``cleanup_gateway_resources`` on the delete
#: path. It happened to be wired correctly, so the gap never showed as a failure; the
#: test simply was not checking it.
_NEEDS_WARM_POOL = ("Step", "DeploymentLambda")

#: Lambdas that legitimately do NOT need it, each with the reason. Present so that a
#: NEW Lambda cannot join the stack unclassified: the exhaustiveness assertion below
#: fails until someone decides which side it belongs on.
_NO_GATEWAY_WORK = {
    "StreamLambda": "proxies a runtime's response stream; never creates or deletes a gateway",
    "WorkflowLambda": (
        "the CRUD/API handler. Its in-process deploy route is refused (501) wherever "
        "AWS_LAMBDA_FUNCTION_NAME is set, i.e. anywhere the platform is actually "
        "deployed, so it never reaches gateway_deployer there. That reason previously "
        "cited STATE_MACHINE_ARN, which was wrong twice over: the gate keyed off a "
        "variable this very Lambda does not have (8 env vars live, none of them that "
        "one), so it never fired in production"
    ),
}


def test_every_lambda_that_touches_a_gateway_learns_the_pool_and_domain(template):
    """gateway_deployer reads these two env vars to decide whether it can reuse the
    warm domain. If either is missing it silently falls back to creating its own cold
    pool -- the exact behaviour this construct exists to remove -- and nothing fails
    loudly, so the wiring is what has to be pinned."""
    fns = _resources(template, "AWS::Lambda::Function")
    # CDK's own custom-resource providers (bucket deployment, auto-delete-objects) carry
    # no FunctionName and are not ours to classify.
    ours = {lid: res for lid, res in fns.items() if isinstance(res["Properties"].get("FunctionName"), str)}
    assert ours, "no named Lambdas found — the lookup is broken, not the wiring"

    step_fns = {lid: res for lid, res in ours.items() if lid.startswith(_NEEDS_WARM_POOL)}
    unclassified = sorted(
        lid for lid in ours if not lid.startswith(_NEEDS_WARM_POOL) and not lid.startswith(tuple(_NO_GATEWAY_WORK))
    )
    assert not unclassified, (
        f"these Lambdas are in the stack but this test does not know whether they run "
        f"gateway_deployer: {unclassified}. Add each to _NEEDS_WARM_POOL or to "
        f"_NO_GATEWAY_WORK with a reason."
    )
    assert any(lid.startswith("DeploymentLambda") for lid in step_fns), (
        "DeploymentLambda is not being checked; it runs cleanup_gateway_resources"
    )

    for lid, res in step_fns.items():
        env = (res["Properties"].get("Environment") or {}).get("Variables") or {}
        assert "GATEWAY_SHARED_USER_POOL_ID" in env, f"{lid} missing GATEWAY_SHARED_USER_POOL_ID"
        assert "GATEWAY_SHARED_USER_POOL_DOMAIN" in env, f"{lid} missing GATEWAY_SHARED_USER_POOL_DOMAIN"
        # The pool id must be a real reference to the pool, not an empty string:
        # gateway_deployer treats "" as "no shared pool" and falls back.
        assert isinstance(env["GATEWAY_SHARED_USER_POOL_ID"], dict), (
            f"{lid}'s GATEWAY_SHARED_USER_POOL_ID is a literal {env['GATEWAY_SHARED_USER_POOL_ID']!r}, "
            "so the fallback-to-cold-domain path is what would run"
        )
        assert env["GATEWAY_SHARED_USER_POOL_DOMAIN"], f"{lid}'s domain prefix is empty"


def test_the_gateway_step_can_delete_the_resource_server_it_creates(template):
    """Teardown deletes only this gateway's own app client and resource server inside
    the shared pool. Without DeleteResourceServer the per-gateway scope leaks into the
    shared pool on every delete and accumulates forever."""
    roles = [lid for lid, r in _resources(template, "AWS::IAM::Role").items() if lid.startswith("StepGatewayRole")]
    assert len(roles) == 1, f"expected one StepGatewayRole*, found {roles}"
    actions: set[str] = set()
    # By attachment: the gateway role's inline document is over CDK's size limit, so part
    # of it synthesizes into StepGatewayRoleOverflowPolicy* -- an AWS::IAM::ManagedPolicy.
    # Scanning AWS::IAM::Policy alone reported one of this role's cognito grants as
    # missing when it was present, in the sibling client-secret test.
    for _src, st in statements_for_role(template, roles[0]):
        act = st.get("Action")
        actions.update([act] if isinstance(act, str) else (act or []))
    for needed in (
        "cognito-idp:CreateResourceServer",
        "cognito-idp:DeleteResourceServer",
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:DeleteUserPoolClient",
    ):
        assert needed in actions, f"gateway step role is missing {needed}"


def _cfg(project=PROJECT, env=ENVIRONMENT):
    class _Cfg:
        pass

    _Cfg.project = project
    _Cfg.env = env
    return _Cfg()


def test_the_domain_prefix_is_globally_unique_per_deployment():
    """Cognito domain prefixes share ONE namespace across all of AWS, so two
    unrelated deployments that pick the same project/env name would collide and the
    second stack's create would fail. Uniqueness now comes from two places: the
    ``${AWS::AccountId}`` CloudFormation substitutes at deploy time, and a digest over
    the synth-time-known region/project/env."""
    a = gateway_domain_prefix_pattern(REGION, _cfg())
    b = gateway_domain_prefix_pattern("eu-central-1", _cfg())
    c = gateway_domain_prefix_pattern(REGION, _cfg(project="other"))
    d = gateway_domain_prefix_pattern(REGION, _cfg(env="prod"))

    assert "${AWS::AccountId}" in a, (
        "the account must be a CloudFormation placeholder. Hashing the account at synth "
        "time is what broke this: app.py synthesizes account-agnostic, so the call site "
        "passed the unresolved token ${Token[AWS.AccountId.N]} and the prefix changed "
        "between synths of identical code."
    )
    assert len({a, b, c, d}) == 4, f"region/project/env must each change the prefix: {[a, b, c, d]}"
    # Same inputs -> same output. Weak on its own (see the cross-process test below),
    # but it pins the function itself.
    assert gateway_domain_prefix_pattern(REGION, _cfg()) == a

    for pattern in (a, b, c, d):
        # Validate the RESOLVED prefix: substitute a real 12-digit account id.
        resolved = pattern.replace("${AWS::AccountId}", "166827918465")
        assert len(resolved) <= MAX_PREFIX_LEN, f"prefix is {len(resolved)} chars: {resolved}"
        assert resolved == resolved.lower()
        assert not resolved.startswith("-") and not resolved.endswith("-")
        assert all(ch.isalnum() or ch == "-" for ch in resolved)
        assert not resolved.startswith("aws"), "Cognito rejects a domain prefix beginning with 'aws'"


def test_no_unresolved_cdk_token_can_reach_the_prefix():
    """The original defect in one assertion. ``stack.account`` on an account-agnostic
    stack is the string ``${Token[AWS.AccountId.7]}``; hashing it produced a prefix
    that depended on CDK's token-allocation order, so identical code synthesized four
    different prefixes and a redeploy would REPLACE the hosted domain -- a ~12-minute
    token-mint outage for every deployed agent, and (ARCC cnt_ua0cTwldOsODs8) an
    abandoned name in a global namespace that another account can claim while agents
    still post their client_credentials to it.

    The function no longer takes an account at all, so pin that: it must be
    impossible to pass one in, and no CDK token may appear in the output."""
    import inspect

    params = list(inspect.signature(gateway_domain_prefix_pattern).parameters)
    assert "account" not in params, (
        f"gateway_domain_prefix_pattern accepts {params}; an `account` parameter is how "
        "the token got into the digest in the first place"
    )
    pattern = gateway_domain_prefix_pattern(REGION, _cfg())
    assert "Token[" not in pattern and "${Token" not in pattern, f"a CDK token leaked into the prefix: {pattern}"


def test_the_synthesized_domain_is_identical_across_processes():
    """The decisive regression test, and the one the original version could not be.

    The old test called the prefix function with a literal account string, so it was
    deterministic by construction while the production call site was not. Token
    indices vary between PROCESSES, so reproducing the bug requires synthesizing in a
    separate interpreter -- twice, with different hash seeds -- and comparing the
    resolved template. This fails on the old code and passes on the new."""
    import json
    import os
    import subprocess
    import sys
    import textwrap

    infra_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = textwrap.dedent(
        """
        import json
        import aws_cdk as cdk
        from stacks.platform.config import PlatformConfig
        from stacks.platform.gateway_auth_pool import build_gateway_auth_pool

        app = cdk.App()
        # Exactly how app.py does it: region only, NO account. That is what makes
        # stack.account an unresolved token.
        stack = cdk.Stack(app, "S", env=cdk.Environment(region="us-east-1"))
        cfg = PlatformConfig(
            env="test", project="acf",
            removal_policy=cdk.RemovalPolicy.RETAIN, allow_destroy=False,
        )
        build_gateway_auth_pool(stack, cfg)
        tpl = app.synth().get_stack_by_name("S").template
        print(json.dumps([
            v["Properties"]["Domain"]
            for v in tpl["Resources"].values()
            if v["Type"] == "AWS::Cognito::UserPoolDomain"
        ]))
        """
    )

    outs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONPATH": infra_root, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script], capture_output=True, text=True, cwd=infra_root, env=env, check=False
        )
        assert proc.returncode == 0, f"synth subprocess failed (seed={seed}):\n{proc.stderr[-3000:]}"
        outs.append(json.loads(proc.stdout.strip().splitlines()[-1]))

    assert outs[0] == outs[1] == outs[2], (
        "the hosted-domain name differs between synths of identical code:\n  "
        + "\n  ".join(json.dumps(o) for o in outs)
        + "\n\nA changed domain prefix REPLACES the hosted domain, which is unreachable "
        "for ~12 minutes (727s measured) -- an outage for every deployed agent's token "
        "mint -- and leaves the old name abandoned in Cognito's global namespace."
    )
    assert outs[0] and "Fn::Sub" in json.dumps(outs[0]), (
        f"the account id must be substituted by CloudFormation at deploy time, not baked "
        f"in at synth time. Got {outs[0]}"
    )


def test_a_long_project_name_still_yields_a_legal_prefix():
    """Cognito rejects a prefix over 63 chars, and the project name is user-supplied
    via CDK context, so truncation has to be part of the function rather than a
    convention the caller remembers. The budget must account for the 12 digits
    CloudFormation substitutes, which are not in the pattern's own length."""
    pattern = gateway_domain_prefix_pattern(REGION, _cfg(project="x" * 90, env="production-eu-central"))
    resolved = pattern.replace("${AWS::AccountId}", "166827918465")
    assert len(resolved) <= MAX_PREFIX_LEN, f"resolved prefix is {len(resolved)} chars: {resolved}"
    assert not resolved.startswith("-") and not resolved.endswith("-")
    assert "-gw-" in resolved, "the digest must survive truncation or uniqueness is lost"


def test_a_project_named_aws_does_not_produce_a_prefix_cognito_rejects():
    """Cognito forbids a domain prefix beginning with "aws". The project name comes
    from CDK context, so a project called "aws-agents" would fail the deploy with an
    error that names no cause."""
    resolved = gateway_domain_prefix_pattern(REGION, _cfg(project="aws-agents")).replace(
        "${AWS::AccountId}", "166827918465"
    )
    assert not resolved.startswith("aws"), resolved


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
