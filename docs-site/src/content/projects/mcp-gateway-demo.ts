/**
 * Sourced content for the Enterprise MCP Governance Gateway pages: the verified
 * request flow, the six-query demo, the claims that reach Cedar, per-statement
 * notes for forbid-destructive-db.cedar, the honest notes on what does not
 * engage for the seeded users, and a purpose line for every policy file.
 *
 * Pure data plus one pure helper. Every `quote` is a verbatim substring of the
 * cited repository file and every `heading` is a real heading (checked by
 * projects.test.ts). Numbers in the copy come from the quoted text.
 */
import type { Source } from '../facts';

const GATEWAY = 'enterprise-mcp-governance-gateway';
export const GATEWAY_README = `${GATEWAY}/README.md`;
export const GATEWAY_POLICIES_DIR = `${GATEWAY}/policies`;
export const GATEWAY_MANIFEST = `${GATEWAY_POLICIES_DIR}/manifest.json`;
const REQUEST_INTERCEPTOR = `${GATEWAY}/lambdas/request-interceptor/index.py`;
const REQUEST_GUARDRAIL_HELPER = `${GATEWAY}/lambdas/request-interceptor/guardrail.py`;
const GATEWAY_STACK = `${GATEWAY}/cdk/enterprise_gateway/gateway_stack.py`;
const FORBID_DESTRUCTIVE_DB = `${GATEWAY_POLICIES_DIR}/forbid-destructive-db.cedar`;

/** The README section the request-flow strip is drawn from. */
export const VERIFIED_ARCHITECTURE: Source = { file: GATEWAY_README, heading: 'Verified architecture' };

/** The README section the six-query demo table is copied from. */
export const DEMO_QUERIES_SOURCE: Source = {
  file: GATEWAY_README,
  quote: 'Ask the agent (or the headless script asks for you). The **outcome is produced by',
};

/** File name of the policy rendered statement by statement on the project page. */
export const FEATURED_POLICY_FILE = 'forbid-destructive-db.cedar';

/** One hop in the verified request path. */
export interface FlowStep {
  id: string;
  name: string;
  detail: string;
  source: Source;
}

/** The request path as drawn in the README "Verified architecture" diagram. */
export const requestFlow: FlowStep[] = [
  {
    id: 'client',
    name: 'MCP client',
    detail: 'Sends each request over HTTPS with a Cognito access token as the Bearer JWT.',
    source: { ...VERIFIED_ARCHITECTURE, quote: 'HTTPS + Bearer JWT (Cognito access token)' },
  },
  {
    id: 'gateway',
    name: 'AgentCore Gateway',
    detail: 'Validates the JWT against the Cognito OIDC discovery URL (CUSTOM_JWT authorizer).',
    source: { ...VERIFIED_ARCHITECTURE, quote: '1. JWT validated against Cognito OIDC discovery URL' },
  },
  {
    id: 'request-interceptor',
    name: 'Request interceptor',
    detail: 'Lambda that writes the audit record and blocks SQL-injection and abuse patterns.',
    source: { ...VERIFIED_ARCHITECTURE, quote: '2. REQUEST interceptor Lambda  (audit, SQL/abuse block)' },
  },
  {
    id: 'cedar',
    name: 'Cedar policy engine',
    detail: 'Evaluates the Cedar policies in ENFORCE mode and allows or denies the tool call.',
    source: { ...VERIFIED_ARCHITECTURE, quote: '3. Cedar policy engine (ENFORCE)' },
  },
  {
    id: 'target',
    name: 'Tool Lambda',
    detail: 'The target Lambda runs with the GATEWAY_IAM_ROLE credential.',
    source: { ...VERIFIED_ARCHITECTURE, quote: '4. Target Lambda invoked (GATEWAY_IAM_ROLE credential)' },
  },
  {
    id: 'response-interceptor',
    name: 'Response interceptor',
    detail: 'Lambda that redacts PII and truncates large payloads before the result returns to the client.',
    source: { ...VERIFIED_ARCHITECTURE, quote: '5. RESPONSE interceptor Lambda (PII redact, truncate)' },
  },
];

/** One row of the README six-query walkthrough. */
export interface DemoQuery {
  /** Letter as printed in the README table. */
  id: 'A' | 'B' | 'C' | 'D' | 'E' | 'F';
  /** What to ask the agent. */
  ask: string;
  /** Expected governance outcome, in plain words. */
  outcome: string;
  /** The control that produces the outcome, as named in the README. */
  enforcedBy: string;
  source: Source;
}

export const demoQueries: DemoQuery[] = [
  {
    id: 'A',
    ask: 'List every tool from enterprise-gateway',
    outcome:
      'Only the Cedar-permitted tools appear (for example DocsAPI___get_page, search_pages, list_spaces, execute_query, export_pii_report). Forbidden tools like drop_table are filtered out of the list entirely.',
    enforcedBy: 'Cedar (visibility)',
    source: {
      file: GATEWAY_README,
      quote: 'Forbidden tools like `drop_table` are **filtered out of the list entirely**.',
    },
  },
  {
    id: 'B',
    ask: 'Call DocsAPI___get_page with pageId arch-overview',
    outcome: 'Succeeds and returns the page content.',
    enforcedBy: 'Cedar allow-docs-read (ALLOW)',
    source: { file: GATEWAY_README, quote: 'Cedar `allow-docs-read` (ALLOW)' },
  },
  {
    id: 'C',
    ask: 'Call DatabaseAPI___drop_table with tableName users',
    outcome:
      'The agent cannot even attempt it. Cedar filtered the tool out at step A, so a well-behaved agent replies that no such tool exists. Called directly with curl, the gateway answers Tool Execution Denied and names forbid_destructive_db.',
    enforcedBy: 'Cedar forbid-destructive-db (DENY)',
    source: {
      file: GATEWAY_README,
      quote: 'Cedar filtered it out at step A, so a well-behaved agent replies that no such tool exists',
    },
  },
  {
    id: 'D',
    ask: 'Call DatabaseAPI___execute_query with query DROP TABLE users; --',
    outcome: 'Blocked before execution with the message Request blocked: dangerous SQL pattern detected.',
    enforcedBy: 'REQUEST interceptor Lambda',
    source: {
      file: GATEWAY_README,
      quote: '**Blocked before execution:** `Request blocked: dangerous SQL pattern detected',
    },
  },
  {
    id: 'E',
    ask: 'Call DatabaseAPI___export_pii_report for department engineering',
    outcome:
      'PII comes back masked, for example Name: {NAME}, SSN: {US_SOCIAL_SECURITY_NUMBER}. The {TYPE} form means the managed Bedrock Guardrail anonymized it. With the guardrail disabled the local regex backstop produces [REDACTED_SSN] instead. Business hours only; see the note below.',
    enforcedBy: 'RESPONSE interceptor + Guardrail',
    source: {
      file: GATEWAY_README,
      quote: 'the `{TYPE}` form means the **managed Bedrock Guardrail** anonymized it',
    },
  },
  {
    id: 'F',
    ask: 'Call DocsAPI___create_page (a write)',
    outcome:
      'Denied for demo users with No policy applies to the request (denied by default). create_page is gated on role admin, and custom:role never reaches the access token the gateway validates.',
    enforcedBy: 'Cedar (default deny)',
    source: { file: GATEWAY_README, quote: '`No policy applies to the request (denied by default)`' },
  },
];

/** Why case C never shows a deny to the agent. */
export const hidingWinsNote = {
  text: 'Cedar both hides a forbidden tool and denies it, and hiding wins. An agent therefore never triggers the deny in case C; the README shows a direct curl call that does.',
  source: {
    file: GATEWAY_README,
    quote: 'Cedar both hides a forbidden tool and denies it, and hiding wins',
  } satisfies Source,
};

/** The business-hours gate that affects case E. */
export const businessHoursNote = {
  text: 'export_pii_report and query_audit_logs are gated to 09:00 to 17:00 UTC by the request interceptor. Outside that window they are blocked before execution.',
  source: {
    file: GATEWAY_README,
    quote: 'by the REQUEST interceptor. Outside that window they',
  } satisfies Source,
};

/** One JWT claim and whether it reaches Cedar as a principal tag. */
export interface ClaimRow {
  claim: string;
  token: 'Access token' | 'ID token only';
  reachesCedar: boolean;
  note: string;
  source: Source;
}

export const claims: ClaimRow[] = [
  {
    claim: 'sub',
    token: 'Access token',
    reachesCedar: true,
    note: 'Becomes the sub principal tag.',
    source: {
      ...VERIFIED_ARCHITECTURE,
      quote: 'The Cognito **access token** carries `sub`, `username`, and `scope`, which the',
    },
  },
  {
    claim: 'username',
    token: 'Access token',
    reachesCedar: true,
    note: 'With this Cognito setup the access-token username is the user sub UUID, not the email address.',
    source: {
      file: `${GATEWAY_POLICIES_DIR}/sensitive-tool-restrict.cedar`,
      quote: "the access-token `username` claim is the user's",
    },
  },
  {
    claim: 'scope',
    token: 'Access token',
    reachesCedar: true,
    note: 'The demo users share the default aws.cognito.signin.user.admin scope.',
    source: {
      file: `${GATEWAY_POLICIES_DIR}/sensitive-tool-restrict.cedar`,
      quote: "the demo users share Cognito's default `aws.cognito.signin.user.admin`",
    },
  },
  {
    claim: 'email',
    token: 'ID token only',
    reachesCedar: false,
    note: 'The gateway rejects the ID token because it carries no scope claim.',
    source: {
      file: GATEWAY_README,
      quote: 'token (carries `email` and `custom:role`, and the gateway rejects it with',
    },
  },
  {
    claim: 'custom:role',
    token: 'ID token only',
    reachesCedar: false,
    note: 'Role-gated permits therefore never fire for the seeded users.',
    source: {
      file: GATEWAY_README,
      quote: 'never reaches the policy engine, so the role-gated `permit` never fires.',
    },
  },
];

/** A Cedar statement extracted from a policy file. */
export interface CedarStatement {
  effect: 'permit' | 'forbid';
  /** Action id inside the quotes, for example DatabaseAPI___drop_table. */
  action: string;
  /** The statement text, comments excluded. */
  text: string;
}

/**
 * Split a Cedar policy file into its permit and forbid statements. Comments
 * between statements are dropped. Used by the page (to order the notes) and
 * by the test (to prove every statement has exactly one note).
 */
export function splitCedarStatements(policyText: string): CedarStatement[] {
  const pattern = /(permit|forbid)\s*\(([^;]*?)\)(?:\s*(?:when|unless)\s*\{[^}]*\})?\s*;/g;
  const out: CedarStatement[] = [];
  for (const match of policyText.matchAll(pattern)) {
    const effect = match[1] as CedarStatement['effect'];
    const action = /AgentCore::Action::"([^"]+)"/.exec(match[2])?.[1] ?? '';
    out.push({ effect, action, text: match[0] });
  }
  return out;
}

/** A note about one statement of the featured policy. */
export interface CedarStatementNote {
  effect: 'permit' | 'forbid';
  action: string;
  note: string;
  sources: Source[];
}

export const featuredPolicyNotes: CedarStatementNote[] = [
  {
    effect: 'forbid',
    action: 'DatabaseAPI___drop_table',
    note: 'Forbids drop_table for every authenticated user, whatever their role.',
    sources: [
      {
        file: FORBID_DESTRUCTIVE_DB,
        quote: 'Block ALL users from calling destructive database tools regardless of role.',
      },
    ],
  },
  {
    effect: 'forbid',
    action: 'DatabaseAPI___truncate_table',
    note: 'Forbids truncate_table for everyone. Each tool gets its own single-action statement because the policy engine does not match Cedar action set-membership.',
    sources: [
      {
        file: FORBID_DESTRUCTIVE_DB,
        quote: 'One single-action forbid per tool: the policy engine does not match Cedar',
      },
    ],
  },
  {
    effect: 'forbid',
    action: 'DatabaseAPI___delete_records',
    note: 'Forbids delete_records for everyone.',
    sources: [
      {
        file: FORBID_DESTRUCTIVE_DB,
        quote: 'Block ALL users from calling destructive database tools regardless of role.',
      },
    ],
  },
  {
    effect: 'permit',
    action: 'DatabaseAPI___execute_query',
    note: 'Permits execute_query only when the query argument matches like "SELECT *". In Cedar the asterisk is a wildcard, so this is a case-sensitive prefix match on SELECT followed by a space, not a literal SELECT * statement. A lower-case select does not match and falls to the default deny.',
    sources: [{ file: FORBID_DESTRUCTIVE_DB, quote: 'context.input.query like "SELECT *"' }],
  },
  {
    effect: 'forbid',
    action: 'DatabaseAPI___execute_query',
    note: 'Forbids execute_query when the query contains DROP, DELETE, TRUNCATE, INSERT or UPDATE in upper case. Cedar like is case-sensitive; the case-insensitive catch is the request interceptor regex, compiled with re.IGNORECASE, which runs before Cedar.',
    sources: [
      { file: FORBID_DESTRUCTIVE_DB, quote: 'Forbid execute_query if it contains dangerous patterns.' },
      { file: REQUEST_INTERCEPTOR, quote: 'DANGEROUS_SQL = re.compile(' },
    ],
  },
];

/** A plain statement about a limit of the sample, with its evidence. */
export interface HonestNote {
  id: string;
  title: string;
  text: string;
  sources: Source[];
}

export const honestNotes: HonestNote[] = [
  {
    id: 'role-gates',
    title: 'Three of the five active policy files carry gates that never engage for the seeded users',
    text: 'admin-write-only.cedar permits writes only for role admin. block-large-queries.cedar lifts its bulk_export forbid only for role data-engineer. sensitive-tool-restrict.cedar permits query_audit_logs only for a username equal to an email address. custom:role lives in the ID token only and the access-token username is a sub UUID, so none of these conditions holds for the seeded users. The result is deny, which the files describe as the safe default.',
    sources: [
      {
        file: `${GATEWAY_POLICIES_DIR}/admin-write-only.cedar`,
        quote: 'so for the demo users these permits do not fire and writes',
      },
      {
        file: `${GATEWAY_POLICIES_DIR}/block-large-queries.cedar`,
        quote: 'Forbid bulk export tool unless user has "data-engineer" role.',
      },
      {
        file: `${GATEWAY_POLICIES_DIR}/sensitive-tool-restrict.cedar`,
        quote: "so this exact match won't fire for the demo users",
      },
      {
        file: GATEWAY_README,
        heading: 'Tracked production hardening (not in this sample)',
        quote: 'A Cognito **pre-token-generation Lambda** to surface `custom:role` in the',
      },
    ],
  },
  {
    id: 'guardrail-draft',
    title: 'The managed Guardrail is wired to the unpinned DRAFT version',
    text: 'gateway_stack.py sets GUARDRAIL_VERSION to DRAFT for the interceptor Lambdas, and the guardrail helper defaults to DRAFT when the variable is unset. DRAFT changes whenever the guardrail is edited, so what is enforced is not pinned to a numbered version.',
    sources: [
      { file: GATEWAY_STACK, quote: 'fn.add_environment("GUARDRAIL_VERSION", "DRAFT")' },
      { file: REQUEST_GUARDRAIL_HELPER, quote: 'GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")' },
    ],
  },
  {
    id: 'guardrail-error',
    title: 'On a Guardrail API error the interceptors log and continue',
    text: 'The README states that a guardrail API error on the request path is logged and the local controls still apply, and the request interceptor comments say not to fail the request on a guardrail outage. The regex SQL blocking and the regex PII redaction still run. The README suggests tuning to fail-closed for stricter environments.',
    sources: [
      {
        file: GATEWAY_README,
        heading: 'Security notes',
        quote: 'is safe to demo, but it is **not hardened for production**',
      },
      {
        file: GATEWAY_README,
        heading: 'Managed guardrail (Amazon Bedrock Guardrails)',
        quote: 'A guardrail API error on the request path is',
      },
      { file: REQUEST_INTERCEPTOR, quote: "# Don't fail the request on a guardrail outage; log and continue" },
    ],
  },
];

/** How the deployer validates policies, per the manifest. */
export const manifestValidationNote = {
  text: 'policies/manifest.json creates every policy with validationMode IGNORE_ALL_FINDINGS. The manifest comment describes FAIL_ON_ANY_FINDINGS and warns against falling back without understanding the finding.',
  sources: [
    { file: GATEWAY_MANIFEST, quote: '"validationMode": "IGNORE_ALL_FINDINGS",' },
    { file: GATEWAY_MANIFEST, quote: 'fall back to IGNORE_ALL_FINDINGS without understanding the finding.' },
  ] satisfies Source[],
};

/** Why two of the seven files are not deployed. */
export const disabledPoliciesNote = {
  text: 'atlassian-read-all-write-admin and github-read-only-by-default are listed under disabledPolicies. Their MCP-server targets are not registered on the live gateway, and the policy engine derives its Cedar schema from registered tools, so policies for unregistered tools fail validation with unrecognized action.',
  sources: [
    {
      file: GATEWAY_MANIFEST,
      quote: "registered on the live gateway. The policy engine's Cedar schema is derived",
    },
  ] satisfies Source[],
};

/** One sentence per policy file, taken from the file's own header comment. */
export interface PolicyPurpose {
  /** File name without extension, matching virtual:repo-index `name`. */
  name: string;
  purpose: string;
  /** True when manifest.json lists the file under `policies`; false for `disabledPolicies`. */
  active: boolean;
  sources: Source[];
}

const policyFile = (name: string): string => `${GATEWAY_POLICIES_DIR}/${name}.cedar`;

export const policyPurposes: PolicyPurpose[] = [
  {
    name: 'admin-write-only',
    purpose: 'Only users with role admin can create, update or delete docs pages.',
    active: true,
    sources: [
      { file: policyFile('admin-write-only'), quote: 'Policy 2: Only users with role "admin" can create/update/delete docs.' },
    ],
  },
  {
    name: 'allow-docs-read',
    purpose: 'Allows every authenticated OAuth user to call the read-only Docs tools.',
    active: true,
    sources: [
      {
        file: policyFile('allow-docs-read'),
        quote: 'Policy 1: Allow all authenticated OAuth users to call read-only Docs tools.',
      },
    ],
  },
  {
    name: 'atlassian-read-all-write-admin',
    purpose:
      'Every authenticated user may read and search Jira and Confluence; writes are permitted only for users carrying role atlassian-writer.',
    active: false,
    sources: [
      {
        file: policyFile('atlassian-read-all-write-admin'),
        quote: 'permitted ONLY for users carrying role = "atlassian-writer"',
      },
    ],
  },
  {
    name: 'block-large-queries',
    purpose:
      'Forbids database queries requesting more than 1000 rows and forbids bulk export unless the user has the data-engineer role.',
    active: true,
    sources: [
      {
        file: policyFile('block-large-queries'),
        quote: 'Policy 4: Block queries exceeding the row-limit threshold, and gate bulk export',
      },
      { file: policyFile('block-large-queries'), quote: 'Forbid database queries requesting more than 1000 rows.' },
    ],
  },
  {
    name: 'forbid-destructive-db',
    purpose:
      'Forbids destructive database operations for everyone and constrains execute_query to read-only SELECT statements.',
    active: true,
    sources: [
      {
        file: policyFile('forbid-destructive-db'),
        quote: 'Policy 3: Forbid destructive database operations for everyone, and constrain',
      },
    ],
  },
  {
    name: 'github-read-only-by-default',
    purpose: 'Everyone may read and search GitHub; writes are forbidden unless the user carries the github-writer role tag.',
    active: false,
    sources: [
      {
        file: policyFile('github-read-only-by-default'),
        quote: 'Everyone may read/search; writes forbidden unless the user carries the',
      },
    ],
  },
  {
    name: 'sensitive-tool-restrict',
    purpose:
      'Restricts sensitive tools to specific users or scopes: query_audit_logs to one username and export_pii_report to an OAuth scope.',
    active: true,
    sources: [
      {
        file: policyFile('sensitive-tool-restrict'),
        quote: 'Policy 5: Restrict sensitive tools to specific users / scopes.',
      },
    ],
  },
];

/** Look up the purpose entry for a policy file name (without extension). */
export function policyPurposeFor(name: string): PolicyPurpose | undefined {
  return policyPurposes.find(p => p.name === name);
}

const WORKSHOP_README_FOR_COMPARISON = 'workshop-building-agentic-ai-platform/README.md';
const WORKSHOP_MODULE_3B_INDEX = 'workshop-building-agentic-ai-platform/content/module-3b/index.en.md';
const WORKSHOP_MODULE_3B_STEP_7 = 'workshop-building-agentic-ai-platform/content/module-3b/step-7/index.en.md';

/**
 * How this project differs from the workshop's Module 3b, which also puts an
 * AgentCore Gateway with Cedar policies in front of tools.
 */
export const module3bComparison = {
  title: "How this differs from the workshop's Module 3b",
  text: 'Both place an Amazon Bedrock AgentCore Gateway with Cedar policies and interceptors in front of tools. The workshop module is a teaching build: it creates an AgentCore Registry, registers three MCP tools, walks through the Publisher and Admin approval workflow, and attaches its Cedar policy in LOG_ONLY mode because ENFORCE would empty tools/list there. This project is a deployable governance layer: the policy engine runs in ENFORCE mode with one single-action statement per tool, and five integration tests run against the live gateway.',
  sources: [
    { file: WORKSHOP_README_FOR_COMPARISON, heading: "What you'll build", quote: 'AWS-native tool governance with Amazon Bedrock' },
    { file: WORKSHOP_MODULE_3B_INDEX, quote: 'Created an AgentCore Registry and registered 3 MCP tools with metadata' },
    { file: WORKSHOP_MODULE_3B_STEP_7, quote: 'Attach with `"mode": "LOG_ONLY"`, not `"ENFORCE"`.' },
    { file: GATEWAY_README, heading: 'Verified architecture', quote: '3. Cedar policy engine (ENFORCE)' },
    { file: FORBID_DESTRUCTIVE_DB, quote: 'One single-action forbid per tool' },
    { file: GATEWAY_README, heading: 'Quickstart', quote: '# 4. prove it: 5 governance tests against the LIVE gateway, never mocked' },
  ] satisfies Source[],
};
