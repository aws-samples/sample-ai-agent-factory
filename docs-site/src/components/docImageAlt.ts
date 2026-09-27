/**
 * Descriptive alt text for repository images whose Markdown alt is a single generic
 * word (for example "Architecture"). Keyed by the original file name; the bundled
 * file name keeps it with a content hash appended. Multi-word Markdown alts win.
 */
const CURATED_ALT: Record<string, string> = {
  'canvas.png':
    'Visual canvas of the AgentCore Visual Workflow Platform: a Support Agent node built on Strands Agents connected to Support Gateway, Cognito Identity, Conversation Memory and OTEL Monitoring nodes, with the component palette on the left and a Ready to deploy status',
  'templates.png':
    'Workflow Templates dialog listing starting points by level: Lightweight Web Search Agent (beginner), Strands Agent plus Gateway (intermediate) and Customer Support Assistant (advanced), each with its built-in tools and a Use Template button',
  'architecture.jpg':
    'Architecture of the AgentCore Visual Workflow Platform in one AWS Region: a React single-page app served by CloudFront and S3 calls API Gateway and Lambda workflow and deployment APIs backed by DynamoDB tables; Step Functions runs a 14-step agent deployment pipeline; each deployed agent gets AgentCore runtime, MCP gateway, memory, policy and observability resources and a Cognito user pool',
  'enterprise-agent-factory-concept.svg':
    'Enterprise Agent Factory operating model: the governed flow from platform controls through agent blueprints to deployed workloads',
  'enterprise-agent-factory-aws-services.svg':
    'Enterprise Agent Factory AWS service-level reference architecture across governance, platform and workload accounts',
  'agentic-ai-platform-architecture.png':
    'Agentic AI platform architecture: an AWS Organization with management, log archive and audit accounts; a central platform account hosting the auth boundary (API Gateway, WAF, Cognito), Bedrock AgentCore gateway and registry, the LiteLLM inference gateway and security and cost controls; per-application workload accounts; and a CI/CD pipeline from build through evaluation, canary and production',
};

const HASHED_FILE = /^(.*?)(?:-[A-Za-z0-9_-]{8})?\.([a-z0-9]+)$/i;

function isGenericAlt(alt: string): boolean {
  return alt.trim().split(/\s+/).filter(Boolean).length <= 1;
}

/** Curated alt for a known image when the Markdown alt is generic, else the Markdown alt. */
export function resolveAlt(src: string, alt: string): string {
  if (!isGenericAlt(alt)) return alt;
  const file = src.split('#')[0].split('?')[0].split('/').pop() ?? '';
  const match = file.match(HASHED_FILE);
  const key = match ? `${match[1]}.${match[2]}`.toLowerCase() : file.toLowerCase();
  return CURATED_ALT[key] ?? alt;
}
