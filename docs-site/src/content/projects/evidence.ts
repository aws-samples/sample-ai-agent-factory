/**
 * Evidence: what each project tests, the command that runs it, what a pass
 * proves, and how much of it runs against live AWS. Rendered in the "Evidence"
 * section of every project page.
 *
 * Pure data. Every `quote` is a verbatim substring of the cited repository file
 * and every `heading` is a real Markdown heading (checked by projects.test.ts).
 * Commands are copied from the cited file without edits.
 */
import type { ProjectId } from '../data';
import type { Source } from '../facts';

const WORKSHOP = 'workshop-building-agentic-ai-platform';
const WORKSHOP_README = `${WORKSHOP}/README.md`;
const WORKSHOP_SELF_TEST = `${WORKSHOP}/scripts/self-test.sh`;
const WORKSHOP_WALKTHROUGH_PARITY = `${WORKSHOP}/scripts/verify-walkthrough-parity.py`;
const WORKSHOP_ASSETS_PARITY = `${WORKSHOP}/scripts/verify-assets-parity.py`;
const WORKSHOP_IDE_POLICY_PARITY = `${WORKSHOP}/scripts/verify-ide-policy-parity.py`;
const SELF_SERVICE_README = 'Agentic-ai-self-service/README.md';
const SELF_SERVICE_DEVELOPMENT = 'Agentic-ai-self-service/docs/DEVELOPMENT.md';
const GATEWAY_README = 'enterprise-mcp-governance-gateway/README.md';
const BLUEPRINT_README = 'enterprise-agentic-ai-platform-blueprint/README.md';

/** One test, script or gate. */
export interface EvidenceItem {
  id: string;
  /** What is tested, in a few words. */
  what: string;
  /** Command copied verbatim from the cited file, when it gives one. */
  command?: string;
  /** What a pass proves, in plain words. */
  proves: string;
  /** Ordered list rendered under `proves` (for example the completion conditions). */
  list?: string[];
  sources: Source[];
}

/** Evidence block for one project. */
export interface ProjectEvidence {
  /** One sentence on how the project verifies itself. */
  intro: string;
  /** What runs against real AWS and what does not. */
  live: { text: string; sources: Source[] };
  items: EvidenceItem[];
}

export const evidence: Record<ProjectId, ProjectEvidence> = {
  workshop: {
    intro:
      'A self-paced deployment is checked by a post-deploy health check; the workshop content and its infrastructure copies are guarded by parity scripts that run against the repository.',
    live: {
      text: 'self-test.sh runs against the deployed account: it verifies the four platform stacks plus the IDE and that key endpoints respond, and exits non-zero if any check fails. The parity scripts and the module unit tests need no AWS account.',
      sources: [
        { file: WORKSHOP_SELF_TEST, quote: 'Verifies the four platform stacks + the IDE' },
        { file: WORKSHOP_SELF_TEST, quote: 'Exits non-zero if any check fails.' },
      ],
    },
    items: [
      {
        id: 'self-test',
        what: 'Post-deploy health check (scripts/self-test.sh)',
        command: './scripts/self-test.sh -r "$(aws configure get region)"   # expect: 5 passed, 0 failed',
        proves: 'The five stacks are deployed in the region you chose and their key endpoints respond. Expect 5 passed, 0 failed before starting Module 1.',
        sources: [
          { file: WORKSHOP_README, heading: 'Quick start (self-paced)', quote: '# expect: 5 passed, 0 failed' },
          { file: WORKSHOP_README, heading: 'Repository structure', quote: 'Post-deploy health check (5 stacks + endpoints)' },
        ],
      },
      {
        id: 'walkthrough-parity',
        what: 'CLI and notebook parity (scripts/verify-walkthrough-parity.py)',
        proves:
          'A CLI walkthrough page and its notebook write the same source files, so participants on either path run identical code.',
        sources: [
          {
            file: WORKSHOP_WALKTHROUGH_PARITY,
            quote: 'Fail when a CLI walkthrough and its notebook write different source files.',
          },
        ],
      },
      {
        id: 'assets-parity',
        what: 'Assets bucket parity (scripts/verify-assets-parity.py)',
        proves:
          'The assets/ copy served from the shared assets bucket matches the git-tracked static/ and content/ sources, so a fix pushed to git cannot leave a stale copy behind.',
        sources: [{ file: WORKSHOP_ASSETS_PARITY, quote: 'Fail when assets/ drifts from the git-tracked sources' }],
      },
      {
        id: 'ide-policy-parity',
        what: 'Participant IAM policy parity (scripts/verify-ide-policy-parity.py)',
        proves:
          'The five participant IAM policy files match the copies embedded in code-editor.yaml, and each stays under the IAM managed-policy size quota.',
        sources: [
          { file: WORKSHOP_IDE_POLICY_PARITY, quote: 'Guard the five participant IAM policies against drift.' },
          {
            file: WORKSHOP_IDE_POLICY_PARITY,
            quote: 'It also re-checks the IAM managed-policy size quota (6144 chars minified)',
          },
        ],
      },
      {
        id: 'contributor-gates',
        what: 'Contributor gates: cfn-lint and the module unit tests',
        proves: 'Changed CloudFormation templates lint clean and the module unit tests pass before a change is pushed.',
        sources: [
          {
            file: WORKSHOP_README,
            heading: 'Content guidelines',
            quote: 'Run `cfn-lint` on any changed template and the module unit tests (`pytest`) before pushing.',
          },
        ],
      },
    ],
  },

  'self-service': {
    intro:
      'The README lists three local test suites and points to integration tests and live verification scripts that run against a deployed stack.',
    live: {
      text: 'Integration tests run against a real deployed stack with zero mocking: they deploy each built-in template, invoke the deployed runtimes, verify responses, and clean up all resources. The backend, CDK and frontend suites below run locally.',
      sources: [
        { file: SELF_SERVICE_README, heading: 'Running Tests', quote: 'Integration tests run against a real deployed stack' },
        {
          file: SELF_SERVICE_DEVELOPMENT,
          heading: 'Integration Tests',
          quote: 'Integration tests perform real AWS API calls with zero mocking.',
        },
        {
          file: SELF_SERVICE_DEVELOPMENT,
          heading: 'Integration Tests',
          quote: 'invoke the deployed runtimes, verify responses, and clean up all resources',
        },
      ],
    },
    items: [
      {
        id: 'backend',
        what: 'Backend unit and property tests',
        command: 'cd backend && pip install -e ".[dev]" && pytest   # backend unit + property tests',
        proves: 'The backend logic holds under unit tests and property-based tests (Pytest with Hypothesis).',
        sources: [
          { file: SELF_SERVICE_README, heading: 'Running Tests', quote: 'cd backend && pip install -e ".[dev]" && pytest' },
          { file: SELF_SERVICE_DEVELOPMENT, quote: 'Pytest + Hypothesis (backend properties)' },
        ],
      },
      {
        id: 'infra',
        what: 'CDK assertions',
        command: 'cd infra && pip install -r requirements.txt && pytest tests/ -v   # CDK assertions',
        proves:
          'The synthesized CloudFormation template contains the expected serverless resources (API Gateway, Lambda, Step Functions, DynamoDB) and none of the removed ones (VPC, ECS, ALB, ECR, CodeBuild).',
        sources: [
          {
            file: SELF_SERVICE_README,
            heading: 'Running Tests',
            quote: 'cd infra && pip install -r requirements.txt && pytest tests/ -v',
          },
          {
            file: SELF_SERVICE_DEVELOPMENT,
            quote: 'Verifies the synthesized CloudFormation template contains expected serverless resources',
          },
        ],
      },
      {
        id: 'frontend',
        what: 'Frontend tests',
        command: 'cd frontend && npm install && npm test            # frontend tests',
        proves: 'The frontend passes its unit and property tests (Vitest with fast-check).',
        sources: [
          { file: SELF_SERVICE_README, heading: 'Running Tests', quote: 'cd frontend && npm install && npm test' },
          { file: SELF_SERVICE_DEVELOPMENT, quote: 'Vitest + fast-check (frontend)' },
        ],
      },
      {
        id: 'integration',
        what: 'Integration tests against a deployed stack',
        command:
          'cd backend\n\n# Set required environment variables\nexport API_GATEWAY_URL="https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com"\nexport AWS_REGION="us-east-1"\n\n# Run integration tests only\npytest -m integration -v',
        proves:
          'Each built-in template deploys, the deployed runtimes answer, and every resource is cleaned up afterwards. Needs AWS credentials, a deployed stack and the API Gateway URL.',
        sources: [{ file: SELF_SERVICE_DEVELOPMENT, heading: 'Integration Tests', quote: 'pytest -m integration -v' }],
      },
      {
        id: 'live-verification',
        what: 'Live verification scripts',
        proves:
          'Standalone probes drive the shipped product code against the real external system and print a PASS or FAIL line per check, exiting non-zero on any failure.',
        sources: [
          {
            file: SELF_SERVICE_DEVELOPMENT,
            heading: 'Live verification scripts',
            quote: 'Each drives the shipped product code against the real',
          },
        ],
      },
    ],
  },

  'mcp-gateway': {
    intro:
      'The README ships five integration tests that run against the deployed gateway, a one-command smoke test, and local unit tests for the interceptors.',
    live: {
      text: 'C1 to C5 run against the deployed, live gateway and are never mocked. C6 is the local interceptor unit tests, the only row that needs no AWS.',
      sources: [
        { file: GATEWAY_README, heading: 'What the tests prove', quote: 'C6 is the local interceptor unit tests' },
        {
          file: GATEWAY_README,
          heading: 'Quickstart',
          quote: '# 4. prove it: 5 governance tests against the LIVE gateway, never mocked',
        },
      ],
    },
    items: [
      {
        id: 'integration',
        what: 'Five governance tests against the live gateway (tests/integration)',
        command:
          'source scripts/get-token.sh\nGATEWAY_URL="$(aws ssm get-parameter --region "$AWS_REGION" \\\n  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)" \\\nAUTH_TOKEN="$AGENTCORE_JWT" \\\n  python3 -m pytest tests/integration -v',
        proves:
          'tools/list is Cedar-filtered (C1), an allowed call succeeds (C2), a forbidden tool is denied (C3), a SQL-injection payload is blocked by the request interceptor (C4), and PII in a response is redacted (C5). Expect 5 passed; the three Atlassian tests skip unless the connector is also deployed.',
        sources: [
          { file: GATEWAY_README, heading: 'Run tests', quote: 'python3 -m pytest tests/integration -v' },
          {
            file: GATEWAY_README,
            heading: 'What the tests prove',
            quote: 'Cedar-filtered (C1), an allowed call succeeds (C2), a forbidden tool is denied (C3)',
          },
          {
            file: GATEWAY_README,
            heading: 'What the tests prove',
            quote: 'SQL-injection payload is blocked by the REQUEST interceptor (C4), and PII in a response is',
          },
          { file: GATEWAY_README, heading: 'Quickstart', quote: '3 Atlassian tests skip unless you also deploy the' },
        ],
      },
      {
        id: 'smoke',
        what: 'Curl smoke test (scripts/test-gateway.sh)',
        command: 'source scripts/get-token.sh\nbash scripts/test-gateway.sh',
        proves: 'The same idea in one command: the deployed gateway, resolved from SSM, answers an authenticated request.',
        sources: [{ file: GATEWAY_README, heading: 'Run tests', quote: 'bash scripts/test-gateway.sh' }],
      },
      {
        id: 'unit',
        what: 'Interceptor and Cedar-parsing unit tests (tests/unit)',
        command: 'python3 -m pytest tests/unit -v',
        proves:
          'The interceptor and Cedar-parsing logic behaves correctly against fakes. No AWS and no token: these pass whether or not anything is deployed, so they check code changes, not a deploy.',
        sources: [
          { file: GATEWAY_README, heading: 'Run tests', quote: 'python3 -m pytest tests/unit -v' },
          { file: GATEWAY_README, heading: 'Run tests', quote: 'They pass whether or not anything is deployed' },
        ],
      },
    ],
  },

  blueprint: {
    intro:
      'The README defines local gates, a strict synth for infrastructure changes, and seven conditions that a behavior-changing revision must meet before it counts as complete.',
    live: {
      text: 'Live mode fails closed: missing credentials, probes, resources, or expected denials are errors, not passing skips. The adversarial harness validates the evidence schema, twin ledger, sanitization, and domain catalog offline.',
      sources: [
        {
          file: BLUEPRINT_README,
          heading: '6.6 Validation',
          quote: 'Live mode fails closed. Missing credentials, probes, resources, or expected denials are errors',
        },
        {
          file: BLUEPRINT_README,
          heading: '10.2 Threat and evidence model',
          quote: 'validates the evidence schema, twin ledger, sanitization, and domain catalog offline',
        },
      ],
    },
    items: [
      {
        id: 'local-gates',
        what: 'Local gates',
        command:
          'npm run build\nnpm test\nnpm run lint\nnpm run scrub\n\npython3 -m pytest tests/adversarial/unit -q\npython3 -m pytest scripts/test_final_teardown.py scripts/test_residue_inventory.py -q',
        proves:
          'The packages build, their unit tests and lint pass, the scrub script passes, the adversarial harness unit tests pass, and the teardown and residue-inventory scripts are tested.',
        sources: [{ file: BLUEPRINT_README, heading: '6.6 Validation', quote: 'python3 -m pytest tests/adversarial/unit -q' }],
      },
      {
        id: 'strict-synth',
        what: 'Strict synth and cdk-nag for infrastructure changes',
        command: 'npx cdk synth --strict',
        proves: 'The exact account and Region topology synthesizes, the generated templates are reviewed, and cdk-nag reports are clean.',
        sources: [
          {
            file: BLUEPRINT_README,
            heading: '6.6 Validation',
            quote: 'synthesize the exact account and Region topology with `npx cdk synth --strict`',
          },
        ],
      },
      {
        id: 'completion',
        what: 'Seven conditions for a behavior-changing revision',
        proves: 'A behavior-changing revision is complete only after all seven:',
        list: [
          'reviewed pipeline deployment;',
          'an authorized positive call;',
          'an unauthorized adversarial twin with an exact denial;',
          'a mutation proving the test fails when the control is removed;',
          'rollback and re-run to green;',
          'centralized logs, metrics, and traces where claimed;',
          'dependency-ordered teardown and direct resource inventory.',
        ],
        sources: [
          {
            file: BLUEPRINT_README,
            heading: '6.6 Validation',
            quote: 'a mutation proving the test fails when the control is removed;',
          },
          {
            file: BLUEPRINT_README,
            heading: '6.6 Validation',
            quote: 'dependency-ordered teardown and direct resource inventory.',
          },
        ],
      },
    ],
  },
};

/** Evidence for one project. */
export function getEvidence(projectId: ProjectId): ProjectEvidence {
  return evidence[projectId];
}
