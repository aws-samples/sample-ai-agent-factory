/**
 * Deployment modal component for showing deployment progress and results.
 * Requirements: 11.5, 11.6, 11.7
 */

import { useCallback } from 'react';
import type { DeploymentResult } from '../../services/api';

export type DeploymentState =
  | { status: 'idle' }
  | { status: 'configuring' }
  | { status: 'deploying'; progress?: string }
  | { status: 'success'; result: DeploymentResult }
  | { status: 'error'; error: string; details?: string[] };

export interface DeploymentModalProps {
  isOpen: boolean;
  onClose: () => void;
  deploymentState: DeploymentState;
  onDeploy: (region: string) => void;
}

/**
 * Modal for deployment configuration, progress, and results.
 * Requirements: 11.5, 11.6, 11.7
 */
export function DeploymentModal({
  isOpen,
  onClose,
  deploymentState,
  onDeploy,
}: DeploymentModalProps) {
  const handleBackdropClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget && deploymentState.status !== 'deploying') {
        onClose();
      }
    },
    [onClose, deploymentState.status]
  );

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50"
      onClick={handleBackdropClick}
      data-testid="deployment-modal"
    >
      <div className="bg-white rounded-lg shadow-xl mx-4" style={{ width: 'var(--modal-width, 540px)' }}>
        {deploymentState.status === 'configuring' && (
          <DeploymentConfigForm onDeploy={onDeploy} onCancel={onClose} />
        )}
        {deploymentState.status === 'deploying' && (
          <DeploymentProgress progress={deploymentState.progress} />
        )}
        {deploymentState.status === 'success' && (
          <DeploymentSuccess result={deploymentState.result} onClose={onClose} />
        )}
        {deploymentState.status === 'error' && (
          <DeploymentError
            error={deploymentState.error}
            details={deploymentState.details}
            onClose={onClose}
          />
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Sub-components
// ============================================================================

interface DeploymentConfigFormProps {
  onDeploy: (region: string) => void;
  onCancel: () => void;
}

function DeploymentConfigForm({ onDeploy, onCancel }: DeploymentConfigFormProps) {
  const handleSubmit = useCallback(
    (e: React.FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      const formData = new FormData(e.currentTarget);
      const region = formData.get('region') as string;
      onDeploy(region);
    },
    [onDeploy]
  );

  const regions = [
    { value: 'us-east-1', label: 'US East (N. Virginia)' },
    { value: 'us-west-2', label: 'US West (Oregon)' },
    { value: 'eu-west-1', label: 'Europe (Ireland)' },
    { value: 'eu-central-1', label: 'Europe (Frankfurt)' },
    { value: 'ap-northeast-1', label: 'Asia Pacific (Tokyo)' },
    { value: 'ap-southeast-1', label: 'Asia Pacific (Singapore)' },
    { value: 'ap-southeast-2', label: 'Asia Pacific (Sydney)' },
  ];

  return (
    <>
      <div className="px-6 py-4 border-b border-gray-200">
        <div className="flex items-center gap-3">
          <span className="text-2xl">🚀</span>
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Deploy Workflow
            </h2>
            <p className="text-sm text-gray-500">
              Configure deployment settings
            </p>
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit}>
        <div className="px-6 py-4 space-y-4">
          <div>
            <label
              htmlFor="region"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              AWS Region
            </label>
            <select
              id="region"
              name="region"
              defaultValue="us-east-1"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            >
              {regions.map((region) => (
                <option key={region.value} value={region.value}>
                  {region.label}
                </option>
              ))}
            </select>
          </div>

          <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
            <p className="text-sm text-blue-700">
              <strong>Note:</strong> Deployment will create AWS resources including
              IAM roles, Lambda functions, and API Gateway endpoints. Standard AWS
              charges may apply.
            </p>
          </div>
        </div>

        <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors font-medium"
          >
            Deploy
          </button>
        </div>
      </form>
    </>
  );
}

interface DeploymentProgressProps {
  progress?: string;
}

function DeploymentProgress({ progress }: DeploymentProgressProps) {
  return (
    <>
      <div className="px-6 py-4 border-b border-gray-200">
        <div className="flex items-center gap-3">
          <div className="animate-spin text-2xl">⚙️</div>
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Deploying...
            </h2>
            <p className="text-sm text-gray-500">
              Please wait while your workflow is being deployed
            </p>
          </div>
        </div>
      </div>

      <div className="px-6 py-8">
        <div className="flex flex-col items-center">
          {/* Progress bar */}
          <div className="w-full bg-gray-200 rounded-full h-2 mb-4">
            <div
              className="bg-blue-600 h-2 rounded-full animate-pulse"
              style={{ width: '60%' }}
            ></div>
          </div>

          <p className="text-sm text-gray-600">
            {progress || 'Creating AWS resources...'}
          </p>

          <div className="mt-4 text-xs text-gray-400">
            This may take a few minutes
          </div>
        </div>
      </div>
    </>
  );
}

interface DeploymentSuccessProps {
  result: DeploymentResult;
  onClose: () => void;
}

function DeploymentSuccess({ result, onClose }: DeploymentSuccessProps) {
  const handleCopyEndpoint = useCallback(() => {
    if (result.endpoint_url) {
      navigator.clipboard.writeText(result.endpoint_url);
    }
  }, [result.endpoint_url]);

  return (
    <>
      <div className="px-6 py-4 border-b border-gray-200">
        <div className="flex items-center gap-3">
          <span className="text-2xl">✅</span>
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Deployment Successful
            </h2>
            <p className="text-sm text-gray-500">
              Your workflow has been deployed to AWS
            </p>
          </div>
        </div>
      </div>

      <div className="px-6 py-4 space-y-4">
        {/* Endpoint URL */}
        {result.endpoint_url && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Endpoint URL
            </label>
            <div className="flex items-center gap-2">
              <input
                type="text"
                readOnly
                value={result.endpoint_url}
                className="flex-1 px-3 py-2 bg-gray-50 border border-gray-300 rounded-lg text-sm font-mono"
              />
              <button
                onClick={handleCopyEndpoint}
                className="px-3 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
                title="Copy to clipboard"
              >
                📋
              </button>
            </div>
          </div>
        )}

        {/* Deployment ID */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Deployment ID
          </label>
          <p className="text-sm font-mono text-gray-600">{result.deployment_id}</p>
        </div>

        {/* Created Resources */}
        {result.created_resources.length > 0 && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Created Resources ({result.created_resources.length})
            </label>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 max-h-32 overflow-y-auto">
              <ul className="text-sm text-gray-600 space-y-1">
                {result.created_resources.map((resource, index) => (
                  <li key={index} className="font-mono text-xs">
                    {resource}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </div>

      <div className="px-6 py-4 border-t border-gray-200 flex justify-end">
        <button
          onClick={onClose}
          className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors font-medium"
        >
          Done
        </button>
      </div>
    </>
  );
}

interface DeploymentErrorProps {
  error: string;
  details?: string[];
  onClose: () => void;
}

function DeploymentError({ error, details, onClose }: DeploymentErrorProps) {
  return (
    <>
      <div className="px-6 py-4 border-b border-gray-200">
        <div className="flex items-center gap-3">
          <span className="text-2xl">❌</span>
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Deployment Failed
            </h2>
            <p className="text-sm text-gray-500">
              An error occurred during deployment
            </p>
          </div>
        </div>
      </div>

      <div className="px-6 py-4 space-y-4">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <p className="text-sm text-red-700 font-medium">{error}</p>
        </div>

        {details && details.length > 0 && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Error Details
            </label>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 max-h-40 overflow-y-auto">
              <ul className="text-sm text-gray-600 space-y-1">
                {details.map((detail, index) => (
                  <li key={index} className="flex items-start gap-2">
                    <span className="text-red-400 mt-0.5">•</span>
                    <span>{detail}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
          <p className="text-sm text-yellow-700">
            <strong>Note:</strong> Any partially created resources have been
            automatically rolled back.
          </p>
        </div>
      </div>

      <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
        <button
          onClick={onClose}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors font-medium"
        >
          Close
        </button>
      </div>
    </>
  );
}

export default DeploymentModal;
