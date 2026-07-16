/**
 * Data source field components for the Knowledge Base config modal.
 * Each component renders the fields specific to its data source type.
 */

/* eslint-disable react-refresh/only-export-components */

import { TextField, SelectField } from '../FormFields';
import type { KnowledgeBaseToolConfig, KBDataSourceType } from '../../../types/components';
import type { ValidationError } from '../ConfigurationModal';

// ============================================================================
// Shared Props
// ============================================================================

export interface DataSourceFieldProps {
  config: KnowledgeBaseToolConfig;
  updateField: <K extends keyof KnowledgeBaseToolConfig>(field: K, value: KnowledgeBaseToolConfig[K]) => void;
  errors: ValidationError[];
}

function getError(errors: ValidationError[], field: string) {
  return errors.find((e) => e.field === field)?.message;
}

function CredentialsInfo({ service }: { service: string }) {
  return (
    <div className="p-2.5 bg-amber-50 rounded-lg border border-amber-200">
      <p className="text-xs text-amber-700">
        Create a secret in AWS Secrets Manager containing {service} authentication credentials.
        Provide the secret ARN below. See the{' '}
        <span className="font-medium">Bedrock Knowledge Base documentation</span> for the required secret format.
      </p>
    </div>
  );
}

// ============================================================================
// S3
// ============================================================================

function DataSourceS3Fields({ config, updateField, errors }: DataSourceFieldProps) {
  return (
    <TextField
      label="S3 Bucket URI"
      id="kb-s3-uri"
      value={config.s3BucketUri || ''}
      onChange={(v) => updateField('s3BucketUri', v)}
      placeholder="s3://my-bucket/documents/"
      required
      helpText="S3 path containing your documents (PDF, TXT, HTML, MD, CSV, DOCX)"
      error={getError(errors, 's3BucketUri')}
    />
  );
}

// ============================================================================
// Web Crawler
// ============================================================================

function DataSourceWebCrawlerFields({ config, updateField, errors }: DataSourceFieldProps) {
  return (
    <>
      <TextField
        label="Seed URL"
        id="kb-web-url"
        value={config.webCrawlerUrl || ''}
        onChange={(v) => updateField('webCrawlerUrl', v)}
        placeholder="https://docs.example.com"
        required
        helpText="Starting URL for the web crawler"
        error={getError(errors, 'webCrawlerUrl')}
      />
      <SelectField
        label="Crawl Scope"
        id="kb-web-scope"
        value={config.webCrawlerScope || 'HOST_ONLY'}
        onChange={(v) => updateField('webCrawlerScope', v as 'HOST_ONLY' | 'SUBDOMAINS')}
        options={[
          { value: 'HOST_ONLY', label: 'Same host only' },
          { value: 'SUBDOMAINS', label: 'Include subdomains' },
        ]}
      />
    </>
  );
}

// ============================================================================
// Confluence
// ============================================================================

function DataSourceConfluenceFields({ config, updateField, errors }: DataSourceFieldProps) {
  return (
    <>
      <CredentialsInfo service="Confluence" />
      <TextField
        label="Confluence URL"
        id="kb-confluence-url"
        value={config.confluenceHostUrl || ''}
        onChange={(v) => updateField('confluenceHostUrl', v)}
        placeholder="https://your-domain.atlassian.net"
        required
        helpText="Your Confluence instance URL"
        error={getError(errors, 'confluenceHostUrl')}
      />
      {/* Note: The Bedrock API currently only supports SAAS hostType for Confluence.
          ON_PREMISE / Data Center is not supported. */}
      <div className="p-2 bg-gray-50 rounded border border-gray-200">
        <p className="text-xs text-gray-600">Host Type: <span className="font-medium">Confluence Cloud (SaaS)</span></p>
      </div>
      <TextField
        label="Credentials Secret ARN"
        id="kb-confluence-secret"
        value={config.confluenceCredentialsSecretArn || ''}
        onChange={(v) => updateField('confluenceCredentialsSecretArn', v)}
        placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-confluence-creds"
        required
        helpText="Secrets Manager ARN containing Confluence API credentials"
        error={getError(errors, 'confluenceCredentialsSecretArn')}
      />
    </>
  );
}

// ============================================================================
// Salesforce
// ============================================================================

function DataSourceSalesforceFields({ config, updateField, errors }: DataSourceFieldProps) {
  return (
    <>
      <CredentialsInfo service="Salesforce" />
      <TextField
        label="Salesforce URL"
        id="kb-salesforce-url"
        value={config.salesforceHostUrl || ''}
        onChange={(v) => updateField('salesforceHostUrl', v)}
        placeholder="https://your-org.salesforce.com"
        required
        helpText="Your Salesforce instance URL"
        error={getError(errors, 'salesforceHostUrl')}
      />
      <TextField
        label="Credentials Secret ARN"
        id="kb-salesforce-secret"
        value={config.salesforceCredentialsSecretArn || ''}
        onChange={(v) => updateField('salesforceCredentialsSecretArn', v)}
        placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-salesforce-creds"
        required
        helpText="Secrets Manager ARN containing Salesforce OAuth credentials"
        error={getError(errors, 'salesforceCredentialsSecretArn')}
      />
    </>
  );
}

// ============================================================================
// SharePoint
// ============================================================================

function DataSourceSharePointFields({ config, updateField, errors }: DataSourceFieldProps) {
  return (
    <>
      <CredentialsInfo service="SharePoint" />
      <TextField
        label="SharePoint Domain"
        id="kb-sharepoint-domain"
        value={config.sharePointDomain || ''}
        onChange={(v) => updateField('sharePointDomain', v)}
        placeholder="your-org"
        required
        helpText="SharePoint Online domain name (without .sharepoint.com)"
        error={getError(errors, 'sharePointDomain')}
      />
      <TextField
        label="Site URLs"
        id="kb-sharepoint-sites"
        value={config.sharePointSiteUrls || ''}
        onChange={(v) => updateField('sharePointSiteUrls', v)}
        placeholder="https://your-org.sharepoint.com/sites/site1"
        required
        helpText="Comma-separated list of SharePoint site URLs to crawl"
        error={getError(errors, 'sharePointSiteUrls')}
      />
      <TextField
        label="Tenant ID"
        id="kb-sharepoint-tenant"
        value={config.sharePointTenantId || ''}
        onChange={(v) => updateField('sharePointTenantId', v)}
        placeholder="12345678-1234-1234-1234-123456789012"
        required
        helpText="Azure AD tenant ID for authentication"
        error={getError(errors, 'sharePointTenantId')}
      />
      <TextField
        label="Credentials Secret ARN"
        id="kb-sharepoint-secret"
        value={config.sharePointCredentialsSecretArn || ''}
        onChange={(v) => updateField('sharePointCredentialsSecretArn', v)}
        placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:my-sharepoint-creds"
        required
        helpText="Secrets Manager ARN with Azure AD app registration credentials"
        error={getError(errors, 'sharePointCredentialsSecretArn')}
      />
    </>
  );
}

// ============================================================================
// Dispatch Map
// ============================================================================

export const DATA_SOURCE_FIELDS_MAP: Record<KBDataSourceType, React.ComponentType<DataSourceFieldProps>> = {
  s3: DataSourceS3Fields,
  web_crawler: DataSourceWebCrawlerFields,
  confluence: DataSourceConfluenceFields,
  salesforce: DataSourceSalesforceFields,
  sharepoint: DataSourceSharePointFields,
};
