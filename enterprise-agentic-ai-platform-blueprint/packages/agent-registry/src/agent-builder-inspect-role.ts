/**
 * AgentBuilderInspectRole — sanctioned control-plane surface #2 of 3.
 *
 * A read-only role a builder experience assumes to show a customer the real
 * state of their agents. Per the target-state architecture (§9 surface table):
 *
 *   Direction : Builder → Platform and Workstream
 *   Authority : Describe and list only. NO mutation of any kind.
 *
 * This construct is deliberately minimal and auditable: it grants only
 * describe/list/get actions against the Registry (and its records) and the
 * AgentCore Runtime/Memory read surface, scoped to exact resource ARNs — never
 * a wildcard resource, never a create/update/delete/put/tag action. The
 * adversarial catalog asserts both that this role CAN read workstream state and
 * that it CANNOT mutate it; keeping the action list strictly read-only is what
 * makes the "cannot mutate" twin fail closed.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { Construct } from 'constructs';
import {
  Effect,
  IPrincipal,
  PolicyStatement,
  Role,
} from 'aws-cdk-lib/aws-iam';

export interface AgentBuilderInspectRoleProps {
  /** Registry ARN the builder may inspect. Records are scoped to `${arn}/record/*`. */
  readonly registryArn: string;
  /**
   * The principal (a builder-experience service role or Identity Center
   * permission set role) permitted to assume this inspection role.
   */
  readonly trustedPrincipal: IPrincipal;
  /** Explicit role name so SCPs and trust policies can reference it exactly. */
  readonly roleName: string;
  /**
   * Optional exact AgentCore Runtime ARNs the builder may describe. When
   * omitted, only the Registry read surface is granted. Never a wildcard.
   */
  readonly runtimeArns?: readonly string[];
}

/**
 * Read-only actions this role is allowed to hold. Any action NOT in this set —
 * in particular every create/update/delete/put/tag/associate/approve verb — is
 * a contract violation and must never be added here. Kept as an exported
 * constant so a conformance test can assert the role's rendered policy contains
 * only these actions.
 */
export const AGENT_BUILDER_INSPECT_ACTIONS: readonly string[] = [
  // Registry (GA control-plane read surface).
  'agent-registry:GetRegistry',
  'agent-registry:ListRegistryRecords',
  'agent-registry:GetRegistryRecord',
  'agent-registry:GetDiscoverableRegistryRecord',
  'agent-registry:ListDiscoverableRegistryRecords',
  'agent-registry:ListTagsForResource',
];

/** AgentCore Runtime read-only actions, granted only when runtimeArns given. */
export const AGENT_BUILDER_INSPECT_RUNTIME_ACTIONS: readonly string[] = [
  'bedrock-agentcore:GetAgentRuntime',
  'bedrock-agentcore:ListAgentRuntimes',
];

/**
 * Any action fragment that, if present in this role, means the read-only
 * contract has been broken. Exported so tests can assert absence.
 */
export const AGENT_BUILDER_INSPECT_FORBIDDEN_FRAGMENTS: readonly string[] = [
  'Create',
  'Update',
  'Delete',
  'Put',
  'Untag',
  'Associate',
  'Disassociate',
  'Approve',
  'Submit',
  'Invoke',
];

/** Operation-name prefixes that are unambiguously read-only. */
const READ_ONLY_VERB_PREFIXES: readonly string[] = [
  'Get',
  'List',
  'Describe',
  'Search',
  'BatchGet',
];

/**
 * Assert an IAM action is read-only by inspecting its operation verb (the part
 * after the `service:`), not a naive substring — otherwise a legitimate read
 * such as `ListTagsForResource` (which contains "Tag") would false-positive.
 */
export function assertReadOnlyAction(action: string): void {
  const operation = action.includes(':') ? action.slice(action.indexOf(':') + 1) : action;
  const isRead = READ_ONLY_VERB_PREFIXES.some((prefix) => operation.startsWith(prefix));
  if (!isRead) {
    throw new Error(
      `AgentBuilderInspectRole: action '${action}' is not a read-only operation ` +
        `(must start with one of ${READ_ONLY_VERB_PREFIXES.join(', ')}).`,
    );
  }
}

export class AgentBuilderInspectRole extends Construct {
  readonly role: Role;

  constructor(scope: Construct, id: string, props: AgentBuilderInspectRoleProps) {
    super(scope, id);

    if (!props.registryArn || props.registryArn.includes('*')) {
      throw new Error(
        'AgentBuilderInspectRole: registryArn must be an exact ARN (no wildcard).',
      );
    }
    for (const action of AGENT_BUILDER_INSPECT_ACTIONS) {
      assertReadOnlyAction(action);
    }

    this.role = new Role(this, 'Role', {
      roleName: props.roleName,
      assumedBy: props.trustedPrincipal,
      description:
        'Read-only builder inspection of agent state (sanctioned surface #2). Describe/list only.',
    });

    const recordArn = `${props.registryArn}/record/*`;
    this.role.addToPrincipalPolicy(
      new PolicyStatement({
        sid: 'AgentBuilderInspectRegistryRead',
        effect: Effect.ALLOW,
        actions: [...AGENT_BUILDER_INSPECT_ACTIONS],
        resources: [props.registryArn, recordArn],
      }),
    );

    if (props.runtimeArns && props.runtimeArns.length > 0) {
      for (const arn of props.runtimeArns) {
        if (arn.includes('*')) {
          throw new Error(
            'AgentBuilderInspectRole: runtimeArns must be exact ARNs (no wildcard).',
          );
        }
      }
      this.role.addToPrincipalPolicy(
        new PolicyStatement({
          sid: 'AgentBuilderInspectRuntimeRead',
          effect: Effect.ALLOW,
          actions: [...AGENT_BUILDER_INSPECT_RUNTIME_ACTIONS],
          resources: [...props.runtimeArns],
        }),
      );
    }
  }
}
