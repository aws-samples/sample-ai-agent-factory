import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { GuardrailsConfiguration, ObservabilityConfiguration } from '../types/components';

const { getFlow } = vi.hoisted(() => ({ getFlow: vi.fn() }));
vi.mock('../services/api', () => ({
  getApiClient: () => ({ getFlow }),
  getErrorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
}));

import { useFlowStore } from './flowStore';
import { useWorkflowStore } from './workflowStore';
import { toBackendNodes } from '../utils/flowSavePayload';

function savedFlow() {
  return {
    id: 'governed-research',
    name: 'Governed research',
    version: 47,
    deploymentStatus: 'not_deployed',
    workflow: {
      id: 'governed-research-workflow',
      name: 'Governed research',
      description: '',
      version: '1.0.0',
      nodes: [
        {
          id: 'guardrails',
          type: 'guardrails',
          position: { x: 0, y: 0 },
          data: {
            component_type: 'guardrails',
            name: 'Production Guardrails',
            mode: 'create_new',
            enabled: true,
            content_filters: {
              hate: 'HIGH',
              insults: 'HIGH',
              misconduct: 'HIGH',
              prompt_attack: 'HIGH',
              sexual: 'HIGH',
              violence: 'HIGH',
            },
            pii_filters: [{ type: 'EMAIL', action: 'ANONYMIZE' }],
          },
        },
        {
          id: 'observability',
          type: 'observability',
          position: { x: 0, y: 150 },
          data: {
            component_type: 'observability',
            name: 'Release Telemetry',
            enable_otel: false,
            service_name: 'developer-owned-agent',
            sample_rate: 0.25,
            resource_attributes: { deployment_id: 'customer-release', team_name: 'research' },
          },
        },
      ],
      edges: [],
      viewport: { x: 0, y: 0, zoom: 1 },
      metadata: { author: 'owner', tags: [], aws_region: 'us-east-1', deployment_status: 'not_deployed' },
      createdAt: '2026-11-04T00:00:00Z',
      updatedAt: '2026-11-04T00:00:00Z',
    },
    createdAt: '2026-11-04T00:00:00Z',
    updatedAt: '2026-11-04T00:00:00Z',
  };
}

describe('saved control hydration', () => {
  beforeEach(() => {
    getFlow.mockReset();
    useFlowStore.setState({
      flows: [],
      activeFlowId: null,
      activeFlowName: null,
      isLoading: false,
      error: null,
      navigationGeneration: 0,
      pendingFlowId: null,
    });
    useWorkflowStore.getState().resetWorkflowDocument(null);
  });

  it('retains every Guardrail category key used by the editor and export request', async () => {
    const flow = savedFlow();
    const before = structuredClone(flow);
    getFlow.mockResolvedValue(flow);

    await useFlowStore.getState().openFlow(flow.id);

    const node = useWorkflowStore.getState().nodes.find(item => item.id === 'guardrails');
    const config = node?.data.configuration as GuardrailsConfiguration;
    expect(config.contentFilters).toEqual(flow.workflow.nodes[0].data.content_filters);
    expect(config.contentFilters?.prompt_attack).toBe('HIGH');
    expect(config.contentFilters).not.toHaveProperty('promptAttack');
    expect(config.piiFilters).toEqual([{ type: 'EMAIL', action: 'ANONYMIZE' }]);
    expect(config.name).toBe('Production Guardrails');
    expect(config.enabled).toBe(true);
    expect(flow).toEqual(before);
  });

  it('converts telemetry field names but preserves disabling intent and customer attribute names', async () => {
    const flow = savedFlow();
    getFlow.mockResolvedValue(flow);

    await useFlowStore.getState().openFlow(flow.id);

    const node = useWorkflowStore.getState().nodes.find(item => item.id === 'observability');
    const config = node?.data.configuration as ObservabilityConfiguration;
    expect(config.enableOtel).toBe(false);
    expect(config.serviceName).toBe('developer-owned-agent');
    expect(config.sampleRate).toBe(0.25);
    expect(config.resourceAttributes).toEqual({ deployment_id: 'customer-release', team_name: 'research' });
    expect(useWorkflowStore.getState().persistenceRevision).toBe(0);
  });

  it('preserves policy and customer attribute dictionaries through save and reopen', async () => {
    const flow = savedFlow();
    getFlow.mockResolvedValue(flow);
    await useFlowStore.getState().openFlow(flow.id);
    const node = useWorkflowStore.getState().nodes.find(item => item.id === 'observability');
    const config = node?.data.configuration as ObservabilityConfiguration;
    const resourceAttributes = {
      teamName: 'research',
      team_name: 'customer-owned',
      'deployment.environment.name': 'staging',
      AcmeRelease: 'v1',
      deployment_id: 'release-47',
    };
    useWorkflowStore.getState().updateNodeConfiguration('observability', {
      ...config,
      resourceAttributes,
    });

    const before = structuredClone(useWorkflowStore.getState().nodes);
    const nodes = toBackendNodes(before) as Array<{ id: string; data: Record<string, unknown> }>;
    expect(nodes.find(item => item.id === 'observability')?.data.resource_attributes).toEqual(resourceAttributes);
    expect(nodes.find(item => item.id === 'observability')?.data.enable_otel).toBe(false);
    expect(nodes.find(item => item.id === 'guardrails')?.data.content_filters).toEqual(
      flow.workflow.nodes[0].data.content_filters,
    );
    expect(useWorkflowStore.getState().nodes).toEqual(before);

    getFlow.mockResolvedValue({ ...flow, workflow: { ...flow.workflow, nodes } });
    await useFlowStore.getState().openFlow(flow.id);
    expect(getFlow).toHaveBeenCalledTimes(2);
    expect(useFlowStore.getState().error).toBeNull();
    const reopened = useWorkflowStore.getState().nodes.find(item => item.id === 'observability');
    expect((reopened?.data.configuration as ObservabilityConfiguration).resourceAttributes).toEqual(resourceAttributes);
    const guardrails = useWorkflowStore.getState().nodes.find(item => item.id === 'guardrails');
    expect((guardrails?.data.configuration as GuardrailsConfiguration).contentFilters?.prompt_attack).toBe('HIGH');
  });
});
