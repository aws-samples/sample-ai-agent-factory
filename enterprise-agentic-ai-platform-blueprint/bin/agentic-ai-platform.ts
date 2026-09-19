#!/usr/bin/env node
/**
 * CDK App entry for the Enterprise Agentic AI Platform Blueprint.
 *
 * Stage routing:
 *   - `management` : Organization, OUs, SCPs. Deploy to the AWS Organization
 *                    management account. First phase to land.
 *   - `platform`   : Guardrail Admin, baseline guardrail template, Registry,
 *                    base images, CDK Pipelines. Deploys to
 *                    agenticai-platform-{nonprod,prod}.
 *   - `workload`   : Per-application Agentic VPC + 9 VPCEs, LiteLLM,
 *                    AgentCore Runtime/Gateway/Identity/Memory, application
 *                    inference profile. Deploys to agenticai-<app>-{nonprod,prod}.
 *   - `sandbox`    : SCP soak account.
 *
 * Stages and stacks are instantiated lazily based on the `stage` context value
 * so a single `cdk synth` matches a single concern at a time.
 *
 * cdk-nag Aspects:
 *   - `AwsSolutionsChecks` is always applied.
 *   - `NIST80053R5Checks` is applied when the `agenticai/regulated` context
 *     flag is true (default true).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { App, Aspects } from 'aws-cdk-lib';
import { AwsSolutionsChecks, NIST80053R5Checks } from 'cdk-nag';
import 'source-map-support/register';

import { OrgStack } from '../apps/management-account/lib/org-stack';
import { LogArchiveStack } from '../apps/platform-account/lib/log-archive-stack';
import { AuditStack } from '../apps/platform-account/lib/audit-stack';
import { GuardrailStack } from '../apps/platform-account/lib/guardrail-stack';
import { RegistryStack } from '../apps/platform-account/lib/registry-stack';
import { InferenceGatewayStack } from '../apps/platform-account/lib/inference-gateway-stack';
import { WorkloadNetworkStack } from '../apps/workload-account/lib/workload-network-stack';
import { WorkloadAppStack } from '../apps/workload-account/lib/workload-app-stack';
import { PlatformPipelineStack } from '../pipelines/platform-pipeline-stack';
import { WorkloadPipelineStack } from '../pipelines/workload-pipeline-stack';
import { D03PlatformCoreStack } from '../apps/platform-account/lib/d03-platform-core-stack';
import { D03WorkloadAgentStack } from '../apps/workload-account/lib/d03-workload-agent-stack';
import { D03WorkstreamGatewayStack } from '../apps/platform-account/lib/d03-workstream-gateway-stack';
import { GapClosureStack } from '../apps/workload-account/lib/gap-closure-stack';
import type { InferenceModelRateLimit } from '@agenticai/platform-inference-gateway';

const app = new App();

const stage: string | undefined = app.node.tryGetContext('stage');
const regulated: boolean = app.node.tryGetContext('agenticai/regulated') !== false;

/**
 * Read the platform Guardrail Admin role ARN. Until Phase 3 stands up the
 * real role, we default to a clearly-marked deploy-time placeholder. A real
 * deployment supplies the value via `-c agenticai/guardrailAdminRoleArn=...`
 * or via `cdk.context.json`.
 */
function guardrailAdminRoleArn(): string {
  const configured = app.node.tryGetContext('agenticai/guardrailAdminRoleArn');
  if (typeof configured === 'string' && configured.startsWith('arn:aws:iam::')) {
    return configured;
  }
  // Deploy-time placeholder. Using 000000000000 makes it obvious if this
  // leaks into a real environment; SCP-05 will deny everyone until replaced.
  return 'arn:aws:iam::000000000000:role/AgenticAI-PlaceholderUntilPhase3';
}

function stringArrayContext(key: string): readonly string[] {
  const raw = app.node.tryGetContext(key);
  if (raw === undefined) return [];

  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new Error(`Context '${key}' must be a JSON array of strings.`, { cause: error });
    }
  }
  if (
    !Array.isArray(parsed) ||
    parsed.some((value) => typeof value !== 'string' || value.length === 0)
  ) {
    throw new Error(`Context '${key}' must be a JSON array of non-empty strings.`);
  }
  return parsed;
}

function inferenceModelRateLimitsContext(
  key: string,
): readonly InferenceModelRateLimit[] {
  const raw = app.node.tryGetContext(key);
  if (raw === undefined) return [];

  let parsed: unknown = raw;
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new Error(`Context '${key}' must be a JSON array.`, { cause: error });
    }
  }
  if (!Array.isArray(parsed)) {
    throw new Error(`Context '${key}' must be an array of model rate limits.`);
  }

  return parsed.map((value, index) => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new Error(`Context '${key}[${index}]' must be an object.`);
    }
    const entry = value as Record<string, unknown>;
    if (
      typeof entry.qualifiedModelId !== 'string' ||
      typeof entry.requestsPerMinute !== 'number' ||
      typeof entry.tokensPerMinute !== 'number'
    ) {
      throw new Error(
        `Context '${key}[${index}]' requires qualifiedModelId, requestsPerMinute, and tokensPerMinute.`,
      );
    }
    return {
      qualifiedModelId: entry.qualifiedModelId,
      requestsPerMinute: entry.requestsPerMinute,
      tokensPerMinute: entry.tokensPerMinute,
    };
  });
}

function seedAvailabilityZoneContext(
  account: unknown,
  region: string,
  availabilityZones: readonly string[],
): void {
  if (typeof account !== 'string' || availabilityZones.length < 2) {
    throw new Error('Cannot seed Availability Zone context without an account and two zones.');
  }
  const key = `availability-zones:account=${account}:region=${region}`;
  const existing = app.node.tryGetContext(key);
  if (existing !== undefined && JSON.stringify(existing) !== JSON.stringify(availabilityZones)) {
    throw new Error(`Conflicting Availability Zone context for ${account} in ${region}.`);
  }
  if (existing === undefined) {
    app.node.setContext(key, [...availabilityZones]);
  }
}

switch (stage) {
  case 'management': {
    const attachToWorkloadsOu: boolean =
      app.node.tryGetContext('agenticai/attachScpsToWorkloadsOu') === true;
    new OrgStack(app, 'AgenticAI-Management-OrgStack', {
      env: {
        account: process.env.CDK_DEFAULT_ACCOUNT,
        region: process.env.CDK_DEFAULT_REGION ?? 'us-west-2',
      },
      platformGuardrailAdminRoleArn: guardrailAdminRoleArn(),
      attachToWorkloadsOu,
    });
    break;
  }
  case 'platform': {
    // Phase 2 — LogArchive + Audit stacks (deployed into the respective
    // Control-Tower-provisioned accounts).
    const orgId = app.node.tryGetContext('agenticai/organizationId');
    const rawWorkloadIds = app.node.tryGetContext('agenticai/workloadAccountIds');
    const workloadAccountIds: readonly string[] = Array.isArray(rawWorkloadIds)
      ? rawWorkloadIds
      : typeof rawWorkloadIds === 'string'
        ? (JSON.parse(rawWorkloadIds) as string[])
        : [];
    const logArchiveAccount = app.node.tryGetContext('agenticai/logArchiveAccountId');
    const auditAccount = app.node.tryGetContext('agenticai/auditAccountId');
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-west-2';

    if (typeof orgId !== 'string' || !orgId.startsWith('o-')) {
      throw new Error(
        "Platform stage requires context 'agenticai/organizationId' (e.g. 'o-xxxxxxxxxx').",
      );
    }

    if (logArchiveAccount) {
      new LogArchiveStack(app, 'AgenticAI-Platform-LogArchiveStack', {
        env: { account: logArchiveAccount, region },
        organizationId: orgId,
        workloadAccountIds,
      });
    }

    if (auditAccount) {
      new AuditStack(app, 'AgenticAI-Platform-AuditStack', {
        env: { account: auditAccount, region },
        organizationId: orgId,
      });
    }

    // Platform control-plane stacks deployed into platform-{nonprod,prod}.
    const platformAccount = app.node.tryGetContext('agenticai/platformAccountId');
    const pipelineRoleArn = app.node.tryGetContext('agenticai/pipelineRoleArn');
    const platformEnvName = app.node.tryGetContext('agenticai/envName') ?? 'nonprod';
    const inferenceModelRateLimits = inferenceModelRateLimitsContext(
      'agenticai/inferenceModelRateLimits',
    );
    const applicationId =
      app.node.tryGetContext('agenticai/applicationId') ?? 'platform-inference';
    const tenantId = app.node.tryGetContext('agenticai/tenantId') ?? 'shared';
    const agentId = app.node.tryGetContext('agenticai/agentId') ?? 'shared';
    const costCentre = app.node.tryGetContext('agenticai/costCentre') ?? 'platform';
    if (platformAccount && typeof pipelineRoleArn === 'string') {
      if (inferenceModelRateLimits.length === 0) {
        throw new Error(
          "Platform stage requires context 'agenticai/inferenceModelRateLimits'.",
        );
      }
      if (platformEnvName !== 'nonprod' && platformEnvName !== 'prod') {
        throw new Error("Platform stage context 'agenticai/envName' must be 'nonprod' or 'prod'.");
      }
      if (workloadAccountIds.length === 0) {
        throw new Error(
          "Platform stage requires non-empty context 'agenticai/workloadAccountIds' for the GA Registry reader trust.",
        );
      }
      new GuardrailStack(app, 'AgenticAI-Platform-GuardrailStack', {
        env: { account: platformAccount, region },
        pipelineRoleArn,
      });
      new RegistryStack(app, 'AgenticAI-Platform-RegistryStack', {
        env: { account: platformAccount, region },
        envName: platformEnvName,
        workloadAccountIds,
        applicationId: String(applicationId),
        agentId: String(agentId),
        tenantId: String(tenantId),
        costCentre: String(costCentre),
      });
      new InferenceGatewayStack(app, 'AgenticAI-Platform-InferenceGatewayStack', {
        env: { account: platformAccount, region },
        envName: String(platformEnvName),
        applicationId: String(applicationId),
        tenantId: String(tenantId),
        agentId: String(agentId),
        costCentre: String(costCentre),
        modelRateLimits: inferenceModelRateLimits,
      });
    }
    break;
  }
  case 'workload': {
    // Phase 4 workload-account stack: Agentic VPC + 9 VPCEs + Bedrock
    // Model Invocation Logging.
    const workloadAccount = app.node.tryGetContext('agenticai/workloadAccountId');
    const vpcCidr = app.node.tryGetContext('agenticai/vpcCidr');
    const availabilityZones = stringArrayContext('agenticai/availabilityZones');
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-west-2';

    const missing: string[] = [];
    if (!workloadAccount) missing.push('agenticai/workloadAccountId');
    if (availabilityZones.length < 2) missing.push('agenticai/availabilityZones');
    if (missing.length > 0) {
      throw new Error(
        `Workload stage requires context keys: ${missing.join(', ')}. Availability Zones must be preflight-derived for the target account.`,
      );
    }

    seedAvailabilityZoneContext(workloadAccount, region, availabilityZones);

    const networkStack = new WorkloadNetworkStack(app, 'AgenticAI-Workload-NetworkStack', {
      env: { account: workloadAccount, region },
      vpcCidr,
      availabilityZones,
    });

    // Phase 5 — WorkloadAppStack composes LiteLLM + AgentCore + RAG + AgenticApp.
    // Gated on an explicit context flag so a customer can split the deploys.
    const deployApp = app.node.tryGetContext('agenticai/deployWorkloadApp');
    if (deployApp === true || deployApp === 'true') {
      const tenantId = app.node.tryGetContext('agenticai/tenantId') ?? 'demo';
      const agentId = app.node.tryGetContext('agenticai/agentId') ?? 'primary';
      const costCentre = app.node.tryGetContext('agenticai/costCentre') ?? 'platform';
      const envName = app.node.tryGetContext('agenticai/envName') ?? 'nonprod';

      const auditOamSinkArn = app.node.tryGetContext('agenticai/auditOamSinkArn');
      const notificationEmail = app.node.tryGetContext('agenticai/notificationEmail');
      const monthlyBudgetUsd = app.node.tryGetContext('agenticai/monthlyBudgetUsd');
      const appStack = new WorkloadAppStack(app, 'AgenticAI-Workload-AppStack', {
        env: { account: workloadAccount, region },
        vpcId: networkStack.vpc.vpc.vpcId,
        workloadSubnetIds: networkStack.vpc.vpc
          .selectSubnets({ subnetGroupName: 'workload' })
          .subnetIds,
        workloadSubnetRouteTableIds: networkStack.vpc.vpc
          .selectSubnets({ subnetGroupName: 'workload' })
          .subnets.map((subnet) => subnet.routeTable.routeTableId),
        vpcCidr: networkStack.vpc.vpc.vpcCidrBlock,
        availabilityZones: networkStack.vpc.vpc.availabilityZones,
        bedrockRuntimeVpceId: networkStack.vpc.endpoints.bedrockRuntime.vpcEndpointId,
        vpceSecurityGroupId: networkStack.vpc.vpceEniSg.securityGroupId,
        envName,
        tenantId,
        agentId,
        costCentre,
        auditOamSinkArn: typeof auditOamSinkArn === 'string' ? auditOamSinkArn : undefined,
        notificationEmail: typeof notificationEmail === 'string' ? notificationEmail : undefined,
        monthlyBudgetUsd: typeof monthlyBudgetUsd === 'number' ? monthlyBudgetUsd : undefined,
      });
      appStack.addDependency(networkStack);
    }
    break;
  }
  case 'sandbox':
    // Phase 1 SCP sandbox stack lands here.
    break;
  case 'd03-platform': {
    // D-03 centralised-platform deployment (see README §3.3).
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-east-1';
    const account = process.env.CDK_DEFAULT_ACCOUNT;
    const rawIds = app.node.tryGetContext('agenticai/d03WorkloadAccountIds');
    const workloadAccountIds: readonly string[] = Array.isArray(rawIds)
      ? rawIds
      : typeof rawIds === 'string'
        ? (JSON.parse(rawIds) as string[])
        : [];
    const externalId = app.node.tryGetContext('agenticai/d03ExternalId');
    if (!workloadAccountIds.length || typeof externalId !== 'string') {
      throw new Error(
        "d03-platform stage requires context 'agenticai/d03WorkloadAccountIds' (array) and 'agenticai/d03ExternalId' (string).",
      );
    }
    // Per-tenant allocations drive the platform-owned application inference
    // profiles (D-03 CUR-attribution control). Accept either an array or a
    // JSON string (CI flows pass `-c agenticai/d03TenantAllocations='[...]'`).
    // Falls back to `undefined` — stack default emits a single demo/primary
    // allocation for the first workload account.
    const rawAllocations = app.node.tryGetContext('agenticai/d03TenantAllocations');
    const tenantAllocations = Array.isArray(rawAllocations)
      ? rawAllocations
      : typeof rawAllocations === 'string'
        ? (JSON.parse(rawAllocations) as unknown[])
        : undefined;
    // v0.5.0 — opt-in AgentCore Registry seed. Default off for back-compat
    // with the v0.4.0 D-03 v3 path. When `agenticai/d03EnableAgentRegistry`
    // is true, the platform stack provisions a single AgentCore Registry and
    // seeds it from PLATFORM_TOOL_CATALOGUE (one MCP record per lambda tool).
    const enableAgentRegistryRaw = app.node.tryGetContext('agenticai/d03EnableAgentRegistry');
    const enableAgentRegistry =
      enableAgentRegistryRaw === true || enableAgentRegistryRaw === 'true';
    const registryName =
      app.node.tryGetContext('agenticai/d03RegistryName') ?? 'agenticai-platform-registry';
    const registryAutoApproveRaw = app.node.tryGetContext('agenticai/d03RegistryAutoApproveOnSeed');
    const registryAutoApproveOnSeed =
      registryAutoApproveRaw === true || registryAutoApproveRaw === 'true';
    new D03PlatformCoreStack(app, 'AgenticAI-D03-PlatformCoreStack', {
      env: { account, region },
      workloadAccountIds,
      externalId,
      tenantAllocations: tenantAllocations as
        | import('../apps/platform-account/lib/d03-platform-core-stack').D03TenantAllocation[]
        | undefined,
      enableAgentRegistry,
      registryName: typeof registryName === 'string' ? registryName : undefined,
      registryAutoApproveOnSeed,
    });
    // Note: the D-03 PrivateLink primitive (PlatformInferenceGatewayConstruct
    // in packages/platform-inference-gateway) is consumed by a platform
    // inference-stack that will be added when LiteLLM is stood up in the
    // platform account. Example wiring:
    //
    //   import { PlatformInferenceGatewayConstruct } from '@agenticai/platform-inference-gateway';
    //   const gw = new PlatformInferenceGatewayConstruct(stack, 'InferenceGw', {
    //     vpc: platformVpc,
    //     workloadAccountIds,
    //     targetAlb: litellm.alb,     // once litellm is deployed
    //   });
    //   // Propagate `gw.endpointServiceName` to each workload stack via SSM/context
    //   // and set `agenticai/d03PlatformInferenceServiceName` on the d03-workload stage.
    break;
  }
  case 'd03-workload': {
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-east-1';
    const account = process.env.CDK_DEFAULT_ACCOUNT;
    const platformAccountId = app.node.tryGetContext('agenticai/d03PlatformAccountId');
    const externalId = app.node.tryGetContext('agenticai/d03ExternalId');
    const tenantId = app.node.tryGetContext('agenticai/tenantId') ?? 'demo';
    const agentId = app.node.tryGetContext('agenticai/agentId') ?? 'primary';
    const vpcCidr = app.node.tryGetContext('agenticai/vpcCidr');
    const platformInferenceServiceName = app.node.tryGetContext(
      'agenticai/d03PlatformInferenceServiceName',
    );
    const envName = app.node.tryGetContext('agenticai/envName') ?? 'nonprod';
    // allowLocalRootAssume: OPT-IN ONLY, for D-03 integration tests run from
    // the workload IAM user (the Strands agent path uses the AgentCore service
    // principal and does NOT need this). Hard-denied when envName === 'prod'.
    const allowLocalRootAssumeRaw = app.node.tryGetContext('agenticai/d03AllowLocalRootAssume');
    const allowLocalRootAssume =
      allowLocalRootAssumeRaw === true || allowLocalRootAssumeRaw === 'true';
    // retainDataKeys: default true (RETAIN + 30-day pending window on all CMKs
    // per the production posture). Flip to false for ephemeral dev/test loops.
    const retainDataKeysRaw = app.node.tryGetContext('agenticai/d03RetainDataKeys');
    const retainDataKeys = !(retainDataKeysRaw === false || retainDataKeysRaw === 'false');
    if (!platformAccountId || typeof externalId !== 'string') {
      throw new Error(
        "d03-workload stage requires context 'agenticai/d03PlatformAccountId' and 'agenticai/d03ExternalId'.",
      );
    }
    new D03WorkloadAgentStack(app, 'AgenticAI-D03-WorkloadAgentStack', {
      env: { account, region },
      platformAccountId,
      externalId,
      tenantId,
      agentId,
      vpcCidr,
      envName,
      allowLocalRootAssume,
      retainDataKeys,
      platformInferenceServiceName:
        typeof platformInferenceServiceName === 'string'
          ? platformInferenceServiceName
          : undefined,
    });
    break;
  }
  case 'd03-workstream-gateway': {
    // D-03 v3 per-workstream AgentCore Gateway + Targets. Runs AFTER
    // `d03-platform` (catalogue SSOT) and `d03-workload` (runtime role) —
    // the workload account must already carry the runtime role the Gateway
    // resource policy references. Deployed INTO the workload account via
    // the platform pipeline's cross-account CDK deploy role.
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-east-1';
    const account = process.env.CDK_DEFAULT_ACCOUNT; // must be the workload account at deploy time
    const tenantId = app.node.tryGetContext('agenticai/tenantId');
    const agentId = app.node.tryGetContext('agenticai/agentId');
    const envName = app.node.tryGetContext('agenticai/envName') ?? 'nonprod';
    const platformAccountId = app.node.tryGetContext('agenticai/d03PlatformAccountId');
    const workloadAccountId =
      account ?? app.node.tryGetContext('agenticai/d03WorkloadAccountId');
    const rawAllowed = app.node.tryGetContext('agenticai/d03AllowedToolIds');
    const allowedToolIds: string[] = Array.isArray(rawAllowed)
      ? rawAllowed
      : typeof rawAllowed === 'string'
        ? (JSON.parse(rawAllowed) as string[])
        : [];
    // v0.5.0 — Registry-based subscription path. When set, the gateway stack
    // resolves each subscribed record at deploy time via GetRegistryRecord
    // against the platform AgentCore Registry. Mutually exclusive with the
    // legacy `agenticai/d03AllowedToolIds`.
    const rawSubscribed = app.node.tryGetContext('agenticai/subscribedRegistryRecords');
    const subscribedRegistryRecords: string[] = Array.isArray(rawSubscribed)
      ? rawSubscribed
      : typeof rawSubscribed === 'string'
        ? (JSON.parse(rawSubscribed) as string[])
        : [];
    const registryId = app.node.tryGetContext('agenticai/d03RegistryId');
    const registryReaderRoleArn = app.node.tryGetContext(
      'agenticai/d03RegistryReaderRoleArn',
    );
    const registryReaderExternalId = app.node.tryGetContext(
      'agenticai/d03RegistryReaderExternalId',
    );
    const cognitoDiscoveryUrl = app.node.tryGetContext('agenticai/cognitoDiscoveryUrl');
    const cognitoAudience = app.node.tryGetContext('agenticai/cognitoAudience');

    const usingRegistryPath = subscribedRegistryRecords.length > 0;
    const missing: string[] = [];
    if (!tenantId) missing.push('agenticai/tenantId');
    if (!agentId) missing.push('agenticai/agentId');
    if (!platformAccountId) missing.push('agenticai/d03PlatformAccountId');
    if (!workloadAccountId) missing.push('agenticai/d03WorkloadAccountId');
    if (!usingRegistryPath && !allowedToolIds.length) {
      missing.push(
        'agenticai/d03AllowedToolIds OR agenticai/subscribedRegistryRecords (one required)',
      );
    }
    if (usingRegistryPath && (typeof registryId !== 'string' || registryId.length === 0)) {
      missing.push('agenticai/d03RegistryId (required with subscribedRegistryRecords)');
    }
    if (missing.length) {
      throw new Error(
        `d03-workstream-gateway stage requires: ${missing.join(', ')}`,
      );
    }

    new D03WorkstreamGatewayStack(
      app,
      `AgenticAI-D03-WorkstreamGateway-${tenantId}-${agentId}`,
      {
        env: { account: workloadAccountId, region },
        tenantId,
        agentId,
        envName,
        workloadAccountId,
        platformAccountId,
        allowedToolIds: usingRegistryPath ? undefined : allowedToolIds,
        subscribedRegistryRecords: usingRegistryPath
          ? subscribedRegistryRecords
          : undefined,
        registryId: usingRegistryPath ? (registryId as string) : undefined,
        registryReaderRoleArn:
          typeof registryReaderRoleArn === 'string' && registryReaderRoleArn.length > 0
            ? registryReaderRoleArn
            : undefined,
        registryReaderExternalId:
          typeof registryReaderExternalId === 'string' &&
          registryReaderExternalId.length > 0
            ? registryReaderExternalId
            : undefined,
        cognitoDiscoveryUrl:
          typeof cognitoDiscoveryUrl === 'string' ? cognitoDiscoveryUrl : undefined,
        cognitoAudience: Array.isArray(cognitoAudience)
          ? cognitoAudience
          : typeof cognitoAudience === 'string'
            ? (JSON.parse(cognitoAudience) as string[])
            : undefined,
      },
    );
    break;
  }
  case 'pipeline': {
    // Phase 7 — CDK Pipelines stacks.
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-west-2';
    const githubRepo = app.node.tryGetContext('agenticai/githubRepo');
    const githubBranch = app.node.tryGetContext('agenticai/githubBranch');
    const githubConnectionArn = app.node.tryGetContext('agenticai/githubConnectionArn');
    const organizationId = app.node.tryGetContext('agenticai/organizationId');
    const platformNonprodAccount = app.node.tryGetContext('agenticai/platformNonprodAccountId');
    const platformProdAccount = app.node.tryGetContext('agenticai/platformProdAccountId');
    const auditAccount = app.node.tryGetContext('agenticai/auditAccountId');
    const logArchiveAccount = app.node.tryGetContext('agenticai/logArchiveAccountId');
    const workloadNonprodAccount = app.node.tryGetContext('agenticai/workloadNonprodAccountId');
    const workloadProdAccount = app.node.tryGetContext('agenticai/workloadProdAccountId');
    const pipelineRoleArn = app.node.tryGetContext('agenticai/pipelineRoleArn');
    const configuredWorkloadAccountIds = stringArrayContext('agenticai/workloadAccountIds');
    const workloadNonprodAvailabilityZones = stringArrayContext(
      'agenticai/workloadNonprodAvailabilityZones',
    );
    const workloadProdAvailabilityZones = stringArrayContext(
      'agenticai/workloadProdAvailabilityZones',
    );
    const tenantId = app.node.tryGetContext('agenticai/tenantId') ?? 'demo';
    const agentId = app.node.tryGetContext('agenticai/agentId') ?? 'primary';
    const applicationId = app.node.tryGetContext('agenticai/applicationId') ?? tenantId;
    const costCentre = app.node.tryGetContext('agenticai/costCentre') ?? 'engineering';
    const inferenceModelRateLimits = inferenceModelRateLimitsContext(
      'agenticai/inferenceModelRateLimits',
    );
    const auditOamSinkArn = app.node.tryGetContext('agenticai/auditOamSinkArn');
    const notificationEmail = app.node.tryGetContext('agenticai/notificationEmail');

    const missing: string[] = [];
    if (typeof githubRepo !== 'string') missing.push('agenticai/githubRepo');
    if (typeof githubConnectionArn !== 'string') missing.push('agenticai/githubConnectionArn');
    if (typeof organizationId !== 'string') missing.push('agenticai/organizationId');
    if (!platformNonprodAccount) missing.push('agenticai/platformNonprodAccountId');
    if (!platformProdAccount) missing.push('agenticai/platformProdAccountId');
    if (!auditAccount) missing.push('agenticai/auditAccountId');
    if (!logArchiveAccount) missing.push('agenticai/logArchiveAccountId');
    if (!workloadNonprodAccount) missing.push('agenticai/workloadNonprodAccountId');
    if (!workloadProdAccount) missing.push('agenticai/workloadProdAccountId');
    if (workloadNonprodAvailabilityZones.length < 2) {
      missing.push('agenticai/workloadNonprodAvailabilityZones');
    }
    if (workloadProdAvailabilityZones.length < 2) {
      missing.push('agenticai/workloadProdAvailabilityZones');
    }
    if (typeof pipelineRoleArn !== 'string') missing.push('agenticai/pipelineRoleArn');
    if (inferenceModelRateLimits.length === 0) {
      missing.push('agenticai/inferenceModelRateLimits');
    }
    if (missing.length > 0) {
      throw new Error(
        `Pipeline stage requires context keys: ${missing.join(', ')}. Populate cdk.context.json or pass via -c.`,
      );
    }

    seedAvailabilityZoneContext(
      workloadNonprodAccount,
      region,
      workloadNonprodAvailabilityZones,
    );
    seedAvailabilityZoneContext(
      workloadProdAccount,
      region,
      workloadProdAvailabilityZones,
    );

    const workloadAccountIds = configuredWorkloadAccountIds.length > 0
      ? configuredWorkloadAccountIds
      : [...new Set([String(workloadNonprodAccount), String(workloadProdAccount)])];
    const sharedSynthContext: Record<string, string> = {
      'agenticai/githubRepo': githubRepo as string,
      'agenticai/githubConnectionArn': githubConnectionArn as string,
      'agenticai/organizationId': organizationId as string,
      'agenticai/platformNonprodAccountId': String(platformNonprodAccount),
      'agenticai/platformProdAccountId': String(platformProdAccount),
      'agenticai/auditAccountId': String(auditAccount),
      'agenticai/logArchiveAccountId': String(logArchiveAccount),
      'agenticai/workloadNonprodAccountId': String(workloadNonprodAccount),
      'agenticai/workloadProdAccountId': String(workloadProdAccount),
      'agenticai/workloadNonprodAvailabilityZones': JSON.stringify(
        workloadNonprodAvailabilityZones,
      ),
      'agenticai/workloadProdAvailabilityZones': JSON.stringify(
        workloadProdAvailabilityZones,
      ),
      'agenticai/pipelineRoleArn': pipelineRoleArn as string,
      'agenticai/workloadAccountIds': JSON.stringify(workloadAccountIds),
      'agenticai/tenantId': String(tenantId),
      'agenticai/agentId': String(agentId),
      'agenticai/applicationId': String(applicationId),
      'agenticai/costCentre': String(costCentre),
      'agenticai/inferenceModelRateLimits': JSON.stringify(inferenceModelRateLimits),
    };
    if (typeof githubBranch === 'string') {
      sharedSynthContext['agenticai/githubBranch'] = githubBranch;
    }
    if (typeof auditOamSinkArn === 'string') {
      sharedSynthContext['agenticai/auditOamSinkArn'] = auditOamSinkArn;
    }
    if (typeof notificationEmail === 'string') {
      sharedSynthContext['agenticai/notificationEmail'] = notificationEmail;
    }

    new PlatformPipelineStack(app, 'AgenticAI-PlatformPipelineStack', {
      env: { account: platformNonprodAccount, region },
      githubRepo: githubRepo as string,
      githubBranch: typeof githubBranch === 'string' ? githubBranch : undefined,
      githubConnectionArn: githubConnectionArn as string,
      organizationId: organizationId as string,
      logArchive: {
        env: { account: logArchiveAccount, region },
        envName: 'nonprod',
      },
      audit: {
        env: { account: auditAccount, region },
        envName: 'nonprod',
      },
      platformNonprod: {
        env: { account: platformNonprodAccount, region },
        envName: 'nonprod',
      },
      platformProd: {
        env: { account: platformProdAccount, region },
        envName: 'prod',
      },
      workloadAccountIds,
      pipelineRoleArn: pipelineRoleArn as string,
      applicationId: String(applicationId),
      tenantId: String(tenantId),
      agentId: String(agentId),
      costCentre: String(costCentre),
      inferenceModelRateLimits,
      synthContext: sharedSynthContext,
    });

    new WorkloadPipelineStack(app, 'AgenticAI-WorkloadPipelineStack', {
      env: { account: platformNonprodAccount, region },
      githubRepo: githubRepo as string,
      githubBranch: typeof githubBranch === 'string' ? githubBranch : undefined,
      githubConnectionArn: githubConnectionArn as string,
      tenantId,
      agentId,
      costCentre,
      workloadNonprodEnv: { account: workloadNonprodAccount, region },
      workloadProdEnv: { account: workloadProdAccount, region },
      workloadNonprodAvailabilityZones,
      workloadProdAvailabilityZones,
      auditOamSinkArn: typeof auditOamSinkArn === 'string' ? auditOamSinkArn : undefined,
      notificationEmail: typeof notificationEmail === 'string' ? notificationEmail : undefined,
      synthContext: sharedSynthContext,
    });
    break;
  }
  case 'gap-closure': {
    const region = process.env.CDK_DEFAULT_REGION ?? 'us-east-1';
    const account = process.env.CDK_DEFAULT_ACCOUNT;
    const tenantId = app.node.tryGetContext('agenticai/tenantId') ?? 'demo';
    const agentId = app.node.tryGetContext('agenticai/agentId') ?? 'primary';
    const envName = app.node.tryGetContext('agenticai/envName') ?? 'nonprod';
    const blueprintId = app.node.tryGetContext('agenticai/blueprintId') ?? 'multi-agent';
    const providerName = app.node.tryGetContext('agenticai/providerName') ?? 'AWS Solutions';
    const contactEmail = app.node.tryGetContext('agenticai/contactEmail') ?? 'compliance@example.com';
    const humanOversightContact = app.node.tryGetContext('agenticai/humanOversightContact') ?? 'oversight@example.com';
    const approverRoleArn = app.node.tryGetContext('agenticai/approverRoleArn');
    const chargebackEmail = app.node.tryGetContext('agenticai/chargebackEmail') ?? 'finops@example.com';
    const mcpGatewayUrl = app.node.tryGetContext('agenticai/mcpGatewayUrl') ?? 'https://gateway.example.com/a2a';
    const cognitoUserPoolId = app.node.tryGetContext('agenticai/cognitoUserPoolId') ?? 'us-east-1_AAAAAAAAA';
    const cognitoUserPoolClientId = app.node.tryGetContext('agenticai/cognitoUserPoolClientId') ?? 'placeholderClientId';
    const workloadIdentityName = app.node.tryGetContext('agenticai/workloadIdentityName') ?? `${tenantId}-${agentId}-wi`;
    const gatewayTargetId = app.node.tryGetContext('agenticai/gatewayTargetId') ?? 'placeholdr1';
    const inferenceProfileArn =
      app.node.tryGetContext('agenticai/inferenceProfileArn') ??
      `arn:aws:bedrock:${region}:${account ?? '111111111111'}:application-inference-profile/${tenantId}-${agentId}`;
    if (typeof approverRoleArn !== 'string' || !approverRoleArn.startsWith('arn:aws:iam::')) {
      throw new Error(
        "gap-closure stage requires context 'agenticai/approverRoleArn' to be a valid IAM role ARN.",
      );
    }
    new GapClosureStack(app, 'AgenticAI-GapClosureStack', {
      env: { account, region },
      envName,
      tenantId,
      agentId,
      blueprintId,
      providerName,
      contactEmail,
      humanOversightContact,
      approverRoleArn,
      chargebackEmail,
      mcpGatewayUrl,
      cognitoUserPoolId,
      cognitoUserPoolClientId,
      workloadIdentityName,
      gatewayTargetId,
      inferenceProfileArn,
    });
    break;
  }
  case undefined:
    throw new Error(
      "Missing required CDK context 'stage'. Pass --context stage=<management|platform|workload|sandbox|d03-platform|d03-workload|d03-workstream-gateway|pipeline|gap-closure>.",
    );
  default:
    throw new Error(
      `Unknown stage '${stage}'. Valid stages: management | platform | workload | sandbox | d03-platform | d03-workload | d03-workstream-gateway | pipeline | gap-closure.`,
    );
}

Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
if (regulated) {
  Aspects.of(app).add(new NIST80053R5Checks({ verbose: true }));
}

app.synth();
