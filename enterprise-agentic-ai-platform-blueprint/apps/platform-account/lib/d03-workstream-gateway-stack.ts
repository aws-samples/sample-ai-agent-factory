/**
 * D03WorkstreamGatewayStack — per-workstream AgentCore Gateway + Targets.
 *
 * Deployed INTO the workload account (the workstream's own account) by the
 * platform pipeline via cross-account CDK deploy role. One stack per
 * `(tenantId, agentId)` allocation — each allocation gets a dedicated
 * Gateway + N Targets where N = `allowedToolIds.length`.
 *
 * D-03 v3 three-layer tool-governance model, runtime enforcement layer 3:
 *   - Layer 1 (synth-time): `resolveSubscribedTools()` throws if any id is
 *     not in the PLATFORM_TOOL_CATALOGUE SSOT or is marked deprecated.
 *   - Layer 2 (SCP-10, org-level): denies `lambda:InvokeFunction` on any
 *     ARN that is not an approved tool alias.
 *   - Layer 3 (this stack): GatewayServiceRole IAM policy lists exactly the
 *     resolved tool target ARNs. No wildcards; no extras. The Gateway
 *     physically cannot invoke anything outside the subscribed set.
 *
 * AgentCore API shape (CreateGateway + CreateGatewayTarget) is SDK-only as of
 * the cut of this blueprint — no CloudFormation L1 exists — so we wrap the
 * calls in `AwsCustomResource`, matching the pattern used by
 * `D03PlatformCoreStack.ConfigureInvocationLogging`.
 *
 * Policy engine: the live API requires `policyEngineConfiguration.arn` to be
 * a pre-existing `policy-engine/*` ARN (Cedar policies are attached to a
 * policy-engine resource, not inlined on the Gateway). Creating the
 * policy-engine is a separate AgentCore API that is still stabilising; until
 * the PolicyEngine CR lands we emit the composed Cedar document as a
 * `PerTenantCedarPolicy` CfnOutput for audit. This is TODO-GW-POLICY-ENGINE
 * — tracked in README §3 — and is the documented deviation for Phase 10.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  CfnOutput,
  CustomResource,
  Duration,
  Stack,
  StackProps,
  Tags,
} from "aws-cdk-lib";
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from "aws-cdk-lib/aws-iam";
import {
  Code,
  Function as LambdaFunction,
  Runtime,
} from "aws-cdk-lib/aws-lambda";
import { RetentionDays } from "aws-cdk-lib/aws-logs";
import {
  AwsCustomResource,
  PhysicalResourceId,
  PhysicalResourceIdReference,
  Provider,
} from "aws-cdk-lib/custom-resources";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

import type { GaRegistryConsumerContext } from "@agenticai/agent-registry";
import {
  composeCedarPolicyDocument,
  resolveSubscribedTools,
  resolveTargetArn,
  validateToolSpec,
  ToolSpec,
} from "@agenticai/platform-tool-catalogue";

export interface D03WorkstreamGatewayStackProps extends StackProps {
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
  readonly workloadAccountId: string;
  readonly platformAccountId: string;
  /**
   * Legacy v0.4.0 path: kebab-case tool ids resolved against the in-process
   * `PLATFORM_TOOL_CATALOGUE`. Mutually exclusive with
   * `gaRegistryContext` — pass exactly one.
   */
  readonly allowedToolIds?: readonly string[];
  /**
   * R2 GA path: complete, pipeline-resolved and template-bound Registry
   * context. Mutually exclusive with `allowedToolIds`.
   *
   * The Platform-side Workload synth reads the versioned SSM pointers and
   * APPROVED records, validates the complete governance documents, and pins
   * the resulting target/schema/Cedar data here. This stack re-fetches every
   * record through RegistryReaderRole at deploy time and compares its digest,
   * closing the synth-to-deploy drift window.
   */
  readonly gaRegistryContext?: GaRegistryConsumerContext;
  /** Application allocation tag; defaults to tenantId for legacy callers. */
  readonly applicationId?: string;
  /** Cost allocation tag; required for the pipeline-owned R2 path. */
  readonly costCentre?: string;
  /** Cognito User Pool discoveryUrl for CUSTOM_JWT authorizer. Optional — falls back to AWS_IAM. */
  readonly cognitoDiscoveryUrl?: string;
  readonly cognitoAudience?: readonly string[];
  /**
   * Optional — import an existing IAM role instead of creating a new one.
   * Use when the role was pre-created out-of-band so that its RoleId is
   * stable across stack rollbacks. Platform tool Lambdas' resource policies
   * capture the RoleId server-side; re-creating the role breaks those
   * policies. When set, the stack skips role creation and imports the ARN.
   */
  readonly gatewayServiceRoleArnOverride?: string;
  /** Import the stable pipeline-created Registry validator role. */
  readonly registryValidatorRoleArnOverride?: string;
  /**
   * Optional — import an existing IAM role for the AgentCore provisioning
   * custom resources instead of creating one inline. Because a freshly-created
   * IAM role's authorization takes minutes to propagate to the AgentCore
   * control plane (live-verified: `CreateGateway` denied for >4 min after
   * role creation), a pre-created + pre-propagated role removes the
   * deploy-time race entirely. The role must trust `lambda.amazonaws.com` and
   * carry `bedrock-agentcore:*` + `iam:PassRole` on the gateway service role +
   * `AWSLambdaBasicExecutionRole`. When set, the inline CR role and the
   * IAM-propagation gate are skipped.
   */
  readonly crExecRoleArnOverride?: string;
}

export class D03WorkstreamGatewayStack extends Stack {
  /** Resolved ToolSpec subset — exposed for test assertion convenience. */
  readonly subscribedTools: readonly ToolSpec[];
  /** Service role the AgentCore Gateway assumes to invoke tool Lambdas. */
  readonly gatewayServiceRole: Role;
  /** AwsCustomResource for CreateGateway — physical id stable across deploys. */
  readonly gatewayResource: AwsCustomResource;

  constructor(
    scope: Construct,
    id: string,
    props: D03WorkstreamGatewayStackProps,
  ) {
    super(scope, id, props);

    // ---- Mode selection: legacy catalogue rollback vs strict GA Registry ----
    const registryContext = props.gaRegistryContext;
    const usingRegistry = registryContext !== undefined;
    if (
      usingRegistry &&
      Array.isArray(props.allowedToolIds) &&
      props.allowedToolIds.length > 0
    ) {
      throw new Error(
        "D03WorkstreamGatewayStack: 'allowedToolIds' (legacy) and 'gaRegistryContext' are mutually exclusive — pass exactly one.",
      );
    }
    if (
      !usingRegistry &&
      (!props.allowedToolIds || props.allowedToolIds.length === 0)
    ) {
      throw new Error(
        "D03WorkstreamGatewayStack: must supply either 'allowedToolIds' (legacy rollback) or 'gaRegistryContext' (R2 GA).",
      );
    }
    if (usingRegistry) {
      if (registryContext.environment !== props.envName) {
        throw new Error(
          "D03WorkstreamGatewayStack: GA Registry environment does not match envName.",
        );
      }
      if (registryContext.platformAccountId !== props.platformAccountId) {
        throw new Error(
          "D03WorkstreamGatewayStack: GA Registry platform account does not match.",
        );
      }
      if (registryContext.records.length === 0) {
        throw new Error(
          "D03WorkstreamGatewayStack: GA Registry context has no records.",
        );
      }
      if (!props.costCentre) {
        throw new Error(
          "D03WorkstreamGatewayStack: costCentre is required in R2 GA mode.",
        );
      }
    }

    let subset: readonly ToolSpec[] = [];
    const resolvedToolArns: Record<string, string> = {};
    let cedarPolicy: string;
    /** When using the Registry, each entry is a deploy-time `GetRegistryRecord` validator custom resource. */
    const registryFetchers: Record<string, CustomResource> = {};

    if (usingRegistry) {
      // ---- R2 GA Registry path ----
      // The Platform-side pipeline synth already resolved and validated the
      // complete governance documents. Use those immutable values to build
      // exact target schemas/IAM/Cedar, then re-fetch each record at deploy
      // time and compare its status + descriptor digest to close the TOCTOU
      // window between synth and CloudFormation execution.
      const context = registryContext;
      const validatorRoleName = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-RegistryValidator`;
      if (validatorRoleName.length > 64) {
        throw new Error(
          "D03WorkstreamGatewayStack: generated RegistryValidator role name exceeds 64 characters.",
        );
      }
      let validatorRole: Role;
      if (props.registryValidatorRoleArnOverride) {
        validatorRole = Role.fromRoleArn(
          this,
          "RegistryValidatorRole",
          props.registryValidatorRoleArnOverride,
          { mutable: false },
        ) as Role;
      } else {
        validatorRole = new Role(this, "RegistryValidatorRole", {
          roleName: validatorRoleName,
          assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
          description:
            "Pipeline-owned Workstream role that revalidates GA Registry subscriptions at deploy time.",
          inlinePolicies: {
            AssumeRegistryReader: new PolicyDocument({
              statements: [
                new PolicyStatement({
                  effect: Effect.ALLOW,
                  actions: ["sts:AssumeRole"],
                  resources: [context.readerRoleArn],
                }),
              ],
            }),
          },
          managedPolicies: [
            ManagedPolicy.fromAwsManagedPolicyName(
              "service-role/AWSLambdaBasicExecutionRole",
            ),
          ],
        });
        NagSuppressions.addResourceSuppressions(
          validatorRole,
          [
            {
              id: "AwsSolutions-IAM4",
              appliesTo: [
                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
              ],
              reason:
                "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for the pipeline-owned Registry validator Lambda.",
            },
          ],
          true,
        );
      }

      const validatorEnv: Record<string, string> = {
        REGISTRY_READER_ROLE_ARN: context.readerRoleArn,
        REGISTRY_READER_EXTERNAL_ID: context.readerExternalId,
        REGISTRY_READER_SESSION_NAME: `registry-${props.envName}-validator`,
        GATEWAY_AUTHORIZER_MODE:
          typeof props.cognitoDiscoveryUrl === "string" &&
          props.cognitoDiscoveryUrl.length > 0
            ? "CUSTOM_JWT"
            : "AWS_IAM",
      };
      const validatorFn = new LambdaFunction(
        this,
        "RegistryRecordValidatorFn",
        {
          functionName:
            `agenticai-d03-${props.envName}-${props.tenantId}-${props.agentId}-reg-validator`.slice(
              0,
              64,
            ),
          runtime: Runtime.NODEJS_20_X,
          handler: "index.handler",
          timeout: Duration.minutes(1),
          memorySize: 256,
          logRetention: RetentionDays.ONE_MONTH,
          description:
            "Revalidates APPROVED GA Registry records and descriptor digests at deploy time.",
          code: Code.fromInline(REGISTRY_RECORD_VALIDATOR_HANDLER),
          environment: validatorEnv,
          role: validatorRole,
        },
      );
      const validatorProvider = new Provider(
        this,
        "RegistryRecordValidatorProvider",
        {
          onEventHandler: validatorFn,
          logRetention: RetentionDays.ONE_MONTH,
        },
      );
      NagSuppressions.addResourceSuppressions(
        validatorProvider,
        [
          {
            id: "AwsSolutions-IAM5",
            reason:
              "SEC-029: CDK Provider framework invokes versions/aliases of the single validator Lambda created in this stack.",
          },
          {
            id: "AwsSolutions-IAM4",
            reason:
              "SEC-010: AWSLambdaBasicExecutionRole is the documented logging policy for CDK provider framework Lambdas.",
          },
          {
            id: "AwsSolutions-L1",
            reason:
              "SEC-006: Provider framework Lambda runtime is managed by aws-cdk-lib.",
          },
          {
            id: "NIST.800.53.R5-LambdaConcurrency",
            reason: "SEC-007: Provisioning-time only.",
          },
          {
            id: "NIST.800.53.R5-LambdaDLQ",
            reason: "SEC-008: CloudFormation surfaces failures.",
          },
          {
            id: "NIST.800.53.R5-LambdaInsideVPC",
            reason: "SEC-009: Control-plane only.",
          },
        ],
        true,
      );

      const registrySpecs: ToolSpec[] = [];
      for (const resolved of context.records) {
        const document = resolved.document;
        const spec: ToolSpec = {
          toolId: document.toolId,
          toolType: document.target.type,
          targetArn: document.target.arn,
          cedarPolicy: document.authorization.cedarPolicy,
          ownerTeam: document.ownership.ownerTeam,
          costCentre: document.ownership.costCentre,
          description: document.description,
          approvalStatus: "approved",
          inputSchema: document.mcp.inputSchema,
          allowedGroups:
            document.authorization.allowedGroups.length > 0
              ? document.authorization.allowedGroups
              : undefined,
        };
        validateToolSpec(spec);
        registrySpecs.push(spec);
        resolvedToolArns[spec.toolId] = spec.targetArn;
        registryFetchers[spec.toolId] = new CustomResource(
          this,
          `RegistryValidate-${spec.toolId}`,
          {
            resourceType: "Custom::AgenticAIRegistryRecordValidator",
            serviceToken: validatorProvider.serviceToken,
            properties: {
              registryId: context.registryId,
              recordId: resolved.recordId,
              expectedToolId: spec.toolId,
              expectedTargetArn: spec.targetArn,
              expectedDescriptorSha256: resolved.descriptorSha256,
              validationRevision: context.sourceRevision,
              tenantId: props.tenantId,
              agentId: props.agentId,
            },
          },
        );
      }
      subset = registrySpecs;
      const usingJwt =
        typeof props.cognitoDiscoveryUrl === "string" &&
        props.cognitoDiscoveryUrl.length > 0;
      const entitledTools = subset.filter(
        (spec) =>
          Array.isArray(spec.allowedGroups) && spec.allowedGroups.length > 0,
      );
      if (entitledTools.length > 0 && !usingJwt) {
        throw new Error(
          `D03WorkstreamGatewayStack: GA record tool(s) [${entitledTools
            .map((spec) => spec.toolId)
            .join(
              ", ",
            )}] require CUSTOM_JWT because allowedGroups is non-empty.`,
        );
      }
      cedarPolicy = composeCedarPolicyDocument(subset);
      NagSuppressions.addResourceSuppressions(
        validatorFn,
        [
          {
            id: "AwsSolutions-L1",
            reason:
              "SEC-006: NodeJS 20 is the latest CDK-supported runtime for the inline validator.",
          },
          {
            id: "NIST.800.53.R5-LambdaConcurrency",
            reason:
              "SEC-007: Provisioning-time Lambda invoked only by CloudFormation.",
          },
          {
            id: "NIST.800.53.R5-LambdaDLQ",
            reason:
              "SEC-008: CloudFormation surfaces custom-resource failures.",
          },
          {
            id: "NIST.800.53.R5-LambdaInsideVPC",
            reason:
              "SEC-009: The GA Agent Registry control plane is reached only during deployment.",
          },
        ],
        true,
      );
    } else {
      // ---- Legacy v0.4.0 catalogue path (unchanged) ----
      // resolveSubscribedTools throws on unknown ids or deprecated subscriptions.
      subset = resolveSubscribedTools(props.allowedToolIds!);
      // Resolve every ToolSpec to a concrete tool Lambda ARN.
      // `${PLATFORM_ACCOUNT_ID}` is substituted with props.platformAccountId
      // unless the tool explicitly declares a cross-account targetAccountId.
      for (const spec of subset) {
        resolvedToolArns[spec.toolId] = resolveTargetArn(
          spec,
          props.platformAccountId,
        );
      }
      // Phase Q (v0.6.0): when any subscribed tool declares allowedGroups, the
      // workstream Gateway MUST be configured for CUSTOM_JWT — Cedar group
      // binding has nothing to evaluate against without JWT claims. Fail the
      // synth with an actionable error rather than silently degrading to
      // "any authenticated principal" semantics.
      const usingJwt =
        typeof props.cognitoDiscoveryUrl === "string" &&
        props.cognitoDiscoveryUrl.length > 0;
      const entitledTools = subset.filter(
        (s) => Array.isArray(s.allowedGroups) && s.allowedGroups.length > 0,
      );
      if (entitledTools.length > 0 && !usingJwt) {
        throw new Error(
          `D03WorkstreamGatewayStack: tool(s) [${entitledTools
            .map((s) => s.toolId)
            .join(
              ", ",
            )}] declare allowedGroups (per-developer entitlement) but no cognitoDiscoveryUrl was supplied. ` +
            `Phase Q requires CUSTOM_JWT auth so the Cedar evaluator can read the principal's cognito:groups claim. ` +
            `Either set cognitoDiscoveryUrl on D03WorkstreamGatewayStackProps or remove allowedGroups from the affected tool(s).`,
        );
      }
      cedarPolicy = composeCedarPolicyDocument(subset);
    }
    this.subscribedTools = subset;
    const subscribedIds: readonly string[] = subset.map((spec) => spec.toolId);
    const targetArns = Object.values(resolvedToolArns);

    // ---- GatewayServiceRole (D-03 v3, layer 3 enforcement) ----
    // Trusted by bedrock-agentcore.amazonaws.com — the AgentCore Gateway
    // service principal. The inline policy lists the EXACT N resolved tool
    // ARNs. No wildcards; no lambda:* — just `lambda:InvokeFunction` on the
    // approved set. SCP-10 at org level backs this up one layer deeper.
    //
    // Role-id stability note (discovered live 2026-05-05): if this role is
    // re-created by the stack on a rollback+redeploy, its underlying IAM
    // `RoleId` changes even though the ARN string is stable. Any Lambda
    // resource policy that captured the OLD RoleId server-side will then
    // fail to authorise. To keep role-id stable across deploys, the role
    // may be pre-created out-of-band and imported via the
    // `agenticai/d03GatewayRoleArnOverride` context flag; in that mode the
    // stack imports the existing role rather than creating a new one.
    const gwRoleNameDefault = `AgenticAI-D03-${props.envName}-${props.tenantId}-${props.agentId}-gw-svc`;
    if (gwRoleNameDefault.length > 64) {
      throw new Error(
        "D03WorkstreamGatewayStack: generated Gateway service role name exceeds 64 characters.",
      );
    }
    const gwRoleArnOverride =
      (this.node.tryGetContext("agenticai/d03GatewayRoleArnOverride") as
        | string
        | undefined) ?? props.gatewayServiceRoleArnOverride;

    if (typeof gwRoleArnOverride === "string" && gwRoleArnOverride.length > 0) {
      this.gatewayServiceRole = Role.fromRoleArn(
        this,
        "GatewayServiceRole",
        gwRoleArnOverride,
        { mutable: false },
      ) as Role;
    } else {
      this.gatewayServiceRole = new Role(this, "GatewayServiceRole", {
        roleName: gwRoleNameDefault,
        assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        description: `D-03 v3: service role assumed by AgentCore Gateway for tenant=${props.tenantId} agent=${props.agentId}. Scoped to the exact N subscribed tool ARNs.`,
        inlinePolicies: {
          InvokeSubscribedTools: new PolicyDocument({
            statements: [
              new PolicyStatement({
                sid: "InvokeSubscribedTools",
                effect: Effect.ALLOW,
                actions: ["lambda:InvokeFunction"],
                // EXACT set — no wildcards. Layer-3 of the three-layer model.
                resources: targetArns,
              }),
            ],
          }),
        },
      });
    }

    // ---- Authorizer config ----
    // Prefer CUSTOM_JWT when the platform Cognito discovery URL is supplied;
    // fall back to AWS_IAM (SigV4) otherwise — `aws:PrincipalArn` on the
    // runtime role is still enforced by the Gateway resource policy below.
    const useJwt =
      typeof props.cognitoDiscoveryUrl === "string" &&
      props.cognitoDiscoveryUrl.length > 0;
    const authorizerType = useJwt ? "CUSTOM_JWT" : "AWS_IAM";
    const authorizerConfiguration = useJwt
      ? {
          customJWTAuthorizer: {
            discoveryUrl: props.cognitoDiscoveryUrl,
            ...(props.cognitoAudience && props.cognitoAudience.length > 0
              ? { allowedAudience: [...props.cognitoAudience] }
              : {}),
          },
        }
      : undefined;

    // ---- Gateway (CreateGateway — SDK-only API, wrapped as AwsCustomResource) ----
    // Name pattern per live API help: ([0-9a-zA-Z][-]?){1,100}. We bake
    // env/tenant/agent into the name so an operator reading the Bedrock console
    // can see the mapping without cross-referencing tags.
    const gatewayName =
      `agenticai-d03-${props.envName}-${props.tenantId}-${props.agentId}-gw`.slice(
        0,
        100,
      );

    const createGatewayParams: Record<string, unknown> = {
      name: gatewayName,
      description: `D-03 v3 per-workstream AgentCore Gateway for ${props.tenantId}/${props.agentId} (${props.envName}).`,
      roleArn: this.gatewayServiceRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          // MCP versions accepted by AgentCore as of 2026-05-05:
          // 2025-11-25, 2025-03-26, 2025-06-18. We pin the earliest that
          // satisfies the currently-documented MCP feature set we rely on.
          supportedVersions: ["2025-06-18"],
          searchType: "SEMANTIC",
        },
      },
      authorizerType,
      ...(authorizerConfiguration ? { authorizerConfiguration } : {}),
      tags: {
        deviation: "D-03",
        "application-id": props.applicationId ?? props.tenantId,
        "tenant-id": props.tenantId,
        "agent-id": props.agentId,
        "cost-centre": props.costCentre ?? "unassigned",
        "workload-account-id": props.workloadAccountId,
        environment: props.envName,
      },
    };

    // LANDMINE (live-verified 2026-07-02): if each AgentCore CR gets its own
    // CDK-generated role, the CR Lambda fires `CreateGateway` within
    // milliseconds of its inline policy being created — before IAM propagates
    // — and fails with "not authorized to perform bedrock-agentcore:
    // CreateGateway" (verified: the exact same policy authorizes the call
    // after ~12s propagation). Fix: (1) a SINGLE shared, explicit CR role so
    // the policy is created once, and (2) a propagation-wait gate the gateway
    // CR depends on, so the first CreateGateway call happens only after IAM
    // has settled.
    // When an operator supplies a pre-created + pre-propagated CR execution
    // role, import it and skip both the inline role AND the propagation gate:
    // a role that already exists has long since propagated to the AgentCore
    // control plane, so there is no deploy-time race to wait out.
    const crExecRoleArnOverride =
      (this.node.tryGetContext("agenticai/d03CrExecRoleArnOverride") as
        | string
        | undefined) ?? props.crExecRoleArnOverride;
    let crRole: import("aws-cdk-lib/aws-iam").IRole;
    let propGate: CustomResource | undefined;
    if (crExecRoleArnOverride) {
      // addGrantsToResources:false + same-account import so CDK treats it as a
      // pre-existing role and does not attempt to mutate it or emit a
      // cross-account PassRole. The role is pre-created with all needed
      // permissions out-of-band.
      crRole = Role.fromRoleArn(
        this,
        "AgentCoreCrRole",
        crExecRoleArnOverride,
        {
          mutable: false,
          addGrantsToResources: false,
        },
      );
    } else {
      const inlineCrRole = new Role(this, "AgentCoreCrRole", {
        roleName: `AgenticAI-D03-${props.envName}-GatewayAdmin`,
        assumedBy: new ServicePrincipal("lambda.amazonaws.com"),
        description:
          "Shared execution role for the D-03 AgentCore Gateway/Target custom resources.",
        inlinePolicies: {
          AgentCoreProvisioning: new PolicyDocument({
            statements: [
              new PolicyStatement({
                // SEC-028: service-scoped wildcard required by AgentCore's
                // action-family evaluator for Create/Update/Delete Gateway +
                // GatewayTarget (+ the Workload Identity CreateGateway spawns).
                // Provisioning-only shared CR role; bounded by SCP-09 at org
                // level. Application/runtime IAM must use explicit actions.
                actions: ["bedrock-agentcore:*"],
                resources: ["*"],
              }),
              new PolicyStatement({
                actions: ["iam:PassRole"],
                resources: [this.gatewayServiceRole.roleArn],
              }),
            ],
          }),
        },
        managedPolicies: [
          ManagedPolicy.fromAwsManagedPolicyName(
            "service-role/AWSLambdaBasicExecutionRole",
          ),
        ],
      });
      NagSuppressions.addResourceSuppressions(
        inlineCrRole,
        [
          {
            id: "AwsSolutions-IAM4",
            appliesTo: [
              "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ],
            reason: "SEC-010: CDK custom-resource default execution role.",
          },
          {
            id: "AwsSolutions-IAM5",
            reason:
              "SEC-028: shared AgentCore provisioning CR role; bedrock-agentcore:* required by action-family evaluator, bounded by SCP-09 + provisioning-only lifetime.",
          },
        ],
        true,
      );
      crRole = inlineCrRole;

      // LANDMINE (live-verified 2026-07-02): a freshly-created CR role's
      // authorization takes MINUTES to propagate to the AgentCore control
      // plane — `CreateGateway` was denied for >4 min after role creation.
      // This gate delays the first AgentCore mutate until propagation settles.
      // (When crExecRoleArnOverride is supplied, this branch is skipped
      // entirely — a pre-created role has already propagated.)
      const propGateOnEvent = new LambdaFunction(this, "CrPropGateOnEvent", {
        runtime: Runtime.NODEJS_20_X,
        handler: "index.onEvent",
        timeout: Duration.seconds(30),
        code: Code.fromInline(IAM_PROP_GATE_HANDLER),
        description: "AgentCore CR IAM-propagation gate — onEvent.",
      });
      const propGateIsComplete = new LambdaFunction(
        this,
        "CrPropGateIsComplete",
        {
          runtime: Runtime.NODEJS_20_X,
          handler: "index.isComplete",
          timeout: Duration.seconds(30),
          code: Code.fromInline(IAM_PROP_GATE_HANDLER),
          description: "AgentCore CR IAM-propagation gate — isComplete.",
        },
      );
      const propProvider = new Provider(this, "CrPropGateProvider", {
        onEventHandler: propGateOnEvent,
        isCompleteHandler: propGateIsComplete,
        queryInterval: Duration.seconds(15),
        totalTimeout: Duration.minutes(10),
      });
      propGate = new CustomResource(this, "CrPropGate", {
        serviceToken: propProvider.serviceToken,
        properties: { RoleArn: inlineCrRole.roleArn },
      });
      propGate.node.addDependency(inlineCrRole);
      NagSuppressions.addResourceSuppressions(
        propGateOnEvent,
        [
          {
            id: "AwsSolutions-IAM4",
            appliesTo: [
              "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ],
            reason:
              "SEC-010: CDK Provider framework Lambda default execution role.",
          },
        ],
        true,
      );
      NagSuppressions.addResourceSuppressions(
        propGateIsComplete,
        [
          {
            id: "AwsSolutions-IAM4",
            appliesTo: [
              "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ],
            reason:
              "SEC-010: CDK Provider framework Lambda default execution role.",
          },
        ],
        true,
      );
    }

    this.gatewayResource = new AwsCustomResource(this, "GatewayResource", {
      resourceType: "Custom::BedrockAgentCoreGateway",
      role: crRole,
      onCreate: {
        service: "bedrock-agentcore-control",
        action: "createGateway",
        parameters: createGatewayParams,
        physicalResourceId: PhysicalResourceId.fromResponse("gatewayId"),
      },
      onUpdate: {
        // Update-on-change remains a read validation for v1, but uses the
        // actual service-minted Gateway ID retained by CloudFormation.
        service: "bedrock-agentcore-control",
        action: "getGateway",
        parameters: {
          gatewayIdentifier: new PhysicalResourceIdReference(),
        },
        physicalResourceId: PhysicalResourceId.fromResponse("gatewayId"),
      },
      onDelete: {
        service: "bedrock-agentcore-control",
        action: "deleteGateway",
        parameters: {
          gatewayIdentifier: new PhysicalResourceIdReference(),
        },
        // Tolerate only an already-absent Gateway. ValidationException must
        // fail loudly because it can mean asynchronously deleting targets are
        // still associated; swallowing that response orphaned a live Gateway.
        ignoreErrorCodesMatching: "ResourceNotFoundException",
      },
      // Uses the shared crRole (policy attached above) — no per-CR policy, so
      // the IAM-propagation race is gated by CrPropGate below.
    });
    // Gate the first CreateGateway call behind IAM propagation of the inline
    // crRole (skipped when a pre-propagated role was imported via override).
    if (propGate) {
      this.gatewayResource.node.addDependency(propGate);
    }

    // The Gateway create returns an object of shape `{ gatewayId, gatewayArn, ... }`.
    // We can read back those attributes for downstream CfnOutputs + per-target wiring.
    const gatewayIdToken = this.gatewayResource.getResponseField("gatewayId");
    const gatewayArnToken = this.gatewayResource.getResponseField("gatewayArn");

    // DeleteGateway rejects a Gateway while asynchronously deleting targets
    // still appear in ListGatewayTargets. Insert a polling barrier in the
    // dependency chain: create Gateway -> barrier -> targets, which reverses
    // to delete targets -> wait until none remain -> delete Gateway.
    const targetDeleteBarrierOnEvent = new LambdaFunction(
      this,
      "TargetDeleteBarrierOnEvent",
      {
        runtime: Runtime.NODEJS_20_X,
        handler: "index.onEvent",
        timeout: Duration.seconds(30),
        code: Code.fromInline(TARGET_DELETION_BARRIER_HANDLER),
        role: crRole,
        description: "Records the Gateway target-deletion barrier lifecycle.",
      },
    );
    const targetDeleteBarrierIsComplete = new LambdaFunction(
      this,
      "TargetDeleteBarrierIsComplete",
      {
        runtime: Runtime.NODEJS_20_X,
        handler: "index.isComplete",
        timeout: Duration.seconds(30),
        code: Code.fromInline(TARGET_DELETION_BARRIER_HANDLER),
        role: crRole,
        description:
          "Waits until AgentCore reports no targets before Gateway deletion.",
      },
    );
    const targetDeleteBarrierProvider = new Provider(
      this,
      "TargetDeleteBarrierProvider",
      {
        onEventHandler: targetDeleteBarrierOnEvent,
        isCompleteHandler: targetDeleteBarrierIsComplete,
        queryInterval: Duration.seconds(10),
        totalTimeout: Duration.minutes(10),
      },
    );
    const targetDeleteBarrier = new CustomResource(
      this,
      "TargetDeleteBarrier",
      {
        serviceToken: targetDeleteBarrierProvider.serviceToken,
        properties: { GatewayIdentifier: gatewayIdToken },
      },
    );
    targetDeleteBarrier.node.addDependency(this.gatewayResource);

    // ---- N GatewayTargets, one per subscribed tool ----
    // Naming: `target-<toolId>` (kebab-case). Both the legacy catalogue and
    // strict GA context expose a validated stable toolId; opaque Registry
    // record IDs never become MCP tool names.
    for (const subId of subscribedIds) {
      const resolvedArn = resolvedToolArns[subId];
      const targetName = `target-${subId}`.slice(0, 100);

      // Description + inputSchema source — both modes now use a validated
      // ToolSpec; GA mode builds it from the pipeline-resolved governance document.
      const legacySpec = subset.find((s) => s.toolId === subId);
      const description =
        legacySpec?.description ?? `Subscribed registry record ${subId}`;
      const inputSchema = legacySpec?.inputSchema ?? { type: "object" };

      const createTargetParams = {
        gatewayIdentifier: gatewayIdToken,
        name: targetName,
        description,
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: resolvedArn,
              toolSchema: {
                inlinePayload: [
                  {
                    name: subId,
                    description,
                    inputSchema,
                  },
                ],
              },
            },
          },
        },
        // Credential provider: use the Gateway's service role (SigV4) by default.
        // Gateway-targets may also carry OAuth2/API-key credential providers;
        // the SigV4 path matches the `GatewayServiceRole → lambda:InvokeFunction`
        // layer-3 enforcement above.
        credentialProviderConfigurations: [
          {
            credentialProviderType: "GATEWAY_IAM_ROLE",
          },
        ],
      };

      const targetResource = new AwsCustomResource(
        this,
        `GatewayTarget-${subId}`,
        {
          resourceType: "Custom::BedrockAgentCoreGatewayTarget",
          role: crRole,
          onCreate: {
            service: "bedrock-agentcore-control",
            action: "createGatewayTarget",
            parameters: createTargetParams,
            // Use the API-returned targetId as the physical id so onDelete can
            // reference it. AgentCore mints a 10-char id (`[0-9a-zA-Z]{10}`);
            // friendly names (like our per-tool kebab) are rejected on delete.
            physicalResourceId: PhysicalResourceId.fromResponse("targetId"),
          },
          onUpdate: {
            service: "bedrock-agentcore-control",
            action: "getGatewayTarget",
            parameters: {
              gatewayIdentifier: gatewayIdToken,
              targetId: new PhysicalResourceIdReference(),
            },
            physicalResourceId: PhysicalResourceId.fromResponse("targetId"),
          },
          onDelete: {
            service: "bedrock-agentcore-control",
            action: "deleteGatewayTarget",
            parameters: {
              gatewayIdentifier: gatewayIdToken,
              targetId: new PhysicalResourceIdReference(),
            },
            // When Create fails, CFN calls Delete with the ORIGINAL physical id
            // (our logical name) instead of the 10-char id from a successful
            // Create — AgentCore rejects it with ValidationException. Swallow
            // that + the normal "already-deleted" case to keep rollback clean.
            ignoreErrorCodesMatching:
              "(ResourceNotFoundException|ValidationException)",
          },
          // Uses the shared crRole (see GatewayResource) — its policy already
          // grants the bedrock-agentcore:* provisioning scope, and the IAM
          // propagation race is gated by CrPropGate (dependency added below).
        },
      );
      // Explicit dependency so the Gateway exists before its targets.
      targetResource.node.addDependency(this.gatewayResource);
      targetResource.node.addDependency(targetDeleteBarrier);
      // When using the Registry, also depend on the per-record fetcher so the
      // CFN graph orders the live-record validation before target creation.
      const fetcher = registryFetchers[subId];
      if (fetcher) {
        targetResource.node.addDependency(fetcher);
      }

      // Per-tool CfnOutput so auditors / downstream stacks can consume the
      // resolved tool ARN without re-deriving from catalogue + platform-acct.
      new CfnOutput(this, `ToolTarget-${subId}`, {
        value: resolvedArn,
        description: legacySpec
          ? `Resolved Lambda ARN for tool ${subId} (owner: ${legacySpec.ownerTeam}).`
          : `Resolved Lambda ARN for subscribed registry record ${subId}.`,
      });
    }

    // ---- Stack-level tags (flow to every taggable resource) ----
    Tags.of(this).add("deviation", "D-03");
    Tags.of(this).add("application-id", props.applicationId ?? props.tenantId);
    Tags.of(this).add("tenant-id", props.tenantId);
    Tags.of(this).add("agent-id", props.agentId);
    Tags.of(this).add("cost-centre", props.costCentre ?? "unassigned");
    Tags.of(this).add("workload-account-id", props.workloadAccountId);
    Tags.of(this).add("environment", props.envName);

    // ---- CfnOutputs ----
    new CfnOutput(this, "GatewayId", {
      value: gatewayIdToken,
      description:
        "AgentCore Gateway id (opaque). Workload runtime consumes this as the MCP endpoint target.",
      exportName: `AgenticAI-D03-GatewayId-${props.envName}-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, "GatewayArn", {
      value: gatewayArnToken,
      description: "AgentCore Gateway ARN.",
      exportName: `AgenticAI-D03-GatewayArn-${props.envName}-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, "GatewayServiceRoleArn", {
      value: this.gatewayServiceRole.roleArn,
      description:
        "IAM role the Gateway assumes to invoke the N subscribed tool Lambdas.",
      exportName: `AgenticAI-D03-GatewayServiceRoleArn-${props.envName}-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, "SubscribedToolCount", {
      value: String(subscribedIds.length),
      description:
        "Number of tools subscribed via allowedToolIds. Matches the N GatewayTarget resources.",
    });
    new CfnOutput(this, "PerTenantCedarPolicy", {
      // The composed Cedar doc — emitted for audit. Once AgentCore policy-engine
      // CR lands (TODO-GW-POLICY-ENGINE) this becomes the payload uploaded
      // to the policy-engine resource rather than just an output.
      value: cedarPolicy,
      description:
        "Composed Cedar policy for this workstream (union of per-tool snippets + default forbid). Audit-only until policy-engine CR support lands.",
    });

    // ---- NagSuppressions ----
    // AwsCustomResource generates a singleton Lambda + default role per stack,
    // which triggers the familiar six-pack of SEC-006..SEC-011 plus the CDK
    // default-role AwsSolutions-IAM4. Same rationale as
    // D03PlatformCoreStack.ConfigureInvocationLogging; apply at stack scope
    // because the singleton sits at stack root, outside the construct tree.
    NagSuppressions.addStackSuppressions(
      this,
      [
        {
          id: "AwsSolutions-L1",
          reason: "SEC-006: CDK-managed AwsCustomResource Lambda runtime.",
        },
        {
          id: "NIST.800.53.R5-LambdaConcurrency",
          reason:
            "SEC-007: Provisioning-time Lambda invoked only by CloudFormation; concurrency would break deploys.",
        },
        {
          id: "NIST.800.53.R5-LambdaDLQ",
          reason:
            "SEC-008: CFN surfaces custom-resource failures directly; DLQ would go unconsumed.",
        },
        {
          id: "NIST.800.53.R5-LambdaInsideVPC",
          reason:
            "SEC-009: AgentCore control-plane is a public IAM-auth endpoint; placing the provisioning Lambda in a VPC would require extra VPCEs only for stack deploys.",
        },
        {
          id: "AwsSolutions-IAM4",
          appliesTo: [
            "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
          ],
          reason:
            "SEC-010: AWSLambdaBasicExecutionRole is the documented managed policy for CDK custom-resource Lambdas.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: ["Resource::*"],
          reason:
            "SEC-011: AgentCore CreateGateway / CreateGatewayTarget are account-level control-plane APIs; no concrete ARN exists at the time of the Create call (the Gateway is being minted). Scoped by action list.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: ["Action::bedrock-agentcore:*"],
          reason:
            "SEC-028: AgentCore action-family evaluation rejects narrow per-action allow-lists for `CreateGatewayTarget` (discovered live 2026-05-05). Scoped by (a) the CDK-managed singleton Lambda lifetime (bounded to stack create/update/delete), (b) Resource:* constrained by the AgentCore control-plane service itself having no per-resource ARN until post-create, and (c) SCP-09 org-level deny on gateway-mutation to every principal except the platform GatewayAdmin role.",
        },
        {
          id: "NIST.800.53.R5-IAMNoInlinePolicy",
          reason:
            "SEC-005: GatewayServiceRole uses an inline policy to keep the exact N target-ARN allow-list visible on the role itself (layer-3 enforcement of the three-layer model).",
        },
        // SEC-029: CDK custom-resources Provider framework internals for the
        // IAM-propagation and target-deletion gates. Their waiter Step
        // Functions and framework onEvent/isComplete/onTimeout Lambda roles
        // are framework-generated and reference each other with function-arn
        // version wildcards (<arn>:*), without ALL-events logging or X-Ray.
        // Not authorable without forking the framework; provisioning-only.
        {
          id: "AwsSolutions-SF1",
          reason:
            "SEC-029: CDK Provider framework waiter Step Function (IAM-propagation gate); logging config is framework-owned.",
        },
        {
          id: "AwsSolutions-SF2",
          reason:
            "SEC-029: CDK Provider framework waiter Step Function; X-Ray is framework-owned.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: ["Resource::<CrPropGateIsComplete083CF04D.Arn>:*"],
          reason:
            "SEC-029: Provider framework inter-Lambda invoke version wildcard.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: ["Resource::<CrPropGateOnEventB36FE5AB.Arn>:*"],
          reason:
            "SEC-029: Provider framework inter-Lambda invoke version wildcard.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: [
            "Resource::<CrPropGateProviderframeworkisCompleteDF39D816.Arn>:*",
          ],
          reason:
            "SEC-029: Provider framework waiter → isComplete invoke version wildcard.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: [
            "Resource::<CrPropGateProviderframeworkonTimeout9DAB4B92.Arn>:*",
          ],
          reason:
            "SEC-029: Provider framework waiter → onTimeout invoke version wildcard.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: [
            "Resource::<TargetDeleteBarrierIsCompleteFE1AD9CE.Arn>:*",
          ],
          reason:
            "SEC-029: Target deletion Provider framework invokes the versioned isComplete handler.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: ["Resource::<TargetDeleteBarrierOnEvent9DDC866B.Arn>:*"],
          reason:
            "SEC-029: Target deletion Provider framework invokes the versioned onEvent handler.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: [
            "Resource::<TargetDeleteBarrierProviderframeworkisCompleteCC8BC692.Arn>:*",
          ],
          reason:
            "SEC-029: Target deletion waiter invokes the Provider framework isComplete version.",
        },
        {
          id: "AwsSolutions-IAM5",
          appliesTo: [
            "Resource::<TargetDeleteBarrierProviderframeworkonTimeout94122D57.Arn>:*",
          ],
          reason:
            "SEC-029: Target deletion waiter invokes the Provider framework timeout version.",
        },
      ],
      true,
    );
  }
}

/**
 * Provider handler for the deletion-order barrier between Gateway targets and
 * the Gateway itself. Target delete APIs are asynchronous: CloudFormation can
 * mark their custom resources deleted before ListGatewayTargets is empty.
 */
const TARGET_DELETION_BARRIER_HANDLER = `
const https = require('https');
const crypto = require('crypto');
function hmac(key, value) { return crypto.createHmac('sha256', key).update(value, 'utf8').digest(); }
function hash(value) { return crypto.createHash('sha256').update(value, 'utf8').digest('hex'); }
async function listTargets(gatewayIdentifier) {
  const region = process.env.AWS_REGION;
  const host = 'bedrock-agentcore-control.' + region + '.amazonaws.com';
  const path = '/gateways/' + encodeURIComponent(gatewayIdentifier) + '/targets/';
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\\.\\d{3}/g, '');
  const dateStamp = amzDate.substring(0, 8);
  const headers = {
    host,
    'x-amz-date': amzDate,
    'x-amz-security-token': process.env.AWS_SESSION_TOKEN,
  };
  const keys = Object.keys(headers).sort();
  const canonicalHeaders = keys.map(k => k + ':' + headers[k] + '\\n').join('');
  const signedHeaders = keys.join(';');
  const canonicalRequest =
    'GET\\n' + path + '\\n\\n' + canonicalHeaders + '\\n' + signedHeaders + '\\n' + hash('');
  const scope = dateStamp + '/' + region + '/bedrock-agentcore/aws4_request';
  const stringToSign =
    'AWS4-HMAC-SHA256\\n' + amzDate + '\\n' + scope + '\\n' + hash(canonicalRequest);
  const kDate = hmac('AWS4' + process.env.AWS_SECRET_ACCESS_KEY, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, 'bedrock-agentcore');
  const kSigning = hmac(kService, 'aws4_request');
  const signature = crypto.createHmac('sha256', kSigning).update(stringToSign, 'utf8').digest('hex');
  headers.authorization =
    'AWS4-HMAC-SHA256 Credential=' + process.env.AWS_ACCESS_KEY_ID + '/' + scope +
    ', SignedHeaders=' + signedHeaders + ', Signature=' + signature;
  return new Promise((resolve, reject) => {
    const req = https.request({ host, path, method: 'GET', headers }, res => {
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode || 0, body }));
    });
    req.on('error', reject);
    req.end();
  });
}
exports.onEvent = async (event) => {
  const gatewayIdentifier = String(event.ResourceProperties.GatewayIdentifier || '');
  if (!gatewayIdentifier) throw new Error('TargetDeleteBarrier requires GatewayIdentifier');
  return { PhysicalResourceId: 'target-delete-barrier-' + gatewayIdentifier };
};
exports.isComplete = async (event) => {
  if (event.RequestType !== 'Delete') return { IsComplete: true };
  const gatewayIdentifier = String(event.ResourceProperties.GatewayIdentifier || '');
  const response = await listTargets(gatewayIdentifier);
  if (response.status === 404) return { IsComplete: true };
  if (response.status < 200 || response.status >= 300) {
    throw new Error('ListGatewayTargets HTTP ' + response.status + ': ' + response.body);
  }
  let parsed;
  try { parsed = JSON.parse(response.body); }
  catch (error) { throw new Error('ListGatewayTargets returned invalid JSON'); }
  const items = Array.isArray(parsed.items) ? parsed.items : [];
  return { IsComplete: items.length === 0 && !parsed.nextToken };
};
`;

/**
 * Inline handler for the AgentCore CR IAM-propagation gate. `onEvent` stamps a
 * completion deadline into the physical id; `isComplete` reports done once
 * that deadline passes. This deterministically delays the first AgentCore
 * mutate call until the shared CR role's inline policy has propagated.
 */
const IAM_PROP_GATE_HANDLER = `
// Fresh IAM role → AgentCore control-plane authorization propagation was
// live-measured to exceed 2 minutes (a 30s wait consistently produced
// "not authorized to perform bedrock-agentcore:CreateGateway"; even 4 min
// was marginal). Use a generous, bounded window; IAM propagation completes
// within minutes. Operators who want to skip this wait can pre-create the CR
// role out-of-band and pass agenticai/d03CrExecRoleArnOverride.
const WAIT_MS = 300000;
exports.onEvent = async (event) => {
  if (event.RequestType === 'Delete') return { PhysicalResourceId: event.PhysicalResourceId };
  const deadline = Date.now() + WAIT_MS;
  return { PhysicalResourceId: 'iam-prop-gate-' + deadline };
};
exports.isComplete = async (event) => {
  if (event.RequestType === 'Delete') return { IsComplete: true };
  const pid = String(event.PhysicalResourceId || '');
  const deadline = parseInt(pid.slice(pid.lastIndexOf('-') + 1), 10) || 0;
  return { IsComplete: Date.now() >= deadline };
};
`;

/**
 * Inline Lambda handler — deploy-time GA Agent Registry record validator.
 *
 * Calls `agent-registry:GetRegistryRecord` through the R1 reader role for the
 * exact Registry/record pair, requires `APPROVED`, and compares the complete
 * custom descriptor SHA-256 to the pipeline-synth value. Target ARN, MCP schema,
 * Cedar policy, entitlement, and ownership drift therefore fail CloudFormation
 * before any Gateway target is created.
 *
 * Idempotent across CloudFormation event types: Create/Update validate, while
 * Delete is a no-op because the Workstream stack never owns Registry records.
 */
const REGISTRY_RECORD_VALIDATOR_HANDLER = `
// GA Agent Registry control-plane REST shape:
//   GET https://agent-registry-control.<region>.api.aws/registries/<rid>/records/<recId>
// Signing service: agent-registry. Built-in SigV4 below avoids any @aws-sdk
// dependency because the inline Lambda must not depend on the runtime's SDK
// version for this newly released service.
const https = require('https');
const crypto = require('crypto');

function hmac(key, str) { return crypto.createHmac('sha256', key).update(str, 'utf8').digest(); }
function hash(str) { return crypto.createHash('sha256').update(str, 'utf8').digest('hex'); }

// Self-contained STS AssumeRole call. The Lambda's execution role has
// sts:AssumeRole on the cross-account RegistryReader role; we sign an
// AssumeRole call with the execution role's task creds and capture the
// returned temporary creds for use against agent-registry-control.
async function sigv4PostForm(opts) {
  const { region, host, body, accessKeyId, secretAccessKey, sessionToken, service } = opts;
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\\.\\d{3}/g, '');
  const dateStamp = amzDate.substring(0, 8);
  const payloadHash = hash(body);
  const headersList = sessionToken
    ? { 'content-type': 'application/x-www-form-urlencoded; charset=utf-8', host, 'x-amz-date': amzDate, 'x-amz-security-token': sessionToken }
    : { 'content-type': 'application/x-www-form-urlencoded; charset=utf-8', host, 'x-amz-date': amzDate };
  const sortedKeys = Object.keys(headersList).sort();
  const canonicalHeaders = sortedKeys.map(k => k + ':' + headersList[k] + '\\n').join('');
  const signedHeaders = sortedKeys.join(';');
  const canonicalRequest =
    'POST\\n/\\n\\n' + canonicalHeaders + '\\n' + signedHeaders + '\\n' + payloadHash;
  const credentialScope = dateStamp + '/' + region + '/' + service + '/aws4_request';
  const stringToSign =
    'AWS4-HMAC-SHA256\\n' + amzDate + '\\n' + credentialScope + '\\n' + hash(canonicalRequest);
  const kDate = hmac('AWS4' + secretAccessKey, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, service);
  const kSigning = hmac(kService, 'aws4_request');
  const signature = crypto.createHmac('sha256', kSigning).update(stringToSign, 'utf8').digest('hex');
  const authHeader =
    'AWS4-HMAC-SHA256 Credential=' + accessKeyId + '/' + credentialScope +
    ', SignedHeaders=' + signedHeaders + ', Signature=' + signature;
  const reqHeaders = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
    'X-Amz-Date': amzDate,
    Authorization: authHeader,
  };
  if (sessionToken) reqHeaders['X-Amz-Security-Token'] = sessionToken;
  return new Promise((resolve, reject) => {
    const req = https.request(
      { host, path: '/', method: 'POST', headers: reqHeaders },
      res => {
        const chunks = [];
        res.on('data', c => chunks.push(c));
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') }));
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function assumeRole(roleArn, externalId, region) {
  const params = new URLSearchParams({
    Action: 'AssumeRole',
    Version: '2011-06-15',
    RoleArn: roleArn,
    RoleSessionName: process.env.REGISTRY_READER_SESSION_NAME || 'registry-validator',
    DurationSeconds: '900',
  });
  if (externalId) params.set('ExternalId', externalId);
  // STS uses a global endpoint; sign for us-east-1 (default region for STS).
  const stsRegion = 'us-east-1';
  const resp = await sigv4PostForm({
    region: stsRegion,
    host: 'sts.amazonaws.com',
    body: params.toString(),
    service: 'sts',
    accessKeyId: process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
    sessionToken: process.env.AWS_SESSION_TOKEN,
  });
  if (resp.status < 200 || resp.status >= 300) {
    throw new Error('STS AssumeRole HTTP ' + resp.status + ': ' + resp.body);
  }
  // Minimal XML extraction — STS returns a stable, simple shape.
  const accessKeyId = (/<AccessKeyId>([^<]+)<\\/AccessKeyId>/.exec(resp.body) || [])[1];
  const secretAccessKey = (/<SecretAccessKey>([^<]+)<\\/SecretAccessKey>/.exec(resp.body) || [])[1];
  const stsSession = (/<SessionToken>([^<]+)<\\/SessionToken>/.exec(resp.body) || [])[1];
  if (!accessKeyId || !secretAccessKey || !stsSession) {
    throw new Error('AssumeRole response missing credentials: ' + resp.body);
  }
  return { accessKeyId, secretAccessKey, sessionToken: stsSession };
}

async function sigv4Get(opts) {
  const { region, host, path, accessKeyId, secretAccessKey, sessionToken } = opts;
  const service = 'agent-registry';
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\\.\\d{3}/g, '');
  const dateStamp = amzDate.substring(0, 8);
  const canonicalUri = path.split('/').map(seg =>
    seg === '' ? '' : encodeURIComponent(decodeURIComponent(seg))
  ).join('/');
  const payloadHash = hash('');
  const headersList = sessionToken
    ? { host, 'x-amz-date': amzDate, 'x-amz-security-token': sessionToken }
    : { host, 'x-amz-date': amzDate };
  const sortedHeaderKeys = Object.keys(headersList).sort();
  const canonicalHeaders = sortedHeaderKeys.map(k => k + ':' + headersList[k] + '\\n').join('');
  const signedHeaders = sortedHeaderKeys.join(';');
  const canonicalRequest =
    'GET\\n' + canonicalUri + '\\n\\n' + canonicalHeaders + '\\n' + signedHeaders + '\\n' + payloadHash;
  const credentialScope = dateStamp + '/' + region + '/' + service + '/aws4_request';
  const stringToSign =
    'AWS4-HMAC-SHA256\\n' + amzDate + '\\n' + credentialScope + '\\n' + hash(canonicalRequest);
  const kDate = hmac('AWS4' + secretAccessKey, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, service);
  const kSigning = hmac(kService, 'aws4_request');
  const signature = crypto.createHmac('sha256', kSigning).update(stringToSign, 'utf8').digest('hex');
  const authHeader =
    'AWS4-HMAC-SHA256 Credential=' + accessKeyId + '/' + credentialScope +
    ', SignedHeaders=' + signedHeaders + ', Signature=' + signature;
  const reqHeaders = { 'X-Amz-Date': amzDate, Authorization: authHeader };
  if (sessionToken) reqHeaders['X-Amz-Security-Token'] = sessionToken;
  return new Promise((resolve, reject) => {
    const req = https.request(
      { host, path, method: 'GET', headers: reqHeaders },
      res => {
        const chunks = [];
        res.on('data', c => chunks.push(c));
        res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') }));
      }
    );
    req.on('error', reject);
    req.end();
  });
}

exports.handler = async (event) => {
  const props = event.ResourceProperties || {};
  const {
    registryId,
    recordId,
    expectedToolId,
    expectedTargetArn,
    expectedDescriptorSha256,
    validationRevision,
    tenantId,
    agentId,
  } = props;
  if (event.RequestType === 'Delete') {
    // No-op delete — the workstream stack never owns the record.
    return { PhysicalResourceId: event.PhysicalResourceId || ('reg-validator-' + recordId) };
  }
  if (!registryId || !recordId || !expectedToolId || !expectedTargetArn || !expectedDescriptorSha256 || !validationRevision) {
    throw new Error(
      'RegistryRecordValidator: registryId, recordId, expectedToolId, expectedTargetArn, expectedDescriptorSha256, and validationRevision are required'
    );
  }
  if (!/^[0-9a-f]{64}$/.test(expectedDescriptorSha256)) {
    throw new Error('RegistryRecordValidator: expectedDescriptorSha256 is invalid');
  }
  if (!/^[0-9a-f]{40}$/.test(validationRevision)) {
    throw new Error('RegistryRecordValidator: validationRevision must be a full Git SHA');
  }
  const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION;
  const host = 'agent-registry-control.' + region + '.api.aws';
  const path = '/registries/' + encodeURIComponent(registryId) + '/records/' + encodeURIComponent(recordId);
  // When the platform Registry lives in a different account, assume the
  // cross-account RegistryReader role and use its temporary creds.
  let creds = {
    accessKeyId: process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
    sessionToken: process.env.AWS_SESSION_TOKEN,
  };
  if (process.env.REGISTRY_READER_ROLE_ARN) {
    creds = await assumeRole(
      process.env.REGISTRY_READER_ROLE_ARN,
      process.env.REGISTRY_READER_EXTERNAL_ID,
      region
    );
  }
  const resp = await sigv4Get({
    region,
    host,
    path,
    accessKeyId: creds.accessKeyId,
    secretAccessKey: creds.secretAccessKey,
    sessionToken: creds.sessionToken,
  });
  if (resp.status === 404) {
    throw new Error(
      'AgentCore Registry record \\'' + recordId + '\\' not found in registry \\'' + registryId +
      '\\'. Subscribe to an existing approved record, or ask a curator to publish + approve this id. ' +
      '(tenant=' + tenantId + ' agent=' + agentId + ')'
    );
  }
  if (resp.status < 200 || resp.status >= 300) {
    throw new Error(
      'AgentCore Registry GetRegistryRecord HTTP ' + resp.status + ' for record \\'' + recordId +
      '\\': ' + resp.body
    );
  }
  let parsed;
  try { parsed = JSON.parse(resp.body); }
  catch (e) { throw new Error('Could not parse GetRegistryRecord response: ' + resp.body); }
  const status = parsed && parsed.status;
  if (status !== 'APPROVED') {
    throw new Error(
      'AgentCore Registry record \\'' + recordId + '\\' has status \\'' + (status || 'UNKNOWN') +
      '\\'. Only APPROVED records may be subscribed by a workstream Gateway. ' +
      'Pick a different record, or ask a curator to re-approve this one. ' +
      '(tenant=' + tenantId + ' agent=' + agentId + ')'
    );
  }
  if (parsed.recordId !== recordId || parsed.name !== expectedToolId) {
    throw new Error(
      'GA Registry record identity differs from the pipeline-resolved context for record ' + recordId
    );
  }
  if (parsed.recordType !== 'CUSTOM') {
    throw new Error('GA Registry record ' + recordId + ' is not CUSTOM');
  }
  const descriptors = parsed.descriptors;
  const custom = descriptors && descriptors.custom;
  const data = custom && custom.data;
  if (typeof data !== 'string' || data.length === 0) {
    throw new Error('GA Registry record ' + recordId + ' has no custom descriptor data');
  }
  const actualDigest = hash(data);
  if (actualDigest !== expectedDescriptorSha256) {
    throw new Error(
      'GA Registry record ' + recordId + ' descriptor digest changed after pipeline synth'
    );
  }
  let governance;
  try { governance = JSON.parse(data); }
  catch (e) { throw new Error('GA Registry record ' + recordId + ' descriptor is not JSON'); }
  if (!governance || typeof governance !== 'object' || Array.isArray(governance)) {
    throw new Error('GA Registry record ' + recordId + ' governance document is not an object');
  }
  const documentKeys = [
    'authorization', 'catalogueVersion', 'description', 'desiredApprovalStatus',
    'mcp', 'ownership', 'schemaVersion', 'target', 'toolId'
  ];
  if (JSON.stringify(Object.keys(governance).sort()) !== JSON.stringify(documentKeys)) {
    throw new Error('GA Registry record ' + recordId + ' governance keys changed');
  }
  if (
    governance.schemaVersion !== 'agenticai.tool-governance/1.0' ||
    typeof governance.catalogueVersion !== 'string' ||
    !/^[1-9][0-9]*$/.test(governance.catalogueVersion) ||
    parsed.recordVersion !== governance.catalogueVersion + '.0.0' ||
    governance.toolId !== expectedToolId ||
    governance.desiredApprovalStatus !== 'approved'
  ) {
    throw new Error('GA Registry record ' + recordId + ' governance identity/status changed');
  }
  const target = governance.target;
  if (
    !target || target.type !== 'lambda' ||
    typeof target.arn !== 'string' || target.arn.length === 0
  ) {
    throw new Error('GA Registry record ' + recordId + ' target is invalid');
  }
  if (target.arn !== expectedTargetArn) {
    throw new Error(
      'GA Registry record ' + recordId + ' target ARN differs from the Gateway target'
    );
  }
  const mcp = governance.mcp;
  if (
    !mcp || mcp.toolName !== expectedToolId ||
    typeof mcp.description !== 'string' ||
    !mcp.inputSchema || typeof mcp.inputSchema !== 'object' || Array.isArray(mcp.inputSchema)
  ) {
    throw new Error('GA Registry record ' + recordId + ' MCP contract is invalid');
  }
  const authorization = governance.authorization;
  if (
    !authorization || authorization.defaultDecision !== 'DENY' ||
    typeof authorization.cedarPolicy !== 'string' ||
    authorization.cedarPolicy.indexOf('permit') < 0 ||
    !Array.isArray(authorization.allowedSubjects) ||
    authorization.allowedSubjects.length !== 0 ||
    !Array.isArray(authorization.allowedGroups)
  ) {
    throw new Error('GA Registry record ' + recordId + ' authorization contract is invalid');
  }
  const groups = authorization.allowedGroups;
  if (groups.some(function (group) { return typeof group !== 'string' || group.length === 0; })) {
    throw new Error('GA Registry record ' + recordId + ' allowedGroups is invalid');
  }
  const expectedCombination = groups.length > 0 ? 'GROUP_ONLY' : 'AUTHENTICATED';
  if (authorization.combination !== expectedCombination) {
    throw new Error('GA Registry record ' + recordId + ' authorization combination changed');
  }
  const ownership = governance.ownership;
  if (
    !ownership || typeof ownership.ownerTeam !== 'string' || !ownership.ownerTeam ||
    typeof ownership.costCentre !== 'string' || !ownership.costCentre
  ) {
    throw new Error('GA Registry record ' + recordId + ' ownership contract is invalid');
  }
  if (groups.length > 0 && process.env.GATEWAY_AUTHORIZER_MODE !== 'CUSTOM_JWT') {
    throw new Error(
      'GA Registry record ' + recordId + ' carries allowedGroups but the Workstream Gateway is not CUSTOM_JWT'
    );
  }
  return {
    PhysicalResourceId: 'AgenticAI-RegistryValidator-' + tenantId + '-' + agentId + '-' + recordId,
    Data: {
      toolId: expectedToolId,
      descriptorSha256: actualDigest,
      status,
      validationRevision,
    },
  };
};
`;
