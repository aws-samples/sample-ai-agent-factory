/**
 * Frequently asked questions, seeded from README notices. Every answer cites at
 * least one repository source.
 */
import type { ProjectId } from './data';
import type { Source } from './facts';
import { ISSUES_URL, VULN_REPORT_URL, WORKSHOP_URL, WORKSHOP_LINK_LABEL } from './links';

/** An external link offered with an answer. */
export interface FaqLink {
  /** Visible label. */
  label: string;
  /** Absolute URL or site route. */
  href: string;
}

/** One question and answer. */
export interface FaqEntry {
  /** Stable id for anchors. */
  id: string;
  /** The question. */
  question: string;
  /** Plain-language answer. */
  answer: string;
  /** Repository sources the answer rests on (at least one). */
  sources: Source[];
  /** Projects the question concerns; empty for repository-wide questions. */
  projectIds: ProjectId[];
  /** Optional links (site routes or external). */
  links?: FaqLink[];
}

const ROOT_README = 'README.md';
const CONTRIBUTING = 'CONTRIBUTING.md';
const WORKSHOP_INTRO = 'workshop-building-agentic-ai-platform/content/introduction/index.en.md';
const WORKSHOP_SELF_PACED_PAGE = 'workshop-building-agentic-ai-platform/content/introduction/getting-started/self-service.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_COSTS = 'Agentic-ai-self-service/docs/COSTS.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';

export const faq: FaqEntry[] = [
  {
    id: 'workshop-published',
    question: 'Is the workshop published, and where do I run it?',
    answer:
      'Yes. The workshop is published on AWS Builder Center (Workshop Studio). Open it there to follow the guide. At an AWS event the account is provided; self-paced, you clone this repository and run one deploy script in your own account. The workshop content is not reproduced on this site.',
    sources: [
      { file: ROOT_README, quote: 'in the AWS workshop catalog, listed on [AWS Builder Center]' },
    ],
    projectIds: ['workshop'],
    links: [{ label: WORKSHOP_LINK_LABEL, href: WORKSHOP_URL }],
  },
  {
    id: 'costs',
    question: 'What does it cost to run these projects?',
    answer:
      'All four deploy real, billable AWS resources. The workshop estimates about $15 to $30 for a one-day self-paced run in us-west-2. The Self-Service platform infrastructure is estimated at about $0.02 to $0.39 per month at low to moderate usage, excluding agent inference, AgentCore usage, vector stores and the WAF web ACL. The MCP Gateway and the Blueprint publish no cost figure. Tear down when you finish.',
    sources: [
      { file: WORKSHOP_INTRO, heading: 'Cost', quote: 'expect roughly **$15-30 for a one-day run** in `us-west-2`' },
      { file: SELF_SERVICE_COSTS, heading: 'Monthly Cost Estimates', quote: '| **Total** | **~$0.02/mo** | **~$0.39/mo** |' },
      { file: ROOT_README, heading: 'Costs', quote: '**Tear down resources when finished** using each project\'s cleanup commands.' },
    ],
    projectIds: ['workshop', 'self-service', 'mcp-gateway', 'blueprint'],
    links: [{ label: 'Costs and cleanup', href: '/start/costs-and-cleanup/' }],
  },
  {
    id: 'workshop-other-regions',
    question: 'Can I run the workshop in eu-central-1 or ap-southeast-1?',
    answer:
      'No. The validated regions are us-west-2 (default), us-east-1 and eu-west-1. Elsewhere the Amazon Bedrock AgentCore Registry control plane returns an internal error, which breaks Modules 3b and 4. Model access must also be granted in the region you pick.',
    sources: [
      {
        file: WORKSHOP_SELF_PACED_PAGE,
        heading: 'Region',
        quote: 'it returns an internal error in regions such as `eu-central-1` and `ap-southeast-1`',
      },
    ],
    projectIds: ['workshop'],
  },
  {
    id: 'self-service-other-regions',
    question: 'Can I deploy the Self-Service platform outside us-east-1?',
    answer:
      'Yes, any region works by setting AWS_REGION. Two things differ. Outside us-east-1 the WAF rule set is attached as a REGIONAL web ACL on the Cognito user pool, and the CloudFront distribution runs without an edge ACL unless you pass CLOUDFRONT_WEB_ACL_ARN for one created in us-east-1. Account-global names also get a region suffix. In APAC regions, current-generation models use country or global prefixes, so the model ID may need to be set explicitly.',
    sources: [
      {
        file: SELF_SERVICE_README,
        heading: 'Deploying to another region',
        quote: 'One `CLOUDFRONT`-scoped WebACL on the CloudFront distribution',
      },
      {
        file: SELF_SERVICE_README,
        heading: 'Deploying to another region',
        quote: 'family covers only the older Claude models. In APAC, current-generation models',
      },
    ],
    projectIds: ['self-service'],
  },
  {
    id: 'self-service-first-sign-in',
    question: 'Why is my new Self-Service user read-only after signing in?',
    answer:
      'COGNITO_USERS pre-creates Cognito users but assigns them to no group, and group membership grants the capability scopes. Add the user to groups such as g-admins-super, t-admin and registry-admin (or g-users-default and t-user) with the AWS CLI, passing the region you deployed to, then sign out and back in so the new scopes are read from the ID token.',
    sources: [
      { file: SELF_SERVICE_README, quote: 'pre-creates Cognito **users** but assigns them to **no group**' },
      { file: SELF_SERVICE_README, quote: '**Sign out and back in** after changing groups' },
    ],
    projectIds: ['self-service'],
  },
  {
    id: 'gateway-production-ready',
    question: 'Is the MCP Governance Gateway production-ready?',
    answer:
      'No. The README calls it a sample and demonstration stack that is safe to demo but not hardened for production. It lists what to change first. Tighten the gateway-resource IAM scope for multi-gateway accounts. Use ENFORCE without exceptionLevel in production. Add a Cognito pre-token-generation Lambda so role-based policies fire. Set per-Lambda log retention. Federate the pool to your own IdP.',
    sources: [
      { file: GATEWAY_README, heading: 'Security notes', quote: 'is safe to demo, but it is **not hardened for production**' },
    ],
    projectIds: ['mcp-gateway'],
    links: [{ label: 'Support envelope', href: '/reference/support-envelope/' }],
  },
  {
    id: 'gateway-role-policies',
    question: 'Why do role-based Cedar policies never fire in the MCP Gateway demo?',
    answer:
      'Cognito issues two tokens. The gateway validates the access token, which carries sub, username and scope. The custom:role attribute and the email claim live only in the ID token, and the gateway rejects ID tokens because they have no scope claim. So a permit gated on role never matches for demo users; the README documents this as a known limitation and names a pre-token-generation Lambda as the production fix.',
    sources: [
      { file: GATEWAY_README, quote: '`custom:role` never reaches the access token the gateway validates (documented limitation)' },
    ],
    projectIds: ['mcp-gateway'],
  },
  {
    id: 'aws-service',
    question: 'Is this an AWS service or a supported product?',
    answer:
      'No. This is sample code under the MIT-0 license. It is not an AWS service, an AppSec-reviewed product, or a compliance attestation. Review the architecture, security posture and costs before use, and file bugs and feature requests through GitHub issues.',
    sources: [
      { file: ROOT_README, heading: 'What This Is', quote: 'Not an AWS service, AppSec-reviewed product, or compliance attestation.' },
    ],
    projectIds: [],
    links: [{ label: 'GitHub issues', href: ISSUES_URL }],
  },
  {
    id: 'report-security-issue',
    question: 'How do I report a security issue?',
    answer:
      'Use the AWS vulnerability reporting page. Do not open a public GitHub issue for a potential security problem. Each project README repeats this instruction.',
    sources: [
      {
        file: CONTRIBUTING,
        heading: 'Security issue notifications',
        quote: 'notify AWS/Amazon Security via our [vulnerability reporting page]',
      },
    ],
    projectIds: [],
    links: [{ label: 'AWS vulnerability reporting', href: VULN_REPORT_URL }],
  },
];
