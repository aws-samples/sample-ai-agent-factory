"""The shared runtime role must be able to read the gateway client secret -- and it
must be able to do so WITHOUT a Cognito grant. See TestTheCognitoGrant below: the
Cognito half of this file asserted the wrong thing for as long as it existed, and is
now an inversion. The file name is kept so the history stays findable.

The deploy paths stopped injecting ``COGNITO_CLIENT_SECRET`` as a runtime
environment variable, because ``GetAgentRuntime`` returns a runtime's environment
variables in plaintext and every Task in the deployment state machine re-emits the
whole event into the execution history (measured live: 89 copies of one secret, 90-day
retention). The generated agent re-reads the secret at the moment of use instead —
``DescribeUserPoolClient`` for a Cognito gateway, ``GetSecretValue`` for an external
IDP.

That makes the IAM grant load-bearing in a way that is invisible until it is too late:
without it every deploy still goes GREEN and the agent then cannot mint a gateway token
at all, so every tool call fails with no tools discovered. A unit test on the generated
code cannot catch it. These tests pin the grant onto the synthesized template.

ARCC cnt_LuG2TKuO0errRp: access to a specific secret must only be allowed where the
principal using the policy needs that secret. cnt_SFJJhkOueCPRkd: put a condition key on
a wildcard that cannot be removed.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "agentcore-workflow"
ENVIRONMENT = "test"


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


def _shared_runtime_role_policies(template_json) -> list[dict]:
    """Every policy statement attached to the shared runtime exec role, any shape.

    The role is found by its RoleName rather than its logical id so a refactor of the
    construct tree cannot make these tests silently pass against nothing.

    Resolution is delegated to ``tests/iam_attachment.py`` because this function used to
    read ``AWS::IAM::Policy`` only, and the over-reach assertions in this module are the
    ``assert not _statements_allowing(...)`` shape -- they pass when the statement set is
    EMPTY. CDK spills statements past the inline-policy size limit into a
    ``<Role>OverflowPolicy<N>`` managed policy on its own, so a grant this module forbids
    could have moved out of view and every "must not be granted" assertion would have
    gone on passing. The same blind spot was caught next door reporting a grant that IS
    present as absent (``StepGatewayRoleOverflowPolicy1EBBF24B3``), which is the loud
    direction; this file had the silent one.
    """
    resources = template_json["Resources"]
    role_logical_ids = [
        lid
        for lid, res in resources.items()
        if res["Type"] == "AWS::IAM::Role"
        and res["Properties"].get("RoleName") == f"AgentCoreRuntime-{PROJECT}-{ENVIRONMENT}-shared"
    ]
    assert role_logical_ids, "the shared AgentCore runtime exec role is not in the template"
    assert len(role_logical_ids) == 1, (
        f"expected exactly one shared runtime exec role, found {role_logical_ids}. "
        "Assertions scoped to an ambiguous role stop being evidence."
    )
    return [st for _src, st in statements_for_role(template_json, role_logical_ids[0])]


def _statements_allowing(statements: list[dict], action: str) -> list[dict]:
    out = []
    for st in statements:
        if st.get("Effect") != "Allow":
            continue
        actions = st.get("Action")
        actions = [actions] if isinstance(actions, str) else (actions or [])
        if action in actions:
            out.append(st)
    return out


class TestTheCognitoGrant:
    """This class used to assert the OPPOSITE, and the belief it encoded was wrong.

    It required the shared runtime role to hold ``DescribeUserPoolClient`` conditioned
    on ``aws:ResourceTag/AgentCoreStack``, under a docstring claiming the condition
    scoped the grant to "this deployment's pools only, never a co-resident
    deployment's". That reading is false: the tag VALUE is
    ``{project}-{env}-{region}``, which names the STACK, and every deployment in the
    stack stamps the identical value on the pool it creates. So the condition that
    looked like per-deployment scoping authorized reading the gateway client secret of
    every co-resident deployment — exactly the cross-tenant read the test's own
    docstring said it prevented.

    Cognito's IAM resource type is ``userpool`` with no granularity below it, so there
    is no narrower version to retreat to. The credential moved to Secrets Manager
    instead (``gateway_deployer._mint_client_secret_ref``), which scopes per ARN, and
    this role now holds no Cognito read at all.

    The tests below are kept as an inversion rather than deleted, because a deleted
    test cannot fail when someone re-adds the grant to fix a "legacy agent can't get a
    token" bug report.
    """

    def test_the_shared_role_holds_no_client_secret_grant_at_all(self, template_json):
        grants = _statements_allowing(
            _shared_runtime_role_policies(template_json), "cognito-idp:DescribeUserPoolClient"
        )
        assert not grants, (
            "the SHARED, tenant-facing runtime role was granted "
            f"cognito-idp:DescribeUserPoolClient: {grants}. Cognito IAM cannot scope "
            "below the pool, so this reads every co-resident deployment's gateway "
            "client secret — with or without the AgentCoreStack condition, whose value "
            "is the stack id and therefore identical for every deployment in it. The "
            "runtime resolves its secret from OAUTH_CLIENT_SECRET_REF instead."
        )

    def test_the_replacement_is_actually_reachable(self, template_json):
        """Vacuity guard for the test above. Removing a grant is only correct if the
        path that replaces it works; otherwise this file would happily pass against a
        role that cannot resolve a client secret by ANY means — which is the original
        green-deploy-dead-tool-plane failure with a different cause."""
        grants = _statements_allowing(_shared_runtime_role_policies(template_json), "secretsmanager:GetSecretValue")
        covered = []
        for grant in grants:
            resources = grant.get("Resource")
            resources = [resources] if not isinstance(resources, list) else resources
            covered.extend(r for r in resources if isinstance(r, str))
        assert any("secret:agentcore-connector/" in r for r in covered), (
            "the runtime role has no GetSecretValue on the agentcore-connector/ "
            "namespace, which is where the gateway step now writes the client secret. "
            f"Without it no gateway token can be minted at all. Grants: {covered}"
        )

    def test_the_role_cannot_write_to_cognito(self, template_json):
        """The grant exists to read one app client's configuration. Nothing about the
        secret fix needs a write, and a runtime that can mutate the pool that
        authenticates it is a privilege-escalation path."""
        statements = _shared_runtime_role_policies(template_json)
        forbidden = (
            "cognito-idp:UpdateUserPoolClient",
            "cognito-idp:CreateUserPoolClient",
            "cognito-idp:DeleteUserPoolClient",
            "cognito-idp:AdminCreateUser",
            "cognito-idp:*",
        )
        for action in forbidden:
            assert not _statements_allowing(statements, action), f"the shared runtime role must not be granted {action}"


class TestTheSecretsManagerGrant:
    def test_no_secrets_grant_is_account_wide(self, template_json):
        """The single most dangerous way to satisfy the external-IDP half of this fix.
        Every other tenant's connector credentials and the platform's git PATs live in
        this account's Secrets Manager; an agent's runtime role reading all of them
        would be a cross-tenant break, not a least-privilege slip."""
        grants = _statements_allowing(_shared_runtime_role_policies(template_json), "secretsmanager:GetSecretValue")
        assert grants, "the runtime role has no GetSecretValue at all — the external-IDP path cannot work"
        for grant in grants:
            resources = grant.get("Resource")
            resources = [resources] if not isinstance(resources, list) else resources
            for resource in resources:
                assert resource != "*", "GetSecretValue on '*' would expose every secret in the account"
                if isinstance(resource, str):
                    assert not resource.endswith(":secret:*"), (
                        f"GetSecretValue scoped to {resource} is every secret in the region — "
                        "scope it to the platform's own namespace, or use the per-agent role"
                    )

    def test_the_grant_is_confined_to_the_platform_namespace(self, template_json):
        """A customer's own long-lived secret, outside the two platform namespaces
        (`agentcore-connector/` and `agentcore-provider/`), is deliberately NOT readable
        by the shared role — that case must use the per-agent identity mode, which scopes
        to the one secret ARN at deploy time."""
        grants = _statements_allowing(_shared_runtime_role_policies(template_json), "secretsmanager:GetSecretValue")
        flattened = []
        for grant in grants:
            resources = grant.get("Resource")
            resources = [resources] if not isinstance(resources, list) else resources
            flattened.extend(r for r in resources if isinstance(r, str))
        assert any("secret:agentcore-connector/" in r for r in flattened), (
            "no GetSecretValue grant covers the agentcore-connector/ namespace, so an "
            f"external-IDP client secret can never be read; grants: {flattened}"
        )


class TestTheModelProviderKeyGrant:
    """The runtime reads only the deployment-bound copy of a provider key.

    The reusable source outlives a deployment under ``agentcore-provider/``. The
    deploy boundary validates its tenant/stack tags and copies it into this
    deployment's ``agentcore-connector/`` lifecycle before the runtime is created.
    Letting the shared runtime role read the source namespace would let every
    co-resident runtime read every tenant's long-lived provider key.
    """

    def _secret_grants(self, template_json) -> list[str]:
        grants = _statements_allowing(_shared_runtime_role_policies(template_json), "secretsmanager:GetSecretValue")
        flattened: list[str] = []
        for grant in grants:
            resources = grant.get("Resource")
            resources = [resources] if not isinstance(resources, list) else resources
            flattened.extend(r for r in resources if isinstance(r, str))
        return flattened

    def test_the_shared_role_cannot_read_long_lived_provider_sources(self, template_json):
        flattened = self._secret_grants(template_json)
        assert not any("secret:agentcore-provider/" in r for r in flattened), (
            "the tenant-facing shared runtime role can read long-lived "
            f"agentcore-provider sources: {flattened}. Provider keys must be copied "
            "into one deployment-bound connector secret before this role sees them."
        )

    def test_the_deployment_bound_connector_namespace_is_reachable(self, template_json):
        flattened = self._secret_grants(template_json)
        assert any("secret:agentcore-connector/" in r for r in flattened), (
            f"the runtime cannot read the deployment-bound provider/OTEL/gateway credential copies; grants: {flattened}"
        )

    def test_the_namespace_is_not_merged_into_one_prefix(self, template_json):
        """A grant on ``agentcore-*`` would also cover source and admin namespaces."""
        flattened = self._secret_grants(template_json)
        for resource in flattened:
            assert ":secret:agentcore-*" not in resource, (
                f"{resource} grants every agentcore-prefixed namespace at once, including ones that do not exist yet"
            )

    def test_the_provider_grant_is_read_only(self, template_json):
        """A runtime that can write the provider key can swap in its own and bill the
        account, or overwrite a key every other agent shares."""
        statements = _shared_runtime_role_policies(template_json)
        for action in (
            "secretsmanager:PutSecretValue",
            "secretsmanager:UpdateSecret",
            "secretsmanager:DeleteSecret",
            "secretsmanager:CreateSecret",
            "secretsmanager:*",
        ):
            assert not _statements_allowing(statements, action), f"the shared runtime role must not be granted {action}"
