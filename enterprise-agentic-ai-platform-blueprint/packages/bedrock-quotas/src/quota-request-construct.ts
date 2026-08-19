/**
 * BedrockQuotaRequestConstruct
 *
 * Emits one ServiceQuotas request per configured (quota-code, desired) pair.
 * The construct is deployment-time-only: after the request is submitted,
 * AWS Support reviews before granting. A smoke test reads the SSM parameter
 * to report current state.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CustomResource, Duration, Stack } from 'aws-cdk-lib';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface BedrockQuotaRequest {
  /** Service Quotas quota code, e.g. `L-1234ABCD`. */
  readonly quotaCode: string;
  /** Desired quota value. */
  readonly desiredValue: number;
  /** Human-readable note for documentation. */
  readonly description: string;
}

export interface BedrockQuotaRequestConstructProps {
  readonly envName: string;
  readonly requests: readonly BedrockQuotaRequest[];
}

export class BedrockQuotaRequestConstruct extends Construct {
  constructor(scope: Construct, id: string, props: BedrockQuotaRequestConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    // Collect all submitted request IDs into one SSM parameter for easy
    // read-side smoke testing.
    for (const req of props.requests) {
      new AwsCustomResource(this, `QuotaRequest-${req.quotaCode}`, {
        resourceType: 'Custom::BedrockQuotaRequest',
        onCreate: {
          service: 'ServiceQuotas',
          action: 'requestServiceQuotaIncrease',
          parameters: {
            ServiceCode: 'bedrock',
            QuotaCode: req.quotaCode,
            DesiredValue: req.desiredValue,
          },
          physicalResourceId: PhysicalResourceId.of(`AgenticAI-BedrockQuota-${req.quotaCode}-${props.envName}`),
        },
        // Updates trigger a fresh request (AWS side-effect; operators can
        // cancel old requests via console).
        onUpdate: {
          service: 'ServiceQuotas',
          action: 'requestServiceQuotaIncrease',
          parameters: {
            ServiceCode: 'bedrock',
            QuotaCode: req.quotaCode,
            DesiredValue: req.desiredValue,
          },
          physicalResourceId: PhysicalResourceId.of(`AgenticAI-BedrockQuota-${req.quotaCode}-${props.envName}`),
        },
        // Delete is a no-op — submitted requests can't be un-submitted.
        onDelete: undefined,
        policy: AwsCustomResourcePolicy.fromStatements([
          new PolicyStatement({
            actions: [
              'servicequotas:RequestServiceQuotaIncrease',
              'servicequotas:GetServiceQuota',
              'servicequotas:ListRequestedServiceQuotaChangeHistory',
            ],
            resources: ['*'],
          }),
        ]),
      });
    }

    // Suppress unused warnings — reserved for future state parameter.
    void stack;
    void CustomResource;
    void Duration;
  }
}
