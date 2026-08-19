/**
 * PlatformInferenceGatewayConstruct — D-03 centralised-platform PrivateLink
 * primitive (see README §3.3, residual-risks row
 * "Cross-account PrivateLink → LiteLLM attack surface").
 *
 * The construct stands up, in the platform account:
 *   - An internal NetworkLoadBalancer (multi-AZ, private-isolated subnets).
 *   - A TCP:443 listener (or TLS:443 when an ACM cert is supplied) with a
 *     target group that points at the platform's LiteLLM ALB if one is passed.
 *     Callers that pre-date a LiteLLM deployment may omit `targetAlb` — the
 *     construct emits an empty target group so the D-03 *current* shape
 *     (AssumeRole → Bedrock-direct) continues to synth, and a future LiteLLM
 *     ALB can be wired by passing the prop later.
 *   - A VpcEndpointService (PrivateLink) that fronts the NLB with
 *     `acceptanceRequired: false` and `allowedPrincipals` restricted to the
 *     *root* principals of the supplied workload account ids. Any other
 *     principal attempting to create a VPCE against this service is refused
 *     by the PrivateLink control plane.
 *
 * The only surface-area this opens cross-account is an L4 TCP:443 flow to the
 * LiteLLM ALB (through the NLB). TLS + WAF + JWT-authn are enforced upstream
 * (at the API Gateway fronting the ALB in the D-01 / D-03 composition) — this
 * construct is the *network* primitive.
 *
 * cdk-nag posture:
 *   - Deletion protection on (NIST `ELBDeletionProtectionEnabled`).
 *   - S3 access logs on (NIST `ELBLoggingEnabled`).
 *   - If `certificate` is omitted, `ELBv2ACMCertificateRequired` is suppressed
 *     under SEC-026 (rationale: PrivateLink cross-account callers create their
 *     own InterfaceVpcEndpoint which performs TLS-at-VPCE; a TCP passthrough
 *     listener is the deliberate contract; when callers care about
 *     end-to-end TLS to the NLB they pass `certificate`).
 *   - `ALBWAFEnabled` does not apply (NLBs do not support WAF); rule
 *     skips non-ALB but suppress at stack level defensively — not needed.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { ICertificate } from 'aws-cdk-lib/aws-certificatemanager';
import {
  IVpc,
  SubnetSelection,
  SubnetType,
  VpcEndpointService,
} from 'aws-cdk-lib/aws-ec2';
import {
  ApplicationLoadBalancer,
  NetworkListener,
  NetworkLoadBalancer,
  NetworkTargetGroup,
  Protocol,
  TargetType,
} from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { AlbArnTarget } from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';
import { ArnPrincipal } from 'aws-cdk-lib/aws-iam';
import {
  BlockPublicAccess,
  Bucket,
  BucketEncryption,
  ObjectOwnership,
} from 'aws-cdk-lib/aws-s3';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

export interface PlatformInferenceGatewayConstructProps {
  /** Platform-account VPC the NLB lives in. Required. */
  readonly vpc: IVpc;

  /**
   * The LiteLLM-fronting ApplicationLoadBalancer (internal ALB) the NLB
   * forwards 443 traffic to. Optional — if omitted the construct emits a
   * placeholder target group with no registered targets, so D-03's current
   * `AssumeRole → Bedrock-direct` shape keeps synthesising. Wire the prop in
   * when LiteLLM is stood up in the platform account.
   */
  readonly targetAlb?: ApplicationLoadBalancer;

  /**
   * The port on `targetAlb` to forward to. Default 443. ALB-as-NLB-target
   * requires the ALB to have a listener on this port (validated at deploy).
   */
  readonly targetAlbPort?: number;

  /**
   * Workload account ids permitted to create an InterfaceVpcEndpoint against
   * this endpoint service. Each becomes an
   * `AccountPrincipal → arn:aws:iam::<acct>:root` entry on the
   * `AllowedPrincipals` of the endpoint service. Empty list is rejected —
   * a service with no consumers has no purpose.
   */
  readonly workloadAccountIds: readonly string[];

  /**
   * Optional ACM cert. Supplied → NLB listener runs TLS:443 (cert terminates
   * at the NLB). Omitted → the listener runs TCP:443 passthrough and the
   * ALB behind it terminates TLS.
   */
  readonly certificate?: ICertificate;

  /**
   * Subnet selection for the NLB. Defaults to PRIVATE_ISOLATED — matches
   * the blueprint's AgenticVpcConstruct shape.
   */
  readonly subnets?: SubnetSelection;
}

export class PlatformInferenceGatewayConstruct extends Construct {
  /** The internal NLB fronting the PrivateLink endpoint service. */
  readonly nlb: NetworkLoadBalancer;

  /** The 443 listener on the NLB (TLS if cert supplied, otherwise TCP). */
  readonly listener: NetworkListener;

  /** The NLB target group (populated when `targetAlb` is supplied). */
  readonly targetGroup: NetworkTargetGroup;

  /** The endpoint service wrapping the NLB. */
  readonly endpointService: VpcEndpointService;

  /**
   * The service name consumers use to create an InterfaceVpcEndpoint, e.g.
   * `com.amazonaws.vpce.<region>.vpce-svc-xxxxxxxxxxxxxxxx`. Pass this
   * (as a synth-time CFN Output / SSM Parameter / context key) to each
   * workload stack.
   */
  readonly endpointServiceName: string;

  constructor(
    scope: Construct,
    id: string,
    props: PlatformInferenceGatewayConstructProps,
  ) {
    super(scope, id);

    const stack = Stack.of(this);

    if (props.workloadAccountIds.length === 0) {
      throw new Error(
        'PlatformInferenceGatewayConstruct: workloadAccountIds must contain at least one account id. An endpoint service with no allowed principals is unusable.',
      );
    }
    // Defensive validation — `AccountPrincipal(acct).arn` is a deploy-time
    // token, but if a literal is passed we catch typos early.
    for (const acct of props.workloadAccountIds) {
      if (!/^\d{12}$/.test(acct)) {
        throw new Error(
          `PlatformInferenceGatewayConstruct: workloadAccountIds entries must be 12-digit account ids; got '${acct}'.`,
        );
      }
    }

    // ---- NLB access log bucket ----
    // ELBLoggingEnabled (NIST + AwsSolutions) requires access logs on.
    const accessLogsBucket = new Bucket(this, 'NlbAccessLogs', {
      bucketName: `agenticai-platform-inference-nlb-access-${stack.account}-${stack.region}`,
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
      accessLogsBucket,
      [
        {
          id: 'AwsSolutions-S1',
          reason:
            'SEC-001: self-logging loop; NLB access-log destination bucket cannot log to itself.',
        },
        {
          id: 'NIST.800.53.R5-S3BucketLoggingEnabled',
          reason: 'SEC-001: log destination cannot log to itself.',
        },
        {
          id: 'NIST.800.53.R5-S3BucketReplicationEnabled',
          reason: 'SEC-002: CRR deferred to v2 DR roadmap.',
        },
        {
          id: 'NIST.800.53.R5-S3DefaultEncryptionKMS',
          reason:
            'SEC-003: ObjectWriter ownership (required for ELB log delivery) is incompatible with SSE-KMS + bucket-key. ELB access-log records are non-sensitive request metadata (source IP, latency, response code).',
        },
      ],
      true,
    );

    // ---- NLB ----
    this.nlb = new NetworkLoadBalancer(this, 'Nlb', {
      vpc: props.vpc,
      internetFacing: false,
      crossZoneEnabled: true,
      vpcSubnets: props.subnets ?? { subnetType: SubnetType.PRIVATE_ISOLATED },
      deletionProtection: true,
    });
    this.nlb.logAccessLogs(accessLogsBucket, 'platform-inference-nlb/');

    // ---- Target group ----
    // If the caller supplied a LiteLLM ALB, wire it up via AlbArnTarget (ALB-
    // as-NLB-target). Otherwise emit a placeholder ALB-typed target group with
    // no registered targets — stack still synthesises, future deployer wires
    // the LiteLLM ALB by re-deploying the construct with `targetAlb` set.
    this.targetGroup = new NetworkTargetGroup(this, 'Tg', {
      vpc: props.vpc,
      port: props.targetAlbPort ?? 443,
      protocol: Protocol.TCP,
      targetType: TargetType.ALB,
      targets: props.targetAlb
        ? [new AlbArnTarget(props.targetAlb.loadBalancerArn, props.targetAlbPort ?? 443)]
        : [],
      healthCheck: {
        enabled: true,
        protocol: Protocol.HTTPS,
        port: String(props.targetAlbPort ?? 443),
        path: '/health',
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 2,
        interval: Duration.seconds(30),
      },
    });

    // ---- Listener (TLS:443 if cert, else TCP:443) ----
    this.listener = this.nlb.addListener('Listener', {
      port: 443,
      protocol: props.certificate ? Protocol.TLS : Protocol.TCP,
      certificates: props.certificate ? [props.certificate] : undefined,
      defaultTargetGroups: [this.targetGroup],
    });

    if (!props.certificate) {
      // TCP passthrough listener — ACM cert rule does not apply; TLS
      // terminates at the consumer-side VPCE or at the upstream ALB.
      NagSuppressions.addResourceSuppressions(
        this.listener,
        [
          {
            id: 'NIST.800.53.R5-ELBv2ACMCertificateRequired',
            reason:
              "SEC-026: PrivateLink-fronted NLB uses TCP:443 passthrough so the platform's LiteLLM ALB terminates TLS. Cross-account consumers see a PrivateLink ENI whose TLS is negotiated end-to-end to the ALB. Callers that need cert-at-NLB pass `certificate` at construct time.",
          },
          {
            id: 'AwsSolutions-ELB2',
            reason: 'SEC-026: see above — TCP passthrough is the deliberate contract.',
          },
        ],
        true,
      );
    }

    // ---- Endpoint service (PrivateLink) ----
    // Principals are workload-account root ARNs — only those accounts can
    // create an InterfaceVpcEndpoint against the service.
    const allowedPrincipals = props.workloadAccountIds.map(
      (acct) => new ArnPrincipal(`arn:${stack.partition}:iam::${acct}:root`),
    );

    this.endpointService = new VpcEndpointService(this, 'EndpointService', {
      vpcEndpointServiceLoadBalancers: [this.nlb],
      acceptanceRequired: false,
      allowedPrincipals,
    });

    this.endpointServiceName = this.endpointService.vpcEndpointServiceName;

    // ---- Outputs ----
    new CfnOutput(this, 'EndpointServiceName', {
      value: this.endpointServiceName,
      description:
        'PrivateLink endpoint service name. Consumers pass this to InterfaceVpcEndpointService() to create the cross-account VPCE.',
      exportName: `AgenticAI-D03-PlatformInferenceEndpointServiceName-${stack.region}`,
    });
    new CfnOutput(this, 'NlbArn', {
      value: this.nlb.loadBalancerArn,
      description: 'NLB ARN (platform-account internal NLB fronting the endpoint service).',
    });
    new CfnOutput(this, 'NlbDnsName', {
      value: this.nlb.loadBalancerDnsName,
      description: 'NLB internal DNS name (platform-account consumers can hit directly).',
    });
  }
}
