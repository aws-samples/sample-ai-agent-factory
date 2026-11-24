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

  python3 pipelines/bootstrap/render-cfn-execution-policy.py hub-role-boundary \
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
the existing organization. Without `--organization-id` the document carries
only the Omni organization domain and the LogArchive statements; the Omni hub
statements it used to carry now render as `audit`.

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
ROLES = ("platform", "workstream", "management", "audit", "hub-role-boundary")
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

# ADR-15: CloudWatch Omni hub (Audit/Observability account). The Observability
# Admin actions below are the ones the organization centralization rule, the
# Dataset integration and the delegated-administrator path need. Access grants
# and the Dataset integration are applied post-deploy by
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

# Roles the Audit account's OmniHubStack creates must carry a permissions
# boundary, so a template deployed through the execution role cannot turn one of
# them into an administrator through an inline policy, a managed-policy
# attachment or a new trust policy. The operator role keeps the AWS-managed
# boundary the Omni documentation prescribes and may only receive the AWS-managed
# Omni policies (it has no inline policy); every other hub role carries the
# blueprint boundary rendered by `hub-role-boundary`. The actions that set or
# widen a role's permissions are gated on the boundary that role must carry,
# and a boundary can never be removed (no iam:DeleteRolePermissionsBoundary).
OMNI_HUB_ROLE_BOUNDARY_NAME = "AgenticAI-OmniHubRoleBoundary"
OPERATOR_ROLE_BOUNDARY_ARN = "arn:aws:iam::aws:policy/CloudWatchOmniSpaceAccessPolicy"
OPERATOR_ROLE_POLICY_ARNS = [
    OPERATOR_ROLE_BOUNDARY_ARN,
    "arn:aws:iam::aws:policy/CloudWatchOmniModelInferencePolicy",
    "arn:aws:iam::aws:policy/CloudWatchOmniAWSIntegrationPolicy",
]
OPERATOR_ROLE_GATED_ACTIONS = [
    "iam:AttachRolePolicy",
    "iam:CreateRole",
    "iam:PutRolePermissionsBoundary",
]
ROLE_BOUNDARY_GATED_ACTIONS = [
    "iam:AttachRolePolicy",
    "iam:CreateRole",
    "iam:PutRolePermissionsBoundary",
    "iam:PutRolePolicy",
]
ROLE_LIFECYCLE_ACTIONS = [
    "iam:DeleteRole",
    "iam:DeleteRolePolicy",
    "iam:DetachRolePolicy",
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListAttachedRolePolicies",
    "iam:ListRolePolicies",
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
    families and gated on the hub role boundary; passing roles is pinned per
    consuming service. The organization domain itself is the management
    account's (`omni_domain_statements`).
    """
    operator_role = iam_arn(account, "role/CloudWatchOmniOperatorRole")
    hub_roles = [
        iam_arn(account, f"role/CloudWatchOmniDatasetIntegrationRole-{region}"),
        iam_arn(account, "role/service-role/AgentCoreEvaluationRole-omni-*"),
        iam_arn(account, "role/AgenticAI-Omni-HubStack-*"),
    ]
    # The golden-gate runner (agenticai/omniGoldenGateRunnerPrincipalArns) is
    # assumed by the workload pipeline, never passed to a service.
    runner_role = iam_arn(account, f"role/AgenticAIGoldenGateRunner-{region}")
    omni_roles = [operator_role, *hub_roles]
    bounded_roles = [*hub_roles, runner_role]
    managed_roles = [operator_role, *bounded_roles]
    hub_boundary = iam_arn(account, f"policy/{OMNI_HUB_ROLE_BOUNDARY_NAME}")
    return [
        {
            # Actions with no resource type: the space, its dashboards, the
            # centralization rule, Transaction Search and its Application Signals
            # discovery.
            "Sid": "ProvisionOmniHubUnscoped",
            "Effect": "Allow",
            "Action": [
                "application-signals:StartDiscovery",
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
            # The operator role: created only with the AWS-managed Omni boundary
            # and given only the documented AWS-managed Omni policies; it has no
            # inline policy, so iam:PutRolePolicy is not granted on it.
            "Sid": "CreateBoundedOperatorRole",
            "Effect": "Allow",
            "Action": OPERATOR_ROLE_GATED_ACTIONS,
            "Resource": operator_role,
            "Condition": {
                "StringEquals": {"iam:PermissionsBoundary": OPERATOR_ROLE_BOUNDARY_ARN},
                "StringEqualsIfExists": {"iam:PolicyARN": OPERATOR_ROLE_POLICY_ARNS},
            },
        },
        {
            # Every other hub role: created, or given or widened permissions, only
            # while it carries the blueprint boundary (CreateRole checks the
            # requested boundary, the other actions the one the role carries).
            "Sid": "CreateBoundedHubRoles",
            "Effect": "Allow",
            "Action": ROLE_BOUNDARY_GATED_ACTIONS,
            "Resource": bounded_roles,
            "Condition": {"StringEquals": {"iam:PermissionsBoundary": hub_boundary}},
        },
        {
            "Sid": "ManageOmniHubRoles",
            "Effect": "Allow",
            "Action": ROLE_LIFECYCLE_ACTIONS,
            "Resource": managed_roles,
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


# AuditStack is instantiated twice: the platform stage (bin, stack id
# AgenticAI-Platform-AuditStack) and the pipeline's Nonprod stage (stack name
# Nonprod-Audit). AWS::Oam::Sink declares cloudFormationSystemTags, so
# CloudFormation stamps aws:cloudformation:stack-name inside CreateSink and the
# sink lifecycle is pinned to those stacks the way the management document pins
# OUs and SCPs. Extend the list when the AuditStack is deployed under another name.
AUDIT_STACK_NAMES = ["AgenticAI-Platform-AuditStack", "Nonprod-Audit"]
# The hub's keys are deployed by operators (OmniHubKeysStack, `omni-hub-keys`
# stage) and carry agenticai:omni-hub=true; OmniHubStack puts the same tag on
# everything it creates. CloudFormation applies no system tags to KMS keys (the
# live AWS::KMS::Key schema sets cloudFormationSystemTags false), so that tag is
# how the audit document recognizes a hub key: the role may use a key that
# carries it (the AWS::CloudWatch::Space handler encrypts with the space key on
# the caller's behalf) and read key metadata, and never creates, tags or changes
# a key. Tagging a key at creation would need kms:TagResource on every key of
# the account and Region (KMS, "Controlling access to tags"), which is the
# claim-any-key path this keeps out of the pipeline role.
OMNI_HUB_OWNERSHIP_TAG = "agenticai:omni-hub"
APPLICATION_SIGNALS_SLR = (
    "role/aws-service-role/application-signals.cloudwatch.amazonaws.com/"
    "AWSServiceRoleForCloudWatchApplicationSignals"
)


def audit_telemetry_statements(account: str, region: str) -> list[dict[str, Any]]:
    """KMS use, Logs, OAM and Transaction Search prerequisites for the Audit account.

    Every action is one the CloudFormation handlers for AWS::Oam::Sink,
    AWS::CloudWatch::Space and AWS::XRay::TransactionSearchConfig call (registry
    schemas plus the Transaction Search prerequisites), pinned to the hub's
    operator-provisioned keys (ownership tag), the AuditStack's sink and the two
    Transaction Search log groups. No AWS::KMS::Key or AWS::KMS::Alias handler
    action is granted: the hub stage imports its keys (`omni-hub-keys`).
    """
    key_any = arn("kms", region, account, "key/*")
    sink_any = arn("oam", region, account, "sink/*")
    return [
        {
            # The AWS::CloudWatch::Space handler encrypts with the space key on the
            # caller's behalf; only keys that carry the ownership tag.
            "Sid": "UseOmniHubKeys",
            "Effect": "Allow",
            "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
            "Resource": key_any,
            "Condition": {
                "StringEquals": {f"aws:ResourceTag/{OMNI_HUB_OWNERSHIP_TAG}": "true"}
            },
        },
        {
            # The Space and TransactionSearchConfig handlers describe the keys they
            # are given; metadata only, no key material and no change.
            "Sid": "ReadKeyMetadata",
            "Effect": "Allow",
            "Action": "kms:DescribeKey",
            "Resource": key_any,
        },
        {
            # Tag-on-create: CloudFormation puts its reserved stack-name tag in the
            # CreateSink request, which nothing else can, so the dependent
            # TagResource is granted only for that request.
            "Sid": "CreateAuditStackOamSink",
            "Effect": "Allow",
            "Action": ["oam:CreateSink", "oam:TagResource"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {f"aws:RequestTag/{CFN_STACK_NAME_TAG}": AUDIT_STACK_NAMES}
            },
        },
        {
            # Everything on an existing sink, pinned to the sinks the two AuditStack
            # deployments created.
            "Sid": "ManageAuditStackOamSink",
            "Effect": "Allow",
            "Action": [
                "oam:DeleteSink",
                "oam:GetSink",
                "oam:GetSinkPolicy",
                "oam:ListTagsForResource",
                "oam:PutSinkPolicy",
                "oam:TagResource",
                "oam:UntagResource",
            ],
            "Resource": sink_any,
            "Condition": {
                "StringEquals": {f"aws:ResourceTag/{CFN_STACK_NAME_TAG}": AUDIT_STACK_NAMES}
            },
        },
        {
            # AWS::XRay::TransactionSearchConfig creates exactly these two log groups.
            "Sid": "CreateTransactionSearchLogGroups",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy"],
            "Resource": [
                arn("logs", region, account, "log-group:aws/spans:*"),
                arn("logs", region, account, "log-group:/aws/application-signals/data:*"),
            ],
        },
        {
            # Transaction Search prerequisites: application-signals:StartDiscovery
            # creates and reads this service-linked role and a CloudTrail channel
            # (each action only matches its own service's ARN).
            "Sid": "ApplicationSignalsPrerequisites",
            "Effect": "Allow",
            "Action": [
                "cloudtrail:CreateServiceLinkedChannel",
                "iam:CreateServiceLinkedRole",
                "iam:GetRole",
            ],
            "Resource": [
                iam_arn(account, APPLICATION_SIGNALS_SLR),
                arn(
                    "cloudtrail",
                    region,
                    account,
                    "channel/aws-service-channel/application-signals/*",
                ),
            ],
        },
    ]


# What the hub roles' own policies use (OmniHubSpaceConstruct, the evaluation
# execution role, the centralization tag-propagation role and the golden-gate
# runner), and nothing more: the boundary below is their ceiling. Each action is
# placed by the resource type the Service Authorization Reference gives it: an
# action with no resource type is authorized against "*" only, so pinning it to
# an ARN would silently deny it (IAM policy simulator, 2026-11-24).
HUB_ROLE_LOG_GROUP_ACTIONS = [
    "logs:AssociateKmsKey",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:DescribeLogStreams",
    "logs:FilterLogEvents",
    "logs:GetLogEvents",
    "logs:GetQueryResults",
    "logs:IntegrateWithDataset",
    "logs:ListTagsForResource",
    "logs:PutLogEvents",
    "logs:PutRetentionPolicy",
    "logs:StartQuery",
    "logs:TagResource",
    "logs:UntagResource",
]
# No resource type in the Service Authorization Reference (the JSON service
# reference, confirmed in the IAM policy simulator): StartBatchEvaluation and
# ListBatchEvaluations (the batch-evaluate ARN applies only to Get, Stop and
# Delete), the field-index policy calls, DescribeLogGroups, DescribeQueries and
# StopQuery (GetQueryResults and StartQuery take the log group), PutRecords and
# PutMetricData.
HUB_ROLE_RESOURCELESS_ACTIONS = [
    "bedrock-agentcore:ListBatchEvaluations",
    "bedrock-agentcore:StartBatchEvaluation",
    "cloudwatch:PutMetricData",
    "cloudwatch:PutRecords",
    "logs:DescribeIndexPolicies",
    "logs:DescribeLogGroups",
    "logs:DescribeQueries",
    "logs:PutIndexPolicy",
    "logs:StopQuery",
]
HUB_ROLE_EVALUATION_ACTIONS = [
    "bedrock-agentcore:DeleteBatchEvaluation",
    "bedrock-agentcore:GetBatchEvaluation",
    "bedrock-agentcore:GetDataset",
    "bedrock-agentcore:ListDatasetExamples",
    "bedrock-agentcore:ListDatasetVersions",
    "bedrock-agentcore:StopBatchEvaluation",
]


def hub_role_boundary_policy(account: str, region: str) -> dict[str, Any]:
    """Permissions boundary for the roles the Audit account's OmniHubStack creates.

    A boundary grants nothing: it caps what a role's own policies can grant to
    what the hub roles do (Dataset forwarding, evaluations, centralization tag
    propagation, the golden-dataset gate), in this account and Region: log
    groups of the account (any Region, because the tag-propagation role covers a
    backup Region), the account's datasets and batch evaluations, judge models
    and inference profiles, the account's evaluator functions and keys that
    carry the hub ownership tag. Create it once in the Audit account as
    `AgenticAI-OmniHubRoleBoundary`; the audit execution policy refuses to
    create or widen a hub role without it, and never lets it be removed.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "HubLogGroups",
                "Effect": "Allow",
                "Action": HUB_ROLE_LOG_GROUP_ACTIONS,
                "Resource": f"arn:aws:logs:*:{account}:log-group:*",
            },
            {
                "Sid": "HubResourcelessTelemetry",
                "Effect": "Allow",
                "Action": HUB_ROLE_RESOURCELESS_ACTIONS,
                "Resource": "*",
            },
            {
                "Sid": "HubEvaluations",
                "Effect": "Allow",
                "Action": HUB_ROLE_EVALUATION_ACTIONS,
                "Resource": [
                    arn("bedrock-agentcore", region, account, "dataset/*"),
                    arn("bedrock-agentcore", region, account, "batch-evaluate/*"),
                ],
            },
            {
                "Sid": "HubJudgeModels",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    arn("bedrock", region, account, "inference-profile/*"),
                    arn("bedrock", region, account, "application-inference-profile/*"),
                ],
            },
            {
                "Sid": "HubCodeEvaluators",
                "Effect": "Allow",
                "Action": ["lambda:GetFunction", "lambda:InvokeFunction"],
                "Resource": arn("lambda", region, account, "function:*"),
            },
            {
                "Sid": "HubKeys",
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey*"],
                "Resource": arn("kms", region, account, "key/*"),
                "Condition": {
                    "StringEquals": {f"aws:ResourceTag/{OMNI_HUB_OWNERSHIP_TAG}": "true"}
                },
            },
        ],
    }


def audit_policy(account: str, region: str) -> dict[str, Any]:
    """Audit/Observability account: AuditStack (OAM sink) and the `omni-hub` stage."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            *audit_telemetry_statements(account, region),
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
    if args.account_role == "hub-role-boundary":
        return hub_role_boundary_policy(args.account_id, args.region)
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
