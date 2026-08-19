/**
 * AgentCoreRuntimeConstruct — execution role + ECR repo + log group per agent.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Repository, TagMutability } from 'aws-cdk-lib/aws-ecr';
import {
  Effect,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { allowedModelArns } from '@agenticai/platform-baselines';

export interface AgentCoreRuntimeConstructProps {
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
}

export class AgentCoreRuntimeConstruct extends Construct {
  readonly executionRole: Role;
  readonly repo: Repository;
  readonly logGroup: LogGroup;
  readonly kmsKey: Key;

  constructor(scope: Construct, id: string, props: AgentCoreRuntimeConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    // CMK for ECR encryption + log group encryption.
    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/runtime-${props.envName}-${props.tenantId}-${props.agentId}`,
      description: `CMK for AgentCore Runtime ${props.envName}/${props.tenantId}/${props.agentId}.`,
      enableKeyRotation: true,
      pendingWindow: Duration.days(7),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    this.kmsKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchLogs',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
      }),
    );

    // ECR repo with image-tag immutability (spec §3.1.4 L2689-2700 / R-RT-022).
    this.repo = new Repository(this, 'Repo', {
      repositoryName: `agenticai-${props.envName}-${props.tenantId}-${props.agentId}`,
      imageTagMutability: TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      encryptionKey: this.kmsKey,
      emptyOnDelete: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Log group per spec §2.5.3 L2108 pattern — `/agenticai/<app>/*`.
    this.logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: `/agenticai/${props.envName}/${props.tenantId}/${props.agentId}`,
      retention: RetentionDays.THREE_MONTHS,
      encryptionKey: this.kmsKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Execution role — under D-01 this does NOT include direct bedrock:InvokeModel
    // (LiteLLM task role holds it). Kept here as Deny-on-null belt-and-braces
    // + CloudWatch + ECR pulls.
    this.executionRole = new Role(this, 'ExecutionRole', {
      roleName: `AgenticAI-${props.envName}-${props.tenantId}-${props.agentId}-exec`,
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'AgentCore Runtime execution role (D-01 mode: reaches LiteLLM, not Bedrock directly).',
    });
    this.executionRole.addToPolicy(
      new PolicyStatement({
        sid: 'CloudWatchLogs',
        effect: Effect.ALLOW,
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents', 'logs:CreateLogGroup'],
        resources: [this.logGroup.logGroupArn, `${this.logGroup.logGroupArn}:*`],
      }),
    );
    this.executionRole.addToPolicy(
      new PolicyStatement({
        sid: 'EcrImagePull',
        effect: Effect.ALLOW,
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchCheckLayerAvailability',
        ],
        resources: [this.repo.repositoryArn],
      }),
    );
    // Belt-and-braces: even if something ever grants InvokeModel to this role,
    // the guardrail deny still applies. Preserves spec §2.2.3 R-BED-028.
    // SEC (security review — INTENTIONAL): resources:['*'] is correct on
    // a DENY — it must cover every Bedrock resource so no model/profile can
    // escape guardrail enforcement. Narrowing it would be a security
    // regression; a broad Deny only ever removes access.
    this.executionRole.addToPolicy(
      new PolicyStatement({
        sid: 'DenyDirectBedrockWithoutGuardrail',
        effect: Effect.DENY,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: ['*'],
        conditions: {
          Null: { 'bedrock:GuardrailIdentifier': 'true' },
        },
      }),
    );

    // If D-01 is ever relaxed, allow-listed models get scoped access here.
    void allowedModelArns;
  }
}
