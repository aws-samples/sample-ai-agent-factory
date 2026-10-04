/**
 * Unit tests for the AgentRegistrationApi authorization core.
 *
 * Each test maps to an adversarial-catalog case (REG-07..REG-12) so the
 * sanctioned write path and its denials are pinned to the documented contract.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import {
  evaluateRegistrationRequest,
  type RegistrationPrincipal,
  type RegistrationRecordState,
} from './agent-registration-api';

const registrar: RegistrationPrincipal = {
  principalId: 'admin-1',
  role: 'registry_admin',
  tenantId: 'demo',
};
const approver: RegistrationPrincipal = {
  principalId: 'approver-1',
  role: 'registry_approver',
  tenantId: 'demo',
};
const workstream: RegistrationPrincipal = {
  principalId: 'ws-1',
  role: 'other',
  tenantId: 'demo',
};

const draftRecord: RegistrationRecordState = {
  recordId: 'rec-1',
  tenantId: 'demo',
  status: 'DRAFT',
  submittedBy: 'admin-1',
  references: {
    inferenceTargetArn: 'arn:target:v1',
    modelId: 'target-demo/openai.gpt-oss-120b',
    rateProfile: 'baseline-prod',
  },
};
const approvedRecord: RegistrationRecordState = {
  ...draftRecord,
  status: 'APPROVED',
};

describe('AgentRegistrationApi — create (REG-07)', () => {
  it('REG-07-P: registrar create is allowed', () => {
    const d = evaluateRegistrationRequest({ action: 'create', principal: registrar, tenantId: 'demo' });
    expect(d.decision).toBe('allow');
  });

  it('REG-07-N: create from a non-registrar identity is denied 403', () => {
    const d = evaluateRegistrationRequest({ action: 'create', principal: workstream, tenantId: 'demo' });
    expect(d.decision).toBe('deny');
    if (d.decision === 'deny') expect(d.httpStatus).toBe(403);
  });

  it('registrar cannot create in another tenant', () => {
    const d = evaluateRegistrationRequest({ action: 'create', principal: registrar, tenantId: 'other-tenant' });
    expect(d.decision).toBe('deny');
  });
});

describe('AgentRegistrationApi — update (REG-08, REG-12)', () => {
  it('REG-08-N: update from a read-only identity is denied 403', () => {
    const d = evaluateRegistrationRequest(
      { action: 'update', principal: { ...workstream, role: 'other' }, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('deny');
  });

  it('registrar update of a DRAFT record is allowed', () => {
    const d = evaluateRegistrationRequest(
      { action: 'update', principal: registrar, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('allow');
  });

  it('REG-12-N: changing an APPROVED record’s modelId without re-approval is denied 403', () => {
    const d = evaluateRegistrationRequest(
      {
        action: 'update',
        principal: registrar,
        recordId: 'rec-1',
        tenantId: 'demo',
        proposedReferences: { modelId: 'target-demo/some-other-model' },
      },
      approvedRecord,
    );
    expect(d.decision).toBe('deny');
    if (d.decision === 'deny') expect(d.httpStatus).toBe(403);
  });

  it('REG-12: changing an APPROVED record’s inferenceTargetArn is denied', () => {
    const d = evaluateRegistrationRequest(
      {
        action: 'update',
        principal: registrar,
        recordId: 'rec-1',
        tenantId: 'demo',
        proposedReferences: { inferenceTargetArn: 'arn:target:v2' },
      },
      approvedRecord,
    );
    expect(d.decision).toBe('deny');
  });

  it('a no-op update to an APPROVED record (references unchanged) is allowed', () => {
    const d = evaluateRegistrationRequest(
      {
        action: 'update',
        principal: registrar,
        recordId: 'rec-1',
        tenantId: 'demo',
        proposedReferences: { modelId: approvedRecord.references!.modelId },
      },
      approvedRecord,
    );
    expect(d.decision).toBe('allow');
  });
});

describe('AgentRegistrationApi — approve (REG-09, REG-11)', () => {
  it('REG-11-P: a separate approver approves the registrar’s record', () => {
    const d = evaluateRegistrationRequest(
      { action: 'approve', principal: approver, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord, // submittedBy: admin-1, approver is approver-1
    );
    expect(d.decision).toBe('allow');
  });

  it('REG-11-N: the submitter cannot approve its own record (SoD) 403', () => {
    const selfApprover: RegistrationPrincipal = {
      principalId: 'admin-1', // same as draftRecord.submittedBy
      role: 'registry_approver',
      tenantId: 'demo',
    };
    const d = evaluateRegistrationRequest(
      { action: 'approve', principal: selfApprover, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('deny');
    if (d.decision === 'deny') expect(d.httpStatus).toBe(403);
  });

  it('REG-09-N: approve from a non-approver (workstream admin) is denied 403', () => {
    const d = evaluateRegistrationRequest(
      { action: 'approve', principal: { ...workstream, role: 'other' }, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('deny');
  });
});

describe('AgentRegistrationApi — delete (REG-10) and fail-closed', () => {
  it('REG-10-N: delete from a non-registrar identity is denied 403', () => {
    const d = evaluateRegistrationRequest(
      { action: 'delete', principal: { ...workstream, role: 'other' }, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('deny');
  });

  it('registrar delete is allowed', () => {
    const d = evaluateRegistrationRequest(
      { action: 'delete', principal: registrar, recordId: 'rec-1', tenantId: 'demo' },
      draftRecord,
    );
    expect(d.decision).toBe('allow');
  });

  it('fails closed on a missing principal', () => {
    const d = evaluateRegistrationRequest({
      action: 'create',
      principal: undefined as unknown as RegistrationPrincipal,
      tenantId: 'demo',
    });
    expect(d.decision).toBe('deny');
  });

  it('fails closed on an unknown action', () => {
    const d = evaluateRegistrationRequest({
      action: 'purge' as unknown as 'create',
      principal: registrar,
      tenantId: 'demo',
    });
    expect(d.decision).toBe('deny');
  });
});
