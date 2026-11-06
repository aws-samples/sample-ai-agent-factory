/**
 * OmniCentralizedLogsKeyConstruct
 *
 * The customer managed key under which every destination log group created by
 * the organization centralization rule is encrypted in the hub account. The
 * key carries the `LogsManaged=true` tag the centralization service reads and
 * a key policy that lets the Regional CloudWatch Logs principal use it for log
 * groups in this account only.
 *
 * Shared by the full hub (OmniHubSpaceConstruct) and by the adopt-existing-space
 * path of OmniHubStack.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { RemovalPolicy, Stack, Tags } from 'aws-cdk-lib';
import {
  AccountRootPrincipal,
  Effect,
  PolicyDocument,
  PolicyStatement,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface OmniCentralizedLogsKeyConstructProps {
  /** Default RETAIN. Use DESTROY only for throwaway validation. */
  readonly removalPolicy?: RemovalPolicy;
  readonly tags?: Record<string, string>;
}

export class OmniCentralizedLogsKeyConstruct extends Construct {
  readonly key: Key;

  constructor(scope: Construct, id: string, props: OmniCentralizedLogsKeyConstructProps = {}) {
    super(scope, id);
    const stack = Stack.of(this);

    this.key = new Key(this, 'Key', {
      alias: `alias/agenticai/omni-centralized-logs-${stack.region}`,
      description: 'Encrypts every destination log group created by the organization centralization rule in this hub.',
      enableKeyRotation: true,
      removalPolicy: props.removalPolicy ?? RemovalPolicy.RETAIN,
      policy: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'EnableRootKeyAdministration',
            effect: Effect.ALLOW,
            principals: [new AccountRootPrincipal()],
            actions: ['kms:*'],
            resources: ['*'],
          }),
          new PolicyStatement({
            sid: 'AllowCloudWatchLogsUse',
            effect: Effect.ALLOW,
            principals: [new ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
            actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
            resources: ['*'],
            conditions: {
              ArnLike: {
                'kms:EncryptionContext:aws:logs:arn': `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:*`,
              },
            },
          }),
        ],
      }),
    });
    // The centralization service reads this tag to use the key for destination log groups.
    Tags.of(this.key).add('LogsManaged', 'true');
    for (const [key, value] of Object.entries(props.tags ?? {})) {
      Tags.of(this.key).add(key, value);
    }

    NagSuppressions.addResourceSuppressions(this.key, [
      {
        id: 'AwsSolutions-KMS5',
        reason: 'Key rotation is enabled; the CloudWatch Logs grant is conditioned on the encryption context of this account.',
      },
    ]);
  }
}
