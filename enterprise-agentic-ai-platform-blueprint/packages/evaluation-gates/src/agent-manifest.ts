/**
 * @agenticai/evaluation-gates — agent manifest.
 *
 * Multi-artefact versioning per BLUEPRINT_GAP_ANALYSIS (2).md Partial-1:
 * "treat prompts/agent-config/tool-permissions as versioned assets".
 *
 * `buildAgentManifest()` produces a stable JSON object that hashes to the
 * same SHA-256 if the inputs are equivalent. The manifest is uploaded to
 * the evaluation S3 bucket keyed by `<agentId>/<gitSha>/manifest.json` and
 * read by the online evaluation watchdog (Phase B) for drift detection.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { createHash } from 'crypto';

export interface AgentManifestInput {
  readonly agentId: string;
  readonly tenantId: string;
  readonly gitSha: string;                              // 7+ chars
  readonly promptHashes: Readonly<Record<string, string>>; // file -> sha256
  readonly toolPermissions: readonly string[];          // tool ids subscribed
  readonly configHash: string;                          // sha256 of bedrock.config.yaml etc
  readonly thresholdsHash: string;                      // sha256 of thresholds object
}

export interface AgentManifest extends AgentManifestInput {
  readonly manifestVersion: 1;
  readonly manifestSha: string;
  readonly emittedAt: string;                           // ISO-8601 UTC
}

const GIT_SHA_REGEX = /^[a-f0-9]{7,40}$/;

/**
 * Build a deterministic, immutable manifest from the inputs. Same input ⇒
 * same `manifestSha`. Used by Phase D agent-lifecycle to gate alias promotion.
 */
export function buildAgentManifest(
  input: AgentManifestInput,
  /** Injectable clock — keep deterministic in tests. */
  now: () => Date = () => new Date(),
): AgentManifest {
  if (!GIT_SHA_REGEX.test(input.gitSha)) {
    throw new Error(`gitSha must be 7-40 hex chars, got: ${input.gitSha}`);
  }
  if (input.toolPermissions.some((t) => !/^[a-z0-9-]{3,50}$/.test(t))) {
    throw new Error('toolPermissions ids must be kebab-case 3-50 chars');
  }

  const orderedTools = [...input.toolPermissions].sort();
  const orderedPrompts = Object.keys(input.promptHashes)
    .sort()
    .reduce<Record<string, string>>((acc, k) => {
      acc[k] = input.promptHashes[k];
      return acc;
    }, {});

  const stable = {
    manifestVersion: 1 as const,
    agentId: input.agentId,
    tenantId: input.tenantId,
    gitSha: input.gitSha,
    promptHashes: orderedPrompts,
    toolPermissions: orderedTools,
    configHash: input.configHash,
    thresholdsHash: input.thresholdsHash,
  };
  const manifestSha = createHash('sha256').update(JSON.stringify(stable)).digest('hex');

  return {
    ...stable,
    toolPermissions: orderedTools,
    promptHashes: orderedPrompts,
    manifestSha,
    emittedAt: now().toISOString(),
  };
}
