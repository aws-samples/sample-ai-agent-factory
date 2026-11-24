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

  python3 pipelines/bootstrap/render-cfn-execution-policy.py management \
    --account-id 333333333333 --region eu-west-1 --organization-id o-exampleorg0a

  python3 pipelines/bootstrap/render-cfn-execution-policy.py audit \
    --account-id 444444444444 --region eu-west-1

Each rendered document must fit one IAM managed policy (6,144 characters
without whitespace); the script exits non-zero instead of emitting a document
IAM would refuse.

With `--organization-id` the management document also carries the OU and SCP
lifecycle of that organization: CloudFormation may create OUs and SCPs only
as part of the OrgStack (default `--stack-name AgenticAI-Management-OrgStack`)
and change or delete only the OUs and SCPs that stack created, keyed on the
reserved aws:cloudformation:stack-name tag CloudFormation applies itself. A
scoped document cannot cover creating the Organization (its id does not exist
yet): create it once, then render with the id and deploy the stage against
the existing organization. Without `--organization-id` the document is the
previous one (Omni domain and LogArchive statements only).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from typing import Any

ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
ORGANIZATION_ID = re.compile(r"^o-[a-z0-9]{10,32}$")
# CloudFormation stack names: a letter, then letters, digits and hyphens.
STACK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
# The `management` stage stack id in bin/agentic-ai-platform.ts.
MANAGEMENT_STACK_NAME = "AgenticAI-Management-OrgStack"
# CloudFormation stamps every OU and SCP it creates with this reserved tag
# inside the Create request, and no principal can set an `aws:`-prefixed tag
# through the Organizations API, so the tag identifies one stack's resources.
CFN_STACK_NAME_TAG = "aws:cloudformation:stack-name"
ROLES = ("platform", "workstream", "management", "audit")
# IAM counts a managed policy's characters without whitespace. Each rendered
# role becomes one CDK CloudFormation execution policy, so it must fit.
IAM_MANAGED_POLICY_MAX_CHARS = 6144
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


OAM_SOURCE_LINK_ACTIONS = [
    "oam:CreateLink",
    "oam:DeleteLink",
    "oam:GetLink",
    "oam:ListLinks",
    "oam:ListTagsForResource",
    "oam:TagResource",
    "oam:UntagResource",
    "oam:UpdateLink",
    "xray:Link",
]

# ADR-13/ADR-15: a cell or hub may opt into Transaction Search declaratively
# (AWS::XRay::TransactionSearchConfig). These are the exact account-wide
# settings that resource manages; nothing else in xray is granted here.
TRANSACTION_SEARCH_ACTIONS = [
    "xray:GetTraceSegmentDestination",
    "xray:UpdateTraceSegmentDestination",
    "xray:GetIndexingRules",
    "xray:UpdateIndexingRule",
    "logs:PutResourcePolicy",
    "logs:DescribeResourcePolicies",
    "logs:DeleteResourcePolicy",
]

# ADR-15: CloudWatch Omni hub (Audit/Observability account). The cloudwatch
# namespace is already fully granted to the hub by ProvisionGovernanceTelemetryStacks;
# the Observability Admin actions below are the ones the organization
# centralization rule, the Dataset integration and the delegated-administrator
# path need. Access grants and the Dataset integration are applied post-deploy by
# scripts/omni/omni_space_bootstrap.py with the operator's own credentials.
OMNI_HUB_OBSERVABILITY_ADMIN_ACTIONS = [
    "observabilityadmin:CreateCentralizationRuleForOrganization",
    "observabilityadmin:UpdateCentralizationRuleForOrganization",
    "observabilityadmin:DeleteCentralizationRuleForOrganization",
    "observabilityadmin:GetCentralizationRuleForOrganization",
    "observabilityadmin:ListCentralizationRulesForOrganization",
    "observabilityadmin:ListTagsForResource",
    "observabilityadmin:TagResource",
    "observabilityadmin:UntagResource",
]


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
                    *OAM_SOURCE_LINK_ACTIONS,
                    *TRANSACTION_SEARCH_ACTIONS,
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
                    *OAM_SOURCE_LINK_ACTIONS,
                    *TRANSACTION_SEARCH_ACTIONS,
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


# Role lifecycle the Omni stacks need on their own, exactly named roles.
OMNI_ROLE_MANAGEMENT_ACTIONS = [
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
    "iam:PutRolePermissionsBoundary",
    "iam:DeleteRolePermissionsBoundary",
    "iam:PutRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
]

# LogArchiveStack roles (CloudWatch Logs destination, S3 auto-delete provider):
# the union of what CloudFormation's IAM::Role handler calls for the two roles.
LOG_ARCHIVE_ROLE_ACTIONS = [
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
]

# `management` stage (OrgStack): the OU hierarchy and SCPs 01-13 through the
# AWS::Organizations::* CloudFormation handlers. Every action below is one
# those handlers call (their registry schemas list the same set).
# CloudFormation sends its reserved aws:cloudformation:* tags inside each
# CreateOrganizationalUnit / CreatePolicy request and the resources keep them,
# so creation requires the stack-name request tag and every later change
# requires the matching resource tag: the role cannot rename, delete, detach or
# re-tag an OU or SCP that an operator or another stack created. A tagged
# create also authorizes organizations:TagResource (dependent action), which
# the same request-tag condition covers.
ORGANIZATIONS_CREATE_ACTIONS = [
    "organizations:CreateOrganizationalUnit",
    "organizations:CreatePolicy",
    "organizations:TagResource",
]
ORGANIZATIONS_MUTATE_ACTIONS = [
    "organizations:UpdateOrganizationalUnit",
    "organizations:DeleteOrganizationalUnit",
    "organizations:UpdatePolicy",
    "organizations:DeletePolicy",
    "organizations:AttachPolicy",
    "organizations:DetachPolicy",
    "organizations:TagResource",
    "organizations:UntagResource",
]
ORGANIZATIONS_READ_ACTIONS = [
    "organizations:DescribeOrganizationalUnit",
    "organizations:DescribePolicy",
    "organizations:ListParents",
    "organizations:ListTagsForResource",
    "organizations:ListTargetsForPolicy",
]
# Reads that take no resource, or only the root. Creating or deleting the
# Organization itself is never granted: a scoped document is pinned to an
# organization id that only exists once the Organization does, so the stage
# adopts an existing organization when it deploys through this role.
ORGANIZATION_READ_ACTIONS = [
    "organizations:DescribeOrganization",
    "organizations:ListOrganizationalUnitsForParent",
    "organizations:ListPolicies",
    "organizations:ListRoots",
]


def organizations_statements(
    account: str, organization_id: str, stack_name: str
) -> list[dict[str, Any]]:
    """OU and SCP lifecycle for the `management` stage.

    Resources are pinned to one organization; creating (and tagging) an OU or
    SCP requires CloudFormation's stack-name request tag for `stack_name`, and
    changing or deleting one requires the matching resource tag, so only the
    OUs and SCPs that stack created are ever mutable through this role.
    """
    root = f"arn:aws:organizations::{account}:root/{organization_id}/*"
    ou = f"arn:aws:organizations::{account}:ou/{organization_id}/*"
    scp = f"arn:aws:organizations::{account}:policy/{organization_id}/service_control_policy/*"
    return [
        {
            # OUs are created directly under the root (the parent is the resource
            # CreateOrganizationalUnit authorizes against; the stage never nests
            # them); the dependent TagResource is authorized on the new OU or SCP.
            "Sid": "CreateOuAndScpForStack",
            "Effect": "Allow",
            "Action": ORGANIZATIONS_CREATE_ACTIONS,
            "Resource": [root, ou, scp],
            "Condition": {
                "StringEquals": {f"aws:RequestTag/{CFN_STACK_NAME_TAG}": stack_name}
            },
        },
        {
            "Sid": "ChangeStackOwnedOuAndScp",
            "Effect": "Allow",
            "Action": ORGANIZATIONS_MUTATE_ACTIONS,
            "Resource": [ou, scp],
            "Condition": {
                "StringEquals": {f"aws:ResourceTag/{CFN_STACK_NAME_TAG}": stack_name}
            },
        },
        {
            "Sid": "ReadOuAndScp",
            "Effect": "Allow",
            "Action": ORGANIZATIONS_READ_ACTIONS,
            "Resource": [ou, scp],
        },
        {
            "Sid": "ReadOrganization",
            "Effect": "Allow",
            "Action": ORGANIZATION_READ_ACTIONS,
            "Resource": "*",
        },
    ]


def omni_domain_statements(account: str) -> list[dict[str, Any]]:
    """ADR-15 CloudWatch Omni organization domain (OmniDomainStack), management account.

    Only the organization domain, its access grants and the domain access role
    live in the management account; the space, the hub roles and the
    centralization rule belong to the Audit account (`audit_policy`).
    """
    domain_role = iam_arn(account, "role/CloudWatchOmniDomainAccessRole")
    return [
        {
            "Sid": "ProvisionOmniOrganizationDomain",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:CreateDomainForOrganization",
                "cloudwatch:GetDomainForOrganization",
                "cloudwatch:UpdateDomainForOrganization",
                "cloudwatch:DeleteDomainForOrganization",
                "cloudwatch:CreateDomainAccessGrantForOrganization",
                "cloudwatch:DeleteDomainAccessGrantForOrganization",
                "cloudwatch:TagResource",
                "cloudwatch:UntagResource",
                "cloudwatch:ListTagsForResource",
            ],
            "Resource": "*",
        },
        {
            "Sid": "ManageOmniDomainAccessRole",
            "Effect": "Allow",
            "Action": OMNI_ROLE_MANAGEMENT_ACTIONS,
            "Resource": domain_role,
        },
        {
            "Sid": "PassOmniDomainAccessRoleToCloudWatch",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": domain_role,
            "Condition": {
                "StringEquals": {"iam:PassedToService": "cloudwatch.amazonaws.com"}
            },
        },
    ]


def omni_hub_statements(account: str, region: str) -> list[dict[str, Any]]:
    """ADR-15 CloudWatch Omni hub (OmniHubStack) in the Audit/Observability account.

    The space, its roles and keys, Transaction Search and the organization
    centralization rule. Role management is pinned to the exact Omni role
    families; passing roles is pinned per consuming service. The organization
    domain itself is the management account's (`omni_domain_statements`).
    """
    omni_roles = [
        iam_arn(account, "role/CloudWatchOmniOperatorRole"),
        iam_arn(account, f"role/CloudWatchOmniDatasetIntegrationRole-{region}"),
        iam_arn(account, "role/service-role/AgentCoreEvaluationRole-omni-*"),
        iam_arn(account, "role/AgenticAI-Omni-HubStack-*"),
    ]
    return [
        {
            "Sid": "ProvisionOmniHubSpaceAndCentralization",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:GetDomainForOrganization",
                "cloudwatch:CreateSpace",
                "cloudwatch:GetSpace",
                "cloudwatch:UpdateSpace",
                "cloudwatch:DeleteSpace",
                "cloudwatch:ListSpaces",
                "cloudwatch:CreateOmniDashboard",
                "cloudwatch:GetOmniDashboard",
                "cloudwatch:UpdateOmniDashboard",
                "cloudwatch:DeleteOmniDashboard",
                "cloudwatch:TagResource",
                "cloudwatch:UntagResource",
                "cloudwatch:ListTagsForResource",
                *OMNI_HUB_OBSERVABILITY_ADMIN_ACTIONS,
                *TRANSACTION_SEARCH_ACTIONS,
            ],
            "Resource": "*",
        },
        {
            "Sid": "ManageOmniHubRoles",
            "Effect": "Allow",
            "Action": OMNI_ROLE_MANAGEMENT_ACTIONS,
            "Resource": omni_roles,
        },
        {
            "Sid": "PassOmniHubRolesToServices",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": omni_roles,
            "Condition": {
                "StringEquals": {
                    "iam:PassedToService": [
                        "cloudwatch.amazonaws.com",
                        "logs.amazonaws.com",
                        "bedrock-agentcore.amazonaws.com",
                        "logs-centralization.observabilityadmin.amazonaws.com",
                    ]
                }
            },
        },
        {
            "Sid": "CreateOmniServiceLinkedRoles",
            "Effect": "Allow",
            "Action": "iam:CreateServiceLinkedRole",
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "iam:AWSServiceName": [
                        "omni.cloudwatch.amazonaws.com",
                        "logs-centralization.observabilityadmin.amazonaws.com",
                    ]
                }
            },
        },
    ]


def audit_policy(account: str, region: str) -> dict[str, Any]:
    """Audit/Observability account: AuditStack (OAM sink) and the `omni-hub` stage."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ProvisionGovernanceTelemetryStacks",
                "Effect": "Allow",
                "Action": ["kms:*", "logs:*", "oam:*"],
                "Resource": "*",
            },
            *omni_hub_statements(account, region),
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
        ],
    }


def management_policy(
    account: str, region: str, organization_id: str | None, stack_name: str | None
) -> dict[str, Any]:
    """Management account document; the OU/SCP lifecycle needs the organization id."""
    organizations = (
        organizations_statements(account, organization_id, stack_name or MANAGEMENT_STACK_NAME)
        if organization_id
        else []
    )
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
            *organizations,
            *omni_domain_statements(account),
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
                "Sid": "ManageLogArchiveRoles",
                "Effect": "Allow",
                "Action": LOG_ARCHIVE_ROLE_ACTIONS,
                "Resource": [destination_role, provider_role],
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
    parser.add_argument(
        "--organization-id",
        help="AWS Organization id (o-...). Management policy only: adds the OU and "
        "SCP lifecycle pinned to that organization. Without it the document "
        "cannot deploy the OU/SCP stage.",
    )
    parser.add_argument(
        "--stack-name",
        help="CloudFormation stack name of the management stage (default "
        f"{MANAGEMENT_STACK_NAME}; needs --organization-id). The management "
        "policy lets the role create OUs and SCPs only for this stack and "
        "change only the ones it created.",
    )
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
    if args.account_role == "management":
        if args.organization_id and not ORGANIZATION_ID.fullmatch(args.organization_id):
            parser.error("--organization-id must look like o-xxxxxxxxxx")
        if args.stack_name is not None and not args.organization_id:
            parser.error("--stack-name needs --organization-id")
        if args.organization_id and args.stack_name is None:
            args.stack_name = MANAGEMENT_STACK_NAME
        if args.stack_name is not None and not STACK_NAME.fullmatch(args.stack_name):
            parser.error("--stack-name must be a CloudFormation stack name")
        if not args.organization_id:
            print(
                "note: no --organization-id, so the management document carries no "
                "OU/SCP lifecycle and cannot deploy the OrgStack. Create the "
                "Organization first, then re-render with its id.",
                file=sys.stderr,
            )
    elif args.organization_id or args.stack_name:
        parser.error(
            "--organization-id and --stack-name apply only to the management policy"
        )
    return args


def policy_size(policy: dict[str, Any]) -> int:
    """Characters IAM counts toward the managed-policy limit (whitespace excluded)."""
    return len(json.dumps(policy, separators=(",", ":"), sort_keys=True))


def render(args: argparse.Namespace) -> dict[str, Any]:
    if args.account_role == "workstream":
        return workstream_policy(args.account_id)
    if args.account_role == "management":
        return management_policy(
            args.account_id, args.region, args.organization_id, args.stack_name
        )
    if args.account_role == "audit":
        return audit_policy(args.account_id, args.region)
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
    policy = render(parse_args())
    size = policy_size(policy)
    if size > IAM_MANAGED_POLICY_MAX_CHARS:
        raise SystemExit(
            f"rendered policy is {size} characters without whitespace; IAM managed "
            f"policies allow {IAM_MANAGED_POLICY_MAX_CHARS}. Trim or split the role "
            "before creating the policy."
        )
    print(json.dumps(policy, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
