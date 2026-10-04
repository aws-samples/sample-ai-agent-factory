"""The documented target-account role must satisfy credential lifecycle calls.

Cross-account deployment runs the same bind/copy/delete code as the home-account
path. A sample policy that can create a secret but cannot describe its ownership
tags makes every reference-based deploy and every guarded teardown fail.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
from app.services.runtime_deployer import RUNTIME_LOG_GROUP_PREFIX


def _document() -> dict:
    repository = Path(__file__).resolve().parents[2]
    return json.loads((repository / "docs" / "cross-account-deploy-role.json").read_text())


def _deployment_role_policies() -> list[dict]:
    document = _document()
    return [value for key, value in document.items() if key.startswith("permissions-policy") and key.endswith(".json")]


def _deployment_role_actions() -> set[str]:
    actions: set[str] = set()
    for policy in _deployment_role_policies():
        for statement in policy["Statement"]:
            value = statement.get("Action") or []
            actions.update([value] if isinstance(value, str) else value)
    return actions


def _deployment_role_statements() -> list[dict]:
    return [statement for policy in _deployment_role_policies() for statement in policy["Statement"]]


@pytest.mark.parametrize(
    ("component", "required"),
    [
        (
            "runtime",
            {
                "bedrock-agentcore:CreateAgentRuntime",
                "bedrock-agentcore:GetAgentRuntime",
                "bedrock-agentcore:UpdateAgentRuntime",
                "bedrock-agentcore:DeleteAgentRuntime",
                "bedrock-agentcore:ListAgentRuntimes",
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                "bedrock-agentcore:GetAgentRuntimeEndpoint",
                "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                "bedrock-agentcore:ListAgentRuntimeEndpoints",
            },
        ),
        (
            "gateway and connectors",
            {
                "bedrock-agentcore:CreateGateway",
                "bedrock-agentcore:GetGateway",
                "bedrock-agentcore:UpdateGateway",
                "bedrock-agentcore:DeleteGateway",
                "bedrock-agentcore:ListGateways",
                "bedrock-agentcore:CreateGatewayTarget",
                "bedrock-agentcore:GetGatewayTarget",
                "bedrock-agentcore:UpdateGatewayTarget",
                "bedrock-agentcore:DeleteGatewayTarget",
                "bedrock-agentcore:ListGatewayTargets",
                "bedrock-agentcore:SynchronizeGatewayTargets",
                "bedrock-agentcore:InvokeGateway",
                "bedrock-agentcore:CreateOauth2CredentialProvider",
                "bedrock-agentcore:GetOauth2CredentialProvider",
                "bedrock-agentcore:UpdateOauth2CredentialProvider",
                "bedrock-agentcore:DeleteOauth2CredentialProvider",
                "bedrock-agentcore:ListOauth2CredentialProviders",
                "bedrock-agentcore:CreateApiKeyCredentialProvider",
                "bedrock-agentcore:GetApiKeyCredentialProvider",
                "bedrock-agentcore:UpdateApiKeyCredentialProvider",
                "bedrock-agentcore:DeleteApiKeyCredentialProvider",
                "bedrock-agentcore:ListApiKeyCredentialProviders",
                "bedrock-agentcore:CreateTokenVault",
                "bedrock-agentcore:GetTokenVault",
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:GetResourceApiKey",
            },
        ),
        (
            "harness",
            {
                "bedrock-agentcore:CreateHarness",
                "bedrock-agentcore:GetHarness",
                "bedrock-agentcore:UpdateHarness",
                "bedrock-agentcore:DeleteHarness",
                "bedrock-agentcore:ListHarnesses",
                "bedrock-agentcore:InvokeHarness",
                "bedrock-agentcore:CreateMemory",
                "bedrock-agentcore:UpdateMemory",
                "bedrock-agentcore:DeleteMemory",
                "bedrock-agentcore:CreateWorkloadIdentity",
                "bedrock-agentcore:DeleteWorkloadIdentity",
            },
        ),
        (
            "policy",
            {
                "bedrock-agentcore:CreatePolicyEngine",
                "bedrock-agentcore:GetPolicyEngine",
                "bedrock-agentcore:DeletePolicyEngine",
                "bedrock-agentcore:ListPolicyEngines",
                "bedrock-agentcore:CreatePolicy",
                "bedrock-agentcore:GetPolicy",
                "bedrock-agentcore:UpdatePolicy",
                "bedrock-agentcore:DeletePolicy",
                "bedrock-agentcore:ListPolicies",
                "bedrock-agentcore:ManageAdminPolicy",
                "bedrock-agentcore:ManageResourceScopedPolicy",
            },
        ),
        (
            "evaluation",
            {
                "bedrock-agentcore:Evaluate",
                "bedrock-agentcore:CreateOnlineEvaluationConfig",
                "bedrock-agentcore:GetOnlineEvaluationConfig",
                "bedrock-agentcore:UpdateOnlineEvaluationConfig",
                "bedrock-agentcore:DeleteOnlineEvaluationConfig",
                "bedrock-agentcore:ListOnlineEvaluationConfigs",
                "logs:DescribeIndexPolicies",
                "logs:PutIndexPolicy",
                "xray:GetIndexingRules",
                "application-signals:Get*",
            },
        ),
        (
            "knowledge base",
            {
                "bedrock:CreateKnowledgeBase",
                "bedrock:GetKnowledgeBase",
                "bedrock:DeleteKnowledgeBase",
                "bedrock:CreateDataSource",
                "bedrock:GetDataSource",
                "bedrock:DeleteDataSource",
                "bedrock:StartIngestionJob",
                "bedrock:GetIngestionJob",
                "bedrock:ListIngestionJobs",
                "s3vectors:CreateVectorBucket",
                "s3vectors:DeleteVectorBucket",
                "s3vectors:CreateIndex",
                "s3vectors:DeleteIndex",
                "aoss:CreateCollection",
                "aoss:DeleteCollection",
                "aoss:CreateAccessPolicy",
                "aoss:DeleteAccessPolicy",
            },
        ),
        (
            "guardrails",
            {
                "bedrock:CreateGuardrail",
                "bedrock:GetGuardrail",
                "bedrock:UpdateGuardrail",
                "bedrock:DeleteGuardrail",
                "bedrock:ListGuardrails",
                "bedrock:CreateGuardrailVersion",
            },
        ),
        (
            "tool and MCP provisioning",
            {
                "lambda:CreateFunction",
                "lambda:GetFunction",
                "lambda:UpdateFunctionCode",
                "lambda:UpdateFunctionConfiguration",
                "lambda:DeleteFunction",
                "lambda:AddPermission",
                "lambda:GetPolicy",
                "lambda:RemovePermission",
                "lambda:ListTags",
                "lambda:TagResource",
                "cognito-idp:CreateUserPool",
                "cognito-idp:DescribeUserPool",
                "cognito-idp:DeleteUserPool",
                "cognito-idp:CreateUserPoolClient",
                "cognito-idp:DescribeUserPoolClient",
                "cognito-idp:DeleteUserPoolClient",
                "cognito-idp:CreateResourceServer",
                "cognito-idp:DeleteResourceServer",
            },
        ),
        (
            "dashboard",
            {
                "cloudwatch:PutDashboard",
                "cloudwatch:GetDashboard",
                "cloudwatch:DeleteDashboards",
            },
        ),
    ],
)
def test_target_role_covers_each_cross_account_component(component, required):
    actions = _deployment_role_actions()
    assert required <= actions, f"target role is missing {component} actions: {sorted(required - actions)}"


def test_target_role_can_bind_copy_record_and_delete_deployment_credentials():
    actions = _deployment_role_actions()
    required = {
        "secretsmanager:CreateSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:TagResource",
        "secretsmanager:DeleteSecret",
    }
    assert required <= actions, f"target role is missing credential lifecycle actions: {sorted(required - actions)}"


@pytest.mark.parametrize(
    ("action", "operation"),
    [
        # The shared-pool reuse proof. Its caller swallows a denial and answers "no
        # scope", so without the grant every same-name redeploy fails with the create's
        # AlreadyExists and nothing in the error names IAM.
        ("cognito-idp:DescribeResourceServer", "describe_resource_server"),
        # The co-residency check before a resource server is deleted; fails closed.
        ("cognito-idp:ListUserPoolClients", "list_user_pool_clients"),
        # Teardown's tag discovery for a secret no manifest row names.
        ("secretsmanager:ListSecrets", "list_secrets"),
    ],
)
def test_target_role_can_make_the_reads_the_gateway_and_teardown_make(action, operation):
    """The assumed target role runs the same gateway_deployer code, so each operation
    it calls must be in the document. The operation is checked as well, so a rename
    in the code shows up here instead of silently leaving this test vacuous."""
    source = (Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "gateway_deployer.py").read_text()
    assert operation in source, f"gateway_deployer no longer calls {operation}; re-derive this contract"
    assert action in _deployment_role_actions(), f"target role is missing {action}"


def test_target_role_scopes_runtime_log_retention_without_tag_authority():
    statements = _deployment_role_statements()
    governance = next(statement for statement in statements if statement.get("Sid") == "RuntimeLogGroupGovernance")
    assert set(governance["Action"]) == {
        "logs:CreateLogGroup",
        "logs:PutRetentionPolicy",
    }
    assert governance["Resource"] == (
        f"arn:aws:logs:<REGION>:<TARGET_ACCOUNT_ID>:log-group:{RUNTIME_LOG_GROUP_PREFIX}*"
    )
    assert "logs:TagResource" not in _deployment_role_actions()


def test_target_role_scopes_tool_lambda_log_retention_to_the_functions_it_creates():
    """The assumed role runs the same gateway_deployer code, and the governance is fail-closed:
    without this statement every cross-account gateway deploy that makes a tool function fails
    at PutRetentionPolicy. Its reach is the function grant's, under /aws/lambda/."""
    source = (Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "gateway_deployer.py").read_text()
    assert "logs_client.put_retention_policy(" in source, "re-derive this contract: the governance call moved"
    statements = _deployment_role_statements()
    governance = next(statement for statement in statements if statement.get("Sid") == "ToolLambdaLogGroupGovernance")
    functions = next(statement for statement in statements if statement.get("Sid") == "ManagedLambdaLifecycle")
    assert set(governance["Action"]) == {"logs:CreateLogGroup", "logs:PutRetentionPolicy"}
    assert functions["Resource"] == "arn:aws:lambda:<REGION>:<TARGET_ACCOUNT_ID>:function:AgentCore*"
    assert governance["Resource"] == "arn:aws:logs:<REGION>:<TARGET_ACCOUNT_ID>:log-group:/aws/lambda/AgentCore*"


def test_target_role_can_tag_the_runtime_it_creates():
    """Otherwise every cross-account runtime deploy fails at CreateAgentRuntime.

    ``tags`` on a create call is authorized as ``TagResource`` on the resource being
    created, never as part of the create action.  The home-account equivalent of this
    gap was an outage, observed live: ``AccessDeniedException ... not authorized to
    perform: bedrock-agentcore:TagResource on resource: .../runtime/*`` raised from
    ``CreateAgentRuntime`` with ``runtime_id`` still null.  The target role grants
    ``CreateAgentRuntime``, and ``create_agent_runtime`` is target-aware, so the same
    call fails the same way here.
    """
    assert "bedrock-agentcore:TagResource" in _deployment_role_actions(), (
        "the documented target role cannot tag a runtime, so CreateAgentRuntime's tags= "
        "argument is denied and no cross-account deployment can create a runtime"
    )


def test_the_runtime_tag_grant_is_conditioned_and_not_folded_into_the_control_plane():
    """Both ownership VALUES are pinned, which is less than it might sound like.

    ``runtime/*`` is unavoidable (the id does not exist when the role is written), so
    the request tags carry what safety there is.  Pinning the key list and ManagedBy
    alone leaves ``AgentCoreStack`` -- the value teardown matches on -- caller-chosen,
    which would authorize forging or stripping another deployment's ownership on any
    runtime in the target account.  Pinning both values bounds this statement to
    writing *this platform's own* pair.

    It does NOT prevent stamping that pair onto a runtime the platform did not create:
    ``bedrock-agentcore:TagResource`` has no create-only condition key, so a dependent
    tag-on-create is indistinguishable in policy from a standalone retag.  That is
    stated in the document's notes and is why the Sid is not called ``...OnCreate``.
    """
    statements = _deployment_role_statements()
    tagging = [statement for statement in statements if "bedrock-agentcore:TagResource" in statement.get("Action", [])]
    assert len(tagging) == 1, (
        "expected exactly one statement to grant bedrock-agentcore:TagResource; found "
        f"{len(tagging)}. Folding it into a broader statement drops the conditions that "
        "bound it to this platform's own ownership pair"
    )
    statement = tagging[0]
    assert statement["Action"] == ["bedrock-agentcore:TagResource"], (
        "the tagging statement carries other actions, which means its conditions now "
        f"restrict them too: {statement['Action']}"
    )
    prefix = "arn:aws:bedrock-agentcore:<REGION>:<TARGET_ACCOUNT_ID>"
    assert sorted(statement["Resource"]) == sorted(
        [
            f"{prefix}:runtime/*",
            f"{prefix}:workload-identity-directory/default",
            f"{prefix}:workload-identity-directory/default/workload-identity/*",
        ]
    ), (
        "the tagging statement must name the workload-identity directory AND its child "
        "alongside runtime/*. create_agent_runtime does not only tag the runtime -- it "
        "mints and tags a workload identity in the same call, and AgentCore authorizes "
        "that against both the container ARN and the per-identity child, returning a 403 "
        "naming whichever it reached first. With runtime/* alone the deploy is denied at "
        "the workload-identity tag, not at the runtime tag, so this statement's own test "
        f"passing proves nothing about the create succeeding. Found: {statement['Resource']}"
    )
    condition = statement["Condition"]
    assert condition["StringEquals"]["aws:RequestTag/ManagedBy"] == "agentcore-flows"
    assert condition["StringEquals"]["aws:RequestTag/AgentCoreStack"] == "<PLATFORM_PROJECT>-<PLATFORM_ENV>-<REGION>", (
        "aws:RequestTag/AgentCoreStack is not pinned to the documented value shape: "
        f"{condition['StringEquals'].get('aws:RequestTag/AgentCoreStack')!r}. Unpinned, the "
        "statement authorizes relabelling any runtime in the target account under an "
        "arbitrary owner; pinned to a literal region it denies every deploy to a "
        "non-home allowlisted region"
    )
    assert sorted(condition["ForAllValues:StringEquals"]["aws:TagKeys"]) == [
        "AgentCoreStack",
        "ManagedBy",
    ]
    assert statement["Sid"] != "AgentCoreRuntimeTagOnCreate", (
        "the Sid claims the grant is confined to tag-on-create. TagResource has no "
        "create-only condition key, so it is not -- a reviewer who reads only the Sid "
        "would be misled about the residual risk"
    )


def test_target_role_can_read_the_ownership_tags_it_writes():
    """The read half. Without it teardown refuses and the resource keeps billing.

    ``TagResource`` above lets the platform stamp ownership; this lets it check ownership
    back. ``deployment_handler`` runs its whole delete cascade against the assumed target
    session and calls ``assert_agentcore_resource_owned`` on every resource it is about to
    delete (:2365, :2389, :2410, :2430, :2551, :3706, :3733, :3806) plus
    ``delete_owned_credential_provider`` (:2484, :4171). Each of those ends in
    ``list_tags_for_resource``. Denied, ``resource_ownership`` converts the
    ``AccessDeniedException`` into ``ResourceDeletionRefused`` -- the delete is refused,
    correctly, because an unreadable owner is not proof of ownership, and the resource is
    left running in the customer's account.

    A write grant with no matching read is the shape of this whole defect class, so this
    asserts the pairing rather than just the action's presence.
    """
    actions = _deployment_role_actions()
    assert "bedrock-agentcore:ListTagsForResource" in actions, (
        "the documented target role can TAG AgentCore resources but cannot READ those "
        "tags back, so every ownership check in the delete cascade fails closed and every "
        "cross-account teardown leaves its resources running and billing"
    )
    assert "bedrock-agentcore:UntagResource" not in actions, (
        "UntagResource has no caller anywhere in backend/src/app -- teardown deletes "
        "resources, it does not un-tag them. Add it with its first caller or not at all"
    )


def test_the_ownership_read_covers_both_credential_provider_namespaces():
    """Both, or teardown deletes half the providers and then fails.

    ``delete_owned_credential_provider`` probes the OAuth and API-key namespaces in that
    order, for back-compat with older manifests that recorded an API-key provider as
    OAuth. It only ``continue``s when the error means the resource is MISSING; an
    ``AccessDeniedException`` re-raises. Because the OAuth provider is deleted BEFORE the
    API-key probe runs, a role granted the read for one namespace only does not fail
    closed -- it partially tears down and then errors, which is strictly worse.

    Nested shapes also need the container ARN, for the same double-authorization reason as
    the tagging statement: both providers live under ``token-vault/default``.
    """
    read = [
        st for st in _deployment_role_statements() if "bedrock-agentcore:ListTagsForResource" in st.get("Action", [])
    ]
    assert len(read) == 1, f"expected exactly one ownership-read statement, found {len(read)}"
    statement = read[0]
    resources = statement["Resource"]
    resources = [resources] if isinstance(resources, str) else resources
    prefix = "arn:aws:bedrock-agentcore:<REGION>:<TARGET_ACCOUNT_ID>"
    for tail in (
        "token-vault/default",
        "token-vault/default/oauth2credentialprovider/*",
        "token-vault/default/apikeycredentialprovider/*",
    ):
        assert f"{prefix}:{tail}" in resources, (
            f"the ownership read does not cover {tail}. delete_owned_credential_provider "
            "reads BOTH provider namespaces on one call and deletes the OAuth one first, "
            f"so omitting this leaves a half-torn-down deployment. Found: {resources}"
        )
    for tail in ("runtime/*", "gateway/*", "memory/*", "policy-engine/*", "harness/*"):
        assert f"{prefix}:{tail}" in resources, f"ownership read missing {tail}: {resources}"
    assert "*" not in resources, (
        "the ownership read is granted on a resource wildcard. ARCC cnt_L4ZLZgjrCctfxl: "
        "enumerate the resources; never widen a grant to make an authorization check pass"
    )


def test_the_ownership_read_is_unconditioned_because_a_read_sends_no_tags():
    """The copy-paste trap, and it fails CLOSED into the very bug being fixed.

    The tagging statement immediately above this one is heavily conditioned on
    ``aws:RequestTag``. A ``ListTagsForResource`` call sends no tags at all, so carrying
    those conditions down would deny every read. An ``aws:ResourceTag`` condition was also
    rejected: it yields the right outcome but replaces the diagnosable refusal with
    ``live ownership could not be read (AccessDeniedException)``, which is
    indistinguishable from this defect.
    """
    read = [
        st for st in _deployment_role_statements() if "bedrock-agentcore:ListTagsForResource" in st.get("Action", [])
    ][0]
    condition = read.get("Condition") or {}
    keys = {key for block in condition.values() if isinstance(block, dict) for key in block}
    assert not any(key.startswith("aws:RequestTag") for key in keys), (
        f"the ownership read is conditioned on a request tag: {condition}. A read sends no "
        "tags, so this denies every ownership check and fails closed into the outage"
    )
    assert "aws:TagKeys" not in keys, condition


def test_the_documented_owner_tag_value_is_the_one_the_code_will_send(monkeypatch):
    """Drift guard on the VALUE, not just the keys.

    A pinned value that does not match what ``owner_tags`` emits is not a hardening
    gap, it is an outage: ``StringEquals`` fails, ``TagResource`` is denied, and
    ``CreateAgentRuntime`` fails for every cross-account deploy.  So the document's
    template is substituted here with concrete inputs and compared against what
    ``stack_id`` actually builds from the same inputs, rather than being eyeballed.

    ``<REGION>`` is the registered onboarding region, which is exact here: an account
    target carries exactly one region and a deploy requesting another is rejected
    (``deploy_target.resolve_registered_account_target``).  It is deliberately a
    literal rather than ``${aws:RequestedRegion}`` -- ``${...}`` is Terraform
    interpolation syntax, and this document is consumed by Terraform estates.
    """
    from app.services import resource_ownership

    project, environment, region = "acmeplat", "prod7", "eu-central-1"
    documented = next(
        statement
        for statement in _deployment_role_statements()
        if "bedrock-agentcore:TagResource" in statement.get("Action", [])
    )["Condition"]["StringEquals"]["aws:RequestTag/AgentCoreStack"]
    assert "${" not in documented, (
        f"the documented value {documented!r} contains ${{...}}, which Terraform will try to "
        "interpolate unless escaped as $${...}; the target role is single-region by "
        "construction, so a literal <REGION> is both exact and safe to paste"
    )
    substituted = (
        documented.replace("<PLATFORM_PROJECT>", project)
        .replace("<PLATFORM_ENV>", environment)
        .replace("<REGION>", region)
    )
    assert "<" not in substituted, (
        f"the documented value {documented!r} still holds an unsubstituted placeholder after "
        "replacing the ones the notes tell an operator to replace -- an operator following "
        "those instructions would deploy a policy that denies every runtime create"
    )

    monkeypatch.setenv("PROJECT_NAME", project)
    monkeypatch.setenv("ENVIRONMENT", environment)
    actual = resource_ownership.stack_id(region)

    assert substituted == actual, (
        f"the documented owner-tag value resolves to {substituted!r} but resource_ownership "
        f"sends {actual!r}. Every cross-account CreateAgentRuntime would be denied"
    )


def test_the_documented_tag_keys_still_match_the_tags_the_code_sends(monkeypatch):
    """Drift guard: the allow-list is a tripwire only while it matches the caller.

    ``create_agent_runtime`` sends ``owner_tags(region)`` with no ``extra``, so exactly
    the two ownership keys reach the API.  If a third key is ever added at that call
    site, ``ForAllValues:StringEquals`` denies the whole request -- a live outage, not
    an untagged resource.  Reading the keys from the source of truth means this test
    fails when the code changes, rather than when someone remembers to update it.
    """
    from app.services.resource_ownership import owner_tags

    statements = _deployment_role_statements()
    tagging = next(
        statement for statement in statements if "bedrock-agentcore:TagResource" in statement.get("Action", [])
    )
    documented = set(tagging["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"])

    # The keys owner_tags RETURNS, not the two constants it is built from. Comparing
    # constant to constant passes even when owner_tags grows a third key, which is the
    # exact drift this test exists to catch.
    monkeypatch.setenv("PROJECT_NAME", "acmeplat")
    monkeypatch.setenv("ENVIRONMENT", "prod7")
    emitted = set(owner_tags("eu-central-1"))

    assert documented == emitted, (
        f"the documented tag-key allow-list {sorted(documented)} no longer matches the keys "
        f"owner_tags() actually returns ({sorted(emitted)}). A key the policy does not list "
        "makes CreateAgentRuntime fail outright for every cross-account deploy; widen the "
        "policy in the same change as the caller"
    )


def test_create_agent_runtime_sends_exactly_the_documented_tag_keys(monkeypatch):
    """The call shape, captured by running it -- not inferred from reading it.

    The test above compares the policy against ``owner_tags()``.  That is still blind
    to the other half of the drift: ``create_agent_runtime`` passing ``extra=`` and
    injecting a governance tag the policy never lists.  ``ForAllValues:StringEquals``
    denies the whole request in that case, so the symptom is a total deploy outage, not
    a mis-tagged resource.  So this drives the real function with a stub client and
    asserts on the ``tags`` that actually reach the API.
    """
    from app.services import runtime_deployer

    captured: dict = {}

    class _Stub:
        def create_agent_runtime(self, **params):
            captured.update(params)
            return {"agentRuntimeId": "probe-id", "agentRuntimeArn": "arn:probe", "status": "CREATING"}

    monkeypatch.setenv("PROJECT_NAME", "acmeplat")
    monkeypatch.setenv("ENVIRONMENT", "prod7")
    runtime_deployer.create_agent_runtime(
        _Stub(),
        runtime_name="probe",
        role_arn="arn:aws:iam::123456789012:role/AgentCoreRuntime-probe",
        s3_bucket="probe-bucket",
        s3_key="probe/key.zip",
        region="eu-central-1",
    )

    documented = set(
        next(
            statement
            for statement in _deployment_role_statements()
            if "bedrock-agentcore:TagResource" in statement.get("Action", [])
        )["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"]
    )
    sent = captured.get("tags")
    assert sent is not None, (
        "create_agent_runtime sent no tags at all. An untagged runtime is unattributable to "
        "teardown -- if this is now deliberate, the policy statement and its notes are dead "
        "weight and must be removed in the same change"
    )
    assert set(sent) == documented, (
        f"create_agent_runtime sends tag keys {sorted(sent)} but the documented policy allows "
        f"{sorted(documented)}. ForAllValues:StringEquals denies the ENTIRE request on any "
        "unlisted key, so this is an outage for every runtime-creating deploy, in both the "
        "home and cross-account paths"
    )
    assert sent["AgentCoreStack"] == "acmeplat-prod7-eu-central-1", (
        f"the owner tag value sent is {sent['AgentCoreStack']!r}: it must be "
        "{project}-{env}-{deploy region}, which is what both pinned policies expect"
    )


def test_target_role_separates_long_lived_sources_from_deployment_lifecycle():
    statements = _deployment_role_statements()
    source = next(statement for statement in statements if statement.get("Sid") == "DeploymentCredentialSources")
    assert set(source["Action"]) == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    }
    assert any("secret:agentcore-provider/" in arn for arn in source["Resource"])
    assert any("secret:agentcore-otel/" in arn for arn in source["Resource"])

    destructive = {
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource",
        "secretsmanager:DeleteSecret",
    }
    for statement in statements:
        actions = set(statement.get("Action") or [])
        if not actions & destructive:
            continue
        resources = statement.get("Resource") or []
        resources = [resources] if isinstance(resources, str) else resources
        assert not any(
            "secret:agentcore-provider/" in arn or "secret:agentcore-otel/" in arn or "secret:agentcore-*" in arn
            for arn in resources
            if isinstance(arn, str)
        ), f"long-lived source secret is included in destructive lifecycle statement: {statement}"


def test_target_role_can_read_every_kb_customer_authorization_tag():
    actions = _deployment_role_actions()
    assert {
        "bedrock:ListTagsForResource",
        "s3:GetBucketTagging",
        "s3vectors:ListTagsForResource",
        "aoss:ListTagsForResource",
        "rds:ListTagsForResource",
        "lambda:ListTags",
        "kms:ListResourceTags",
        "secretsmanager:DescribeSecret",
    } <= actions


def test_kb_source_secret_read_is_opt_in_and_read_only():
    source = next(
        statement
        for statement in _deployment_role_statements()
        if statement.get("Sid") == "KnowledgeBaseCredentialSources"
    )
    assert set(source["Action"]) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert source["Resource"] == ("arn:aws:secretsmanager:*:<TARGET_ACCOUNT_ID>:secret:*")
    assert source["Condition"] == {
        "StringEquals": {
            "aws:ResourceTag/AgentCoreFlowsAccess": "allow",
        }
    }
    assert not {
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:TagResource",
        "secretsmanager:DeleteSecret",
    } & set(source["Action"])


def test_target_runtime_role_reads_only_deployment_bound_secret_copies():
    statements = _document()["_STEP_2_runtime-role-permissions.json"]["Statement"]
    grants = [
        resource
        for statement in statements
        if "secretsmanager:GetSecretValue"
        in ([statement.get("Action")] if isinstance(statement.get("Action"), str) else statement.get("Action", []))
        for resource in (
            [statement.get("Resource")] if isinstance(statement.get("Resource"), str) else statement.get("Resource", [])
        )
    ]
    assert any("secret:agentcore-connector/" in resource for resource in grants)
    assert not any("secret:agentcore-provider/" in resource for resource in grants)
    assert not any("secret:agentcore-otel/" in resource for resource in grants)


def test_target_role_can_enumerate_attached_policies_before_deleting_a_role():
    actions = _deployment_role_actions()
    assert "iam:ListAttachedRolePolicies" in actions
    assert "iam:UpdateAssumeRolePolicy" in actions


def test_target_role_can_read_and_pass_both_stable_execution_roles():
    account = "123456789012"
    required = [
        f"arn:aws:iam::{account}:role/AgentCoreFlowsRuntimeRole",
        f"arn:aws:iam::{account}:role/AgentCoreFlowsHarnessRole",
    ]
    statements = _deployment_role_statements()
    get_role_resources = [
        resource
        for statement in statements
        if "iam:GetRole" in statement.get("Action", [])
        for resource in (
            [statement.get("Resource")] if isinstance(statement.get("Resource"), str) else statement.get("Resource", [])
        )
    ]
    pass_role_resources = [
        resource
        for statement in statements
        if "iam:PassRole" in statement.get("Action", [])
        for resource in (
            [statement.get("Resource")] if isinstance(statement.get("Resource"), str) else statement.get("Resource", [])
        )
    ]

    for arn in required:
        templated = arn.replace(account, "<TARGET_ACCOUNT_ID>")
        assert any(fnmatchcase(templated, pattern) for pattern in get_role_resources)
        assert any(fnmatchcase(templated, pattern) for pattern in pass_role_resources)


def test_custom_stable_execution_role_placeholders_are_readable_and_passable():
    statements = _deployment_role_statements()
    for placeholder in (
        "<TARGET_RUNTIME_ROLE_ARN>",
        "<TARGET_HARNESS_ROLE_ARN>",
    ):
        assert any(
            "iam:GetRole" in statement.get("Action", [])
            and placeholder
            in (
                [statement.get("Resource")]
                if isinstance(statement.get("Resource"), str)
                else statement.get("Resource", [])
            )
            for statement in statements
        )
        assert any(
            "iam:PassRole" in statement.get("Action", [])
            and placeholder
            in (
                [statement.get("Resource")]
                if isinstance(statement.get("Resource"), str)
                else statement.get("Resource", [])
            )
            for statement in statements
        )


def test_target_artifact_bucket_is_validatable_and_mutable_by_deployment_role():
    statements = _deployment_role_statements()
    object_statement = next(statement for statement in statements if statement.get("Sid") == "TargetArtifactBucket")
    bucket_statement = next(statement for statement in statements if statement.get("Sid") == "TargetArtifactBucketList")
    assert object_statement["Resource"] == "arn:aws:s3:::<TARGET_ARTIFACT_BUCKET>/*"
    assert {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    } <= set(object_statement["Action"])
    assert bucket_statement["Resource"] == "arn:aws:s3:::<TARGET_ARTIFACT_BUCKET>"
    assert {"s3:ListBucket", "s3:GetBucketLocation"} <= set(bucket_statement["Action"])


def test_target_runtime_role_reads_target_bucket_and_covers_runtime_tools():
    policy = _document()["_STEP_2_runtime-role-permissions.json"]
    statements = policy["Statement"]
    artifact = next(statement for statement in statements if statement.get("Sid") == "RuntimeArtifactsRead")
    assert artifact["Resource"] == "arn:aws:s3:::<TARGET_ARTIFACT_BUCKET>/*"

    actions = {action for statement in statements for action in statement.get("Action", [])}
    assert {
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock:ApplyGuardrail",
        "bedrock:Retrieve",
    } <= actions


def test_every_documented_policy_fragment_is_copyable_and_size_bounded():
    allowed_policy_keys = {"Version", "Statement"}
    allowed_statement_keys = {
        "Sid",
        "Effect",
        "Principal",
        "Action",
        "Resource",
        "Condition",
        "NotAction",
        "NotResource",
    }
    document = _document()
    policies = {
        key: value
        for key, value in document.items()
        if isinstance(value, dict) and "Version" in value and "Statement" in value
    }
    for name, policy in policies.items():
        assert set(policy) <= allowed_policy_keys, f"{name} contains documentation-only keys inside the copyable policy"
        for statement in policy["Statement"]:
            assert set(statement) <= allowed_statement_keys, (
                f"{name} has invalid IAM statement keys: {sorted(set(statement) - allowed_statement_keys)}"
            )
        compact = json.dumps(policy, separators=(",", ":"))
        assert len(compact) <= 6144, (
            f"{name} is {len(compact)} characters; split it before it exceeds the IAM managed-policy document limit"
        )


def test_target_harness_role_is_documented_with_agentcore_trust_and_data_plane():
    document = _document()
    trust = document["_STEP_3_harness-role-trust.json"]
    principal = trust["Statement"][0]["Principal"]["Service"]
    assert principal == "bedrock-agentcore.amazonaws.com"

    actions = {
        action
        for statement in document["_STEP_3_harness-role-permissions.json"]["Statement"]
        for action in statement["Action"]
    }
    assert {
        "bedrock:InvokeModel",
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:InvokeGateway",
        "bedrock-agentcore:GetResourceOauth2Token",
        "secretsmanager:GetSecretValue",
    } <= actions
