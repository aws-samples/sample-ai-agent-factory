/**
 * Cedar policy snippet for HITL approver scope.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-5 (authorisation half).
 *
 * The HITL state machine pauses execution and emits a token. Resuming the
 * task requires `Action::"ResumeTaskWithApproval"`. Cedar permits ONLY the
 * configured `approverRole` to invoke that action; everyone else is denied
 * by the default forbid in the Gateway policy document.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface ApproverConfig {
  readonly approverRoleArn: string;        // IAM role of the approver group
  readonly tenantId: string;
  readonly agentId: string;
  /** Confidence floor below which the agent must escalate. */
  readonly confidenceThreshold: number;     // [0, 1]
}

const ARN_REGEX = /^arn:aws:iam::\d{12}:role\/[a-zA-Z0-9+=,.@_-]{1,64}$/;

export function validateApproverConfig(c: ApproverConfig): void {
  if (!ARN_REGEX.test(c.approverRoleArn)) {
    throw new Error(`approverRoleArn is not an IAM role ARN: ${c.approverRoleArn}`);
  }
  if (c.confidenceThreshold < 0 || c.confidenceThreshold > 1) {
    throw new Error(`confidenceThreshold must be in [0, 1]; got ${c.confidenceThreshold}`);
  }
  if (!/^[a-z0-9-]{3,64}$/.test(c.tenantId)) {
    throw new Error(`tenantId must be kebab-case 3-64 chars: ${c.tenantId}`);
  }
  if (!/^[a-z0-9-]{3,64}$/.test(c.agentId)) {
    throw new Error(`agentId must be kebab-case 3-64 chars: ${c.agentId}`);
  }
}

export function buildApproverCedarPolicy(c: ApproverConfig): string {
  validateApproverConfig(c);
  // AVP requires the schema namespace prefix on every entity type and
  // action. The HITL policy store schema declares the `AgenticAI`
  // namespace; entity types are `AgenticAI::Role` and `AgenticAI::Agent`,
  // action is `AgenticAI::Action::"ResumeTaskWithApproval"`.
  // Default-deny is implicit when no permit matches.
  return [
    `// HITL approver scope - only this role can resume the task`,
    `permit(`,
    `  principal == AgenticAI::Role::"${c.approverRoleArn}",`,
    `  action == AgenticAI::Action::"ResumeTaskWithApproval",`,
    `  resource == AgenticAI::Agent::"${c.tenantId}/${c.agentId}"`,
    `);`,
  ].join('\n');
}
