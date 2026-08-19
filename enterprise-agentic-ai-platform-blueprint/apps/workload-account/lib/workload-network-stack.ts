/**
 * WorkloadNetworkStack — deployed to `agenticai-<app>-{nonprod,prod}`.
 *
 * Agentic VPC (9 VPCEs, SG pair, no IGW/NAT) + Bedrock Model Invocation
 * Logging. Provides the Phase 4 exit — a workload account that can be
 * reached only through VPCEs and whose inference is guardrailed.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack, StackProps, CfnOutput } from 'aws-cdk-lib';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { AgenticVpcConstruct } from '@agenticai/agentic-vpc';
import { BedrockInvocationLoggingConstruct } from '@agenticai/bedrock-invocation-logging';

export interface WorkloadNetworkStackProps extends StackProps {
  readonly vpcCidr?: string;
  readonly enableBrowserInternetEgress?: boolean;
}

export class WorkloadNetworkStack extends Stack {
  readonly vpc: AgenticVpcConstruct;
  readonly invocationLogging: BedrockInvocationLoggingConstruct;

  constructor(scope: Construct, id: string, props: WorkloadNetworkStackProps = {}) {
    super(scope, id, props);

    this.vpc = new AgenticVpcConstruct(this, 'Vpc', {
      vpcCidr: props.vpcCidr,
      enableBrowserInternetEgress: props.enableBrowserInternetEgress,
    });

    this.invocationLogging = new BedrockInvocationLoggingConstruct(this, 'InvocationLogging');

    // Stack-wide suppressions for CDK-generated custom-resource and
    // flow-log helpers — these resources live outside the constructs we
    // authored and CDK regenerates them on every synth. Using stack-scoped
    // addStackSuppressions with appliesTo filters keeps the blast radius
    // to matching rule+resource combinations only.
    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'AwsSolutions-L1', reason: 'SEC-006: Applies only to CDK-managed custom-resource Lambdas; runtime tracks aws-cdk-lib bumps.' },
        { id: 'AwsSolutions-IAM4', reason: 'SEC-010: AWSLambdaBasicExecutionRole is the documented role for CDK custom-resource Lambdas.', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'] },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CDK custom-resource Lambdas run only during stack deploy/rollback; concurrency caps would break stack deploys.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CloudFormation-managed custom resources surface failures + rollback; DLQ would never consume.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: CDK custom-resource Lambdas call AWS control plane (Bedrock, etc.) via IAM-auth public endpoint.' },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: CDK-generated roles (custom-resource + FlowLog + service) use inline policies whose shape we cannot control.' },
      ],
      true,
    );

    new CfnOutput(this, 'VpcId', { value: this.vpc.vpc.vpcId });
    new CfnOutput(this, 'BedrockRuntimeVpceId', {
      value: this.vpc.endpoints.bedrockRuntime.vpcEndpointId,
    });
    new CfnOutput(this, 'VpceSecurityGroupId', {
      value: this.vpc.vpceEniSg.securityGroupId,
    });
    new CfnOutput(this, 'InvocationLogGroup', { value: this.invocationLogging.logGroup.logGroupName });
  }
}
