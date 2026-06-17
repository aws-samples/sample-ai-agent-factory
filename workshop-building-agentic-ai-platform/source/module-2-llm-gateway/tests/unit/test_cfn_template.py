# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validate the LiteLLM Proxy CloudFormation template structure."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CFN_TEMPLATE = (
    Path(__file__).resolve().parents[4]
    / "static" / "cfn" / "llm-gateway" / "workshop-llm-gateway-stack.yaml"
)


# Custom YAML loader that handles CloudFormation intrinsic functions
class _CfnLoader(yaml.SafeLoader):
    pass


_CFN_TAGS = [
    "!Ref", "!Sub", "!GetAtt", "!Join", "!Select", "!Split",
    "!If", "!Equals", "!Not", "!And", "!Or", "!FindInMap",
    "!GetAZs", "!ImportValue", "!Condition",
]

for _tag in _CFN_TAGS:
    _CfnLoader.add_multi_constructor(
        _tag,
        lambda loader, suffix, node: loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else node.value,
    )


@pytest.fixture
def template() -> dict:
    """Load and parse the CloudFormation template (with CFN intrinsics)."""
    with open(CFN_TEMPLATE) as f:
        # nosec B506 — _CfnLoader subclasses yaml.SafeLoader; it only adds CFN
        # intrinsic-tag constructors, so arbitrary-object instantiation is not possible.
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506


class TestTemplateStructure:
    def test_template_file_exists(self):
        assert CFN_TEMPLATE.exists(), f"Template not found at {CFN_TEMPLATE}"

    def test_has_required_sections(self, template: dict):
        assert "AWSTemplateFormatVersion" in template
        assert "Parameters" in template
        assert "Resources" in template
        assert "Outputs" in template

    def test_has_description(self, template: dict):
        assert "Description" in template
        assert "LLM Gateway" in template["Description"]


class TestParameters:
    def test_has_litellm_image_tag(self, template: dict):
        assert "LiteLLMImageTag" in template["Parameters"]

    def test_has_postgres_image_tag(self, template: dict):
        assert "PostgresImageTag" in template["Parameters"]

    def test_has_admin_key_parameter(self, template: dict):
        params = template["Parameters"]
        assert "AdminKey" in params
        assert params["AdminKey"].get("NoEcho") is True

    def test_has_image_tag_parameters(self, template: dict):
        params = template["Parameters"]
        assert "LiteLLMImageTag" in params
        assert "PostgresImageTag" in params


class TestNetworkingResources:
    def test_has_vpc(self, template: dict):
        assert "VPC" in template["Resources"]
        assert template["Resources"]["VPC"]["Type"] == "AWS::EC2::VPC"

    def test_has_public_subnets(self, template: dict):
        assert "PublicSubnet1" in template["Resources"]
        assert "PublicSubnet2" in template["Resources"]

    def test_has_private_subnets(self, template: dict):
        assert "PrivateSubnet1" in template["Resources"]
        assert "PrivateSubnet2" in template["Resources"]

    def test_has_nat_gateway(self, template: dict):
        assert "NATGateway" in template["Resources"]

    def test_has_internet_gateway(self, template: dict):
        assert "InternetGateway" in template["Resources"]


class TestSecurityGroups:
    def test_has_alb_security_group(self, template: dict):
        assert "ALBSecurityGroup" in template["Resources"]

    def test_has_ecs_security_group(self, template: dict):
        assert "ECSSecurityGroup" in template["Resources"]

    def test_has_efs_security_group(self, template: dict):
        assert "EFSSecurityGroup" in template["Resources"]


class TestSecretsManagerResources:
    def test_has_admin_key_secret(self, template: dict):
        assert "AdminKeySecret" in template["Resources"]
        assert template["Resources"]["AdminKeySecret"]["Type"] == "AWS::SecretsManager::Secret"

    def test_has_postgres_password_secret(self, template: dict):
        assert "PostgresPasswordSecret" in template["Resources"]
        assert template["Resources"]["PostgresPasswordSecret"]["Type"] == "AWS::SecretsManager::Secret"


class TestEFSResources:
    def test_has_file_system(self, template: dict):
        assert "FileSystem" in template["Resources"]
        assert template["Resources"]["FileSystem"]["Type"] == "AWS::EFS::FileSystem"

    def test_has_mount_targets(self, template: dict):
        assert "EFSMountTarget1" in template["Resources"]
        assert "EFSMountTarget2" in template["Resources"]

    def test_has_access_point(self, template: dict):
        assert "EFSAccessPoint" in template["Resources"]

    def test_efs_encrypted(self, template: dict):
        props = template["Resources"]["FileSystem"]["Properties"]
        assert props.get("Encrypted") is True


class TestECSResources:
    def test_has_cluster(self, template: dict):
        assert "ECSCluster" in template["Resources"]

    def test_has_task_definition(self, template: dict):
        assert "TaskDefinition" in template["Resources"]
        td = template["Resources"]["TaskDefinition"]
        assert td["Type"] == "AWS::ECS::TaskDefinition"

    def test_task_is_fargate(self, template: dict):
        td = template["Resources"]["TaskDefinition"]["Properties"]
        assert "FARGATE" in td["RequiresCompatibilities"]

    def test_task_has_two_containers(self, template: dict):
        td = template["Resources"]["TaskDefinition"]["Properties"]
        containers = td["ContainerDefinitions"]
        assert len(containers) == 2
        names = {c["Name"] for c in containers}
        assert "litellm" in names
        assert "postgres" in names

    def test_has_service(self, template: dict):
        assert "Service" in template["Resources"]

    def test_has_log_group(self, template: dict):
        assert "LogGroup" in template["Resources"]


class TestIAMResources:
    def test_has_execution_role(self, template: dict):
        assert "ECSExecutionRole" in template["Resources"]

    def test_has_task_role(self, template: dict):
        assert "ECSTaskRole" in template["Resources"]

    def test_task_role_has_bedrock_permissions(self, template: dict):
        role = template["Resources"]["ECSTaskRole"]
        policies = role["Properties"]["Policies"]
        bedrock_policy = next(
            (p for p in policies if p["PolicyName"] == "BedrockAccess"), None
        )
        assert bedrock_policy is not None

        statements = bedrock_policy["PolicyDocument"]["Statement"]
        actions = statements[0]["Action"]
        assert "bedrock:InvokeModel" in actions
        assert "bedrock:InvokeModelWithResponseStream" in actions

    def test_task_role_has_guardrail_permissions(self, template: dict):
        role = template["Resources"]["ECSTaskRole"]
        policies = role["Properties"]["Policies"]
        guardrail_policy = next(
            (p for p in policies if p["PolicyName"] == "BedrockGuardrails"), None
        )
        assert guardrail_policy is not None

        statements = guardrail_policy["PolicyDocument"]["Statement"]
        actions = statements[0]["Action"]
        assert "bedrock:ApplyGuardrail" in actions


class TestALBResources:
    def test_has_alb(self, template: dict):
        assert "ALB" in template["Resources"]

    def test_has_single_target_group(self, template: dict):
        assert "TargetGroup" in template["Resources"]

    def test_has_listener(self, template: dict):
        assert "ALBListenerHTTP" in template["Resources"]


class TestOutputs:
    def test_has_proxy_url(self, template: dict):
        assert "ProxyUrl" in template["Outputs"]

    def test_has_alb_dns_name(self, template: dict):
        assert "ALBDnsName" in template["Outputs"]

    def test_has_admin_key_secret_arn(self, template: dict):
        assert "AdminKeySecretArn" in template["Outputs"]

    def test_has_vpc_id(self, template: dict):
        assert "VpcId" in template["Outputs"]

    def test_has_private_subnet_ids(self, template: dict):
        assert "PrivateSubnetIds" in template["Outputs"]

    def test_has_ecs_cluster_name(self, template: dict):
        assert "ECSClusterName" in template["Outputs"]
