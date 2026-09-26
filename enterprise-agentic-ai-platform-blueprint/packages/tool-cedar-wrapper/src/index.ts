/**
 * @agenticai/tool-cedar-wrapper — Phase Q (v0.6.0).
 *
 * Per-tool Lambda Cedar enforcement wrapper. Acts as the second-layer
 * entitlement check that runs INSIDE the tool Lambda, before the user's
 * tool body. This remains the rollback and parity gate while pipeline-owned
 * Gateway PolicyEngine support moves through LOG_ONLY and ENFORCE validation.
 * It is removed only after live parity, rollback, and zero-residual teardown
 * pass (TODO-GW-POLICY-ENGINE in README §3).
 *
 * Threat model the wrapper closes:
 *   - A developer's Cognito JWT is accepted by the AgentCore Gateway
 *     CUSTOM_JWT authorizer (the JWT is valid).
 *   - AgentCore forwards the call to the tool Lambda along with the JWT
 *     claims in the event payload.
 *   - Without this wrapper, every authenticated developer could invoke
 *     every subscribed tool — there is no per-developer scoping.
 *   - With this wrapper, the tool's Cedar policy is evaluated against the
 *     JWT's `cognito:groups` claim. Only members of the allow-listed
 *     groups proceed; everyone else gets a 403-shaped error before user
 *     code runs.
 *
 * Why a Cedar mini-evaluator instead of the official @cedar-policy/cedar-wasm
 * runtime: the AgentCore Gateway emits a tightly-bounded grammar (the
 * `composeCedarPolicyDocument` output of @agenticai/platform-tool-catalogue):
 *   permit(principal in CognitoGroup::"<g>", action == Action::"InvokeTool", resource == Tool::"<id>");
 *   forbid(principal, action, resource) unless { principal has allowed && resource has allowed };
 * A 60-line regex evaluator handles that grammar deterministically and ships
 * with zero runtime dependencies (Lambda inline-handler-friendly). The opt-in
 * Gateway PolicyEngine path now compiles the native AgentCore schema separately;
 * this evaluator stays active until that path passes live parity and rollback.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface CedarEvaluationInput {
  /** Tool id this Lambda implements — matches the Cedar resource literal. */
  readonly toolId: string;
  /** Composed Cedar policy document for the workstream (from composeCedarPolicyDocument). */
  readonly cedarPolicyDocument: string;
  /**
   * Cognito groups present in the principal's JWT (`cognito:groups` claim).
   * Empty array means the principal has no group membership.
   */
  readonly principalGroups: readonly string[];
  /**
   * The principal's stable subject identifier (the JWT `sub` claim). Enables
   * per-developer entitlements (`permit(principal == Developer::"<sub>", ...)`)
   * alongside group entitlements. Undefined/empty means no subject was
   * presented, so subject-bound permits cannot match — fail-closed.
   */
  readonly principalSubject?: string;
}

export type CedarDecision =
  | { readonly decision: 'allow'; readonly reason: string }
  | { readonly decision: 'deny'; readonly reason: string };

const PERMIT_REGEX =
  /permit\s*\(\s*principal(?:\s+in\s+CognitoGroup::"([^"]+)")?\s*,\s*action\s*==\s*Action::"InvokeTool"\s*,\s*resource\s*==\s*Tool::"([^"]+)"\s*\)\s*;/g;

/**
 * Per-developer (subject) permit grammar, additive to the group grammar:
 *   permit(principal == Developer::"<sub>", action == Action::"InvokeTool",
 *          resource == Tool::"<id>");
 * The `sub` claim is a stable, opaque per-user identifier, so the principal is
 * matched by exact equality (`==`), not set membership (`in`).
 */
const SUBJECT_PERMIT_REGEX =
  /permit\s*\(\s*principal\s*==\s*Developer::"([^"]+)"\s*,\s*action\s*==\s*Action::"InvokeTool"\s*,\s*resource\s*==\s*Tool::"([^"]+)"\s*\)\s*;/g;

/**
 * Pure Cedar evaluator scoped to the grammar emitted by
 * `composeCedarPolicyDocument` in @agenticai/platform-tool-catalogue.
 *
 * Decision algorithm:
 *   1. Walk every `permit(principal[ in CognitoGroup::"<g>"], action ==
 *      Action::"InvokeTool", resource == Tool::"<toolId>")` matching the
 *      caller's toolId.
 *   2. If at least one matching permit is unconditional (no group binding),
 *      ALLOW.
 *   3. If at least one matching permit names a group, allow iff
 *      principalGroups intersects the bound group set.
 *   4. Otherwise DENY (the catalogue's default forbid).
 *
 * The wrapper is fail-closed: any parse error or missing input denies.
 */
export function evaluateCedar(input: CedarEvaluationInput): CedarDecision {
  if (!input.toolId || typeof input.toolId !== 'string') {
    return { decision: 'deny', reason: 'evaluateCedar: missing or invalid toolId' };
  }
  if (!input.cedarPolicyDocument || typeof input.cedarPolicyDocument !== 'string') {
    return {
      decision: 'deny',
      reason: 'evaluateCedar: missing or invalid cedarPolicyDocument — fail-closed default forbid',
    };
  }
  const principalGroups = Array.isArray(input.principalGroups) ? input.principalGroups : [];
  const principalSubject =
    typeof input.principalSubject === 'string' && input.principalSubject.length > 0
      ? input.principalSubject
      : undefined;

  // 1. Subject-bound permits win first — the most specific entitlement.
  const subjectBoundPermits: string[] = [];
  SUBJECT_PERMIT_REGEX.lastIndex = 0;
  let subjectMatch: RegExpExecArray | null;
  while ((subjectMatch = SUBJECT_PERMIT_REGEX.exec(input.cedarPolicyDocument)) !== null) {
    const boundSubject = subjectMatch[1];
    const permitToolId = subjectMatch[2];
    if (permitToolId === input.toolId && typeof boundSubject === 'string' && boundSubject.length > 0) {
      subjectBoundPermits.push(boundSubject);
    }
  }
  if (subjectBoundPermits.length > 0 && principalSubject !== undefined) {
    if (subjectBoundPermits.includes(principalSubject)) {
      return {
        decision: 'allow',
        reason: `principal subject matches Developer permit for tool '${input.toolId}'`,
      };
    }
  }

  let unconditionalPermitMatched = false;
  const groupBoundPermits: string[] = [];
  PERMIT_REGEX.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = PERMIT_REGEX.exec(input.cedarPolicyDocument)) !== null) {
    const boundGroup = match[1];
    const permitToolId = match[2];
    if (permitToolId !== input.toolId) {
      continue;
    }
    if (typeof boundGroup === 'string' && boundGroup.length > 0) {
      groupBoundPermits.push(boundGroup);
    } else {
      unconditionalPermitMatched = true;
    }
  }

  if (unconditionalPermitMatched && groupBoundPermits.length === 0) {
    return {
      decision: 'allow',
      reason: `unconditional permit matched for tool '${input.toolId}'`,
    };
  }
  if (groupBoundPermits.length > 0) {
    const intersect = groupBoundPermits.filter((g) => principalGroups.includes(g));
    if (intersect.length > 0) {
      return {
        decision: 'allow',
        reason:
          `principal in group(s) [${intersect.join(', ')}] matches Cedar permit for tool '${input.toolId}'`,
      };
    }
    // Fall through to deny — but distinguish whether subject permits also
    // existed so the reason is actionable.
    const subjectNote =
      subjectBoundPermits.length > 0
        ? ` (subject permits present but principal sub ${principalSubject ? 'did not match' : 'absent'})`
        : '';
    return {
      decision: 'deny',
      reason:
        `principal groups [${principalGroups.join(', ') || '(none)'}] do not intersect tool '${input.toolId}' allow-list [${groupBoundPermits.join(', ')}]${subjectNote}`,
    };
  }
  if (subjectBoundPermits.length > 0) {
    // Only subject permits exist for this tool and none matched the principal.
    return {
      decision: 'deny',
      reason:
        `principal sub ${principalSubject ? `'${principalSubject}'` : '(none)'} does not match tool '${input.toolId}' subject allow-list [${subjectBoundPermits.join(', ')}]`,
    };
  }
  return {
    decision: 'deny',
    reason: `no permit matched for tool '${input.toolId}' — default forbid applies`,
  };
}

/**
 * AgentCore Gateway forwards the caller's identity context to a Lambda
 * tool target inside the event payload. The exact shape varies by auth
 * mode; we accept the three documented shapes (preview API, ListGatewayMcp
 * GA payload, and the bare-Lambda invocation shape used in unit tests).
 *
 * Returns an empty array when no claims are present — fail-closed.
 */
export function extractPrincipalGroupsFromEvent(event: unknown): readonly string[] {
  if (!event || typeof event !== 'object') return [];
  const ev = event as Record<string, unknown>;

  const candidates: unknown[] = [
    // Preview AgentCore Gateway shape (2026-05-05): `event.identity.claims`.
    (ev.identity as Record<string, unknown> | undefined)?.claims,
    // GA-track shape: `event.requestContext.authorizer.jwt.claims`.
    ((ev.requestContext as Record<string, unknown> | undefined)?.authorizer as
      | Record<string, unknown>
      | undefined)?.jwt &&
      (((ev.requestContext as Record<string, unknown>).authorizer as Record<string, unknown>)
        .jwt as Record<string, unknown>).claims,
    // Bare-Lambda test shape: `event.claims`.
    ev.claims,
  ];

  for (const c of candidates) {
    if (c && typeof c === 'object') {
      const claims = c as Record<string, unknown>;
      const raw = claims['cognito:groups'];
      if (Array.isArray(raw)) {
        return raw.filter((g): g is string => typeof g === 'string');
      }
      if (typeof raw === 'string' && raw.length > 0) {
        // Cognito sometimes serialises the claim as a comma-separated string
        // when bound to an OIDC client that flattens array claims.
        return raw.split(',').map((s) => s.trim()).filter((s) => s.length > 0);
      }
    }
  }
  return [];
}

/**
 * Extracts the principal's stable subject identifier (JWT `sub` claim) from the
 * same three documented event shapes as {@link extractPrincipalGroupsFromEvent}.
 * Returns undefined when no `sub` is present — subject-bound permits then cannot
 * match, which is fail-closed.
 */
export function extractPrincipalSubjectFromEvent(event: unknown): string | undefined {
  if (!event || typeof event !== 'object') return undefined;
  const ev = event as Record<string, unknown>;
  const candidates: unknown[] = [
    (ev.identity as Record<string, unknown> | undefined)?.claims,
    ((ev.requestContext as Record<string, unknown> | undefined)?.authorizer as
      | Record<string, unknown>
      | undefined)?.jwt &&
      (((ev.requestContext as Record<string, unknown>).authorizer as Record<string, unknown>)
        .jwt as Record<string, unknown>).claims,
    ev.claims,
  ];
  for (const c of candidates) {
    if (c && typeof c === 'object') {
      const claims = c as Record<string, unknown>;
      const sub = claims['sub'];
      if (typeof sub === 'string' && sub.length > 0) {
        return sub;
      }
    }
  }
  return undefined;
}

export class CedarDeniedError extends Error {
  readonly toolId: string;
  readonly principalGroups: readonly string[];
  constructor(toolId: string, principalGroups: readonly string[], reason: string) {
    super(
      `Tool '${toolId}' denied by Cedar policy. Principal groups: [${principalGroups.join(', ') || '(none)'}]. Reason: ${reason}`,
    );
    this.name = 'CedarDeniedError';
    this.toolId = toolId;
    this.principalGroups = principalGroups;
  }
}

export interface CedarWrapperOptions {
  /** Tool id this Lambda implements. */
  readonly toolId: string;
  /**
   * Composed Cedar policy document. Either passed in (unit test) or read from
   * `process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT` (Lambda env at deploy time).
   */
  readonly cedarPolicyDocument?: string;
}

type LambdaHandler = (event: unknown, context?: unknown) => Promise<unknown> | unknown;

/**
 * Wraps a Lambda handler with Cedar enforcement. The wrapper extracts the
 * principal's Cognito groups from the event, evaluates the per-tool Cedar
 * policy, and either invokes the inner handler (allow) or throws
 * `CedarDeniedError` (deny). The default forbid is fail-closed.
 */
export function withCedarEnforcement(
  options: CedarWrapperOptions,
  inner: LambdaHandler,
): LambdaHandler {
  if (!options.toolId || typeof options.toolId !== 'string') {
    throw new Error('withCedarEnforcement: options.toolId is required');
  }
  return async function wrappedHandler(event: unknown, context?: unknown) {
    const cedarDoc =
      options.cedarPolicyDocument ?? process.env.AGENTICAI_CEDAR_POLICY_DOCUMENT ?? '';
    const principalGroups = extractPrincipalGroupsFromEvent(event);
    const principalSubject = extractPrincipalSubjectFromEvent(event);
    const decision = evaluateCedar({
      toolId: options.toolId,
      cedarPolicyDocument: cedarDoc,
      principalGroups,
      principalSubject,
    });
    if (decision.decision === 'deny') {
      throw new CedarDeniedError(options.toolId, principalGroups, decision.reason);
    }
    return inner(event, context);
  };
}
