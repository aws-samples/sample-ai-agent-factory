/**
 * Known limitations and support-envelope statements per project.
 *
 * Every `text` is copied from the project README (or the named doc) without
 * paraphrase. Where a README sentence runs across lines, the whitespace is
 * normalised; where a bullet continues past an em-dash, the text stops at the
 * sentence boundary before it. Markdown emphasis markers are dropped from the
 * displayed text; the `source.quote` keeps them so the test can match the
 * file verbatim.
 */
import type { ProjectId } from './data';
import type { Source } from './facts';

/** One verbatim limitation (or validated-envelope statement) with its source. */
export interface Limitation {
  /** Stable id for anchors and keys. */
  id: string;
  /** Optional short title (also verbatim, usually the bullet's bold lead). */
  title?: string;
  /** Verbatim README text. */
  text: string;
  /** Where the text lives. */
  source: Source;
}

const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_RBAC_ROLLOUT = 'Agentic-ai-self-service/docs/RBAC_ROLLOUT.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

const BLUEPRINT_OUTSIDE = 'Outside the current envelope';
const BLUEPRINT_VALIDATED = 'Live-validated reference envelope';
const GATEWAY_HARDENING = 'Tracked production hardening (not in this sample)';

export const limitations: Record<ProjectId, Limitation[]> = {
  workshop: [
    {
      id: 'workshop-regions',
      title: 'Region lock',
      text: 'Other regions are not supported',
      source: {
        file: WORKSHOP_README,
        heading: 'Prerequisites (self-paced)',
        quote: '(Other regions are not supported',
      },
    },
    {
      id: 'workshop-registry-control-plane',
      title: 'AgentCore Registry availability',
      text: 'the Amazon Bedrock AgentCore Registry control plane is not yet generally available everywhere, which breaks Modules 3b and 4.',
      source: {
        file: WORKSHOP_README,
        heading: 'Prerequisites (self-paced)',
        quote: 'Amazon Bedrock AgentCore Registry control plane is not yet generally available everywhere,',
      },
    },
    {
      id: 'workshop-model-access',
      title: 'Model access',
      text: 'Model access must be granted per region.',
      source: {
        file: WORKSHOP_README,
        heading: 'Prerequisites (self-paced)',
        quote: 'Model access must be granted per region.',
      },
    },
    {
      id: 'workshop-run-in-ide',
      title: 'Where commands run',
      text: 'Run everything in the IDE terminal/notebooks, not your local machine.',
      source: {
        file: WORKSHOP_README,
        quote: '**Run everything in the IDE terminal/notebooks**, not your local machine.',
      },
    },
  ],

  'self-service': [
    {
      id: 'self-service-waf-scope',
      title: 'WAF outside us-east-1',
      text: 'The same rule set as a `REGIONAL` WebACL on the Cognito user pool. The distribution runs without an edge ACL',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Deploying to another region',
        quote: 'The distribution runs without an edge ACL',
      },
    },
    {
      id: 'self-service-apac-prefix',
      title: 'APAC inference prefixes',
      text: "One region-specific caveat worth knowing before you pick a region: Bedrock's cross-region inference prefixes are `us.`, `eu.` and `apac.`, and the `apac.` family covers only the older Claude models. In APAC, current-generation models are published under *country* prefixes (`jp.` in `ap-northeast-1`, `au.` in `ap-southeast-2`) or as `global.`, so an APAC deployment may need its model ID set explicitly. `us-*` and `eu-*` regions need no such adjustment.",
      source: {
        file: SELF_SERVICE_README,
        heading: 'Deploying to another region',
        quote: 'family covers only the older Claude models. In APAC, current-generation models',
      },
    },
    {
      id: 'self-service-rbac-advisory',
      title: 'RBAC is advisory by default',
      text: 'Scope-based RBAC (`services/rbac.py`) ships advisory by default (`RBAC_ENFORCE=false`): every request is allowed, but a request that would be denied logs `RBAC advisory (would-deny): ...`.',
      source: {
        file: SELF_SERVICE_RBAC_ROLLOUT,
        heading: 'RBAC Enforcement Rollout Runbook',
        quote: 'Scope-based RBAC (`services/rbac.py`) ships **advisory by default**',
      },
    },
    {
      id: 'self-service-no-vpc-egress',
      title: 'Control plane has no VPC egress',
      text: 'The proxy must be reachable from the deploy Lambda. The control plane has no VPC egress, so a VPC-private LiteLLM cannot be probed and the deploy will fail at step 3 even though a VPC-mode Runtime could reach it at invoke time.',
      source: {
        file: SELF_SERVICE_README,
        heading: 'As the gateway itself, per agent',
        quote: 'The proxy must be reachable from the deploy Lambda.** The control plane has no',
      },
    },
  ],

  'mcp-gateway': [
    {
      id: 'gateway-not-hardened',
      title: 'Not hardened for production',
      text: 'This is a sample / demonstration stack. It is deployed to a real account and is safe to demo, but it is not hardened for production',
      source: {
        file: GATEWAY_README,
        heading: 'Security notes',
        quote: 'is safe to demo, but it is **not hardened for production**',
      },
    },
    {
      id: 'gateway-access-vs-id-token',
      title: 'Access token versus ID token',
      text: 'Query F is a known demo limitation, not a bug. `custom:role` therefore never reaches the policy engine, so the role-gated `permit` never fires.',
      source: {
        file: GATEWAY_README,
        quote: 'never reaches the policy engine, so the role-gated `permit` never fires.',
      },
    },
    {
      id: 'gateway-iam-scope',
      title: 'Tighten the gateway-resource IAM scope for multi-gateway accounts.',
      text: 'This sample deploys a single gateway, so the wildcard effectively resolves to it. If your account runs multiple gateways in the region, restrict the statement to the specific gateway ARN after the first deploy, or use a two-phase deploy (create the gateway, then update the policy with its exact ARN).',
      source: {
        file: GATEWAY_README,
        heading: GATEWAY_HARDENING,
        quote: 'Tighten the gateway-resource IAM scope for multi-gateway accounts.',
      },
    },
    {
      id: 'gateway-config-profiles',
      title: 'Env-based config profiles',
      text: '`ENFORCE` + no `exceptionLevel` for prod (`DEBUG` returns verbose denial reasons, useful only for a demo).',
      source: {
        file: GATEWAY_README,
        heading: GATEWAY_HARDENING,
        quote: '`ENFORCE` + no `exceptionLevel` for prod',
      },
    },
    {
      id: 'gateway-pre-token-lambda',
      title: 'Cognito pre-token-generation Lambda',
      text: 'A Cognito pre-token-generation Lambda to surface `custom:role` in the access token, so the role-based Cedar policies fire (today `role`/`email` live only in the ID token, which the gateway does not validate).',
      source: {
        file: GATEWAY_README,
        heading: GATEWAY_HARDENING,
        quote: 'A Cognito **pre-token-generation Lambda** to surface `custom:role` in the',
      },
    },
    {
      id: 'gateway-log-retention',
      title: 'Per-Lambda log retention',
      text: 'Per-Lambda log retention.',
      source: {
        file: GATEWAY_README,
        heading: GATEWAY_HARDENING,
        quote: 'Per-Lambda **log retention**.',
      },
    },
  ],

  blueprint: [
    {
      id: 'blueprint-substitutions',
      text: 'Any substituted LLM Gateway, Tool Gateway, runtime, memory, identity, registry, policy, delivery, observability, or safety implementation until its full contract matrix passes.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Any substituted LLM Gateway, Tool Gateway, runtime, memory, identity, registry, policy',
      },
    },
    {
      id: 'blueprint-scale-benchmark',
      text: 'A demonstrated rollout to hundreds of engineers or a measured fleet-capacity benchmark.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'A demonstrated rollout to hundreds of engineers or a measured fleet-capacity benchmark.',
      },
    },
    {
      id: 'blueprint-regions',
      text: 'Any Region other than `eu-west-1` until independently validated.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Any Region other than `eu-west-1` until independently validated.',
      },
    },
    {
      id: 'blueprint-legacy-paths',
      text: 'Legacy direct-Bedrock evaluation, online-evaluation, ECS LiteLLM, and direct circuit-breaker paths that rely on cross-Region profiles.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Legacy direct-Bedrock evaluation, online-evaluation, ECS LiteLLM',
      },
    },
    {
      id: 'blueprint-vpc-lattice',
      text: 'VPC Lattice private endpoints.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'VPC Lattice private endpoints.',
      },
    },
    {
      id: 'blueprint-transaction-search',
      text: 'Transaction Search enabled by default.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Transaction Search enabled by default.',
      },
    },
    {
      id: 'blueprint-rate-limiting',
      text: 'Native Gateway rate limiting as a hard quota or authorization control.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Native Gateway rate limiting as a hard quota or authorization control.',
      },
    },
    {
      id: 'blueprint-cedar-wrapper',
      text: 'Automatic retirement of the Lambda Cedar wrapper.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Automatic retirement of the Lambda Cedar wrapper.',
      },
    },
    {
      id: 'blueprint-no-certification',
      text: 'A compliance certification, availability SLA, or guarantee that future AWS changes preserve behavior.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'A compliance certification, availability SLA, or guarantee that future AWS changes preserve behavior.',
      },
    },
    {
      id: 'blueprint-runtime-network-mode',
      title: 'Runtime network posture',
      text: 'Network posture: `networkMode = PUBLIC`, matching the live commit.',
      source: {
        file: 'enterprise-agentic-ai-platform-blueprint/apps/workload-account/lib/d03-workstream-runtime-memory-stack.ts',
        quote: 'Network posture: `networkMode = PUBLIC`, matching the live commit.',
      },
    },
    {
      id: 'blueprint-evaluation-gate-scaffold',
      title: 'Evaluation gate',
      text: 'This is a scaffolded implementation',
      source: {
        file: 'enterprise-agentic-ai-platform-blueprint/scripts/evaluation_gate.py',
        quote: 'This is a scaffolded implementation',
      },
    },
    {
      id: 'blueprint-do-not-extrapolate',
      title: 'Region support',
      text: 'A Region is supportable only after independent service-availability, model and residency, IAM/SCP, availability-zone, strict synth, positive/adversarial, rollback, observability, and teardown gates pass there. Do not extrapolate from Ireland.',
      source: {
        file: BLUEPRINT_README,
        heading: BLUEPRINT_OUTSIDE,
        quote: 'Do not extrapolate from Ireland.',
      },
    },
  ],
};

/**
 * What each project states it has validated, verbatim. Only the Blueprint
 * publishes such a list (README section 15, "Live-validated reference
 * envelope", validated in eu-west-1); the other projects have none.
 */
export const validated: Record<ProjectId, Limitation[]> = {
  workshop: [],
  'self-service': [],
  'mcp-gateway': [],
  blueprint: [
    {
      id: 'blueprint-validated-pipelines',
      text: 'Platform and Workload pipelines through production.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Platform and Workload pipelines through production.' },
    },
    {
      id: 'blueprint-validated-registry',
      text: 'AWS Agent Registry record resolution and governance.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'AWS Agent Registry record resolution and governance.' },
    },
    {
      id: 'blueprint-validated-generated-agents',
      text: 'Generated agents using LiteLLMModel and MCPClient.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Generated agents using `LiteLLMModel` and `MCPClient`.' },
    },
    {
      id: 'blueprint-validated-agentcore',
      text: 'AgentCore Identity, Runtime, Memory, Inference Gateway, and Tool Gateway.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'AgentCore Identity, Runtime, Memory, Inference Gateway, and Tool Gateway.' },
    },
    {
      id: 'blueprint-validated-guardrail',
      text: 'Benign and adversarial Guardrail calls with exact admitted and blocked outcomes.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Benign and adversarial Guardrail calls with exact admitted and blocked outcomes.' },
    },
    {
      id: 'blueprint-validated-http-429',
      text: 'Exact HTTP 429 behavior for an unallocated model.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Exact HTTP 429 behavior for an unallocated model.' },
    },
    {
      id: 'blueprint-validated-cross-account-denial',
      text: 'Direct cross-account Runtime denial.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Direct cross-account Runtime denial.' },
    },
    {
      id: 'blueprint-validated-runtime-update',
      text: 'Runtime update cancellation, rollback, and re-run while sampled sessions remained available.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Runtime update cancellation, rollback, and re-run while sampled sessions remained available.' },
    },
    {
      id: 'blueprint-validated-evaluation-gates',
      text: 'Evaluation gates for regression, quality, tool success, refusal, latency, and cost.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Evaluation gates for regression, quality, tool success, refusal, latency, and cost.' },
    },
    {
      id: 'blueprint-validated-management-queries',
      text: 'Management queries across linked Platform and Workstream logs and metrics.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Management queries across linked Platform and Workstream logs and metrics.' },
    },
    {
      id: 'blueprint-validated-otel-correlation',
      text: 'Gateway application-log and OTEL span correlation after regional propagation.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Gateway application-log and OTEL span correlation after regional propagation.' },
    },
    {
      id: 'blueprint-validated-teardown',
      text: 'Dependency-ordered teardown and direct zero-residual inventories across all three account roles.',
      source: { file: BLUEPRINT_README, heading: BLUEPRINT_VALIDATED, quote: 'Dependency-ordered teardown and direct zero-residual inventories across all three account roles.' },
    },
  ],
};

/** Limitations for one project. */
export function getLimitations(projectId: ProjectId): Limitation[] {
  return limitations[projectId];
}
