/**
 * A2A v0.2 Agent Card schema (subset that's stable across the spec).
 *
 * The full A2A spec evolves; we intentionally pin the **required** fields
 * so we can validate cards at synth without being overly strict on the
 * extension fields the spec keeps adding.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Partial-2 (synth-time validation).
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */

export interface A2ASkill {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: Record<string, unknown>;
  readonly outputSchema?: Record<string, unknown>;
}

export interface A2AEndpoint {
  readonly url: string;
  readonly transport: 'http+sse' | 'streamable-http';
}

export interface A2AAuth {
  readonly type: 'oauth2' | 'sigv4' | 'mtls';
  readonly description?: string;
}

export interface AgentCard {
  readonly schemaVersion: '0.2';
  readonly name: string;
  readonly description: string;
  readonly provider: string;
  readonly version: string;          // semver
  readonly endpoints: readonly A2AEndpoint[];
  readonly skills: readonly A2ASkill[];
  readonly auth: A2AAuth;
}

const SEMVER = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const KEBAB = /^[a-z0-9-]{3,64}$/;

/**
 * Validate an Agent Card. Throws with an actionable message at synth.
 */
export function validateAgentCard(card: AgentCard): void {
  if (card.schemaVersion !== '0.2') {
    throw new Error(`AgentCard schemaVersion must be '0.2', got: ${card.schemaVersion}`);
  }
  if (!card.name || card.name.length > 64) {
    throw new Error(`AgentCard name must be 1-64 chars`);
  }
  if (!card.description || card.description.length < 10) {
    throw new Error(`AgentCard description must be ≥ 10 chars`);
  }
  if (!SEMVER.test(card.version)) {
    throw new Error(`AgentCard version must be semver, got: ${card.version}`);
  }
  if (!card.endpoints.length) {
    throw new Error(`AgentCard must declare at least one endpoint`);
  }
  for (const e of card.endpoints) {
    if (!/^https:\/\//.test(e.url)) {
      throw new Error(`AgentCard endpoint URL must be HTTPS, got: ${e.url}`);
    }
  }
  if (!card.skills.length) {
    throw new Error(`AgentCard must declare at least one skill`);
  }
  for (const s of card.skills) {
    if (!KEBAB.test(s.id)) {
      throw new Error(`AgentCard skill id must be kebab-case 3-64 chars, got: ${s.id}`);
    }
  }
}

/** Normalise a card into a stable JSON string (sorted skill ids). */
export function serializeAgentCard(card: AgentCard): string {
  validateAgentCard(card);
  const sortedSkills = [...card.skills].sort((a, b) => a.id.localeCompare(b.id));
  const stable = { ...card, skills: sortedSkills };
  return JSON.stringify(stable);
}
