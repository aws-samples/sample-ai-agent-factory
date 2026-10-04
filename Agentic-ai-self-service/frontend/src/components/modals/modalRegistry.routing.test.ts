import { describe, expect, it } from 'vitest';

import type { AgentCoreNode } from '../../store/workflowStore';
import type { AgentCoreComponentType } from '../../types/workflow';
import {
  configurationTargetForExistingNode,
  configurationTargetForNewNode,
  shouldOpenConfigurationForNewNode,
} from './modalRegistry';

describe('new-node configuration modal routing', () => {
  it.each<AgentCoreComponentType>([
    'runtime',
    'gateway',
    'identity',
    'memory',
    'policy',
    'guardrails',
    'observability',
    'evaluation',
    'a2a',
  ])('opens a modal for configurable %s nodes', (componentType) => {
    expect(shouldOpenConfigurationForNewNode(componentType)).toBe(true);
  });

  it.each<AgentCoreComponentType>(['browser', 'code_interpreter'])(
    'does not enter an uncloseable pending-modal state for %s nodes',
    (componentType) => {
      expect(shouldOpenConfigurationForNewNode(componentType)).toBe(false);
    },
  );

  it('opens connector credentials but skips preconfigured tool nodes', () => {
    expect(
      shouldOpenConfigurationForNewNode('tool', 'connector:jira'),
    ).toBe(true);
    expect(
      shouldOpenConfigurationForNewNode('tool', 'duckduckgo_search'),
    ).toBe(false);
    expect(
      shouldOpenConfigurationForNewNode('tool', 'knowledge_base'),
    ).toBe(false);
  });

  it('routes by the exact new node identity even when type and position repeat', () => {
    const repeatedPosition = { x: 100, y: 100 };
    const olderTool = {
      id: 'tool-old',
      type: 'tool',
      position: repeatedPosition,
      data: {
        label: 'DuckDuckGo Search',
        componentType: 'tool',
        configuration: {
          name: 'duckduckgo_search',
          toolId: 'duckduckgo_search',
          description: 'Search the web',
          enabled: true,
        },
        validationStatus: 'valid',
      },
    } satisfies AgentCoreNode;
    const newConnector = {
      id: 'tool-new',
      type: 'tool',
      position: repeatedPosition,
      data: {
        label: 'Asana',
        componentType: 'tool',
        configuration: {
          toolId: 'connector:asana',
          connectorId: 'asana',
          authMethod: 'api_key',
          isConnector: true,
          configured: false,
          name: 'asana',
          description: 'Asana tasks and projects',
          enabled: true,
        },
        validationStatus: 'pending',
      },
    } satisfies AgentCoreNode;

    expect(olderTool.position).toEqual(newConnector.position);
    expect(olderTool.data.componentType).toBe(newConnector.data.componentType);
    expect(configurationTargetForNewNode(newConnector)).toMatchObject({
      nodeId: 'tool-new',
      componentType: 'tool',
      initialConfig: {
        toolId: 'connector:asana',
        connectorId: 'asana',
      },
    });
  });

  it('offers configuration only when an existing node has a real modal', () => {
    const browserNode = {
      id: 'browser-1',
      type: 'browser',
      position: { x: 0, y: 0 },
      data: {
        label: 'Browser Tool',
        componentType: 'browser',
        validationStatus: 'valid',
      },
    } satisfies AgentCoreNode;
    const existingTool = {
      id: 'tool-1',
      type: 'tool',
      position: { x: 0, y: 0 },
      data: {
        label: 'DuckDuckGo Search',
        componentType: 'tool',
        configuration: {
          name: 'duckduckgo_search',
          toolId: 'duckduckgo_search',
          description: 'Search the web',
          enabled: true,
        },
        validationStatus: 'valid',
      },
    } satisfies AgentCoreNode;

    expect(configurationTargetForExistingNode(browserNode)).toBeNull();
    expect(configurationTargetForExistingNode(existingTool)).toMatchObject({
      nodeId: 'tool-1',
      componentType: 'tool',
    });
  });
});
