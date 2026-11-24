/**
 * OmniHubSpaceConstruct
 *
 * The CloudWatch Omni fleet space for one Region, deployed in the
 * Audit/Observability account that receives centralized telemetry (ADR-15).
 * It creates everything that has a CloudFormation type:
 *
 *   - the space operator role (`DataAccessRoleArn`), carrying
 *     CloudWatchOmniSpaceAccessPolicy as both policy and permissions boundary
 *     (the service does the same), plus the optional inference and AWS
 *     integration policies when explicitly enabled (ADR-16);
 *   - the Dataset integration execution role (assumed by logs.amazonaws.com)
 *     scoped to the log groups the hub forwards into the Dataset;
 *   - the AgentCore evaluation execution role for Omni online evaluations
 *     (reads the centralized span groups);
 *   - a customer managed key for the space and Dataset with the documented
 *     key policy, tagged `cw-omni` (`createOmniSpaceKey`);
 *   - a second customer managed key for centralized destination log groups,
 *     tagged `LogsManaged=true`, exported for the organization centralization
 *     rule;
 *     or, with `existingSpaceKey` and `existingCentralizedLogsKey`, neither:
 *     the keys were deployed ahead of the hub by operators (OmniHubKeysStack)
 *     and the hub only uses them, which is what the Audit account's scoped
 *     CloudFormation execution policy allows;
 *   - Transaction Search in this account and Region (required for spans to
 *     arrive and for trace centralization);
 *   - the space itself (AWS::CloudWatch::Space, declared with CfnResource
 *     because the pinned aws-cdk-lib has no L1 yet).
 *
 * Two operations have no CloudFormation type and are applied by
 * `scripts/omni/omni_space_bootstrap.py` after deploy: the Dataset integration
 * (observabilityadmin CreateDatasetIntegration) and the access grants
 * (cloudwatchomni CreateAccessGrant). The construct emits the inputs they need.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, CfnResource, Duration, RemovalPolicy, Stack, Tags, Token, Validations } from 'aws-cdk-lib';
import {
  Effect,
  IManagedPolicy,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import type { IKey } from 'aws-cdk-lib/aws-kms';
import { CfnTransactionSearchConfig } from 'aws-cdk-lib/aws-xray';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { EvaluationExecutionRoleConstruct } from '@agenticai/agentcore-evaluations';

import { assertOmniHostingRegion, assertOmniSpaceName } from './naming';
import { OmniCentralizedLogsKeyConstruct } from './omni-centralized-logs-key-construct';
import { createOmniSpaceKey } from './omni-space-key';
import { TransactionSearchConstruct } from './transaction-search-construct';

export interface OmniHubSpaceConstructProps {
  /** Space name, `^[a-z0-9]+(-[a-z0-9]+)*$`, 3-64 chars. */
  readonly spaceName: string;
  /** Organization domain id (`d-...`) from the management-account domain stack. */
  readonly domainId: string;
  /**
   * Operator role name. For an organization domain the service expects one
   * shared role per account; the documented name is `CloudWatchOmniOperatorRole`.
   */
  readonly operatorRoleName?: string;
  /**
   * ADR-16. Attach CloudWatchOmniModelInferencePolicy so the space can run the
   * prompt playground and custom judge evaluators. Default false.
   */
  readonly allowModelInference?: boolean;
  /**
   * Attach CloudWatchOmniAWSIntegrationPolicy so Context Graph can discover
   * resources through AWS Config. Default false (regulated profile).
   */
  readonly enableAwsIntegrationDiscovery?: boolean;
  /**
   * Log-group name prefixes (without account id) the Dataset integration may
   * forward. Default: the blueprint's centralized prefix `/centralized/` plus
   * local `/agenticai/`, `/aws/bedrock-agentcore/` and `aws/spans`.
   */
  readonly datasetLogGroupPrefixes?: readonly string[];
  /**
   * Bedrock model/inference-profile ARNs custom judge evaluators may invoke.
   * Only honoured together with `allowModelInference` (ADR-16).
   */
  readonly judgeModelArns?: readonly string[];
  /** Code-based evaluator function ARNs the evaluation role may invoke. */
  readonly codeEvaluatorFunctionArns?: readonly string[];
  /**
   * Permissions boundary for the Dataset integration and evaluation roles (the
   * operator role keeps the AWS-managed CloudWatchOmniSpaceAccessPolicy boundary
   * the Omni documentation prescribes). Required when the stack deploys through
   * the Audit account's scoped CloudFormation execution policy.
   */
  readonly roleBoundary?: IManagedPolicy;
  /**
   * Operator-provisioned space key (OmniHubKeysStack output `OmniSpaceKeyArn`).
   * When set, together with `existingCentralizedLogsKey`, the construct creates
   * no key: the Audit account's scoped CloudFormation execution policy lets the
   * hub use keys tagged `agenticai:omni-hub=true` but never create or change
   * one. The Dataset integration role then receives its key grant as an
   * identity policy, because a key policy cannot name a role that does not
   * exist when the key is created.
   */
  readonly existingSpaceKey?: IKey;
  /** Operator-provisioned centralized-logs key (output `OmniCentralizedLogsKeyArn`); see `existingSpaceKey`. */
  readonly existingCentralizedLogsKey?: IKey;
  /** Transaction Search indexing percentage (0-100). Default 1. */
  readonly transactionSearchIndexingPercentage?: number;
  /** Manage Transaction Search from this stack. Default true. */
  readonly enableTransactionSearch?: boolean;
  /** Keep the keys on stack deletion (production). Default RETAIN. */
  readonly keyRemovalPolicy?: RemovalPolicy;
  /** Allocation tags. */
  readonly tags?: Record<string, string>;
}

export class OmniHubSpaceConstruct extends Construct {
  readonly operatorRole: Role;
  readonly datasetIntegrationRole: Role;
  readonly evaluationRole: Role;
  readonly spaceKey: IKey;
  readonly centralizedLogsKey: IKey;
  readonly space: CfnResource;
  readonly spaceId: string;
  readonly spaceArn: string;
  readonly transactionSearch?: CfnTransactionSearchConfig;

  constructor(scope: Construct, id: string, props: OmniHubSpaceConstructProps) {
    super(scope, id);

    assertOmniSpaceName(props.spaceName);
    const stack = Stack.of(this);
    if (!Token.isUnresolved(stack.region)) {
      assertOmniHostingRegion(stack.region);
    }
    if (!Token.isUnresolved(props.domainId) && !/^d-[0-9a-z]{1,25}$/.test(props.domainId)) {
      throw new Error(`OmniHubSpaceConstruct domainId '${props.domainId}' must match ^d-[0-9a-z]{1,25}$.`);
    }
    if (props.judgeModelArns && props.judgeModelArns.length > 0 && !props.allowModelInference) {
      throw new Error(
        'OmniHubSpaceConstruct: judgeModelArns requires allowModelInference=true (ADR-16 records the decision to let judge inference bypass the Inference Gateway).',
      );
    }
    const indexing = props.transactionSearchIndexingPercentage ?? 1;
    if (indexing < 0 || indexing > 100) {
      throw new Error('OmniHubSpaceConstruct transactionSearchIndexingPercentage must be between 0 and 100.');
    }
    if ((props.existingSpaceKey === undefined) !== (props.existingCentralizedLogsKey === undefined)) {
      throw new Error(
        'OmniHubSpaceConstruct: pass both existingSpaceKey and existingCentralizedLogsKey (operator-provisioned keys) or neither.',
      );
    }

    const keyRemovalPolicy = props.keyRemovalPolicy ?? RemovalPolicy.RETAIN;
    const datasetArn = `arn:${stack.partition}:cloudwatch:${stack.region}:${stack.account}:dataset/default`;

    // ---------------------------------------------------------------- roles
    const operatorManagedPolicies = [ManagedPolicy.fromAwsManagedPolicyName('CloudWatchOmniSpaceAccessPolicy')];
    if (props.allowModelInference) {
      operatorManagedPolicies.push(ManagedPolicy.fromAwsManagedPolicyName('CloudWatchOmniModelInferencePolicy'));
    }
    if (props.enableAwsIntegrationDiscovery) {
      operatorManagedPolicies.push(ManagedPolicy.fromAwsManagedPolicyName('CloudWatchOmniAWSIntegrationPolicy'));
    }
    // The documented operator-role trust (CloudWatch Omni "cross-service confused
    // deputy prevention"): the service assumes the role with session tags, a source
    // identity and a session context, conditioned on aws:SourceAccount ONLY. A
    // narrower aws:SourceArn on space/* breaks GetSpaceCredentialsForOrganization
    // ("Unable to assume the data access role for this space", observed live).
    this.operatorRole = new Role(this, 'OperatorRole', {
      roleName: props.operatorRoleName ?? 'CloudWatchOmniOperatorRole',
      description: 'CloudWatch Omni space operator role (DataAccessRoleArn): the account-wide ceiling for every space in this account.',
      assumedBy: new ServicePrincipal('cloudwatch.amazonaws.com').withSessionTags().withConditions({
        StringEquals: { 'aws:SourceAccount': stack.account },
      }),
      managedPolicies: operatorManagedPolicies,
      permissionsBoundary: ManagedPolicy.fromAwsManagedPolicyName('CloudWatchOmniSpaceAccessPolicy'),
      maxSessionDuration: Duration.hours(1),
    });
    // withSessionTags() adds sts:TagSession; the service also needs the two actions below.
    const trust = this.operatorRole.assumeRolePolicy;
    if (trust) {
      trust.addStatements(
        new PolicyStatement({
          sid: 'OmniSourceIdentityAndContext',
          effect: Effect.ALLOW,
          principals: [new ServicePrincipal('cloudwatch.amazonaws.com')],
          actions: ['sts:SetSourceIdentity', 'sts:SetContext'],
          conditions: { StringEquals: { 'aws:SourceAccount': stack.account } },
        }),
      );
    }

    const datasetPrefixes = props.datasetLogGroupPrefixes ?? [
      '/centralized/',
      '/agenticai/',
      '/aws/bedrock-agentcore/',
      'aws/spans',
    ];
    const datasetLogGroupArns = datasetPrefixes.flatMap((prefix) => [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:${prefix}*`,
    ]);
    const datasetForwarding: PolicyStatement[] = [
      new PolicyStatement({
        sid: 'IntegrateSelectedLogGroups',
        effect: Effect.ALLOW,
        actions: ['logs:IntegrateWithDataset'],
        resources: datasetLogGroupArns,
      }),
      new PolicyStatement({
        sid: 'PutRecordsIntoDataset',
        effect: Effect.ALLOW,
        actions: ['cloudwatch:PutRecords'],
        resources: ['*'],
      }),
    ];
    if (props.existingSpaceKey) {
      // An operator-provisioned key names no hub role in its key policy (the
      // roles do not exist when the key is created), so the Dataset forwarding
      // grant moves to the role: same actions and conditions as the key-policy
      // statement the construct writes when it creates the key itself.
      datasetForwarding.push(
        new PolicyStatement({
          sid: 'UseSpaceKeyForDatasetForwarding',
          effect: Effect.ALLOW,
          actions: ['kms:GenerateDataKey', 'kms:Encrypt', 'kms:Decrypt'],
          resources: [props.existingSpaceKey.keyArn],
          conditions: {
            StringEquals: {
              'kms:EncryptionContext:aws:cloudwatch:arn': datasetArn,
              'kms:ViaService': `cloudwatch.${stack.region}.amazonaws.com`,
            },
          },
        }),
      );
    }
    this.datasetIntegrationRole = new Role(this, 'DatasetIntegrationRole', {
      roleName: `CloudWatchOmniDatasetIntegrationRole-${stack.region}`,
      permissionsBoundary: props.roleBoundary,
      description: 'Assumed by CloudWatch Logs to forward the hub log groups and traces into the CloudWatch Dataset.',
      assumedBy: new ServicePrincipal('logs.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': stack.account },
          ArnLike: {
            'aws:SourceArn': `arn:${stack.partition}:observabilityadmin:${stack.region}:${stack.account}:dataset-integration/default`,
          },
        },
      }),
      inlinePolicies: {
        DatasetForwarding: new PolicyDocument({ statements: datasetForwarding }),
      },
    });

    const centralizedSpanGroupArns = [
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:aws/spans`,
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:aws/spans:*`,
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:/centralized/*aws/spans`,
      `arn:${stack.partition}:logs:${stack.region}:${stack.account}:log-group:/centralized/*aws/spans:*`,
    ];
    const evaluation = new EvaluationExecutionRoleConstruct(this, 'EvaluationRole', {
      nameSuffix: `omni-${stack.region}`,
      permissionsBoundary: props.roleBoundary,
      spanLogGroupArns: centralizedSpanGroupArns,
      judgeModelArns: props.allowModelInference ? props.judgeModelArns : undefined,
      codeEvaluatorFunctionArns: props.codeEvaluatorFunctionArns,
    });
    this.evaluationRole = evaluation.role;

    // ----------------------------------------------------------------- keys
    if (props.existingSpaceKey && props.existingCentralizedLogsKey) {
      // Operator-provisioned keys (OmniHubKeysStack): the hub only uses them.
      this.spaceKey = props.existingSpaceKey;
      this.centralizedLogsKey = props.existingCentralizedLogsKey;
    } else {
      this.spaceKey = createOmniSpaceKey(this, 'SpaceKey', {
        removalPolicy: keyRemovalPolicy,
        roleGrants: {
          operatorRoleArn: this.operatorRole.roleArn,
          datasetIntegrationRoleArn: this.datasetIntegrationRole.roleArn,
        },
      });
      this.centralizedLogsKey = new OmniCentralizedLogsKeyConstruct(this, 'CentralizedLogs', {
        removalPolicy: keyRemovalPolicy,
      }).key;
    }

    // ------------------------------------------------------ transaction search
    if (props.enableTransactionSearch !== false) {
      // Resource policy for X-Ray first, then the config (fails 403 without it in a fresh account).
      this.transactionSearch = new TransactionSearchConstruct(this, 'TransactionSearch', {
        indexingPercentage: indexing,
      }).config;
    }

    // ---------------------------------------------------------------- space
    const properties: Record<string, unknown> = {
      Name: props.spaceName,
      DomainId: props.domainId,
      DataAccessRoleArn: this.operatorRole.roleArn,
      AgentCoreEvaluationRoleArn: this.evaluationRole.roleArn,
      EncryptionConfiguration: {
        EncryptionStrategy: 'CUSTOMER_MANAGED',
        KmsKeyArn: this.spaceKey.keyArn,
      },
    };
    if (props.tags) {
      properties.Tags = Object.entries(props.tags).map(([Key, Value]) => ({ Key, Value }));
    }
    this.space = new CfnResource(this, 'Space', {
      type: 'AWS::CloudWatch::Space',
      properties,
    });
    this.space.node.addDependency(this.operatorRole, this.evaluationRole, this.spaceKey);
    if (this.transactionSearch) {
      this.space.node.addDependency(this.transactionSearch);
    }
    // The CDK CloudFormation validator's bundled schema predates AWS::CloudWatch::Space;
    // CloudFormation itself validates the resource at deploy time.
    Validations.of(this.space).acknowledge({
      id: 'CloudFormation-Validate::F3006',
      reason: 'SEC-036: AWS::CloudWatch::Space is a 2026 resource type absent from the bundled validator schema; the live deploy is the schema gate.',
    });
    this.spaceId = this.space.getAtt('SpaceId').toString();
    this.spaceArn = this.space.getAtt('Arn').toString();

    for (const [key, value] of Object.entries(props.tags ?? {})) {
      Tags.of(this.operatorRole).add(key, value);
      Tags.of(this.datasetIntegrationRole).add(key, value);
      Tags.of(this.evaluationRole).add(key, value);
      Tags.of(this.spaceKey).add(key, value);
      Tags.of(this.centralizedLogsKey).add(key, value);
    }

    // ------------------------------------------------------------- outputs
    new CfnOutput(this, 'OmniSpaceId', { value: this.spaceId });
    new CfnOutput(this, 'OmniSpaceArn', { value: this.spaceArn });
    new CfnOutput(this, 'OmniDatasetIntegrationRoleArn', {
      value: this.datasetIntegrationRole.roleArn,
      description: 'Pass to scripts/omni/omni_space_bootstrap.py dataset-integration create.',
    });
    new CfnOutput(this, 'OmniCentralizedLogsKeyArn', {
      value: this.centralizedLogsKey.keyArn,
      description: 'Destination CMK for the organization centralization rule (DestinationLogsConfiguration).',
    });
    new CfnOutput(this, 'OmniEvaluationRoleArn', { value: this.evaluationRole.roleArn });

    NagSuppressions.addResourceSuppressions(this.operatorRole, [
      {
        id: 'AwsSolutions-IAM4',
        reason:
          'SEC-034: CloudWatchOmniSpaceAccessPolicy (and, only when ADR-16 enables it, CloudWatchOmniModelInferencePolicy / CloudWatchOmniAWSIntegrationPolicy) are the AWS managed policies the service requires on the space operator role; the same policy is applied as the permissions boundary, matching service behaviour.',
      },
    ]);
    NagSuppressions.addResourceSuppressions(
      this.datasetIntegrationRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-035: logs:IntegrateWithDataset is scoped to the hub log-group prefixes; cloudwatch:PutRecords has no resource type (documented Dataset integration shape).',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason:
            'SEC-035: the forwarding policy names this hub\'s exact log-group prefixes and is deleted with the role; it is not reusable by any other principal.',
        },
      ],
      true,
    );
  }
}
