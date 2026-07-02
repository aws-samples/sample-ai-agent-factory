/**
 * Advanced KB configuration fields: Parsing Strategy, Transformation Lambda,
 * Data Deletion Policy, and KMS Key.
 */

import { TextField, SelectField } from '../FormFields';
import type { KnowledgeBaseToolConfig, KBParsingStrategy } from '../../../types/components';
import type { ValidationError } from '../ConfigurationModal';

export interface AdvancedFieldProps {
  config: KnowledgeBaseToolConfig;
  updateField: <K extends keyof KnowledgeBaseToolConfig>(field: K, value: KnowledgeBaseToolConfig[K]) => void;
  errors: ValidationError[];
}

function getError(errors: ValidationError[], field: string) {
  return errors.find((e) => e.field === field)?.message;
}

// ============================================================================
// Constants
// ============================================================================

const PARSING_STRATEGY_OPTIONS = [
  { value: 'default', label: 'Amazon Bedrock Default Parser' },
  { value: 'bedrock_data_automation', label: 'Data Automation (Multimodal)' },
  { value: 'bedrock_foundation_model', label: 'Foundation Model Parser' },
];

const PARSING_MODELS = [
  // Bedrock-current parsing models. Titan removed (Legacy).
  // See tasks/lessons.md Bug 113.
  // New-generation Claude IDs have no date suffix and no ":N" version suffix
  // (verified live via bedrock-runtime converse, 2026-07-01).
  { value: 'us.anthropic.claude-sonnet-5', label: 'Claude Sonnet 5' },
  { value: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { value: 'us.anthropic.claude-opus-4-8', label: 'Claude Opus 4.8' },
  { value: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
];

const DELETION_POLICY_OPTIONS = [
  { value: 'DELETE', label: 'Delete vector data when data source is deleted' },
  { value: 'RETAIN', label: 'Retain vector data when data source is deleted' },
];

// ============================================================================
// Parsing Strategy Fields
// ============================================================================

export function ParsingStrategyFields({ config, updateField, errors }: AdvancedFieldProps) {
  const strategy = config.parsingStrategy || 'default';

  return (
    <>
      <SelectField
        label="Parsing Strategy"
        id="kb-parsing-strategy"
        value={strategy}
        onChange={(v) => updateField('parsingStrategy', v as KBParsingStrategy)}
        options={PARSING_STRATEGY_OPTIONS}
      />

      {strategy === 'default' && (
        <div className="p-2.5 bg-gray-50 rounded-lg border border-gray-200">
          <p className="text-xs text-gray-600">
            Handles common text formats (Word, Excel, HTML, Markdown, TXT, CSV). Multimodal content is ignored.
          </p>
        </div>
      )}

      {strategy === 'bedrock_data_automation' && (
        <div className="p-2.5 bg-blue-50 rounded-lg border border-blue-200">
          <p className="text-xs text-blue-700">
            Parses PDFs, images, audio, and video. Extracts text, generates descriptions for visuals,
            transcribes audio/video, and creates video summaries.
          </p>
        </div>
      )}

      {strategy === 'bedrock_foundation_model' && (
        <>
          <div className="p-2.5 bg-blue-50 rounded-lg border border-blue-200">
            <p className="text-xs text-blue-700">
              Uses a foundation model for advanced text and image parsing. Ideal for PDFs with complex layouts,
              structured documents, and visually rich content.
            </p>
          </div>
          <SelectField
            label="Parser Model"
            id="kb-parsing-model"
            value={config.parsingModelId || 'us.anthropic.claude-sonnet-5'}
            onChange={(v) => updateField('parsingModelId', v)}
            options={PARSING_MODELS}
            required
            error={getError(errors, 'parsingModelId')}
          />
          <TextField
            label="Custom Parsing Prompt"
            id="kb-parsing-prompt"
            value={config.parsingPrompt || ''}
            onChange={(v) => updateField('parsingPrompt', v)}
            placeholder="Optional: custom instructions for the parser model..."
            helpText="Leave empty to use the default parsing prompt"
          />
        </>
      )}
    </>
  );
}

// ============================================================================
// Transformation Lambda Fields
// ============================================================================

export function TransformationFields({ config, updateField, errors }: AdvancedFieldProps) {
  return (
    <>
      <div className="p-2.5 bg-gray-50 rounded-lg border border-gray-200">
        <p className="text-xs text-gray-600">
          Optional: Use a Lambda function to customize chunking and document metadata processing.
          The function receives parsed documents and returns modified chunks.
        </p>
      </div>
      <TextField
        label="Lambda Function ARN"
        id="kb-transform-lambda"
        value={config.transformationLambdaArn || ''}
        onChange={(v) => updateField('transformationLambdaArn', v)}
        placeholder="arn:aws:lambda:us-east-1:123456789012:function:my-transform"
        helpText="ARN of the Lambda function for custom transformation"
        error={getError(errors, 'transformationLambdaArn')}
      />
      {config.transformationLambdaArn && (
        <TextField
          label="Intermediate S3 URI"
          id="kb-transform-s3"
          value={config.transformationS3Uri || ''}
          onChange={(v) => updateField('transformationS3Uri', v)}
          placeholder="s3://my-bucket/intermediate/"
          required
          helpText="S3 location for intermediate storage during transformation"
          error={getError(errors, 'transformationS3Uri')}
        />
      )}
    </>
  );
}

// ============================================================================
// Advanced Settings Fields (Deletion Policy + KMS)
// ============================================================================

export function AdvancedSettingsFields({ config, updateField }: AdvancedFieldProps) {
  return (
    <>
      <SelectField
        label="Data Deletion Policy"
        id="kb-deletion-policy"
        value={config.dataDeletionPolicy || 'DELETE'}
        onChange={(v) => updateField('dataDeletionPolicy', v as 'DELETE' | 'RETAIN')}
        options={DELETION_POLICY_OPTIONS}
      />
      <TextField
        label="KMS Key ARN"
        id="kb-kms-key"
        value={config.kmsKeyArn || ''}
        onChange={(v) => updateField('kmsKeyArn', v)}
        placeholder="arn:aws:kms:us-east-1:123456789012:key/12345678-..."
        helpText="Optional: KMS key for transient data encryption. Leave empty to use AWS-managed key."
      />
    </>
  );
}
