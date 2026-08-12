# Enterprise MCP Governance Gateway (AWS AgentCore)

A real, deployed governance layer that sits in front of MCP tool servers using
**Amazon Bedrock AgentCore Gateway**. It gives an enterprise a single, governed
MCP endpoint that:

- **Authenticates** every caller with a JWT (Amazon Cognito OIDC, inbound auth).
- **Authorizes** each tool call with **Cedar policies** evaluated by an AgentCore
  **policy engine** (role-, scope-, and user-based allow/deny).
- **Inspects and transforms** requests and responses with **Lambda interceptors**
  (SQL-injection blocking, business-hours gating, PII redaction, payload
  truncation, structured audit logging) — backed by a **managed Amazon Bedrock
  Guardrail** (`ApplyGuardrail`: prompt-attack/content filters on the request,
  PII anonymization on the response), with the local regex rules as defense-in-depth.
- **Fronts MCP tool servers** (here: two sample Lambda targets, `DocsAPI` and
  `DatabaseAPI`) behind one MCP URL that an MCP client (e.g. Kiro) connects to.

Provisioned with **AWS CDK (Python)** to your account in `us-west-2`
(configurable). After deploy, the live endpoint and all non-secret resource ids
are published to **AWS Systems Manager Parameter Store** under
`/enterprise-mcp-gateway/*` (and as CloudFormation stack outputs); the demo
credential lives in **AWS Secrets Manager**.

> **Migration note:** this stack was originally provisioned by a boto3 script
> (`deploy.py`). It is now an AWS CDK app under [`cdk/`](cdk/).

---

## Quickstart

Deploy, then prove the governance works. Five steps, ~5 minutes. Prerequisites:
**Node.js + `npm install -g aws-cdk@2.1129.0`**, **Python 3.12+**, and AWS credentials for
the target account.

```bash
export AWS_REGION=us-west-2
python3 -m venv .venv && source .venv/bin/activate
pip install -r cdk/requirements.txt -r requirements-dev.txt

# 1. deploy the gateway (one stack; ~2 min)
cd cdk && cdk bootstrap && cdk deploy EnterpriseMcpGatewayStack --require-approval never && cd ..

# 2. create the demo users (CloudFormation can't set a Cognito password)
bash scripts/seed-demo-users.sh

# 3. mint a JWT as admin@example.com
source scripts/get-token.sh

# 4. prove it: 5 governance tests against the LIVE gateway, never mocked
GATEWAY_URL="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)" \
AUTH_TOKEN="$AGENTCORE_JWT" python3 -m pytest tests/integration -q

# 5. drive it from a real agent
bash scripts/connect-coding-agent.sh kiro     # or: claude-register
```

Step 4 should report **5 passed** (3 Atlassian tests skip unless you also deploy the
[connector](#governed-connectors-per-user-saas)). Then open this folder in Kiro, reload MCP
servers, and work through the
[governance walkthrough](#hands-on-governance-walkthrough-the-real-test) — the part that
actually demonstrates allow, deny, block and redact.

When you're done, [disconnect the client](#disconnect-the-mcp-client-before-teardown) and
[tear down](#teardown).

Everything below is reference: how it works, what each control does, and the optional
Atlassian connector.

---

## Verified architecture

```
  MCP client (Kiro)
        │  HTTPS + Bearer JWT (Cognito access token)
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  AgentCore Gateway  (authorizerType = CUSTOM_JWT)          │
  │                                                            │
  │   1. JWT validated against Cognito OIDC discovery URL      │
  │   2. REQUEST interceptor Lambda  (audit, SQL/abuse block)  │
  │   3. Cedar policy engine (ENFORCE) — allow/deny per tool   │
  │   4. Target Lambda invoked (GATEWAY_IAM_ROLE credential)   │
  │   5. RESPONSE interceptor Lambda (PII redact, truncate)    │
  └──────────────────────────────────────────────────────────┘
        │                         │
        ▼                         ▼
   DocsAPI Lambda           DatabaseAPI Lambda
   (get_page, search,       (execute_query, drop_table,
    create/update/...)       export_pii_report, ...)
```

Everything is provisioned with **AWS CDK (Python)** using the
`AWS::BedrockAgentCore::*` L1 constructs (`CfnGateway`, `CfnGatewayTarget`,
`CfnPolicyEngine`, `CfnPolicy`) plus standard IAM / Lambda / Cognito / Secrets
Manager / SSM constructs.

Key verified mechanisms:

- **Inbound auth** — `CfnGateway(authorizerType="CUSTOM_JWT",
authorizerConfiguration={customJWTAuthorizer: {discoveryUrl, allowedClients}})`.
  The Cognito **access token** carries `sub`, `username`, and `scope`, which the
  policy engine exposes as Cedar **principal tags**.
- **Authorization (Cedar)** — a `CfnPolicyEngine` plus one `CfnPolicy` **per Cedar
  statement** (`definition.cedar.statement`), attached to the gateway via
  `policyEngineConfiguration={arn, mode: "ENFORCE"}`.
- **Interceptors** — `interceptorConfigurations` (a list of REQUEST + RESPONSE
  Lambda configs) on `CfnGateway`.
- **Targets** — `CfnGatewayTarget` Lambda targets with
  `targetConfiguration.mcp.lambda.{lambdaArn, toolSchema.inlinePayload}` and
  `credentialProviderConfigurations=[{credentialProviderType: GATEWAY_IAM_ROLE}]`.
  Tools surface as `<TargetName>___<tool>` (triple underscore), e.g.
  `DocsAPI___get_page`.
- **MCP URL** — the real `gatewayUrl` (`CfnGateway` attribute) is published to SSM
  and as a CloudFormation output. It is **not** hand-constructed.

### Cedar specifics

- Principal type is `AgentCore::OAuthUser`; JWT claims become **tags**
  (`principal.getTag("scope")`, `principal.getTag("username")`).
- Action ids are `AgentCore::Action::"<Target>___<tool>"`; resource is
  `AgentCore::Gateway::"<gatewayArn>"` (the CDK injects the real ARN token).
- The engine derives its Cedar schema from the **registered** tools, so the CDK
  declares each `CfnPolicy` with `add_dependency()` on the targets — targets are
  created (schema populated) **before** any policy that references a tool. **One
  single-action statement per tool** — `action == AgentCore::Action::"..."`.
- Numeric tool arguments map to the Cedar `decimal` extension; compare with
  `.greaterThan(decimal("1000.0"))`, not `> 1000`.

---

## Repository layout

```
cdk/                          # AWS CDK app (Python)
  app.py                      # App entry point (instantiates the stack)
  cdk.json                    # CDK CLI config
  requirements.txt            # aws-cdk-lib (pinned) + constructs
  enterprise_gateway/
    gateway_stack.py          # the whole stack (IAM, Lambdas, Cognito, gateway, policies, SSM)
  connectors/                 # governed connector stacks + custom resources (CDK)
    atlassian_stack.py        # Atlassian: Runtime + OAuth2 provider (EXTERNAL secret) + target + 12 Cedar policies
    connector_auth_stack.py   # shared per-user 3LO consent SPA (Cognito Hosted UI + Identity Pool + S3/CloudFront)
scripts/seed-demo-users.sh    # post-deploy: create the 3 demo users + set password from Secrets Manager
scripts/register-user.sh      # self-service: register YOUR OWN email as a user (real testing)
scripts/get-token.sh          # mint a Cognito JWT -> $AGENTCORE_JWT / $AUTH_TOKEN (SSM + Secrets Manager)
scripts/test-gateway.sh       # curl smoke test of the live gateway (URL from SSM)
scripts/connect-coding-agent.sh # connect Claude Code / Kiro to the gateway
scripts/deploy-connector-auth.sh # publish the consent SPA (config.json from stack outputs + S3 sync + CloudFront)
scripts/mcp_client.py         # minimal Python MCP client helper
policies/*.cedar              # Cedar source (active + disabled-for-now sets)
policies/manifest.json        # which .cedar files the stack loads, in order
schemas/*.json                # inlinePayload tool schemas for the two targets
lambdas/                      # request/response interceptors + sample targets
connectors/atlassian/         # Atlassian MCP server (Runtime source) + authorize.py (dev 3LO shortcut)
connectors/authorize-spa/     # consent SPA static files (index.html, callback.html, config.example.json)
kiro-config/mcp.json          # Kiro MCP config template (__GATEWAY_URL__ token)
tests/unit/                   # interceptor unit tests (no AWS)
tests/integration/            # live gateway governance tests
requirements-dev.txt          # test/tooling deps (pytest, requests, boto3) — see "Run tests"
LICENSE                       # MIT-0
```

There is **no `DEPLOY_STATE.json`** — non-secret resource ids/URLs live in **SSM
Parameter Store** (`/enterprise-mcp-gateway/*`) and CloudFormation outputs; the
demo credential lives in **Secrets Manager**.

---

## Deploy

Prerequisites:

- **Node.js + AWS CDK Toolkit** (the `cdk` CLI), pinned to the version this sample was
  validated with: `npm install -g aws-cdk@2.1129.0`.
- **Python 3.12+** and the CDK Python deps. A virtualenv is recommended:
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r cdk/requirements.txt
  ```
- AWS credentials for the target account with permission to create the resources.
  Region defaults to `us-west-2` (override with `AWS_REGION` / `CDK_DEFAULT_REGION`).
- A **container runtime** (Docker, Finch, or Podman) **running** — the connector and
  connector stacks bundle their Lambda dependencies with CDK's Docker bundling (it runs
  `pip install` inside the official Lambda build image), and the Atlassian connector also
  builds a container image. With Finch, prefix CDK commands with `CDK_DOCKER=finch`, e.g.
  `CDK_DOCKER=finch cdk deploy`. The core gateway stack alone does not need it.

```bash
export AWS_REGION=us-west-2
aws sts get-caller-identity                       # confirm the target account

cd cdk
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/$AWS_REGION  # first time per acct/region

# This CDK app holds three stacks (the gateway + the two optional connector stacks), so
# name the one you want — a bare `cdk deploy` will stop and ask which. `cdk ls` lists them.
cdk deploy EnterpriseMcpGatewayStack             # review the IAM change summary, then approve
```

> The connector stacks (`AtlassianConnectorStack`, `ConnectorAuthStack`) are **optional**
> and deployed later — they read this stack's ids from SSM, so the gateway must exist first.
> See [Governed connectors](#governed-connectors-per-user-saas).

The stack:

1. Creates the gateway exec role (least-privilege; per-Lambda exec roles are
   CDK-managed) and the 4 Lambdas (request/response interceptors + 2 targets).
2. Creates the Cognito user pool, resource server, and an **admin-auth** app
   client, plus a **Secrets Manager** secret holding a generated demo password.
3. Creates the Cedar **policy engine**, then the **gateway** (CUSTOM_JWT +
   interceptors + policy engine in `ENFORCE`), with a SourceArn-pinned Lambda
   invoke grant (confused-deputy protection).
4. Registers the `DocsAPI` and `DatabaseAPI` targets, then the **Cedar policies**
   (each depending on the targets so the schema is populated first).
5. Publishes non-secret discovery values to **SSM Parameter Store** and as
   CloudFormation outputs.

### Verify the deploy

One read-only check before going further (no token needed). It matters because a policy
engine that isn't in `ENFORCE` permits **every** call silently — you'd otherwise discover
that mid-demo:

```bash
cd ..
GW="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/gateway/id --query Parameter.Value --output text)"
aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW" \
  --query '{status:status,auth:authorizerType,interceptors:(interceptorConfigurations!=null),policyMode:policyEngineConfiguration.mode}'
```

Expect `READY` / `CUSTOM_JWT` / `interceptors: true` / `policyMode: ENFORCE`.

### Seed the demo users

CloudFormation cannot set a Cognito permanent password, so a post-deploy script does it:

```bash
bash scripts/seed-demo-users.sh                   # creates demo users, sets password from Secrets Manager
```

---

## Get a token

The gateway requires a Cognito JWT (inbound auth). Mint one from a demo user
(`get-token.sh` reads the pool/client from SSM and the password from Secrets
Manager, and authenticates with the IAM-gated `ADMIN_USER_PASSWORD_AUTH` flow —
no third-party SRP library, no public password flow):

```bash
source scripts/get-token.sh           # exports $AGENTCORE_JWT and $AUTH_TOKEN
```

You need this for the **curl smoke test**, the **integration tests**, and hand-written MCP
configs. You do _not_ need it before `connect-coding-agent.sh` — that script calls
`get-token.sh` itself and embeds a fresh token.

**Optional — use a different demo user.** This _changes the governance outcome_, because
the JWT's claims become Cedar principal tags, so the same tool call can be allowed for one
user and denied for another. Skip it to stay as `admin@example.com`:

```bash
export COGNITO_USERNAME=security-admin@example.com    # default is admin@example.com
source scripts/get-token.sh
```

> **zsh note:** the script is safe to `source` in both bash and zsh. Set env
> overrides as their own line (`export COGNITO_USERNAME=…`) rather than inline
> (`VAR=… source …`), which zsh doesn't apply to sourced scripts.

Tokens expire (Cognito default 1 hour); re-source when they do.

### Optional: test as yourself (register your own email)

Not required for any step below — the demo users are enough. But for realistic testing you
can register **your own email** and mint a token as yourself:

```bash
bash scripts/register-user.sh you@example.com admin    # role: admin | analyst | data-engineer
export COGNITO_USERNAME=you@example.com
source scripts/get-token.sh
```

`register-user.sh` creates the user in the pool with a permanent password (the same
generated credential from Secrets Manager) and sets a `custom:role` attribute. It is
idempotent — re-run to change your role or reset the password. If
`COGNITO_USERNAME=you@example.com` fails with `UserNotFoundException`, this script is the
step that was missed: the user has to exist in the pool first.

> **Note on `custom:role`:** setting it does _not_ make role-based Cedar policies fire in
> this sample. `custom:role` lands only in the **ID** token, which the gateway rejects — see
> the [Query F limitation](#2-ask-these-queries--and-what-governance-should-do) below.

> This registers a **local** Cognito user for testing. In production you would
> **federate the pool to your enterprise IdP** instead of registering users
> directly, and every employee would authenticate as themselves through that IdP.

---

## Hands-on governance walkthrough (the real test)

This is the test that matters: drive the governed tools through a **real coding
agent** and watch the gateway allow, deny, block, and redact — server-side, no
mocks. Everything below hits the live gateway with your JWT.

### 1. Connect a client

**Prerequisite:** you must have the **Claude Code CLI** or the **Kiro CLI** installed
and on your `PATH` — the script invokes the agent through your shell. The gateway must be
deployed and the demo users seeded; you do **not** need to mint a token first —
`connect-coding-agent.sh` calls `get-token.sh` itself and embeds a fresh JWT in the config
(so the ~1h clock starts when you connect, not earlier).

**Claude Code** — register the gateway as a project MCP server (mints a fresh JWT
and writes `.mcp.json`):

```bash
bash scripts/connect-coding-agent.sh claude-register
# then run `claude`, approve the server; tools appear as mcp__enterprise-gateway__*
```

**Kiro IDE** — write the workspace MCP config with a live URL + JWT:

```bash
bash scripts/connect-coding-agent.sh kiro      # writes .kiro/settings/mcp.json
# open this workspace in Kiro; the enterprise-gateway tools appear in the MCP panel
```

Or fully driven/headless (the script asks the agent to call each tool and prints the
verbatim gateway response): `bash scripts/connect-coding-agent.sh claude` (or
`kiro-cli`).

The script authenticates as **`admin@example.com`** (the `get-token.sh` default) unless you
`export COGNITO_USERNAME=…` first. Keep the default — the expected outcomes below are written
for that user, and a different identity legitimately produces different results.

<details>
<summary>Configuring a client by hand (reference)</summary>

The script writes this for you; this is the shape it produces, at `.kiro/settings/mcp.json`
(Kiro) or `.mcp.json` (Claude Code). Get the URL from
`aws ssm get-parameter --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value
--output text` and the token from `source scripts/get-token.sh`:

```json
{
  "mcpServers": {
    "enterprise-gateway": {
      "type": "http",
      "url": "https://<gateway-id>.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
      "headers": { "Authorization": "Bearer ${AGENTCORE_JWT}" }
    }
  }
}
```

**Kiro requires `"type": "http"`** — without it Kiro treats the entry as a stdio server and
silently ignores it. Kiro also reads two scopes: the workspace file above (only when this
folder is open) and the global `~/.kiro/settings/mcp.json` (always). Use
`connect-coding-agent.sh kiro-global` to merge into the global one.

</details>

### 2. Ask these queries — and what governance should do

Ask the agent (or the headless script asks for you). The **outcome is produced by
the gateway**, not the agent — that's the whole point.

| #   | Ask the agent to…                                                      | Expected governance outcome                                                                                                                                                                                                                                                                                                | Enforced by                          |
| --- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| A   | "List every tool from enterprise-gateway"                              | Only the **Cedar-permitted** tools appear (e.g. `DocsAPI___get_page`, `search_pages`, `list_spaces`, `execute_query`, `export_pii_report`). Forbidden tools like `drop_table` are **filtered out of the list entirely**.                                                                                                   | Cedar (visibility)                   |
| B   | "Call `DocsAPI___get_page` with pageId `arch-overview`"                | **Succeeds** — returns the page content.                                                                                                                                                                                                                                                                                   | Cedar `allow-docs-read` (ALLOW)      |
| C   | "Call `DatabaseAPI___drop_table` with tableName `users`"               | The agent **cannot even attempt it** — Cedar filtered it out at step A, so a well-behaved agent replies that no such tool exists. To see the DENY itself, call it directly (see [below](#see-the-cedar-deny-directly)): `Tool Execution Denied … [forbid_destructive_db_*]`.                                               | Cedar `forbid-destructive-db` (DENY) |
| D   | "Call `DatabaseAPI___execute_query` with query `DROP TABLE users; --`" | **Blocked before execution:** `Request blocked: dangerous SQL pattern detected …`                                                                                                                                                                                                                                          | REQUEST interceptor Lambda           |
| E   | "Call `DatabaseAPI___export_pii_report` for department `engineering`"  | PII comes back **masked:** `Name: {NAME}, SSN: {US_SOCIAL_SECURITY_NUMBER}, Credit Card: {CREDIT_DEBIT_CARD_NUMBER}, …` — the `{TYPE}` form means the **managed Bedrock Guardrail** anonymized it. (If the guardrail is disabled the local regex backstop produces `[REDACTED_SSN]` instead.) In business hours; see note. | RESPONSE interceptor + Guardrail     |
| F   | "Call `DocsAPI___create_page` …" (a write)                             | **Denied** for demo users — `No policy applies to the request (denied by default)`. `create_page` is `role=admin`-gated and `custom:role` never reaches the access token the gateway validates (documented limitation).                                                                                                    | Cedar (default deny)                 |

> **Query F is a known demo limitation, not a bug.** Cognito issues two tokens: an
> **access** token (carries `scope` — this is the one the gateway validates) and an **ID**
> token (carries `email` and `custom:role`, and the gateway rejects it with
> `-32002 insufficient_scope` because it has no `scope` claim). `custom:role` therefore
> never reaches the policy engine, so the role-gated `permit` never fires. A production
> setup adds a Cognito **pre-token-generation Lambda** to copy `custom:role` into the
> access token (see
> [Tracked production hardening](#tracked-production-hardening-not-in-this-sample)).
> To see the split for yourself, mint an ID token with
> `TOKEN_TYPE=id source scripts/get-token.sh` and decode it — `custom:role` is present
> there but absent from the access token. (`unset TOKEN_TYPE` afterwards; an ID token
> cannot be used against the gateway.)

> The agent itself never decides any of this. It simply calls the tool; the
> **gateway** runs the JWT check → REQUEST interceptor → Cedar engine → target →
> RESPONSE interceptor, and returns the allow/deny/redacted result the agent reports
> back to you verbatim.

#### See the Cedar deny directly

Cedar both hides a forbidden tool and denies it, and hiding wins — so an agent never
triggers the deny (case C). To see the engine refuse, bypass the agent:

```bash
source scripts/get-token.sh
GW="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)"
curl -s -X POST "$GW" -H "Authorization: Bearer $AGENTCORE_JWT" \
  -H "Content-Type: application/json" -H "Mcp-Protocol-Version: 2025-11-25" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"DatabaseAPI___drop_table","arguments":{"tableName":"users"}}}'
# -32002 Tool Execution Denied ... [denied due to forbid_destructive_db_1-...]
```

The named policy proves the refusal was server-side, not the agent's judgement; a tool that
truly doesn't exist returns `-32602 Unknown tool`.

> **Business-hours note (case E):** `export_pii_report` and `query_audit_logs` are
> gated to **09:00–17:00 UTC** by the REQUEST interceptor. Outside that window they
> are blocked _before_ execution. To see PII **redaction** specifically outside
> hours, ask the agent to call `DatabaseAPI___execute_query` with
> `SELECT * FROM users` — its sample rows contain an email/phone the RESPONSE
> interceptor redacts.

### 3. See the audit trail

Every call is logged by the interceptors. Tail the REQUEST interceptor log:

```bash
REQ_ARN="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/lambda/request-interceptor-arn --query Parameter.Value --output text)"
aws logs tail "/aws/lambda/${REQ_ARN##*:}" --region "$AWS_REGION" --since 15m --format short
```

You'll see one structured `request_audit` record per call (user, tool, args keys)
and `request_blocked` records for the SQLi / business-hours blocks. Two things worth knowing:

- **Refused calls are audited too** — the interceptor runs before Cedar, so a denied
  `drop_table` still leaves a record of the attempt.
- **`user` is the Cognito `sub`**, not the email (the access token carries `sub`), and
  argument **values** are never logged — only names, plus the matched rule and value length
  on a block.

## Run tests

Install the test dependencies (pytest, requests, …) — separate from the deploy deps in
`cdk/requirements.txt`:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
```

**Integration tests — these are the ones that validate your deployment.** They run against
the live gateway and are never mocked, so they prove the deployed system really enforces
governance:

```bash
source scripts/get-token.sh
GATEWAY_URL="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)" \
AUTH_TOKEN="$AGENTCORE_JWT" \
  python3 -m pytest tests/integration -v
```

If `GATEWAY_URL` is unset the integration module is skipped automatically.

**Curl smoke test** — the same idea in one command (resolves the URL from SSM):

```bash
source scripts/get-token.sh
bash scripts/test-gateway.sh
```

**Unit tests** exercise the interceptor and Cedar-parsing logic against fakes — no AWS, no
token. They pass whether or not anything is deployed, so run them when you **change the
code** (or in CI), not to check a deploy:

```bash
python3 -m pytest tests/unit -v
```

### What the tests prove

C1–C5 run against the **deployed, live gateway — never mocked**: `tools/list` is
Cedar-filtered (C1), an allowed call succeeds (C2), a forbidden tool is denied (C3), a
SQL-injection payload is blocked by the REQUEST interceptor (C4), and PII in a response is
redacted (C5). C6 is the local interceptor unit tests — the only no-AWS row.

---

### Inspecting the live policy

Policies are live `CfnPolicy` resources in the deployed engine — you don't edit `.cedar`
files to change what's enforced. To see exactly what the engine holds right now:

```bash
PE="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/policy-engine/id --query Parameter.Value --output text)"
aws bedrock-agentcore-control list-policies --region "$AWS_REGION" \
  --policy-engine-id "$PE" --max-results 50 \
  --query 'policies[].{name:name,status:status}' --output table
```

Remember that **the identity you test as changes the result** — the JWT's claims become Cedar
principal tags, so a token for a different user can flip an ALLOW to a DENY.

---

## Governed connectors (per-user SaaS)

Beyond the sample Lambda targets, the gateway can front **per-user SaaS connectors** —
each caller authorizes AWS to act on _their own_ SaaS account (OAuth), while the same
Cedar + interceptor governance applies. A shared, provider-generic consent SPA
(`ConnectorAuthStack`, Cognito Hosted UI + Identity Pool + S3/CloudFront) completes the
browser-based consent for MCP clients (e.g. Kiro) that can't drive it themselves.

**Available connectors** — see each connector's README for prerequisites, deploy,
consent, and test steps:

- **[Atlassian (Jira & Confluence)](connectors/atlassian/README.md)** — per-user OAuth
  3LO; read = all, write = role-gated (`atlassian-writer`).

---

## Discovery & state (SSM + Secrets Manager)

After deploy, fetch every non-secret value in one call:

```bash
aws ssm get-parameters-by-path --region "$AWS_REGION" \
  --path /enterprise-mcp-gateway --recursive \
  --query "Parameters[].[Name,Value]" --output table
```

Published parameters include `gateway/{url,id,arn}`, `policy-engine/{id,arn}`,
`cognito/{pool-id,client-id,discovery-url,demo-secret-arn}`, `targets/*-id`,
`lambda/*-arn`, and `roles/gateway-exec-arn`. The demo **password** is never in
SSM or any file — it lives in the Secrets Manager secret
`enterprise-mcp-gateway/demo-user`.

---

## Security notes

This is a **sample / demonstration** stack. It is deployed to a real account and
is safe to demo, but it is **not hardened for production** — see
[Tracked production hardening](#tracked-production-hardening-not-in-this-sample)
for what to change first. Notes:

### IAM — least privilege

- **Per-Lambda execution roles** are CDK-managed and grant only CloudWatch Logs
  (the sample targets touch no other AWS service).
- **Gateway invoke** is granted both as an identity policy on the gateway role and
  as a resource-based `lambda:InvokeFunction` permission pinned with **`SourceArn`**
  = this gateway's ARN (confused-deputy protection).
- **Gateway exec role — Cedar enforcement** grants only the three documented actions
  (`GetPolicyEngine`, `AuthorizeAction`, `PartiallyAuthorizeActions`), not a
  `bedrock-agentcore:*` wildcard. The **policy engine** is scoped to its **exact ARN**;
  the **gateway** resource stays a `…:gateway/*` account+region pattern because the
  gateway must wait for this role's policy at creation, so referencing its own ARN
  would be a circular dependency (see
  [Tracked production hardening](#tracked-production-hardening-not-in-this-sample) for
  how to tighten this in multi-gateway accounts). Note: omitting the two _authorize_
  actions breaks ENFORCE at runtime (_"Insufficient Permissions for Policy
  Evaluation"_ — verified live).
- **Connector grants** (per-user 3LO token vault + AgentCore-managed OAuth secrets) are
  read-only and scoped by the `mcp-connector-*` naming convention, so connectors can be
  added without widening the core stack. AgentCore names the managed secrets itself, so
  a prefix is required there; scope to exact ARNs for a fixed connector set.

### Identity & secrets

- The app client enables **only `ADMIN_USER_PASSWORD_AUTH`** (IAM-gated; not the
  public `USER_PASSWORD_AUTH` flow), so token minting needs no third-party SRP
  library. **Production: federate the gateway to your own OIDC IdP** (the gateway
  is IdP-agnostic — point its discovery URL at your IdP) and delete the demo users.
- **Encryption at rest uses a customer-managed KMS key** (one CMK, annual rotation
  enabled) for the gateway (`kmsKeyArn`), the Cedar policy engine and all its policies
  (`encryptionKeyArn`), and the demo credential secret — rather than the service-managed
  default. The key policy follows the AgentCore requirements exactly: the **gateway
  service role** gets `DescribeKey`/`Decrypt`/`GenerateDataKey`/`CreateGrant` gated by
  `kms:ViaService` and a gateway-ARN encryption-context condition, while the **policy
  engine** grants go to the *account* principal because it calls `CreateGrant` through a
  Forward Access Session. Those actions are also on the gateway role's identity policy —
  the key policy alone is not sufficient, and omitting them fails the deploy with
  *"no identity-based policy allows the kms:GenerateDataKey action"*.
- The demo password is **generated into AWS Secrets Manager** (CMK-encrypted,
  rotatable, audited) — never written to a file. `seed-demo-users.sh` reads it to
  set the users' permanent password.
- Non-secret resource ids/URLs are published to **SSM Parameter Store** and
  CloudFormation outputs — there is no committed or generated state file with
  secrets.
- Interceptors decode the JWT **without signature verification** for audit
  attribution only (the gateway already validated the signature upstream).

### Managed guardrail (Amazon Bedrock Guardrails)

The interceptors enforce a **managed** Amazon Bedrock Guardrail in addition to the
local regex rules, via `bedrock:ApplyGuardrail`:

- **Request path (INPUT):** prompt-attack + misconduct content filters and managed
  word/topic filters; a hard `BLOCKED` short-circuits the call.
- **Response path (OUTPUT):** PII entities (email, phone, SSN, card, name, address)
  set to **ANONYMIZE**, so PII is masked before it reaches the caller. The
  `[REDACTED_*]` regex remains as a defense-in-depth backstop.
- **Opt-in / fail-aware:** the Lambdas read `GUARDRAIL_ID`/`GUARDRAIL_VERSION`; unset,
  they fall back to the local rules. A guardrail API error on the request path is
  logged and the local controls still apply (tune to fail-closed for stricter envs).
- **IAM:** `bedrock:ApplyGuardrail` is scoped to the single guardrail ARN; the
  interceptor roles otherwise hold only CloudWatch Logs.

### Synthetic data

- All sample PII is **synthetic**: `example.com` emails, the reserved `555-01xx`
  phone range, the documented invalid sample SSN `123-45-6789`, and the standard
  Visa test card `4111-1111-1111-1111`. `DatabaseAPI___export_pii_report`
  intentionally returns these so the RESPONSE interceptor's redaction is provable.

### Compliance considerations

This is a **demonstration stack**. If you adapt it for workloads handling
regulated data, you are responsible for implementing controls appropriate to your
compliance requirements under the
[AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/)
(e.g. [HIPAA](https://aws.amazon.com/compliance/hipaa-compliance/),
[PCI-DSS](https://aws.amazon.com/compliance/pci-dss-level-1-faqs/),
[GDPR](https://aws.amazon.com/compliance/gdpr-center/)).

### Tracked production hardening (not in this sample)

- **Tighten the gateway-resource IAM scope for multi-gateway accounts.** The gateway
  exec role's Cedar-enforcement grant is scoped to `…:gateway/*` (account + region),
  because the gateway must wait for this role's policy at creation time — referencing
  the gateway's own ARN would be a circular dependency. This sample deploys a single
  gateway, so the wildcard effectively resolves to it. If your account runs **multiple
  gateways** in the region, restrict the statement to the specific gateway ARN after
  the first deploy, or use a two-phase deploy (create the gateway, then update the
  policy with its exact ARN). The **policy engine** half is already scoped to its exact
  ARN (`PolicyEngineScoped`).
- **Env-based config profiles** — `ENFORCE` + no `exceptionLevel` for prod
  (`DEBUG` returns verbose denial reasons, useful only for a demo).
- A Cognito **pre-token-generation Lambda** to surface `custom:role` in the
  **access** token, so the role-based Cedar policies fire (today `role`/`email`
  live only in the ID token, which the gateway does not validate).
- Per-Lambda **log retention**.

---

## Disconnect the MCP client (before teardown)

After testing — and **always before you `cdk destroy`** (a destroyed gateway leaves
a dead MCP entry that errors on every reload) — remove what the connect step added.
The artifacts also hold a live JWT, so removing them is good hygiene.

**Claude Code** (you registered it with `connect-coding-agent.sh claude-register`,
which runs `claude mcp add --scope project`):

```bash
claude mcp remove enterprise-gateway        # removes it from .mcp.json (project scope)
claude mcp list                             # confirm it's gone
```

If you added it at user scope instead, use `claude mcp remove -s user
enterprise-gateway`. The `connect-coding-agent.sh claude` (headless) mode uses a
throwaway `/tmp/agc_mcp.json` and registers nothing persistent — just delete that
temp file: `rm -f /tmp/agc_mcp.json`.

**Kiro IDE** (you ran `connect-coding-agent.sh kiro`, which wrote
`.kiro/settings/mcp.json`):

```bash
rm -f .kiro/settings/mcp.json               # remove the workspace MCP config
# then reload MCP servers in Kiro (or reopen the workspace)
```

To keep Kiro configured but stop it reaching a torn-down gateway, instead edit
`.kiro/settings/mcp.json` and delete the `enterprise-gateway` entry under
`mcpServers`.

> **If you destroyed the gateway before removing the MCP entry**, the dead URL stays
> in your client config and Kiro/Claude Code will throw a connection error (e.g.
> "connection refused") **every time you reload MCP servers**. Remove the entry
> immediately to stop the errors — there is nothing left to connect to.

**Kiro CLI / Amazon Q** (`connect-coding-agent.sh kiro-cli` wrote an agent file):

```bash
rm -f .kiro/agents/governance-test.json     # the isolated test agent (contains a live JWT)
```

> All of these files are **git-ignored** (`.mcp.json`, `.kiro/settings/mcp.json`,
> `.kiro/agents/governance-test.json`) and hold a short-lived JWT, so deleting them
> is safe and leaves no trace in the repo. Re-run `connect-coding-agent.sh` any time
> to recreate them with a fresh token.

---

## Teardown

First [disconnect the MCP client](#disconnect-the-mcp-client-before-teardown) (above),
then destroy the stacks. **If you deployed the [Atlassian connector](connectors/atlassian/README.md), destroy its two stacks first** — they import the gateway's ids from SSM:

```bash
cd cdk
cdk destroy ConnectorAuthStack AtlassianConnectorStack --force   # only if you deployed the connector
cdk destroy EnterpriseMcpGatewayStack
cd ..
```

Removes the entire stack (gateway, targets, policies, policy engine, Cognito pool

- secret, the 4 Lambdas, IAM roles, and the SSM parameters). The demo users seeded
  by `seed-demo-users.sh` live in the pool and are deleted when the pool is removed.

> **CloudWatch log groups are NOT removed by `cdk destroy`.** Lambda auto-creates
> `/aws/lambda/<function>` log groups that CloudFormation doesn't own, so they
> survive teardown — for **every** stack you deployed, and they accumulate across
> repeated deploys. Delete them if you want a fully clean account:
>
> ```bash
> for prefix in EnterpriseMcpGatewayStack AtlassianConnectorStack \
>               ConnectorAuthStack; do
>   for lg in $(aws logs describe-log-groups --region "$AWS_REGION" \
>     --query "logGroups[?contains(logGroupName,'$prefix')].logGroupName" --output text); do
>     aws logs delete-log-group --region "$AWS_REGION" --log-group-name "$lg"; done
> done
> ```

## Related projects

- **[Loom for AWS](https://github.com/awslabs/loom)** — an enterprise platform for building,
  deploying and operating agents on AgentCore Runtime and Strands: a management UI for agent
  lifecycle, memory, MCP server and A2A registration, AWS **Agent Registry** governance
  (approval workflow, `APPROVED`-only visibility), OBO token exchange, HITL approvals, cost
  tracking. Loom manages **which** agents and MCP servers exist and who may be wired to them.

  This sample is complementary and sits a layer lower: it governs **individual tool calls in
  the request path** through an AgentCore **Gateway** — Cedar policy evaluation per call,
  request/response interceptor Lambdas, and a managed Bedrock Guardrail. If you want a
  platform, start with Loom; if you want to see enforcement at the MCP front door, start here.
