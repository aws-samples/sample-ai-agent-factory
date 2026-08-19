/**
 * LogArchiveStack — deployed into the Log Archive account.
 *
 * Emits the centralised archive + CUR buckets and the cross-account CWL
 * destination (spec §2.1.3 + §2.1.4 L357-358 / R-ARCH-018 and R-ARCH-023).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

import { LogArchiveConstruct } from '@agenticai/landing-zone';

export interface LogArchiveStackProps extends StackProps {
  readonly organizationId: string;
  readonly workloadAccountIds: readonly string[];
  readonly retainOnDelete?: boolean;
}

export class LogArchiveStack extends Stack {
  readonly logArchive: LogArchiveConstruct;

  constructor(scope: Construct, id: string, props: LogArchiveStackProps) {
    super(scope, id, props);

    this.logArchive = new LogArchiveConstruct(this, 'LogArchive', {
      organizationId: props.organizationId,
      workloadAccountIds: props.workloadAccountIds,
      retainOnDelete: props.retainOnDelete,
    });
  }
}
