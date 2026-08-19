/**
 * AgentCoreGatewayConstruct — the AgentCore Gateway itself (CUSTOM_JWT inbound
 * preserved as belt-and-braces behind API Gateway per §08 Option A).
 *
 * Emits:
 *   - Private ALB that API Gateway fronts (VPC Link target).
 *   - CMK-encrypted log group for Gateway request/response telemetry.
 *   - IAM role for Gateway service with scoped Lambda/Smithy invoke permissions.
 *   - Placeholder for Cedar micro-policies distributed via platform constructs.
 *
 * The actual AgentCore Gateway resource itself (data-plane Cedar + target
 * integrations) is a forthcoming `AWS::BedrockAgentCore::Gateway` L1 that this
 * construct will wrap once the CFN resource type is available. Until then, the
 * ALB + SG + log group + role contract is locked so that downstream API-Gateway
 * fronting work can proceed without re-plumbing.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { ICertificate } from 'aws-cdk-lib/aws-certificatemanager';
import { IVpc, ISecurityGroup, SubnetType, SecurityGroup, Port } from 'aws-cdk-lib/aws-ec2';
import {
  ApplicationLoadBalancer,
  ApplicationListener,
  ApplicationProtocol,
  ApplicationTargetGroup,
  TargetType,
} from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { IpTarget } from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';
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
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface AgentCoreGatewayConstructProps {
  readonly vpc: IVpc;
  /**
   * Environment name: 'nonprod' | 'prod' | free-form. Embedded into resource
   * names + tags. Replaces the hard-coded 'workshop' value from Module 4a.
   */
  readonly envName: string;
  /**
   * Optional ACM certificate. When supplied the internal ALB runs an HTTPS:443
   * listener terminating TLS with this cert. When omitted the listener falls
   * back to plaintext HTTP:80 — permitted ONLY because the intra-VPC hop sits
   * behind API Gateway (which terminates the public TLS) and is gated by
   * security-group trust boundaries. A synth-time `console.warn` is emitted in
   * the plaintext fallback path; see the SEC-013 suppression on the listener below.
   */
  readonly certificate?: ICertificate;
  /**
   * Downstream security groups reachable from this ALB's egress on TCP/443.
   * Each entry results in a per-SG egress rule, scoping ALB → AgentCore ENIs
   * instead of the whole VPC CIDR. If omitted, no egress is opened — the ALB
   * fails secure until downstream SG(s) are supplied (for example once
   * `AWS::BedrockAgentCore::Gateway` L1 exposes its ENI SG).
   */
  readonly downstreamSecurityGroups?: readonly ISecurityGroup[];
}

export class AgentCoreGatewayConstruct extends Construct {
  readonly alb: ApplicationLoadBalancer;
  readonly albSg: SecurityGroup;
  readonly albListener: ApplicationListener;
  readonly gatewayRole: Role;
  readonly kmsKey: Key;
  readonly logGroup: LogGroup;
  readonly targetGroup: ApplicationTargetGroup;

  constructor(scope: Construct, id: string, props: AgentCoreGatewayConstructProps) {
    super(scope, id);

    const stack = Stack.of(this);

    // ---- CMK ----
    this.kmsKey = new Key(this, 'Key', {
      alias: `alias/agenticai/agentcore-gateway-${props.envName}`,
      description: `CMK for AgentCore Gateway (${props.envName}) logs + configuration.`,
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
      logGroupName: `/agenticai/agentcore-gateway/${props.envName}`,
      retention: RetentionDays.SIX_MONTHS,
      encryptionKey: this.kmsKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ---- Gateway service role ----
    this.gatewayRole = new Role(this, 'ServiceRole', {
      roleName: `AgenticAI-AgentCoreGateway-${props.envName}`,
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Role assumed by AgentCore Gateway for Lambda / Smithy target invocations.',
    });
    // Least-privilege Lambda invoke scoped to per-env / per-app resource name pattern.
    this.gatewayRole.addToPolicy(
      new PolicyStatement({
        sid: 'InvokeApprovedLambdaTargets',
        effect: Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [`arn:aws:lambda:${stack.region}:${stack.account}:function:agenticai-${props.envName}-*`],
      }),
    );

    // ---- Internal ALB (VPC Link target) ----
    this.albSg = new SecurityGroup(this, 'AlbSg', {
      vpc: props.vpc,
      description: `AgentCore Gateway internal ALB SG (${props.envName}). Inbound from API GW VPC Link only.`,
      allowAllOutbound: false,
    });

    const albAccessLogsBucket = new Bucket(this, 'AlbAccessLogs', {
      bucketName: `agai-acg-alb-${props.envName}-${stack.account}-${stack.region}`,
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
        { id: 'AwsSolutions-S1', reason: 'SEC-001: self-logging loop; ALB access-logs destination.' },
        { id: 'NIST.800.53.R5-S3BucketLoggingEnabled', reason: 'SEC-001: log destination cannot log to itself.' },
        { id: 'NIST.800.53.R5-S3BucketReplicationEnabled', reason: 'SEC-002: CRR deferred to v2 DR roadmap.' },
        { id: 'NIST.800.53.R5-S3DefaultEncryptionKMS', reason: 'SEC-003: ObjectWriter ownership incompatible with CMK+bucket-key.' },
      ],
      true,
    );

    this.alb = new ApplicationLoadBalancer(this, 'Alb', {
      vpc: props.vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: SubnetType.PRIVATE_ISOLATED },
      securityGroup: this.albSg,
      deletionProtection: true,
    });
    this.alb.logAccessLogs(albAccessLogsBucket, 'agentcore-gateway-alb/');
    NagSuppressions.addResourceSuppressions(
      this.alb,
      [
        {
          id: 'NIST.800.53.R5-ALBWAFEnabled',
          reason: 'SEC-012: Primary WAF is at API Gateway (§08 Option A). Internal ALB behind VPC Link.',
        },
      ],
      true,
    );

    // Listener protocol is governed by whether an ACM cert is supplied.
    //   - cert supplied  → HTTPS:443 with the ACM cert (preferred).
    //   - cert omitted   → HTTP:80 (intra-VPC only; TLS terminates upstream
    //     at API Gateway per §08 Option A, SG trust boundary enforces tenant
    //     isolation; tracked under SEC-013 and surfaced at synth time).
    this.targetGroup = new ApplicationTargetGroup(this, 'Tg', {
      vpc: props.vpc,
      port: 443,
      protocol: props.certificate ? ApplicationProtocol.HTTPS : ApplicationProtocol.HTTP,
      targetType: TargetType.IP,
      // Placeholder IP; overridden once AWS::BedrockAgentCore::Gateway L1 lands
      // and exposes ENIs that can be registered as targets.
      targets: [new IpTarget('10.20.0.1')],
      healthCheck: { path: '/ping', healthyHttpCodes: '200-299' },
    });

    if (props.certificate) {
      this.albListener = this.alb.addListener('Listener', {
        port: 443,
        protocol: ApplicationProtocol.HTTPS,
        certificates: [props.certificate],
        defaultTargetGroups: [this.targetGroup],
        open: false,
      });
    } else {
      // eslint-disable-next-line no-console
      console.warn(
        `[AgentCoreGatewayConstruct] '${id}' (${props.envName}) created WITHOUT an ACM certificate — ` +
          'falling back to plaintext HTTP:80 on the internal ALB. This is permitted ONLY ' +
          'because API Gateway terminates public TLS upstream and the intra-VPC hop is ' +
          'gated by SG + VPC isolation (SEC-013). For production, pass `certificate` so ' +
          'the ALB listener runs HTTPS:443. SEC-013.',
      );
      this.albListener = this.alb.addListener('Listener', {
        port: 80,
        protocol: ApplicationProtocol.HTTP,
        defaultTargetGroups: [this.targetGroup],
        open: false,
      });
      NagSuppressions.addResourceSuppressions(
        this.albListener,
        [
          {
            id: 'NIST.800.53.R5-ALBHttpToHttpsRedirection',
            reason:
              'SEC-013: Internal ALB listener behind API Gateway VPC Link. Public TLS terminates upstream at API Gateway (ApiGatewayFronting); the intra-VPC hop relies on SG trust + VPC isolation. Redirect would create a loop. Production deployments pass an ACM cert via `certificate` prop to upgrade to HTTPS.',
          },
          {
            id: 'NIST.800.53.R5-ELBv2ACMCertificateRequired',
            reason:
              'SEC-013: ACM cert lives at API Gateway upstream, not the internal ALB, when `certificate` is omitted. Production deployments supply the optional `certificate` prop to attach an ACM cert to this listener directly.',
          },
        ],
        true,
      );
    }

    // Egress: ALB → AgentCore ENIs. Prefer per-SG references over the VPC
    // CIDR; the CIDR shape let the ALB reach anything in the VPC, which
    // failed the trust-boundary review. Fail secure when no downstream SG
    // is supplied yet.
    if (props.downstreamSecurityGroups && props.downstreamSecurityGroups.length > 0) {
      for (const downstream of props.downstreamSecurityGroups) {
        this.albSg.addEgressRule(
          downstream,
          Port.tcp(443),
          `TLS to AgentCore ENI SG ${downstream.securityGroupId}`,
        );
      }
    } else {
      // eslint-disable-next-line no-console
      console.warn(
        `[AgentCoreGatewayConstruct] '${id}' (${props.envName}) created WITHOUT any ` +
          '`downstreamSecurityGroups`. ALB egress is left closed (fail-secure). ' +
          'Supply the AgentCore ENI SG(s) once AWS::BedrockAgentCore::Gateway L1 is ' +
          'wired so the ALB can reach its targets.',
      );
    }
  }
}
