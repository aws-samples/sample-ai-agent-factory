/**
 * AgenticApp — per-application composition.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, Tags, CfnOutput } from 'aws-cdk-lib';
import { IVpc, SecurityGroup } from 'aws-cdk-lib/aws-ec2';
import { CfnApplicationInferenceProfile } from 'aws-cdk-lib/aws-bedrock';
import { Construct } from 'constructs';

import { AgentCoreRuntimeConstruct } from '@agenticai/agentcore-runtime';
import { AgentCoreMemoryConstruct } from '@agenticai/agentcore-memory';
import { PLATFORM_ALLOWED_MODELS } from '@agenticai/platform-baselines';

export interface AgenticAppProps {
  readonly vpc: IVpc;
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
  /** Cost-centre tag value applied to the inference profile + all resources. */
  readonly costCentre: string;
  /**
   * Foundation model identifier to copy from. Defaults to the first entry
   * of PLATFORM_ALLOWED_MODELS (Claude Sonnet 4.5).
   */
  readonly modelId?: string;
  /** Memory short-term TTL days. ≤ 365. */
  readonly memoryShortTermTtlDays?: number;
}

export class AgenticApp extends Construct {
  readonly runtime: AgentCoreRuntimeConstruct;
  readonly memory: AgentCoreMemoryConstruct;
  readonly appSg: SecurityGroup;
  readonly inferenceProfile: CfnApplicationInferenceProfile;

  constructor(scope: Construct, id: string, props: AgenticAppProps) {
    super(scope, id);

    const stack = Stack.of(this);
    const { tenantId, agentId, envName } = props;

    // ---- Tag every child resource ----
    Tags.of(this).add('application-id', tenantId);
    Tags.of(this).add('agent-id', agentId);
    Tags.of(this).add('environment', envName);
    Tags.of(this).add('cost-centre', props.costCentre);

    // ---- Per-app SG (R-TEN-022) ----
    this.appSg = new SecurityGroup(this, 'AppSg', {
      vpc: props.vpc,
      description: `Per-app SG for ${tenantId}/${agentId}/${envName}. No cross-app ingress.`,
      allowAllOutbound: false,
    });

    // ---- Runtime + Memory ----
    this.runtime = new AgentCoreRuntimeConstruct(this, 'Runtime', {
      tenantId,
      agentId,
      envName,
    });
    this.memory = new AgentCoreMemoryConstruct(this, 'Memory', {
      tenantId,
      agentId,
      envName,
      shortTermTtlDays: props.memoryShortTermTtlDays,
    });

    // ---- Application inference profile (R-TEN-013, R-TEN-029) ----
    const modelId = props.modelId ?? PLATFORM_ALLOWED_MODELS[0];
    this.inferenceProfile = new CfnApplicationInferenceProfile(this, 'InferenceProfile', {
      inferenceProfileName: `agenticai-${envName}-${tenantId}-${agentId}`,
      // Bedrock ApplicationInferenceProfile description regex:
      //   ^([0-9a-zA-Z:.][ _-]?)+$
      // No slashes, commas, parens, asterisks. Keep safe ASCII + spaces/dashes.
      description: `Application inference profile for ${tenantId}-${agentId} in ${envName}`,
      modelSource: {
        copyFrom: `arn:aws:bedrock:${stack.region}::foundation-model/${modelId}`,
      },
      tags: [
        { key: 'application-id', value: tenantId },
        { key: 'agent-id', value: agentId },
        { key: 'environment', value: envName },
        { key: 'cost-centre', value: props.costCentre },
      ],
    });

    new CfnOutput(this, 'InferenceProfileArn', {
      value: this.inferenceProfile.attrInferenceProfileArn,
      description: `Application inference profile ARN for ${tenantId}/${agentId}.`,
    });
    new CfnOutput(this, 'MemoryNamespace', {
      value: this.memory.namespacePath,
      description: 'Memory namespace root (actorId / memoryStrategyId / sessionId appended at runtime).',
    });
  }
}
