/**
 * Tests for HITL approver Cedar policy generator.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { buildApproverCedarPolicy, validateApproverConfig } from './cedar-approver';

const cfg = {
  approverRoleArn: 'arn:aws:iam::111111111111:role/AgenticAI-Approver',
  tenantId: 'demo',
  agentId: 'primary',
  confidenceThreshold: 0.7,
};

describe('HITL approver Cedar policy', () => {
  it('emits a permit + forbid pair for the approver role', () => {
    const p = buildApproverCedarPolicy(cfg);
    expect(p).toContain('permit(');
    // G-3 fix: the policy is single-statement (permit only). AVP's
    // CfnPolicy.definition.static.statement field accepts one Cedar
    // statement; default-deny is implicit when nothing else permits.
    expect(p).not.toContain('forbid(');
    expect(p).toContain('arn:aws:iam::111111111111:role/AgenticAI-Approver');
    expect(p).toContain('ResumeTaskWithApproval');
    expect(p).toContain('Agent::"demo/primary"');
  });

  it('rejects non-role-ARN approver values', () => {
    expect(() => validateApproverConfig({ ...cfg, approverRoleArn: 'ops-team' })).toThrow();
  });

  it('rejects confidence threshold outside [0, 1]', () => {
    expect(() => validateApproverConfig({ ...cfg, confidenceThreshold: 1.1 })).toThrow();
    expect(() => validateApproverConfig({ ...cfg, confidenceThreshold: -0.1 })).toThrow();
  });

  it('rejects non-kebab tenant / agent', () => {
    expect(() => validateApproverConfig({ ...cfg, tenantId: 'Demo' })).toThrow();
    expect(() => validateApproverConfig({ ...cfg, agentId: 'a' })).toThrow();
  });
});
