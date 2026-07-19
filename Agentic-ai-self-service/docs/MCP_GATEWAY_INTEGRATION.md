# External MCP → AgentCore Gateway: authorization architecture

[← Back to README](../README.md)

How the platform connects an **external MCP server** as a Gateway `mcpServer`
target, with the correct outbound authorization for each auth style. Grounded in
the live `bedrock-agentcore-control` API model (boto3 1.43.8):

```
CreateGatewayTarget.targetConfiguration.mcp.mcpServer = { endpoint (https://.*), listingMode, mcpToolSchema }
credentialProviderConfigurations[].credentialProviderType ∈
    { GATEWAY_IAM_ROLE, OAUTH, API_KEY, CALLER_IAM_CREDENTIALS, JWT_PASSTHROUGH }
  API_KEY provider   → { credentialParameterName, credentialPrefix, credentialLocation: HEADER|QUERY_PARAMETER }
  OAUTH  provider    → { providerArn, scopes, grantType: CLIENT_CREDENTIALS|AUTHORIZATION_CODE|TOKEN_EXCHANGE, customParameters }
  IAM    provider    → { service, region }   (SigV4 outbound)
```

## Decision: four integration tiers

An external MCP falls into exactly one tier based on **(a) is it a remote HTTPS
endpoint?** and **(b) which outbound auth does it require?**

### Tier 1 — Direct target, no credentials
Remote HTTPS MCP, no auth. Wire `mcpServer.endpoint` with **no** credential
provider (or `GATEWAY_IAM_ROLE` where the service ignores it).
- **AWS Knowledge MCP**, **DeepWiki**, **Cloudflare Docs**, **Shopify Storefront**.
- Also works for the free tiers of **Exa / Firecrawl** (rate-limited, no key).
- *Live-verified from this machine via real `initialize`+`tools/list`.*

### Tier 2 — Direct target, static credential (API key / bearer / query param)
Remote HTTPS MCP whose auth is a **static secret** the user supplies once. Create
an **API_KEY credential provider** and attach it:
- header key (Exa `x-api-key`; Firecrawl `Authorization`+prefix `Bearer `),
- query param (Tavily `tavilyApiKey`),
- bearer token (GitHub PAT, Stripe restricted key, Linear/Monday/Datadog/Intercom
  token, Sentry `Sentry-Bearer`).
The user's secret is stored in **Secrets Manager**, owner-scoped; the provider
references it. `credentialLocation`/`credentialParameterName`/`credentialPrefix`
come from the catalog entry.

### Tier 3 — Direct target, OAuth 2LO / SigV4 (machine credentials, no browser)
Remote HTTPS MCP with **client-credentials OAuth** or **IAM SigV4**:
- **OAUTH** provider, `grantType=CLIENT_CREDENTIALS` — **Databricks** (service
  principal), **Snowflake** (OAuth 2LO), **Datadog** OAuth.
- **OAUTH** `grantType=TOKEN_EXCHANGE` (RFC 8693) — Databricks per-user federation.
- **IAM** provider (`service`, `region`) — **AWS MCP (preview)** SigV4.
These need vendor creds but **no interactive browser** → fully deployable headless.

### Tier 4 — Adapter required (host it ourselves, then target the adapter)
Two sub-cases that CANNOT be a direct Gateway target:

**4a. Interactive 3LO / dynamic client registration** — Notion, Linear (default),
Atlassian, Asana, HubSpot, Salesforce, Box, Figma, GitLab, Supabase, PayPal,
Square. The Gateway's OAUTH provider does client-credentials/token-exchange, not
an end-user browser consent + DCR. **Solution:** the platform hosts a thin
**MCP-proxy adapter on AgentCore Runtime** that (i) is fronted by the platform's
own Cognito (the auth the Gateway already speaks — this is the existing
`mcp_server_runtime_arn` path), and (ii) holds the completed downstream OAuth
token (obtained once via a one-time consent captured by the platform's Identity
provider / a stored refresh token) and injects it on each outbound call. The
Gateway targets the *adapter's* Runtime endpoint; the adapter speaks 3LO to the
vendor. This reuses the platform's existing Runtime-MCP target wiring.

**4b. stdio-only servers** — ~58 AWS-labs servers, SAP dev tooling, Brave,
Perplexity, Kagi, BigQuery Toolbox, Mongo, Postgres, ClickHouse, Grafana,
PagerDuty, Airtable, Slack, Zendesk. No HTTPS endpoint at all. **Solution:**
package the stdio server into a container that runs it behind
`mcp-proxy`/streamable-HTTP on **AgentCore Runtime** (or Lambda for short calls),
then target that endpoint (which then falls into Tier 1/2/3 by its own auth).
This is the "host it on Lambda/Runtime and expose via MCP" path.

## Why this is the right decomposition
- Tiers 1–3 are **native** Gateway targets — one new deploy code path
  (external endpoint + a credential provider chosen by `auth_type`). No adapter,
  no extra compute, lowest latency. Covers ~26 of the surveyed remote MCPs.
- Tier 4 **reuses** the Runtime-as-MCP path the platform already has
  (`gateway_deployer.py` `mcp_server_runtime_arn`) — the adapter is "just another
  platform-deployed Runtime MCP," so the Gateway wiring is unchanged; only the
  adapter image differs. No new Gateway concept required.
- The catalog entry carries the tier + auth descriptor, so the deploy path is
  data-driven: pick provider from `auth_type`, done.

## This change set implements
- **Tier 1–3 direct external `mcpServer` target** deploy path + API_KEY/OAUTH/IAM
  provider creation from a catalog entry (new product code).
- The **MCP catalog** (`mcp_catalog.py`) with every verified server + its tier,
  endpoint, auth descriptor, and live-test status.
- A **live-verified end-to-end**: a real Gateway targeting **AWS Knowledge MCP**
  (Tier 1), invoked through a Runtime agent, asserting a real doc-search canary.
- Tier 4 adapter is **documented + scaffolded** (the container/Runtime recipe),
  wired opportunistically since it reuses the existing Runtime-MCP path.
