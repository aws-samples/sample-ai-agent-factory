#!/usr/bin/env python3
"""Render scoped CDK CloudFormation execution policies for this blueprint.

The output mirrors the live-proven role-specific policies while making every
regional ARN explicit. It never calls AWS and never creates a policy. Validate
the rendered document with IAM Access Analyzer before using it as a CDK
bootstrap CloudFormation execution policy.

Examples:
  python3 pipelines/bootstrap/render-cfn-execution-policy.py workstream \
    --account-id 111111111111 --region eu-west-1

  python3 pipelines/bootstrap/render-cfn-execution-policy.py platform \
    --account-id 111111111111 --region eu-west-1 \
    --target-account-id 222222222222 --target-account-id 333333333333 \
    --connection-arn arn:aws:codeconnections:us-west-2:111111111111:connection/example

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from typing import Any

ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
ROLES = ("platform", "workstream", "management")
POLICY_NAME_PATTERNS = ("*AgenticAI*", "*agenticai*")


def arn(service: str, region: str, account: str, resource: str) -> str:
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def iam_arn(account: str, resource: str) -> str:
    return f"arn:aws:iam::{account}:{resource}"


def project_role_arns(account: str) -> list[str]:
    return [
        iam_arn(account, "role/cdk-*"),
        iam_arn(account, "role/*AgenticAI*"),
        iam_arn(account, "role/*agenticai*"),
    ]


def project_policy_arns(account: str) -> list[str]:
    return [
        iam_arn(account, "policy/cdk-*"),
        *(iam_arn(account, f"policy/{pattern}") for pattern in POLICY_NAME_PATTERNS),
    ]


def managed_policy_actions() -> list[str]:
    return [
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "iam:ListEntitiesForPolicy",
        "iam:ListPolicyVersions",
        "iam:SetDefaultPolicyVersion",
        "iam:TagPolicy",
        "iam:UntagPolicy",
    ]


def named_role_statement(account: str) -> dict[str, Any]:
    return {
        "Sid": "ManageNamedDeploymentRoles",
        "Effect": "Allow",
        "Action": [
            "iam:AttachRolePolicy",
            "iam:CreateRole",
            "iam:DeleteRole",
            "iam:DeleteRolePolicy",
            "iam:DetachRolePolicy",
            "iam:GetRole",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListRolePolicies",
            "iam:PutRolePolicy",
            "iam:TagRole",
            "iam:UntagRole",
            "iam:UpdateAssumeRolePolicy",
            "iam:UpdateRole",
            "iam:UpdateRoleDescription",
        ],
        "Resource": project_role_arns(account),
    }


def pass_role_statement(account: str, services: Sequence[str]) -> dict[str, Any]:
    return {
        "Sid": "PassNamedDeploymentRolesToServices",
        "Effect": "Allow",
        "Action": "iam:PassRole",
        "Resource": project_role_arns(account),
        "Condition": {"StringEquals": {"iam:PassedToService": list(services)}},
    }


def named_policy_statement(account: str) -> dict[str, Any]:
    return {
        "Sid": "ManageNamedDeploymentPolicies",
        "Effect": "Allow",
        "Action": managed_policy_actions(),
        "Resource": project_policy_arns(account),
    }


def read_iam_metadata_statement() -> dict[str, Any]:
    return {
        "Sid": "ReadIamMetadata",
        "Effect": "Allow",
        "Action": [
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
            "iam:GetRole",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListPolicyVersions",
            "iam:ListRolePolicies",
            "iam:ListRoles",
        ],
        "Resource": "*",
    }


def service_linked_role_statement(services: Sequence[str]) -> dict[str, Any]:
    return {
        "Sid": "CreateRequiredServiceLinkedRoles",
        "Effect": "Allow",
        "Action": "iam:CreateServiceLinkedRole",
        "Resource": "*",
        "Condition": {"StringEquals": {"iam:AWSServiceName": list(services)}},
    }


def workstream_policy(account: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ProvisionWorkstreamResourceFamilies",
                "Effect": "Allow",
                "Action": [
                    "apigateway:*",
                    "bedrock:*",
                    "bedrock-agentcore:*",
                    "cloudwatch:*",
                    "cognito-idp:*",
                    "dynamodb:*",
                    "ec2:*",
                    "ecr:*",
                    "ecs:*",
                    "elasticloadbalancing:*",
                    "events:*",
                    "kms:*",
                    "lambda:*",
                    "logs:*",
                    "s3:*",
                    "secretsmanager:*",
                    "servicequotas:*",
                    "sns:*",
                    "sqs:*",
                    "ssm:*",
                    "states:*",
                    "wafv2:*",
                ],
                "Resource": "*",
            },
            named_role_statement(account),
            pass_role_statement(
                account,
                [
                    "bedrock-agentcore.amazonaws.com",
                    "cloudformation.amazonaws.com",
                    "ecs-tasks.amazonaws.com",
                    "lambda.amazonaws.com",
                    "states.amazonaws.com",
                ],
            ),
            named_policy_statement(account),
            read_iam_metadata_statement(),
            service_linked_role_statement(
                [
                    "bedrock-agentcore.amazonaws.com",
                    "ecs.amazonaws.com",
                    "elasticloadbalancing.amazonaws.com",
                ],
            ),
        ],
    }


def platform_policy(
    account: str,
    region: str,
    target_accounts: Sequence[str],
    connection_arn: str,
    application_id: str,
    tenant_id: str,
    agent_id: str,
    cost_centre: str,
) -> dict[str, Any]:
    environments = ["nonprod", "prod"]
    tag_keys = ["application-id", "agent-id", "tenant-id", "cost-centre", "environment"]
    request_tags = {
        "aws:RequestTag/application-id": application_id,
        "aws:RequestTag/agent-id": agent_id,
        "aws:RequestTag/tenant-id": tenant_id,
        "aws:RequestTag/cost-centre": cost_centre,
        "aws:RequestTag/environment": environments,
    }
    registry_arn = arn("agent-registry", region, account, "registry/*")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ProvisionBlueprintResourceFamilies",
                "Effect": "Allow",
                "Action": [
                    "apigateway:*",
                    "bedrock:*",
                    "bedrock-agentcore:*",
                    "cloudwatch:*",
                    "codebuild:*",
                    "codepipeline:*",
                    "cognito-idp:*",
                    "dynamodb:*",
                    "ec2:*",
                    "ecr:*",
                    "ecs:*",
                    "elasticloadbalancing:*",
                    "kms:*",
                    "lambda:*",
                    "logs:*",
                    "oam:*",
                    "s3:*",
                    "secretsmanager:*",
                    "servicequotas:*",
                    "sns:*",
                    "ssm:*",
                    "wafv2:*",
                ],
                "Resource": "*",
            },
            {
                "Sid": "UsePlatformSourceConnection",
                "Effect": "Allow",
                "Action": [
                    "codeconnections:GetConnection",
                    "codeconnections:PassConnection",
                    "codeconnections:UseConnection",
                    "codestar-connections:GetConnection",
                    "codestar-connections:PassConnection",
                    "codestar-connections:UseConnection",
                ],
                "Resource": connection_arn,
            },
            named_role_statement(account),
            pass_role_statement(
                account,
                [
                    "bedrock-agentcore.amazonaws.com",
                    "cloudformation.amazonaws.com",
                    "codebuild.amazonaws.com",
                    "codepipeline.amazonaws.com",
                    "ecs-tasks.amazonaws.com",
                    "lambda.amazonaws.com",
                ],
            ),
            named_policy_statement(account),
            read_iam_metadata_statement(),
            service_linked_role_statement(
                [
                    "bedrock-agentcore.amazonaws.com",
                    "codebuild.amazonaws.com",
                    "codepipeline.amazonaws.com",
                    "ecs.amazonaws.com",
                    "elasticloadbalancing.amazonaws.com",
                ],
            ),
            {
                "Sid": "PassCrossAccountCdkDeploymentRolesToCodePipeline",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [
                    iam_arn(
                        target,
                        f"role/cdk-hnb659fds-deploy-role-{target}-{region}",
                    )
                    for target in target_accounts
                ],
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "codepipeline.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "PassCrossAccountCfnExecutionRolesToCloudFormation",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [
                    iam_arn(
                        target,
                        f"role/cdk-hnb659fds-cfn-exec-role-{target}-{region}",
                    )
                    for target in target_accounts
                ],
                "Condition": {
                    "StringEquals": {
                        "iam:PassedToService": "cloudformation.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "CreateTaggedGaRegistryResources",
                "Effect": "Allow",
                "Action": [
                    "agent-registry:CreateRegistry",
                    "agent-registry:TagResource",
                ],
                "Resource": "*",
                "Condition": {
                    "StringEquals": request_tags,
                    "ForAllValues:StringEquals": {"aws:TagKeys": tag_keys},
                },
            },
            {
                "Sid": "CreateTaggedGaRegistryRecords",
                "Effect": "Allow",
                "Action": "agent-registry:CreateRegistryRecord",
                "Resource": registry_arn,
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceTag/application-id": application_id,
                        "aws:ResourceTag/environment": environments,
                        **request_tags,
                    },
                    "ForAllValues:StringEquals": {"aws:TagKeys": tag_keys},
                },
            },
            {
                "Sid": "ManageTaggedGaRegistries",
                "Effect": "Allow",
                "Action": [
                    "agent-registry:GetRegistry",
                    "agent-registry:ListRegistryRecords",
                    "agent-registry:UpdateRegistry",
                    "agent-registry:DeleteRegistry",
                    "agent-registry:ListTagsForResource",
                    "agent-registry:TagResource",
                    "agent-registry:UntagResource",
                ],
                "Resource": registry_arn,
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceTag/application-id": application_id,
                        "aws:ResourceTag/environment": environments,
                    }
                },
            },
            {
                "Sid": "ManageTaggedGaRegistryRecords",
                "Effect": "Allow",
                "Action": [
                    "agent-registry:GetRegistryRecord",
                    "agent-registry:UpdateRegistryRecord",
                    "agent-registry:DeleteRegistryRecord",
                    "agent-registry:ListTagsForResource",
                    "agent-registry:TagResource",
                    "agent-registry:UntagResource",
                ],
                "Resource": f"{registry_arn}/record/*",
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceTag/application-id": application_id,
                        "aws:ResourceTag/environment": environments,
                    }
                },
            },
        ],
    }


def management_policy(account: str, region: str) -> dict[str, Any]:
    destination_role = iam_arn(account, "role/AgenticAI-LogArchive-CWLDestinationRole")
    provider_role = iam_arn(
        account, "role/Nonprod-LogArchive-CustomS3AutoDeleteObjects*"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ProvisionGovernanceTelemetryStacks",
                "Effect": "Allow",
                "Action": ["kms:*", "logs:*", "oam:*", "s3:*"],
                "Resource": "*",
            },
            {
                "Sid": "ReadCdkBootstrapVersion",
                "Effect": "Allow",
                "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                "Resource": arn(
                    "ssm",
                    region,
                    account,
                    "parameter/cdk-bootstrap/hnb659fds/version",
                ),
            },
            {
                "Sid": "ProvisionCentralLogStream",
                "Effect": "Allow",
                "Action": [
                    "kinesis:AddTagsToStream",
                    "kinesis:CreateStream",
                    "kinesis:DecreaseStreamRetentionPeriod",
                    "kinesis:DeleteStream",
                    "kinesis:DescribeStream",
                    "kinesis:DescribeStreamSummary",
                    "kinesis:IncreaseStreamRetentionPeriod",
                    "kinesis:ListTagsForStream",
                    "kinesis:RemoveTagsFromStream",
                    "kinesis:StartStreamEncryption",
                    "kinesis:StopStreamEncryption",
                    "kinesis:UpdateShardCount",
                    "kinesis:UpdateStreamMode",
                ],
                "Resource": arn(
                    "kinesis", region, account, "stream/agenticai-central-logs"
                ),
            },
            {
                "Sid": "ManageLogArchiveDestinationRole",
                "Effect": "Allow",
                "Action": [
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:DeleteRolePolicy",
                    "iam:GetRole",
                    "iam:GetRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListInstanceProfilesForRole",
                    "iam:ListRolePolicies",
                    "iam:PutRolePolicy",
                    "iam:TagRole",
                    "iam:UntagRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:UpdateRole",
                ],
                "Resource": destination_role,
            },
            {
                "Sid": "PassLogArchiveDestinationRoleToCloudWatchLogs",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": destination_role,
                "Condition": {
                    "StringEquals": {"iam:PassedToService": "logs.amazonaws.com"}
                },
            },
            {
                "Sid": "ManageLogArchiveAutoDeleteProviderRole",
                "Effect": "Allow",
                "Action": [
                    "iam:AttachRolePolicy",
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:DeleteRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:GetRole",
                    "iam:GetRolePolicy",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListInstanceProfilesForRole",
                    "iam:ListRolePolicies",
                    "iam:PutRolePolicy",
                    "iam:TagRole",
                    "iam:UntagRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:UpdateRole",
                ],
                "Resource": provider_role,
            },
            {
                "Sid": "ReadLogArchiveAutoDeleteManagedPolicy",
                "Effect": "Allow",
                "Action": ["iam:GetPolicy", "iam:GetPolicyVersion"],
                "Resource": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            },
            {
                "Sid": "PassLogArchiveAutoDeleteProviderRoleToLambda",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": provider_role,
                "Condition": {
                    "StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}
                },
            },
            {
                "Sid": "ManageLogArchiveAutoDeleteProviderFunction",
                "Effect": "Allow",
                "Action": [
                    "lambda:CreateFunction",
                    "lambda:DeleteFunction",
                    "lambda:GetFunction",
                    "lambda:GetFunctionConfiguration",
                    "lambda:GetRuntimeManagementConfig",
                    "lambda:InvokeFunction",
                    "lambda:ListTags",
                    "lambda:PutRuntimeManagementConfig",
                    "lambda:TagResource",
                    "lambda:UntagResource",
                    "lambda:UpdateFunctionCode",
                    "lambda:UpdateFunctionConfiguration",
                ],
                "Resource": arn(
                    "lambda",
                    region,
                    account,
                    "function:Nonprod-LogArchive-CustomS3AutoDeleteObjects*",
                ),
            },
            {
                "Sid": "ReadNamedDeploymentPolicyAttachments",
                "Effect": "Allow",
                "Action": "iam:ListEntitiesForPolicy",
                "Resource": [
                    iam_arn(account, f"policy/{pattern}")
                    for pattern in POLICY_NAME_PATTERNS
                ],
            },
        ],
    }


def connection_arn(value: str, account: str) -> str:
    pattern = re.compile(
        rf"^arn:aws:(?:codeconnections|codestar-connections):[a-z0-9-]+:{account}:connection/[A-Za-z0-9-]+$"
    )
    if not pattern.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "connection ARN must be a connection in the Platform account"
        )
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account_role", choices=ROLES)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--target-account-id", action="append", default=[])
    parser.add_argument("--connection-arn")
    parser.add_argument("--application-id", default="demo")
    parser.add_argument("--tenant-id", default="demo")
    parser.add_argument("--agent-id", default="primary")
    parser.add_argument("--cost-centre", default="engineering")
    args = parser.parse_args()
    if not ACCOUNT_ID.fullmatch(args.account_id):
        parser.error("--account-id must be a 12-digit AWS account id")
    if not REGION.fullmatch(args.region):
        parser.error("--region must be a concrete AWS Region code")
    if any(not ACCOUNT_ID.fullmatch(item) for item in args.target_account_id):
        parser.error("every --target-account-id must be a 12-digit AWS account id")
    if args.account_role == "platform":
        if not args.target_account_id:
            parser.error("platform policy requires at least one --target-account-id")
        if not args.connection_arn:
            parser.error("platform policy requires --connection-arn")
        try:
            args.connection_arn = connection_arn(args.connection_arn, args.account_id)
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
    elif args.target_account_id or args.connection_arn:
        parser.error(
            "target accounts and connection ARN apply only to the platform policy"
        )
    return args


def render(args: argparse.Namespace) -> dict[str, Any]:
    if args.account_role == "workstream":
        return workstream_policy(args.account_id)
    if args.account_role == "management":
        return management_policy(args.account_id, args.region)
    return platform_policy(
        args.account_id,
        args.region,
        sorted(set(args.target_account_id)),
        args.connection_arn,
        args.application_id,
        args.tenant_id,
        args.agent_id,
        args.cost_centre,
    )


def main() -> None:
    print(json.dumps(render(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
