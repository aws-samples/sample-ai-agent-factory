/**
 * MDX loader map: repo-relative source path -> lazy import of the compiled
 * Markdown module. Kept explicit (no import.meta.glob) so that no file outside
 * the doc index can ever be bundled by accident. A test asserts this map and
 * src/content/docs.ts stay in sync.
 */
import { docTitles } from 'virtual:repo-index';
import type { DocEntry } from '../content/docs';
import type { DocModule } from './types';

type Loader = () => Promise<DocModule>;

export const docLoaders: Record<string, Loader> = {
  'workshop-building-agentic-ai-platform/README.md': () =>
    import('../../../workshop-building-agentic-ai-platform/README.md'),
  'Agentic-ai-self-service/README.md': () => import('../../../Agentic-ai-self-service/README.md'),
  'enterprise-mcp-governance-gateway/README.md': () =>
    import('../../../enterprise-mcp-governance-gateway/README.md'),
  'enterprise-agentic-ai-platform-blueprint/README.md': () =>
    import('../../../enterprise-agentic-ai-platform-blueprint/README.md'),
  'Agentic-ai-self-service/docs/API_REFERENCE.md': () =>
    import('../../../Agentic-ai-self-service/docs/API_REFERENCE.md'),
  'Agentic-ai-self-service/docs/COSTS.md': () => import('../../../Agentic-ai-self-service/docs/COSTS.md'),
  'Agentic-ai-self-service/docs/DATA_RETENTION.md': () =>
    import('../../../Agentic-ai-self-service/docs/DATA_RETENTION.md'),
  'Agentic-ai-self-service/docs/DEPLOYMENT_INTERNALS.md': () =>
    import('../../../Agentic-ai-self-service/docs/DEPLOYMENT_INTERNALS.md'),
  'Agentic-ai-self-service/docs/DEVELOPMENT.md': () =>
    import('../../../Agentic-ai-self-service/docs/DEVELOPMENT.md'),
  'Agentic-ai-self-service/docs/ENTERPRISE_CAPABILITIES.md': () =>
    import('../../../Agentic-ai-self-service/docs/ENTERPRISE_CAPABILITIES.md'),
  'Agentic-ai-self-service/docs/MCP_GATEWAY_INTEGRATION.md': () =>
    import('../../../Agentic-ai-self-service/docs/MCP_GATEWAY_INTEGRATION.md'),
  'Agentic-ai-self-service/docs/OBSERVABILITY.md': () =>
    import('../../../Agentic-ai-self-service/docs/OBSERVABILITY.md'),
  'Agentic-ai-self-service/docs/PERSONAS.md': () => import('../../../Agentic-ai-self-service/docs/PERSONAS.md'),
  'Agentic-ai-self-service/docs/RBAC_ROLLOUT.md': () =>
    import('../../../Agentic-ai-self-service/docs/RBAC_ROLLOUT.md'),
  'Agentic-ai-self-service/docs/REGISTRY_AND_RBAC.md': () =>
    import('../../../Agentic-ai-self-service/docs/REGISTRY_AND_RBAC.md'),
  'Agentic-ai-self-service/docs/SECURITY_HARDENING.md': () =>
    import('../../../Agentic-ai-self-service/docs/SECURITY_HARDENING.md'),
  'Agentic-ai-self-service/CHANGELOG.md': () => import('../../../Agentic-ai-self-service/CHANGELOG.md'),
  'enterprise-mcp-governance-gateway/connectors/atlassian/README.md': () =>
    import('../../../enterprise-mcp-governance-gateway/connectors/atlassian/README.md'),
  'CONTRIBUTING.md': () => import('../../../CONTRIBUTING.md'),
};

export function loadDoc(entry: DocEntry): Promise<DocModule> {
  const loader = docLoaders[entry.sourcePath];
  if (!loader) {
    throw new Error(`No MDX loader registered for ${entry.sourcePath}; add it to src/docs/docModules.ts`);
  }
  return loader();
}

/** Title of a doc, read from its first h1 at build time (em-dashes already normalised by the index). */
export function docTitle(entry: DocEntry): string {
  const title = docTitles[entry.sourcePath];
  if (!title) {
    throw new Error(`No title indexed for ${entry.sourcePath}; check src/vite-plugins/repo-index.ts`);
  }
  return title;
}

/** Sidebar label: explicit navLabel, otherwise the h1 title. */
export function docLabel(entry: DocEntry): string {
  return entry.navLabel ?? docTitle(entry);
}
