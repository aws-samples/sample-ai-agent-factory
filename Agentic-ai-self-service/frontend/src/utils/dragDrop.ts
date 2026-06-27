/**
 * Drag-drop utility functions for component palette to canvas operations.
 * Requirements: 1.2, 12.3
 */

import type { AgentCoreComponentType } from '../types/workflow';
import type { ToolConfiguration, ConnectorConfiguration } from '../types/components';
import { CONNECTOR_TOOL_PREFIX } from '../types/components';
import type { AgentCoreNode } from '../store/workflowStore';
import { PALETTE_ITEMS } from '../components/palette/ComponentPalette';

// ============================================================================
// Constants
// ============================================================================

export const DRAG_DATA_TYPE = 'application/agentcore-component';
export const DRAG_TOOL_ID_TYPE = 'application/agentcore-tool-id';

// ============================================================================
// Drag-Drop State
// ============================================================================

export interface DragState {
  isDragging: boolean;
  componentType: AgentCoreComponentType | null;
  ghostPosition: { x: number; y: number } | null;
}

export const initialDragState: DragState = {
  isDragging: false,
  componentType: null,
  ghostPosition: null,
};

// ============================================================================
// Position Calculation
// ============================================================================

/**
 * Calculate the drop position on the canvas from a drag event.
 * Property 1: Node Creation at Drop Position
 * For any component type dragged from the palette and for any valid drop position
 * on the canvas, the created node's position shall equal the drop coordinates.
 *
 * @param event - The drag event
 * @param canvasRect - The bounding rect of the canvas element
 * @param viewport - Current viewport state { x, y, zoom }
 * @returns The position in canvas coordinates
 */
export function calculateDropPosition(
  clientX: number,
  clientY: number,
  canvasRect: DOMRect,
  viewport: { x: number; y: number; zoom: number }
): { x: number; y: number } {
  // Get position relative to canvas element
  const relativeX = clientX - canvasRect.left;
  const relativeY = clientY - canvasRect.top;

  // Convert to canvas coordinates accounting for viewport pan and zoom
  const canvasX = (relativeX - viewport.x) / viewport.zoom;
  const canvasY = (relativeY - viewport.y) / viewport.zoom;

  return { x: canvasX, y: canvasY };
}

/**
 * Calculate ghost preview position during drag.
 * Property 42: Drag Ghost Preview
 * For any component drag from palette, a ghost preview of the component
 * shall follow the cursor position.
 *
 * @param clientX - Client X coordinate
 * @param clientY - Client Y coordinate
 * @param canvasRect - The bounding rect of the canvas element
 * @returns The ghost position in screen coordinates
 */
export function calculateGhostPosition(
  clientX: number,
  clientY: number,
  canvasRect: DOMRect
): { x: number; y: number } {
  return {
    x: clientX - canvasRect.left,
    y: clientY - canvasRect.top,
  };
}

// ============================================================================
// Node Creation
// ============================================================================

/**
 * Create a new node from a dropped component type.
 * @param componentType - The type of component being dropped
 * @param position - The drop position in canvas coordinates
 * @param toolId - Optional tool ID for tool nodes
 * @returns A new AgentCoreNode
 */
export function createNodeFromDrop(
  componentType: AgentCoreComponentType,
  position: { x: number; y: number },
  toolId?: string | null
): AgentCoreNode {
  // For tool nodes, find the specific palette item by toolId
  const paletteItem = componentType === 'tool' && toolId
    ? PALETTE_ITEMS.find((item) => item.type === 'tool' && item.toolId === toolId)
    : PALETTE_ITEMS.find((item) => item.type === componentType);
  const label = paletteItem?.label || componentType;

  // Connector nodes are `tool`-typed with toolId "connector:<id>" — they need
  // credentials before they can deploy, so they start un-configured (pending)
  // and open the ConnectorConfigModal on drop.
  const isConnector = componentType === 'tool' && !!toolId && toolId.startsWith(CONNECTOR_TOOL_PREFIX);
  const connectorId = isConnector ? toolId!.slice(CONNECTOR_TOOL_PREFIX.length) : '';

  // KB tool needs configuration modal (pending), other tools are pre-configured (valid)
  const isKBTool = componentType === 'tool' && toolId === 'knowledge_base';

  // Build initial configuration for tool / connector nodes
  let configuration: ToolConfiguration | ConnectorConfiguration | undefined;
  if (isConnector) {
    configuration = {
      name: label.toLowerCase().replace(/\s+/g, '_'),
      toolId: toolId!,
      description: paletteItem?.description || '',
      enabled: true,
      isConnector: true,
      connectorId,
      // Asana is API-key only; everything branded else defaults to oauth2_cc,
      // generic starts on api_key (most common for a pasted spec).
      authMethod: connectorId === 'asana' || connectorId === 'generic_openapi' ? 'api_key' : 'oauth2_cc',
      configured: false,
    } as ConnectorConfiguration;
  } else if (componentType === 'tool' && toolId) {
    configuration = {
      name: label.toLowerCase().replace(/\s+/g, '_'),
      toolId,
      description: paletteItem?.description || '',
      enabled: true,
      ...(toolId === 'knowledge_base' ? { isKnowledgeBase: true as const } : {}),
    } as ToolConfiguration;
  }

  return {
    id: `${componentType}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
    type: componentType,
    position,
    selected: false,
    data: {
      label,
      componentType,
      configuration,
      validationStatus: isKBTool || isConnector ? 'pending'
        : componentType === 'tool' && toolId ? 'valid'
        : ['code_interpreter', 'browser', 'observability'].includes(componentType) ? 'valid'
        : 'pending',
    },
  };
}

// ============================================================================
// Drag Event Handlers
// ============================================================================

/**
 * Extract component type from drag event data.
 * @param event - The drag event
 * @returns The component type or null if invalid
 */
export function getComponentTypeFromDrag(event: React.DragEvent): AgentCoreComponentType | null {
  const data = event.dataTransfer.getData(DRAG_DATA_TYPE);
  if (!data) return null;

  const validTypes: AgentCoreComponentType[] = ['runtime', 'gateway', 'memory', 'code_interpreter', 'browser', 'observability', 'identity', 'evaluation', 'policy', 'guardrails', 'a2a', 'tool'];
  return validTypes.includes(data as AgentCoreComponentType) ? (data as AgentCoreComponentType) : null;
}

/**
 * Extract tool ID from drag event data (for tool nodes).
 * @param event - The drag event
 * @returns The tool ID or null
 */
export function getToolIdFromDrag(event: React.DragEvent): string | null {
  return event.dataTransfer.getData(DRAG_TOOL_ID_TYPE) || null;
}

/**
 * Check if a drag event contains a valid AgentCore component.
 * @param event - The drag event
 * @returns True if the drag contains a valid component
 */
export function isValidComponentDrag(event: React.DragEvent): boolean {
  return event.dataTransfer.types.includes(DRAG_DATA_TYPE);
}
