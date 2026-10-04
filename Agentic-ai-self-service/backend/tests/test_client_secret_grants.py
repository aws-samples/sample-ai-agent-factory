"""The one grant that makes the "no secret in an env var" fix actually work.

Every deploy path stopped injecting ``COGNITO_CLIENT_SECRET`` into the runtime's
environment, because ``GetAgentRuntime`` returns a runtime's environment variables in
plaintext and every Task in the deployment state machine copies the whole event into
the execution history for 90 days. The generated agent now resolves the secret at the
moment of use — ``cognito-idp:DescribeUserPoolClient`` for a platform-minted Cognito
gateway, ``secretsmanager:GetSecretValue`` for an external IDP.

The failure mode of getting the IAM wrong is the reason these tests exist: the deploy
still reports SUCCESS, and the agent then cannot mint a gateway token at all, so every
tool call fails with an empty tool list. Nothing goes red until a human invokes the
agent. Three deploy paths derive these ARNs (per-agent role, legacy per-deploy role,
direct deploy), and a mismatch between them is invisible the same way — hence one
helper, tested here.

ARCC cnt_LuG2TKuO0errRp: a policy may allow access to a specific secret only where the
principal using it needs that secret.
"""

import pytest
from app.services import per_agent_identity
from app.services.runtime_deployer import client_secret_grant_targets

REGION = "us-east-1"
ACCOUNT = "123456789012"


def _sids(policy: dict) -> set[str]:
    return {st.get("Sid") for st in policy["Statement"]}


def _statement(policy: dict, sid: str) -> dict:
    matches = [st for st in policy["Statement"] if st.get("Sid") == sid]
    assert len(matches) == 1, f"expected exactly one {sid} statement, found {len(matches)}"
    return matches[0]


class TestClientSecretGrantTargets:
    def test_a_cognito_pool_id_becomes_a_pool_arn(self):
        pool_arn, secret_arn = client_secret_grant_targets({"user_pool_id": "us-east-1_AbC123"}, REGION, ACCOUNT)
        assert pool_arn == f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_AbC123"
        assert secret_arn is None

    def test_a_secret_ref_is_a_name_so_it_needs_the_suffix_wildcard(self):
        """Secrets Manager appends a random six-character suffix to every secret's
        ARN. A policy naming ``secret:my/secret`` exactly matches nothing — the
        GetSecretValue call is denied and the agent cannot authenticate. The trailing
        ``-*`` is load-bearing, not cosmetic."""
        _, secret_arn = client_secret_grant_targets(
            {"client_secret_ref": "agentcore-connector/acme/abc123"}, REGION, ACCOUNT
        )
        assert secret_arn == f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentcore-connector/acme/abc123-*"

    def test_a_ref_that_is_already_an_arn_is_passed_through_unchanged(self):
        """Appending ``-*`` to a full ARN would produce a resource that matches the
        intended secret plus anything sharing its six-character suffix prefix."""
        arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:okta/client-AbCdEf"
        _, secret_arn = client_secret_grant_targets({"client_secret_ref": arn}, REGION, ACCOUNT)
        assert secret_arn == arn

    def test_the_camel_case_spelling_is_accepted_too(self):
        """client_info crosses a JSON boundary between the gateway step and the IAM
        step, and the API models use camelCase. Reading only snake_case would silently
        grant nothing for an external IDP."""
        _, secret_arn = client_secret_grant_targets({"clientSecretRef": "ext/idp"}, REGION, ACCOUNT)
        assert secret_arn == f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:ext/idp-*"

    @pytest.mark.parametrize("client_info", [None, {}, {"user_pool_id": "", "client_secret_ref": ""}])
    def test_an_agent_with_no_gateway_is_granted_nothing(self, client_info):
        """The default must be no grant at all. An agent that never authenticates to a
        gateway has no business reading a user pool or a secret, and ``{}`` is what the
        helper sees on every non-gateway deploy."""
        assert client_secret_grant_targets(client_info, REGION, ACCOUNT) == (None, None)

    def test_both_targets_can_be_present_at_once(self):
        """INVERTED. This test used to assert ``pool_arn and secret_arn``, encoding the
        belief that handing a role both grants was harmless because each is scoped to one
        resource. It is not: ``cognito-idp:DescribeUserPoolClient``'s only IAM resource
        type is ``userpool``, with no granularity below it, so the pool grant reads the
        client secret of EVERY gateway in that pool — which in the default shared-pool
        mode is every co-resident deployment's. When a reference exists the secret is
        readable with a grant that scopes to one ARN, so the pool grant is pure surplus
        authority and the two must be mutually exclusive.

        Kept as an inversion rather than deleted: a deleted test cannot fail when someone
        re-adds the pool grant to fix a "legacy agent can't get a token" bug report.
        """
        pool_arn, secret_arn = client_secret_grant_targets(
            {"user_pool_id": "us-east-1_Zz9", "client_secret_ref": "ext/idp"}, REGION, ACCOUNT
        )
        assert secret_arn == f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:ext/idp-*"
        assert pool_arn is None, (
            "the secret reference must WIN over the pool id. Returning both hands the "
            f"role {pool_arn}, and Cognito cannot narrow that to the one client the "
            "agent owns — which is what would make per_agent mode isolating-except-for-"
            "the-credential."
        )


class TestPerAgentPolicyEmitsTheGrants:
    def test_a_cognito_gateway_gets_describe_on_exactly_one_pool(self):
        pool_arn = f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_AbC123"
        policy = per_agent_identity.build_scoped_runtime_policy(["gateway"], user_pool_arn=pool_arn)
        statement = _statement(policy, "GatewayClientSecretFromUserPool")
        assert statement["Action"] == ["cognito-idp:DescribeUserPoolClient"]
        assert statement["Resource"] == [pool_arn]
        assert statement["Effect"] == "Allow"

    def test_an_external_idp_gets_getsecretvalue_on_exactly_one_secret(self):
        secret_arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:okta/client-*"
        policy = per_agent_identity.build_scoped_runtime_policy(["gateway"], client_secret_arn=secret_arn)
        statement = _statement(policy, "ExternalIdpClientSecret")
        assert statement["Action"] == ["secretsmanager:GetSecretValue"]
        assert statement["Resource"] == [secret_arn]

    def test_neither_statement_appears_when_no_arn_is_supplied(self):
        """The whole point of the per-agent mode is that a role carries only what its
        agent needs. A grant that appears unconditionally would put every agent one
        step from reading the pool that authenticates every other agent."""
        policy = per_agent_identity.build_scoped_runtime_policy(["gateway", "memory"])
        sids = _sids(policy)
        assert "GatewayClientSecretFromUserPool" not in sids
        assert "ExternalIdpClientSecret" not in sids

    def test_the_grants_never_widen_to_a_wildcard_resource(self):
        """Unlike the gateway/memory/kb statements in this builder, these two have NO
        ``"*"`` fallback: if the ARN is unknown the correct answer is no statement.
        A wildcard here would be account-wide read access to every pool or secret."""
        policy = per_agent_identity.build_scoped_runtime_policy(
            ["gateway"],
            user_pool_arn=f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_AbC123",
            client_secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:okta/client-*",
        )
        for sid in ("GatewayClientSecretFromUserPool", "ExternalIdpClientSecret"):
            for resource in _statement(policy, sid)["Resource"]:
                assert resource != "*"
                assert not resource.endswith(":userpool/*")
                assert not resource.endswith(":secret:*")

    def test_the_policy_is_still_a_valid_document_with_the_grants_added(self):
        """Cheap guard on the thing that breaks a deploy loudly rather than quietly:
        IAM rejects a malformed document, and put_role_policy is the last step before
        the runtime is created."""
        policy = per_agent_identity.build_scoped_runtime_policy(
            ["gateway", "memory", "knowledge_base"],
            user_pool_arn=f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_AbC123",
            client_secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:okta/client-*",
        )
        assert policy["Version"] == "2012-10-17"
        for statement in policy["Statement"]:
            assert statement["Effect"] in ("Allow", "Deny")
            assert statement["Action"]
            assert statement["Resource"]


class TestLegacyRoleEmitsTheGrants:
    """The legacy per-deploy path in ``create_runtime_iam_role`` must agree with the
    per-agent path. It is still reachable on any stack deployed before
    ``SHARED_RUNTIME_ROLE_ARN`` existed, and a divergence there fails the same silent
    way."""

    def _policy_for(self, **kwargs) -> dict:
        import json
        from unittest.mock import MagicMock

        from app.services import runtime_deployer

        iam = MagicMock()
        iam.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
        iam.create_role.return_value = {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/AgentCoreRuntime-t"}}
        runtime_deployer.create_runtime_iam_role(
            iam_client=iam,
            role_name="AgentCoreRuntime-t",
            account_id=ACCOUNT,
            region=REGION,
            connected_tools=["gateway"],
            **kwargs,
        )
        documents = [json.loads(c.kwargs["PolicyDocument"]) for c in iam.put_role_policy.call_args_list]
        assert documents, "create_runtime_iam_role attached no inline policy"
        merged = {"Version": "2012-10-17", "Statement": []}
        for doc in documents:
            merged["Statement"].extend(doc["Statement"])
        return merged

    def test_the_two_grants_appear_when_the_arns_are_supplied(self):
        policy = self._policy_for(
            user_pool_arn=f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/us-east-1_AbC123",
            client_secret_arn=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:okta/client-*",
        )
        assert "GatewayClientSecretFromUserPool" in _sids(policy)
        assert "ExternalIdpClientSecret" in _sids(policy)

    def test_neither_grant_appears_by_default(self):
        policy = self._policy_for()
        assert "GatewayClientSecretFromUserPool" not in _sids(policy)
        assert "ExternalIdpClientSecret" not in _sids(policy)
