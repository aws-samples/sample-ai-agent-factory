/**
 * Spec v2 cross-language contract fixtures.
 *
 * For every gallery template this writes two files under
 * backend/tests/fixtures/spec_v2/templates/ (vitest file snapshots: `vitest -u` rewrites
 * them, a plain run fails when they drift):
 *
 *   <id>.flow.json     the v1 flow document the canvas persists for the template, built by
 *                      the real instantiate + save-payload code with a fixed clock and PRNG;
 *   <id>.request.json  the POST /api/deploy body the frontend derives from that canvas,
 *                      built by the real derivation (`deriveCanvasDeployInputs`,
 *                      `runtimeConfigForRequest`, `mapGatewayDeployTargets`,
 *                      `identityConfigForRequest`) and keyed exactly as `useDeployment` keys it.
 *
 * The backend's Spec v2 migration + compiler
 * (backend/tests/test_agent_spec_v2.py) must turn the first into the second byte for byte.
 * A frontend change to the derivation therefore shows up here first, as a fixture diff that
 * the backend compiler has to follow, instead of as a silent split between the two paths.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WORKFLOW_TEMPLATES } from './templates';
import { instantiateTemplate } from '../utils/templates';
import { buildFlowSavePayload } from '../utils/flowSavePayload';
import {
  deriveCanvasDeployInputs,
  findDefaultDeployableRuntime,
  identityConfigForRequest,
} from '../utils/deployRequestFromCanvas';
import { runtimeConfigForRequest } from '../utils/runtimeRequestConfig';
import { mapGatewayDeployTargets } from '../utils/gatewayConfig';
import { createEmptyDeploymentGovernance } from '../types/workflow';
import type { RuntimeConfiguration } from '../types/components';

const FIXTURE_DIR = '../../../backend/tests/fixtures/spec_v2/templates';
const FIXED_CLOCK = new Date('2026-01-01T00:00:00.000Z');

function stable(value: unknown): string {
  return `${JSON.stringify(value, null, 2)}\n`;
}

describe('Spec v2 fixtures: every gallery template, as the canvas persists it and as the deploy request derives', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_CLOCK);
    // instantiateTemplate salts ids with Math.random(); (0.5).toString(36) is "0.i".
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each(WORKFLOW_TEMPLATES.map((template) => [template.id, template] as const))(
    '%s',
    async (templateId, template) => {
      const { nodes, edges } = instantiateTemplate(template);
      const flow = buildFlowSavePayload(`flow-${templateId}`, {
        nodes,
        edges,
        viewport: { x: 0, y: 0, zoom: 1 },
        governance: createEmptyDeploymentGovernance(),
      });

      const deployable = findDefaultDeployableRuntime(nodes, edges);
      expect(deployable, `${templateId} has a deployable runtime`).toBeDefined();
      const deployableConfig = deployable!.data.configuration as RuntimeConfiguration;
      const inputs = deriveCanvasDeployInputs(deployable!.id, nodes, edges, {});
      const { externalMcpServers: mcp, gatewayTargets } = mapGatewayDeployTargets(inputs.gatewayConfig);
      const gatewayConfigForDeploy = inputs.gatewayConfig ? { ...inputs.gatewayConfig, targets: gatewayTargets } : inputs.gatewayConfig;

      // Same keys, same order, same undefined-dropping as useDeployment's JSON.stringify body.
      const request = {
        deploymentMode: 'runtime',
        nodeId: deployable!.id,
        flowId: undefined,
        config: runtimeConfigForRequest(deployableConfig, templateId),
        connectedTools: inputs.tools,
        gatewayConfig: gatewayConfigForDeploy,
        gatewayTools: inputs.gatewayTools,
        templateId,
        identityConfig: identityConfigForRequest(inputs.identityConfig),
        customTools: inputs.customTools.length > 0 ? inputs.customTools : undefined,
        connectors: inputs.connectors.length > 0 ? inputs.connectors : undefined,
        externalMcpServers: mcp.length > 0 ? mcp : undefined,
        memoryConfig: inputs.memoryConfig || undefined,
        evaluationConfig: inputs.evaluationConfig || undefined,
        policyConfig: inputs.policyConfig || undefined,
        guardrailsConfig: inputs.guardrailsConfig || undefined,
        mcpServerConfig: inputs.mcpServerConfig || undefined,
        knowledgeBaseConfig: inputs.knowledgeBaseConfig || undefined,
        observabilityConfig: inputs.observabilityConfig || undefined,
        a2aConfig: inputs.a2aConfig || undefined,
        resourceTags: undefined,
        tagProfile: undefined,
        policyRevision: undefined,
        tagProfileUpdatedAt: undefined,
        targetAccountId: undefined,
        targetRegion: undefined,
      };

      await expect(stable(flow)).toMatchFileSnapshot(`${FIXTURE_DIR}/${templateId}.flow.json`);
      await expect(stable(request)).toMatchFileSnapshot(`${FIXTURE_DIR}/${templateId}.request.json`);
    },
  );
});
