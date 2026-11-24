/**
 * createOmniSpaceKey
 *
 * The customer managed key of a CloudWatch Omni space and its Dataset: the
 * documented Omni key policy (CloudWatch service-principal statements for the
 * space, the Dataset and the Intelligence branch key) and the `cw-omni=true`
 * tag the AWS-managed CloudWatchOmniSpaceAccessPolicy keys on.
 *
 * A function rather than a construct, so the key keeps the CloudFormation
 * logical id (`<scope>/<id>/Resource`) it had when OmniHubSpaceConstruct
 * created it inline and existing hubs see no replacement. Two callers:
 *   - OmniHubSpaceConstruct (default mode) passes `roleGrants`: the key policy
 *     also names the space operator and Dataset integration roles created in
 *     the same stack, exactly as before;
 *   - OmniHubKeysStack (deployed by operators, ahead of the hub) omits them:
 *     KMS refuses a key policy that names a role which does not exist yet, so
 *     those roles get their key use through IAM policies instead (the root
 *     statement delegates to IAM).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { RemovalPolicy, Stack, Tags } from 'aws-cdk-lib';
import {
  AccountRootPrincipal,
  ArnPrincipal,
  Effect,
  PolicyDocument,
  PolicyStatement,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key, KeySpec, KeyUsage } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface OmniSpaceKeyRoleGrants {
  /** ARN of the space operator role (`DataAccessRoleArn`), created in the same stack. */
  readonly operatorRoleArn: string;
  /** ARN of the Dataset integration execution role, created in the same stack. */
  readonly datasetIntegrationRoleArn: string;
}

export interface OmniSpaceKeyProps {
  /** Default RETAIN. Use DESTROY only for throwaway validation. */
  readonly removalPolicy?: RemovalPolicy;
  /** Allocation tags. */
  readonly tags?: Record<string, string>;
  /**
   * Key-policy statements for the operator and Dataset integration roles.
   * Only for roles that exist in the same stack as the key (see above).
   */
  readonly roleGrants?: OmniSpaceKeyRoleGrants;
}

export function createOmniSpaceKey(scope: Construct, id: string, props: OmniSpaceKeyProps = {}): Key {
  const stack = Stack.of(scope);
  const spaceArnPattern = `arn:${stack.partition}:cloudwatch:${stack.region}:${stack.account}:space/*`;
  const datasetArn = `arn:${stack.partition}:cloudwatch:${stack.region}:${stack.account}:dataset/default`;
  const cloudwatchService = new ServicePrincipal('cloudwatch.amazonaws.com');

  const statements: PolicyStatement[] = [
    new PolicyStatement({
      sid: 'EnableRootKeyAdministration',
      effect: Effect.ALLOW,
      principals: [new AccountRootPrincipal()],
      actions: ['kms:*'],
      resources: ['*'],
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchIntelligenceDescribeKey',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:DescribeKey'],
      resources: ['*'],
      conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchIntelligenceEncryptionService',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:GenerateDataKey', 'kms:Encrypt', 'kms:Decrypt'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'kms:EncryptionContext:aws:cloudwatch:account': stack.account },
      },
    }),
    new PolicyStatement({
      sid: 'AllowOmniIntelligenceBranchKey',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: [
        'kms:GenerateDataKey',
        'kms:GenerateDataKeyWithoutPlaintext',
        'kms:Encrypt',
        'kms:Decrypt',
        'kms:ReEncrypt*',
      ],
      resources: ['*'],
      conditions: {
        StringEquals: { 'kms:EncryptionContext:aws-crypto-ec:aws:cloudwatch:account': stack.account },
      },
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchDatasetDescribeKey',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:DescribeKey'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'aws:SourceAccount': stack.account },
        ArnLike: { 'aws:SourceArn': datasetArn },
      },
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchDatasetEncryption',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:GenerateDataKey', 'kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*'],
      resources: ['*'],
      conditions: {
        StringEquals: {
          'aws:SourceAccount': stack.account,
          'kms:EncryptionContext:aws:cloudwatch:arn': datasetArn,
        },
        ArnLike: { 'aws:SourceArn': datasetArn },
      },
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchSpaceDescribeKey',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:DescribeKey'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'aws:SourceAccount': stack.account },
        ArnLike: { 'aws:SourceArn': spaceArnPattern },
      },
    }),
    new PolicyStatement({
      sid: 'AllowCloudWatchSpaceEncryptionService',
      effect: Effect.ALLOW,
      principals: [cloudwatchService],
      actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'aws:SourceAccount': stack.account },
        StringLike: { 'kms:EncryptionContext:aws:cloudwatch:arn': spaceArnPattern },
        ArnLike: { 'aws:SourceArn': spaceArnPattern },
      },
    }),
  ];

  if (props.roleGrants) {
    statements.push(
      new PolicyStatement({
        sid: 'AllowCallerDatasetForwarding',
        effect: Effect.ALLOW,
        principals: [new ArnPrincipal(props.roleGrants.datasetIntegrationRoleArn)],
        actions: ['kms:GenerateDataKey', 'kms:Encrypt', 'kms:Decrypt'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'kms:EncryptionContext:aws:cloudwatch:arn': datasetArn,
            'kms:ViaService': `cloudwatch.${stack.region}.amazonaws.com`,
          },
        },
      }),
      new PolicyStatement({
        sid: 'AllowOperatorRoleDecrypt',
        effect: Effect.ALLOW,
        principals: [new ArnPrincipal(props.roleGrants.operatorRoleArn)],
        actions: ['kms:Decrypt'],
        resources: ['*'],
        conditions: { StringEquals: { 'kms:ViaService': `cloudwatch.${stack.region}.amazonaws.com` } },
      }),
      new PolicyStatement({
        sid: 'AllowOperatorRoleIntelligenceDecrypt',
        effect: Effect.ALLOW,
        principals: [new ArnPrincipal(props.roleGrants.operatorRoleArn)],
        actions: ['kms:Decrypt'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'kms:EncryptionContext:aws-crypto-ec:aws:cloudwatch:account': stack.account },
        },
      }),
    );
  }

  const key = new Key(scope, id, {
    alias: `alias/agenticai/omni-space-${stack.region}`,
    description: 'CloudWatch Omni space and Dataset encryption (documented Omni key policy).',
    keySpec: KeySpec.SYMMETRIC_DEFAULT,
    keyUsage: KeyUsage.ENCRYPT_DECRYPT,
    enableKeyRotation: true,
    removalPolicy: props.removalPolicy ?? RemovalPolicy.RETAIN,
    policy: new PolicyDocument({ statements }),
  });
  // The AWS-managed space access policy keys its kms:Decrypt on this tag.
  Tags.of(key).add('cw-omni', 'true');
  for (const [tagKey, value] of Object.entries(props.tags ?? {})) {
    Tags.of(key).add(tagKey, value);
  }
  return key;
}
