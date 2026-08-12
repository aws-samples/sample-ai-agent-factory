# Atlassian connector (Jira & Confluence) — per-user 3LO

Surfaces Jira + Confluence as governed MCP tools (`Atlassian___getVisibleJiraProjects`,
`…_getJiraIssue`, `…_getConfluencePage`, …) behind the **same** Cedar + interceptor
governance as the core gateway — plus a second, **per-user** auth leg: each caller
authorizes AWS to act on **their own** Atlassian account (OAuth **3LO**).

> Run all commands from the **repo root** (the scripts and `cdk/` paths are relative to it).
> Deploy the core gateway stack first — this connector imports its ids from SSM.

## Two auth legs (why there's a consent step)

- **Inbound** (MCP client → gateway): the Cognito JWT + Cedar, exactly as the core gateway.
- **Outbound** (gateway → Atlassian, *as the user*): a per-user Atlassian OAuth token,
  obtained once via **3LO consent** and stored in AgentCore Identity's token vault
  (keyed to the caller's `sub`). Governance is unchanged — the connector only adds
  this outbound consent.

It ships **fully in CDK** as two independent stacks:

- **`AtlassianConnectorStack`** — the Jira/Confluence MCP server on an AgentCore
  Runtime, the **OAuth2 credential provider**, the gateway **target**, and the
  **12 Cedar policies** (read = all, write = role `atlassian-writer`).
- **`ConnectorAuthStack`** — a shared, provider-generic **consent SPA** (Cognito
  Hosted UI + Identity Pool + S3/CloudFront) that completes the browser-based 3LO
  for MCP clients (e.g. Kiro) that can't drive the consent themselves.

## Prerequisites

1. A **container runtime** (Docker, Finch, or Podman) **running** — this connector builds
   the MCP server's container image and bundles its custom-resource Lambda with CDK's
   Docker bundling. With Finch, prefix the CDK commands below with `CDK_DOCKER=finch`.
2. An **Atlassian OAuth 2.0 (3LO) app** (Atlassian Developer console) — note its
   **Client ID** and your site's **Cloud ID**. They are non-secret but
   deployment-specific, so you pass them at deploy time with CDK context (step 1 exports
   them as `ATLASSIAN_CLIENT_ID` / `ATLASSIAN_CLOUD_ID`); there are no real defaults in
   the repo.
3. The app's **client secret** in Secrets Manager, stored as **JSON** (the native
   integration reads a named key):
   ```bash
   aws secretsmanager create-secret --region "$AWS_REGION" \
     --name enterprise-mcp-connector/atlassian-client-secret \
     --secret-string '{"clientSecret":"<your-atlassian-oauth-client-secret>"}'
   ```

## 1. Deploy the connector

Put the two ids in your shell first — they're **not** secrets (the client *secret* stays in
Secrets Manager and is never passed on the command line), just deployment-specific:

```bash
export ATLASSIAN_CLIENT_ID='your-3lo-app-client-id'
export ATLASSIAN_CLOUD_ID='your-site-cloud-id'

cd cdk && cdk deploy AtlassianConnectorStack --require-approval never \
  -c atlassian_client_id="$ATLASSIAN_CLIENT_ID" \
  -c atlassian_cloud_id="$ATLASSIAN_CLOUD_ID" && cd ..
```

The stack publishes both ids to SSM (`/enterprise-mcp-connector/atlassian/{client-id,cloud-id}`),
so on any later deploy you can read them back instead of retyping:

```bash
export ATLASSIAN_CLIENT_ID="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-connector/atlassian/client-id --query Parameter.Value --output text)"
export ATLASSIAN_CLOUD_ID="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-connector/atlassian/cloud-id --query Parameter.Value --output text)"
```

The provider uses the native **AgentCore Identity ↔ Secrets Manager** integration
(`clientSecretConfig` + `clientSecretSource: EXTERNAL`): AgentCore reads the client
secret **directly from your Secrets Manager** at runtime — the value never enters
the CloudFormation template, and rotation is picked up automatically. `EXTERNAL`
(vs `MANAGED`, where AgentCore holds a copy in its own vault) requires two
least-privilege secret grants, both in CDK: the **gateway execution role** reads it
when fetching the outbound token, and the SPA onboarding role reads it during
browser-side completion.

Then **register the provider's callback** in your Atlassian 3LO app (Developer
console → your app → Authorization → OAuth 2.0 (3LO) → Callback URL):

```bash
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name AtlassianConnectorStack \
  --query "Stacks[0].Outputs[?OutputKey=='CallbackUrl'].OutputValue" --output text
```

## 2. Deploy the consent SPA

```bash
cd cdk && cdk deploy ConnectorAuthStack --require-approval never && cd ..
bash scripts/deploy-connector-auth.sh      # publishes the SPA + writes its config.json from stack outputs
# prints:  https://<id>.cloudfront.net/?provider=atlassian
```

`ConnectorAuthStack` also registers its Hosted-UI client in the gateway's
`allowedClients` and allow-lists the SPA callback on the gateway workload identity —
both via **config-preserving** custom resources (get → merge → update, so they never
drop existing gateway config such as the interceptors or policy engine).

## 3. Consent once per user

A CLI MCP client (e.g. Kiro) can't complete the browser 3LO itself, so each user
authorizes **once** via the SPA (their token is then vaulted and reused until
revoked; refresh is automatic):

> Open `https://<id>.cloudfront.net/?provider=atlassian` → sign in (Cognito Hosted
> UI, same pool as the gateway) → **Allow** on Atlassian → the callback completes
> and vaults the token.

> **Dev shortcut:** `connectors/atlassian/scripts/authorize.py` does the same 3LO
> dance from a localhost callback — handy for a single developer without the SPA.

## 4. Test the integration

**Integration tests** — run as a user who has **consented** (the Atlassian cases
skip automatically if the connector isn't deployed):

```bash
export COGNITO_USERNAME=you@example.com      # a user who consented via the SPA
source scripts/get-token.sh
GATEWAY_URL="$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /enterprise-mcp-gateway/gateway/url --query Parameter.Value --output text)" \
AUTH_TOKEN="$AGENTCORE_JWT" \
  python3 -m pytest tests/integration -v
```

The Atlassian cases prove: read tools are Cedar-**visible** (`getVisibleJiraProjects`),
role-gated write tools are Cedar-**hidden/denied** (`createJiraIssue`), and a read is
**permitted** (live data, or a `-32042` consent prompt if that user hasn't consented
yet). The tests are Jira-based; Confluence tools share the same 12 policies and
interceptors, so their enforcement is covered by the same cases.

**Through Kiro (the real MCP client)** — connect as a **consented** user, then drive
the tools; the read returns live Jira data, the write is denied:

```bash
export COGNITO_USERNAME=you@example.com
source scripts/get-token.sh
bash scripts/connect-coding-agent.sh kiro    # writes .kiro/settings/mcp.json
```

Reload MCP servers, then ask Kiro to call `Atlassian___getVisibleJiraProjects`
(returns your projects) and `Atlassian___createJiraIssue` (Cedar-denied — the write
tool is filtered from the list for non-`atlassian-writer` users).

> If a read returns a `-32042`/authorization error instead of data, that user hasn't
> consented (step 3), or the JWT is for a **different** user than the one who
> consented (the vault is keyed to the Cognito `sub` — connect as the same user).

## Security posture

- **The MCP server runtime has public network access, deliberately.** Its purpose is to call
  `https://api.atlassian.com`, so it needs internet egress. Atlassian is third-party SaaS —
  there is no PrivateLink target — so a VPC-private runtime would still need a NAT gateway to
  reach the same public endpoint: more cost and moving parts, no less exposure. Inbound is
  restricted: the runtime accepts only requests carrying a valid **Atlassian OIDC token**
  (JWT authorizer), limited to the declared scopes, and only the `Authorization` header is
  forwarded into the container. In this sample it is reached **only** through the AgentCore
  Gateway, which applies the Cognito JWT check, the interceptors and Cedar `ENFORCE` first.
  For an internal MCP server with no third-party egress, use a VPC configuration and front it
  with PrivateLink instead.
- **The client secret never enters CloudFormation.** AgentCore Identity reads it straight from
  Secrets Manager at runtime (`clientSecretSource: EXTERNAL`), so rotation is picked up
  automatically and the value is not in the template, state or outputs.
- **Per-user tokens are vaulted by AgentCore Identity**, keyed to the caller's Cognito `sub`.
  One user's consent never grants another user access, and the same Cedar policies apply to
  every caller regardless of whose Atlassian token is used.

## Teardown

The connector stacks import the gateway's ids from SSM, so destroy them **before**
the gateway stack, and remove the client secret:

```bash
cd cdk && cdk destroy ConnectorAuthStack AtlassianConnectorStack --force && cd ..
aws secretsmanager delete-secret --region "$AWS_REGION" \
  --secret-id enterprise-mcp-connector/atlassian-client-secret --force-delete-without-recovery
```

Both stacks are fully CDK, so this leaves no orphaned target, policies, provider, or
SPA resources.
