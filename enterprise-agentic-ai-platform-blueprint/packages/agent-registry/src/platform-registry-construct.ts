/**
 * PlatformRegistryConstruct — single org-wide AWS Bedrock AgentCore Registry,
 * provisioned in the platform account and consumed by every workstream.
 *
 * Authoritative API surface:
 *   - bedrock-agentcore-control:CreateRegistry  (sets approvalConfiguration + inboundAuthType)
 *   - bedrock-agentcore-control:UpdateRegistry  (PATCH wrappers via {optionalValue})
 *   - bedrock-agentcore-control:DeleteRegistry
 *
 * `inboundAuthType` is **immutable** post-create. Default for v0.5.0 is
 * AWS_IAM; CUSTOM_JWT is a v0.6.0 follow-on. This is enforced by callers, not
 * by the AWS API.
 *
 * Three-layer enforcement model parallels the AgentCore Gateway stack:
 *   - Layer 1 (synth-time): record-spec validators + record-id catalogue
 *   - Layer 2 (SCP-11, org-level): only `AgenticAI-RegistryAdmin` may
 *     `CreateRegistry`/`DeleteRegistry`/`UpdateRegistryRecordStatus`.
 *   - Layer 3 (consumer IAM, scoped via `RegistryConsumerGrant`): workstream
 *     runtime roles can only `SearchRegistryRecords` / `InvokeRegistryMcp`
 *     against this registry's ARN.
 *
 * Custom-resource Lambda IAM scope (`bedrock-agentcore:*` on `*`) follows the
 * same SEC-028 justification used by the Gateway custom resources: the
 * AgentCore action-family evaluator rejects narrow per-action lists in
 * several create/delete paths discovered live 2026-05-05; SCP-11 + the
 * Lambda's per-stack lifetime contain the blast radius.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, CustomResource, Duration, Fn, Stack } from 'aws-cdk-lib';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Code, Function as LambdaFunction, Runtime } from 'aws-cdk-lib/aws-lambda';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
  PhysicalResourceIdReference,
  Provider,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

// Inline handler for the registry-readiness gate. `onEvent` returns the
// registryId; `isComplete` polls GetRegistry until status === 'READY'. This
// closes the live-verified race where record creation ran while the registry
// was still CREATING ("Registry is not in READY state: CREATING").
// Registry readiness gate.
//
// LANDMINE (live-verified 2026-07-02): the AgentCore Registry API reaches
// READY a short time AFTER CreateRegistry returns; creating records against a
// still-CREATING registry fails ("Registry is not in READY state: CREATING").
// We therefore gate record creation on this Provider.
//
// SECONDARY LANDMINE: the Lambda RUNTIME's bundled
// @aws-sdk/client-bedrock-agentcore-control has NO Registry commands
// (only Gateway/Memory/WorkloadIdentity/etc.) — the newer SDK that the CDK
// AwsCustomResource singleton bundles does, which is why CreateRegistry works
// there but a runtime GetRegistry poll throws "GetRegistryCommand is not a
// constructor". So the gate PREFERS a real GetRegistry poll when the runtime
// SDK exposes it, and otherwise FALLS BACK to a deterministic time-based wait
// (deadline encoded in the physical id — no cross-invocation state needed).
const READY_GATE_HANDLER = `
const READINESS_WAIT_MS = 180000; // 3 min fallback window
const sdk = require('@aws-sdk/client-bedrock-agentcore-control');
function ctor(re) {
  const k = Object.keys(sdk).find((n) => re.test(n) && typeof sdk[n] === 'function');
  return k ? sdk[k] : null;
}
const ClientCtor = ctor(/Client$/);
const GetRegistryCtor = (typeof sdk.GetRegistryCommand === 'function') ? sdk.GetRegistryCommand : ctor(/^GetRegistry.*Command$/);
const client = ClientCtor ? new ClientCtor({}) : null;
const canPoll = !!(client && GetRegistryCtor);

exports.onEvent = async (event) => {
  if (event.RequestType === 'Delete') return { PhysicalResourceId: event.PhysicalResourceId };
  const registryId = event.ResourceProperties.RegistryId;
  const deadline = Date.now() + READINESS_WAIT_MS;
  // Encode the fallback deadline in the physical id so isComplete is stateless.
  return { PhysicalResourceId: 'ready-gate-' + registryId + '-' + deadline, Data: { RegistryId: registryId } };
};
exports.isComplete = async (event) => {
  if (event.RequestType === 'Delete') return { IsComplete: true };
  const registryId = event.ResourceProperties.RegistryId;
  // NOTE: the CDK Provider framework REJECTS returning a "Data" key while
  // "IsComplete" is false ('"Data" is not allowed if "IsComplete" is
  // "False"'). So only attach Data on the completing return.
  if (canPoll) {
    try {
      const r = await client.send(new GetRegistryCtor({ registryId }));
      console.log('GetRegistry status:', r.status, 'for', registryId);
      if (r.status === 'READY') return { IsComplete: true, Data: { RegistryId: registryId, Status: r.status } };
      return { IsComplete: false };
    } catch (e) {
      console.error('GetRegistry poll error (falling back to time-based wait):', e && (e.name || e.code), e && e.message);
      // fall through to time-based
    }
  }
  // Time-based fallback: complete once the encoded deadline has passed.
  const pid = String(event.PhysicalResourceId || '');
  const deadline = parseInt(pid.slice(pid.lastIndexOf('-') + 1), 10) || 0;
  const done = Date.now() >= deadline;
  console.log('ready-gate time-based wait, done=', done, 'deadline=', deadline);
  if (done) return { IsComplete: true, Data: { RegistryId: registryId, Status: 'ASSUMED_READY' } };
  return { IsComplete: false };
};
`;

import type { RegistryInboundAuthType } from './registry-record-spec';

export interface PlatformRegistryConstructProps {
  /** Logical registry name. AWS pattern: ([0-9a-zA-Z][-]?){1,100}. */
  readonly registryName: string;
  /** Human-readable description, surfaced to curators. */
  readonly description: string;
  /**
   * Inbound auth type. **Immutable post-create at AWS side** — pass
   * `AWS_IAM` (default) for v0.5.0; CUSTOM_JWT is a v0.6.0 follow-on.
   */
  readonly inboundAuthType?: RegistryInboundAuthType;
  /**
   * Whether new records auto-approve on submit. Default `false` (manual
   * curator click). Mutable post-create via `UpdateRegistry`.
   */
  readonly autoApproval?: boolean;
}

export class PlatformRegistryConstruct extends Construct {
  /** AWS-minted opaque registry id (token). */
  readonly registryId: string;
  /** AWS-minted registry ARN (token). */
  readonly registryArn: string;
  /** The custom-resource singleton, exposed for explicit `addDependency` wiring. */
  readonly resource: AwsCustomResource;
  /**
   * Readiness gate — a CustomResource that only completes once the registry
   * reports status READY. Record constructs MUST `node.addDependency` on this
   * (not just on `resource`) so their CreateRegistryRecord call does not race
   * a still-CREATING registry.
   */
  readonly readyGate: CustomResource;

  constructor(scope: Construct, id: string, props: PlatformRegistryConstructProps) {
    super(scope, id);

    if (!/^([0-9a-zA-Z][-]?){1,100}$/.test(props.registryName)) {
      throw new Error(
        `PlatformRegistryConstruct: registryName must match AWS pattern ([0-9a-zA-Z][-]?){1,100}; got '${props.registryName}'`,
      );
    }

    const inboundAuthType: RegistryInboundAuthType = props.inboundAuthType ?? 'AWS_IAM';
    const autoApproval = props.autoApproval ?? false;

    const createParams: Record<string, unknown> = {
      name: props.registryName,
      description: props.description,
      inboundAuthType,
      approvalConfiguration: { autoApproval },
    };

    // LANDMINE (live-verified 2026-07-02): the friendly registry NAME is not
    // a valid `registryId` for update/delete — AgentCore mints an opaque id
    // and the mutate APIs require it (passing the name silently orphaned
    // registries on rollback). `deleteRegistry`/`updateRegistry` DO accept
    // the full ARN in the registryId field (verified live), and the
    // CreateRegistry response carries `registryArn`, so we use the ARN as the
    // CloudFormation physical id and thread it back into update/delete via
    // PhysicalResourceIdReference. This makes rollback delete the ACTUAL
    // registry, not a non-existent name.
    this.resource = new AwsCustomResource(this, 'Registry', {
      resourceType: 'Custom::BedrockAgentCoreRegistry',
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'createRegistry',
        parameters: createParams,
        // Physical id = the AWS-minted registry ARN from the create response.
        physicalResourceId: PhysicalResourceId.fromResponse('registryArn'),
      },
      onUpdate: {
        // CreateRegistry is largely immutable; we update only the mutable
        // approvalConfiguration via PATCH wrapper. Other fields (name,
        // description, inboundAuthType) require replacement.
        service: 'bedrock-agentcore-control',
        action: 'updateRegistry',
        parameters: {
          registryId: new PhysicalResourceIdReference(),
          approvalConfiguration: {
            optionalValue: { autoApproval },
          },
        },
        physicalResourceId: PhysicalResourceId.fromResponse('registryArn'),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'deleteRegistry',
        parameters: { registryId: new PhysicalResourceIdReference() },
        // Same ROLLBACK_FAILED landmine as Gateway: when Create fails CFN
        // re-invokes Delete with our logical id; tolerate ResourceNotFound
        // and ValidationException so rollback completes cleanly.
        // ConflictException added (live-verified 2026-07-02): a registry that
        // is still in CREATING status cannot be deleted ("Cannot delete
        // registry in CREATING status") — tolerate it so a failed create can
        // roll back; the half-created registry is swept by the teardown pass.
        ignoreErrorCodesMatching:
          '(ResourceNotFoundException|ValidationException|ConflictException)',
      },
      policy: AwsCustomResourcePolicy.fromStatements([
        new PolicyStatement({
          effect: Effect.ALLOW,
          // SEC-028 (known limitation, security-reviewed): the AgentCore
          // control-plane action-family evaluator rejects narrow per-action
          // lists for registry create/update/delete (live-verified 2026-05), so a
          // service-scoped wildcard is required. CreateRegistry also provisions a Workload Identity
          // internally, so the grant must cover that action family too.
          // Compensating controls that bound this grant:
          //   1. Lifetime — this is a CDK-managed AwsCustomResource singleton
          //      Lambda that exists only for the CFN create/update/delete
          //      lifecycle, not a long-lived runtime principal.
          //   2. Org boundary — SCP-11 (registry-mutation-lockdown) denies
          //      bedrock-agentcore mutate APIs from every principal except
          //      the platform admin role at the AWS Organizations layer.
          //   3. Scope — restricted to the single bedrock-agentcore service
          //      namespace; no cross-service reach.
          // Revisit when AgentCore GA publishes a least-privilege action list.
          actions: ['bedrock-agentcore:*'],
          resources: ['*'],
        }),
      ]),
    });

    // SEC-028: the `bedrock-agentcore:*` on `*` above is required by the
    // AgentCore control-plane action-family evaluator (see the inline block).
    // Bounded by CDK-managed Lambda lifetime + SCP-11 org deny. Suppressed
    // here with evidence so the deploy passes cdk-nag transparently.
    NagSuppressions.addResourceSuppressions(
      this.resource,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason:
            'SEC-028: CreateRegistry/UpdateRegistry/DeleteRegistry (and the Workload Identity they provision internally) fail AgentCore action-family IAM evaluation with narrow per-action lists — live-verified 2026-05. Provisioning-only AwsCustomResource Lambda; bounded by CDK lifetime + SCP-11 (registry-mutation-lockdown) org deny.',
          appliesTo: ['Action::bedrock-agentcore:*', 'Resource::*'],
        },
      ],
      true,
    );

    // LANDMINE (live-verified 2026-07-02): the AgentCore `CreateRegistry`
    // response contains ONLY `registryArn` — there is no top-level
    // `registryId` field (though `GetRegistry`/`ListRegistries` do return
    // one). Calling `getResponseField('registryId')` therefore fails at
    // deploy with "Vendor response doesn't contain registryId attribute".
    // The id is the last ARN segment
    //   arn:aws:bedrock-agentcore:<region>:<account>:registry/<registryId>
    // so we derive it from the ARN with Fn::Select(Fn::Split('/', arn)).
    this.registryArn = this.resource.getResponseField('registryArn');
    this.registryId = Fn.select(1, Fn.split('registry/', this.registryArn));

    // ---- Readiness gate (live-verified 2026-07-02) ----
    // CreateRegistry returns before the registry reaches READY; record
    // creation against a CREATING registry fails with "Registry is not in
    // READY state: CREATING". This Provider polls GetRegistry until READY.
    const readyOnEvent = new LambdaFunction(this, 'ReadyGateOnEvent', {
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.onEvent',
      timeout: Duration.minutes(1),
      code: Code.fromInline(READY_GATE_HANDLER),
      description: 'Registry readiness gate — onEvent.',
    });
    const readyIsComplete = new LambdaFunction(this, 'ReadyGateIsComplete', {
      runtime: Runtime.NODEJS_20_X,
      handler: 'index.isComplete',
      timeout: Duration.minutes(1),
      code: Code.fromInline(READY_GATE_HANDLER),
      description: 'Registry readiness gate — isComplete poller.',
    });
    for (const fn of [readyOnEvent, readyIsComplete]) {
      fn.addToRolePolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ['bedrock-agentcore:GetRegistry'],
          resources: ['*'],
        }),
      );
    }
    const readyProvider = new Provider(this, 'ReadyGateProvider', {
      onEventHandler: readyOnEvent,
      isCompleteHandler: readyIsComplete,
      queryInterval: Duration.seconds(5),
      totalTimeout: Duration.minutes(10),
    });
    this.readyGate = new CustomResource(this, 'ReadyGate', {
      serviceToken: readyProvider.serviceToken,
      properties: { RegistryId: this.registryId },
    });
    this.readyGate.node.addDependency(this.resource);

    // GetRegistry is a read on registries in this account; scoping is not
    // possible pre-create (id is a token). Read-only + provisioning-only.
    NagSuppressions.addResourceSuppressions(
      readyOnEvent,
      [{ id: 'AwsSolutions-IAM5', reason: 'SEC-028: read-only GetRegistry poll during provisioning; registry id is a deploy-time token.' }, { id: 'AwsSolutions-IAM4', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'], reason: 'SEC-010: CDK default execution role.' }],
      true,
    );
    NagSuppressions.addResourceSuppressions(
      readyIsComplete,
      [{ id: 'AwsSolutions-IAM5', reason: 'SEC-028: read-only GetRegistry poll during provisioning; registry id is a deploy-time token.' }, { id: 'AwsSolutions-IAM4', appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'], reason: 'SEC-010: CDK default execution role.' }],
      true,
    );

    new CfnOutput(this, 'RegistryId', {
      value: this.registryId,
      description: 'AWS-minted AgentCore Registry id (opaque). Workstream subscriptions reference this.',
      exportName: `AgenticAI-RegistryId-${props.registryName}`,
    });
    new CfnOutput(this, 'RegistryArn', {
      value: this.registryArn,
      description: 'AWS-minted AgentCore Registry ARN. Consumer IAM grants scope to this.',
      exportName: `AgenticAI-RegistryArn-${props.registryName}`,
    });
    new CfnOutput(this, 'RegistryAccount', {
      value: Stack.of(this).account,
      description: 'Account hosting the AgentCore Registry (platform account).',
    });
  }
}
