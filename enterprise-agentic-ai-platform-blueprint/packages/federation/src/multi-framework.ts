/**
 * Multi-framework adapter helpers.
 *
 * Closes BLUEPRINT_GAP_ANALYSIS (2).md Missing-7.
 *
 * The blueprint claims framework-agnostic. To be honest about that, we ship
 * adapter wiring for LangGraph + CrewAI alongside the existing Strands
 * blueprints. Both frameworks need the same three things:
 *   - Locked MCP protocol version (`2025-06-18`).
 *   - Tools resolved from the platform tool catalogue with qualified names.
 *   - Bedrock Guardrail enforced on every model call.
 *
 * The adapter functions here produce the same FrameworkAdapterConfig that
 * each blueprint's bootstrap consumes, so the framework code itself stays
 * tiny.
 *
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import { MCP_PROTOCOL_VERSION, qualifyToolName } from '@agenticai/agent-protocols';

export type FrameworkId = 'strands' | 'langgraph' | 'crewai';

export interface FrameworkAdapterConfig {
  readonly framework: FrameworkId;
  readonly gatewayUrl: string;
  readonly mcpProtocolVersion: typeof MCP_PROTOCOL_VERSION;
  readonly toolNames: readonly string[];      // qualified names
  readonly guardrailIdentifier: string;
  readonly inferenceProfileArn: string;
}

export interface AdapterInputs {
  readonly framework: FrameworkId;
  readonly gatewayUrl: string;
  readonly targetName: string;
  readonly toolIds: readonly string[];
  readonly guardrailIdentifier: string;
  readonly inferenceProfileArn: string;
}

export function buildFrameworkAdapterConfig(input: AdapterInputs): FrameworkAdapterConfig {
  if (!['strands', 'langgraph', 'crewai'].includes(input.framework)) {
    throw new Error(`Unsupported framework: ${input.framework}`);
  }
  if (!/^https:\/\//.test(input.gatewayUrl)) {
    throw new Error(`Gateway URL must be HTTPS, got: ${input.gatewayUrl}`);
  }
  if (!input.guardrailIdentifier) {
    throw new Error('guardrailIdentifier is mandatory (R-BED-028 + SCP-02 + IAM deny + VPCE policy)');
  }
  return {
    framework: input.framework,
    gatewayUrl: input.gatewayUrl,
    mcpProtocolVersion: MCP_PROTOCOL_VERSION,
    toolNames: input.toolIds.map((id) => qualifyToolName(input.targetName, id)),
    guardrailIdentifier: input.guardrailIdentifier,
    inferenceProfileArn: input.inferenceProfileArn,
  };
}
