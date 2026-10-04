import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DeployActions } from './DeployActions';

const base = {
  canDeploy: false,
  canPublish: false,
  state: 'idle' as const,
  isDownloadingCfn: false,
  isExportingPython: false,
  isPublishing: false,
  publishMsg: null,
  onDownloadCfn: vi.fn(),
  onExportPython: vi.fn(),
  onPublish: vi.fn(),
};

describe('DeployActions CloudFormation download availability', () => {
  it('explains the actual blocker instead of always blaming the naming profile', () => {
    render(
      <DeployActions
        {...base}
        canDownloadCfn={false}
        cfnDownloadBlockedReason={'Tag profile "regulated" changed after this workflow captured it.'}
      />,
    );
    const button = screen.getByRole('button', { name: /download cloudformation template/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Tag profile "regulated" changed after this workflow captured it.');
  });

  it('falls back to a neutral reason when none is supplied', () => {
    render(<DeployActions {...base} canDownloadCfn={false} />);
    const button = screen.getByRole('button', { name: /download cloudformation template/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'The CloudFormation download is not available yet');
  });

  it('carries no explanatory title once the download is available', () => {
    render(<DeployActions {...base} canDownloadCfn={true} cfnDownloadBlockedReason={null} />);
    const button = screen.getByRole('button', { name: /download cloudformation template/i });
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute('title');
  });
});
