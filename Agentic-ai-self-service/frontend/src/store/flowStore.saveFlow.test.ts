import { beforeEach, describe, expect, it, vi } from 'vitest';

const { updateFlow } = vi.hoisted(() => ({
  updateFlow: vi.fn(),
}));

vi.mock('../services/api', () => ({
  getApiClient: () => ({ updateFlow }),
  getErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

import { useFlowStore } from './flowStore';

describe('flowStore.saveFlow', () => {
  beforeEach(() => {
    updateFlow.mockReset();
    useFlowStore.setState({ error: null });
  });

  it('rejects when persistence fails so useAutoSave can show its dedicated warning', async () => {
    const failure = new Error('autosave endpoint unavailable');
    updateFlow.mockRejectedValueOnce(failure);

    await expect(
      useFlowStore.getState().saveFlow('flow-1', {} as never),
    ).rejects.toBe(failure);

    expect(useFlowStore.getState().error).toBe(failure.message);
  });

  it('resolves only after the workflow was persisted', async () => {
    updateFlow.mockResolvedValueOnce({ flow: {} });

    await expect(
      useFlowStore.getState().saveFlow('flow-1', {} as never),
    ).resolves.toBeUndefined();

    expect(updateFlow).toHaveBeenCalledWith('flow-1', { workflow: {} });
    expect(useFlowStore.getState().error).toBeNull();
  });
});
