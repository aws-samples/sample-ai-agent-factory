/**
 * After a verified download, the panel must show the two integrity anchors the recipient needs.
 *
 * Every script in a downloaded bundle calls `verify-integrity.py`, which refuses to run unless
 * EXPECTED_ARTIFACT_MANIFEST_SHA256 is set to the manifest digest from the authenticated API
 * response. Only this browser saw that response, so if the UI does not surface the digest the
 * customer cannot run what they downloaded. Found live by the Python-download release scenario.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DeployActions } from './DeployActions';

const RECEIPT = {
  filename: 'support-agent-python.zip',
  archiveSha256: '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08',  // pragma: allowlist secret
  artifactManifestSha256: 'c0535e4be2b79ffd93291305436bf889314e4a3faec05ecffcbb7df31ad9e51a',  // pragma: allowlist secret
};

function renderActions(extra: Partial<Parameters<typeof DeployActions>[0]>) {
  return render(
    <DeployActions
      canDeploy
      canDownloadCfn
      canPublish={false}
      state="idle"
      isDownloadingCfn={false}
      isExportingPython={false}
      isPublishing={false}
      publishMsg={null}
      onDownloadCfn={() => {}}
      onExportPython={() => {}}
      onPublish={() => {}}
      {...extra}
    />,
  );
}

describe('download integrity receipt', () => {
  it('shows both digests and the exact export line for a Python download', () => {
    renderActions({ pythonDownloadReceipt: RECEIPT });
    const panel = screen.getByTestId('python-download-integrity');
    expect(panel).toHaveTextContent(RECEIPT.filename);
    expect(panel).toHaveTextContent(RECEIPT.archiveSha256);
    expect(panel).toHaveTextContent(`export EXPECTED_ARTIFACT_MANIFEST_SHA256=${RECEIPT.artifactManifestSha256}`);
    expect(panel).toHaveAttribute('role', 'status');
    expect(screen.queryByTestId('cfn-download-integrity')).toBeNull();
  });

  it('shows the CloudFormation receipt beside its own button', () => {
    renderActions({ cfnDownloadReceipt: { ...RECEIPT, filename: 'support-agent-cfn.zip' } });
    expect(screen.getByTestId('cfn-download-integrity')).toHaveTextContent('support-agent-cfn.zip');
    expect(screen.queryByTestId('python-download-integrity')).toBeNull();
  });

  it('shows nothing for a kind whose last export failed, and no receipt before any download', () => {
    renderActions({ pythonDownloadReceipt: RECEIPT, pythonExportError: 'Python export failed (500)' });
    expect(screen.queryByTestId('python-download-integrity')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('Python export failed (500)');
    renderActions({});
    expect(screen.queryByTestId('python-download-integrity')).toBeNull();
    expect(screen.queryByTestId('cfn-download-integrity')).toBeNull();
  });
});
