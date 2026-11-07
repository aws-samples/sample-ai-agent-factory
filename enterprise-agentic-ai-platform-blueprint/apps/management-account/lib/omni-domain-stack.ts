/**
 * OmniDomainStack — deployed ONCE to the AWS Organizations management account.
 *
 * Creates the CloudWatch Omni organization domain (single sign-in URL bound to
 * IAM Identity Center) that every member-account space links to (ADR-15).
 * Only the management account can create an organization domain; everything
 * else in the Omni hub model (centralization rule, spaces, grants) can be
 * owned by the Audit/Observability account as CloudWatch delegated
 * administrator.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, type StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";

import { OmniOrganizationDomainConstruct } from "@agenticai/omni";

export interface OmniDomainStackProps extends StackProps {
  /** Organization-unique domain name (becomes `<name>.cloudwatch-omni.global.app.aws`). */
  readonly domainName: string;
  /** IAM Identity Center instance ARN in this Region. */
  readonly identityCenterInstanceArn: string;
  /** Also allow IAM principal sign-in (console deep links). Default false. */
  readonly allowIamSignIn?: boolean;
  readonly tags?: Record<string, string>;
}

export class OmniDomainStack extends Stack {
  readonly domain: OmniOrganizationDomainConstruct;

  constructor(scope: Construct, id: string, props: OmniDomainStackProps) {
    const { domainName, identityCenterInstanceArn, allowIamSignIn, tags, ...stackProps } = props;
    super(scope, id, stackProps);

    this.domain = new OmniOrganizationDomainConstruct(this, "OmniDomain", {
      domainName,
      identityCenterInstanceArn,
      identityProviders: allowIamSignIn ? ["IDC", "IAM"] : ["IDC"],
      tags,
    });
  }
}
