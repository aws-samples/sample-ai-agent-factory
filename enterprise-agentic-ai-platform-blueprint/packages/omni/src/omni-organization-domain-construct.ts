/**
 * OmniOrganizationDomainConstruct
 *
 * Deployed ONCE in the AWS Organizations management account. Creates the
 * CloudWatch Omni organization domain (the single sign-in URL and identity
 * boundary for every space in the Organization) and the customer-provided
 * domain access role the service requires.
 *
 * Constraints carried from the user guide:
 *   - only the management account can create an organization domain;
 *   - with IAM Identity Center, the domain must live in the Identity Center
 *     primary Region, and spaces in other Regions need multi-Region replication;
 *   - trusted access between AWS Organizations and CloudWatch must be enabled
 *     (organizations:EnableAWSServiceAccess + iam:CreateServiceLinkedRole).
 *
 * The pinned aws-cdk-lib (2.272.0) has no L1 for AWS::CloudWatch::DomainForOrganization,
 * so the resource is declared with CfnResource against the documented schema.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, CfnResource, Stack, Tags, Validations } from 'aws-cdk-lib';
import { ManagedPolicy, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { assertOmniDomainName, omniDomainUrl } from './naming';

export interface OmniOrganizationDomainConstructProps {
  /** Organization-unique domain name; becomes `<name>.cloudwatch-omni.global.app.aws`. */
  readonly domainName: string;
  /**
   * Identity providers for sign-in. `IDC` requires `identityCenterInstanceArn`.
   * Default `['IDC']`. Add `IAM` only when IAM-principal deep links are wanted.
   */
  readonly identityProviders?: readonly ('IDC' | 'IAM')[];
  /** IAM Identity Center instance ARN (`arn:aws:sso:::instance/ssoins-...`). */
  readonly identityCenterInstanceArn?: string;
  /** Override the domain access role name. Default `CloudWatchOmniDomainAccessRole`. */
  readonly domainAccessRoleName?: string;
  /** Allocation tags applied to the domain and the role. */
  readonly tags?: Record<string, string>;
}

export class OmniOrganizationDomainConstruct extends Construct {
  readonly domain: CfnResource;
  readonly domainAccessRole: Role;
  /** `d-...` identifier every member-account space needs. */
  readonly domainId: string;
  readonly domainEndpointUrl: string;

  constructor(scope: Construct, id: string, props: OmniOrganizationDomainConstructProps) {
    super(scope, id);

    assertOmniDomainName(props.domainName);
    const identityProviders = props.identityProviders ?? ['IDC'];
    if (identityProviders.length === 0 || identityProviders.length > 2) {
      throw new Error('OmniOrganizationDomainConstruct identityProviders must list one or two of IDC, IAM.');
    }
    if (identityProviders.includes('IDC') && !props.identityCenterInstanceArn) {
      throw new Error(
        'OmniOrganizationDomainConstruct: identityCenterInstanceArn is required when identityProviders includes IDC.',
      );
    }
    if (
      props.identityCenterInstanceArn &&
      !/^arn:aws[a-z-]*:sso:::instance\/[a-zA-Z0-9-]+$/.test(props.identityCenterInstanceArn)
    ) {
      throw new Error(
        `OmniOrganizationDomainConstruct identityCenterInstanceArn '${props.identityCenterInstanceArn}' is not an IAM Identity Center instance ARN.`,
      );
    }

    const stack = Stack.of(this);

    // The domain access role is customer-created and passed to the service.
    // It trusts the CloudWatch Omni service principal and carries the AWS
    // managed CloudWatchOmniDomainAccessPolicy (organization domain administration).
    this.domainAccessRole = new Role(this, 'DomainAccessRole', {
      roleName: props.domainAccessRoleName ?? 'CloudWatchOmniDomainAccessRole',
      description: 'CloudWatch Omni organization domain access role (customer-provided; passed as DomainAccessRoleArn).',
      assumedBy: new ServicePrincipal('cloudwatch.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
          ArnLike: {
            'aws:SourceArn': `arn:${stack.partition}:cloudwatch:${stack.region}:${stack.account}:organization-domain/*`,
          },
        },
      }),
      managedPolicies: [ManagedPolicy.fromAwsManagedPolicyName('CloudWatchOmniDomainAccessPolicy')],
    });

    const properties: Record<string, unknown> = {
      Name: props.domainName,
      DomainAccessRoleArn: this.domainAccessRole.roleArn,
      IdentityProviders: [...identityProviders],
    };
    if (props.identityCenterInstanceArn) {
      properties.IdentityProviderConfiguration = {
        IdentityCenterConfiguration: {
          IdentityCenterInstanceArn: props.identityCenterInstanceArn,
        },
      };
    }
    if (props.tags) {
      properties.Tags = Object.entries(props.tags).map(([Key, Value]) => ({ Key, Value }));
    }

    this.domain = new CfnResource(this, 'Domain', {
      type: 'AWS::CloudWatch::DomainForOrganization',
      properties,
    });
    this.domain.node.addDependency(this.domainAccessRole);

    // The CDK CloudFormation validator's bundled schema predates AWS::CloudWatch::DomainForOrganization;
    // CloudFormation itself validates the resource at deploy time.
    Validations.of(this.domain).acknowledge({
      id: 'CloudFormation-Validate::F3006',
      reason: 'SEC-036: AWS::CloudWatch::DomainForOrganization is a 2026 resource type absent from the bundled validator schema; the live deploy is the schema gate.',
    });
    this.domainId = this.domain.getAtt('DomainId').toString();
    this.domainEndpointUrl = this.domain.getAtt('DomainEndpointUrl').toString();

    if (props.tags) {
      for (const [key, value] of Object.entries(props.tags)) {
        Tags.of(this.domainAccessRole).add(key, value);
      }
    }

    new CfnOutput(this, 'OmniDomainId', {
      value: this.domainId,
      description: 'CloudWatch Omni organization domain id; pass to every member-account space.',
    });
    new CfnOutput(this, 'OmniDomainUrl', {
      value: this.domainEndpointUrl,
      description: `CloudWatch Omni sign-in URL (expected ${omniDomainUrl(props.domainName)}).`,
    });

    NagSuppressions.addResourceSuppressions(this.domainAccessRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'SEC-032: CloudWatchOmniDomainAccessPolicy is the AWS managed policy the service documents for the customer-provided organization domain access role; no equivalent customer-managed policy is published.',
      },
    ]);
  }
}
