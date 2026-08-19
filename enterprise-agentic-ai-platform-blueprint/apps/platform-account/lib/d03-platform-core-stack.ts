/**
 * D03PlatformCoreStack — centralised platform-account resources for the
 * D-03 deployment pattern.
 *
 * Deployed to: agenticai-platform-nonprod (account id supplied at synth).
 *
 * Emits:
 *   - Bedrock Guardrail (platform baseline — HIGH filters + AU PII + denied topics)
 *   - BedrockCallerRole — cross-account role workload agents assume to call Bedrock
 *   - Cognito User Pool + App Client — issues JWTs for API Gateway authorizer
 *   - Agent + Tool Registry (DynamoDB, CMK, PITR)
 *   - Experiment-tracking DynamoDB table (ref-arch gap G4)
 *   - Model-Invocation-Logging CMK log group (captures every Bedrock call under D-03)
 *   - Shared ECR repo (ref-arch gap G2) with cross-account pull policy
 *
 * CMK removal policy:
 *   - `retainDataKeys` defaults to `true` → RETAIN + 30-day pending window so
 *     a stack destroy cannot orphan data encrypted under these keys.
 *   - Set `retainDataKeys = false` ONLY for ephemeral dev/test loops where
 *     teardown velocity matters more than recoverability. In that mode the
 *     stack uses DESTROY + 7-day pending window — unsafe for prod data.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import { CfnApplicationInferenceProfile } from 'aws-cdk-lib/aws-bedrock';
import {
  AccountRecovery,
  UserPool,
  UserPoolClient,
  UserPoolClientIdentityProvider,
  OAuthScope,
} from 'aws-cdk-lib/aws-cognito';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { Repository, TagMutability } from 'aws-cdk-lib/aws-ecr';
import {
  AccountPrincipal,
  Effect,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import {
  Alias,
  Code,
  Function as LambdaFunction,
  Runtime,
} from 'aws-cdk-lib/aws-lambda';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { PlatformBaselineGuardrail } from '@agenticai/bedrock-guardrails';
import { allowedBedrockResources, PLATFORM_ALLOWED_MODELS } from '@agenticai/platform-baselines';
import {
  PlatformRegistryConstruct,
  RegistryRecordConstruct,
  toolSpecToRegistryRecordSpec,
} from '@agenticai/agent-registry';
import {
  PLATFORM_TOOL_CATALOGUE,
  composeCedarPolicyDocument,
} from '@agenticai/platform-tool-catalogue';

/**
 * Per-tenant allocation. One application inference profile is emitted per
 * entry, pre-tagged for CUR attribution (application-id / agent-id /
 * cost-centre / workload-account-id). This is the real fix for the D-03
 * CUR-attribution obligation — session-tag propagation across role chaining
 * is blocked by AWS (BUG-005), so platform-owned pre-tagged inference
 * profiles carry the attribution instead.
 */
export interface D03TenantAllocation {
  readonly tenantId: string;
  readonly agentId: string;
  readonly workloadAccountId: string;
  readonly costCentre: string;
  readonly envName: string;
  /** Optional foundation-model override. Defaults to PLATFORM_ALLOWED_MODELS[1] (Claude Haiku 4.5). */
  readonly modelId?: string;
  /** Optional inference-profile prefix (us / apac / eu / global). Defaults to 'us'. */
  readonly inferenceProfilePrefix?: string;
}

export interface D03PlatformCoreStackProps extends StackProps {
  /**
   * Workload account ids allowed to assume the BedrockCallerRole + pull ECR
   * + read registries.
   */
  readonly workloadAccountIds: readonly string[];
  /** Stable shared ExternalId for cross-account AssumeRole. Rotate quarterly. */
  readonly externalId: string;
  /** Cognito user-pool callback URL (OAuth flow). Placeholder for test. */
  readonly cognitoCallbackUrl?: string;
  /**
   * Per-tenant allocations. One platform-owned application inference profile
   * is created per entry with CUR tags baked in. If omitted, a single
   * `demo/primary` default is emitted (back-compat with the D-03 live test).
   */
  readonly tenantAllocations?: readonly D03TenantAllocation[];
  /**
   * When true (default), CMKs created by this stack use `RETAIN` + a 30-day
   * pending deletion window so a stack destroy cannot orphan encrypted data.
   * Set to false ONLY for ephemeral dev/test loops — switches to `DESTROY` +
   * 7-day pending window.
   */
  readonly retainDataKeys?: boolean;
  /**
   * v0.5.0 — when true, provision a single platform-account
   * `PlatformRegistryConstruct` (AWS Bedrock AgentCore Registry) and seed it
   * from `PLATFORM_TOOL_CATALOGUE` via one `RegistryRecordConstruct` per tool.
   *
   * Defaults to `false` to keep the v0.4.0 D-03 v3 path bit-identical for
   * back-compat — the platform-tool-catalogue SSOT remains the synth-time
   * authority for existing deployments. Set to `true` once the workstream
   * gateway stack is wired to consume `subscribedRegistryRecords` (Phase L
   * cutover).
   */
  readonly enableAgentRegistry?: boolean;
  /**
   * AgentCore Registry name (slug). Required when `enableAgentRegistry` is
   * true. AWS pattern: `([0-9a-zA-Z][-]?){1,100}`.
   */
  readonly registryName?: string;
  /**
   * When true and `enableAgentRegistry` is set, every record seeded from the
   * tool catalogue is auto-submitted + auto-approved on create. Recommended
   * only for nonprod / dev pipelines. Defaults to `false`.
   */
  readonly registryAutoApproveOnSeed?: boolean;
}

export class D03PlatformCoreStack extends Stack {
  readonly guardrail: PlatformBaselineGuardrail;
  readonly bedrockCallerRole: Role;
  readonly userPool: UserPool;
  readonly userPoolClient: UserPoolClient;
  readonly agentTable: Table;
  readonly toolTable: Table;
  readonly experimentTable: Table;
  readonly sharedEcrRepo: Repository;
  readonly invocationLogGroup: LogGroup;
  /** Application inference profiles keyed by `${tenantId}__${agentId}`. */
  readonly appInferenceProfiles: Record<string, CfnApplicationInferenceProfile> = {};
  /** v0.5.0 — present when `enableAgentRegistry: true`. */
  readonly agentRegistry?: PlatformRegistryConstruct;
  /** v0.5.0 — record constructs keyed by stable record-id slug (== ToolId). */
  readonly agentRegistryRecords: Record<string, RegistryRecordConstruct> = {};

  constructor(scope: Construct, id: string, props: D03PlatformCoreStackProps) {
    super(scope, id, props);

    const workloadPrincipals = props.workloadAccountIds.map(
      (acct) => new AccountPrincipal(acct),
    );

    // Safe defaults: retain keys + 30d pending window. Flip via
    // `retainDataKeys: false` for dev-loop teardown only (see class JSDoc).
    const retainKeys = props.retainDataKeys ?? true;
    const keyRemovalPolicy = retainKeys ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
    const keyPendingWindow = retainKeys ? Duration.days(30) : Duration.days(7);

    // ---- CMK for registry + experiment table ----
    const registryKey = new Key(this, 'RegistryKey', {
      alias: 'alias/agenticai/platform-registry',
      description: 'Platform-shared registry + experiment-tracking CMK.',
      enableKeyRotation: true,
      pendingWindow: keyPendingWindow,
      removalPolicy: keyRemovalPolicy,
    });
    // Allow DynamoDB service + cross-account read principals.
    registryKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowDynamoDB',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('dynamodb.amazonaws.com')],
        actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
        resources: ['*'],
      }),
    );
    for (const p of workloadPrincipals) {
      registryKey.addToResourcePolicy(
        new PolicyStatement({
          sid: `AllowWorkloadReadKms${p.accountId}`,
          effect: Effect.ALLOW,
          principals: [p],
          actions: ['kms:Decrypt', 'kms:DescribeKey'],
          resources: ['*'],
          // Tighten cross-account usage: the call MUST originate from the
          // named workload account AND be delegated by the DynamoDB service
          // (registry key only encrypts DDB item data). This blocks arbitrary
          // `kms:Decrypt` by any principal in the workload account against
          // bare ciphertext obtained elsewhere.
          conditions: {
            StringEquals: {
              'kms:CallerAccount': p.accountId,
              'kms:ViaService': [
                `dynamodb.${this.region}.amazonaws.com`,
              ],
            },
          },
        }),
      );
    }

    // ---- Bedrock Guardrail (baseline; the only guardrail in the test) ----
    this.guardrail = new PlatformBaselineGuardrail(this, 'BaselineGuardrail');

    // ---- Invocation-log group (D-03: logging is in platform) ----
    const invocationKey = new Key(this, 'InvocationLogKey', {
      alias: 'alias/agenticai/platform-invocation-logs',
      description: 'Platform-level Bedrock Model Invocation Logging CMK.',
      enableKeyRotation: true,
      pendingWindow: keyPendingWindow,
      removalPolicy: keyRemovalPolicy,
    });
    invocationKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCWL',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
      }),
    );
    this.invocationLogGroup = new LogGroup(this, 'InvocationLogs', {
      logGroupName: '/agenticai/bedrock-invocations',
      // 10-year retention aligns with the audit-trail obligation; workload
      // account CloudTrail records are subordinate to this stream for Bedrock.
      retention: RetentionDays.TEN_YEARS,
      encryptionKey: invocationKey,
      // Mirror the CMK retention posture — destroying the log group would
      // discard the audit trail. Overridable only via `retainDataKeys=false`.
      removalPolicy: keyRemovalPolicy,
    });

    // Workload accounts get read-only access to their own rows (filter by
    // session tag client-side) via a cross-account role below.
    for (const p of workloadPrincipals) {
      invocationKey.addToResourcePolicy(
        new PolicyStatement({
          sid: `AllowWorkloadReadInvKms${p.accountId}`,
          effect: Effect.ALLOW,
          principals: [p],
          actions: ['kms:Decrypt'],
          resources: ['*'],
          // Invocation-log key only ever encrypts CloudWatch Logs payloads.
          // Pin both the caller account and the delegating service.
          conditions: {
            StringEquals: {
              'kms:CallerAccount': p.accountId,
              'kms:ViaService': [
                `logs.${this.region}.amazonaws.com`,
              ],
            },
          },
        }),
      );
    }

    // ---- Bedrock service role for invocation logging delivery ----
    const bedrockLogRole = new Role(this, 'BedrockLogDeliveryRole', {
      roleName: 'AgenticAI-D03-BedrockLogDelivery',
      assumedBy: new ServicePrincipal('bedrock.amazonaws.com'),
      description: 'Role Bedrock assumes to deliver invocation logs to the platform log group.',
      inlinePolicies: {
        logs: new PolicyDocument({
          statements: [
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
              resources: [this.invocationLogGroup.logGroupArn],
            }),
          ],
        }),
      },
    });

    // ---- Actually turn on Bedrock Model Invocation Logging (was a gap:
    // previously the log group + role existed but Bedrock had never been
    // told to use them). Account-level API, scoped-as-wide-as-needed.
    const configureInvocationLogging = new AwsCustomResource(this, 'ConfigureInvocationLogging', {
      resourceType: 'Custom::BedrockInvocationLoggingD03',
      onCreate: {
        service: 'Bedrock',
        action: 'putModelInvocationLoggingConfiguration',
        parameters: {
          loggingConfig: {
            cloudWatchConfig: {
              logGroupName: this.invocationLogGroup.logGroupName,
              roleArn: bedrockLogRole.roleArn,
            },
            textDataDeliveryEnabled: true,
            imageDataDeliveryEnabled: false,
            embeddingDataDeliveryEnabled: false,
            videoDataDeliveryEnabled: false,
          },
        },
        physicalResourceId: PhysicalResourceId.of('AgenticAI-D03-InvocationLogging'),
      },
      onUpdate: {
        service: 'Bedrock',
        action: 'putModelInvocationLoggingConfiguration',
        parameters: {
          loggingConfig: {
            cloudWatchConfig: {
              logGroupName: this.invocationLogGroup.logGroupName,
              roleArn: bedrockLogRole.roleArn,
            },
            textDataDeliveryEnabled: true,
            imageDataDeliveryEnabled: false,
            embeddingDataDeliveryEnabled: false,
            videoDataDeliveryEnabled: false,
          },
        },
        physicalResourceId: PhysicalResourceId.of('AgenticAI-D03-InvocationLogging'),
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
          resources: [bedrockLogRole.roleArn],
        }),
      ]),
    });
    // CDK AwsCustomResource Lambda — the usual run of rule suppressions
    // apply (see SEC-006..-011; bedrock account-level API has no ARN).
    NagSuppressions.addResourceSuppressions(
      configureInvocationLogging,
      [
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed Lambda runtime.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CFN-only invocation.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces failures.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Bedrock control-plane public IAM-auth endpoint.' },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          reason: 'SEC-010: CDK custom-resource default role.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason: 'SEC-011: PutModelInvocationLoggingConfiguration is account-level; no ARN.',
        },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: CDK framework-generated inline policy.' },
      ],
      true,
    );

    // ---- BedrockCallerRole — cross-account entry point for workloads ----
    // Trust:
    //   - Principal set = workload account roots (the identity space).
    //   - StringEquals sts:ExternalId = shared rotated ExternalId.
    //   - StringLike aws:PrincipalArn = the per-tenant runtime role ARNs the
    //     workload accounts are expected to mint
    //     (`arn:aws:iam::<acct>:role/AgenticAI-D03-*-runtime`). This narrows
    //     the set of concrete principals within the workload accounts that
    //     can actually assume — a random IAM user in the workload account
    //     cannot.
    //   - StringLike sts:RoleSessionName = `workload-*` (stable convention
    //     `workload-<acct>-<tenant>-<agent>` documented in OPERATIONS.md).
    //     Audit attribution survives role chaining via RoleSessionName.
    // AssumeRole only — we do NOT grant `sts:TagSession` because session
    // tags do not survive the `account-root → runtime-role →
    // cross-account-role` chain in AWS (BUG-005, verified in live test
    // 2026-04-30). CUR attribution is instead carried by per-tenant
    // platform-owned application inference profiles (see
    // `appInferenceProfiles` below) whose tags flow directly to CUR.
    this.bedrockCallerRole = new Role(this, 'BedrockCallerRole', {
      roleName: 'AgenticAI-D03-BedrockCaller',
      // A syntactically-valid placeholder; we overwrite the trust document
      // below via the underlying CfnRole so we can emit a hand-tuned doc
      // with the full Condition block (CDK's `externalIds` + `AssumedBy`
      // sugar does not support additional StringLike conditions).
      assumedBy: new (require('aws-cdk-lib/aws-iam').CompositePrincipal)(...workloadPrincipals),
      description:
        'D-03: cross-account role that workload agents assume to call Bedrock via platform-enforced guardrail.',
      maxSessionDuration: Duration.hours(1),
    });
    // Hand-written trust policy — replaces the CDK-generated doc so we can
    // add StringLike conditions on aws:PrincipalArn and sts:RoleSessionName.
    const runtimeRoleArnGlobs = props.workloadAccountIds.map(
      (acct) => `arn:aws:iam::${acct}:role/AgenticAI-D03-*-runtime`,
    );
    const cfnBedrockCallerRole = this.bedrockCallerRole.node
      .defaultChild as import('aws-cdk-lib/aws-iam').CfnRole;
    cfnBedrockCallerRole.assumeRolePolicyDocument = {
      Version: '2012-10-17',
      Statement: [
        {
          Sid: 'AllowWorkloadRuntimeRolesWithExternalId',
          Effect: 'Allow',
          Principal: {
            AWS: props.workloadAccountIds.map((acct) => `arn:aws:iam::${acct}:root`),
          },
          Action: 'sts:AssumeRole',
          Condition: {
            StringEquals: {
              'sts:ExternalId': props.externalId,
            },
            StringLike: {
              // Only runtime roles named `AgenticAI-D03-*-runtime` may assume.
              'aws:PrincipalArn': runtimeRoleArnGlobs,
              // Stable audit-attribution session name convention
              // `workload-<acct>-<tenant>-<agent>`.
              'sts:RoleSessionName': 'workload-*',
            },
          },
        },
      ],
    };
    // NOTE: the `AllowInvokeAllowlistedBedrock` statement is attached AFTER
    // per-tenant application inference profiles are created below, so it can
    // list the exact per-tenant profile ARNs instead of a blanket
    // `application-inference-profile/*` (which would cross tenant boundaries).

    // Deny inference if `bedrock:GuardrailIdentifier` is missing OR wrong.
    // Must be TWO statements:
    //   (a) Null-gate: fires when the key is absent from the request.
    //   (b) StringNotEquals: fires when the key IS present but carries an
    //       id/arn other than the baseline guardrail (or empty string).
    // A single `ForAnyValue:StringNotEquals` does NOT cover the missing-key
    // case — when the request carries no value, the set is empty and
    // `ForAnyValue` evaluates false, so the deny never fires. Live test
    // 2026-05-01 T7 caught this: unguardrailed Converse passed when only
    // the `ForAnyValue:StringNotEquals` statement was present.
    this.bedrockCallerRole.addToPolicy(
      new PolicyStatement({
        sid: 'DenyInferenceWithMissingGuardrail',
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
    this.bedrockCallerRole.addToPolicy(
      new PolicyStatement({
        sid: 'DenyInferenceWithWrongGuardrail',
        effect: Effect.DENY,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: ['*'],
        conditions: {
          'ForAnyValue:StringNotEquals': {
            'bedrock:GuardrailIdentifier': [
              this.guardrail.guardrail.attrGuardrailId,
              this.guardrail.guardrail.attrGuardrailArn,
            ],
          },
        },
      }),
    );

    // ---- Cognito User Pool (platform-issued JWT) ----
    this.userPool = new UserPool(this, 'UserPool', {
      userPoolName: 'agenticai-d03-platform',
      selfSignUpEnabled: false,
      signInAliases: { email: true, username: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: AccountRecovery.EMAIL_ONLY,
      deletionProtection: false, // test stack — DESTROY on teardown
      removalPolicy: RemovalPolicy.DESTROY,
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      customAttributes: {
        // Custom claim carried in JWT so downstream authorizer can enforce tenant scoping.
        tenantId: new (require('aws-cdk-lib/aws-cognito').StringAttribute)({ mutable: false }),
        workloadAccountId: new (require('aws-cdk-lib/aws-cognito').StringAttribute)({ mutable: false }),
      },
    });
    this.userPoolClient = new UserPoolClient(this, 'UserPoolClient', {
      userPool: this.userPool,
      userPoolClientName: 'agenticai-d03-api',
      authFlows: { userSrp: true, userPassword: true, custom: true, adminUserPassword: false },
      generateSecret: true,
      preventUserExistenceErrors: true,
      supportedIdentityProviders: [UserPoolClientIdentityProvider.COGNITO],
      oAuth: {
        flows: { authorizationCodeGrant: true, clientCredentials: false, implicitCodeGrant: false },
        scopes: [OAuthScope.OPENID, OAuthScope.EMAIL, OAuthScope.PROFILE],
        callbackUrls: [props.cognitoCallbackUrl ?? 'http://localhost:3000/callback'],
      },
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
    });

    // ---- Registry tables (Agent + Tool) ----
    const commonTableProps = {
      billingMode: BillingMode.PAY_PER_REQUEST,
      encryption: TableEncryption.CUSTOMER_MANAGED as TableEncryption,
      encryptionKey: registryKey,
      pointInTimeRecovery: true,
      removalPolicy: RemovalPolicy.DESTROY,
    };
    this.agentTable = new Table(this, 'AgentTable', {
      tableName: 'agenticai-d03-registry-agents',
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'agentId', type: AttributeType.STRING },
      ...commonTableProps,
    });
    this.toolTable = new Table(this, 'ToolTable', {
      tableName: 'agenticai-d03-registry-tools',
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'toolId', type: AttributeType.STRING },
      ...commonTableProps,
    });

    // ---- Experiment tracking table (ref-arch gap G4) ----
    this.experimentTable = new Table(this, 'ExperimentTable', {
      tableName: 'agenticai-d03-experiment-tracking',
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'runId', type: AttributeType.STRING },
      ...commonTableProps,
    });
    this.experimentTable.addGlobalSecondaryIndex({
      indexName: 'by-timestamp',
      partitionKey: { name: 'tenantId', type: AttributeType.STRING },
      sortKey: { name: 'timestamp', type: AttributeType.STRING },
    });

    // H-A: scope cross-account DDB access to runtime-role ARNs only via
    // identity-policy condition on the calling principal's IAM policy.
    // (Previous attempt to put this in the DDB resource policy hit a CFN
    // circular dep across the three tables. Defence-in-depth via the
    // workload-side runtime role's identity policy + dynamodb:LeadingKeys
    // continues to enforce tenant isolation; security-agent F-01 closure
    // is documented in CHANGELOG.md as scope-narrowed in v0.4.1.)
    for (const acct of props.workloadAccountIds) {
      for (const table of [this.agentTable, this.toolTable, this.experimentTable]) {
        table.grantReadData(new AccountPrincipal(acct));
      }
      this.experimentTable.grantWriteData(new AccountPrincipal(acct));
    }

    // ---- Shared ECR repo (ref-arch gap G2) ----
    this.sharedEcrRepo = new Repository(this, 'SharedAgentImages', {
      repositoryName: 'agenticai-d03-agent-base',
      imageTagMutability: TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      encryption: require('aws-cdk-lib/aws-ecr').RepositoryEncryption.KMS,
      encryptionKey: registryKey,
      emptyOnDelete: true,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    // Cross-account pull policy. CDK L2 `Repository.addToResourcePolicy` is a
    // no-op ("ECR resource policy does not allow resource statements"); set via
    // the underlying CfnRepository.
    // Principal is account-root for the identity space, but each statement is
    // further narrowed by `aws:PrincipalArn StringLike` to only allow the
    // workload runtime roles (`AgenticAI-D03-*-runtime`) to pull — not every
    // IAM principal in the workload account.
    const cfnRepo = this.sharedEcrRepo.node.defaultChild as import('aws-cdk-lib/aws-ecr').CfnRepository;
    cfnRepo.repositoryPolicyText = {
      Version: '2012-10-17',
      Statement: workloadPrincipals.map((p) => ({
        Sid: `AllowWorkloadPull${p.accountId}`,
        Effect: 'Allow',
        Principal: { AWS: `arn:aws:iam::${p.accountId}:root` },
        Action: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
        ],
        Condition: {
          StringLike: {
            'aws:PrincipalArn': `arn:aws:iam::${p.accountId}:role/AgenticAI-D03-*-runtime`,
          },
        },
      })),
    };

    // ---- Per-tenant application inference profiles (D-03 CUR attribution) ----
    // Platform-owned, pre-tagged with tenantId / agentId / costCentre /
    // workloadAccountId. CUR picks up these tags on every Bedrock line item
    // via `tag:application-id` / `tag:agent-id` / `tag:workload-account-id` /
    // `tag:cost-centre`. This is the compensating control for BUG-005
    // (session tags do not propagate across role chains).
    const allocations: readonly D03TenantAllocation[] = props.tenantAllocations ?? [
      {
        tenantId: 'demo',
        agentId: 'primary',
        workloadAccountId: props.workloadAccountIds[0] ?? '000000000000',
        costCentre: 'platform',
        envName: 'nonprod',
      },
    ];
    for (const alloc of allocations) {
      const modelId = alloc.modelId ?? PLATFORM_ALLOWED_MODELS[1];
      const prefix = alloc.inferenceProfilePrefix ?? 'us';
      const key = `${alloc.tenantId}__${alloc.agentId}`;
      // Inference-profile-name regex: ^([0-9a-zA-Z][ _-]?)+$ (no dots/slashes).
      const name = `agenticai-d03-${alloc.envName}-${alloc.tenantId}-${alloc.agentId}`;
      const profile = new CfnApplicationInferenceProfile(
        this,
        `AppInfProfile-${key}`,
        {
          inferenceProfileName: name,
          description: `D03 inference profile for ${alloc.tenantId}-${alloc.agentId} in ${alloc.envName}`,
          modelSource: {
            copyFrom: `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/${prefix}.${modelId}`,
          },
          tags: [
            { key: 'deviation', value: 'D-03' },
            { key: 'application-id', value: alloc.tenantId },
            { key: 'agent-id', value: alloc.agentId },
            { key: 'tenant-id', value: alloc.tenantId },
            { key: 'workload-account-id', value: alloc.workloadAccountId },
            { key: 'cost-centre', value: alloc.costCentre },
            { key: 'environment', value: alloc.envName },
          ],
        },
      );
      this.appInferenceProfiles[key] = profile;
      new CfnOutput(this, `AppInfProfileArn-${key}`, {
        value: profile.attrInferenceProfileArn,
        description: `Application inference profile ARN for ${alloc.tenantId}/${alloc.agentId}. Workload role must invoke this ARN (not the raw model) to attribute cost.`,
        exportName: `AgenticAI-D03-AppInfProfile-${alloc.tenantId}-${alloc.agentId}`,
      });
    }

    // ---- BedrockCallerRole allow statement (attached AFTER profiles exist) ----
    // Use explicit per-tenant profile ARNs rather than
    // `application-inference-profile/*`, so the caller role cannot invoke
    // profiles that belong to other tenants' allocations.
    const tenantProfileArns = Object.values(this.appInferenceProfiles).map(
      (p) => p.attrInferenceProfileArn,
    );
    this.bedrockCallerRole.addToPolicy(
      new PolicyStatement({
        sid: 'AllowInvokeAllowlistedBedrock',
        effect: Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
          'bedrock:ApplyGuardrail',
        ],
        resources: [
          ...allowedBedrockResources(this.region, this.account),
          `arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`,
          ...tenantProfileArns,
        ],
      }),
    );

    // Suppressions — narrow to the specific resources that legitimately need
    // them. Stack-wide `AwsSolutions-IAM5` has been intentionally removed and
    // replaced with resource-level suppressions below (SEC-024).
    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'AwsSolutions-COG8', reason: 'SEC-014: Cognito Plus tier is per-MAU priced; opt-in.' },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: Single-purpose service/admin roles with inline policies.' },
        { id: 'NIST.800.53.R5-DynamoDBInBackupPlan', reason: 'SEC-023: PITR enabled; AWS Backup opt-in for customers under formal compliance.' },
        { id: 'NIST.800.53.R5-CognitoUserPoolMFA', reason: 'MFA opt-in via stack override; baseline posture has 12-char password + email verification.' },
        { id: 'AwsSolutions-COG3', reason: 'AdvancedSecurityMode deferred to Cognito Plus tier (SEC-014).' },
        // CDK-managed AwsCustomResource singleton Lambda used by
        // ConfigureInvocationLogging. The suppressions attached to the
        // AwsCustomResource node itself do not cover the singleton Lambda
        // at stack root. Same rationale as SEC-006..SEC-011 in
        // BedrockInvocationLoggingConstruct (Phase 4 equivalent).
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed AwsCustomResource Lambda runtime.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CFN-only invocation; concurrency would break deploys.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces failures; DLQ unconsumed.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Bedrock control-plane public IAM-auth endpoint.' },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          reason: 'SEC-010: CDK custom-resource default managed role.',
        },
        // CDK custom-resources Provider framework internals (registry
        // readiness gate). The framework's own waiter Step Function + its
        // onEvent/isComplete/onTimeout Lambda roles are CDK-generated and
        // reference each other with `<lambda-arn>:*` version wildcards; we do
        // not author them and cannot tighten them without forking the
        // framework. Read-only GetRegistry polling; provisioning-only.
        { id: 'AwsSolutions-SF1', reason: 'SEC-029: CDK Provider framework waiter Step Function (readiness gate); logging config is framework-owned.' },
        { id: 'AwsSolutions-SF2', reason: 'SEC-029: CDK Provider framework waiter Step Function; X-Ray is framework-owned.' },
        // SEC-029: CDK custom-resources Provider framework wires its
        // onEvent/isComplete/onTimeout Lambdas + waiter state machine to each
        // other with function-arn version wildcards (<arn>:*). Framework-
        // generated internals of the registry readiness gate; not authorable.
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<AgentRegistryReadyGateIsCompleteE18FE654.Arn>:*'], reason: 'SEC-029: Provider framework inter-Lambda invoke wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<AgentRegistryReadyGateOnEvent4B5FC073.Arn>:*'], reason: 'SEC-029: Provider framework inter-Lambda invoke wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<AgentRegistryReadyGateProviderframeworkisCompleteE5FCEF80.Arn>:*'], reason: 'SEC-029: Provider framework waiter → isComplete invoke wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<AgentRegistryReadyGateProviderframeworkonTimeout60B4F21A.Arn>:*'], reason: 'SEC-029: Provider framework waiter → onTimeout invoke wildcard.' },
      ],
      true,
    );
    // Resource-level IAM5 suppressions — scoped to the resources whose
    // actions AWS genuinely requires `Resource: *` on.
    NagSuppressions.addResourceSuppressions(
      bedrockLogRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-024: Bedrock invocation logging delivery role needs log-stream write to the platform log group; log-stream ARNs are not knowable at synth time so the policy uses the log-group ARN + * suffix.',
          appliesTo: ['Resource::*'],
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      this.bedrockCallerRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-024: Deny-unless-matches guardrail gate applies to any bedrock:Invoke* call, hence Resource: *. This is a DENY statement whose scope is defined by the Condition, not the resource list.',
          appliesTo: ['Resource::*'],
        },
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-025: Baseline guardrail id is only known post-deploy (Bedrock mints it). `guardrail/*` in the platform account is scoped to this account only; the companion Deny statement forces any invoke to carry the baseline guardrail id. Cannot tighten further without a circular reference.',
          appliesTo: [
            `Resource::arn:aws:bedrock:${this.region}:${this.account}:guardrail/*`,
          ],
        },
      ],
      true,
    );

    // ---- GatewayAdmin role (D-03 v3 AgentCore Gateway admin contract) ----
    // Mirrors the GuardrailAdminRole pattern: the only principal permitted
    // to mutate AgentCore Gateways in ANY workstream account. SCP-09
    // enforces this org-wide by denying `bedrock-agentcore:{Create,Update,
    // Delete}Gateway*` to every principal whose ARN does NOT match this
    // role's ARN (role-name pin: `AgenticAI-D03-GatewayAdmin`).
    //
    // Trust: `new AccountPrincipal(this.account)` — for v1 simplicity this
    // opens the whole platform account root. v2 follow-on: narrow trust to
    // the platform CDK Pipelines role's ARN once the pipeline stack lands.
    // NEVER set looser trust than this.
    const gatewayAdminRole = new Role(this, 'GatewayAdminRole', {
      roleName: 'AgenticAI-D03-GatewayAdmin',
      // v1: platform-account root; v2 will swap to the CDK Pipelines role ARN.
      assumedBy: new AccountPrincipal(this.account),
      description:
        'D-03 v3: sole principal permitted to mutate AgentCore Gateways in workstream accounts. SCP-09 references this role by name.',
      maxSessionDuration: Duration.hours(1),
    });
    gatewayAdminRole.addToPolicy(
      new PolicyStatement({
        sid: 'ManageAgentCoreGateways',
        effect: Effect.ALLOW,
        actions: [
          'bedrock-agentcore:CreateGateway',
          'bedrock-agentcore:UpdateGateway',
          'bedrock-agentcore:DeleteGateway',
          'bedrock-agentcore:GetGateway',
          'bedrock-agentcore:ListGateways',
          'bedrock-agentcore:CreateGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
          'bedrock-agentcore:DeleteGatewayTarget',
          'bedrock-agentcore:GetGatewayTarget',
          'bedrock-agentcore:ListGatewayTargets',
          'bedrock-agentcore:SynchronizeGatewayTargets',
          'bedrock-agentcore:TagResource',
          'bedrock-agentcore:UntagResource',
        ],
        resources: ['arn:aws:bedrock-agentcore:*:*:gateway/*'],
      }),
    );
    // Separate PassRole statement — Gateway creation hands the Gateway
    // service role (created by the per-workstream gateway stack) off to
    // bedrock-agentcore. Scoped to the role-name pattern only.
    gatewayAdminRole.addToPolicy(
      new PolicyStatement({
        sid: 'PassGatewayServiceRole',
        effect: Effect.ALLOW,
        actions: ['iam:PassRole'],
        resources: ['arn:aws:iam::*:role/AgenticAI-D03-*-GatewayServiceRole'],
        conditions: {
          StringEquals: {
            'iam:PassedToService': 'bedrock-agentcore.amazonaws.com',
          },
        },
      }),
    );

    // ---- Demo tool Lambdas (D-03 v3 live-test targets) ----
    // Two minimal Lambdas targeted by the workstream Gateway in the live
    // test: `agenticai-d03-tool-echo` and `agenticai-d03-tool-ping`. Each:
    //   - CMK log group (registryKey)
    //   - Alias `PROD` (mandatory per Q5)
    //   - Resource policy permitting invocation from any
    //     `AgenticAI-D03-*-GatewayServiceRole` across the workload accounts
    //   - Tags: deviation=D-03, tool-id=<id>, cost-centre=platform
    //   - Phase Q (v0.6.0): inlined Cedar gate that reads
    //     `AGENTICAI_CEDAR_POLICY_DOCUMENT` from the Lambda env and the JWT
    //     `cognito:groups` claim from the event. Denies before the user body
    //     when the principal does not intersect the tool's allow-list. The
    //     env var defaults to the v0.5.0 unconditional-permit bundle (any
    //     authenticated principal) so existing back-compat tests/live runs
    //     are unchanged. The live tester rotates the env var via
    //     `aws lambda update-function-configuration` to drive entitlement
    //     scenarios. This is the TODO-GW-POLICY-ENGINE deviation
    //     (README §3) — Cedar evaluation moves to the Gateway when the
    //     AgentCore PolicyEngine API is GA.
    //
    // Handlers are inline because these are pure-demo Lambdas whose
    // behaviour is deterministic (echo / ping). Using `Code.fromInline` +
    // `Runtime.NODEJS_20_X` keeps the toolchain free of esbuild (the repo
    // does not pull `@aws-cdk/aws-lambda-nodejs` esbuild bundling deps).
    // The inlined Cedar gate is kept verbatim-equivalent to the
    // `@agenticai/tool-cedar-wrapper` evaluator (regex grammar, three
    // claim-shape probes, fail-closed defaults) — the package serves as the
    // tested reference implementation that this inline copy mirrors.
    const CEDAR_GATE_PROLOGUE = `
function __agenticaiExtractGroups(event) {
  if (!event || typeof event !== 'object') return [];
  var candidates = [
    event.identity && event.identity.claims,
    event.requestContext && event.requestContext.authorizer && event.requestContext.authorizer.jwt && event.requestContext.authorizer.jwt.claims,
    event.claims,
  ];
  for (var i = 0; i < candidates.length; i++) {
    var c = candidates[i];
    if (c && typeof c === 'object') {
      var raw = c['cognito:groups'];
      if (Array.isArray(raw)) return raw.filter(function (g) { return typeof g === 'string'; });
      if (typeof raw === 'string' && raw.length > 0) {
        return raw.split(',').map(function (s) { return s.trim(); }).filter(function (s) { return s.length > 0; });
      }
    }
  }
  return [];
}
function __agenticaiEvaluateCedar(toolId, doc, principalGroups) {
  if (!toolId || !doc) {
    return { decision: 'deny', reason: 'fail-closed: missing toolId or policy doc' };
  }
  var re = /permit\\s*\\(\\s*principal(?:\\s+in\\s+CognitoGroup::"([^"]+)")?\\s*,\\s*action\\s*==\\s*Action::"InvokeTool"\\s*,\\s*resource\\s*==\\s*Tool::"([^"]+)"\\s*\\)\\s*;/g;
  var unconditional = false;
  var groupBound = [];
  var m;
  while ((m = re.exec(doc)) !== null) {
    if (m[2] !== toolId) continue;
    if (m[1]) groupBound.push(m[1]); else unconditional = true;
  }
  if (unconditional && groupBound.length === 0) {
    return { decision: 'allow', reason: 'unconditional permit matched for tool ' + toolId };
  }
  if (groupBound.length > 0) {
    var intersect = groupBound.filter(function (g) { return principalGroups.indexOf(g) !== -1; });
    if (intersect.length > 0) {
      return { decision: 'allow', reason: 'principal groups [' + intersect.join(',') + '] match Cedar permit for tool ' + toolId };
    }
    return {
      decision: 'deny',
      reason: 'principal groups [' + (principalGroups.join(',') || '(none)') + '] do not intersect tool ' + toolId + ' allow-list [' + groupBound.join(',') + ']',
    };
  }
  return { decision: 'deny', reason: 'no permit matched for tool ' + toolId + ' — default forbid' };
}
function __agenticaiCedarDeniedError(toolId, principalGroups, reason) {
  var err = new Error('Tool ' + toolId + ' denied by Cedar policy. Groups: [' + (principalGroups.join(',') || '(none)') + ']. Reason: ' + reason);
  err.name = 'CedarDeniedError';
  err.toolId = toolId;
  err.principalGroups = principalGroups;
  return err;
}
async function __agenticaiCedarGate(event) {
  var toolId = process.env.AGENTICAI_TOOL_ID || '';
  var doc = process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT || '';
  var groups = __agenticaiExtractGroups(event);
  var decision = __agenticaiEvaluateCedar(toolId, doc, groups);
  if (decision.decision === 'deny') {
    throw __agenticaiCedarDeniedError(toolId, groups, decision.reason);
  }
}
`;

    const toolSpecs: ReadonlyArray<{
      id: string;
      catalogueToolId: string;
      functionName: string;
      innerBody: string;
    }> = [
      {
        id: 'echo',
        catalogueToolId: 'tool-echo',
        functionName: 'agenticai-d03-tool-echo',
        innerBody: `
exports.handler = async (event) => {
  await __agenticaiCedarGate(event);
  return { message: (event && event.message) };
};
`,
      },
      {
        id: 'ping',
        catalogueToolId: 'tool-ping',
        functionName: 'agenticai-d03-tool-ping',
        innerBody: `
exports.handler = async (event, context) => {
  await __agenticaiCedarGate(event);
  return {
    pong: true,
    ts: new Date().toISOString(),
    caller: context && context.invokedFunctionArn,
  };
};
`,
      },
    ];

    const toolAliasArns: Record<string, string> = {};
    for (const spec of toolSpecs) {
      const toolLogGroup = new LogGroup(this, `ToolLogGroup-${spec.id}`, {
        logGroupName: `/aws/lambda/${spec.functionName}`,
        retention: RetentionDays.ONE_MONTH,
        encryptionKey: registryKey,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      // Let CloudWatch Logs use the registryKey.
      registryKey.addToResourcePolicy(
        new PolicyStatement({
          sid: `AllowCWLForTool-${spec.id}`,
          effect: Effect.ALLOW,
          principals: [new ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
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
      // Compose the per-tool Cedar policy bundle from the catalogue. The
      // bundle is identical to what `composeCedarPolicyDocument([toolSpec])`
      // emits — when a tool has `allowedGroups` the env carries
      // principal-bound permits; otherwise the v0.5.0 unconditional permit.
      // The live tester rotates this env var to drive Q2 vs Q3 scenarios
      // without redeploying the stack.
      const catalogueSpec = PLATFORM_TOOL_CATALOGUE[spec.catalogueToolId];
      if (!catalogueSpec) {
        throw new Error(
          `D03PlatformCoreStack: tool '${spec.catalogueToolId}' missing from PLATFORM_TOOL_CATALOGUE`,
        );
      }
      const initialCedarBundle = composeCedarPolicyDocument([catalogueSpec]);
      const inlineHandler = CEDAR_GATE_PROLOGUE + spec.innerBody;
      const fn = new LambdaFunction(this, `ToolFn-${spec.id}`, {
        functionName: spec.functionName,
        runtime: Runtime.NODEJS_20_X,
        handler: 'index.handler',
        code: Code.fromInline(inlineHandler),
        environment: {
          TOOL_ID: spec.id,
          AGENTICAI_TOOL_ID: spec.catalogueToolId,
          AGENTICAI_CEDAR_POLICY_DOCUMENT: initialCedarBundle,
        },
        logGroup: toolLogGroup,
        description: `D-03 v3 demo tool Lambda (${spec.id}). Invoked by AgentCore Gateway targets. Phase Q Cedar gate inlined.`,
      });
      // Cost-centre / deviation tags (CUR attribution).
      (require('aws-cdk-lib').Tags.of(fn) as {
        add: (k: string, v: string) => void;
      }).add('deviation', 'D-03');
      (require('aws-cdk-lib').Tags.of(fn) as {
        add: (k: string, v: string) => void;
      }).add('tool-id', spec.id);
      (require('aws-cdk-lib').Tags.of(fn) as {
        add: (k: string, v: string) => void;
      }).add('cost-centre', 'platform');

      // Explicit Version → Alias(PROD). `currentVersion` gets the
      // code-hash-bound version so alias always tracks the deployed code.
      const version = fn.currentVersion;
      const alias = new Alias(this, `ToolFnAlias-${spec.id}`, {
        aliasName: 'PROD',
        version,
      });

      // Cross-account invoke permissions — grant each workstream's Gateway
      // service role the exact-ARN permission to invoke this tool alias.
      //
      // Discovered live 2026-05-05 as "Bug #7": AgentCore Gateway's
      // `CreateGatewayTarget` validator performs an up-front permission check
      // on the supplied Lambda ARN and FAILS when the Lambda's resource
      // policy grants `AccountPrincipal` + `sourceAccount` (the normal
      // cross-account open-the-account-door pattern). AgentCore requires the
      // specific Gateway service role ARN as the principal — not account
      // root, even though runtime IAM evaluation would have accepted the
      // broader delegation. So we grant one `lambda:addPermission` per
      // (allocation) pair. Lambda's principal-field regex `[\w+=,.@-]*`
      // rejects wildcards, so every principal is an exact role ARN.
      //
      // Layers 2+3 of the D-03 v3 governance model still hold:
      //   - SCP-10 (org-level) denies `lambda:InvokeFunction` from any
      //     runtime-role principal to a non-catalogued ARN
      //   - Gateway service role's identity policy scopes invoke to exactly
      //     the subscribed tool ARNs (synth-time)
      for (const alloc of allocations) {
        const gwRoleArn = `arn:aws:iam::${alloc.workloadAccountId}:role/AgenticAI-D03-${alloc.tenantId}-${alloc.agentId}-gw-svc`;
        alias.addPermission(
          `AllowInvokeFromGwRole-${alloc.tenantId}-${alloc.agentId}`,
          {
            principal: new (require('aws-cdk-lib/aws-iam').ArnPrincipal)(gwRoleArn),
            action: 'lambda:InvokeFunction',
          },
        );
      }

      toolAliasArns[spec.id] = alias.functionArn;

      // L1/concurrency/DLQ/VPC suppressions — pure demo Lambdas (<1s runtime).
      NagSuppressions.addResourceSuppressions(
        fn,
        [
          { id: 'AwsSolutions-L1', reason: 'SEC-006: Pinned to NODEJS_20_X; upgraded on next platform refresh.' },
          { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: Demo tool Lambda; reserved concurrency would throttle legit gateway traffic.' },
          { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: Demo tool; AgentCore Gateway surfaces invoke failures to the caller.' },
          { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Demo tool Lambda has no external network dependencies; VPC attach would add cold-start latency for no security gain.' },
          {
            id: 'AwsSolutions-IAM4',
            appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
            reason: 'SEC-010: CDK-default Lambda execution role uses the AWS-managed basic-execution policy.',
          },
        ],
        true,
      );
    }

    // GatewayAdminRole wildcard-resource suppression — see SEC-027 inline.
    NagSuppressions.addResourceSuppressions(
      gatewayAdminRole,
      [
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::arn:aws:bedrock-agentcore:*:*:gateway/*'],
          reason:
            'SEC-027: Gateway mutation is the administrative contract; resource-level scoping to specific gateway ARNs would require circular refs across workstream accounts.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::arn:aws:iam::*:role/AgenticAI-D03-*-GatewayServiceRole'],
          reason:
            'SEC-027: Gateway service role ARN is per-workstream-account; the role-name pattern is the identity contract the platform agreed with each workstream account.',
        },
      ],
      true,
    );

    // ---- v0.5.0: AgentCore Registry (optional) ----
    // When `enableAgentRegistry` is set, provision a single platform-account
    // Registry and seed it from PLATFORM_TOOL_CATALOGUE — one MCP record per
    // tool with the resolved Lambda alias ARN substituted in. Records start
    // in DRAFT (or APPROVED if `registryAutoApproveOnSeed=true`).
    //
    // The catalogue's `${PLATFORM_ACCOUNT_ID}` placeholder is resolved here
    // (via `this.account`) so the Registry stores the concrete ARN; the
    // workstream Gateway synth then reads it back as the truth source.
    if (props.enableAgentRegistry) {
      if (!props.registryName) {
        throw new Error(
          "D03PlatformCoreStack: 'registryName' is required when 'enableAgentRegistry' is true.",
        );
      }
      this.agentRegistry = new PlatformRegistryConstruct(this, 'AgentRegistry', {
        registryName: props.registryName,
        description:
          'Platform-owned AgentCore Registry. Source of truth for the workstream tool catalogue. v0.5.0+.',
        inboundAuthType: 'AWS_IAM',
        autoApproval: false,
      });
      const autoApprove = props.registryAutoApproveOnSeed ?? false;
      for (const toolSpec of Object.values(PLATFORM_TOOL_CATALOGUE)) {
        const recordSpec = toolSpecToRegistryRecordSpec(toolSpec);
        // Substitute `${PLATFORM_ACCOUNT_ID}` so the Registry holds a
        // concrete ARN. This mirrors `resolveTargetArn` semantics.
        const resolvedSpec =
          recordSpec.descriptorType === 'MCP'
            ? {
                ...recordSpec,
                gatewayTargetArn: recordSpec.gatewayTargetArn.replace(
                  '${PLATFORM_ACCOUNT_ID}',
                  recordSpec.targetAccountId ?? this.account,
                ),
              }
            : recordSpec;
        const rec = new RegistryRecordConstruct(this, `AgentRegistryRecord-${recordSpec.recordId}`, {
          registryId: this.agentRegistry.registryId,
          spec: resolvedSpec,
          autoApproveOnCreate: autoApprove,
          approvalReason: autoApprove
            ? `Auto-seeded from PLATFORM_TOOL_CATALOGUE (tool=${toolSpec.toolId})`
            : undefined,
        });
        // Do not create records until the registry is READY (live-verified
        // race). Depend on the readiness gate, not just the registry resource.
        rec.node.addDependency(this.agentRegistry.readyGate);
        this.agentRegistryRecords[recordSpec.recordId] = rec;
      }
      // GatewayAdminRole carries write access to the Registry record-status
      // surface so a curator job can promote DRAFT → APPROVED via the
      // platform principal. SCP-11 backs this up at the org level.
      gatewayAdminRole.addToPolicy(
        new PolicyStatement({
          sid: 'CuratePlatformRegistryRecords',
          effect: Effect.ALLOW,
          actions: [
            'bedrock-agentcore:SubmitRegistryRecordForApproval',
            'bedrock-agentcore:UpdateRegistryRecordStatus',
            'bedrock-agentcore:UpdateRegistryRecord',
            'bedrock-agentcore:GetRegistryRecord',
            'bedrock-agentcore:ListRegistryRecords',
          ],
          // Record ARN shape: <registry-arn>/record/<id>. Scope to this registry.
          resources: [
            this.agentRegistry.registryArn,
            `${this.agentRegistry.registryArn}/record/*`,
          ],
        }),
      );
      // The `record/*` grant above is a per-record wildcard within a single
      // registry — the curator must promote any record's status. cdk-nag
      // flags the `/record/*` suffix; suppress with evidence (scoped to this
      // registry only, backed by SCP-11 at the org layer).
      NagSuppressions.addResourceSuppressions(
        gatewayAdminRole,
        [
          {
            id: 'AwsSolutions-IAM5',
            reason:
              'SEC-027: registry curator (GatewayAdmin) must act on any record within THIS registry to promote DRAFT→APPROVED. Scoped to <registryArn>/record/*; org-level SCP-11 restricts who may assume the role.',
          },
        ],
        true,
      );
      new CfnOutput(this, 'AgentRegistryId', {
        value: this.agentRegistry.registryId,
        description: 'Stable AgentCore Registry id. Workstream gateway stacks consume this as agenticai/d03RegistryId.',
        exportName: 'AgenticAI-D03-AgentRegistryId',
      });
      new CfnOutput(this, 'AgentRegistryArn', {
        value: this.agentRegistry.registryArn,
        description: 'AgentCore Registry ARN. Consumer IAM grants scope here.',
        exportName: 'AgenticAI-D03-AgentRegistryArn',
      });
    }

    // Outputs consumed by the workload stack (via context / SSM).
    new CfnOutput(this, 'GatewayAdminRoleArn', {
      value: gatewayAdminRole.roleArn,
      description:
        'D-03 v3 AgentCore Gateway administrator role. Sole principal permitted to mutate Gateways across workstream accounts (SCP-09).',
      exportName: 'AgenticAI-D03-GatewayAdminRoleArn',
    });
    new CfnOutput(this, 'ToolEchoArn', {
      value: toolAliasArns['echo'],
      description: 'PROD alias ARN for the agenticai-d03-tool-echo demo Lambda.',
      exportName: 'AgenticAI-D03-ToolEchoArn',
    });
    new CfnOutput(this, 'ToolPingArn', {
      value: toolAliasArns['ping'],
      description: 'PROD alias ARN for the agenticai-d03-tool-ping demo Lambda.',
      exportName: 'AgenticAI-D03-ToolPingArn',
    });
    new CfnOutput(this, 'BedrockCallerRoleArn', {
      value: this.bedrockCallerRole.roleArn,
      description: 'ARN of the cross-account Bedrock caller role.',
      exportName: 'AgenticAI-D03-BedrockCallerRoleArn',
    });
    new CfnOutput(this, 'GuardrailId', {
      value: this.guardrail.guardrail.attrGuardrailId,
      exportName: 'AgenticAI-D03-GuardrailId',
    });
    new CfnOutput(this, 'GuardrailArn', {
      value: this.guardrail.guardrail.attrGuardrailArn,
      exportName: 'AgenticAI-D03-GuardrailArn',
    });
    new CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      exportName: 'AgenticAI-D03-UserPoolId',
    });
    new CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      exportName: 'AgenticAI-D03-UserPoolClientId',
    });
    new CfnOutput(this, 'AgentRegistryTable', {
      value: this.agentTable.tableName,
      exportName: 'AgenticAI-D03-AgentRegistryTable',
    });
    new CfnOutput(this, 'ExperimentTrackingTable', {
      value: this.experimentTable.tableName,
      exportName: 'AgenticAI-D03-ExperimentTrackingTable',
    });
    new CfnOutput(this, 'SharedEcrRepoUri', {
      value: this.sharedEcrRepo.repositoryUri,
      exportName: 'AgenticAI-D03-SharedEcrRepoUri',
    });
    new CfnOutput(this, 'BedrockLogDeliveryRoleArn', {
      value: bedrockLogRole.roleArn,
      exportName: 'AgenticAI-D03-BedrockLogDeliveryRoleArn',
    });
    new CfnOutput(this, 'InvocationLogGroup', {
      value: this.invocationLogGroup.logGroupName,
      exportName: 'AgenticAI-D03-InvocationLogGroup',
    });
  }
}
