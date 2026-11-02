"""Agent execution roles cannot mutate the Gateway/Cedar control plane."""

from __future__ import annotations

import copy

import boto3
import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import CfnTemplateGenerator
from app.services.harness_deployer import create_harness_iam_role
from app.services.per_agent_identity import build_scoped_runtime_policy
from app.services.runtime_deployer import create_runtime_iam_role
from app.services.runtime_policy_safety import (
    FORBIDDEN_RUNTIME_CONTROL_ACTIONS,
    RuntimePolicyEscalation,
    assert_runtime_policy_is_data_plane_only,
)

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402


def _actions(statement: dict) -> set[str]:
    raw = statement.get("Action") or []
    return {raw} if isinstance(raw, str) else set(raw)


def _assert_boundary(policy: dict) -> None:
    denies = [
        statement
        for statement in policy["Statement"]
        if statement.get("Sid") == "NeverMutateGatewayOrPolicyControlPlane"
    ]
    assert len(denies) == 1
    assert denies[0]["Effect"] == "Deny"
    assert _actions(denies[0]) == FORBIDDEN_RUNTIME_CONTROL_ACTIONS
    assert not any(
        action in FORBIDDEN_RUNTIME_CONTROL_ACTIONS
        for statement in policy["Statement"]
        if statement.get("Effect") == "Allow"
        for action in _actions(statement)
    )
    assert_runtime_policy_is_data_plane_only(policy)


def test_per_agent_policy_node_grants_no_control_plane_authority():
    policy = build_scoped_runtime_policy(["gateway", "policy"])

    _assert_boundary(policy)
    assert "GatewayAccess" in {statement["Sid"] for statement in policy["Statement"]}
    assert "PolicyAccess" not in {statement["Sid"] for statement in policy["Statement"]}


def test_mutation_wildcard_agentcore_allow_is_rejected():
    policy = build_scoped_runtime_policy(["gateway"])
    mutant = copy.deepcopy(policy)
    mutant["Statement"].append(
        {
            "Sid": "MutantControlPlaneAccess",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:*",
            "Resource": "*",
        }
    )

    with pytest.raises(RuntimePolicyEscalation, match="UpdateGateway"):
        assert_runtime_policy_is_data_plane_only(mutant)


def test_cloudformation_export_runtime_role_has_same_boundary():
    bundle = CfnTemplateGenerator().generate(
        DeployRequest(
            nodeId="node-1",
            config=RuntimeConfig(
                name="policy-boundary",
                model={"modelId": "us.anthropic.claude-sonnet-5"},
            ),
        )
    )
    template = yaml.safe_load(bundle.template_yaml)
    policy = template["Resources"]["RuntimeExecutionRole"]["Properties"]["Policies"][0]["PolicyDocument"]

    _assert_boundary(policy)


@mock_aws
def test_dynamic_runtime_role_has_explicit_deny_and_no_policy_mutation(monkeypatch):
    iam = boto3.client("iam", region_name="us-east-1")
    monkeypatch.setattr("app.services.runtime_deployer.time.sleep", lambda _seconds: None)
    create_runtime_iam_role(
        iam,
        "AgentCoreRuntime-policy-boundary",
        "123456789012",
        "us-east-1",
        connected_tools=["gateway", "policy"],
    )

    policy = iam.get_role_policy(
        RoleName="AgentCoreRuntime-policy-boundary",
        PolicyName="AgentCoreRuntimePolicy",
    )["PolicyDocument"]
    _assert_boundary(policy)


@mock_aws
def test_harness_role_has_explicit_deny_and_keeps_gateway_data_plane():
    iam = boto3.client("iam", region_name="us-east-1")
    gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-1"
    create_harness_iam_role(
        iam,
        "AgentCoreHarness-policy-boundary",
        harness_name="orders",
        model_id="us.anthropic.claude-sonnet-5",
        gateway_arn=gateway_arn,
        region="us-east-1",
    )

    policy = iam.get_role_policy(
        RoleName="AgentCoreHarness-policy-boundary",
        PolicyName="HarnessExecutionPolicy",
    )["PolicyDocument"]
    _assert_boundary(policy)
    allowed = {
        action
        for statement in policy["Statement"]
        if statement.get("Effect") == "Allow"
        for action in _actions(statement)
    }
    assert "bedrock-agentcore:InvokeGateway" in allowed
