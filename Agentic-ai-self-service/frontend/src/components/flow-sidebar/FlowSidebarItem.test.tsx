/**
 * Unit and property-based tests for FlowSidebarItem component.
 * Requirements: 4.1, 4.4, 5.1, 6.1, 6.2
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import fc from 'fast-check';
import type { FlowSummary } from '../../types/flow';
import type { DeploymentStatus } from '../../types/workflow';
import { FlowSidebarItem } from './FlowSidebarItem';

// ============================================================================
// Arbitraries
// ============================================================================

const deploymentStatusArb: fc.Arbitrary<DeploymentStatus> = fc.constantFrom(
  'not_deployed',
  'deploying',
  'deployed',
  'failed',
);

const isoDateArb: fc.Arbitrary<string> = fc
  .integer({ min: new Date('2020-01-01').getTime(), max: new Date('2030-01-01').getTime() })
  .map((ts) => new Date(ts).toISOString());

const flowSummaryArb: fc.Arbitrary<FlowSummary> = fc.record({
  id: fc.uuid(),
  name: fc.string({ minLength: 1, maxLength: 50 }).filter((s) => s.trim().length > 0),
  deploymentStatus: deploymentStatusArb,
  createdAt: isoDateArb,
  updatedAt: isoDateArb,
});

// ============================================================================
// Helpers
// ============================================================================

type OnOpenFn = (id: string) => void;
type OnRenameFn = (id: string, currentName: string) => void;
type OnDeleteFn = (id: string) => void;

function renderItem(
  flow: FlowSummary,
  overrides: {
    isActive?: boolean;
    onOpen?: OnOpenFn;
    onRename?: OnRenameFn;
    onDelete?: OnDeleteFn;
  } = {},
) {
  return render(
    <FlowSidebarItem
      flow={flow}
      isActive={overrides.isActive ?? false}
      onOpen={overrides.onOpen ?? vi.fn<OnOpenFn>()}
      onRename={overrides.onRename ?? vi.fn<OnRenameFn>()}
      onDelete={overrides.onDelete ?? vi.fn<OnDeleteFn>()}
    />,
  );
}

// ============================================================================
// Property: Clicking the row calls onOpen with the flow id
// Validates: Requirements 4.1
// ============================================================================

describe('Property: clicking the row calls onOpen with the flow id', () => {
  it('should call onOpen with flow.id when the row is clicked', { timeout: 15000 }, () => {
    fc.assert(
      fc.property(flowSummaryArb, (flow) => {
        const onOpen = vi.fn();
        const { unmount } = renderItem(flow, { onOpen });

        fireEvent.click(screen.getByTestId('flow-sidebar-item'));
        expect(onOpen).toHaveBeenCalledOnce();
        expect(onOpen).toHaveBeenCalledWith(flow.id);

        unmount();
      }),
      { numRuns: 50 },
    );
  });
});

// ============================================================================
// Property: Active flow has distinct styling
// Validates: Requirements 4.4
// ============================================================================

describe('Property: active flow has distinct styling', () => {
  it('should contain the active accent class when isActive is true', { timeout: 15000 }, () => {
    fc.assert(
      fc.property(flowSummaryArb, (flow) => {
        const { unmount } = renderItem(flow, { isActive: true });

        const row = screen.getByTestId('flow-sidebar-item');
        expect(row.className).toContain('bg-[#0972d3]');

        unmount();
      }),
      { numRuns: 50 },
    );
  });

  it('should NOT contain the active accent class when isActive is false', { timeout: 15000 }, () => {
    fc.assert(
      fc.property(flowSummaryArb, (flow) => {
        const { unmount } = renderItem(flow, { isActive: false });

        const row = screen.getByTestId('flow-sidebar-item');
        expect(row.className).not.toContain('bg-[#0972d3]');

        unmount();
      }),
      { numRuns: 50 },
    );
  });
});


// ============================================================================
// Test: Pen icon triggers onRename
// Validates: Requirements 5.1
// ============================================================================

describe('Test: pen icon triggers onRename', () => {
  let onRename: ReturnType<typeof vi.fn<OnRenameFn>>;
  const flow: FlowSummary = {
    id: 'test-id-123',
    name: 'My Test Flow',
    deploymentStatus: 'not_deployed',
    createdAt: '2024-01-01T00:00:00.000Z',
    updatedAt: '2024-01-01T00:00:00.000Z',
  };

  beforeEach(() => {
    onRename = vi.fn<OnRenameFn>();
  });

  it('should call onRename with (flow.id, flow.name) when edit button is clicked', () => {
    renderItem(flow, { onRename });

    fireEvent.click(screen.getByTestId('flow-sidebar-item-edit'));
    expect(onRename).toHaveBeenCalledOnce();
    expect(onRename).toHaveBeenCalledWith(flow.id, flow.name);
  });
});

// ============================================================================
// Test: Trash icon shows delete confirmation dialog
// Validates: Requirements 6.1, 6.2
// ============================================================================

describe('Test: trash icon shows delete confirmation dialog', () => {
  const flow: FlowSummary = {
    id: 'delete-id-456',
    name: 'Flow To Delete',
    deploymentStatus: 'deployed',
    createdAt: '2024-06-01T00:00:00.000Z',
    updatedAt: '2024-06-15T00:00:00.000Z',
  };

  it('should show the DeleteConfirmDialog when delete button is clicked', () => {
    renderItem(flow);

    // Dialog should not be visible initially
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('flow-sidebar-item-delete'));

    // Dialog should now be visible
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Delete Flow')).toBeInTheDocument();
  });
});

// ============================================================================
// Test: Confirming delete calls onDelete
// Validates: Requirements 6.1, 6.2
// ============================================================================

describe('Test: confirming delete calls onDelete', () => {
  let onDelete: ReturnType<typeof vi.fn<OnDeleteFn>>;
  const flow: FlowSummary = {
    id: 'confirm-delete-789',
    name: 'Flow To Confirm Delete',
    deploymentStatus: 'not_deployed',
    createdAt: '2024-03-01T00:00:00.000Z',
    updatedAt: '2024-03-10T00:00:00.000Z',
  };

  beforeEach(() => {
    onDelete = vi.fn<OnDeleteFn>();
  });

  it('should call onDelete with flow.id when the Delete confirm button is clicked', () => {
    renderItem(flow, { onDelete });

    // Open the dialog
    fireEvent.click(screen.getByTestId('flow-sidebar-item-delete'));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();

    // Click the "Delete" confirm button inside the dialog
    const confirmButton = within(dialog).getByRole('button', { name: 'Delete' });
    fireEvent.click(confirmButton);

    expect(onDelete).toHaveBeenCalledOnce();
    expect(onDelete).toHaveBeenCalledWith(flow.id);
  });
});
