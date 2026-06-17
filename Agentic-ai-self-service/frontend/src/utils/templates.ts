/**
 * Template instantiation utilities.
 * Converts template definitions into concrete nodes and edges for the workflow store.
 */

import type { Edge } from '@xyflow/react';
import type { WorkflowTemplate } from '../types/templates';
import type { AgentCoreNode } from '../store/workflowStore';
import { PALETTE_ITEMS } from '../components/palette/ComponentPalette';

/**
 * Instantiate a template into concrete nodes and edges ready for the store.
 * Generates unique IDs using timestamps (same pattern as createNodeFromDrop in dragDrop.ts).
 */
export function instantiateTemplate(template: WorkflowTemplate): {
  nodes: AgentCoreNode[];
  edges: Edge[];
} {
  const timestamp = Date.now();
  const idMap = new Map<string, string>();

  // Build nodes with unique IDs
  const nodes: AgentCoreNode[] = template.nodes.map((nodeDef, index) => {
    const fullId = `${nodeDef.type}-${timestamp}-${index}-${Math.random().toString(36).substr(2, 9)}`;
    idMap.set(nodeDef.idSuffix, fullId);

    const paletteItem = PALETTE_ITEMS.find((item) => item.type === nodeDef.type);
    const label = nodeDef.label || paletteItem?.label || nodeDef.type;

    return {
      id: fullId,
      type: nodeDef.type,
      position: { ...nodeDef.position },
      selected: false,
      data: {
        label,
        componentType: nodeDef.type,
        configuration: nodeDef.configuration,
        validationStatus: 'pending' as const,
      },
    };
  });

  // Build edges using the ID map
  const edges: Edge[] = template.edges.map((edgeDef, index) => {
    const sourceId = idMap.get(edgeDef.sourceIdSuffix)!;
    const targetId = idMap.get(edgeDef.targetIdSuffix)!;

    return {
      id: `edge-${timestamp}-${index}-${Math.random().toString(36).substr(2, 9)}`,
      source: sourceId,
      target: targetId,
      type: 'connection',
      data: {
        connectionType: edgeDef.connectionType,
        validationStatus: 'valid',
      },
    };
  });

  return { nodes, edges };
}
