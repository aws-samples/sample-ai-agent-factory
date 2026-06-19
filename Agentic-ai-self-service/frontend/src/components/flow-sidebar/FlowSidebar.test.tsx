/**
 * Unit tests for FlowSidebar component.
 * Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 8.1, 8.2, 8.3
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import type { FlowSummary } from '../../types/flow';

// ============================================================================
// Mocks
// ============================================================================

const mockFetchFlows = vi.fn().mockResolvedValue(undefined);
const mockCreateFlow = vi.fn().mockResolvedValue(undefined);
const mockOpenFlow = vi.fn().mockResolvedValue(undefined);
const mockRenameFlow = vi.fn();
const mockDeleteFlow = vi.fn();

const mockStoreState: Record<string, unknown> = {
  flows: [] as FlowSummary[],
  activeFlowId: null,
  isLoading: false,
  error: null,
  fetchFlows: mockFetchFlows,
  createFlow: mockCreateFlow,
  openFlow: mockOpenFlow,
  renameFlow: mockRenameFlow,
  deleteFlow: mockDeleteFlow,
};

vi.mock('../../store/flowStore', () => ({
  useFlowStore: vi.fn((selector?: (state: any) => any) => {
    if (selector) return selector(mockStoreState);
    return mockStoreState;
  }),
}));

// ============================================================================
// Import component after mocks
// ============================================================================

import { FlowSidebar } from './FlowSidebar';

// ============================================================================
// Helpers
// ============================================================================

const sampleFlows: FlowSummary[] = [
  {
    id: 'flow-1',
    name: 'My First Flow',
    deploymentStatus: 'not_deployed',
    createdAt: '2024-01-01T00:00:00.000Z',
    updatedAt: '2024-01-02T00:00:00.000Z',
  },
  {
    id: 'flow-2',
    name: 'Second Flow',
    deploymentStatus: 'deployed',
    createdAt: '2024-02-01T00:00:00.000Z',
    updatedAt: '2024-02-05T00:00:00.000Z',
  },
];

/**
 * Render the sidebar and flush the mount's async `fetchFlows().then(...)` so the
 * resulting state update happens inside act() (no "not wrapped in act" warning).
 */
async function renderSidebar() {
  const result = render(<FlowSidebar />);
  await act(async () => { await Promise.resolve(); });
  return result;
}


// ============================================================================
// Tests
// ============================================================================

describe('FlowSidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStoreState.flows = [];
    mockStoreState.activeFlowId = null;
    mockStoreState.isLoading = false;
    mockStoreState.error = null;
  });

  // --------------------------------------------------------------------------
  // Test 1: fetchFlows is called on mount
  // Validates: Requirements 8.1, 2.6
  // --------------------------------------------------------------------------
  it('should call fetchFlows on mount', async () => {
    await renderSidebar();
    expect(mockFetchFlows).toHaveBeenCalledOnce();
  });

  // --------------------------------------------------------------------------
  // Test 2: Flows are rendered when loaded
  // Validates: Requirements 2.5, 8.2
  // --------------------------------------------------------------------------
  it('should render flow items when flows are loaded', async () => {
    mockStoreState.flows = sampleFlows;

    await renderSidebar();

    const items = screen.getAllByTestId('flow-sidebar-item');
    expect(items).toHaveLength(2);
    expect(screen.getByText('My First Flow')).toBeInTheDocument();
    expect(screen.getByText('Second Flow')).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 3: Clicking header toggles expanded/collapsed state
  // Validates: Requirements 2.2, 2.3, 2.4
  // --------------------------------------------------------------------------
  it('should toggle expanded/collapsed state when header is clicked', async () => {
    mockStoreState.flows = sampleFlows;

    await renderSidebar();

    // Initially expanded — list should be visible
    expect(screen.getByTestId('flow-sidebar-list')).toBeInTheDocument();

    // Click header to collapse
    fireEvent.click(screen.getByTestId('flow-sidebar-header'));
    expect(screen.queryByTestId('flow-sidebar-list')).not.toBeInTheDocument();

    // Click header again to expand
    fireEvent.click(screen.getByTestId('flow-sidebar-header'));
    expect(screen.getByTestId('flow-sidebar-list')).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 4: "+" button reveals an inline input and calls createFlow on confirm
  // Validates: Requirements 3.1, 3.2, 3.3
  // --------------------------------------------------------------------------
  it('should reveal an inline input and call createFlow with the typed name', async () => {
    // Seed a flow so the auto-create-default effect doesn't fire and call
    // createFlow itself — we want to assert on the explicit create action.
    mockStoreState.flows = sampleFlows;
    mockStoreState.activeFlowId = 'flow-1';

    await renderSidebar();

    // No inline input until the "+" is clicked.
    expect(screen.queryByTestId('flow-sidebar-create-input')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('flow-sidebar-create'));

    const input = screen.getByTestId('flow-sidebar-create-input');
    fireEvent.change(input, { target: { value: 'New Flow' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(mockCreateFlow).toHaveBeenCalledWith('New Flow');
    // Input closes after confirming.
    expect(screen.queryByTestId('flow-sidebar-create-input')).not.toBeInTheDocument();
  });

  it('cancels inline create on Escape without calling createFlow', async () => {
    mockStoreState.flows = sampleFlows;
    mockStoreState.activeFlowId = 'flow-1';

    await renderSidebar();

    fireEvent.click(screen.getByTestId('flow-sidebar-create'));
    const input = screen.getByTestId('flow-sidebar-create-input');
    fireEvent.change(input, { target: { value: 'Discarded' } });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(mockCreateFlow).not.toHaveBeenCalled();
    expect(screen.queryByTestId('flow-sidebar-create-input')).not.toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 5: Loading state renders correctly
  // Validates: Requirements 8.2
  // --------------------------------------------------------------------------
  it('should show loading indicator when isLoading is true', async () => {
    mockStoreState.isLoading = true;

    await renderSidebar();

    expect(screen.getByTestId('flow-sidebar-loading')).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 6: Error state renders correctly
  // Validates: Requirements 8.3
  // --------------------------------------------------------------------------
  it('should show error message when error is set', async () => {
    mockStoreState.error = 'Something went wrong';

    await renderSidebar();

    const errorEl = screen.getByTestId('flow-sidebar-error');
    expect(errorEl).toBeInTheDocument();
    expect(errorEl.textContent).toContain('Something went wrong');
  });

  // --------------------------------------------------------------------------
  // Test 7: Active flow is highlighted
  // Validates: Requirements 4.4
  // --------------------------------------------------------------------------
  it('should highlight the active flow item', async () => {
    mockStoreState.flows = sampleFlows;
    mockStoreState.activeFlowId = 'flow-1';

    await renderSidebar();

    const items = screen.getAllByTestId('flow-sidebar-item');
    // First item (flow-1) should have the active accent class
    expect(items[0].className).toContain('bg-[#0972d3]');
    // Second item (flow-2) should NOT have the active accent class
    expect(items[1].className).not.toContain('bg-[#0972d3]');
  });
});
