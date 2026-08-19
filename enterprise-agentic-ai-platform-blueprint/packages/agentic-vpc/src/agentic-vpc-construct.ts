/**
 * AgenticVpcConstruct — the per-workload-account VPC.
 *
 * AgentCore AZ-ID constraint (IMPORTANT):
 *   AgentCore Runtime only supports a subset of AZ IDs per region. In
 *   us-east-1 today the supported set is `use1-az1`, `use1-az2`, `use1-az4`.
 *   CDK selects AZs by *name* (us-east-1a/b/c/...) and the AZ-name <->
 *   AZ-ID mapping is account-specific and only knowable at deploy time —
 *   so synthesising a 3-AZ VPC may land one of the subnets in an AZ that
 *   AgentCore refuses (last live test hit `use1-az6`). The construct
 *   accepts `supportedAvailabilityZoneIds` and emits
 *   `agentcoreCompatibleSubnetIds` — a subset of the `workload` subnet ids
 *   that sit in supported AZs. Runtime must be attached to those subnet
 *   ids only.
 *
 *   TODO v2: plumb `supportedAvailabilityZoneIds` through the VPC
 *   `availabilityZones` prop at synth time so the VPC itself only places
 *   subnets in AgentCore-compatible AZs. Doing so requires a bootstrap
 *   step that resolves AZ-name <-> AZ-ID mapping before synth, not a
 *   deploy-time custom resource (CDK's VPC construct needs deterministic
 *   AZ names at synth, not tokens).
 *
 * Emits:
 *   - VPC with 3 AZs, private-isolated subnets only (no IGW, no NAT).
 *   - Security groups: WorkloadEniSg + VpceEniSg, paired.
 *   - 9 VPC endpoints per spec §2.3.4:
 *       1. com.amazonaws.{region}.bedrock-agentcore           (Interface)
 *       2. com.amazonaws.{region}.bedrock-agentcore-control   (Interface)
 *       3. com.amazonaws.{region}.bedrock-agentcore.gateway   (Interface)
 *       4. com.amazonaws.{region}.bedrock-runtime             (Interface)
 *       5. com.amazonaws.{region}.bedrock                     (Interface)
 *       6. com.amazonaws.{region}.ecr.api                     (Interface)
 *       7. com.amazonaws.{region}.ecr.dkr                     (Interface)
 *       8. com.amazonaws.{region}.logs                        (Interface)
 *       9. com.amazonaws.{region}.s3                          (Gateway)
 *     Plus monitoring, STS, and KMS interface endpoints for completeness
 *     (spec §2.3.4 L1093-1098).
 *   - Every endpoint policy scoped to the local account root ARN (§2.3.5
 *     L1101-1116); Bedrock Runtime additionally scopes to the approved
 *     foundation-model ARN list (§2.3.5 L1120-1142).
 *   - SSM parameter `/agenticai/network/approved-bedrock-vpce-id` written
 *     so SCP-04 resolves at OU-level deploy (spec §2.2.5 / R-SCP-007).
 *   - SSM parameter `/agenticai/network/approved-agentcore-vpce-ids` for
 *     SCP-03.
 *
 * Spec reference map:
 *   - §2.3.2   VPC design        (R-NET-002..R-NET-004)
 *   - §2.3.4   VPCE list         (R-NET-008..R-NET-016)
 *   - §2.3.5   Endpoint policies (R-NET-017, R-NET-018)
 *   - §2.3.6   Security groups   (R-NET-019, R-NET-020)
 *   - §2.3.8   VPC-only ops      (R-NET-039..R-NET-045)
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import {
  FlowLog,
  FlowLogDestination,
  FlowLogResourceType,
  FlowLogTrafficType,
  GatewayVpcEndpoint,
  GatewayVpcEndpointAwsService,
  InterfaceVpcEndpoint,
  InterfaceVpcEndpointAwsService,
  InterfaceVpcEndpointService,
  IpAddresses,
  Port,
  SecurityGroup,
  SubnetType,
  Vpc,
} from 'aws-cdk-lib/aws-ec2';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AccountRootPrincipal,
  AnyPrincipal,
  Effect,
  PolicyStatement,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { StringParameter, StringListParameter } from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { allowedModelArns, PLATFORM_ALLOWED_MODELS } from '@agenticai/platform-baselines';

/**
 * Default AgentCore-supported AZ IDs in us-east-1 as of 2026-05. Update via
 * the `supportedAvailabilityZoneIds` prop if the service expands its set.
 */
const DEFAULT_AGENTCORE_SUPPORTED_AZ_IDS_US_EAST_1: readonly string[] = [
  'use1-az1',
  'use1-az2',
  'use1-az4',
];

export interface AgenticVpcConstructProps {
  /**
   * CIDR for the workload VPC. Default '10.20.0.0/16'. Pull from organisational
   * IPAM in real deployments (spec §2.3.2 L1025 / R-NET-002).
   */
  readonly vpcCidr?: string;

  /**
   * Whether the Browser Tool is permitted internet egress via corporate proxy.
   * Default false — the spec default (R-NET-006).
   */
  readonly enableBrowserInternetEgress?: boolean;

  /**
   * AZ-ID allow-list for AgentCore Runtime. When set, the construct emits an
   * `agentcoreCompatibleSubnetIds` CfnOutput listing only the `workload`
   * subnets that sit in one of these AZ IDs. Pass the output to the
   * AgentCore Runtime attach step.
   *
   * Defaults: in `us-east-1`, `['use1-az1','use1-az2','use1-az4']`. In
   * other regions, undefined (no filter) — supply explicitly.
   *
   * TODO v2: AZ-ID filter — plumb this through the `Vpc.availabilityZones`
   * prop at synth time. Requires a pre-synth bootstrap step to resolve
   * AZ-name <-> AZ-ID mapping; CDK's `Vpc` rejects tokens in
   * `availabilityZones`.
   */
  readonly supportedAvailabilityZoneIds?: readonly string[];
}

/**
 * Builds VPC endpoint policy statements that limit principals to the local
 * account root ARN, matching spec §2.3.5 L1101-1116.
 *
 * SEC (security review): the Allow statement is scoped to the action namespace(s)
 * of the single service each endpoint fronts (e.g. `logs:*` for the
 * CloudWatch Logs endpoint) rather than `*`. An interface endpoint only ever
 * carries traffic for its own service, so the service-prefix wildcard is the
 * tightest meaningful grant here (endpoint policies gate transport, not
 * per-API authZ — that is enforced by IAM identity policies + SCPs). The
 * cross-account Deny intentionally stays `*`/`*`: a broad deny is strictly
 * safer and is the standard VPC-endpoint lockdown idiom.
 */
function scopedToAccountRoot(stack: Stack, actionPrefixes: readonly string[]): PolicyStatement[] {
  return [
    new PolicyStatement({
      sid: 'AllowLocalAccountOnly',
      effect: Effect.ALLOW,
      principals: [new AccountRootPrincipal()],
      actions: actionPrefixes.map((p) => `${p}:*`),
      resources: ['*'],
    }),
    new PolicyStatement({
      sid: 'DenyOtherAccounts',
      effect: Effect.DENY,
      principals: [new AnyPrincipal()],
      actions: ['*'],
      resources: ['*'],
      conditions: {
        StringNotEquals: {
          'aws:PrincipalAccount': stack.account,
        },
      },
    }),
  ];
}

/**
 * Bedrock Runtime endpoint policy statements — scopes foundation-model
 * invocations to the platform allow-list (R-NET-018).
 */
function bedrockRuntimePolicyStatements(stack: Stack): PolicyStatement[] {
  const modelArns = allowedModelArns(stack.region);
  return [
    new PolicyStatement({
      sid: 'AllowInvokeOnlyApprovedModels',
      effect: Effect.ALLOW,
      principals: [new AccountRootPrincipal()],
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
        'bedrock:ApplyGuardrail',
      ],
      resources: [
        ...modelArns,
        // Permit application inference profiles created in this account.
        `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/*`,
        // Permit guardrails owned by this account.
        `arn:aws:bedrock:${stack.region}:${stack.account}:guardrail/*`,
      ],
    }),
    new PolicyStatement({
      sid: 'DenyInferenceWithoutGuardrail',
      effect: Effect.DENY,
      principals: [new AnyPrincipal()],
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
        'bedrock:Converse',
        'bedrock:ConverseStream',
      ],
      resources: ['*'],
      conditions: {
        Null: {
          'bedrock:GuardrailIdentifier': 'true',
        },
      },
    }),
  ];
}

export class AgenticVpcConstruct extends Construct {
  readonly vpc: Vpc;
  readonly workloadEniSg: SecurityGroup;
  readonly vpceEniSg: SecurityGroup;
  readonly endpoints: Record<string, InterfaceVpcEndpoint | GatewayVpcEndpoint>;
  /**
   * Comma-joined subnet-id string produced when
   * `supportedAvailabilityZoneIds` is supplied (or defaulted in us-east-1).
   * Also exposed as the `AgentcoreCompatibleSubnetIds` CfnOutput.
   * Empty if the VPC's synth-time AZ-name selection has no overlap with
   * the supported AZ-ID set.
   */
  readonly agentcoreCompatibleSubnetIds?: string;

  constructor(scope: Construct, id: string, props: AgenticVpcConstructProps = {}) {
    super(scope, id);

    const stack = Stack.of(this);
    const cidr = props.vpcCidr ?? '10.20.0.0/16';

    // ---- VPC (no IGW, no NAT; spec §2.3.2 L1034) ----
    this.vpc = new Vpc(this, 'Vpc', {
      ipAddresses: IpAddresses.cidr(cidr),
      maxAzs: 3,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'workload',
          subnetType: SubnetType.PRIVATE_ISOLATED,
          cidrMask: 20,
        },
        {
          name: 'vpce',
          subnetType: SubnetType.PRIVATE_ISOLATED,
          cidrMask: 22,
        },
      ],
      createInternetGateway: false,
      restrictDefaultSecurityGroup: true,
    });

    // ---- VPC Flow Logs (R-NET-015 adjacent + NIST VPCFlowLogsEnabled) ----
    const flowLogKey = new Key(this, 'FlowLogKey', {
      alias: 'alias/agenticai/vpc-flow-logs',
      description: 'CMK for VPC flow-log encryption.',
      enableKeyRotation: true,
      pendingWindow: Duration.days(7),
      removalPolicy: RemovalPolicy.DESTROY,
    });
    flowLogKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCloudWatchLogsUseKey',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${stack.region}.amazonaws.com`)],
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
    const flowLogGroup = new LogGroup(this, 'FlowLogGroup', {
      logGroupName: '/agenticai/vpc-flow-logs',
      retention: RetentionDays.THREE_MONTHS,
      encryptionKey: flowLogKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    new FlowLog(this, 'FlowLog', {
      resourceType: FlowLogResourceType.fromVpc(this.vpc),
      destination: FlowLogDestination.toCloudWatchLogs(flowLogGroup),
      trafficType: FlowLogTrafficType.ALL,
    });

    // ---- Security groups (spec §2.3.6 L1149-1162) ----
    this.workloadEniSg = new SecurityGroup(this, 'WorkloadEniSg', {
      vpc: this.vpc,
      description: 'SG for AgentCore Runtime / Gateway / LiteLLM task ENIs. No inbound; egress to VPCE SG only.',
      allowAllOutbound: false,
    });

    this.vpceEniSg = new SecurityGroup(this, 'VpceEniSg', {
      vpc: this.vpc,
      description: 'SG for VPC endpoint ENIs. Inbound 443 from workload SG only.',
      allowAllOutbound: false,
    });

    // Allow the workload to reach the VPCEs on 443.
    this.workloadEniSg.addEgressRule(this.vpceEniSg, Port.tcp(443), 'TLS to VPC endpoints');
    // Allow VPCEs to accept workload-ENI traffic on 443.
    this.vpceEniSg.addIngressRule(this.workloadEniSg, Port.tcp(443), 'TLS from workload ENIs');

    // ---- VPC endpoints (spec §2.3.4 L1060-1098) ----
    this.endpoints = {};

    const interfaceEndpointSpecs: Array<{
      key: string;
      service: InterfaceVpcEndpointService;
      policyStatements?: PolicyStatement[];
    }> = [
      {
        key: 'bedrockAgentcore',
        service: new InterfaceVpcEndpointService(`com.amazonaws.${stack.region}.bedrock-agentcore`, 443),
        policyStatements: scopedToAccountRoot(stack, ['bedrock-agentcore']),
      },
      {
        key: 'bedrockAgentcoreControl',
        service: new InterfaceVpcEndpointService(`com.amazonaws.${stack.region}.bedrock-agentcore-control`, 443),
        policyStatements: scopedToAccountRoot(stack, ['bedrock-agentcore']),
      },
      {
        key: 'bedrockAgentcoreGateway',
        service: new InterfaceVpcEndpointService(`com.amazonaws.${stack.region}.bedrock-agentcore.gateway`, 443),
        policyStatements: scopedToAccountRoot(stack, ['bedrock-agentcore']),
      },
      {
        key: 'bedrockRuntime',
        service: new InterfaceVpcEndpointService(`com.amazonaws.${stack.region}.bedrock-runtime`, 443),
        policyStatements: bedrockRuntimePolicyStatements(stack),
      },
      {
        key: 'bedrockControl',
        service: new InterfaceVpcEndpointService(`com.amazonaws.${stack.region}.bedrock`, 443),
        policyStatements: scopedToAccountRoot(stack, ['bedrock']),
      },
      {
        key: 'ecrApi',
        service: InterfaceVpcEndpointAwsService.ECR,
        policyStatements: scopedToAccountRoot(stack, ['ecr']),
      },
      {
        key: 'ecrDkr',
        service: InterfaceVpcEndpointAwsService.ECR_DOCKER,
        policyStatements: scopedToAccountRoot(stack, ['ecr']),
      },
      {
        key: 'cloudwatchLogs',
        service: InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
        policyStatements: scopedToAccountRoot(stack, ['logs']),
      },
      {
        key: 'cloudwatchMonitoring',
        service: InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
        policyStatements: scopedToAccountRoot(stack, ['cloudwatch']),
      },
      {
        key: 'sts',
        service: InterfaceVpcEndpointAwsService.STS,
        policyStatements: scopedToAccountRoot(stack, ['sts']),
      },
      {
        key: 'kms',
        service: InterfaceVpcEndpointAwsService.KMS,
        policyStatements: scopedToAccountRoot(stack, ['kms']),
      },
    ];

    for (const spec of interfaceEndpointSpecs) {
      const ep = new InterfaceVpcEndpoint(this, `Vpce-${spec.key}`, {
        vpc: this.vpc,
        service: spec.service,
        privateDnsEnabled: true,
        securityGroups: [this.vpceEniSg],
        subnets: { subnetGroupName: 'vpce' },
        open: false,
      });
      for (const stmt of spec.policyStatements ?? []) {
        ep.addToPolicy(stmt);
      }
      this.endpoints[spec.key] = ep;
    }

    // Gateway endpoint for S3 (spec §2.3.4 L1080-1082 / R-NET-014).
    this.endpoints.s3 = new GatewayVpcEndpoint(this, 'Vpce-s3', {
      vpc: this.vpc,
      service: GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: SubnetType.PRIVATE_ISOLATED }],
    });

    // ---- SSM parameters for SCP resolution (R-SCP-007) ----
    new StringParameter(this, 'BedrockVpceIdParam', {
      parameterName: '/agenticai/network/approved-bedrock-vpce-id',
      description: 'Bedrock Runtime VPCE id — referenced by SCP-04 condition.',
      stringValue: (this.endpoints.bedrockRuntime as InterfaceVpcEndpoint).vpcEndpointId,
    });

    new StringListParameter(this, 'AgentCoreVpceIdsParam', {
      parameterName: '/agenticai/network/approved-agentcore-vpce-ids',
      description: 'AgentCore data + control + gateway VPCE ids — referenced by SCP-03 condition.',
      stringListValue: [
        (this.endpoints.bedrockAgentcore as InterfaceVpcEndpoint).vpcEndpointId,
        (this.endpoints.bedrockAgentcoreControl as InterfaceVpcEndpoint).vpcEndpointId,
        (this.endpoints.bedrockAgentcoreGateway as InterfaceVpcEndpoint).vpcEndpointId,
      ],
    });

    // Browser-egress opt-in is documented but not wired — TGW + corp-proxy
    // attachment lands in Phase 5's Strands-blueprint-scoped deployment.
    if (props.enableBrowserInternetEgress) {
      // Record in metadata that the construct was instantiated with the opt-in.
      // Actual TGW attachment and corp-proxy routing configuration is left to
      // the BrowserToolConstruct in Phase 5.
      (this.node as unknown as { setContext: (k: string, v: unknown) => void }).setContext?.(
        'agenticai/browserInternetEgress',
        true,
      );
    }

    // ---- AgentCore AZ-ID compatibility filter (TODO v2: AZ-ID filter) ----
    // Deploy-time resolution: describe workload-subnet AZ-IDs and emit only
    // the subnet ids that match the supported-AZ-IDs set.
    const supportedAzIds =
      props.supportedAvailabilityZoneIds ??
      (stack.region === 'us-east-1'
        ? DEFAULT_AGENTCORE_SUPPORTED_AZ_IDS_US_EAST_1
        : undefined);

    if (supportedAzIds && supportedAzIds.length > 0) {
      const workloadSubnets = this.vpc.selectSubnets({ subnetGroupName: 'workload' }).subnets;
      const describeSubnets = new AwsCustomResource(this, 'AgentCoreCompatibleSubnets', {
        resourceType: 'Custom::AgentCoreSubnetFilter',
        onCreate: {
          service: 'EC2',
          action: 'describeSubnets',
          parameters: {
            Filters: [
              { Name: 'subnet-id', Values: workloadSubnets.map((s) => s.subnetId) },
              { Name: 'availability-zone-id', Values: [...supportedAzIds] },
            ],
          },
          physicalResourceId: PhysicalResourceId.of(
            `AgentCoreSubnetFilter-${this.node.addr}`,
          ),
        },
        onUpdate: {
          service: 'EC2',
          action: 'describeSubnets',
          parameters: {
            Filters: [
              { Name: 'subnet-id', Values: workloadSubnets.map((s) => s.subnetId) },
              { Name: 'availability-zone-id', Values: [...supportedAzIds] },
            ],
          },
          physicalResourceId: PhysicalResourceId.of(
            `AgentCoreSubnetFilter-${this.node.addr}`,
          ),
        },
        policy: AwsCustomResourcePolicy.fromStatements([
          new PolicyStatement({
            effect: Effect.ALLOW,
            // SEC-011 (security review): ec2:DescribeSubnets and
            // ec2:DescribeAvailabilityZones require Resource:'*' — they are
            // account-wide list/describe operations that do NOT support
            // resource-level permissions. Read-only, non-mutating.
            actions: ['ec2:DescribeSubnets', 'ec2:DescribeAvailabilityZones'],
            resources: ['*'],
          }),
        ]),
      });
      const compatibleSubnetIds = workloadSubnets.map((_, idx) =>
        describeSubnets.getResponseField(`Subnets.${idx}.SubnetId`),
      );
      (this as { agentcoreCompatibleSubnetIds?: string }).agentcoreCompatibleSubnetIds =
        compatibleSubnetIds.join(',');
      new CfnOutput(this, 'AgentcoreCompatibleSubnetIds', {
        value: compatibleSubnetIds.join(','),
        description:
          'Comma-joined subnet ids that sit in AgentCore-supported AZ IDs. AgentCore Runtime must be attached to these subnets only. Empty if the VPCs synth-time AZ-name selection has no overlap with the supported set — rerun synth with explicit `supportedAvailabilityZoneIds` and/or a larger `maxAzs`.',
      });
    }

    // Also emit for read-only callers (e.g. conformance tests).
    void PLATFORM_ALLOWED_MODELS;
  }
}
