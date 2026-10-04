"""The standalone FastMCP path needs its own pre-warmed, model-free runtime role.

The ordinary shared runtime role deliberately carries model and AgentCore tool
permissions for Strands agents. Reusing it for a FastMCP server would make the new
runtime model-free only in source code, while its AWS authority remained model-capable.

The second role must also be visible to every create/teardown control path. Otherwise
one Lambda selects it while another records or deletes it as though it were a
deployment-owned role.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from stacks.platform.regional_artifact_bucket_grant import BUCKET_NAMESPACE_PREFIX
from stacks.platform_stack import PlatformStack

from tests.iam_attachment import statements_for_role

BUCKET_LEVEL_S3_ACTIONS = {"s3:ListBucket", "s3:GetBucketLocation"}
OBJECT_LEVEL_S3_ACTIONS = {"s3:GetObject", "s3:GetObjectVersion"}

REGION = "us-east-1"
ACCOUNT = "123456789012"
PROJECT = "agentcore-workflow"
ENVIRONMENT = "test"

MODEL_ROLE_NAME = f"AgentCoreRuntime-{PROJECT}-{ENVIRONMENT}-shared"
MCP_ROLE_NAME = f"AgentCoreRuntime-{PROJECT}-{ENVIRONMENT}-mcp-shared"
MODEL_ACTIONS = {
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
}
# The COMPLETE identity-policy action set a model-free FastMCP runtime needs to boot:
# read the staged ZIP (exact S3 read actions on the current + regional artifact
# buckets) and write its own CloudWatch logs. Asserting equality -- not just the
# absence of the four model verbs -- is what makes a future secret, tool-plane,
# model, or wildcard-action grant fail here even though the role's IAM5 findings are
# construct-suppressed in nag_suppressions.py. All actions are exact by design; a
# wildcard family such as s3:GetObject* would not be in this set and would fail.
EXPECTED_MCP_ACTIONS = {
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:GetObject",
    "s3:GetObjectVersion",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


@pytest.fixture(scope="module")
def template_json() -> dict:
    app = cdk.App()
    stack = PlatformStack(
        app,
        "TestStack",
        environment_name=ENVIRONMENT,
        project_name=PROJECT,
        env=cdk.Environment(region=REGION, account=ACCOUNT),
    )
    return Template.from_stack(stack).to_json()


def _role_logical_id(template_json: dict, role_name: str) -> str:
    matches = [
        logical_id
        for logical_id, resource in template_json["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role" and resource.get("Properties", {}).get("RoleName") == role_name
    ]
    assert len(matches) == 1, (
        f"expected exactly one AWS::IAM::Role named {role_name!r}, found {matches}. "
        "A missing or ambiguous shared role cannot be production evidence."
    )
    return matches[0]


def _role_actions(template_json: dict, logical_id: str) -> set[str]:
    actions: set[str] = set()
    for _source, statement in statements_for_role(template_json, logical_id):
        value = statement.get("Action") or []
        actions.update([value] if isinstance(value, str) else value)
    return actions


def _lambda_by_name(template_json: dict, function_name: str) -> dict:
    matches = [
        resource
        for resource in template_json["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource.get("Properties", {}).get("FunctionName") == function_name
    ]
    assert len(matches) == 1, f"expected exactly one Lambda named {function_name!r}, found {len(matches)}"
    return matches[0]


def test_the_stack_prewarms_a_distinct_shared_mcp_runtime_role(template_json):
    model_role = _role_logical_id(template_json, MODEL_ROLE_NAME)
    mcp_role = _role_logical_id(template_json, MCP_ROLE_NAME)

    assert mcp_role != model_role
    assert MCP_ROLE_NAME.endswith("-shared"), (
        "runtime teardown recognizes stack-owned roles by the shared suffix as a "
        "defence-in-depth guard; changing it would make one deployment able to target "
        "the role reused by every MCP runtime"
    )

    trust = template_json["Resources"][mcp_role]["Properties"]["AssumeRolePolicyDocument"]
    principals = [
        statement.get("Principal", {}).get("Service")
        for statement in trust.get("Statement", [])
        if statement.get("Effect") == "Allow"
    ]
    assert "bedrock-agentcore.amazonaws.com" in principals


def test_the_mcp_role_is_bootable_but_has_no_model_authority(template_json):
    model_role = _role_logical_id(template_json, MODEL_ROLE_NAME)
    mcp_role = _role_logical_id(template_json, MCP_ROLE_NAME)
    model_actions = _role_actions(template_json, model_role)
    mcp_actions = _role_actions(template_json, mcp_role)

    assert MODEL_ACTIONS.issubset(model_actions), (
        "the ordinary Strands role lost model access; model-free isolation must be a "
        "separate role, not a global permission removal"
    )
    assert not MODEL_ACTIONS.intersection(mcp_actions), (
        f"the standalone FastMCP role still has model authority: {sorted(MODEL_ACTIONS.intersection(mcp_actions))}"
    )
    assert any(action.startswith("s3:GetObject") for action in mcp_actions), (
        "AgentCore fetches the staged runtime ZIP as the execution role, so a role "
        "without artifact read authority produces a green control plane and a dead container"
    )
    assert {
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }.issubset(mcp_actions)
    # Exact-equality lock: the role must hold ONLY the model-free boot set. This is
    # the action-level counterpart to the resource-level IAM5 suppression -- the
    # suppression silences wildcard-resource findings, so drift protection has to
    # live here instead of in the Nag gate.
    assert mcp_actions == EXPECTED_MCP_ACTIONS, (
        "the standalone FastMCP role's action set drifted from the exact model-free "
        f"boot set. Unexpected extra: {sorted(mcp_actions - EXPECTED_MCP_ACTIONS)}; "
        f"missing: {sorted(EXPECTED_MCP_ACTIONS - mcp_actions)}. Any model, tool-plane, "
        "secret, or wildcard-action grant must fail here even though this role's IAM5 "
        "findings are construct-suppressed in nag_suppressions.py."
    )


@pytest.mark.parametrize(
    "function_name",
    [
        f"{PROJECT}-{ENVIRONMENT}-deployment",
        f"{PROJECT}-{ENVIRONMENT}-step-iam",
        f"{PROJECT}-{ENVIRONMENT}-step-runtime-configure",
        f"{PROJECT}-{ENVIRONMENT}-step-mcp-server",
        f"{PROJECT}-{ENVIRONMENT}-step-status-update",
    ],
)
def test_every_mcp_create_or_teardown_path_receives_the_same_role(
    template_json,
    function_name,
):
    model_role = _role_logical_id(template_json, MODEL_ROLE_NAME)
    mcp_role = _role_logical_id(template_json, MCP_ROLE_NAME)
    variables = (
        _lambda_by_name(template_json, function_name).get("Properties", {}).get("Environment", {}).get("Variables", {})
    )

    assert variables.get("SHARED_RUNTIME_ROLE_ARN") == {
        "Fn::GetAtt": [model_role, "Arn"],
    }
    assert variables.get("SHARED_MCP_RUNTIME_ROLE_ARN") == {
        "Fn::GetAtt": [mcp_role, "Arn"],
    }, (
        f"{function_name} cannot identify the platform-owned MCP role. Selection, "
        "manifest recording, and teardown must share one explicit authority contract."
    )


def _s3_statements_for(template_json: dict, logical_id: str) -> list[dict]:
    out: list[dict] = []
    for _source, statement in statements_for_role(template_json, logical_id):
        value = statement.get("Action") or []
        actions = [value] if isinstance(value, str) else value
        if any(action.startswith("s3:") for action in actions):
            out.append(statement)
    return out


def _s3_resource_reprs(statement: dict) -> list[str]:
    resource = statement.get("Resource")
    elements = resource if isinstance(resource, list) else [resource]
    return [el if isinstance(el, str) else json.dumps(el) for el in elements]


def test_the_mcp_role_s3_resources_are_scoped_by_shape_and_never_wildcard(template_json):
    """The action-equality lock above leaves the RESOURCE free to drift to '*'. A
    mutation audit found the model role's sibling test let both bucket-level statements
    survive a rewrite to Resource='*'; the model-free MCP role has the identical S3
    structure and needs the same shape pins. Every S3 resource must be a scoped ARN
    matching its action level, never the literal '*' or the account-wide
    arn:aws:s3:::*, and all four artifact-read shapes (current bucket Fn::GetAtt ARN + /*,
    regional namespace + /*) must be present."""
    mcp_role = _role_logical_id(template_json, MCP_ROLE_NAME)
    statements = _s3_statements_for(template_json, mcp_role)
    assert statements, "no S3 read statement on the shared MCP runtime role"

    saw = {"current_bucket": False, "current_object": False, "regional_bucket": False, "regional_object": False}
    for statement in statements:
        value = statement.get("Action") or []
        actions = set([value] if isinstance(value, str) else value)
        for repr_ in _s3_resource_reprs(statement):
            assert repr_ != "*", (
                f"an S3 statement {sorted(actions)} on the MCP role uses the literal '*' "
                "resource; a Resource='*' mutation on a scoped bucket ARN must fail here"
            )
            assert "arn:aws:s3:::*" not in repr_, (
                f"an S3 statement {sorted(actions)} on the MCP role uses the account-wide "
                f"bucket wildcard arn:aws:s3:::* ({repr_})"
            )
            is_object_resource = "/*" in repr_
            is_current_bucket = "Fn::GetAtt" in repr_
            is_regional = BUCKET_NAMESPACE_PREFIX in repr_

            if actions & OBJECT_LEVEL_S3_ACTIONS:
                assert is_object_resource, (
                    f"object-level actions {sorted(actions & OBJECT_LEVEL_S3_ACTIONS)} on a "
                    f"non-object resource: {repr_}"
                )
            if actions & BUCKET_LEVEL_S3_ACTIONS:
                assert not is_object_resource, (
                    f"bucket-level actions {sorted(actions & BUCKET_LEVEL_S3_ACTIONS)} on an "
                    f"object ('/*') resource: {repr_}"
                )

            if actions & BUCKET_LEVEL_S3_ACTIONS and is_current_bucket and not is_object_resource:
                saw["current_bucket"] = True
            if actions & OBJECT_LEVEL_S3_ACTIONS and is_current_bucket and is_object_resource:
                saw["current_object"] = True
            if actions & BUCKET_LEVEL_S3_ACTIONS and is_regional and not is_object_resource:
                saw["regional_bucket"] = True
            if actions & OBJECT_LEVEL_S3_ACTIONS and is_regional and is_object_resource:
                saw["regional_object"] = True

    missing = sorted(k for k, v in saw.items() if not v)
    assert not missing, f"expected all four artifact-read shapes on the MCP role, missing: {missing}"
