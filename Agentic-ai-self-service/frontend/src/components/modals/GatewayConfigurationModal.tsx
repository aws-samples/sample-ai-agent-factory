/**
 * GatewayConfiguration modal for configuring AgentCore Gateway components.
 * Requirements: 4.1, 4.2, 4.3, 4.4
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, TextArea, SelectField, CheckboxField, FormSection } from './FormFields';
import type {
  GatewayConfiguration,
  GatewayTargetType,
  OpenAPITargetConfig,
  LambdaTargetConfig,
  SmithyTargetConfig,
} from '../../types/components';
import {
  TARGET_TYPE_OPTIONS,
  isValidLambdaArn,
  createDefaultGatewayConfig,
  createDefaultTargetConfig,
} from '../../utils/gatewayConfig';

// ============================================================================
// Props Interface
// ============================================================================

export interface GatewayConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: GatewayConfiguration) => void;
  initialConfig?: Partial<GatewayConfiguration>;
}

// ============================================================================
// GatewayConfigurationModal Component
// ============================================================================

export function GatewayConfigurationModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: GatewayConfigurationModalProps) {
  const [config, setConfig] = useState<GatewayConfiguration>(() => ({
    ...createDefaultGatewayConfig(),
    ...initialConfig,
  }));

  // Reset config when modal opens with new initial config
  useEffect(() => {
    if (isOpen) {
      setConfig({
        ...createDefaultGatewayConfig(),
        ...initialConfig,
      });
    }
  }, [isOpen, initialConfig]);

  // Validation
  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.name.trim()) {
      errors.push({ field: 'name', message: 'Name is required' });
    }

    // Target-specific validation
    switch (config.targetType) {
      case 'openapi': {
        const openApiConfig = config.targetConfig as OpenAPITargetConfig;
        if (!openApiConfig.specUrl && !openApiConfig.specContent) {
          errors.push({ field: 'specUrl', message: 'OpenAPI spec URL or content is required' });
        }
        break;
      }
      case 'lambda': {
        const lambdaConfig = config.targetConfig as LambdaTargetConfig;
        if (lambdaConfig.functionArn && !isValidLambdaArn(lambdaConfig.functionArn)) {
          errors.push({ field: 'functionArn', message: 'Invalid Lambda ARN format' });
        }
        break;
      }
    }

    return errors;
  }, [config]);

  // Update handlers
  const updateConfig = useCallback(<K extends keyof GatewayConfiguration>(
    key: K,
    value: GatewayConfiguration[K]
  ) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Handle target type change
  const handleTargetTypeChange = useCallback((targetType: GatewayTargetType) => {
    setConfig((prev) => ({
      ...prev,
      targetType,
      targetConfig: createDefaultTargetConfig(targetType),
    }));
  }, []);

  // Update target config
  const updateTargetConfig = useCallback(<K extends string>(key: K, value: unknown) => {
    setConfig((prev) => ({
      ...prev,
      targetConfig: { ...prev.targetConfig, [key]: value },
    }));
  }, []);

  // Handle save
  const handleSave = useCallback(() => {
    onSave(config);
    onClose();
  }, [config, onSave, onClose]);

  // Get field error
  const getFieldError = (field: string) =>
    validationErrors.find((e) => e.field === field)?.message;

  // Render target-specific fields
  const renderTargetFields = () => {
    switch (config.targetType) {
      case 'openapi':
        return (
          <>
            <TextField
              id="specUrl"
              label="OpenAPI Spec URL"
              value={(config.targetConfig as OpenAPITargetConfig).specUrl || ''}
              onChange={(value) => updateTargetConfig('specUrl', value)}
              placeholder="https://api.example.com/openapi.json"
              error={getFieldError('specUrl')}
              helpText="URL to the OpenAPI specification"
            />
            <TextArea
              id="specContent"
              label="Or paste OpenAPI spec content"
              value={(config.targetConfig as OpenAPITargetConfig).specContent || ''}
              onChange={(value) => updateTargetConfig('specContent', value)}
              placeholder="Paste OpenAPI JSON/YAML here..."
              rows={6}
              helpText="Alternatively, paste the OpenAPI spec directly"
            />
          </>
        );

      case 'lambda':
        return (
          <TextField
            id="functionArn"
            label="Lambda Function ARN"
            value={(config.targetConfig as LambdaTargetConfig).functionArn || ''}
            onChange={(value) => updateTargetConfig('functionArn', value)}
            placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-function"
            error={getFieldError('functionArn')}
            helpText="Leave empty to auto-create a test Lambda function"
          />
        );

      case 'smithy':
        return (
          <SelectField
            id="modelName"
            label="Smithy Model"
            value={(config.targetConfig as SmithyTargetConfig).modelName || 'dynamodb'}
            onChange={(value) => updateTargetConfig('modelName', value)}
            options={[
              { value: 'dynamodb', label: 'DynamoDB' },
            ]}
            helpText="Pre-configured Smithy model"
          />
        );

      default:
        return null;
    }
  };

  // Build tabs
  const tabs = useMemo(() => [
    {
      id: 'general',
      label: 'General',
      hasError: validationErrors.some((e) => e.field === 'name'),
      content: (
        <div className="space-y-6">
          <FormSection title="Basic Information">
            <TextField
              id="name"
              label="Name"
              value={config.name}
              onChange={(value) => updateConfig('name', value)}
              placeholder="Enter gateway name"
              required
              error={getFieldError('name')}
            />

            <SelectField
              id="targetType"
              label="Target Type"
              value={config.targetType}
              onChange={(value) => handleTargetTypeChange(value as GatewayTargetType)}
              options={TARGET_TYPE_OPTIONS}
              required
              helpText="Type of service to expose through the gateway"
            />
          </FormSection>
        </div>
      ),
    },
    {
      id: 'target',
      label: 'Target Configuration',
      hasError: validationErrors.some((e) => ['specUrl', 'functionArn'].includes(e.field)),
      content: (
        <div className="space-y-6">
          <FormSection
            title="Target Settings"
            description={`Configure the ${config.targetType} target`}
          >
            {renderTargetFields()}
          </FormSection>
        </div>
      ),
    },
    {
      id: 'features',
      label: 'Features',
      content: (
        <div className="space-y-6">
          <FormSection
            title="Gateway Features"
            description="Enable or disable gateway features"
          >
            <CheckboxField
              id="enableSemanticSearch"
              label="Enable Semantic Search"
              checked={config.enableSemanticSearch}
              onChange={(checked) => updateConfig('enableSemanticSearch', checked)}
              helpText="Enable semantic search for better tool discovery"
            />
          </FormSection>
        </div>
      ),
    },
  ], [config, validationErrors, updateConfig, handleTargetTypeChange, renderTargetFields, getFieldError]);

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure AgentCore Gateway"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}

export default GatewayConfigurationModal;
