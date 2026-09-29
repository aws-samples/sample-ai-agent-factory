/**
 * Sourced facts for each project: regions, timings, cost, IaC, topology,
 * auth model, status, teardown and version.
 *
 * Every value that carries a number or a region has a `source` whose `quote`
 * is a verbatim substring of the cited repository file (checked by
 * `facts.test.ts`). Where the repository documents no figure, the fact is
 * marked `notDocumented: true` and the page renders "not documented".
 */
import type { ProjectId } from './data';
import { githubSourceUrl, WORKSHOP_URL } from './links';

/**
 * Pointer to the evidence that backs a statement: either a repository file
 * (with optional heading and verbatim quote) or an absolute external URL.
 * Exactly one of `file` or `url` is set.
 */
export interface Source {
  /** Repository-relative path, for example `workshop-building-agentic-ai-platform/README.md`. */
  file?: string;
  /** Exact text of a Markdown heading in that file (without the leading `#`). */
  heading?: string;
  /** Verbatim substring of the file, 20 to 120 characters. */
  quote?: string;
  /** Absolute external URL, used instead of `file` when the evidence lives outside the repository. */
  url?: string;
  /** Display label for a `url` source, for example `Workshop Studio catalog`. */
  label?: string;
}

/** A single displayable fact with provenance. */
export interface Fact {
  /** Plain-language value shown to the reader. */
  value: string;
  /** Where the value comes from. Omitted only when `notDocumented` is set or the note explains why. */
  source?: Source;
  /** True when the repository publishes no figure; pages render "not documented". */
  notDocumented?: boolean;
  /** Extra context, caveats, or the reason a source is missing. */
  note?: string;
  /** Additional sources that back the note or the value; checked like any other source. */
  evidence?: Source[];
}

/** The fact set every project page and the comparison table render. */
export interface ProjectFacts {
  /** Regions the project is validated or supported in. */
  regions: Fact;
  /** Region the README uses by default. */
  defaultRegion: Fact;
  /** What a deploy creates, as listed by the README. */
  deploys: Fact;
  /** Time to a first successful deploy. */
  firstDeploy: Fact;
  /** Hands-on time after the deploy. */
  handsOnTime: Fact;
  /** Cost note for running the project. */
  cost: Fact;
  /** Infrastructure-as-code tooling. */
  iac: Fact;
  /** Account and region topology. */
  accountTopology: Fact;
  /** Authentication and policy model. */
  authAndPolicy: Fact;
  /** Publication or maturity status. */
  status: Fact;
  /** How to remove everything. */
  teardown: Fact;
  /** Version as stated by the project. */
  version: Fact;
}

/** One service control policy shipped by the Blueprint. */
export interface BlueprintScp {
  /** Short id, for example `SCP-06`. */
  id: string;
  /** Title as written in the file's header comment. */
  title: string;
  /** Repository-relative path to the TypeScript definition. */
  file: string;
}

/** Ordered fact keys, used to render the facts table in a stable order. */
export const FACT_KEYS: ReadonlyArray<keyof ProjectFacts> = [
  'regions',
  'defaultRegion',
  'deploys',
  'firstDeploy',
  'handsOnTime',
  'cost',
  'iac',
  'accountTopology',
  'authAndPolicy',
  'status',
  'teardown',
  'version',
];

/** Display labels for each fact key. */
export const FACT_LABELS: Record<keyof ProjectFacts, string> = {
  regions: 'Validated regions',
  defaultRegion: 'Default region',
  deploys: 'What it deploys',
  firstDeploy: 'First deploy',
  handsOnTime: 'Hands-on time',
  cost: 'Cost',
  iac: 'Infrastructure as code',
  accountTopology: 'Account topology',
  authAndPolicy: 'Auth and policy',
  status: 'Status',
  teardown: 'Teardown',
  version: 'Version',
};

/** Text rendered for a fact with `notDocumented: true`. */
export const NOT_DOCUMENTED_LABEL = 'not documented';

const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const WORKSHOP_CONTENTSPEC = 'workshop-building-agentic-ai-platform/contentspec.yaml';
const WORKSHOP_INTRO = 'workshop-building-agentic-ai-platform/content/introduction/index.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_COSTS = 'Agentic-ai-self-service/docs/COSTS.md';
const SELF_SERVICE_CHANGELOG = 'Agentic-ai-self-service/CHANGELOG.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

export const facts: Record<ProjectId, ProjectFacts> = {
  workshop: {
    regions: {
      value:
        'AWS-run events: us-west-2. Self-paced: us-west-2 (default), us-east-1, or eu-west-1. Other regions are not supported.',
      source: {
        file: WORKSHOP_CONTENTSPEC,
        quote: 'Default/baseline region is us-west-2. Limited to the regions where the full',
      },
      note: 'The contentspec.yaml deployableRegions list is us-west-2, us-east-1 and eu-west-1. Workshop Studio events provision the account in us-west-2 (content/introduction/getting-started/aws-event.en.md).',
    },
    defaultRegion: {
      value: 'us-west-2',
      source: {
        file: WORKSHOP_README,
        heading: 'Quick start (self-paced)',
        quote: '# Set a supported region (default us-west-2; see Prerequisites above)',
      },
    },
    deploys: {
      value: 'Five CloudFormation stacks: LLM Gateway, MCP Registry, Tools Gateway, AgentCore, Code Editor IDE',
      source: {
        file: WORKSHOP_README,
        heading: 'Quick start (self-paced)',
        quote: '# Deploy all 5 stacks (LLM Gateway, MCP Registry, Tools Gateway, AgentCore, Code Editor IDE)',
      },
    },
    firstDeploy: {
      value: 'About 30 to 45 minutes for the self-paced deploy script (five CloudFormation stacks)',
      source: {
        file: WORKSHOP_README,
        heading: 'Quick start (self-paced)',
        quote: '~30-45 min; prints the IDE URL + password at the end',
      },
      note: 'The self-paced guide (content/introduction/getting-started/self-service.en.md) notes that the Registry stack alone takes 20 to 30 minutes. At an AWS event the account arrives pre-provisioned, so there is nothing to deploy.',
    },
    handsOnTime: {
      value: '1.5 to 4 hours depending on the track',
      source: {
        file: WORKSHOP_CONTENTSPEC,
        quote: 'estimatedDuration: "1.5-4 hours"',
      },
    },
    cost: {
      value: 'About $15 to $30 for a one-day run in us-west-2 (workshop estimate)',
      source: {
        file: WORKSHOP_INTRO,
        heading: 'Cost',
        quote: 'expect roughly **$15-30 for a one-day run** in `us-west-2`',
      },
      note: 'At an AWS-run event the account is provided and the cost is covered. Cost accrues per hour whether or not the environment is in use.',
    },
    iac: {
      value: 'CloudFormation, run by the self-paced deploy script; Module 4 deploys the FAST agent with the AWS CDK from inside the IDE',
      source: {
        file: WORKSHOP_README,
        heading: 'Quick start (self-paced)',
        quote: '# Deploy all 5 stacks (LLM Gateway, MCP Registry, Tools Gateway, AgentCore, Code Editor IDE)',
      },
    },
    accountTopology: {
      value: 'Single AWS account; a dedicated, disposable account is recommended',
      source: {
        file: WORKSHOP_README,
        heading: 'Prerequisites (self-paced)',
        quote: 'you can tear down when finished',
      },
    },
    authAndPolicy: {
      value:
        'Cognito JWT on the Tools Gateway and the AgentCore Registry; group-based access in interceptors and Cedar policies (Module 3b); scoped IAM deploy policies',
      source: {
        file: WORKSHOP_README,
        heading: "What you'll build",
        quote: 'layer an AgentCore Tools Gateway on top for JWT auth, audit, and guardrails',
      },
      note: 'Module 3b Part C creates an AgentCore Policy Engine with Cedar policies. The CLI path stops short of attaching it to the Gateway; the notebook attaches it in LOG_ONLY mode, not ENFORCE, because ENFORCE would empty tools/list in that setup (content/module-3b/step-7/index.en.md).',
    },
    status: {
      value: 'Published on AWS Builder Center (Workshop Studio); last published 2026-08-18',
      source: { url: WORKSHOP_URL, label: 'Workshop Studio catalog' },
      note: 'Publication state and date come from the Workshop Studio catalog entry, not from the repository, which holds no publish record to quote.',
    },
    teardown: {
      value: 'Follow the workshop Cleanup module for Module 4 and Module 3a resources, then run ./deploy-cfn.sh destroy from the workshop folder. At an AWS event Workshop Studio cleans up the account automatically.',
      source: {
        file: WORKSHOP_README,
        heading: 'Delete Everything',
        quote: './deploy-cfn.sh destroy',
      },
    },
    version: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'The workshop has no version badge, tag or CHANGELOG. contentspec.yaml declares only the Workshop Studio schema version 2.0.',
    },
  },

  'self-service': {
    regions: {
      value: 'Any AWS region; us-east-1 is the default',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Prerequisites',
        quote: '`us-east-1` is the default; see [Deploying to another region]',
      },
      note: 'Outside us-east-1 the WAF web ACL is REGIONAL on the Cognito user pool and the CloudFront distribution runs without an edge ACL. APAC regions may need the model ID set explicitly.',
    },
    defaultRegion: {
      value: 'us-east-1',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Quickstart',
        quote: '# Minimal deploy (dev environment, us-east-1)',
      },
    },
    deploys: {
      value: 'Serverless stack: API Gateway, Lambda, Step Functions, DynamoDB, S3 and CloudFront, plus a Cognito user pool and a WAF web ACL',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Quickstart',
        quote: 'runs `cdk deploy` via `npx` (API Gateway, Lambda, Step Functions, DynamoDB, S3, CloudFront)',
      },
      evidence: [
        {
          file: SELF_SERVICE_README,
          heading: 'Deploying to another region',
          quote: 'One `CLOUDFRONT`-scoped WebACL on the CloudFront distribution',
        },
        {
          file: SELF_SERVICE_README,
          quote: 'pre-creates Cognito **users** but assigns them to **no group**',
        },
      ],
    },
    firstDeploy: {
      value: 'Roughly 15 to 20 minutes for a first-time deploy',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Quickstart',
        quote: 'A first-time deploy takes roughly 15',
      },
    },
    handsOnTime: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'The README documents the deploy time only and publishes no hands-on figure.',
    },
    cost: {
      value: 'About $0.02 to $0.39 per month for the platform infrastructure at low to moderate usage (docs/COSTS.md estimate, us-east-1 list prices)',
      source: {
        file: SELF_SERVICE_COSTS,
        heading: 'Monthly Cost Estimates',
        quote: '| **Total** | **~$0.02/mo** | **~$0.39/mo** |',
      },
      note: 'Excludes the WAF web ACL that infra/stacks/platform_stack.py always creates, which is billed separately and for which the repository publishes no figure, and all agent inference, AgentCore and vector-store usage.',
      evidence: [
        {
          file: 'Agentic-ai-self-service/infra/stacks/platform_stack.py',
          quote: 'self.web_acl = build_waf_web_acl(self, cfg, user_pool=self.user_pool)',
        },
      ],
    },
    iac: {
      value: 'AWS CDK (Python) run through npx; serverless stack of API Gateway, Lambda, Step Functions, DynamoDB, S3 and CloudFront',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Quickstart',
        quote: 'runs `cdk deploy` via `npx` (API Gateway, Lambda, Step Functions, DynamoDB, S3, CloudFront)',
      },
    },
    accountTopology: {
      value: 'Single account and single region per deployment; several deployments can coexist in one account (dev and prod, or two regions)',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Safe to run with more than one deployment in the account',
        quote: 'Deploying and deleting repeatedly is expected, and so is running two deployments',
      },
      note: 'Multi-region and multi-account deploy is opt-in and off by default (docs/ENTERPRISE_CAPABILITIES.md); a cross-account role template ships as docs/cross-account-deploy-role.json.',
    },
    authAndPolicy: {
      value:
        'Cognito user pool with group-based scopes (RBAC advisory by default), owner-scoped tenant isolation, and Cedar ENFORCE per Policy node',
      source: {
        file: SELF_SERVICE_README,
        quote: 'pre-creates Cognito **users** but assigns them to **no group**',
      },
    },
    status: {
      value: 'Version 0.1.0 released 2026-07-17, with unreleased changes recorded in CHANGELOG.md',
      source: {
        file: SELF_SERVICE_CHANGELOG,
        quote: '## [0.1.0] - 2026-07-17',
      },
    },
    teardown: {
      value: 'Run ./scripts/cleanup.sh (prompts for confirmation). It deletes every AgentCore resource the platform created, empties the S3 buckets and runs cdk destroy.',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Cleanup',
        quote: '# Tear down all resources (prompts for confirmation)',
      },
    },
    version: {
      value: '0.1.0 plus unreleased changes',
      source: {
        file: SELF_SERVICE_CHANGELOG,
        quote: '## [0.1.0] - 2026-07-17',
      },
    },
  },

  'mcp-gateway': {
    regions: {
      value: 'us-west-2 by default; configurable; no tested-regions list is published',
      source: {
        file: GATEWAY_README,
        quote: 'Provisioned with **AWS CDK (Python)** to your account in `us-west-2`',
      },
    },
    defaultRegion: {
      value: 'us-west-2',
      source: {
        file: GATEWAY_README,
        heading: 'Deploy',
        quote: 'Region defaults to `us-west-2` (override with `AWS_REGION` / `CDK_DEFAULT_REGION`).',
      },
    },
    deploys: {
      value: 'AgentCore Gateway and Cedar policy engine, four Lambdas (two interceptors, two targets), a Cognito user pool, a Secrets Manager secret, a customer-managed KMS key, SSM parameters and a Bedrock Guardrail',
      source: {
        file: GATEWAY_README,
        quote: 'the 4 Lambdas (request/response interceptors + 2 targets)',
      },
      evidence: [
        { file: GATEWAY_README, quote: '**Encryption at rest uses a customer-managed KMS key**' },
        { file: GATEWAY_README, heading: 'Managed guardrail (Amazon Bedrock Guardrails)' },
      ],
    },
    firstDeploy: {
      value: 'About 5 minutes for the five quickstart steps; the gateway stack itself takes about 2 minutes',
      source: {
        file: GATEWAY_README,
        heading: 'Quickstart',
        quote: 'Deploy, then prove the governance works. Five steps, ~5 minutes.',
      },
    },
    handsOnTime: {
      value: 'About 5 minutes for the quickstart; the governance walkthrough has no stated duration',
      source: {
        file: GATEWAY_README,
        heading: 'Quickstart',
        quote: 'Deploy, then prove the governance works. Five steps, ~5 minutes.',
      },
    },
    cost: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'The README lists what the stack creates (AgentCore Gateway and policy engine, four Lambdas, a Cognito user pool, a Secrets Manager secret, a customer-managed KMS key, SSM parameters, and a Bedrock Guardrail) but publishes no cost figure.',
    },
    iac: {
      value: 'AWS CDK (Python) with AWS::BedrockAgentCore L1 constructs; CDK CLI pinned to 2.1129.0',
      source: {
        file: GATEWAY_README,
        heading: 'Quickstart',
        quote: '**Node.js + `npm install -g aws-cdk@2.1129.0`**, **Python 3.12+**',
      },
    },
    accountTopology: {
      value: 'Single account; one gateway stack plus two optional connector stacks',
      source: {
        file: GATEWAY_README,
        heading: 'Deploy',
        quote: 'This CDK app holds three stacks (the gateway + the two optional connector stacks)',
      },
    },
    authAndPolicy: {
      value: 'Cognito OIDC JWT (CUSTOM_JWT authorizer) and a Cedar policy engine attached in ENFORCE mode',
      source: {
        file: GATEWAY_README,
        heading: 'Verified architecture',
        quote: '`policyEngineConfiguration={arn, mode: "ENFORCE"}`',
      },
    },
    status: {
      value: 'Sample and demonstration stack; not hardened for production',
      source: {
        file: GATEWAY_README,
        heading: 'Security notes',
        quote: 'is safe to demo, but it is **not hardened for production**',
      },
    },
    teardown: {
      value: 'Disconnect the MCP client first, then cdk destroy EnterpriseMcpGatewayStack (destroy the two connector stacks first if you deployed them). CloudWatch log groups are not removed.',
      source: {
        file: GATEWAY_README,
        heading: 'Teardown',
        quote: 'cdk destroy EnterpriseMcpGatewayStack',
      },
    },
    version: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'No version badge, tag or CHANGELOG in the project folder.',
    },
  },

  blueprint: {
    regions: {
      value: 'Validated in eu-west-1; SCP allow-list us-west-2, us-east-1, eu-west-1',
      source: {
        file: BLUEPRINT_README,
        heading: '5. Prerequisites',
        quote: 'The currently validated reference Region is `eu-west-1` (Ireland).',
      },
      note: 'The SCP region allow-list comes from PLATFORM_APPROVED_REGIONS in packages/platform-baselines/src/approved-regions.ts. A different Region is a new validation target, not a configuration-only substitution.',
    },
    defaultRegion: {
      value: 'eu-west-1',
      source: {
        file: BLUEPRINT_README,
        heading: '6.1 One-time setup',
        quote: 'export AWS_REGION=eu-west-1',
      },
    },
    deploys: {
      value: 'A multi-account reference: AgentCore Runtime, Gateway, Identity, Memory, Policy, Registry and Evaluations; Bedrock with Guardrails and application inference profiles; Cognito, IAM Identity Center and Cedar; Organizations SCPs; CodePipeline, CodeBuild and CodeConnections; VPC with endpoints; Lambda; KMS, S3, DynamoDB, Secrets Manager and ECR; CloudWatch, OAM and X-Ray; CloudTrail, Config, Security Hub, GuardDuty and Inspector; Budgets and CUR',
      source: {
        file: BLUEPRINT_README,
        heading: '4. AWS services used',
        quote: 'Amazon Bedrock AgentCore Runtime, Gateway, Identity, Memory, Policy, Registry, Evaluations',
      },
      note: 'README section 4 calls this the deployable and live-tested reference implementation, not a universal mandatory product list. Not every optional construct is inside the Ireland support envelope.',
    },
    firstDeploy: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'The README documents the deployment sequence (sections 6.1 to 6.6: one-time setup, configuration, scoped bootstrap, Platform pipeline, Workstream onboarding, validation) but no duration.',
    },
    handsOnTime: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'The README lists organizational prerequisites (a Platform product owner, an account-vending process, governance and approval policies) but gives no time figure.',
    },
    cost: {
      value: NOT_DOCUMENTED_LABEL,
      notDocumented: true,
      note: 'README section 8 describes a two-layer cost model (shared Platform cost and Workstream cost) and recommended controls such as allocation tags, budgets and CUR reconciliation, but publishes no figure.',
    },
    iac: {
      value: 'AWS CDK (TypeScript) with CDK Pipelines; 12 service control policies; Python and shell utilities',
      source: {
        file: BLUEPRINT_README,
        quote: 'Infrastructure is authored in TypeScript AWS CDK, with Python and shell utilities',
      },
    },
    accountTopology: {
      value: 'Multi-account: Management, Platform, and Workstream account roles (nonproduction and production may be separate accounts)',
      source: {
        file: BLUEPRINT_README,
        heading: '5. Prerequisites',
        quote: 'At least Management, Platform, and Workstream account roles',
      },
    },
    authAndPolicy: {
      value:
        'AWS_IAM on the Workstream Tool Gateway; Cognito M2M and AgentCore Identity for inference; AgentCore PolicyEngine plus a retained Lambda Cedar wrapper; 12 SCPs',
      source: {
        file: BLUEPRINT_README,
        heading: '10.1 Control summary',
        quote: '`AWS_IAM` authentication for the Workstream Tool Gateway.',
      },
    },
    status: {
      value: 'Version 1.0.0 (README badge)',
      source: {
        file: BLUEPRINT_README,
        quote: 'badge/version-1.0.0-blue',
      },
    },
    teardown: {
      value: 'Run python3 scripts/final_teardown.py per account role (workstream, then platform, then management), first as a dry run and then with --apply; verify with scripts/residue_inventory.py',
      source: {
        file: BLUEPRINT_README,
        heading: '16. Cleanup',
        quote: 'python3 scripts/final_teardown.py',
      },
    },
    version: {
      value: '1.0.0',
      source: {
        file: BLUEPRINT_README,
        quote: 'badge/version-1.0.0-blue',
      },
    },
  },
};

/** File stem and header-comment title of each Blueprint SCP, in id order. */
const SCP_FILES: ReadonlyArray<readonly [stem: string, title: string]> = [
  ['scp-01-model-allowlist', 'Restrict Bedrock Model Access'],
  ['scp-02-enforce-guardrail', 'Enforce Bedrock Guardrail Usage'],
  ['scp-03-enforce-agentcore-vpce', 'Enforce VPC Endpoints for AgentCore'],
  ['scp-04-enforce-bedrock-vpce', 'Enforce VPC Endpoints for Bedrock'],
  ['scp-05-deny-guardrail-modification', 'Deny Guardrail Modification in Workload Accounts'],
  ['scp-06-restrict-regions', 'Restrict Region Usage'],
  ['scp-07-deny-public-agentcore', 'Deny Public AgentCore Resources'],
  ['scp-08-deny-ecr-public', 'Deny ECR Public Repositories'],
  ['scp-09-gateway-mutation-lockdown', 'AgentCore Gateway Mutation Lockdown'],
  ['scp-10-tool-invoke-allowlist', 'Tool-Invoke Allow-list'],
  ['scp-11-registry-mutation-lockdown', 'Agent Registry Mutation Lockdown'],
  ['scp-12-developer-platform-tag-deny', 'Developer Permission Set and Platform-Tag Mutation Deny'],
];

/** The 12 service control policies the Blueprint ships, in id order. */
export const blueprintScps: BlueprintScp[] = SCP_FILES.map(([stem, title]) => ({
  id: `SCP-${stem.slice(4, 6)}`,
  title,
  file: `enterprise-agentic-ai-platform-blueprint/packages/organizations/src/scps/${stem}.ts`,
}));

/** Get the fact set for a project. */
export function getFacts(projectId: ProjectId): ProjectFacts {
  return facts[projectId];
}

/** Text to render for a fact, honouring `notDocumented`. */
export function factText(fact: Fact): string {
  return fact.notDocumented ? NOT_DOCUMENTED_LABEL : fact.value;
}

/**
 * GitHub URL for a source pointer (file on the default branch plus heading
 * fragment). Kept here so consumers of `facts` do not also need `links`.
 */
export function githubUrl(source: Source): string {
  return githubSourceUrl(source);
}
