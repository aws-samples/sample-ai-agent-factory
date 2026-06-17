/**
 * KnowledgeBaseConfigModal - Configuration for Bedrock Knowledge Base tool.
 * Supports "Use Existing KB" and "Create New KB" modes.
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { ConfigurationModal, type ValidationError } from './ConfigurationModal';
import { TextField, SelectField, NumberField, FormSection } from './FormFields';
import { DATA_SOURCE_FIELDS_MAP } from './kb/DataSourceFields';
import { VECTOR_STORE_FIELDS_MAP } from './kb/VectorStoreFields';
import { ParsingStrategyFields, TransformationFields, AdvancedSettingsFields } from './kb/AdvancedFields';
import type {
  KnowledgeBaseToolConfig,
  KBMode,
  KBDataSourceType,
  KBChunkingStrategy,
  KBVectorStoreType,
} from '../../types/components';

// ============================================================================
// Props Interface
// ============================================================================

export interface KnowledgeBaseConfigModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (config: KnowledgeBaseToolConfig) => void;
  initialConfig?: Partial<KnowledgeBaseToolConfig>;
}

// ============================================================================
// Option Constants
// ============================================================================

const EMBEDDING_MODELS = [
  { value: 'amazon.titan-embed-text-v2:0', label: 'Amazon Titan Text Embeddings V2' },
  { value: 'cohere.embed-english-v3', label: 'Cohere Embed English v3' },
  { value: 'cohere.embed-multilingual-v3', label: 'Cohere Embed Multilingual v3' },
];

const FOUNDATION_MODELS = [
  // Bedrock-current models (Oct 2025 – May 2026 policy window).
  // Older models (Titan, Llama 3, Mistral Large 2402, Claude 3.x) removed —
  // Bedrock flags them Legacy. See tasks/lessons.md Bug 113.
  { value: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'Claude Sonnet 4.5' },
  { value: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
  { value: 'us.anthropic.claude-opus-4-5-20251101-v1:0', label: 'Claude Opus 4.5' },
  { value: 'us.amazon.nova-2-lite-v1:0', label: 'Amazon Nova 2 Lite' },
  { value: 'us.amazon.nova-premier-v1:0', label: 'Amazon Nova Premier' },
];

const CHUNKING_STRATEGIES: { value: KBChunkingStrategy; label: string; description: string }[] = [
  { value: 'FIXED_SIZE', label: 'Fixed Size', description: 'Split documents into fixed-size chunks with configurable overlap' },
  { value: 'SEMANTIC', label: 'Semantic', description: 'Split at natural semantic boundaries using embeddings' },
  { value: 'HIERARCHICAL', label: 'Hierarchical', description: 'Create parent-child chunk relationships for better context' },
  { value: 'NONE', label: 'None', description: 'Treat each document as a single chunk' },
];

const DATA_SOURCE_OPTIONS = [
  { value: 's3', label: 'Amazon S3' },
  { value: 'web_crawler', label: 'Web Crawler' },
  { value: 'confluence', label: 'Confluence' },
  { value: 'salesforce', label: 'Salesforce' },
  { value: 'sharepoint', label: 'SharePoint' },
];

const VECTOR_STORE_OPTIONS = [
  { value: 's3_vectors', label: 'S3 Vectors (Managed)' },
  { value: 'opensearch_serverless', label: 'OpenSearch Serverless' },
  { value: 'rds', label: 'Aurora PostgreSQL (pgvector)' },
];

// ============================================================================
// Defaults
// ============================================================================

function createDefaultKBConfig(): KnowledgeBaseToolConfig {
  return {
    name: 'knowledge_base',
    toolId: 'knowledge_base',
    description: 'RAG-powered Q&A using Amazon Bedrock Knowledge Bases',
    enabled: true,
    isKnowledgeBase: true,
    kbMode: 'existing',
    knowledgeBaseId: '',
    kbName: '',
    kbDescription: '',
    dataSourceType: 's3',
    s3BucketUri: '',
    webCrawlerUrl: '',
    webCrawlerScope: 'HOST_ONLY',
    chunkingStrategy: 'FIXED_SIZE',
    maxTokens: 300,
    overlapPercentage: 20,
    embeddingModelId: 'amazon.titan-embed-text-v2:0',
    foundationModelId: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
    vectorStoreType: 's3_vectors',
    parsingStrategy: 'default',
    dataDeletionPolicy: 'DELETE',
    retrievalStrategy: 'simple',
  };
}

// Fields that should trigger error indicators on Tab 1
const TAB1_ERROR_FIELDS = [
  'knowledgeBaseId', 'kbName', 'embeddingModelId',
  's3BucketUri', 'webCrawlerUrl',
  'confluenceHostUrl', 'confluenceCredentialsSecretArn',
  'salesforceHostUrl', 'salesforceCredentialsSecretArn',
  'sharePointDomain', 'sharePointSiteUrls', 'sharePointTenantId', 'sharePointCredentialsSecretArn',
  'opensearchCollectionArn', 'opensearchVectorIndexName',
  'rdsResourceArn', 'rdsCredentialsSecretArn', 'rdsDatabaseName', 'rdsTableName',
];

// Fields that should trigger error indicators on Tab 3 (Advanced)
const TAB3_ERROR_FIELDS = [
  'parsingModelId', 'transformationLambdaArn', 'transformationS3Uri', 'kmsKeyArn',
];

// ============================================================================
// Validation Helpers
// ============================================================================

function validateArn(errors: ValidationError[], field: string, value: string | undefined, label: string) {
  if (!value?.trim()) {
    errors.push({ field, message: `${label} is required` });
  } else if (!value.startsWith('arn:aws:')) {
    errors.push({ field, message: `${label} must be a valid AWS ARN` });
  }
}

function validateRequired(errors: ValidationError[], field: string, value: string | undefined, label: string) {
  if (!value?.trim()) {
    errors.push({ field, message: `${label} is required` });
  }
}

// ============================================================================
// KnowledgeBaseConfigModal Component
// ============================================================================

export function KnowledgeBaseConfigModal({
  isOpen,
  onClose,
  onSave,
  initialConfig,
}: KnowledgeBaseConfigModalProps) {
  const [config, setConfig] = useState<KnowledgeBaseToolConfig>(() => ({
    ...createDefaultKBConfig(),
    ...initialConfig,
  }));

  useEffect(() => {
    if (isOpen) {
      setConfig({
        ...createDefaultKBConfig(),
        ...initialConfig,
      });
    }
  }, [isOpen, initialConfig]);

  // ── Validation ──────────────────────────────────────────────────────────

  const validationErrors = useMemo(() => {
    const errors: ValidationError[] = [];

    if (!config.foundationModelId) {
      errors.push({ field: 'foundationModelId', message: 'Foundation model for RAG is required' });
    }

    if (config.kbMode === 'existing') {
      if (!config.knowledgeBaseId?.trim()) {
        errors.push({ field: 'knowledgeBaseId', message: 'Knowledge Base ID is required' });
      } else if (!/^[A-Z0-9]{10}$/i.test(config.knowledgeBaseId.trim())) {
        errors.push({ field: 'knowledgeBaseId', message: 'KB ID must be a 10-character alphanumeric string' });
      }
    }

    if (config.kbMode === 'create_new') {
      if (!config.kbName?.trim()) {
        errors.push({ field: 'kbName', message: 'Knowledge Base name is required' });
      } else if (!/^[a-zA-Z0-9-]+$/.test(config.kbName.trim())) {
        errors.push({ field: 'kbName', message: 'Name must contain only letters, numbers, and hyphens' });
      }
      if (!config.embeddingModelId) {
        errors.push({ field: 'embeddingModelId', message: 'Embedding model is required' });
      }

      // Data source validation
      const ds = config.dataSourceType;
      if (ds === 's3') {
        if (!config.s3BucketUri?.trim()) errors.push({ field: 's3BucketUri', message: 'S3 bucket URI is required' });
        else if (!config.s3BucketUri.startsWith('s3://')) errors.push({ field: 's3BucketUri', message: 'S3 URI must start with s3://' });
      } else if (ds === 'web_crawler') {
        if (!config.webCrawlerUrl?.trim()) errors.push({ field: 'webCrawlerUrl', message: 'Web Crawler URL is required' });
        else if (!config.webCrawlerUrl.startsWith('https://')) errors.push({ field: 'webCrawlerUrl', message: 'URL must start with https://' });
      } else if (ds === 'confluence') {
        validateRequired(errors, 'confluenceHostUrl', config.confluenceHostUrl, 'Confluence URL');
        validateArn(errors, 'confluenceCredentialsSecretArn', config.confluenceCredentialsSecretArn, 'Credentials Secret ARN');
      } else if (ds === 'salesforce') {
        validateRequired(errors, 'salesforceHostUrl', config.salesforceHostUrl, 'Salesforce URL');
        validateArn(errors, 'salesforceCredentialsSecretArn', config.salesforceCredentialsSecretArn, 'Credentials Secret ARN');
      } else if (ds === 'sharepoint') {
        validateRequired(errors, 'sharePointDomain', config.sharePointDomain, 'SharePoint domain');
        validateRequired(errors, 'sharePointSiteUrls', config.sharePointSiteUrls, 'Site URLs');
        validateRequired(errors, 'sharePointTenantId', config.sharePointTenantId, 'Tenant ID');
        validateArn(errors, 'sharePointCredentialsSecretArn', config.sharePointCredentialsSecretArn, 'Credentials Secret ARN');
      }

      // Parsing strategy validation
      if (config.parsingStrategy === 'bedrock_foundation_model' && !config.parsingModelId) {
        errors.push({ field: 'parsingModelId', message: 'Parser model is required for Foundation Model parsing' });
      }

      // Transformation Lambda validation
      if (config.transformationLambdaArn) {
        if (!config.transformationLambdaArn.startsWith('arn:aws:lambda:')) {
          errors.push({ field: 'transformationLambdaArn', message: 'Must be a valid Lambda ARN' });
        }
        if (!config.transformationS3Uri?.trim()) {
          errors.push({ field: 'transformationS3Uri', message: 'Intermediate S3 URI is required when using transformation' });
        } else if (!config.transformationS3Uri.startsWith('s3://')) {
          errors.push({ field: 'transformationS3Uri', message: 'S3 URI must start with s3://' });
        }
      }

      // KMS key validation
      if (config.kmsKeyArn && !config.kmsKeyArn.startsWith('arn:aws:kms:')) {
        errors.push({ field: 'kmsKeyArn', message: 'Must be a valid KMS key ARN' });
      }

      // Vector store validation
      const vs = config.vectorStoreType;
      if (vs === 'opensearch_serverless') {
        validateArn(errors, 'opensearchCollectionArn', config.opensearchCollectionArn, 'Collection ARN');
        validateRequired(errors, 'opensearchVectorIndexName', config.opensearchVectorIndexName, 'Vector index name');
      } else if (vs === 'rds') {
        validateArn(errors, 'rdsResourceArn', config.rdsResourceArn, 'Cluster ARN');
        validateArn(errors, 'rdsCredentialsSecretArn', config.rdsCredentialsSecretArn, 'Credentials Secret ARN');
        validateRequired(errors, 'rdsDatabaseName', config.rdsDatabaseName, 'Database name');
        validateRequired(errors, 'rdsTableName', config.rdsTableName, 'Table name');
      }
    }

    return errors;
  }, [config]);

  // ── Updaters ────────────────────────────────────────────────────────────

  const updateField = useCallback(<K extends keyof KnowledgeBaseToolConfig>(field: K, value: KnowledgeBaseToolConfig[K]) => {
    setConfig((prev) => ({ ...prev, [field]: value }));
  }, []);

  const handleSave = useCallback(() => {
    onSave(config);
  }, [config, onSave]);

  // ── Data Source & Vector Store Field Dispatch ───────────────────────────

  const DataSourceFieldComponent = DATA_SOURCE_FIELDS_MAP[config.dataSourceType || 's3'];
  const VectorStoreFieldComponent = VECTOR_STORE_FIELDS_MAP[config.vectorStoreType || 's3_vectors'];

  // ── Tab 1: Knowledge Base Configuration ─────────────────────────────────

  const kbTab = (
    <div className="space-y-5">
      {/* Mode Selection */}
      <FormSection title="Connection Mode">
        <div className="flex gap-3">
          {(['existing', 'create_new'] as KBMode[]).map((mode) => (
            <button
              key={mode}
              onClick={() => updateField('kbMode', mode)}
              className={`flex-1 px-3 py-2.5 text-sm font-medium rounded-lg border-2 transition-colors ${
                config.kbMode === mode
                  ? 'border-blue-500 bg-blue-50 text-blue-700'
                  : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300'
              }`}
            >
              {mode === 'existing' ? 'Use Existing KB' : 'Create New KB'}
            </button>
          ))}
        </div>
      </FormSection>

      {/* Existing KB Mode */}
      {config.kbMode === 'existing' && (
        <FormSection title="Existing Knowledge Base" description="Provide the ID of an existing Bedrock Knowledge Base.">
          <TextField
            label="Knowledge Base ID"
            id="kb-id"
            value={config.knowledgeBaseId || ''}
            onChange={(v) => updateField('knowledgeBaseId', v)}
            placeholder="ABCDE12345"
            required
            helpText="10-character alphanumeric ID from the AWS Bedrock console"
            error={validationErrors.find((e) => e.field === 'knowledgeBaseId')?.message}
          />
        </FormSection>
      )}

      {/* Create New KB Mode */}
      {config.kbMode === 'create_new' && (
        <>
          <FormSection title="Knowledge Base Details">
            <TextField
              label="Name"
              id="kb-name"
              value={config.kbName || ''}
              onChange={(v) => updateField('kbName', v)}
              placeholder="my-knowledge-base"
              required
              helpText="Letters, numbers, and hyphens only"
              error={validationErrors.find((e) => e.field === 'kbName')?.message}
            />
            <TextField
              label="Description"
              id="kb-description"
              value={config.kbDescription || ''}
              onChange={(v) => updateField('kbDescription', v)}
              placeholder="Knowledge base for product documentation"
            />
          </FormSection>

          <FormSection title="Data Source" description="Where documents are stored for indexing.">
            <SelectField
              label="Source Type"
              id="kb-datasource-type"
              value={config.dataSourceType || 's3'}
              onChange={(v) => updateField('dataSourceType', v as KBDataSourceType)}
              options={DATA_SOURCE_OPTIONS}
            />
            <DataSourceFieldComponent config={config} updateField={updateField} errors={validationErrors} />
          </FormSection>

          <FormSection title="Vector Store" description="Where vector embeddings are stored and searched.">
            <SelectField
              label="Storage Type"
              id="kb-vector-store-type"
              value={config.vectorStoreType || 's3_vectors'}
              onChange={(v) => updateField('vectorStoreType', v as KBVectorStoreType)}
              options={VECTOR_STORE_OPTIONS}
            />
            <VectorStoreFieldComponent config={config} updateField={updateField} errors={validationErrors} />
          </FormSection>

          <FormSection title="Chunking Strategy" description="How documents are split for embedding and retrieval.">
            <div className="space-y-2">
              {CHUNKING_STRATEGIES.map((cs) => (
                <label
                  key={cs.value}
                  className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                    config.chunkingStrategy === cs.value
                      ? 'border-blue-300 bg-blue-50/50'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <input
                    type="radio"
                    name="chunking"
                    checked={config.chunkingStrategy === cs.value}
                    onChange={() => updateField('chunkingStrategy', cs.value)}
                    className="mt-0.5 w-4 h-4 text-blue-600 border-gray-300 focus:ring-blue-500"
                  />
                  <div>
                    <div className="text-sm font-medium text-gray-800">{cs.label}</div>
                    <div className="text-xs text-gray-500">{cs.description}</div>
                  </div>
                </label>
              ))}
            </div>

            {config.chunkingStrategy === 'FIXED_SIZE' && (
              <div className="grid grid-cols-2 gap-4 mt-3">
                <NumberField
                  label="Max Tokens"
                  id="kb-max-tokens"
                  value={config.maxTokens ?? 300}
                  onChange={(v) => updateField('maxTokens', v)}
                  min={20}
                  max={8192}
                  helpText="Tokens per chunk (20-8192)"
                />
                <NumberField
                  label="Overlap %"
                  id="kb-overlap"
                  value={config.overlapPercentage ?? 20}
                  onChange={(v) => updateField('overlapPercentage', v)}
                  min={0}
                  max={99}
                  helpText="Chunk overlap (0-99%)"
                />
              </div>
            )}
          </FormSection>

          <FormSection title="Embedding Model" description="Model used to create vector embeddings of your documents.">
            <SelectField
              label="Embedding Model"
              id="kb-embedding-model"
              value={config.embeddingModelId || 'amazon.titan-embed-text-v2:0'}
              onChange={(v) => updateField('embeddingModelId', v)}
              options={EMBEDDING_MODELS}
              required
            />
          </FormSection>
        </>
      )}
    </div>
  );

  // ── Tab 2: RAG Model ────────────────────────────────────────────────────

  const ragTab = (
    <div className="space-y-5">
      <FormSection
        title="Foundation Model"
        description="The model used to generate answers from retrieved context. This model reads the relevant document chunks and synthesizes a response."
      >
        <SelectField
          label="RAG Foundation Model"
          id="kb-foundation-model"
          value={config.foundationModelId || 'us.anthropic.claude-sonnet-4-5-20250929-v1:0'}
          onChange={(v) => updateField('foundationModelId', v)}
          options={FOUNDATION_MODELS}
          required
          error={validationErrors.find((e) => e.field === 'foundationModelId')?.message}
        />
        <div className="mt-4 p-3 bg-gray-50 rounded-lg border border-gray-200">
          <div className="text-xs font-medium text-gray-700 mb-1">How it works</div>
          <p className="text-xs text-gray-500">
            When a user asks a question, the Knowledge Base retrieves relevant document chunks
            using vector similarity search. The foundation model then reads these chunks and
            generates a comprehensive answer with source citations.
          </p>
        </div>
      </FormSection>

      <FormSection
        title="Retrieval Strategy"
        description="How the agent searches the knowledge base. Multi-hop and Reranked add extra Bedrock calls per query (bounded by max_hops / top_n) for higher answer quality."
      >
        <SelectField
          label="Strategy"
          id="kb-retrieval-strategy"
          value={config.retrievalStrategy || 'simple'}
          onChange={(v) => updateField('retrievalStrategy', v as 'simple' | 'multi_hop' | 'hybrid' | 'reranked')}
          options={[
            { value: 'simple', label: 'Simple (single-shot)' },
            { value: 'multi_hop', label: 'Multi-hop (decompose + chain lookups)' },
            { value: 'hybrid', label: 'Hybrid (vector + keyword)' },
            { value: 'reranked', label: 'Reranked (judge-reordered)' },
          ]}
        />
      </FormSection>
    </div>
  );

  // ── Tab 3: Advanced (only in create_new mode) ──────────────────────────

  const advancedTab = (
    <div className="space-y-5">
      <FormSection title="Parsing Strategy" description="How documents are parsed before chunking and embedding.">
        <ParsingStrategyFields config={config} updateField={updateField} errors={validationErrors} />
      </FormSection>

      <FormSection title="Transformation Function" description="Optional Lambda function for custom chunking and metadata processing.">
        <TransformationFields config={config} updateField={updateField} errors={validationErrors} />
      </FormSection>

      <FormSection title="Advanced Settings">
        <AdvancedSettingsFields config={config} updateField={updateField} errors={validationErrors} />
      </FormSection>
    </div>
  );

  // ── Tabs ─────────────────────────────────────────────────────────────────

  const tabs = [
    {
      id: 'knowledge-base',
      label: 'Knowledge Base',
      content: kbTab,
      hasError: validationErrors.some((e) => TAB1_ERROR_FIELDS.includes(e.field)),
    },
    {
      id: 'rag-model',
      label: 'RAG Model',
      content: ragTab,
      hasError: validationErrors.some((e) => e.field === 'foundationModelId'),
    },
    ...(config.kbMode === 'create_new' ? [{
      id: 'advanced',
      label: 'Advanced',
      content: advancedTab,
      hasError: validationErrors.some((e) => TAB3_ERROR_FIELDS.includes(e.field)),
    }] : []),
  ];

  return (
    <ConfigurationModal
      isOpen={isOpen}
      onClose={onClose}
      onSave={handleSave}
      title="Configure Knowledge Base"
      tabs={tabs}
      validationErrors={validationErrors}
    />
  );
}
