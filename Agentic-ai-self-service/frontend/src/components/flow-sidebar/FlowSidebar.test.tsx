/**
 * Unit tests for FlowSidebar component.
 * Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 8.1, 8.2, 8.3
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
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
  it('should call fetchFlows on mount', () => {
    render(<FlowSidebar />);
    expect(mockFetchFlows).toHaveBeenCalledOnce();
  });

  // --------------------------------------------------------------------------
  // Test 2: Flows are rendered when loaded
  // Validates: Requirements 2.5, 8.2
  // --------------------------------------------------------------------------
  it('should render flow items when flows are loaded', () => {
    mockStoreState.flows = sampleFlows;

    render(<FlowSidebar />);

    const items = screen.getAllByTestId('flow-sidebar-item');
    expect(items).toHaveLength(2);
    expect(screen.getByText('My First Flow')).toBeInTheDocument();
    expect(screen.getByText('Second Flow')).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 3: Clicking header toggles expanded/collapsed state
  // Validates: Requirements 2.2, 2.3, 2.4
  // --------------------------------------------------------------------------
  it('should toggle expanded/collapsed state when header is clicked', () => {
    mockStoreState.flows = sampleFlows;

    render(<FlowSidebar />);

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
  // Test 4: "+" button prompts and calls createFlow
  // Validates: Requirements 3.1, 3.2, 3.3
  // --------------------------------------------------------------------------
  it('should prompt for a name and call createFlow when "+" button is clicked', () => {
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('New Flow');

    render(<FlowSidebar />);

    fireEvent.click(screen.getByTestId('flow-sidebar-create'));

    expect(promptSpy).toHaveBeenCalled();
    expect(mockCreateFlow).toHaveBeenCalledWith('New Flow');

    promptSpy.mockRestore();
  });

  // --------------------------------------------------------------------------
  // Test 5: Loading state renders correctly
  // Validates: Requirements 8.2
  // --------------------------------------------------------------------------
  it('should show loading indicator when isLoading is true', () => {
    mockStoreState.isLoading = true;

    render(<FlowSidebar />);

    expect(screen.getByTestId('flow-sidebar-loading')).toBeInTheDocument();
  });

  // --------------------------------------------------------------------------
  // Test 6: Error state renders correctly
  // Validates: Requirements 8.3
  // --------------------------------------------------------------------------
  it('should show error message when error is set', () => {
    mockStoreState.error = 'Something went wrong';

    render(<FlowSidebar />);

    const errorEl = screen.getByTestId('flow-sidebar-error');
    expect(errorEl).toBeInTheDocument();
    expect(errorEl.textContent).toContain('Something went wrong');
  });

  // --------------------------------------------------------------------------
  // Test 7: Active flow is highlighted
  // Validates: Requirements 4.4
  // --------------------------------------------------------------------------
  it('should highlight the active flow item', () => {
    mockStoreState.flows = sampleFlows;
    mockStoreState.activeFlowId = 'flow-1';

    render(<FlowSidebar />);

    const items = screen.getAllByTestId('flow-sidebar-item');
    // First item (flow-1) should have the active accent class
    expect(items[0].className).toContain('bg-[#0972d3]');
    // Second item (flow-2) should NOT have the active accent class
    expect(items[1].className).not.toContain('bg-[#0972d3]');
  });
});
