/**
 * Domain-scoped registry helpers for the federated-mesh pattern.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-6.
 *
 * In the federated-mesh deployment style, large enterprises split agent
 * ownership across business-unit domains (Sales / Finance / Legal / ...).
 * Each domain owns its registry rows, but cross-domain discovery is allowed
 * read-only via a registry GSI. This module provides:
 *
 *   - A small SSOT of well-known top-level domain ids (validated at synth).
 *   - A helper that builds the registry sort-key shape for a domain row.
 *   - A helper that builds the SCP condition fragment used to scope mutate
 *     actions to the principal's home-domain.
 *
 * Callers (the registry construct, the SCP renderer) consume these so the
 * shape is identical everywhere.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

const DOMAIN_RE = /^[a-z][a-z0-9-]{2,31}$/;

/** Default well-known domain id list. Customers extend at synth via context. */
export const DEFAULT_DOMAIN_IDS: readonly string[] = ['sales', 'finance', 'legal', 'platform'];

export function validateDomainId(id: string): void {
  if (!DOMAIN_RE.test(id)) {
    throw new Error(`Domain id must match ${DOMAIN_RE} (got '${id}'). Use lowercase kebab-case.`);
  }
}

export function buildDomainScopedSk(domainId: string, agentId: string): string {
  validateDomainId(domainId);
  if (!/^[a-z0-9-]{3,64}$/.test(agentId)) {
    throw new Error(`agentId must be kebab-case 3-64 chars: ${agentId}`);
  }
  return `domain#${domainId}#agent#${agentId}`;
}

/** Build an IAM/SCP condition that pins a principal's `aws:PrincipalTag/domainId`. */
export function buildHomeDomainCondition(domainId: string): Record<string, Record<string, string>> {
  validateDomainId(domainId);
  return {
    StringEquals: {
      'aws:PrincipalTag/domainId': domainId,
    },
  };
}
