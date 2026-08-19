/**
 * WorkloadAppStack — deployed alongside WorkloadNetworkStack.
 *
 * Composes the per-workload posture:
 *   - LiteLLM gateway (D-01)
 *   - AgentCore Gateway + API Gateway fronting (§08 Option A)
 *   - AgentCore Identity (Cognito + Token Vault CMK)
 *   - AgenticApp L3 per agent — Runtime + Memory + inference profile
 *   - Bedrock quota-increase requests
 *   - RAG knowledge base per tenant
 *
 * Requires WorkloadNetworkStack to have landed first (VPC + VPCEs + invocation logging).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import { IVpc, SecurityGroup, Vpc } from 'aws-cdk-lib/aws-ec2';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { LiteLLMGatewayConstruct } from '@agenticai/litellm-gateway';
import { AgentCoreGatewayConstruct, ApiGatewayFronting } from '@agenticai/agentcore-gateway';
import { AgentCoreIdentityConstruct } from '@agenticai/agentcore-identity';
import { AgenticApp } from '@agenticai/agentic-app';
import { BedrockQuotaRequestConstruct } from '@agenticai/bedrock-quotas';
import { RagKnowledgeBaseConstruct } from '@agenticai/rag';
import {
  OamSourceLinkConstruct,
  AgenticDashboardConstruct,
  AgenticAlarmsConstruct,
} from '@agenticai/observability';
import { AgenticAppBudgetConstruct } from '@agenticai/cost-allocation';

export interface WorkloadAppStackProps extends StackProps {
  /**
   * VPC ID produced by WorkloadNetworkStack. Resolved via CloudFormation
   * import-value; when the two stacks deploy together the import is implicit.
   */
  readonly vpcId: string;
  /**
   * Subnet ids — at least the 'workload' private subnets.
   */
  readonly workloadSubnetIds: readonly string[];
  readonly vpcCidr: string;
  /**
   * Availability zones covered by the VPC. Must match the source stack
   * (3 AZs per Phase 4 default).
   */
  readonly availabilityZones: readonly string[];
  /**
   * The Bedrock Runtime VPCE id in the same VPC — used to restrict RAG
   * bucket access to in-VPC callers only.
   */
  readonly bedrockRuntimeVpceId: string;
  /**
   * The VPCE-ENI security group id from the network stack (`vpc.vpceEniSg`).
   * Required so LiteLLM's egress rule targets a real SG rather than the
   * historical `pl-0000000000000000` placeholder.
   */
  readonly vpceSecurityGroupId: string;

  readonly envName: string;
  readonly tenantId: string;
  readonly agentId: string;
  readonly costCentre: string;

  /**
   * Optional desired per-account Bedrock RPM quota. Default 100 (non-prod).
   */
  readonly bedrockDesiredRpm?: number;

  /** Audit-account OAM sink ARN (imported from AuditStack). */
  readonly auditOamSinkArn?: string;

  /** Monthly budget alert threshold, USD. Default 500. */
  readonly monthlyBudgetUsd?: number;

  /** Operator notification address for budget alerts. */
  readonly notificationEmail?: string;
}

export class WorkloadAppStack extends Stack {
  readonly vpc: IVpc;
  readonly litellm: LiteLLMGatewayConstruct;
  readonly identity: AgentCoreIdentityConstruct;
  readonly gateway: AgentCoreGatewayConstruct;
  readonly apiGatewayFront: ApiGatewayFronting;
  readonly app: AgenticApp;
  readonly rag: RagKnowledgeBaseConstruct;

  constructor(scope: Construct, id: string, props: WorkloadAppStackProps) {
    super(scope, id, props);

    // Import the VPC produced by WorkloadNetworkStack.
    this.vpc = Vpc.fromVpcAttributes(this, 'ImportedVpc', {
      vpcId: props.vpcId,
      availabilityZones: [...props.availabilityZones],
      isolatedSubnetIds: [...props.workloadSubnetIds],
      vpcCidrBlock: props.vpcCidr,
    });

    // ---- LiteLLM (D-01) ----
    const vpceSg = SecurityGroup.fromSecurityGroupId(this, 'ImportedVpceSg', props.vpceSecurityGroupId, {
      mutable: true,
    });
    this.litellm = new LiteLLMGatewayConstruct(this, 'LiteLLM', {
      vpc: this.vpc,
      bedrockVpceSecurityGroup: vpceSg,
    });

    // ---- AgentCore Identity (Cognito + Token Vault CMK) ----
    this.identity = new AgentCoreIdentityConstruct(this, 'Identity', {
      envName: props.envName,
    });

    // ---- AgentCore Gateway (behind API Gateway per §08) ----
    this.gateway = new AgentCoreGatewayConstruct(this, 'AgentCoreGateway', {
      vpc: this.vpc,
      envName: props.envName,
    });

    // ---- API Gateway fronting (the primary auth boundary) ----
    this.apiGatewayFront = new ApiGatewayFronting(this, 'ApiGwFront', {
      vpc: this.vpc,
      userPool: this.identity.userPool,
      userPoolClientId: this.identity.userPoolClient.userPoolClientId,
      targetAlbListenerArn: this.gateway.albListener.listenerArn,
      targetAlbSecurityGroup: this.gateway.albSg,
    });

    // ---- AgenticApp L3 per tenant/agent ----
    this.app = new AgenticApp(this, 'App', {
      vpc: this.vpc,
      tenantId: props.tenantId,
      agentId: props.agentId,
      envName: props.envName,
      costCentre: props.costCentre,
    });

    // ---- RAG knowledge base (VPCE-only) ----
    this.rag = new RagKnowledgeBaseConstruct(this, 'RagKb', {
      tenantId: props.tenantId,
      kbId: 'primary',
      envName: props.envName,
      approvedVpceId: props.bedrockRuntimeVpceId,
    });

    // ---- Bedrock quota-increase requests ----
    new BedrockQuotaRequestConstruct(this, 'BedrockQuotas', {
      envName: props.envName,
      requests: [
        {
          // Bedrock Runtime RPM for Claude Sonnet 4.5 (indicative code).
          quotaCode: 'L-AGENTICAI-CLAUDE-RPM',
          desiredValue: props.bedrockDesiredRpm ?? 100,
          description: `Requested Bedrock Claude RPM for ${props.envName}`,
        },
      ],
    });

    // ---- Phase 6 — Observability + cost ----
    if (props.auditOamSinkArn) {
      new OamSourceLinkConstruct(this, 'OamSourceLink', {
        sinkArn: props.auditOamSinkArn,
      });
    }

    new AgenticDashboardConstruct(this, 'Dashboard', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      inferenceProfileName: `agenticai-${props.envName}-${props.tenantId}-${props.agentId}`,
    });

    new AgenticAlarmsConstruct(this, 'Alarms', {
      envName: props.envName,
      tenantId: props.tenantId,
      agentId: props.agentId,
      inferenceProfileName: `agenticai-${props.envName}-${props.tenantId}-${props.agentId}`,
    });

    if (props.notificationEmail) {
      new AgenticAppBudgetConstruct(this, 'Budget', {
        tenantId: props.tenantId,
        envName: props.envName,
        monthlyBudgetUsd: props.monthlyBudgetUsd ?? 500,
        notificationEmail: props.notificationEmail,
      });
    }

    // Stack-level cdk-nag suppressions for CDK-generated custom-resource/
    // Lambda helpers and VPC-import noise that can't be authored otherwise.
    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed custom-resource Lambda runtime; tracks aws-cdk-lib.' },
        { id: 'AwsSolutions-IAM4', reason: 'SEC-010: AWSLambdaBasicExecutionRole is the documented CDK custom-resource role.', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'] },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: Provisioning-time only.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CloudFormation surfaces failures.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Control-plane calls to AWS-managed endpoints.' },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: CDK-generated roles use inline policies.' },
        { id: 'AwsSolutions-IAM5', reason: 'SEC-011: Custom-resource account-level APIs have no resource ARN.' },
      ],
      true,
    );

    // ---- Outputs ----
    new CfnOutput(this, 'ApiGatewayUrl', {
      value: `https://${this.apiGatewayFront.api.attrApiEndpoint}`,
      description: 'Primary auth boundary for agent traffic.',
    });
    new CfnOutput(this, 'LiteLLMAlbDns', {
      value: this.litellm.alb.loadBalancerDnsName,
      description: 'Internal LiteLLM ALB DNS (VPC-reachable only).',
    });
    new CfnOutput(this, 'UserPoolId', { value: this.identity.userPool.userPoolId });
    new CfnOutput(this, 'UserPoolClientId', { value: this.identity.userPoolClient.userPoolClientId });
    new CfnOutput(this, 'InferenceProfileArn', { value: this.app.inferenceProfile.attrInferenceProfileArn });
    new CfnOutput(this, 'RagBucketName', { value: this.rag.sourceBucket.bucketName });
  }
}
