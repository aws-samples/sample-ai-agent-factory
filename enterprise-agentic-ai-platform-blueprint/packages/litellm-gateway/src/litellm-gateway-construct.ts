/**
 * LiteLLMGatewayConstruct — D-01 compensating implementation.
 *
 * The triple-gate guardrail enforcement (spec §2.2.3 L625-665 / §3.2.13 +
 * README §3.1):
 *
 *   1. SCP-02 (Org-level)   — enforced by packages/organizations Phase 1
 *   2. IAM task-role deny   — enforced here on the ECS task role
 *   3. Bedrock VPCE policy  — enforced by packages/agentic-vpc Phase 4
 *
 * Also model-allow-list single source of truth — task role `Resource` clause
 * references `allowedModelArns(region)` from @agenticai/platform-baselines.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { IVpc, SubnetType, SubnetSelection, SecurityGroup, ISecurityGroup, Port } from 'aws-cdk-lib/aws-ec2';
import {
  Cluster,
  ContainerImage,
  ContainerInsights,
  CpuArchitecture,
  FargateService,
  FargateTaskDefinition,
  LogDrivers,
  OperatingSystemFamily,
  Secret as EcsSecret,
} from 'aws-cdk-lib/aws-ecs';
import { ApplicationLoadBalancer } from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import {
  Effect,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectOwnership,
} from 'aws-cdk-lib/aws-s3';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import { allowedBedrockResources } from '@agenticai/platform-baselines';

export interface LiteLLMGatewayConstructProps {
  /** Workload VPC (from AgenticVpcConstruct). Required. */
  readonly vpc: IVpc;

  /**
   * Subnet selection for ECS + ALB. Defaults to PRIVATE_ISOLATED — matches
   * the AgenticVpcConstruct shape (Phase 4). Override if deploying into a
   * differently-configured VPC.
   */
  readonly subnets?: SubnetSelection;

  /**
   * Container image for LiteLLM. Defaults to the official image but callers
   * in a regulated environment should point at a private ECR copy.
   */
  readonly image?: ContainerImage;

  /**
   * Desired task count. Default 2 for HA within a single region.
   */
  readonly desiredTaskCount?: number;

  /**
   * VPC endpoint SG to allow task egress to on 443. When the LiteLLM construct
   * is composed with `AgenticVpcConstruct`, pass the `vpceEniSg` from that
   * construct. If unset, task egress is denied by default (secure default);
   * operators must wire an egress rule explicitly post-deploy.
   *
   * Replaces the prior hard-coded placeholder prefix list.
   */
  readonly bedrockVpceSecurityGroup?: ISecurityGroup;
}

export class LiteLLMGatewayConstruct extends Construct {
  readonly cluster: Cluster;
  readonly taskDefinition: FargateTaskDefinition;
  readonly service: FargateService;
  readonly alb: ApplicationLoadBalancer;
  readonly taskRole: Role;
  readonly kmsKey: Key;
  readonly logGroup: LogGroup;

  constructor(scope: Construct, id: string, props: LiteLLMGatewayConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    // ---- CMK for logs + secrets at rest ----
    this.kmsKey = new Key(this, 'Key', {
      alias: 'alias/agenticai/litellm-gateway',
      description: 'CMK for LiteLLM gateway logs + config secrets.',
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

    // ---- Log group ----
    this.logGroup = new LogGroup(this, 'LogGroup', {
      logGroupName: '/agenticai/litellm-gateway',
      retention: RetentionDays.THREE_MONTHS,
      encryptionKey: this.kmsKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---- Task role with triple-gate guardrail deny ----
    this.taskRole = new Role(this, 'TaskRole', {
      assumedBy: new ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'LiteLLM task role; Bedrock access scoped to allow-list + deny without GuardrailIdentifier.',
    });

    // Allow invocation of allow-listed foundation models + application inference profiles.
    this.taskRole.addToPolicy(
      new PolicyStatement({
        sid: 'AllowInvokeAllowlistedModels',
        effect: Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
          'bedrock:ApplyGuardrail',
        ],
        resources: [
          // Covers foundation-model/* + system cross-region inference-profile/*
          // (us./eu./global. prefixes) — required for Claude 4.5 in
          // the approved regions (us-east-1 / us-west-2).
          ...allowedBedrockResources(stack.region, stack.account),
          `arn:aws:bedrock:${stack.region}:${stack.account}:application-inference-profile/*`,
          `arn:aws:bedrock:${stack.region}:${stack.account}:guardrail/*`,
        ],
      }),
    );

    // Deny when GuardrailIdentifier is Null — the critical D-01 compensation.
    // SEC (security review — INTENTIONAL): resources:['*'] is correct on
    // a DENY. The whole point of this statement is to block un-guardrailed
    // inference on EVERY Bedrock resource, including any model/profile a
    // future Allow (or an inherited policy) might grant. Narrowing this Deny
    // to the allow-list ARNs would let inference on any other resource escape
    // guardrail enforcement — a strict security regression. A broad Deny can
    // only ever remove access, never grant it.
    this.taskRole.addToPolicy(
      new PolicyStatement({
        sid: 'DenyInferenceWithoutGuardrail',
        effect: Effect.DENY,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        // DENY STATEMENT ONLY: wildcard resources are correct here because a
        // deny must cover ALL Bedrock resources. NEVER use resources:['*'] in
        // an ALLOW statement — see the scoped Allow above.
        resources: ['*'],
        conditions: {
          Null: {
            'bedrock:GuardrailIdentifier': 'true',
          },
        },
      }),
    );

    // ---- ECS cluster + Fargate task ----
    this.cluster = new Cluster(this, 'Cluster', {
      vpc: props.vpc,
      clusterName: `agenticai-litellm-${stack.region}`,
      containerInsightsV2: ContainerInsights.ENABLED,
    });

    // Secret for the LiteLLM master key, injected into the container at
    // start via ECS secrets (plaintext env vars are nag-flagged; use Secrets).
    const litellmMasterKey = new Secret(this, 'MasterKey', {
      secretName: `agenticai/litellm-gateway/master-key-${stack.region}`,
      description: 'LiteLLM master key for admin/proxy operations.',
      encryptionKey: this.kmsKey,
      generateSecretString: {
        excludePunctuation: true,
        passwordLength: 48,
      },
    });
    NagSuppressions.addResourceSuppressions(
      litellmMasterKey,
      [
        {
          id: 'AwsSolutions-SMG4',
          reason: 'SEC-016: LiteLLM master key has no target service rotation contract; rotation is a manual-release operation gated by the platform. Documented in README section 9 (Operations).',
        },
        {
          id: 'NIST.800.53.R5-SecretsManagerRotationEnabled',
          reason: 'SEC-016: Same as above — rotation is manual + runbook-driven until a compatible rotation Lambda ships.',
        },
      ],
      true,
    );

    this.taskDefinition = new FargateTaskDefinition(this, 'TaskDef', {
      memoryLimitMiB: 1024,
      cpu: 512,
      runtimePlatform: {
        cpuArchitecture: CpuArchitecture.ARM64,
        operatingSystemFamily: OperatingSystemFamily.LINUX,
      },
      taskRole: this.taskRole,
    });

    this.taskDefinition.addContainer('litellm', {
      containerName: 'litellm',
      // SEC (security review): the default base image below pins only the minor
      // tag `:3.12`, which can drift across patch releases. This is a demo
      // placeholder ONLY — production deployments MUST supply `props.image`
      // built from a base image pinned to an immutable SHA256 digest
      // (`public.ecr.aws/lambda/python:3.12@sha256:<digest>`) resolved and
      // refreshed through change management. See README §10 (supply chain).
      image: props.image ?? ContainerImage.fromRegistry('public.ecr.aws/lambda/python:3.12'),
      essential: true,
      portMappings: [{ containerPort: 4000 }],
      logging: LogDrivers.awsLogs({
        logGroup: this.logGroup,
        streamPrefix: 'litellm',
      }),
      secrets: {
        LITELLM_MASTER_KEY: EcsSecret.fromSecretsManager(litellmMasterKey),
      },
    });
    NagSuppressions.addResourceSuppressions(
      this.taskDefinition,
      [
        {
          id: 'AwsSolutions-ECS2',
          reason:
            'SEC-015: LiteLLM master key is injected via ECS secrets from Secrets Manager (not plaintext env). The only environment value used at runtime is LITELLM_CONFIG_PATH, a non-secret filesystem path to a configuration volume, which is not currently set but reserved for future use.',
        },
      ],
      true,
    );

    // ---- Security group for ECS tasks ----
    const serviceSg = new SecurityGroup(this, 'ServiceSg', {
      vpc: props.vpc,
      description: 'LiteLLM ECS service SG. Inbound 4000 from ALB SG only.',
      allowAllOutbound: false,
    });

    this.service = new FargateService(this, 'Service', {
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: props.desiredTaskCount ?? 2,
      assignPublicIp: false,
      vpcSubnets: props.subnets ?? { subnetType: SubnetType.PRIVATE_ISOLATED },
      securityGroups: [serviceSg],
      enableExecuteCommand: false,
    });

    // Tasks egress to Bedrock Runtime VPCE on 443. The destination is the
    // caller-supplied VPCE SG — not a prefix list — because the VPCE is
    // resolvable inside the VPC only and a prefix-list reference would be
    // hard-coded across environments. If the caller did not pass a VPCE SG,
    // egress stays closed (fail-secure). Operators must supply the SG or
    // add their own egress rule post-deploy.
    if (props.bedrockVpceSecurityGroup) {
      serviceSg.addEgressRule(
        props.bedrockVpceSecurityGroup,
        Port.tcp(443),
        'TLS to Bedrock Runtime VPCE',
      );
      // Reciprocally, allow the VPCE SG to accept ingress from this SG.
      props.bedrockVpceSecurityGroup.addIngressRule(
        serviceSg,
        Port.tcp(443),
        'TLS from LiteLLM task SG',
      );
    }

    // ---- Internal ALB with access logs + deletion protection ----
    const albAccessLogsBucket = new Bucket(this, 'AlbAccessLogs', {
      bucketName: `agenticai-litellm-alb-access-${stack.account}-${stack.region}`,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      objectOwnership: ObjectOwnership.OBJECT_WRITER,
      versioned: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ id: 'expire', expiration: Duration.days(365) }],
    });
    NagSuppressions.addResourceSuppressions(
      albAccessLogsBucket,
      [
        { id: 'AwsSolutions-S1', reason: 'SEC-001: self-logging loop; ALB access-log destination.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: log destination cannot log to itself.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3DefaultEncryptionKMS', reason: 'SEC-003: ObjectWriter ownership incompatible with CMK+bucket-key.' },
      ],
      true,
    );

    this.alb = new ApplicationLoadBalancer(this, 'Alb', {
      vpc: props.vpc,
      internetFacing: false,
      vpcSubnets: props.subnets ?? { subnetType: SubnetType.PRIVATE_ISOLATED },
      deletionProtection: true,
    });
    this.alb.logAccessLogs(albAccessLogsBucket, 'litellm-alb/');

    // ALB-level cdk-nag suppressions:
    // - Internal ALB behind API Gateway VPC Link; WAF is at API Gateway (not ALB).
    //   NIST.800.53.R5-ALBWAFEnabled is satisfied by API Gateway WAF upstream.
    // - ELBv2 ACM cert not required for intra-VPC HTTP hops (TLS terminates at
    //   API Gateway upstream); ALB<->tasks is inside the VPC SG trust boundary.
    NagSuppressions.addResourceSuppressions(
      this.alb,
      [
        {
          id: 'NIST.800.53.R5-ALBWAFEnabled',
          reason:
            'SEC-012: WAF is at the public-facing API Gateway stage (primary §08 auth boundary). This internal ALB sits behind API Gateway VPC Link; adding a second WAF here duplicates protection without extra coverage.',
        },
      ],
      true,
    );
  }
}
