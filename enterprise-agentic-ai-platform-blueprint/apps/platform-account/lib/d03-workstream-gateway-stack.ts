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
import { CfnOutput, CustomResource, Duration, Stack, StackProps, Tags } from 'aws-cdk-lib';
import {
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import { RetentionDays } from 'aws-cdk-lib/aws-logs';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
  PhysicalResourceIdReference,
  Provider,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

import {
  composeCedarPolicyDocument,
  resolveSubscribedTools,
  resolveTargetArn,
  ToolSpec,
} from '@agenticai/platform-tool-catalogue';

export interface D03WorkstreamGatewayStackProps extends StackProps {
  readonly tenantId: string;
  readonly agentId: string;
  readonly envName: string;
  readonly workloadAccountId: string;
  readonly platformAccountId: string;
  /**
   * Legacy v0.4.0 path: kebab-case tool ids resolved against the in-process
   * `PLATFORM_TOOL_CATALOGUE`. Mutually exclusive with
   * `subscribedRegistryRecords` — pass exactly one.
   */
  readonly allowedToolIds?: readonly string[];
  /**
   * v0.5.0 path: kebab-case record ids resolved at deploy time via
   * `GetRegistryRecord` against the platform AgentCore Registry. When set,
   * `registryId` MUST also be supplied; the synth fails the build if any
   * record id is missing or DEPRECATED at deploy time.
   *
   * Subscribed record metadata (Lambda arn, Cedar policy) is sourced from the
   * Registry record's `metadata` map populated at seed time by
   * `D03PlatformCoreStack.enableAgentRegistry`.
   */
  readonly subscribedRegistryRecords?: readonly string[];
  /** AgentCore Registry id (token from `D03PlatformCoreStack.agentRegistry.registryId`). */
  readonly registryId?: string;
  /**
   * Optional ARN of a cross-account role in the platform account that the
   * deploy-time Registry validator Lambda will assume to read records.
   *
   * Required when the platform Registry lives in a different AWS account
   * from the workstream gateway (the typical centralised-platform topology).
   * The role must:
   *   - trust this stack's account (`workloadAccountId`)
   *   - trust the supplied externalId (when set, defaults to a
   *     deterministic `agenticai-v05-<workloadAccountId>` token)
   *   - allow `bedrock-agentcore:GetRegistryRecord` on the registry's ARN
   *
   * When unset, the validator Lambda calls GetRegistryRecord directly
   * with its own (workload-account) credentials — works only when the
   * registry lives in the same account as the gateway stack.
   */
  readonly registryReaderRoleArn?: string;
  /** External id used when assuming `registryReaderRoleArn`. */
  readonly registryReaderExternalId?: string;
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

  constructor(scope: Construct, id: string, props: D03WorkstreamGatewayStackProps) {
    super(scope, id, props);

    // ---- Mode selection: legacy catalogue vs v0.5.0 Registry ----
    // The two paths are mutually exclusive: subscribedRegistryRecords requires
    // a registryId. Both empty / both set => synth-time error.
    const usingRegistry =
      Array.isArray(props.subscribedRegistryRecords) &&
      props.subscribedRegistryRecords.length > 0;
    if (usingRegistry && (!props.registryId || props.registryId.length === 0)) {
      throw new Error(
        "D03WorkstreamGatewayStack: 'registryId' is required when 'subscribedRegistryRecords' is set.",
      );
    }
    if (
      usingRegistry &&
      Array.isArray(props.allowedToolIds) &&
      props.allowedToolIds.length > 0
    ) {
      throw new Error(
        "D03WorkstreamGatewayStack: 'allowedToolIds' (legacy) and 'subscribedRegistryRecords' (v0.5.0) are mutually exclusive — pass exactly one.",
      );
    }
    if (!usingRegistry && (!props.allowedToolIds || props.allowedToolIds.length === 0)) {
      throw new Error(
        "D03WorkstreamGatewayStack: must supply either 'allowedToolIds' (legacy) or 'subscribedRegistryRecords' + 'registryId' (v0.5.0).",
      );
    }

    let subset: readonly ToolSpec[] = [];
    let resolvedToolArns: Record<string, string> = {};
    let cedarPolicy: string;
    /** When using the Registry, each entry is a deploy-time `GetRegistryRecord` validator custom resource. */
    const registryFetchers: Record<string, CustomResource> = {};

    if (usingRegistry) {
      // ---- v0.5.0 Registry path ----
      // For every subscribed record, fetch its metadata from the live
      // Registry at deploy time AND assert `status === 'APPROVED'`. The
      // assertion is the third leg of the three-layer governance model:
      // a record that has been deprecated or rejected by a curator MUST
      // not deploy into a workstream Gateway, even if a developer left
      // the recordId in cdk.context.json by mistake.
      //
      // We can't use `AwsCustomResource` here because `AwsCustomResource`
      // has no fail-on-condition primitive — its onCreate Lambda never
      // throws when the SDK call succeeds. Instead, we provision a single
      // Lambda-backed Provider + one CustomResource per subscribed record
      // id. The Lambda calls GetRegistryRecord, asserts status==APPROVED,
      // and throws an actionable error otherwise. Throwing in the Provider
      // returns FAILED to CFN, which fails the stack deploy with the
      // Lambda's error message bubbled into the CFN event log — exactly
      // what the developer needs to triage a "stale subscription" PR.
      const recordIds = props.subscribedRegistryRecords as readonly string[];
      const validatorEnv: Record<string, string> = {};
      if (props.registryReaderRoleArn) {
        validatorEnv.REGISTRY_READER_ROLE_ARN = props.registryReaderRoleArn;
        validatorEnv.REGISTRY_READER_EXTERNAL_ID =
          props.registryReaderExternalId ?? `agenticai-v05-${props.workloadAccountId}`;
      }
      // Phase Q: allow the validator to fail the deploy when a record
      // declares allowedGroups but the workstream Gateway is not configured
      // for CUSTOM_JWT auth — Cedar group binding has nothing to evaluate
      // against without JWT claims.
      validatorEnv.GATEWAY_AUTHORIZER_MODE =
        typeof props.cognitoDiscoveryUrl === 'string' && props.cognitoDiscoveryUrl.length > 0
          ? 'CUSTOM_JWT'
          : 'AWS_IAM';
      const validatorFn = new LambdaFunction(this, 'RegistryRecordValidatorFn', {
        functionName: `agenticai-d03-${props.tenantId}-${props.agentId}-reg-validator`.slice(0, 64),
        runtime: Runtime.NODEJS_20_X,
        handler: 'index.handler',
        timeout: Duration.minutes(1),
        memorySize: 256,
        logRetention: RetentionDays.ONE_MONTH,
        description: 'Validates that subscribed AgentCore Registry records are APPROVED at deploy time.',
        code: Code.fromInline(REGISTRY_RECORD_VALIDATOR_HANDLER),
        environment: validatorEnv,
      });
      if (props.registryReaderRoleArn) {
        validatorFn.addToRolePolicy(
          new PolicyStatement({
            effect: Effect.ALLOW,
            actions: ['sts:AssumeRole'],
            resources: [props.registryReaderRoleArn],
          }),
        );
      } else {
        validatorFn.addToRolePolicy(
          new PolicyStatement({
            effect: Effect.ALLOW,
            actions: [
              'bedrock-agentcore:GetRegistryRecord',
              'bedrock-agentcore:ListRegistryRecords',
            ],
            resources: ['*'],
          }),
        );
      }
      const validatorProvider = new Provider(this, 'RegistryRecordValidatorProvider', {
        onEventHandler: validatorFn,
        logRetention: RetentionDays.ONE_MONTH,
      });
      // The Provider framework Lambda gets auto-generated permissions to
      // invoke the onEventHandler — its DefaultPolicy contains an
      // `lambda:InvokeFunction` allow on the validator Fn.Arn:* that
      // cdk-nag flags. Suppress on the framework path.
      NagSuppressions.addResourceSuppressionsByPath(
        Stack.of(this),
        '/' + Stack.of(this).stackName + '/RegistryRecordValidatorProvider/framework-onEvent/ServiceRole/DefaultPolicy/Resource',
        [
          {
            id: 'AwsSolutions-IAM5',
            reason: 'SEC-029: CDK Provider framework needs lambda:InvokeFunction on the validator Lambda; the Resource wildcard is on Lambda versions/aliases of a single function we just created in this stack.',
          },
        ],
        true,
      );
      NagSuppressions.addResourceSuppressionsByPath(
        Stack.of(this),
        '/' + Stack.of(this).stackName + '/RegistryRecordValidatorProvider/framework-onEvent/ServiceRole/Resource',
        [
          { id: 'AwsSolutions-IAM4', reason: 'SEC-010: AWSLambdaBasicExecutionRole is the documented managed policy for CDK provider framework Lambdas.' },
        ],
        true,
      );
      NagSuppressions.addResourceSuppressionsByPath(
        Stack.of(this),
        '/' + Stack.of(this).stackName + '/RegistryRecordValidatorProvider/framework-onEvent/Resource',
        [
          { id: 'AwsSolutions-L1', reason: 'SEC-006: Provider framework Lambda runtime is managed by aws-cdk-lib.' },
          { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: Provisioning-time only.' },
          { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces failures.' },
          { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: Control-plane only.' },
        ],
        true,
      );
      // Cedar union — assembled from per-record validators' attribute
      // tokens (`Data.cedarPolicy`). Joined with a header + separators
      // identical to the legacy composeCedarPolicyDocument layout so
      // audit downstream stays stable.
      const cedarHeader =
        '// AgenticAI workstream Cedar — union of per-record snippets sourced from\n' +
        `// the platform AgentCore Registry (registryId=${props.registryId}).\n`;
      const cedarParts: string[] = [];
      for (const recId of recordIds) {
        const validator = new CustomResource(this, `RegistryFetch-${recId}`, {
          resourceType: 'Custom::AgenticAIRegistryRecordValidator',
          serviceToken: validatorProvider.serviceToken,
          properties: {
            // Embed the registry/record pair so the Lambda can look it up.
            // ChangeNonce flips on every synth so an updated record (e.g.
            // newly DEPRECATED) is re-validated on the next deploy.
            registryId: props.registryId,
            recordId: recId,
            tenantId: props.tenantId,
            agentId: props.agentId,
            changeNonce: `${Date.now()}`,
          },
        });
        registryFetchers[recId] = validator;
        // Lambda returns the resolved metadata fields under `Data.*`. Pin
        // them as CFN attribute tokens — the same Cedar bytes that the
        // platform stored are the bytes the workstream gateway uses.
        const arnToken = validator.getAttString('gatewayTargetArn');
        const cedarToken = validator.getAttString('cedarPolicy');
        resolvedToolArns[recId] = arnToken;
        cedarParts.push(cedarToken);
      }
      cedarPolicy = cedarHeader + cedarParts.join('\n\n');
      // `subset` stays empty — there is no in-process catalogue to mirror;
      // the synth-time three-layer model is preserved by SCP-11/SCP-09 +
      // the deploy-time validator above (records that don't exist or are
      // not status==APPROVED fail the deploy with an actionable error).
      subset = [];
      NagSuppressions.addResourceSuppressions(
        validatorFn,
        [
          { id: 'AwsSolutions-IAM5', reason: 'SEC-029: GetRegistryRecord/ListRegistryRecords accept resource:* — AgentCore registry-record ARN is the resource being read; deny would require knowing the recordId-to-arn mapping ahead of time, which is what this Lambda is computing.' },
          { id: 'AwsSolutions-IAM4', reason: 'SEC-010: AWSLambdaBasicExecutionRole is the documented managed policy for CDK provider Lambdas.' },
          { id: 'AwsSolutions-L1', reason: 'SEC-006: NodeJS 20 is the latest CDK-supported runtime as of v0.5.0.' },
          { id: 'NIST.800.53.R5-LambdaConcurrency', reason: 'SEC-007: Provisioning-time Lambda invoked only by CloudFormation; concurrency would break deploys.' },
          { id: 'NIST.800.53.R5-LambdaDLQ', reason: 'SEC-008: CFN surfaces custom-resource failures directly; DLQ would go unconsumed.' },
          { id: 'NIST.800.53.R5-LambdaInsideVPC', reason: 'SEC-009: AgentCore control-plane is a public IAM-auth endpoint; placing the provisioning Lambda in a VPC would require extra VPCEs only for stack deploys.' },
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
        resolvedToolArns[spec.toolId] = resolveTargetArn(spec, props.platformAccountId);
      }
      // Phase Q (v0.6.0): when any subscribed tool declares allowedGroups, the
      // workstream Gateway MUST be configured for CUSTOM_JWT — Cedar group
      // binding has nothing to evaluate against without JWT claims. Fail the
      // synth with an actionable error rather than silently degrading to
      // "any authenticated principal" semantics.
      const usingJwt =
        typeof props.cognitoDiscoveryUrl === 'string' && props.cognitoDiscoveryUrl.length > 0;
      const entitledTools = subset.filter(
        (s) => Array.isArray(s.allowedGroups) && s.allowedGroups.length > 0,
      );
      if (entitledTools.length > 0 && !usingJwt) {
        throw new Error(
          `D03WorkstreamGatewayStack: tool(s) [${entitledTools
            .map((s) => s.toolId)
            .join(', ')}] declare allowedGroups (per-developer entitlement) but no cognitoDiscoveryUrl was supplied. ` +
            `Phase Q requires CUSTOM_JWT auth so the Cedar evaluator can read the principal's cognito:groups claim. ` +
            `Either set cognitoDiscoveryUrl on D03WorkstreamGatewayStackProps or remove allowedGroups from the affected tool(s).`,
        );
      }
      cedarPolicy = composeCedarPolicyDocument(subset);
    }
    this.subscribedTools = subset;
    const subscribedIds: readonly string[] = usingRegistry
      ? (props.subscribedRegistryRecords as readonly string[])
      : subset.map((s) => s.toolId);
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
    const gwRoleNameDefault = `AgenticAI-D03-${props.tenantId}-${props.agentId}-gw-svc`;
    const gwRoleArnOverride =
      (this.node.tryGetContext('agenticai/d03GatewayRoleArnOverride') as
        | string
        | undefined) ?? props.gatewayServiceRoleArnOverride;

    if (typeof gwRoleArnOverride === 'string' && gwRoleArnOverride.length > 0) {
      this.gatewayServiceRole = Role.fromRoleArn(
        this,
        'GatewayServiceRole',
        gwRoleArnOverride,
        { mutable: false },
      ) as Role;
    } else {
      this.gatewayServiceRole = new Role(this, 'GatewayServiceRole', {
        roleName: gwRoleNameDefault,
        assumedBy: new ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description: `D-03 v3: service role assumed by AgentCore Gateway for tenant=${props.tenantId} agent=${props.agentId}. Scoped to the exact N subscribed tool ARNs.`,
        inlinePolicies: {
          InvokeSubscribedTools: new PolicyDocument({
            statements: [
              new PolicyStatement({
                sid: 'InvokeSubscribedTools',
                effect: Effect.ALLOW,
                actions: ['lambda:InvokeFunction'],
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
      typeof props.cognitoDiscoveryUrl === 'string' &&
      props.cognitoDiscoveryUrl.length > 0;
    const authorizerType = useJwt ? 'CUSTOM_JWT' : 'AWS_IAM';
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
    const gatewayName = `agenticai-d03-${props.envName}-${props.tenantId}-${props.agentId}-gw`.slice(
      0,
      100,
    );
    const gatewayPhysicalId = `AgenticAI-D03-Gateway-${props.tenantId}-${props.agentId}`;

    const createGatewayParams: Record<string, unknown> = {
      name: gatewayName,
      description: `D-03 v3 per-workstream AgentCore Gateway for ${props.tenantId}/${props.agentId} (${props.envName}).`,
      roleArn: this.gatewayServiceRole.roleArn,
      protocolType: 'MCP',
      protocolConfiguration: {
        mcp: {
          // MCP versions accepted by AgentCore as of 2026-05-05:
          // 2025-11-25, 2025-03-26, 2025-06-18. We pin the earliest that
          // satisfies the currently-documented MCP feature set we rely on.
          supportedVersions: ['2025-06-18'],
          searchType: 'SEMANTIC',
        },
      },
      authorizerType,
      ...(authorizerConfiguration ? { authorizerConfiguration } : {}),
      tags: {
        deviation: 'D-03',
        'tenant-id': props.tenantId,
        'agent-id': props.agentId,
        'workload-account-id': props.workloadAccountId,
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
      (this.node.tryGetContext('agenticai/d03CrExecRoleArnOverride') as string | undefined) ??
      props.crExecRoleArnOverride;
    let crRole: import('aws-cdk-lib/aws-iam').IRole;
    let propGate: CustomResource | undefined;
    if (crExecRoleArnOverride) {
      // addGrantsToResources:false + same-account import so CDK treats it as a
      // pre-existing role and does not attempt to mutate it or emit a
      // cross-account PassRole. The role is pre-created with all needed
      // permissions out-of-band.
      crRole = Role.fromRoleArn(this, 'AgentCoreCrRole', crExecRoleArnOverride, {
        mutable: false,
        addGrantsToResources: false,
      });
    } else {
      const inlineCrRole = new Role(this, 'AgentCoreCrRole', {
        assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
        description: 'Shared execution role for the D-03 AgentCore Gateway/Target custom resources.',
        inlinePolicies: {
          AgentCoreProvisioning: new PolicyDocument({
            statements: [
              new PolicyStatement({
                // SEC-028: service-scoped wildcard required by AgentCore's
                // action-family evaluator for Create/Update/Delete Gateway +
                // GatewayTarget (+ the Workload Identity CreateGateway spawns).
                // Provisioning-only shared CR role; bounded by SCP-09 at org
                // level. Application/runtime IAM must use explicit actions.
                actions: ['bedrock-agentcore:*'],
                resources: ['*'],
              }),
              new PolicyStatement({
                actions: ['iam:PassRole'],
                resources: [this.gatewayServiceRole.roleArn],
              }),
            ],
          }),
        },
        managedPolicies: [
          ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        ],
      });
      NagSuppressions.addResourceSuppressions(
        inlineCrRole,
        [
          { id: 'AwsSolutions-IAM4', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'], reason: 'SEC-010: CDK custom-resource default execution role.' },
          { id: 'AwsSolutions-IAM5', reason: 'SEC-028: shared AgentCore provisioning CR role; bedrock-agentcore:* required by action-family evaluator, bounded by SCP-09 + provisioning-only lifetime.' },
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
      const propGateOnEvent = new LambdaFunction(this, 'CrPropGateOnEvent', {
        runtime: Runtime.NODEJS_20_X,
        handler: 'index.onEvent',
        timeout: Duration.seconds(30),
        code: Code.fromInline(IAM_PROP_GATE_HANDLER),
        description: 'AgentCore CR IAM-propagation gate — onEvent.',
      });
      const propGateIsComplete = new LambdaFunction(this, 'CrPropGateIsComplete', {
        runtime: Runtime.NODEJS_20_X,
        handler: 'index.isComplete',
        timeout: Duration.seconds(30),
        code: Code.fromInline(IAM_PROP_GATE_HANDLER),
        description: 'AgentCore CR IAM-propagation gate — isComplete.',
      });
      const propProvider = new Provider(this, 'CrPropGateProvider', {
        onEventHandler: propGateOnEvent,
        isCompleteHandler: propGateIsComplete,
        queryInterval: Duration.seconds(15),
        totalTimeout: Duration.minutes(10),
      });
      propGate = new CustomResource(this, 'CrPropGate', {
        serviceToken: propProvider.serviceToken,
        properties: { RoleArn: inlineCrRole.roleArn },
      });
      propGate.node.addDependency(inlineCrRole);
      NagSuppressions.addResourceSuppressions(propGateOnEvent, [{ id: 'AwsSolutions-IAM4', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'], reason: 'SEC-010: CDK Provider framework Lambda default execution role.' }], true);
      NagSuppressions.addResourceSuppressions(propGateIsComplete, [{ id: 'AwsSolutions-IAM4', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'], reason: 'SEC-010: CDK Provider framework Lambda default execution role.' }], true);
    }

    this.gatewayResource = new AwsCustomResource(this, 'GatewayResource', {
      resourceType: 'Custom::BedrockAgentCoreGateway',
      role: crRole,
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'createGateway',
        parameters: createGatewayParams,
        physicalResourceId: PhysicalResourceId.of(gatewayPhysicalId),
      },
      onUpdate: {
        // Update-on-change delegated to the service — AgentCore `UpdateGateway`
        // is a separate API; for v1 we no-op updates and rely on replace if
        // the gateway name (physicalResourceId) ever changes. We MUST NOT set
        // `ignoreErrorCodesMatching` on onCreate/onUpdate here: CDK disallows
        // it alongside `getResponseField()` / `getDataString()` (the
        // IgnoreErrorCodesMatchingNotAllowed check). Idempotency is instead
        // enforced by the stable physicalResourceId — CloudFormation will
        // skip the Create call on subsequent deploys.
        service: 'bedrock-agentcore-control',
        action: 'getGateway',
        parameters: {
          gatewayIdentifier: new PhysicalResourceIdReferenceShim(gatewayPhysicalId).value,
        },
        physicalResourceId: PhysicalResourceId.of(gatewayPhysicalId),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'deleteGateway',
        parameters: {
          gatewayIdentifier: new PhysicalResourceIdReferenceShim(gatewayPhysicalId).value,
        },
        // Tolerate the common rollback cases where the Gateway was never
        // created (CFN invokes Delete after a Create-failure) or was
        // deleted out-of-band.
        ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException)',
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
    const gatewayIdToken = this.gatewayResource.getResponseField('gatewayId');
    const gatewayArnToken = this.gatewayResource.getResponseField('gatewayArn');

    // ---- N GatewayTargets, one per subscribed tool ----
    // Naming: `target-<toolId>` (kebab-case). Both the legacy ToolSpec.toolId
    // and the v0.5.0 RegistryRecord.recordId are validated as kebab-case at
    // their source, so the composed name satisfies the AgentCore target-name
    // pattern in either mode.
    for (const subId of subscribedIds) {
      const resolvedArn = resolvedToolArns[subId];
      const targetName = `target-${subId}`.slice(0, 100);
      const targetPhysicalId = `AgenticAI-D03-GwTarget-${props.tenantId}-${props.agentId}-${subId}`;

      // Description + inputSchema source — legacy uses ToolSpec, v0.5.0 uses
      // a deploy-time-readable token from the registry fetcher.
      const legacySpec = subset.find((s) => s.toolId === subId);
      const description = legacySpec?.description ?? `Subscribed registry record ${subId}`;
      const inputSchema = legacySpec?.inputSchema ?? { type: 'object' };

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
            credentialProviderType: 'GATEWAY_IAM_ROLE',
          },
        ],
      };

      const targetResource = new AwsCustomResource(this, `GatewayTarget-${subId}`, {
        resourceType: 'Custom::BedrockAgentCoreGatewayTarget',
        role: crRole,
        onCreate: {
          service: 'bedrock-agentcore-control',
          action: 'createGatewayTarget',
          parameters: createTargetParams,
          // Use the API-returned targetId as the physical id so onDelete can
          // reference it. AgentCore mints a 10-char id (`[0-9a-zA-Z]{10}`);
          // friendly names (like our per-tool kebab) are rejected on delete.
          physicalResourceId: PhysicalResourceId.fromResponse('targetId'),
        },
        onUpdate: {
          service: 'bedrock-agentcore-control',
          action: 'getGatewayTarget',
          parameters: {
            gatewayIdentifier: gatewayIdToken,
            targetId: new PhysicalResourceIdReference(),
          },
          physicalResourceId: PhysicalResourceId.fromResponse('targetId'),
        },
        onDelete: {
          service: 'bedrock-agentcore-control',
          action: 'deleteGatewayTarget',
          parameters: {
            gatewayIdentifier: gatewayIdToken,
            targetId: new PhysicalResourceIdReference(),
          },
          // When Create fails, CFN calls Delete with the ORIGINAL physical id
          // (our logical name) instead of the 10-char id from a successful
          // Create — AgentCore rejects it with ValidationException. Swallow
          // that + the normal "already-deleted" case to keep rollback clean.
          ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException)',
        },
        // Uses the shared crRole (see GatewayResource) — its policy already
        // grants the bedrock-agentcore:* provisioning scope, and the IAM
        // propagation race is gated by CrPropGate (dependency added below).
      });
      // Explicit dependency so the Gateway exists before its targets.
      targetResource.node.addDependency(this.gatewayResource);
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
    Tags.of(this).add('deviation', 'D-03');
    Tags.of(this).add('tenant-id', props.tenantId);
    Tags.of(this).add('agent-id', props.agentId);
    Tags.of(this).add('workload-account-id', props.workloadAccountId);
    Tags.of(this).add('environment', props.envName);

    // ---- CfnOutputs ----
    new CfnOutput(this, 'GatewayId', {
      value: gatewayIdToken,
      description: 'AgentCore Gateway id (opaque). Workload runtime consumes this as the MCP endpoint target.',
      exportName: `AgenticAI-D03-GatewayId-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, 'GatewayArn', {
      value: gatewayArnToken,
      description: 'AgentCore Gateway ARN.',
      exportName: `AgenticAI-D03-GatewayArn-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, 'GatewayServiceRoleArn', {
      value: this.gatewayServiceRole.roleArn,
      description: 'IAM role the Gateway assumes to invoke the N subscribed tool Lambdas.',
      exportName: `AgenticAI-D03-GatewayServiceRoleArn-${props.tenantId}-${props.agentId}`,
    });
    new CfnOutput(this, 'SubscribedToolCount', {
      value: String(subscribedIds.length),
      description: 'Number of tools subscribed via allowedToolIds. Matches the N GatewayTarget resources.',
    });
    new CfnOutput(this, 'PerTenantCedarPolicy', {
      // The composed Cedar doc — emitted for audit. Once AgentCore policy-engine
      // CR lands (TODO-GW-POLICY-ENGINE) this becomes the payload uploaded
      // to the policy-engine resource rather than just an output.
      value: cedarPolicy,
      description:
        'Composed Cedar policy for this workstream (union of per-tool snippets + default forbid). Audit-only until policy-engine CR support lands.',
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
        { id: 'AwsSolutions-L1', reason: 'SEC-006: CDK-managed AwsCustomResource Lambda runtime.' },
        {
          id: 'NIST.800.53.R5-LambdaConcurrency',
          reason: 'SEC-007: Provisioning-time Lambda invoked only by CloudFormation; concurrency would break deploys.',
        },
        {
          id: 'NIST.800.53.R5-LambdaDLQ',
          reason: 'SEC-008: CFN surfaces custom-resource failures directly; DLQ would go unconsumed.',
        },
        {
          id: 'NIST.800.53.R5-LambdaInsideVPC',
          reason: 'SEC-009: AgentCore control-plane is a public IAM-auth endpoint; placing the provisioning Lambda in a VPC would require extra VPCEs only for stack deploys.',
        },
        {
          id: 'AwsSolutions-IAM4',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          reason: 'SEC-010: AWSLambdaBasicExecutionRole is the documented managed policy for CDK custom-resource Lambdas.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Resource::*'],
          reason: 'SEC-011: AgentCore CreateGateway / CreateGatewayTarget are account-level control-plane APIs; no concrete ARN exists at the time of the Create call (the Gateway is being minted). Scoped by action list.',
        },
        {
          id: 'AwsSolutions-IAM5',
          appliesTo: ['Action::bedrock-agentcore:*'],
          reason: 'SEC-028: AgentCore action-family evaluation rejects narrow per-action allow-lists for `CreateGatewayTarget` (discovered live 2026-05-05). Scoped by (a) the CDK-managed singleton Lambda lifetime (bounded to stack create/update/delete), (b) Resource:* constrained by the AgentCore control-plane service itself having no per-resource ARN until post-create, and (c) SCP-09 org-level deny on gateway-mutation to every principal except the platform GatewayAdmin role.',
        },
        {
          id: 'NIST.800.53.R5-IAMNoInlinePolicy',
          reason: 'SEC-005: GatewayServiceRole uses an inline policy to keep the exact N target-ARN allow-list visible on the role itself (layer-3 enforcement of the three-layer model).',
        },
        // SEC-029: CDK custom-resources Provider framework internals for the
        // IAM-propagation gate (CrPropGate). The waiter Step Function and the
        // framework onEvent/isComplete/onTimeout Lambda roles are
        // framework-generated and reference each other with function-arn
        // version wildcards (<arn>:*), without ALL-events logging or X-Ray.
        // Not authorable without forking the framework; provisioning-only.
        { id: 'AwsSolutions-SF1', reason: 'SEC-029: CDK Provider framework waiter Step Function (IAM-propagation gate); logging config is framework-owned.' },
        { id: 'AwsSolutions-SF2', reason: 'SEC-029: CDK Provider framework waiter Step Function; X-Ray is framework-owned.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<CrPropGateIsComplete083CF04D.Arn>:*'], reason: 'SEC-029: Provider framework inter-Lambda invoke version wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<CrPropGateOnEventB36FE5AB.Arn>:*'], reason: 'SEC-029: Provider framework inter-Lambda invoke version wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<CrPropGateProviderframeworkisCompleteDF39D816.Arn>:*'], reason: 'SEC-029: Provider framework waiter → isComplete invoke version wildcard.' },
        { id: 'AwsSolutions-IAM5', appliesTo: ['Resource::<CrPropGateProviderframeworkonTimeout9DAB4B92.Arn>:*'], reason: 'SEC-029: Provider framework waiter → onTimeout invoke version wildcard.' },
      ],
      true,
    );
  }
}

/**
 * Internal shim — builds the physical-resource-id reference the AWS SDK custom
 * resource expects for `onDelete.parameters.*Identifier` lookups. We just pass
 * the stable physical id string (same value used in `PhysicalResourceId.of`)
 * because the onCreate call uses a stable id; AgentCore's gateway-identifier
 * is itself the returned gatewayId, which is retained as the physical id.
 *
 * Kept as a class (not an inline string) so future refactors can swap in
 * `PhysicalResourceIdReference.fromAttribute(...)` without churn across
 * three call sites.
 */
/**
 * Inline handler for the AgentCore CR IAM-propagation gate. `onEvent` stamps a
 * completion deadline (now + ~30s) into the physical id; `isComplete` reports
 * done once that deadline passes. This deterministically delays the first
 * AgentCore mutate call until the shared CR role's inline policy has
 * propagated (live-verified: the same policy authorizes CreateGateway after a
 * short propagation window but is denied if called immediately).
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

class PhysicalResourceIdReferenceShim {
  readonly value: string;
  constructor(id: string) {
    this.value = id;
  }
}

/**
 * Inline Lambda handler — deploy-time AgentCore Registry record validator.
 *
 * Calls `bedrock-agentcore-control:GetRegistryRecord` for the (registryId,
 * recordId) pair passed in `ResourceProperties`, asserts that the record's
 * `status` is exactly `APPROVED`, and returns the metadata fields the
 * workstream Gateway stack pins as CFN attribute tokens.
 *
 * Failure modes (each surfaces an actionable error to the developer in the
 * CFN event log):
 *   - record does not exist            → "registry record <id> not found"
 *   - status !== 'APPROVED'            → "registry record <id> has status <X>; only APPROVED records may be subscribed"
 *   - metadata.gatewayTargetArn missing→ "registry record <id> is missing metadata.gatewayTargetArn"
 *
 * Idempotent across CFN event types — Create/Update both validate; Delete
 * is a no-op (returns Status=SUCCESS) so stack rollback completes cleanly.
 */
const REGISTRY_RECORD_VALIDATOR_HANDLER = `
// AgentCore control-plane REST shape:
//   GET https://bedrock-agentcore-control.<region>.amazonaws.com/registries/<rid>/records/<recId>
// Signing service: bedrock-agentcore (not -control). Built-in SigV4 below
// avoids any @aws-sdk dependency — Node 20 Lambda runtimes do not ship the
// preview AgentCore client and we must not bundle node_modules in an inline
// handler.
const https = require('https');
const crypto = require('crypto');

function hmac(key, str) { return crypto.createHmac('sha256', key).update(str, 'utf8').digest(); }
function hash(str) { return crypto.createHash('sha256').update(str, 'utf8').digest('hex'); }

// Self-contained STS AssumeRole call. The Lambda's execution role has
// sts:AssumeRole on the cross-account RegistryReader role; we sign an
// AssumeRole call with the execution role's task creds and capture the
// returned temporary creds for use against bedrock-agentcore-control.
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
    RoleSessionName: 'agenticai-registry-validator',
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
  const service = 'bedrock-agentcore';
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
  const { registryId, recordId, tenantId, agentId } = props;
  if (event.RequestType === 'Delete') {
    // No-op delete — the workstream stack never owns the record.
    return { PhysicalResourceId: event.PhysicalResourceId || ('reg-validator-' + recordId) };
  }
  if (!registryId || !recordId) {
    throw new Error('RegistryRecordValidator: missing registryId or recordId in ResourceProperties');
  }
  const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION;
  const host = 'bedrock-agentcore-control.' + region + '.amazonaws.com';
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
  // Metadata extraction. The AgentCore preview Registry stores opaque
  // server JSON inside descriptors.mcp.server.inlineContent. We embed
  // platform metadata (gatewayTargetArn, cedarPolicy, ownerTeam, costCentre)
  // under an 'agenticai' key in that JSON at seed time. We accept three
  // shapes for forward/backwards compatibility:
  //   1. descriptors.mcp.server.inlineContent (string) → JSON.parse → agenticai sub-object (preview shape)
  //   2. descriptors.mcp.server.metadata (object)      → direct (future schema)
  //   3. descriptors[0].metadata (object)              → legacy schema
  let metadata = {};
  if (parsed.descriptors && parsed.descriptors.mcp && parsed.descriptors.mcp.server) {
    const srv = parsed.descriptors.mcp.server;
    if (typeof srv.inlineContent === 'string') {
      try {
        const inline = JSON.parse(srv.inlineContent);
        if (inline && typeof inline === 'object' && inline.agenticai && typeof inline.agenticai === 'object') {
          metadata = inline.agenticai;
        } else if (inline && typeof inline === 'object' && inline.metadata && typeof inline.metadata === 'object') {
          metadata = inline.metadata;
        }
      } catch (e) { /* fall through */ }
    }
    if (Object.keys(metadata).length === 0 && srv.metadata && typeof srv.metadata === 'object') {
      metadata = srv.metadata;
    }
  }
  if (Object.keys(metadata).length === 0 && Array.isArray(parsed.descriptors) && parsed.descriptors[0] && parsed.descriptors[0].metadata) {
    metadata = parsed.descriptors[0].metadata;
  }
  const gatewayTargetArn = metadata.gatewayTargetArn;
  const cedarPolicy = metadata.cedarPolicy;
  if (!gatewayTargetArn || typeof gatewayTargetArn !== 'string') {
    throw new Error(
      'AgentCore Registry record \\'' + recordId + '\\' is missing metadata.gatewayTargetArn. ' +
      'Re-publish the record with the resolved Lambda alias ARN. (tenant=' + tenantId + ' agent=' + agentId + ')'
    );
  }
  if (!cedarPolicy || typeof cedarPolicy !== 'string') {
    throw new Error(
      'AgentCore Registry record \\'' + recordId + '\\' is missing metadata.cedarPolicy. ' +
      'Re-publish the record with the per-tool Cedar snippet. (tenant=' + tenantId + ' agent=' + agentId + ')'
    );
  }
  // Phase Q (v0.6.0): per-developer entitlement. allowedGroups, when present,
  // pins the Cognito group names whose JWTs may invoke this tool. The Gateway
  // stack uses it to (a) require CUSTOM_JWT mode and (b) bind the composed
  // Cedar bundle to principal-in-group permits. We serialise as JSON so it
  // travels through the CFN custom-resource Data map (string-only) safely.
  let allowedGroupsJson = '';
  let composedCedar = cedarPolicy;
  if (Array.isArray(metadata.allowedGroups) && metadata.allowedGroups.length > 0) {
    const groups = metadata.allowedGroups.filter(function (g) { return typeof g === 'string' && g.length > 0; });
    if (groups.length === 0) {
      throw new Error(
        'AgentCore Registry record \\'' + recordId + '\\' has metadata.allowedGroups but no valid string entries. ' +
        '(tenant=' + tenantId + ' agent=' + agentId + ')'
      );
    }
    if (process.env.GATEWAY_AUTHORIZER_MODE !== 'CUSTOM_JWT') {
      throw new Error(
        'AgentCore Registry record \\'' + recordId + '\\' carries metadata.allowedGroups but the workstream ' +
        'Gateway is configured with authorizerType=AWS_IAM. Per-developer entitlement requires CUSTOM_JWT — ' +
        'supply cognitoDiscoveryUrl on D03WorkstreamGatewayStackProps. (tenant=' + tenantId + ' agent=' + agentId + ')'
      );
    }
    allowedGroupsJson = JSON.stringify(groups);
    // Build principal-bound permits so the composed Cedar bundle binds the
    // tool to a Cognito group set rather than 'any authenticated principal'.
    const headerLine =
      '// Tool: ' + recordId + ' (Q-entitlement: principal-bound; only members of [' + groups.join(', ') + '] may invoke.)';
    const permits = groups.map(function (g) {
      return 'permit(principal in CognitoGroup::"' + g + '", action == Action::"InvokeTool", resource == Tool::"' + recordId + '");';
    }).join('\\n');
    composedCedar = headerLine + '\\n' + permits;
  }
  return {
    PhysicalResourceId: 'AgenticAI-RegistryValidator-' + tenantId + '-' + agentId + '-' + recordId,
    Data: {
      gatewayTargetArn,
      cedarPolicy: composedCedar,
      ownerTeam: metadata.ownerTeam || '',
      costCentre: metadata.costCentre || '',
      allowedGroups: allowedGroupsJson,
      status,
    },
  };
};
`;
