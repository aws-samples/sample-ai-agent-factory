/**
 * Deploy button component with validation blocking.
 * Requirements: 8.6 - Deployment blocked on validation errors
 * Requirements: 11.5, 11.6, 11.7 - Deployment progress, success, and error handling
 */

import { useState, useCallback } from 'react';
import { useWorkflowStore } from '../../store/workflowStore';
import { ErrorSummaryModal } from './ErrorSummaryModal';
import { DeploymentModal, type DeploymentState } from './DeploymentModal';
import { getApiClient, isApiError, type DeploymentResult } from '../../services/api';

export interface DeployButtonProps {
  workflowId?: string;
  onDeploy?: () => void;
  onDeploySuccess?: (result: DeploymentResult) => void;
  onDeployError?: (error: string) => void;
  className?: string;
}

/**
 * Deploy button that is disabled when validation errors exist.
 * Property 26: Deployment Blocked on Validation Errors
 * For any workflow with validation errors, the deploy action shall be blocked
 * and an error summary shall be displayed.
 */
export function DeployButton({
  workflowId,
  onDeploy,
  onDeploySuccess,
  onDeployError,
  className = ''
}: DeployButtonProps) {
  const [showErrorModal, setShowErrorModal] = useState(false);
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [deploymentState, setDeploymentState] = useState<DeploymentState>({ status: 'idle' });

  const isReadyToDeploy = useWorkflowStore((state) => state.isReadyToDeploy);
  const validationState = useWorkflowStore((state) => state.validationState);
  const nodes = useWorkflowStore((state) => state.nodes);

  const hasErrors = validationState ? validationState.errors.length > 0 : false;
  const hasWarnings = validationState ? validationState.warnings.length > 0 : false;
  const hasNodes = nodes.length > 0;

  /**
   * Handles the deploy action.
   * Requirements: 11.1, 11.5, 11.6, 11.7
   */
  const handleDeploy = useCallback(async (region: string) => {
    if (!workflowId) {
      setDeploymentState({
        status: 'error',
        error: 'No workflow ID available. Please save the workflow first.',
      });
      return;
    }

    setDeploymentState({ status: 'deploying', progress: 'Initiating deployment...' });
    onDeploy?.();

    try {
      const apiClient = getApiClient();

      // Update progress
      setDeploymentState({ status: 'deploying', progress: 'Validating workflow...' });

      // Deploy the workflow
      setDeploymentState({ status: 'deploying', progress: 'Creating AWS resources...' });

      const result = await apiClient.deployWorkflow(workflowId, {
        aws_region: region,
        enable_cloudwatch: true,
        enable_cloudtrail: true,
      });

      if (result.status === 'success') {
        setDeploymentState({ status: 'success', result });
        onDeploySuccess?.(result);
      } else if (result.status === 'failed') {
        setDeploymentState({
          status: 'error',
          error: result.error_message || 'Deployment failed',
        });
        onDeployError?.(result.error_message || 'Deployment failed');
      } else {
        // In progress - this shouldn't happen with sync deployment
        setDeploymentState({ status: 'deploying', progress: 'Deployment in progress...' });
      }
    } catch (error) {
      let errorMessage = 'An unexpected error occurred';
      let errorDetails: string[] | undefined;

      if (isApiError(error)) {
        errorMessage = error.message;
        if (error.details && typeof error.details === 'object') {
          const details = error.details as Record<string, unknown>;
          if (Array.isArray(details.errors)) {
            errorDetails = details.errors as string[];
          }
        }
      } else if (error instanceof Error) {
        errorMessage = error.message;
      }

      setDeploymentState({
        status: 'error',
        error: errorMessage,
        details: errorDetails,
      });
      onDeployError?.(errorMessage);
    }
  }, [workflowId, onDeploy, onDeploySuccess, onDeployError]);

  const handleClick = useCallback(() => {
    if (!hasNodes) {
      // No nodes to deploy
      return;
    }

    if (hasErrors) {
      // Show error summary modal
      setShowErrorModal(true);
      return;
    }

    // Show deployment configuration modal
    setDeploymentState({ status: 'configuring' });
    setShowDeployModal(true);
  }, [hasNodes, hasErrors]);

  const handleCloseErrorModal = useCallback(() => {
    setShowErrorModal(false);
  }, []);

  const handleCloseDeployModal = useCallback(() => {
    setShowDeployModal(false);
    setDeploymentState({ status: 'idle' });
  }, []);

  // Determine button state and styling
  const isDisabled = !hasNodes || hasErrors;

  let buttonStyle = 'bg-blue-600 hover:bg-blue-700 text-white';
  let statusText = 'Deploy';

  if (!hasNodes) {
    buttonStyle = 'bg-gray-300 text-gray-500 cursor-not-allowed';
    statusText = 'Add Components';
  } else if (hasErrors) {
    buttonStyle = 'bg-red-100 text-red-700 border border-red-300 cursor-pointer';
    statusText = `${validationState?.errors.length} Error${validationState?.errors.length !== 1 ? 's' : ''}`;
  } else if (hasWarnings) {
    buttonStyle = 'bg-yellow-100 text-yellow-700 border border-yellow-300 hover:bg-yellow-200';
    statusText = 'Deploy (with warnings)';
  } else if (isReadyToDeploy) {
    buttonStyle = 'bg-green-600 hover:bg-green-700 text-white';
    statusText = 'Deploy';
  }

  return (
    <>
      <button
        onClick={handleClick}
        disabled={isDisabled && !hasErrors}
        className={`
          px-4 py-2 rounded-lg font-medium transition-colors
          flex items-center gap-2
          ${buttonStyle}
          ${className}
        `}
        data-testid="deploy-button"
        aria-disabled={isDisabled}
      >
        {hasErrors ? (
          <span className="text-lg">⚠</span>
        ) : isReadyToDeploy ? (
          <span className="text-lg">🚀</span>
        ) : (
          <span className="text-lg">○</span>
        )}
        <span>{statusText}</span>
      </button>

      {/* Error Summary Modal */}
      <ErrorSummaryModal
        isOpen={showErrorModal}
        onClose={handleCloseErrorModal}
        errors={validationState?.errors ?? []}
        warnings={validationState?.warnings ?? []}
      />

      {/* Deployment Modal */}
      <DeploymentModal
        isOpen={showDeployModal}
        onClose={handleCloseDeployModal}
        deploymentState={deploymentState}
        onDeploy={handleDeploy}
      />
    </>
  );
}

export default DeployButton;
