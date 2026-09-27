/**
 * Sourced content for the Self-Service (AgentCore Visual Workflow Platform)
 * project page. Everything here is described from the project's own README and
 * docs folder in this repository.
 *
 * Pure data. Every `quote` is a verbatim substring of the cited file and every
 * `heading` is a real heading (checked by projects.test.ts).
 */
import type { Source } from '../facts';

const SELF_SERVICE = 'Agentic-ai-self-service';
export const SELF_SERVICE_README_PATH = `${SELF_SERVICE}/README.md`;

/** GitHub-only doc (not rendered on the site). */
export const MCP_CATALOG_PATH = `${SELF_SERVICE}/docs/MCP_CATALOG.md`;
export const mcpCatalogLink = {
  label: 'MCP Catalog',
  description: 'External MCP catalog servers as Gateway targets',
  source: { file: SELF_SERVICE_README_PATH, quote: 'External MCP catalog servers as Gateway targets' } satisfies Source,
};

/** Doc id (see ../docs.ts) of the enterprise capabilities page. */
export const ENTERPRISE_CAPABILITIES_DOC_ID = 'self-service/docs/enterprise-capabilities';

/** How the README's documentation table describes that doc. */
export const enterpriseCapabilitiesLink = {
  description:
    'Versioning and rollback, Cedar policy enforcement, evaluation, cost analytics, registry, prompt library, triggers, connectors, HITL, governance and FinOps.',
  source: {
    file: SELF_SERVICE_README_PATH,
    heading: 'Documentation',
    quote: 'Versioning & rollback, Cedar policy enforcement, evaluation, cost analytics',
  } satisfies Source,
};

/** Alt text, captions and provenance of the three figures. */
export const selfServiceFigures = {
  canvas: {
    alt: 'Visual canvas: a customer support agent wired to Gateway, Identity, Memory, and Observability',
    source: {
      file: SELF_SERVICE_README_PATH,
      quote: 'a customer support agent wired to Gateway, Identity, Memory, and Observability',
    } satisfies Source,
  },
  templates: {
    alt: 'Template gallery',
    text: 'Template gallery: six one-click starting points from beginner to advanced.',
    source: {
      file: SELF_SERVICE_README_PATH,
      quote: 'six one-click starting points from beginner to advanced',
    } satisfies Source,
  },
  architecture: {
    alt: 'Architecture diagram of the AgentCore Visual Workflow Platform',
    caption: 'Architecture, from the Self-Service README.',
    drawio: `${SELF_SERVICE}/docs/architecture.drawio`,
    source: {
      file: SELF_SERVICE_README_PATH,
      heading: 'Architecture',
      quote: 'The editable diagram source is at [`docs/architecture.drawio`]',
    } satisfies Source,
  },
};

/** What happens on first sign-in. */
export const firstSignInNote = {
  title: 'First sign-in: assign a persona',
  text: 'COGNITO_USERS pre-creates Cognito users but assigns them to no group. Group membership grants capability scopes, so a brand-new user signs in effectively read-only (browse works; Clone and publish are disabled) until you assign a group. Sign out and back in after changing groups.',
  sources: [
    { file: SELF_SERVICE_README_PATH, quote: 'pre-creates Cognito **users** but assigns them to **no group**' },
    { file: SELF_SERVICE_README_PATH, quote: '**Sign out and back in** after changing groups' },
  ] satisfies Source[],
};

/** README section on LiteLLM. */
export const BRING_YOUR_OWN_LITELLM: Source = { file: SELF_SERVICE_README_PATH, heading: 'Bring your own LiteLLM' };

/** Fragment id of that section on the rendered README page (GitHub-style slug). */
export const BRING_YOUR_OWN_LITELLM_ANCHOR = 'bring-your-own-litellm';

/** One of the three supported gateway shapes on the canvas. */
export interface LiteLlmShape {
  id: string;
  onCanvas: string;
  created: string;
  useWhen: string;
  source: Source;
}

export const liteLlmShapes: LiteLlmShape[] = [
  {
    id: 'agentcore-default',
    onCanvas: 'Gateway node, Provider = AgentCore (default)',
    created: 'A real AgentCore Gateway with your Lambda, OpenAPI, Smithy or MCP targets.',
    useWhen: 'The default. Nothing about it changes.',
    source: {
      ...BRING_YOUR_OWN_LITELLM,
      quote: 'A real AgentCore Gateway with your Lambda / OpenAPI / Smithy / MCP targets',
    },
  },
  {
    id: 'litellm-replaces-gateway',
    onCanvas: 'Gateway node, Provider = LiteLLM',
    created: 'No AgentCore Gateway at all. The agent talks straight to your proxy.',
    useWhen: 'LiteLLM replaces the gateway. Your proxy already aggregates every tool the agent needs.',
    source: {
      ...BRING_YOUR_OWN_LITELLM,
      quote: 'Your proxy already aggregates every tool the agent needs.',
    },
  },
  {
    id: 'litellm-as-target',
    onCanvas: 'Gateway node, Provider = AgentCore, with a Custom endpoint MCP target pointed at LiteLLM',
    created: 'An AgentCore Gateway that carries your proxy as one mcpServer target.',
    useWhen:
      'You want LiteLLM tools alongside Lambda or OpenAPI targets, or you want AgentCore inbound Cognito auth, semantic search and observability in front of it.',
    source: {
      ...BRING_YOUR_OWN_LITELLM,
      quote: 'An AgentCore Gateway that carries your proxy as one `mcpServer` target',
    },
  },
];

/** The second LiteLLM role, and the opt-in rule. */
export const liteLlmNotes = [
  {
    id: 'registry-role',
    text: 'A LiteLLM proxy can also be the agent catalog behind the Registry. The gateway role and the registry role are independent.',
    source: { ...BRING_YOUR_OWN_LITELLM, quote: 'as the **agent catalog** behind the Registry.' },
  },
  {
    id: 'additive',
    text: 'All of this is additive. AgentCore Gateway stays the default gateway and the built-in DynamoDB catalog stays the default registry until someone opts in.',
    source: { ...BRING_YOUR_OWN_LITELLM, quote: 'built-in DynamoDB catalog stays the default registry' },
  },
] satisfies Array<{ id: string; text: string; source: Source }>;
