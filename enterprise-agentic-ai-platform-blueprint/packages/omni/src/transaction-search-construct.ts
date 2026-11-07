/**
 * TransactionSearchConstruct
 *
 * Turns on CloudWatch Transaction Search for the account and Region the way
 * the console does: the CloudWatch Logs RESOURCE POLICY that lets X-Ray write
 * spans into `aws/spans` and `/aws/application-signals/data`, then the
 * AWS::XRay::TransactionSearchConfig itself, which depends on it.
 *
 * Observed live (2026-11): in an account that has never had Transaction
 * Search on, AWS::XRay::TransactionSearchConfig alone fails with
 *   403 "XRay does not have permission to call PutLogEvents on the aws/spans
 *   Log Group. Please verify that your CloudWatch Logs Resource Policies are
 *   configured correctly"
 * because the console-created policy is missing. The policy is account-wide
 * (named, at most ten per account and Region), so one named policy per
 * blueprint stack is enough and a second copy in another stack is harmless.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Stack } from 'aws-cdk-lib';
import { CfnResourcePolicy } from 'aws-cdk-lib/aws-logs';
import { CfnTransactionSearchConfig } from 'aws-cdk-lib/aws-xray';
import { Construct } from 'constructs';

export interface TransactionSearchConstructProps {
  /** Indexing percentage (0-100). Default 1. */
  readonly indexingPercentage?: number;
  /**
   * Resource policy name, unique per account and Region. Default
   * `AgenticAI-TransactionSearch-<stack name>` so two blueprint stacks in one
   * account never collide.
   */
  readonly resourcePolicyName?: string;
}

export class TransactionSearchConstruct extends Construct {
  readonly resourcePolicy: CfnResourcePolicy;
  readonly config: CfnTransactionSearchConfig;

  constructor(scope: Construct, id: string, props: TransactionSearchConstructProps = {}) {
    super(scope, id);
    const indexing = props.indexingPercentage ?? 1;
    if (indexing < 0 || indexing > 100) {
      throw new Error('TransactionSearchConstruct indexingPercentage must be between 0 and 100.');
    }
    const stack = Stack.of(this);
    const policyName = props.resourcePolicyName ?? `AgenticAI-TransactionSearch-${stack.stackName}`;
    if (!/^[\w.-]{1,255}$/.test(policyName)) {
      throw new Error(`TransactionSearchConstruct resourcePolicyName '${policyName}' must be 1-255 word characters, dots or hyphens.`);
    }

    // The documented Transaction Search policy, scoped to this account's X-Ray.
    this.resourcePolicy = new CfnResourcePolicy(this, 'XRayLogsPolicy', {
      policyName,
      policyDocument: JSON.stringify({
        Version: '2012-10-17',
        Statement: [
          {
            Sid: 'TransactionSearchXRayAccess',
            Effect: 'Allow',
            Principal: { Service: 'xray.amazonaws.com' },
            Action: 'logs:PutLogEvents',
            Resource: [
              `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:aws/spans:*`,
              `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:/aws/application-signals/data:*`,
            ],
            Condition: {
              ArnLike: { 'aws:SourceArn': `arn:${stack.partition}:xray:${stack.region}:${stack.account}:*` },
              StringEquals: { 'aws:SourceAccount': stack.account },
            },
          },
        ],
      }),
    });

    this.config = new CfnTransactionSearchConfig(this, 'Config', { indexingPercentage: indexing });
    this.config.addDependency(this.resourcePolicy);
  }
}
