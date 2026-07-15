/**
 * WorkflowCanvas component - Main visual workflow editor using React Flow.
 * Implements canvas with grid background, zoom controls, and custom node types.
 * Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 8.4, 10.1, 10.2, 12.3
 */

import { useCallback, useMemo, useEffect, useState, useRef } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  BackgroundVariant,
  useReactFlow,
  type OnConnect,
  type NodeTypes,
  type EdgeTypes,
  type Viewport,
  type Node,
  type Connection,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import AgentCoreNode from '../nodes/AgentCoreNode';
import ConnectionEdge from '../edges/ConnectionEdge';
import { useWorkflowStore, type AgentCoreNodeData } from '../../store/workflowStore';
import {
  areComponentsCompatible,
  determineConnectionType,
} from '../../utils/edges';
import {
  calculateDropPosition,
  calculateGhostPosition,
  createNodeFromDrop,
  getComponentTypeFromDrag,
  getToolIdFromDrag,
  isValidComponentDrag,
  DRAG_DATA_TYPE,
  type DragState,
  initialDragState,
} from '../../utils/dragDrop';
import type { AgentCoreComponentType } from '../../types/workflow';
import { PALETTE_ITEMS } from '../palette/ComponentPalette';

// ============================================================================
// Custom Node Types Registration
// ============================================================================

const nodeTypes: NodeTypes = {
  runtime: AgentCoreNode,
  gateway: AgentCoreNode,
  memory: AgentCoreNode,
  code_interpreter: AgentCoreNode,
  browser: AgentCoreNode,
  observability: AgentCoreNode,
  identity: AgentCoreNode,
  evaluation: AgentCoreNode,
  policy: AgentCoreNode,
  guardrails: AgentCoreNode,
  a2a: AgentCoreNode,
  tool: AgentCoreNode,
};

// ============================================================================
// Custom Edge Types Registration
// ============================================================================

const edgeTypes: EdgeTypes = {
  connection: ConnectionEdge,
};

// ============================================================================
// MiniMap Node Color Function
// ============================================================================

// Minimap fills must be literal hex (SVG fill can't read CSS vars). These MIRROR
// the canonical --node-* tokens in index.css / NODE_ACCENT so the minimap and
// the node cards are the same color for a given type (previously they diverged).
const MINIMAP_COLORS: Record<string, string> = {
  runtime: '#0972d3',
  gateway: '#037f0c',
  memory: '#0972d3',
  code_interpreter: '#d45b07',
  browser: '#5b48d3',
  observability: '#c41367',
  identity: '#7d2bd0',
  evaluation: '#037f0c',
  policy: '#d91515',
  guardrails: '#d91515',
  a2a: '#067a6e',
  tool: '#d45b07',
};

const getMinimapNodeColor = (node: Node<AgentCoreNodeData>): string => {
  const componentType = node.data?.componentType;
  return componentType ? MINIMAP_COLORS[componentType] || '#5f6b7a' : '#5f6b7a';
};

// ============================================================================
// Props Interface
// ============================================================================

export interface WorkflowCanvasProps {
  onViewportChange?: (viewport: Viewport) => void;
  onNodeDelete?: (nodeId: string) => void;
  onEdgeDelete?: (edgeId: string) => void;
  onNodeCreate?: (componentType: AgentCoreComponentType, position: { x: number; y: number }, toolId?: string | null) => void;
  onNodeDoubleClick?: (nodeId: string) => void;
  readOnly?: boolean;
}

// ============================================================================
// WorkflowCanvas Component
// ============================================================================

export function WorkflowCanvas({
  onViewportChange,
  onNodeDelete,
  onEdgeDelete,
  onNodeCreate,
  onNodeDoubleClick,
  readOnly = false
}: WorkflowCanvasProps) {
  const {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    setViewport,
    selectNode,
    selectEdge,
    addEdge,
    addNode,
    deleteNode,
    deleteEdge,
    selectedNodeId,
    selectedEdgeId,
    isReadyToDeploy,
    validationState,
    undo,
    redo,
    canUndo,
    canRedo,
  } = useWorkflowStore();

  const reactFlowInstance = useReactFlow();
  const canvasRef = useRef<HTMLDivElement>(null);

  // Drag state for ghost preview
  const [dragState, setDragState] = useState<DragState>(initialDragState);

  // Handle Delete key press for node/edge deletion
  // Requirement 1.7: WHEN a user presses Delete with a Component_Node selected,
  // THE Workflow_Canvas SHALL remove the node and all its connections
  // Handle Undo/Redo keyboard shortcuts
  // Requirement 10.1: WHEN a user presses Ctrl+Z (or Cmd+Z on Mac), THE Workflow_Canvas SHALL undo the last action
  // Requirement 10.2: WHEN a user presses Ctrl+Shift+Z (or Cmd+Shift+Z on Mac), THE Workflow_Canvas SHALL redo the last undone action
  useEffect(() => {
    if (readOnly) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      // Don't handle shortcuts if user is typing in an input
      if (
        event.target instanceof HTMLInputElement ||
        event.target instanceof HTMLTextAreaElement
      ) {
        return;
      }

      // Check for undo: Ctrl+Z (Windows/Linux) or Cmd+Z (Mac)
      if ((event.ctrlKey || event.metaKey) && event.key === 'z' && !event.shiftKey) {
        event.preventDefault();
        if (canUndo) {
          undo();
        }
        return;
      }

      // Check for redo: Ctrl+Shift+Z (Windows/Linux) or Cmd+Shift+Z (Mac)
      if ((event.ctrlKey || event.metaKey) && event.key === 'z' && event.shiftKey) {
        event.preventDefault();
        if (canRedo) {
          redo();
        }
        return;
      }

      // Check for Delete or Backspace key
      if (event.key === 'Delete' || event.key === 'Backspace') {
        // Delete selected node
        if (selectedNodeId) {
          deleteNode(selectedNodeId);
          onNodeDelete?.(selectedNodeId);
          event.preventDefault();
        }
        // Delete selected edge
        else if (selectedEdgeId) {
          deleteEdge(selectedEdgeId);
          onEdgeDelete?.(selectedEdgeId);
          event.preventDefault();
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [readOnly, selectedNodeId, selectedEdgeId, deleteNode, deleteEdge, onNodeDelete, onEdgeDelete, undo, redo, canUndo, canRedo]);

  // Handle viewport changes (pan and zoom)
  const handleMoveEnd = useCallback(
    (_event: MouseEvent | TouchEvent | null, viewport: Viewport) => {
      setViewport(viewport);
      onViewportChange?.(viewport);
    },
    [setViewport, onViewportChange]
  );

  // Handle new connections with compatibility checking
  // Requirements: 2.1, 2.2, 2.3
  const handleConnect: OnConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;

      // Find source and target nodes
      const sourceNode = nodes.find((n) => n.id === connection.source);
      const targetNode = nodes.find((n) => n.id === connection.target);

      if (!sourceNode || !targetNode) return;

      // Check compatibility
      const sourceType = sourceNode.data.componentType;
      const targetType = targetNode.data.componentType;

      if (!areComponentsCompatible(sourceType, targetType)) {
        // Incompatible connection - do not create edge
        return;
      }

      // Determine connection type for color coding
      const connectionType = determineConnectionType(sourceType, targetType);

      const newEdge = {
        id: `edge-${connection.source}-${connection.target}-${Date.now()}`,
        source: connection.source,
        target: connection.target,
        sourceHandle: connection.sourceHandle || null,
        targetHandle: connection.targetHandle || null,
        type: 'connection',
        data: {
          connectionType,
        },
      };
      addEdge(newEdge);
    },
    [nodes, addEdge]
  );

  // Handle node selection
  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node<AgentCoreNodeData>) => {
      selectNode(node.id);
    },
    [selectNode]
  );

  // Handle node double-click to open configuration
  const handleNodeDoubleClick = useCallback(
    (_event: React.MouseEvent, node: Node<AgentCoreNodeData>) => {
      onNodeDoubleClick?.(node.id);
    },
    [onNodeDoubleClick]
  );

  // Handle edge selection
  const handleEdgeClick = useCallback(
    (_event: React.MouseEvent, edge: { id: string }) => {
      selectEdge(edge.id);
    },
    [selectEdge]
  );

  // Handle canvas click (deselect)
  const handlePaneClick = useCallback(() => {
    selectNode(null);
    selectEdge(null);
  }, [selectNode, selectEdge]);

  // ============================================================================
  // Drag-Drop Handlers
  // ============================================================================

  // Handle drag over canvas
  const handleDragOver = useCallback(
    (event: React.DragEvent) => {
      if (readOnly || !isValidComponentDrag(event)) return;

      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';

      // Update ghost position
      if (canvasRef.current) {
        const rect = canvasRef.current.getBoundingClientRect();
        const ghostPos = calculateGhostPosition(event.clientX, event.clientY, rect);
        const componentType = event.dataTransfer.types.includes(DRAG_DATA_TYPE)
          ? (dragState.componentType || 'runtime')
          : null;

        setDragState({
          isDragging: true,
          componentType: componentType as AgentCoreComponentType,
          ghostPosition: ghostPos,
        });
      }
    },
    [readOnly, dragState.componentType]
  );

  // Handle drag enter
  const handleDragEnter = useCallback(
    (event: React.DragEvent) => {
      if (readOnly || !isValidComponentDrag(event)) return;
      event.preventDefault();
    },
    [readOnly]
  );

  // Handle drag leave
  const handleDragLeave = useCallback(
    (event: React.DragEvent) => {
      // Only reset if leaving the canvas entirely
      if (canvasRef.current && !canvasRef.current.contains(event.relatedTarget as HTMLElement)) {
        setDragState(initialDragState);
      }
    },
    []
  );

  // Handle drop on canvas
  // Requirement 1.2: WHEN a user drags a component from the Component_Palette onto the Workflow_Canvas,
  // THE Workflow_Canvas SHALL create a new Component_Node at the drop location
  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      if (readOnly) return;

      event.preventDefault();

      const componentType = getComponentTypeFromDrag(event);
      if (!componentType || !canvasRef.current) {
        setDragState(initialDragState);
        return;
      }

      // Get canvas rect and current viewport
      const rect = canvasRef.current.getBoundingClientRect();
      const currentViewport = reactFlowInstance.getViewport();

      // Calculate drop position in canvas coordinates
      const position = calculateDropPosition(
        event.clientX,
        event.clientY,
        rect,
        currentViewport
      );

      // Extract toolId for tool nodes
      const toolId = getToolIdFromDrag(event);

      // Create and add the new node
      const newNode = createNodeFromDrop(componentType, position, toolId);
      addNode(newNode);

      // Notify parent (pass toolId so connector tool nodes can open their modal)
      onNodeCreate?.(componentType, position, toolId);

      // Reset drag state
      setDragState(initialDragState);
    },
    [readOnly, reactFlowInstance, addNode, onNodeCreate]
  );

  // Default viewport
  const defaultViewport = useMemo(() => ({ x: 0, y: 0, zoom: 1 }), []);

  // Get ghost preview item info
  const ghostItem = dragState.componentType
    ? PALETTE_ITEMS.find((item) => item.type === dragState.componentType)
    : null;

  return (
    <div
      ref={canvasRef}
      className="no-darkmap w-full h-full relative"
      style={{ background: 'var(--canvas-bg)' }}
      data-testid="workflow-canvas"
      onDragOver={handleDragOver}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={handleConnect}
        onMoveEnd={handleMoveEnd}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onEdgeClick={handleEdgeClick}
        onPaneClick={handlePaneClick}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        defaultViewport={defaultViewport}
        defaultEdgeOptions={{ type: 'connection' }}
        fitView={false}
        panOnDrag={!readOnly}
        zoomOnScroll={!readOnly}
        zoomOnPinch={!readOnly}
        zoomOnDoubleClick={false}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable={!readOnly}
        minZoom={0.1}
        maxZoom={4}
        deleteKeyCode={null} // We handle deletion ourselves
      >
        {/* Grid Background — dot color themed via CSS (.react-flow__background),
            bg transparent so the themed canvas wrapper shows through. */}
        <Background
          variant={BackgroundVariant.Dots}
          gap={28}
          size={1}
          color="transparent"
        />

        {/* Zoom Controls */}
        <Controls
          showZoom={true}
          showFitView={true}
          showInteractive={false}
          position="bottom-right"
        />

        {/* Minimap for navigation */}
        <MiniMap
          nodeColor={getMinimapNodeColor}
          nodeStrokeWidth={3}
          maskColor="rgba(6, 8, 15, 0.7)"
          bgColor="#0b1220"
          zoomable
          pannable
          position="bottom-left"
        />
      </ReactFlow>

      {/* Ghost Preview during drag */}
      {dragState.isDragging && dragState.ghostPosition && ghostItem && (
        <div
          className="absolute pointer-events-none z-50 opacity-70"
          style={{
            left: dragState.ghostPosition.x - 75,
            top: dragState.ghostPosition.y - 30,
          }}
          data-testid="drag-ghost-preview"
        >
          <div className="px-4 py-3 rounded-lg border-2 shadow-md min-w-[150px] bg-white border-gray-300">
            <div className="flex items-center gap-2">
              <span className="text-xl">{ghostItem.customIcon || '🔧'}</span>
              <div className="flex-1">
                <div className="font-medium text-gray-800 text-sm">{ghostItem.label}</div>
                <div className="text-xs text-gray-500 capitalize">{ghostItem.type}</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Ready-to-Deploy Indicator */}
      {/* Requirement 8.4: WHEN all components are valid and properly connected,
          THE Workflow_Canvas SHALL display a ready-to-deploy indicator */}
      {nodes.length > 0 && (
        <div
          className="absolute top-4 right-4 z-40"
          data-testid="deploy-status-indicator"
        >
          {isReadyToDeploy ? (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 bg-white text-emerald-700 rounded-md border border-emerald-300 shadow-sm text-xs font-medium">
              <span>✓</span>
              <span>Ready to Deploy</span>
            </div>
          ) : validationState && validationState.errors.length > 0 ? (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 bg-white text-red-600 rounded-md border border-red-300 shadow-sm text-xs font-medium">
              <span>✗</span>
              <span>
                {validationState.errors.length} Error{validationState.errors.length !== 1 ? 's' : ''}
              </span>
            </div>
          ) : validationState && validationState.warnings.length > 0 ? (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 bg-white text-amber-600 rounded-md border border-amber-300 shadow-sm text-xs font-medium">
              <span>⚠</span>
              <span>
                {validationState.warnings.length} Warning{validationState.warnings.length !== 1 ? 's' : ''}
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 bg-white text-[#5f6b7a] rounded-md border border-[#e9ebed] shadow-sm text-xs font-medium">
              <span>○</span>
              <span>Validation Pending</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Wrapper Component with ReactFlowProvider
// ============================================================================

export function WorkflowCanvasWithProvider(props: WorkflowCanvasProps) {
  return (
    <ReactFlowProvider>
      <WorkflowCanvas {...props} />
    </ReactFlowProvider>
  );
}

export default WorkflowCanvasWithProvider;
