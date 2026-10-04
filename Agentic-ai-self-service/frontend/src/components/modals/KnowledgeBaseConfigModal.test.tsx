import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { KnowledgeBaseConfigModal } from './KnowledgeBaseConfigModal';

const createNewBase = {
  kbMode: 'create_new' as const,
  kbName: 'customer-docs',
  dataSourceType: 's3' as const,
  s3BucketUri: 's3://customer-docs/input/',
  embeddingModelId: 'amazon.titan-embed-text-v2:0',
  foundationModelId: 'us.anthropic.claude-sonnet-5',
};

describe('KnowledgeBaseConfigModal customer-resource contract', () => {
  it('makes the live authorization tag and secret-copy behavior visible', () => {
    render(
      <KnowledgeBaseConfigModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialConfig={{ knowledgeBaseId: 'ABCDE12345' }}
      />,
    );

    const notice = screen.getByRole('note', {
      name: 'Customer resource authorization',
    });
    expect(notice).toHaveTextContent('AgentCoreFlowsAccess=allow');
    expect(notice).toHaveTextContent('deployment-bound secret');
    expect(notice).toHaveTextContent('source secret is not granted to Bedrock or deleted');
  });

  it('allows the supported platform-managed OpenSearch path', () => {
    const onSave = vi.fn();
    render(
      <KnowledgeBaseConfigModal
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
        initialConfig={{
          ...createNewBase,
          vectorStoreType: 'opensearch_serverless',
          opensearchCollectionArn: '',
          opensearchVectorIndexName: 'bedrock-knowledge-base-default-index',
        }}
      />,
    );

    expect(screen.getByText(/Leave Collection ARN blank/)).toBeInTheDocument();
    expect(screen.getByTestId('modal-save-button')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('modal-save-button'));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('explains that a custom S3 Vectors index must already be compatible', () => {
    render(
      <KnowledgeBaseConfigModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialConfig={{
          ...createNewBase,
          vectorStoreType: 's3_vectors',
        }}
      />,
    );

    expect(screen.getByText(/platform creates a deployment-bound vector bucket/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Advanced (custom bucket)' }));
    expect(screen.getByText(/pre-created compatible index/)).toBeInTheDocument();
  });

  it('blocks the unsupported Web Crawler and S3 Vectors combination', () => {
    render(
      <KnowledgeBaseConfigModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialConfig={{
          ...createNewBase,
          dataSourceType: 'web_crawler',
          webCrawlerUrl: 'https://docs.example.com',
          vectorStoreType: 's3_vectors',
        }}
      />,
    );

    expect(screen.getByText(/Web Crawler requires the OpenSearch Serverless vector store/)).toBeInTheDocument();
    expect(screen.getByTestId('modal-save-button')).toBeDisabled();
  });

  it('states that connector credential sources are copied, not delegated', () => {
    render(
      <KnowledgeBaseConfigModal
        isOpen
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialConfig={{
          ...createNewBase,
          dataSourceType: 'confluence',
          confluenceHostUrl: 'https://example.atlassian.net',
          confluenceCredentialsSecretArn:
            'arn:aws:secretsmanager:us-east-1:123456789012:secret:confluence',
          vectorStoreType: 's3_vectors',
        }}
      />,
    );

    expect(screen.getByText(/copies its value into a deployment-bound/)).toBeInTheDocument();
    expect(screen.getByText(/leaves the source unchanged/)).toBeInTheDocument();
  });
});
