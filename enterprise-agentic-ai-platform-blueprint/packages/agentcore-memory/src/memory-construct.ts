/**
 * AgentCoreMemoryConstruct — per-tenant memory namespace + CMK + retention.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Effect, PolicyStatement, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { buildMemoryNamespacePath } from './namespace-template';

export interface AgentCoreMemoryConstructProps {
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
  /**
   * Short-term memory TTL in days. Spec §3.4.2 L4596 caps at 365.
   * Default 30.
   */
  readonly shortTermTtlDays?: number;
}

export class AgentCoreMemoryConstruct extends Construct {
  readonly namespacePath: string;
  readonly key: Key;
  readonly shortTermTtlDays: number;

  constructor(scope: Construct, id: string, props: AgentCoreMemoryConstructProps) {
    super(scope, id);

    this.shortTermTtlDays = props.shortTermTtlDays ?? 30;
    if (this.shortTermTtlDays > 365) {
      throw new Error(
        `AgentCoreMemory short-term TTL must be ≤ 365 days per spec §3.4.2 L4596 (got ${this.shortTermTtlDays}).`,
      );
    }

    const stack = Stack.of(this);

    this.namespacePath = buildMemoryNamespacePath({
      tenantId: props.tenantId,
      agentId: props.agentId,
      envName: props.envName,
    });

    this.key = new Key(this, 'MemoryKey', {
      alias: `alias/agenticai/memory-${props.envName}-${props.tenantId}-${props.agentId}`,
      description: `AgentCore Memory CMK for ${props.envName}/${props.tenantId}/${props.agentId} (spec §3.4.7).`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(30),
      removalPolicy: RemovalPolicy.RETAIN,
    });
    // Memory resources follow the naming convention
    //   `${tenantId}-${agentId}-${envName}-<suffix>` (matches the alias /
    //   namespace-path shape). The CfnMemory L1 for AgentCore is deferred
    //   (see index.ts header); when it lands, this wildcard suffix covers
    //   the forthcoming resource name. Scoping aws:SourceArn in addition
    //   to aws:SourceAccount closes the same-account confused-deputy window
    //   where any bedrock-agentcore caller could otherwise ask KMS to
    //   decrypt another tenant's material.
    const memoryArnPattern = `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:memory/${props.tenantId}-${props.agentId}-${props.envName}-*`;
    this.key.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowAgentCoreMemoryService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('bedrock-agentcore.amazonaws.com')],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'aws:SourceAccount': stack.account,
          },
          ArnEquals: {
            'aws:SourceArn': memoryArnPattern,
          },
        },
      }),
    );
  }
}
