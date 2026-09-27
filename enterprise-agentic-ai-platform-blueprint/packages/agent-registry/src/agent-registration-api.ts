/**
 * AgentRegistrationApi — sanctioned control-plane surface #1 of 3.
 *
 * The single intake point for agent metadata: register or update an agent's
 * declared identity, models, tool subscriptions, throughput, and budget
 * (target-state architecture §9). This module is the PURE authorization core;
 * the deployable API Gateway + Lambda shell lives in
 * `agent-registration-api-construct.ts` and delegates every decision here so
 * the whole policy is unit-testable offline with no AWS.
 *
 * Authorization contract (from the adversarial catalog REG-07..REG-12):
 *   - create / update / delete : the registrar role only (others → 403).
 *   - approve                  : the approver role only, AND the approver must
 *                                differ from the record's submitter
 *                                (self-approval is the core SoD attack → 403).
 *   - approved-reference immutability : once a record is APPROVED, an update
 *                                that changes its inferenceTargetArn, modelId,
 *                                or rateProfile requires a fresh review, so it
 *                                is denied on the approved record → 403.
 *   - a caller may only create/update the caller's OWN tenant records; no
 *     cross-tenant mutation.
 *
 * The core is fail-closed: an unknown action, missing principal, or malformed
 * request denies.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export type RegistrationAction = 'create' | 'update' | 'approve' | 'delete';

export type RegistrarRole =
  | 'registry_admin'
  | 'registry_approver'
  | 'other';

/** The subset of a record's fields that bind it to sanctioned resources. */
export interface ApprovedReferences {
  readonly inferenceTargetArn: string;
  readonly modelId: string;
  readonly rateProfile: string;
}

export interface RegistrationPrincipal {
  /** Stable principal id (e.g. the assumed-role session subject). */
  readonly principalId: string;
  /** The registrar/approver authority the principal holds. */
  readonly role: RegistrarRole;
  /** The tenant the principal belongs to. */
  readonly tenantId: string;
}

export interface RegistrationRecordState {
  readonly recordId: string;
  readonly tenantId: string;
  readonly status: 'DRAFT' | 'APPROVED' | 'DEPRECATED';
  /** Principal id that submitted the record (for SoD on approve). */
  readonly submittedBy?: string;
  /** The approved references, present once the record exists. */
  readonly references?: ApprovedReferences;
}

export interface RegistrationRequest {
  readonly action: RegistrationAction;
  readonly principal: RegistrationPrincipal;
  /** Target record id (for update/approve/delete). Absent for create. */
  readonly recordId?: string;
  readonly tenantId: string;
  /** For update: the proposed reference changes, if any. */
  readonly proposedReferences?: Partial<ApprovedReferences>;
}

export type RegistrationDecision =
  | { readonly decision: 'allow'; readonly reason: string }
  | { readonly decision: 'deny'; readonly httpStatus: 403; readonly reason: string };

function deny(reason: string): RegistrationDecision {
  return { decision: 'deny', httpStatus: 403, reason };
}
function allow(reason: string): RegistrationDecision {
  return { decision: 'allow', reason };
}

/**
 * Evaluate one AgentRegistrationApi request against the authorization contract.
 * `currentState` is the existing record for update/approve/delete (undefined
 * for create, or when the target record does not exist).
 */
export function evaluateRegistrationRequest(
  req: RegistrationRequest,
  currentState?: RegistrationRecordState,
): RegistrationDecision {
  if (!req || typeof req !== 'object') return deny('malformed request');
  const p = req.principal;
  if (!p || !p.principalId || !p.role || !p.tenantId) {
    return deny('missing or incomplete principal — fail closed');
  }
  if (!req.tenantId) return deny('missing tenantId — fail closed');

  switch (req.action) {
    case 'create':
      if (p.role !== 'registry_admin') {
        return deny(
          `create requires the registrar role; principal holds '${p.role}' (REG-07-N)`,
        );
      }
      if (p.tenantId !== req.tenantId) {
        return deny('registrar may only create records in its own tenant');
      }
      return allow('registrar create of a new record (REG-07-P)');

    case 'update': {
      if (p.role !== 'registry_admin') {
        return deny(
          `update requires the registrar role; principal holds '${p.role}' (REG-08-N)`,
        );
      }
      if (!currentState) return deny('update target record does not exist');
      if (currentState.tenantId !== p.tenantId || req.tenantId !== p.tenantId) {
        return deny('cross-tenant update is denied');
      }
      // Approved-reference immutability (REG-12-N): once APPROVED, changing a
      // bound reference requires re-approval, so deny the in-place update.
      if (currentState.status === 'APPROVED' && changesApprovedReference(req, currentState)) {
        return deny(
          'update of an approved record’s inferenceTarget/modelId/rateProfile ' +
            'requires re-approval (REG-12-N)',
        );
      }
      return allow('registrar update of a record');
    }

    case 'delete':
      if (p.role !== 'registry_admin') {
        return deny(
          `delete requires the registrar role; principal holds '${p.role}' (REG-10-N)`,
        );
      }
      if (!currentState) return deny('delete target record does not exist');
      if (currentState.tenantId !== p.tenantId) {
        return deny('cross-tenant delete is denied');
      }
      return allow('registrar delete of a record');

    case 'approve': {
      if (p.role !== 'registry_approver') {
        return deny(
          `approve requires the approver role; principal holds '${p.role}' (REG-09-N)`,
        );
      }
      if (!currentState) return deny('approve target record does not exist');
      // Separation of duties (REG-11-N): the approver must not be the submitter.
      if (currentState.submittedBy && currentState.submittedBy === p.principalId) {
        return deny(
          'the submitter cannot approve its own record — separation of duties (REG-11-N)',
        );
      }
      if (currentState.tenantId !== p.tenantId) {
        return deny('cross-tenant approve is denied');
      }
      return allow('separate approver approves the submitted record (REG-11-P)');
    }

    default:
      return deny(`unknown action '${String(req.action)}' — fail closed`);
  }
}

/** True if the update proposes a change to any approved-bound reference. */
export function changesApprovedReference(
  req: RegistrationRequest,
  currentState: RegistrationRecordState,
): boolean {
  const proposed = req.proposedReferences;
  if (!proposed) return false;
  const current = currentState.references;
  if (!current) return false;
  const keys: (keyof ApprovedReferences)[] = [
    'inferenceTargetArn',
    'modelId',
    'rateProfile',
  ];
  return keys.some(
    (k) => proposed[k] !== undefined && proposed[k] !== current[k],
  );
}
