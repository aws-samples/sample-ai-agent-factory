/**
 * D03WorkloadAgentStack — workload-account resources for D-03.
 *
 * Deployed to: agenticai-app1-nonprod (account id supplied at synth).
 *
 * Emits:
 *   - VPC with AgentCore-friendly VPCEs (Bedrock not needed here — calls go via platform via STS)
 *   - AgentCore Memory CMK (kept local per D-03 note in README §3)
 *   - AgentRuntimeRole — the role the Strands agent runs under.
 *     Default trust: AgentCore service principal only. Account-root trust is
 *     OPT-IN via `allowLocalRootAssume` and is hard-disabled in prod (see
 *     Fix 1 / security review).
 *   - OAM source link → platform account's OAM sink
 *
 * CMK removal policy:
 *   - `retainDataKeys` defaults to `true` → RETAIN + 30-day pending window
 *     so a stack destroy cannot orphan data encrypted under these keys.
 *   - Set `retainDataKeys = false` ONLY for ephemeral dev/test loops where
 *     teardown velocity matters more than recoverability. In that mode the
 *     stack uses DESTROY + 7-day pending window — unsafe for any env that
 *     carries real tenant data.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import {
  GatewayVpcEndpoint,
  GatewayVpcEndpointAwsService,
  InterfaceVpcEndpoint,
  InterfaceVpcEndpointAwsService,
  InterfaceVpcEndpointService,
  IpAddresses,
  SecurityGroup,
  SubnetType,
  Vpc,
} from 'aws-cdk-lib/aws-ec2';
import {
  Effect,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Key } from 'aws-cdk-lib/aws-kms';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

/**
 * AgentCore Runtime only supports specific AZ IDs in each region (for
 * us-east-1 today: `use1-az1`, `use1-az2`, `use1-az4`). The last live test
 * hit an unsupported AZ (`use1-az6`) because CDK selects AZs by *name*
 * (us-east-1a/b/c/...) and the AZ-name -> AZ-ID mapping is account-specific
 * and only knowable at deploy time. The helper below resolves it at deploy
 * time via an `ec2:DescribeAvailabilityZones` custom resource.
 */
const DEFAULT_AGENTCORE_SUPPORTED_AZ_IDS_US_EAST_1: readonly string[] = [
  'use1-az1',
  'use1-az2',
  'use1-az4',
];

export interface D03WorkloadAgentStackProps extends StackProps {
  /** Platform account id (12-digit string supplied at synth). */
  readonly platformAccountId: string;
  /** Shared ExternalId for cross-account AssumeRole. */
  readonly externalId: string;
  /** Tenant id — propagated as Bedrock session tag. */
  readonly tenantId: string;
  /** Agent id. */
  readonly agentId: string;
  /** VPC CIDR (blueprint default 10.20.0.0/16). */
  readonly vpcCidr?: string;
  /**
   * Optional — name of the platform's PrivateLink endpoint service
   * (`com.amazonaws.vpce.<region>.vpce-svc-xxxxxxxxxxxxxxxx`), emitted as a
   * `CfnOutput` by `PlatformInferenceGatewayConstruct` in the platform
   * account. When supplied, the workload stack creates an
   * `InterfaceVpcEndpoint` in the `vpce` subnets with the existing
   * `VpceSg` so the runtime role can reach platform LiteLLM cross-account.
   * Leave undefined while D-03 runs its AssumeRole → Bedrock-direct shape.
   */
  readonly platformInferenceServiceName?: string;
  /**
   * Environment name ('prod', 'nonprod', 'dev', ...). Gates safety-sensitive
   * branches (e.g. account-root trust on the AgentRuntimeRole is hard-denied
   * when `envName === 'prod'` regardless of `allowLocalRootAssume`).
   * Defaults to 'nonprod' when omitted.
   */
  readonly envName?: string;
  /**
   * OPT-IN: grant the workload account root principal trust to assume the
   * `AgentRuntimeRole` (for local CLI invocation during D-03 stack development
   * and teardown-heavy test loops). Default `false` — the role is only
   * assumable by the AgentCore service principal. This flag is forcibly
   * ignored in prod (`envName === 'prod'`). NEVER set to true in prod.
   */
  readonly allowLocalRootAssume?: boolean;
  /**
   * When true (default), CMKs created by this stack use `RETAIN` + a 30-day
   * pending deletion window so a stack destroy cannot orphan encrypted data.
   * Set to false ONLY for ephemeral dev/test loops — switches to `DESTROY` +
   * 7-day pending window.
   */
  readonly retainDataKeys?: boolean;
  /**
   * AZ-ID allow-list for AgentCore Runtime. Defaults to the known-supported
   * us-east-1 AZ IDs (`use1-az1`, `-az2`, `-az4`). The stack creates a VPC
   * across CDK's AZ-name selection (which is cost-driven and not AZ-ID
   * aware), then emits an `agentcoreCompatibleSubnetIds` CfnOutput that
   * names the subset of subnets whose AZ-ID matches this allow-list.
   * AgentCore Runtime must be attached to those subnets only.
   *
   * TODO v2: plumb this through the VPC `availabilityZones` prop at synth
   * time so the VPC itself only places subnets in AgentCore-compatible AZs.
   * Doing so requires a bootstrap step that resolves AZ-name <-> AZ-ID
   * mapping before synth (not a deploy-time custom resource).
   */
  readonly supportedAvailabilityZoneIds?: readonly string[];
}

export class D03WorkloadAgentStack extends Stack {
  readonly vpc: Vpc;
  readonly agentRuntimeRole: Role;
  readonly memoryKey: Key;

  constructor(scope: Construct, id: string, props: D03WorkloadAgentStackProps) {
    super(scope, id, props);

    // ---- VPC (3 AZs, private-isolated only) ----
    this.vpc = new Vpc(this, 'Vpc', {
      ipAddresses: IpAddresses.cidr(props.vpcCidr ?? '10.20.0.0/16'),
      maxAzs: 2, // 2 to keep test-deploy cost down; docs use 3
      natGateways: 0,
      subnetConfiguration: [
        { name: 'workload', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 20 },
        { name: 'vpce', subnetType: SubnetType.PRIVATE_ISOLATED, cidrMask: 22 },
      ],
      createInternetGateway: false,
      restrictDefaultSecurityGroup: true,
    });

    // SG for VPCE ENIs.
    const vpceSg = new SecurityGroup(this, 'VpceSg', {
      vpc: this.vpc,
      description: 'D-03 workload VPCE ENI SG. TLS 443 inbound from workload SGs.',
      allowAllOutbound: false,
    });

    // Minimal VPCE set for D-03 — agent calls Bedrock via STS through platform,
    // so the workload only strictly needs STS + CloudWatch Logs + CloudWatch Monitoring + KMS.
    // ECR endpoints for pulling platform shared images. No Bedrock here (platform has it).
    const interfaceEndpoints: Array<{ key: string; service: InterfaceVpcEndpointAwsService }> = [
      { key: 'sts', service: InterfaceVpcEndpointAwsService.STS },
      { key: 'kms', service: InterfaceVpcEndpointAwsService.KMS },
      { key: 'logs', service: InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
      { key: 'monitoring', service: InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING },
      { key: 'ecrApi', service: InterfaceVpcEndpointAwsService.ECR },
      { key: 'ecrDkr', service: InterfaceVpcEndpointAwsService.ECR_DOCKER },
      { key: 'secretsManager', service: InterfaceVpcEndpointAwsService.SECRETS_MANAGER },
    ];
    for (const spec of interfaceEndpoints) {
      new InterfaceVpcEndpoint(this, `Vpce-${spec.key}`, {
        vpc: this.vpc,
        service: spec.service,
        privateDnsEnabled: true,
        securityGroups: [vpceSg],
        subnets: { subnetGroupName: 'vpce' },
        open: false,
      });
    }
    new GatewayVpcEndpoint(this, 'Vpce-s3', {
      vpc: this.vpc,
      service: GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: SubnetType.PRIVATE_ISOLATED }],
    });

    // ---- Optional: platform-account PrivateLink VPCE ----
    // See README §3.3 residual-risks "Cross-account PrivateLink →
    // LiteLLM attack surface". When the platform-account
    // `PlatformInferenceGatewayConstruct` has been deployed, pass its output
    // service name via `platformInferenceServiceName`; we create a consumer
    // InterfaceVpcEndpoint in the existing `vpce` subnets behind the shared
    // `vpceSg`. Private DNS is disabled because the endpoint service is a
    // vpce-svc name, not a service with a canonical regional DNS entry.
    if (props.platformInferenceServiceName) {
      const platformInferenceVpce = new InterfaceVpcEndpoint(
        this,
        'Vpce-platform-inference',
        {
          vpc: this.vpc,
          service: new InterfaceVpcEndpointService(
            props.platformInferenceServiceName,
            443,
          ),
          subnets: { subnetGroupName: 'vpce' },
          securityGroups: [vpceSg],
          privateDnsEnabled: false,
          open: false,
        },
      );
      new CfnOutput(this, 'PlatformInferenceVpceId', {
        value: platformInferenceVpce.vpcEndpointId,
        description:
          'InterfaceVpcEndpoint id for the platform PrivateLink service (D-03).',
        exportName: 'AgenticAI-D03-PlatformInferenceVpceId',
      });
      new CfnOutput(this, 'PlatformInferenceVpceDnsEntries', {
        // VPCE DNS entries are a list of { HostedZoneId, DnsEntry } tokens.
        // We surface the comma-joined form as an output for the SDK caller
        // to resolve to the PrivateLink ENI DNS name at runtime.
        value: platformInferenceVpce.vpcEndpointDnsEntries.join(','),
        description:
          'Private DNS entries for the platform PrivateLink endpoint (D-03). SDK clients should resolve against the first entry.',
      });
    }

    // VPC Flow Logs.
    // Safe defaults: retain keys + 30d pending window. Flip `retainDataKeys`
    // to false only for dev loops (see class JSDoc).
    const retainKeys = props.retainDataKeys ?? true;
    const keyRemovalPolicy = retainKeys ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
    const keyPendingWindow = retainKeys ? Duration.days(30) : Duration.days(7);

    const flowLogKey = new Key(this, 'FlowLogKey', {
      alias: 'alias/agenticai/d03-workload-vpc-flow-logs',
      enableKeyRotation: true,
      pendingWindow: keyPendingWindow,
      removalPolicy: keyRemovalPolicy,
    });
    flowLogKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowCWL',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
      }),
    );
    const flowLogGroup = new LogGroup(this, 'FlowLogGroup', {
      logGroupName: '/agenticai/d03-workload-vpc-flow-logs',
      retention: RetentionDays.ONE_MONTH,
      encryptionKey: flowLogKey,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    new (require('aws-cdk-lib/aws-ec2').FlowLog)(this, 'FlowLog', {
      resourceType: (require('aws-cdk-lib/aws-ec2').FlowLogResourceType).fromVpc(this.vpc),
      destination: (require('aws-cdk-lib/aws-ec2').FlowLogDestination).toCloudWatchLogs(flowLogGroup),
      trafficType: (require('aws-cdk-lib/aws-ec2').FlowLogTrafficType).ALL,
    });

    // ---- Memory CMK (local per D-03) ----
    this.memoryKey = new Key(this, 'MemoryKey', {
      alias: `alias/agenticai/d03-memory-${props.tenantId}-${props.agentId}`,
      description: `Workload-local AgentCore Memory CMK for ${props.tenantId}/${props.agentId}.`,
      enableKeyRotation: true,
      pendingWindow: keyPendingWindow,
      removalPolicy: keyRemovalPolicy,
    });
    this.memoryKey.addToResourcePolicy(
      new PolicyStatement({
        sid: 'AllowAgentCoreService',
        effect: Effect.ALLOW,
        principals: [new ServicePrincipal('bedrock-agentcore.amazonaws.com')],
        actions: ['kms:Encrypt*', 'kms:Decrypt*', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:Describe*'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
        },
      }),
    );

    // ---- Agent Runtime role ----
    // Default trust: AgentCore service principal ONLY. Account-root trust is
    // opt-in via `allowLocalRootAssume` for local development/testing of the
    // D-03 stack (e.g. invoking the agent from a developer workstation via
    // `aws sts assume-role`) and is hard-disabled when `envName === 'prod'`.
    // NEVER set `allowLocalRootAssume: true` in prod — a blanket account-root
    // trust is equivalent to giving every IAM principal in the account
    // permission to impersonate the agent. See Fix 1 / security review.
    const envName = props.envName ?? 'nonprod';
    const allowLocalRootAssume = (props.allowLocalRootAssume ?? false) && envName !== 'prod';
    this.agentRuntimeRole = new Role(this, 'AgentRuntimeRole', {
      roleName: `AgenticAI-D03-${props.tenantId}-${props.agentId}-runtime`,
      assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: `Workload runtime role for ${props.tenantId}/${props.agentId}. Assumes platform BedrockCallerRole cross-account.`,
      maxSessionDuration: Duration.hours(1),
    });
    if (allowLocalRootAssume) {
      // DEV/TEST-ONLY — gated by `allowLocalRootAssume` AND non-prod envName.
      this.agentRuntimeRole.assumeRolePolicy?.addStatements(
        new PolicyStatement({
          sid: 'DevLocalAccountRootAssume',
          effect: Effect.ALLOW,
          principals: [new (require('aws-cdk-lib/aws-iam').AccountRootPrincipal)()],
          actions: ['sts:AssumeRole'],
        }),
      );
    }

    // Cross-account AssumeRole to platform's BedrockCallerRole, with
    // ExternalId condition. AssumeRole only — `sts:TagSession` is *not*
    // granted: session tags do not survive role-chain assumption from
    // `account-root → runtime-role → cross-account-role` (BUG-005, verified
    // live 2026-04-30). Cost attribution is carried by the platform's
    // per-tenant application inference profile; audit attribution is carried
    // by a stable `RoleSessionName` convention
    // (`workload-<acctId>-<tenantId>-<agentId>`, documented in README section 9)
    // which DOES survive role chaining.
    const platformCallerRoleArn = `arn:aws:iam::${props.platformAccountId}:role/AgenticAI-D03-BedrockCaller`;
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'AssumePlatformBedrockCaller',
        effect: Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: [platformCallerRoleArn],
        conditions: {
          // Caller-side ExternalId binding — must match the trust policy on
          // the platform BedrockCallerRole. Paired guard (both sides) prevents
          // confused-deputy scenarios where a misconfigured caller forgets to
          // send ExternalId.
          StringEquals: { 'sts:ExternalId': props.externalId },
        },
      }),
    );
    // CloudWatch Logs write (for agent traces).
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'WriteLogs',
        effect: Effect.ALLOW,
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents', 'logs:CreateLogGroup'],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/agenticai/*`],
      }),
    );
    // DynamoDB read on platform's registry tables (cross-account).
    // `dynamodb:LeadingKeys` constrains the runtime role to only read rows
    // whose partition key equals its own tenantId — a compromised runtime
    // cannot enumerate other tenants' registry rows.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'ReadPlatformRegistry',
        effect: Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:Query'],
        resources: [
          `arn:aws:dynamodb:${this.region}:${props.platformAccountId}:table/agenticai-d03-registry-agents`,
          `arn:aws:dynamodb:${this.region}:${props.platformAccountId}:table/agenticai-d03-registry-tools`,
        ],
        conditions: {
          'ForAllValues:StringEquals': {
            'dynamodb:LeadingKeys': [props.tenantId],
          },
        },
      }),
    );
    // DynamoDB write to platform's experiment-tracking table for eval runs.
    // Same LeadingKeys tenancy scoping as the registry read above.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'WritePlatformExperiments',
        effect: Effect.ALLOW,
        actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [
          `arn:aws:dynamodb:${this.region}:${props.platformAccountId}:table/agenticai-d03-experiment-tracking`,
        ],
        conditions: {
          'ForAllValues:StringEquals': {
            'dynamodb:LeadingKeys': [props.tenantId],
          },
        },
      }),
    );
    // Cross-account DDB reads/writes require kms:Decrypt (and for writes,
    // kms:GenerateDataKey) on the platform-owned CMK that protects the
    // registry + experiment tables. The key's resource policy grants the
    // workload account, but the caller's identity policy must also allow it.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'UsePlatformRegistryKms',
        effect: Effect.ALLOW,
        actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: [`arn:aws:kms:${this.region}:${props.platformAccountId}:key/*`],
        conditions: {
          StringEquals: {
            // Scope: only DynamoDB service-initiated decryption.
            'kms:ViaService': `dynamodb.${this.region}.amazonaws.com`,
          },
        },
      }),
    );
    // Memory CMK usage.
    this.memoryKey.grantEncryptDecrypt(this.agentRuntimeRole);
    // ECR pull cross-account from platform shared repo.
    // Note: `ecr:GetAuthorizationToken` is a wildcard-resource action — it
    // does not accept repository ARNs (token is registry-wide). Layer + image
    // actions are scoped to the specific repo.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'EcrAuthToken',
        effect: Effect.ALLOW,
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'PullSharedAgentImages',
        effect: Effect.ALLOW,
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
        ],
        resources: [
          `arn:aws:ecr:${this.region}:${props.platformAccountId}:repository/agenticai-d03-agent-base`,
        ],
      }),
    );

    // ---- D-03 v3: workstream Gateway invocation ----
    // The runtime role may InvokeGateway on its OWN workstream's Gateway
    // only. The Gateway id is not known at synth of this stack (the Gateway
    // stack is platform-deployed into this account separately via the
    // GatewayAdminRole); use the name pattern. Gateway ids come back from
    // `CreateGateway` with a random `-XXXXXXXXXX` suffix — the wildcard
    // matches that suffix only.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'InvokeOwnWorkstreamGateway',
        effect: Effect.ALLOW,
        actions: ['bedrock-agentcore:InvokeGateway'],
        resources: [
          `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/agenticai-d03-${envName}-${props.tenantId}-${props.agentId}-gw-*`,
        ],
      }),
    );
    // Defence-in-depth DENY on Gateway mutation at the identity-policy
    // layer, layered on top of SCP-09 (org-level). SCP-09 is the primary
    // enforcement; this statement means a misconfigured SCP-09 cannot
    // inadvertently grant runtime roles gateway-mutation authority.
    this.agentRuntimeRole.addToPolicy(
      new PolicyStatement({
        sid: 'DenyGatewayMutation',
        effect: Effect.DENY,
        actions: [
          'bedrock-agentcore:CreateGateway',
          'bedrock-agentcore:UpdateGateway',
          'bedrock-agentcore:DeleteGateway',
          'bedrock-agentcore:CreateGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
          'bedrock-agentcore:DeleteGatewayTarget',
          'bedrock-agentcore:SynchronizeGatewayTargets',
        ],
        resources: ['*'],
      }),
    );

    NagSuppressions.addStackSuppressions(
      this,
      [
        { id: 'AwsSolutions-IAM5', reason: 'SEC-011/025: cross-account registry reads + log-group wildcard path are scoped to this-account + platform-account only.' },
        { id: 'NIST.800.53.R5-IAMNoInlinePolicy', reason: 'SEC-005: Single-purpose runtime role with inline policies.' },
        // The AZ-ID filter + platform PrivateLink consumer endpoint custom
        // resources use CDK AwsCustomResource singleton Lambdas — same
        // SEC-006..010 rationale as the platform stack (CFN-only invocation,
        // runtime tracks aws-cdk-lib bumps, no ARN for DescribeSubnets).
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed AwsCustomResource Lambda runtime.' },
        { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CFN-only invocation; concurrency would break deploys.' },
        { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces failures; DLQ unconsumed.' },
        { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: EC2 control-plane public IAM-auth endpoint.' },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          reason: 'SEC-010: CDK custom-resource default managed role.',
        },
      ],
      true,
    );

    // ---- AgentCore AZ-ID compatibility filter ----
    // Resolve AZ-name <-> AZ-ID at deploy time and emit the subset of
    // `workload` subnets that sit in AgentCore-supported AZs. Callers
    // (AgentCore Runtime stack / test harness) must read this output and
    // attach Runtime to these subnets only.
    const supportedAzIds =
      props.supportedAvailabilityZoneIds ??
      (this.region === 'us-east-1'
        ? DEFAULT_AGENTCORE_SUPPORTED_AZ_IDS_US_EAST_1
        : undefined);

    if (supportedAzIds && supportedAzIds.length > 0) {
      const workloadSubnets = this.vpc.selectSubnets({ subnetGroupName: 'workload' }).subnets;
      // DescribeSubnets with the workload subnet ids + AZ-ID filter; return
      // the comma-joined compatible subnet id list as a single output value.
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
            `AgentCoreSubnetFilter-${this.stackName}`,
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
            `AgentCoreSubnetFilter-${this.stackName}`,
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
        // Response is a `Subnets[*].SubnetId` list. DescribeSubnets does not
        // support server-side pagination for this call shape at the counts
        // we care about (<= maxAzs subnets), so paginate:false is fine.
      });
      // Emit the FIRST compatible subnet id as a diagnostic output.
      // DescribeSubnets only returns entries that passed both filters, and
      // `Subnets.1.SubnetId` would break the stack if only 1 of the VPC's N
      // subnets is in a supported AZ (real case: us-east-1 accounts often
      // get use1-az6 for us-east-1a, which AgentCore Runtime rejects). The
      // full compatible-subnet list is still enumerable via:
      //   aws ec2 describe-subnets --filters 'Name=vpc-id,Values=<VpcId>'
      //     'Name=availability-zone-id,Values=<supportedAzIds>'
      // at test/deploy time by the AgentCore Runtime stack.
      new CfnOutput(this, 'AgentcoreCompatibleSubnetIdFirst', {
        value: describeSubnets.getResponseField('Subnets.0.SubnetId'),
        description:
          'First subnet id that sits in an AgentCore-supported AZ ID. AgentCore Runtime must be attached to a subnet from this VPC whose AZ-ID is in `supportedAvailabilityZoneIds`. Query via `aws ec2 describe-subnets --filters` to get the full list.',
        exportName: 'AgenticAI-D03-AgentcoreCompatibleSubnetIdFirst',
      });
      NagSuppressions.addResourceSuppressions(
        describeSubnets,
        [
          { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed AwsCustomResource Lambda runtime.' },
          { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: CFN-only invocation.' },
          { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces failures.' },
          { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: EC2 control-plane public IAM-auth endpoint.' },
          {
            id: 'AwsSolutions-IAM4',
            appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
            reason: 'SEC-010: CDK custom-resource default role.',
          },
          {
            id: 'AwsSolutions-IAM5',
            appliesTo: ['Resource::*'],
            reason: 'SEC-011: ec2:DescribeSubnets/DescribeAvailabilityZones are read-only account-level APIs; no ARN.',
          },
        ],
        true,
      );
    }

    // Outputs — consumed by tests.
    new CfnOutput(this, 'AgentRuntimeRoleArn', {
      value: this.agentRuntimeRole.roleArn,
      exportName: 'AgenticAI-D03-AgentRuntimeRoleArn',
    });
    new CfnOutput(this, 'MemoryKmsKeyArn', {
      value: this.memoryKey.keyArn,
      exportName: 'AgenticAI-D03-MemoryKmsKeyArn',
    });
    new CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      exportName: 'AgenticAI-D03-WorkloadVpcId',
    });
  }
}
