/**
 * BedrockInvocationLoggingConstruct
 *
 * Creates the resources required by spec §2.4.7 L1968-1984:
 *   - CloudWatch log group `/agenticai/bedrock-invocations` (CMK-encrypted,
 *     90-day retention; longer retention lands in Phase 6 via the observability
 *     stack's subscription-filter target).
 *   - IAM service role `AgenticAI-BedrockInvocationLogging` that Bedrock
 *     assumes to write records.
 *   - Custom resource that calls `bedrock:PutModelInvocationLoggingConfiguration`
 *     with `textDataDeliveryEnabled=true, imageDataDeliveryEnabled=false,
 *     embeddingDataDeliveryEnabled=false` per R-BED-037.
 *
 * Callers: Phase 4 WorkloadNetworkStack instantiates one per workload account.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { RemovalPolicy, Stack, Duration } from 'aws-cdk-lib';
import {
  Effect,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface BedrockInvocationLoggingProps {
  /**
   * Retention in days for `/agenticai/bedrock-invocations`. Defaults to 90.
   * Records shipped via subscription filter to the Log Archive in Phase 6.
   */
  readonly retentionDays?: RetentionDays;

  /**
   * Override the log group name. Default '/agenticai/bedrock-invocations'
   * (spec §2.4.7 L1970).
   */
  readonly logGroupName?: string;
}

export class BedrockInvocationLoggingConstruct extends Construct {
  readonly logGroupKey: Key;
  readonly logGroup: LogGroup;
  readonly serviceRole: Role;

  constructor(scope: Construct, id: string, props: BedrockInvocationLoggingProps = {}) {
    super(scope, id);

    const logGroupName = props.logGroupName ?? '/agenticai/bedrock-invocations';

    // ---- CMK for log-group encryption ----
    this.logGroupKey = new Key(this, 'LogGroupKey', {
      alias: 'alias/agenticai/bedrock-invocations',
      description: 'CMK for Bedrock Model Invocation Logging log group.',
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.DESTROY,
      pendingWindow: Duration.days(7),
    });

    // Allow CloudWatch Logs in this region to use the key.
    this.logGroupKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchLogs',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${Stack.of(this).region}.amazonaws.com`)],
        actions: [
          'kms:Encrypt*',
          'kms:Decrypt*',
          'kms:ReEncrypt*',
          'kms:GenerateDataKey*',
          'kms:Describe*',
        ],
        resources: ['*'],
      }),
    );

    // ---- Log group (R-BED-036) ----
    this.logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName,
      retention: props.retentionDays ?? RetentionDays.THREE_MONTHS,
      encryptionKey: this.logGroupKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---- IAM role Bedrock assumes to write ----
    this.serviceRole = new Role(this, 'ServiceRole', {
      roleName: 'AgenticAI-BedrockInvocationLogging',
      assumedBy: new ServicePrincipal('bedrock.amazonaws.com'),
      description: 'Role Bedrock assumes to deliver model invocation logs to CloudWatch Logs (spec §2.4.7).',
      inlinePolicies: {
        logging: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: 'PutLogs',
              effect: Effect.ALLOW,
              actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
              resources: [this.logGroup.logGroupArn],
            }),
          ],
        }),
      },
    });

    // Service role uses an inline policy for the single PutLog statement —
    // single-purpose role, managed-policy indirection adds no auditability.
    NagSuppressions.addResourceSuppressions(
      this.serviceRole,
      [
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-005: Same rationale as GuardrailAdminRole — single-purpose service role, inline policy keeps scope visible. Bedrock assumes this role only for PutLog operations.',
        },
      ],
      true,
    );

    // ---- Configure Bedrock invocation logging via custom resource ----
    const configure = new AwsCustomResource(this, 'ConfigureInvocationLogging', {
      resourceType: 'Custom::BedrockInvocationLogging',
      onCreate: {
        service: 'Bedrock',
        action: 'putModelInvocationLoggingConfiguration',
        parameters: {
          loggingConfig: {
            cloudWatchConfig: {
              logGroupName,
              roleArn: this.serviceRole.roleArn,
            },
            textDataDeliveryEnabled: true,
            imageDataDeliveryEnabled: false,
            embeddingDataDeliveryEnabled: false,
            videoDataDeliveryEnabled: false,
          },
        },
        physicalResourceId: PhysicalResourceId.of('AgenticAIBedrockLogging'),
      },
      onUpdate: {
        service: 'Bedrock',
        action: 'putModelInvocationLoggingConfiguration',
        parameters: {
          loggingConfig: {
            cloudWatchConfig: {
              logGroupName,
              roleArn: this.serviceRole.roleArn,
            },
            textDataDeliveryEnabled: true,
            imageDataDeliveryEnabled: false,
            embeddingDataDeliveryEnabled: false,
            videoDataDeliveryEnabled: false,
          },
        },
        physicalResourceId: PhysicalResourceId.of('AgenticAIBedrockLogging'),
      },
      onDelete: {
        service: 'Bedrock',
        action: 'deleteModelInvocationLoggingConfiguration',
        ignoreErrorCodesMatching: 'ValidationException',
      },
      policy: AwsCustomResourcePolicy.fromStatements([
        new PolicyStatement({
          actions: [
            'bedrock:PutModelInvocationLoggingConfiguration',
            'bedrock:DeleteModelInvocationLoggingConfiguration',
            'bedrock:GetModelInvocationLoggingConfiguration',
          ],
          resources: ['*'],
        }),
        new PolicyStatement({
          actions: ['iam:PassRole'],
          resources: [this.serviceRole.roleArn],
        }),
      ]),
    });

    // CDK's AwsCustomResource creates a Lambda + role behind the scenes we
    // cannot directly control. Suppress the expected nag warnings on those
    // generated resources. cdk-nag stopped recognising these as "infrastructure"
    // in 2.28+, so suppress inline at the construct root.
    NagSuppressions.addResourceSuppressions(
      configure,
      [
        {
          id: 'AwsSolutions-L1',
          reason:
            'SEC-006: Lambda runtime for cdk-lib AwsCustomResource is managed by the CDK; upgrading is the CDK team\'s responsibility. Will pick up automatically on CDK bump.',
        },
        {
          id: 'NIST.800.53.R5-LambdaConcurrency',
          reason:
            'SEC-007: Provisioning-time one-shot Lambda (runs during CloudFormation create/update/delete). Concurrency caps would cause stack failures. Invoked only by CloudFormation.',
        },
        {
          id: 'NIST.800.53.R5-LambdaDLQ',
          reason:
            'SEC-008: DLQ unnecessary for CloudFormation-managed custom resources — CFN surfaces failures directly and rolls back.',
        },
        {
          id: 'NIST.800.53.R5-LambdaInsideVPC',
          reason:
            'SEC-009: Custom resource calls the Bedrock control plane (public AWS endpoint) during stack deploy. Placing it in the workload VPC would require additional VPCEs just for stack deploys; the control-plane endpoint is already IAM-authenticated and AWS-managed.',
        },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          reason:
            'SEC-010: AWSLambdaBasicExecutionRole is the documented role for Lambda-backed custom resources; allows CloudWatch Logs write only.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason:
            'SEC-011: PutModelInvocationLoggingConfiguration API is an account-level setting and does not support resource-level scoping. No workload-visible ARN exists.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-005: Inline policy on CDK-generated custom-resource role — scoped to the exact Bedrock action + service-role PassRole. Generated by CDK framework; we cannot control the shape.',
        },
      ],
      true,
    );
  }
}
