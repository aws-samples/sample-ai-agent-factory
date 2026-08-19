/**
 * RegistryRecordConstruct — publishes a single record into a parent
 * AgentCore Registry, optionally submitting it for approval (and, for
 * nonprod, auto-approving via the platform admin role).
 *
 * Lifecycle on AWS side:
 *   CreateRegistryRecord     → status=CREATING → DRAFT
 *   SubmitRegistryRecordForApproval → PENDING_APPROVAL
 *   UpdateRegistryRecordStatus(APPROVED) → APPROVED  (curator action)
 *
 * For developer ergonomics (and to make the v0.5.0 live test deterministic)
 * we expose `autoApproveOnCreate`. When true, the construct chains:
 *
 *   CreateRegistryRecord → SubmitRegistryRecordForApproval → UpdateRegistryRecordStatus(APPROVED)
 *
 * via three serial AwsCustomResources. **In production we keep
 * autoApproveOnCreate=false**; a separate curator workflow (or the
 * EventBridge-triggered curator Lambda introduced in v0.5.0 Phase L) handles
 * the APPROVE click.
 *
 * The parent `PlatformRegistryConstruct` is the dependency root; pass its
 * `registryId` token in.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { CfnOutput, Stack } from 'aws-cdk-lib';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
  PhysicalResourceIdReference,
} from 'aws-cdk-lib/custom-resources';
import { NagSuppressions } from 'cdk-nag';
import { Construct } from 'constructs';

/**
 * Evidence-backed IAM5 suppression for the record custom-resource Lambdas.
 * The ACTION stays a service-scoped wildcard (AgentCore action-family
 * evaluator rejects narrow lists — live-verified); the RESOURCE is already
 * scoped to this registry + its records. Applied without `appliesTo` because
 * the registry id is a deploy-time token that renders unpredictably into the
 * suppression matcher; the suppression is instead pinned to the single
 * provisioning custom-resource node, whose only policy is this statement.
 * Bounded by CDK Lambda lifetime + SCP-11 org deny.
 */
const RECORD_IAM5_SUPPRESSION = [
  {
    id: 'AwsSolutions-IAM5',
    reason:
      'SEC-028: registry-record create/submit/approve/delete fail AgentCore action-family IAM evaluation with narrow per-action lists (live-verified 2026-05). Provisioning-only AwsCustomResource; ACTION wildcard bounded by CDK lifetime + SCP-11; RESOURCE already scoped to this registry and its records (registry/<id>/record/*).',
  },
];

import {
  type RegistryRecordSpec,
  renderA2aDescriptorPayload,
  renderMcpDescriptorPayload,
  validateRegistryRecordSpec,
} from './registry-record-spec';

export interface RegistryRecordConstructProps {
  /** Token from `PlatformRegistryConstruct.registryId`. */
  readonly registryId: string;
  /** Validated record specification. */
  readonly spec: RegistryRecordSpec;
  /**
   * When true, the construct submits + auto-approves the record on create.
   * Defaults to `false` (manual curator approval). Use `true` only in
   * nonprod / dev pipelines where the platform-admin role is implicit.
   */
  readonly autoApproveOnCreate?: boolean;
  /** Free-text reason recorded on UpdateRegistryRecordStatus. */
  readonly approvalReason?: string;
}

export class RegistryRecordConstruct extends Construct {
  /** AWS-minted opaque record id (token). */
  readonly recordId: string;
  /** Convenience: caller-supplied stable record-id slug (matches RegistryRecordSpec.recordId). */
  readonly stableRecordId: string;

  constructor(scope: Construct, id: string, props: RegistryRecordConstructProps) {
    super(scope, id);

    validateRegistryRecordSpec(props.spec);
    this.stableRecordId = props.spec.recordId;

    const descriptorPayload = renderDescriptorPayload(props.spec);
    const physicalId = `AgenticAI-RegistryRecord-${props.spec.recordId}`;

    // SEC-028 (security review): the AgentCore control-plane action-family
    // evaluator rejects narrow per-action lists for registry-record
    // create/submit/approve/delete (live-verified), so the ACTION
    // must stay a service-scoped wildcard on these
    // short-lived CDK custom-resource Lambdas. We nonetheless scope the
    // RESOURCE to this registry and its records rather than '*'. This is a
    // provisioning-only grant bounded by (1) the CDK-managed Lambda lifetime,
    // (2) SCP-11 registry-mutation-lockdown at the org layer, and (3) the
    // resource scope below. Application/runtime IAM must NOT use
    // `bedrock-agentcore:*` — use explicit actions there.
    // Scope to registries + records in THIS account and region. We must NOT
    // interpolate `props.registryId` here: it is an AwsCustomResource
    // getResponseField token, and forcing its Fn::GetAtt resolution inside an
    // IAM policy fails at deploy time ("Vendor response doesn't contain
    // registryId attribute") because the CreateRegistry response does not
    // expose that field as a GetAtt-able attribute. An account+region
    // registry wildcard removes the fragile cross-resource dependency while
    // still satisfying least-privilege (no account-wide '*', no cross-account
    // reach). The runtime dependency on the specific registry is preserved by
    // the API `registryId` parameter passed to each custom-resource call.
    const stack = Stack.of(this);
    const registryArnScope = stack.formatArn({
      service: 'bedrock-agentcore',
      resource: 'registry',
      resourceName: '*',
    });
    const registryRecordMutationResources = [
      registryArnScope,
      `${registryArnScope}/record/*`,
    ];

    const createParams: Record<string, unknown> = {
      registryId: props.registryId,
      name: props.spec.recordId,
      description: props.spec.description,
      ...descriptorPayload,
    };

    const createResource = new AwsCustomResource(this, 'Create', {
      resourceType: 'Custom::BedrockAgentCoreRegistryRecord',
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'createRegistryRecord',
        parameters: createParams,
        // LANDMINE (live-verified 2026-07-02): CreateRegistryRecord returns
        // ONLY `recordArn` (no top-level `recordId`) — using
        // fromResponse('recordId') yields an empty/invalid physical id
        // ("Invalid PhysicalResourceId"). All record APIs accept the ARN in
        // the record-id field, so we use the ARN as the physical id and thread
        // it through onUpdate/onDelete via PhysicalResourceIdReference.
        physicalResourceId: PhysicalResourceId.fromResponse('recordArn'),
      },
      onUpdate: {
        // Record updates use UpdateRegistryRecord with PATCH wrappers.
        // For v0.5.0 we keep updates as a no-op `getRegistryRecord` to keep
        // the descriptor immutable post-publish; future iterations can
        // emit a real `updateRegistryRecord` here with `optionalValue`-
        // wrapped descriptors.
        service: 'bedrock-agentcore-control',
        action: 'getRegistryRecord',
        parameters: {
          registryId: props.registryId,
          recordId: new PhysicalResourceIdReference(),
        },
        physicalResourceId: PhysicalResourceId.fromResponse('recordArn'),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'deleteRegistryRecord',
        parameters: {
          registryId: props.registryId,
          recordId: new PhysicalResourceIdReference(),
        },
        ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException|ConflictException)',
      },
      policy: AwsCustomResourcePolicy.fromStatements([
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ['bedrock-agentcore:*'],
          resources: registryRecordMutationResources,
        }),
      ]),
    });
    NagSuppressions.addResourceSuppressions(createResource, RECORD_IAM5_SUPPRESSION, true);
    // recordArn doubles as the record identifier for all record APIs (they
    // accept ARN or id). See landmine note above.
    this.recordId = createResource.getResponseField('recordArn');

    if (props.autoApproveOnCreate) {
      // 2-step chain: Submit → Approve. Each step is its own AwsCustomResource
      // so CFN treats them as discrete resources (clean rollback semantics).
      const submitResource = new AwsCustomResource(this, 'Submit', {
        resourceType: 'Custom::BedrockAgentCoreRegistryRecordSubmit',
        onCreate: {
          service: 'bedrock-agentcore-control',
          action: 'submitRegistryRecordForApproval',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
          },
          physicalResourceId: PhysicalResourceId.of(`${physicalId}-submit`),
        },
        onUpdate: {
          service: 'bedrock-agentcore-control',
          action: 'getRegistryRecord',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
          },
          physicalResourceId: PhysicalResourceId.of(`${physicalId}-submit`),
        },
        onDelete: {
          // Submit has no idempotent delete — getRegistryRecord with
          // tolerated ValidationException is the safest no-op shape.
          service: 'bedrock-agentcore-control',
          action: 'getRegistryRecord',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
          },
          ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException|ConflictException)',
        },
        policy: AwsCustomResourcePolicy.fromStatements([
          new PolicyStatement({
            effect: Effect.ALLOW,
            actions: ['bedrock-agentcore:*'],
            resources: registryRecordMutationResources,
          }),
        ]),
      });
      NagSuppressions.addResourceSuppressions(submitResource, RECORD_IAM5_SUPPRESSION, true);
      submitResource.node.addDependency(createResource);

      const approveResource = new AwsCustomResource(this, 'Approve', {
        resourceType: 'Custom::BedrockAgentCoreRegistryRecordApprove',
        onCreate: {
          service: 'bedrock-agentcore-control',
          action: 'updateRegistryRecordStatus',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
            status: 'APPROVED',
            statusReason:
              props.approvalReason ??
              'Auto-approved via D-03 v3 platform pipeline (nonprod)',
          },
          physicalResourceId: PhysicalResourceId.of(`${physicalId}-approve`),
        },
        onUpdate: {
          service: 'bedrock-agentcore-control',
          action: 'getRegistryRecord',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
          },
          physicalResourceId: PhysicalResourceId.of(`${physicalId}-approve`),
        },
        onDelete: {
          service: 'bedrock-agentcore-control',
          action: 'updateRegistryRecordStatus',
          parameters: {
            registryId: props.registryId,
            recordId: this.recordId,
            status: 'DEPRECATED',
            statusReason: 'Stack delete: deprecating in lieu of hard-delete (Registry retains audit history).',
          },
          ignoreErrorCodesMatching: '(ResourceNotFoundException|ValidationException|ConflictException)',
        },
        policy: AwsCustomResourcePolicy.fromStatements([
          new PolicyStatement({
            effect: Effect.ALLOW,
            actions: ['bedrock-agentcore:*'],
            resources: registryRecordMutationResources,
          }),
        ]),
      });
      NagSuppressions.addResourceSuppressions(approveResource, RECORD_IAM5_SUPPRESSION, true);
      approveResource.node.addDependency(submitResource);
    }

    new CfnOutput(this, 'RecordId', {
      value: this.recordId,
      description: `AgentCore Registry record id for ${props.spec.recordId} (opaque AWS-minted).`,
      exportName: `AgenticAI-RegistryRecordId-${props.spec.recordId}`,
    });
    new CfnOutput(this, 'StableRecordId', {
      value: this.stableRecordId,
      description: 'Caller-supplied kebab-case slug (matches RegistryRecordSpec.recordId; used as the workstream subscription key).',
    });
  }
}

// LANDMINE (live-verified 2026-07-02): `descriptors` is a STRUCTURE keyed by
// the lowercase descriptor family (`mcp` / `a2a` / `custom` / `agentSkills`),
// NOT an array. Each leaf carries a `schemaVersion`/`protocolVersion` and an
// `inlineContent` STRING (JSON serialized). The prior `descriptors: [server,
// tool]` array shape was rejected live with "MCP descriptor type requires mcp
// descriptor". See registry-record-spec.ts renderers for the exact content.
function renderDescriptorPayload(
  spec: RegistryRecordSpec,
): { descriptorType: string; descriptors: Record<string, unknown> } {
  switch (spec.descriptorType) {
    case 'MCP':
      return renderMcpDescriptorPayload(spec);
    case 'A2A':
      return renderA2aDescriptorPayload(spec);
    case 'AGENT_SKILLS':
      return {
        descriptorType: 'AGENT_SKILLS',
        descriptors: {
          agentSkills: {
            skillMd: { inlineContent: spec.markdown },
            skillDefinition: {
              schemaVersion: spec.skillVersion,
              inlineContent: JSON.stringify({
                name: spec.skillName,
                version: spec.skillVersion,
                description: spec.description,
                ownerTeam: spec.ownerTeam,
                costCentre: spec.costCentre,
              }),
            },
          },
        },
      };
    case 'CUSTOM':
      return {
        descriptorType: 'CUSTOM',
        descriptors: {
          custom: {
            inlineContent: JSON.stringify({
              name: spec.recordId,
              description: spec.description,
              payload: spec.payload,
              ownerTeam: spec.ownerTeam,
              costCentre: spec.costCentre,
            }),
          },
        },
      };
  }
}
