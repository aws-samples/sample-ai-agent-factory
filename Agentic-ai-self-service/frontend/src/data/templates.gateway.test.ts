import { describe, expect, it } from 'vitest';
import { WORKFLOW_TEMPLATES } from './templates';
import type { ToolConfiguration } from '../types/components';
import {
  validateWorkflow,
  type WorkflowEdge,
  type WorkflowNode,
} from '../utils/validation';

const template = WORKFLOW_TEMPLATES.find(({ id }) => id === 'web-search-agent')!;

function templateGraph(): { nodes: WorkflowNode[]; edges: WorkflowEdge[] } {
  return {
    nodes: template.nodes.map((node) => ({
      id: node.idSuffix,
      type: node.type,
      data: { label: node.label, configuration: node.configuration },
    })),
    edges: template.edges.map((edge) => ({
      id: `${edge.sourceIdSuffix}-${edge.targetIdSuffix}`,
      source: edge.sourceIdSuffix,
      target: edge.targetIdSuffix,
      type: edge.connectionType,
    })),
  };
}

describe('Managed Web Search gallery gateway', () => {
  it('ships a connected managed search tool and a valid gateway', () => {
    const graph = templateGraph();
    const gateway = graph.nodes.find(({ type }) => type === 'gateway')!;
    const search = graph.nodes.find(
      (node) => node.type === 'tool'
        && (node.data.configuration as ToolConfiguration).toolId === 'web_search',
    )!;

    expect(search).toBeDefined();
    expect(template.componentTypes).toContain('tool');
    expect(graph.edges.some((edge) => edge.source === gateway.id && edge.target === search.id)).toBe(true);
    expect(validateWorkflow(graph.nodes, graph.edges).isReadyToDeploy).toBe(true);
  });

  it('retains managed search after saving and reopening without template provenance', () => {
    const reopened = JSON.parse(JSON.stringify(templateGraph())) as ReturnType<typeof templateGraph>;
    const gateway = reopened.nodes.find(({ type }) => type === 'gateway')!;
    const connectedToolIds = reopened.edges
      .filter((edge) => edge.source === gateway.id || edge.target === gateway.id)
      .map((edge) => edge.source === gateway.id ? edge.target : edge.source)
      .map((id) => reopened.nodes.find((node) => node.id === id))
      .filter((node) => node?.type === 'tool')
      .map((node) => (node!.data.configuration as ToolConfiguration).toolId);

    // gateway_tools reaches the backend even when edits clear activeTemplateId.
    expect(connectedToolIds).toContain('web_search');
    expect(validateWorkflow(reopened.nodes, reopened.edges).errors).toEqual([]);
  });

  it('rejects a gateway after its only serving tool is disconnected', () => {
    const graph = templateGraph();
    const gateway = graph.nodes.find(({ type }) => type === 'gateway')!;
    const search = graph.nodes.find((node) => node.type === 'tool')!;
    expect(search).toBeDefined();

    const disconnectedEdges = graph.edges.filter(
      (edge) => edge.source !== search.id && edge.target !== search.id,
    );
    const validation = validateWorkflow(graph.nodes, disconnectedEdges);
    expect(validation.isReadyToDeploy).toBe(false);
    expect(validation.nodeStates.get(gateway.id)?.errors).toEqual(
      expect.arrayContaining([expect.objectContaining({ field: 'targets' })]),
    );
  });
});
