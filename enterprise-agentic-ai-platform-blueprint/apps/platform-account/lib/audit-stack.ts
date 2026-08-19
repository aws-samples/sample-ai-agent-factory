/**
 * AuditStack — deployed into the Audit account.
 *
 * Emits the CloudWatch cross-account observability (OAM) sink. Workload
 * and platform accounts create `AWS::Oam::Link` resources pointing at this
 * sink in Phase 6 `packages/observability/`.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { AuditConstruct } from '@agenticai/landing-zone';

export interface AuditStackProps extends StackProps {
  readonly organizationId?: string;
  readonly trustedAccountIds?: readonly string[];
}

export class AuditStack extends Stack {
  readonly audit: AuditConstruct;

  constructor(scope: Construct, id: string, props: AuditStackProps) {
    super(scope, id, props);

    this.audit = new AuditConstruct(this, 'Audit', {
      organizationId: props.organizationId,
      trustedAccountIds: props.trustedAccountIds,
    });
  }
}
