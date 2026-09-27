/**
 * Ordered quickstart steps per project, with commands copied from each README.
 *
 * Nothing here is invented: every command is a verbatim block from the cited
 * README, every duration is a `Fact`, and link-only steps point at the README
 * heading they summarise.
 */
import type { ProjectId } from './data';
import { facts, type Fact, type Source } from './facts';
import { blob, WORKSHOP_LINK_LABEL, WORKSHOP_URL } from './links';

/** One step in a quickstart. */
export interface QuickstartStep {
  /** Short imperative title. */
  title: string;
  /** Shell command block copied from the README, when there is one. */
  command?: string;
  /** Plain-language note shown under the title or command. */
  note?: string;
  /** External link for link-only steps (for example a README section or the workshop). */
  href?: string;
  /** Accessible label for `href`. */
  hrefLabel?: string;
  /** Where the step comes from. */
  source: Source;
}

/** Teardown instructions for a quickstart. */
export interface QuickstartTeardown {
  /** Plain-language procedure. */
  text: string;
  /** Verbatim teardown command, when there is one. */
  command?: string;
  /** Where the procedure comes from. */
  source: Source;
}

/** A complete quickstart path for a project. */
export interface Quickstart {
  /** Stable id, unique across all quickstarts. */
  id: string;
  /** Project this path belongs to. */
  projectId: ProjectId;
  /** Tab or heading label, for example "Self-paced" or "At an AWS event". */
  name: string;
  /** One or two sentences that set expectations. */
  intro: string;
  /** Expected time to a first result. */
  expectedTime: Fact;
  /** Ordered steps. */
  steps: QuickstartStep[];
  /** How to remove everything afterwards. */
  teardown: QuickstartTeardown;
}

const ROOT_README = 'README.md';
const WORKSHOP_README = 'workshop-building-agentic-ai-platform/README.md';
const WORKSHOP_EVENT_PAGE = 'workshop-building-agentic-ai-platform/content/introduction/getting-started/aws-event.en.md';
const WORKSHOP_CLEANUP_PAGE = 'workshop-building-agentic-ai-platform/content/cleanup/index.en.md';
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

/** Clone step shared by the self-hosted paths. */
function cloneStep(folder: string, sourceQuote: string, sourceFile: string, heading?: string): QuickstartStep {
  return {
    title: 'Clone the repository and enter the project folder',
    command: `git clone https://github.com/aws-samples/sample-ai-agent-factory.git\ncd sample-ai-agent-factory/${folder}`,
    note: 'Folder names are exact and case-sensitive.',
    source: { file: sourceFile, heading, quote: sourceQuote },
  };
}

export const quickstarts: Quickstart[] = [
  {
    id: 'workshop-event',
    projectId: 'workshop',
    name: 'At an AWS event',
    intro: 'Workshop Studio provisions a pre-configured account for you. There is nothing to deploy.',
    expectedTime: {
      value: 'No deploy; start the modules as soon as you have the account',
      source: {
        file: WORKSHOP_README,
        heading: 'Running the workshop',
        quote: 'Nothing to deploy yourself.',
      },
    },
    steps: [
      {
        title: WORKSHOP_LINK_LABEL,
        href: WORKSHOP_URL,
        hrefLabel: WORKSHOP_LINK_LABEL,
        note: 'The workshop guide is published on AWS Builder Center (Workshop Studio).',
        source: {
          file: ROOT_README,
          quote: 'in the AWS workshop catalog, listed on [AWS Builder Center]',
        },
      },
      {
        title: 'Sign in to the pre-provisioned AWS account',
        note: 'All workshop resources are deployed to us-west-2. Log out of other AWS console sessions first.',
        source: {
          file: WORKSHOP_EVENT_PAGE,
          heading: 'Before you start',
          quote: 'All workshop resources are deployed to **US West (Oregon) / us-west-2**.',
        },
      },
      {
        title: 'Open the workshop IDE from the Event outputs',
        note: 'On the Event dashboard, find the row with stack name code-editor and open the URL value.',
        source: {
          file: WORKSHOP_EVENT_PAGE,
          heading: 'Open the workshop IDE',
          quote: 'find the row with stack name `code-editor`, and click the `URL` value',
        },
      },
      {
        title: 'Start at Module 1 and pick a track',
        note: 'Module 1 ends with a track selector: Fast Path, Build the Platform, or Full Journey.',
        source: {
          file: WORKSHOP_README,
          heading: 'Choose your track',
          quote: 'All tracks share **Module 1**, which ends with a track selector',
        },
      },
    ],
    teardown: {
      text: 'Nothing to do. Workshop Studio cleans up the account when the event ends.',
      source: {
        file: WORKSHOP_CLEANUP_PAGE,
        quote: 'Workshop Studio will automatically clean up your account resources when the event ends.',
      },
    },
  },

  {
    id: 'workshop-self-paced',
    projectId: 'workshop',
    name: 'Self-paced in your own account',
    intro: 'One deploy script provisions the same five CloudFormation stacks and browser IDE that events use. Use a dedicated account you can tear down.',
    expectedTime: facts.workshop.firstDeploy,
    steps: [
      cloneStep(
        'workshop-building-agentic-ai-platform',
        'cd sample-ai-agent-factory/workshop-building-agentic-ai-platform',
        WORKSHOP_README,
        'Quick start (self-paced)',
      ),
      {
        title: 'Set a validated region',
        command: 'aws configure set region us-west-2   # or us-east-1, eu-west-1',
        note: 'us-west-2 is the default. us-east-1 and eu-west-1 are also validated. Other regions are not supported.',
        source: {
          file: WORKSHOP_README,
          heading: 'Quick start (self-paced)',
          quote: 'aws configure set region us-west-2   # or us-east-1, eu-west-1',
        },
      },
      {
        title: 'Deploy all five stacks',
        command: './scripts/self-service-deploy.sh',
        note: 'About 30 to 45 minutes. The script runs a preflight check, then prints the IDE URL and password at the end.',
        source: {
          file: WORKSHOP_README,
          heading: 'Quick start (self-paced)',
          quote: '~30-45 min; prints the IDE URL + password at the end',
        },
      },
      {
        title: 'Verify the environment',
        command: './scripts/self-test.sh -r "$(aws configure get region)"',
        note: 'Expect 5 passed, 0 failed.',
        source: {
          file: WORKSHOP_README,
          heading: 'Quick start (self-paced)',
          quote: './scripts/self-test.sh -r "$(aws configure get region)"',
        },
      },
      {
        title: 'Open the IDE and start at Module 1',
        note: 'Sign in with the generated IdePassword. Run every module command inside the IDE terminal or notebooks, not on your laptop.',
        source: {
          file: WORKSHOP_README,
          heading: 'Quick start (self-paced)',
          quote: 'sign in with the generated `IdePassword`',
        },
      },
    ],
    teardown: {
      text: 'Tear everything down to stop charges.',
      command: './deploy-cfn.sh destroy',
      source: {
        file: WORKSHOP_README,
        heading: 'Delete Everything',
        quote: './deploy-cfn.sh destroy',
      },
    },
  },

  {
    id: 'self-service',
    projectId: 'self-service',
    name: 'Deploy the platform',
    intro: 'One script validates prerequisites, deploys the CDK stack, builds the frontend and prints the URLs.',
    expectedTime: facts['self-service'].firstDeploy,
    steps: [
      cloneStep('Agentic-ai-self-service', 'cd sample-ai-agent-factory/<chosen-project>', ROOT_README, 'Quick Start'),
      {
        title: 'Deploy with at least one Cognito user',
        command:
          '# Minimal deploy (dev environment, us-east-1)\nCOGNITO_USERS="user@example.com" ./scripts/deploy.sh\n\n# Specific environment\nCOGNITO_USERS="user@example.com" ENVIRONMENT_NAME=prod ./scripts/deploy.sh\n\n# Another region (Frankfurt)\nCOGNITO_USERS="user@example.com" AWS_REGION=eu-central-1 ./scripts/deploy.sh',
        note: 'A first-time deploy takes roughly 15 to 20 minutes. Lambda code is packaged by CDK; no Docker build or ECR push is needed.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Quickstart',
          quote: 'COGNITO_USERS="user@example.com" ./scripts/deploy.sh',
        },
      },
      {
        title: 'Assign a persona on first sign-in',
        command:
          'POOL_ID=$(aws cognito-idp list-user-pools --max-results 40 --region us-east-1 \\\n  --query "UserPools[?Name==\'agentcore-workflow-dev-users\'].Id | [0]" --output text)\n\n# Full access (all scopes) + admin UI + registry approver:\nfor g in g-admins-super t-admin registry-admin; do\n  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" \\\n    --username you@example.com --group-name "$g" --region us-east-1\ndone',
        note: 'COGNITO_USERS creates users with no group, so a new user is read-only until you assign one. Always pass the region you deployed to. Sign out and back in after changing groups.',
        source: {
          file: SELF_SERVICE_README,
          quote: 'pre-creates Cognito **users** but assigns them to **no group**',
        },
      },
      {
        title: 'Open the frontend URL the script printed',
        note: 'The script prints the CloudFront frontend URL and the API Gateway URL. Both are also CloudFormation stack outputs.',
        source: {
          file: SELF_SERVICE_README,
          heading: 'Accessing the Platform',
          quote: 'After deployment completes, the script prints two URLs:',
        },
      },
    ],
    teardown: {
      text: 'The cleanup script deletes every AgentCore resource the platform created, empties the S3 buckets and runs cdk destroy. It prompts for confirmation.',
      command: './scripts/cleanup.sh',
      source: {
        file: SELF_SERVICE_README,
        heading: 'Cleanup',
        quote: './scripts/cleanup.sh',
      },
    },
  },

  {
    id: 'mcp-gateway',
    projectId: 'mcp-gateway',
    name: 'Deploy and prove the governance',
    intro: 'Five steps, about 5 minutes. Prerequisites: Node.js with the CDK CLI at 2.1129.0, Python 3.12+, and AWS credentials for the target account.',
    expectedTime: facts['mcp-gateway'].firstDeploy,
    steps: [
      {
        title: 'Set the region and install the Python dependencies',
        command:
          'export AWS_REGION=us-west-2\npython3 -m venv .venv && source .venv/bin/activate\npip install -r cdk/requirements.txt -r requirements-dev.txt',
        note: 'Run from the enterprise-mcp-governance-gateway folder of the cloned repository.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: 'pip install -r cdk/requirements.txt -r requirements-dev.txt',
        },
      },
      {
        title: '1. Deploy the gateway',
        command: 'cd cdk && cdk bootstrap && cdk deploy EnterpriseMcpGatewayStack --require-approval never && cd ..',
        note: 'One stack, about 2 minutes.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: 'cd cdk && cdk bootstrap && cdk deploy EnterpriseMcpGatewayStack --require-approval never && cd ..',
        },
      },
      {
        title: '2. Create the demo users',
        command: 'bash scripts/seed-demo-users.sh',
        note: 'CloudFormation cannot set a Cognito password, so a post-deploy script does it.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: '# 2. create the demo users (CloudFormation can\'t set a Cognito password)',
        },
      },
      {
        title: '3. Mint a JWT as admin@example.com',
        command: 'source scripts/get-token.sh',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: '# 3. mint a JWT as admin@example.com',
        },
      },
      {
        title: '4. Run the five governance tests against the live gateway',
        command:
          'GATEWAY_URL="$(aws ssm get-parameter --region "$AWS_REGION" \\\n  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)" \\\nAUTH_TOKEN="$AGENTCORE_JWT" python3 -m pytest tests/integration -q',
        note: 'Expect 5 passed. Three Atlassian tests skip unless you also deploy the connector.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: '# 4. prove it: 5 governance tests against the LIVE gateway, never mocked',
        },
      },
      {
        title: '5. Drive it from a real agent',
        command: 'bash scripts/connect-coding-agent.sh kiro     # or: claude-register',
        note: 'Then work through the governance walkthrough in the README to see allow, deny, block and redact.',
        source: {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: 'bash scripts/connect-coding-agent.sh kiro     # or: claude-register',
        },
      },
    ],
    teardown: {
      text: 'Disconnect the MCP client first. If you deployed the Atlassian connector, destroy its two stacks before the gateway stack. CloudWatch log groups are not removed by cdk destroy.',
      command: 'cd cdk\ncdk destroy EnterpriseMcpGatewayStack\ncd ..',
      source: {
        file: GATEWAY_README,
        heading: 'Teardown',
        quote: 'cdk destroy EnterpriseMcpGatewayStack',
      },
    },
  },

  {
    id: 'blueprint',
    projectId: 'blueprint',
    name: 'Prerequisites and deployment sequence',
    intro: 'The Blueprint is a multi-account rollout, not a single command. Start with one representative Workstream cell and prove the complete lifecycle before onboarding more.',
    expectedTime: facts.blueprint.firstDeploy,
    steps: [
      {
        title: 'Confirm the prerequisites (README section 5)',
        note: 'Node.js 20 or later, Python 3.12 or later, AWS CLI v2 and AWS CDK v2. An AWS Organizations landing zone with Management, Platform and Workstream account roles. A GitHub organization with an AWS CodeConnections connection. Bedrock model access in the target Region. Administrator access for the initial bootstrap only.',
        href: blob(BLUEPRINT_README, '5-prerequisites'),
        hrefLabel: 'README section 5, Prerequisites',
        source: {
          file: BLUEPRINT_README,
          heading: '5. Prerequisites',
          quote: 'At least Management, Platform, and Workstream account roles',
        },
      },
      {
        title: '6.1 One-time setup',
        command:
          'git clone https://github.com/aws-samples/sample-ai-agent-factory.git\ncd sample-ai-agent-factory/enterprise-agentic-ai-platform-blueprint\nnpm ci\nnpm run build\nnpm test\nnpm run lint\nnpm run scrub\n\nexport AWS_REGION=eu-west-1\nexport AWS_DEFAULT_REGION="$AWS_REGION"\nexport CDK_DEFAULT_REGION="$AWS_REGION"',
        note: 'Set all three Region variables; setting only CDK_DEFAULT_REGION is insufficient.',
        href: blob(BLUEPRINT_README, '61-one-time-setup'),
        hrefLabel: 'README section 6.1, One-time setup',
        source: {
          file: BLUEPRINT_README,
          heading: '6.1 One-time setup',
          quote: 'cd sample-ai-agent-factory/enterprise-agentic-ai-platform-blueprint',
        },
      },
      {
        title: '6.2 Configuration',
        note: 'The CDK application reads agenticai/* context values. Keep real account IDs, secret ARNs, tokens and generated Registry context outside source control, and pin agenticai/githubBranch when deploying an unmerged branch.',
        href: blob(BLUEPRINT_README, '62-configuration'),
        hrefLabel: 'README section 6.2, Configuration',
        source: {
          file: BLUEPRINT_README,
          heading: '6.2 Configuration',
          quote: 'The CDK application reads `agenticai/*` context values.',
        },
      },
      {
        title: '6.3 Bootstrap with scoped policies',
        note: 'Generate one CloudFormation execution policy per account and Region, validate each with IAM Access Analyzer, then run the cross-account bootstrap. Do not use AdministratorAccess as the execution policy.',
        href: blob(BLUEPRINT_README, '63-bootstrap-with-scoped-policies'),
        hrefLabel: 'README section 6.3, Bootstrap with scoped policies',
        source: {
          file: BLUEPRINT_README,
          heading: '6.3 Bootstrap with scoped policies',
          quote: 'Generate one CloudFormation execution policy per account and Region:',
        },
      },
      {
        title: '6.4 Deploy the Platform control plane',
        note: 'Create the Platform pipeline stack with Gateway invoke permissions disabled, run it, and review Registry descriptors before approval.',
        href: blob(BLUEPRINT_README, '64-deploy-the-platform-control-plane'),
        hrefLabel: 'README section 6.4, Deploy the Platform control plane',
        source: {
          file: BLUEPRINT_README,
          heading: '6.4 Deploy the Platform control plane',
          quote: 'Create or update `AgenticAI-PlatformPipelineStack` with Gateway invoke permissions disabled:',
        },
      },
      {
        title: '6.5 Onboard a Workstream cell',
        note: 'Resolve one Registry context file per environment, deploy the Workload pipeline root, complete the two-phase Gateway permission handoff, then approve GatewayPermissionReady.',
        href: blob(BLUEPRINT_README, '65-onboard-a-workstream-cell'),
        hrefLabel: 'README section 6.5, Onboard a Workstream cell',
        source: {
          file: BLUEPRINT_README,
          heading: '6.5 Onboard a Workstream cell',
          quote: 'Resolve one non-secret Registry context file per environment:',
        },
      },
      {
        title: '6.6 Validation',
        note: 'Run the local gates, synthesize with strict mode, and require clean cdk-nag reports. Live mode fails closed: missing credentials or expected denials are errors, not skips.',
        href: blob(BLUEPRINT_README, '66-validation'),
        hrefLabel: 'README section 6.6, Validation',
        source: {
          file: BLUEPRINT_README,
          heading: '6.6 Validation',
          quote: 'Live mode fails closed. Missing credentials, probes, resources, or expected denials are errors',
        },
      },
    ],
    teardown: {
      text: 'Retire Platform alias grants first, then run the fail-closed teardown per account role: Workstream, then Platform, then Management. Each run is a dry run until you add --apply. Finish with the residue inventory.',
      command:
        'python3 scripts/final_teardown.py \\\n  --account-role workstream \\\n  --expected-account <WORKSTREAM_ACCOUNT> \\\n  --region eu-west-1',
      source: {
        file: BLUEPRINT_README,
        heading: '16. Cleanup',
        quote: 'python3 scripts/final_teardown.py',
      },
    },
  },
];

/** Quickstarts for a project, in display order. */
export function getQuickstarts(projectId: ProjectId): Quickstart[] {
  return quickstarts.filter(q => q.projectId === projectId);
}
