# Verified MCP catalog for AgentCore Gateway targets

[← Back to README](../README.md)

Researched + verified inventory of external **Model Context Protocol** servers
that can be connected as an AgentCore Gateway `mcp.mcpServer` target. Companion to
[`MCP_GATEWAY_INTEGRATION.md`](MCP_GATEWAY_INTEGRATION.md) (the authorization
architecture) and the code catalog `backend/src/app/services/mcp_catalog.py`.

> **Not in the catalog?** The Gateway node's **MCP Server** dropdown also has a
> **"Custom endpoint…"** option: paste any remote HTTPS MCP endpoint and pick its
> outbound auth (none / API key / OAuth2 client-credentials / IAM SigV4). The
> endpoint is SSRF-validated at deploy (https-only, DNS-resolved, private/link-
> local/metadata ranges blocked) and wired exactly like a catalog entry. Only the
> direct tiers apply — a stdio-only server still needs a hosted proxy first.

## The one filter that matters: remote vs stdio

A Gateway target must be a **remotely-hosted HTTPS MCP endpoint**. Most MCP
servers on the market ship as **local stdio processes** (`npx`/`uvx`) and are
*not* targets as-is — they must first be hosted (containerized behind
`mcp-proxy` on AgentCore Runtime/Lambda). Of ~45 servers surveyed, ~26 are
remotely hosted; the rest are stdio/self-host (Tier 4b).

## Integration tiers

| Tier | Meaning | Gateway wiring |
|------|---------|----------------|
| **1 direct-none** | remote HTTPS, no auth | `mcpServer.endpoint`, no credential provider |
| **2 direct-apikey** | remote HTTPS, static key/bearer/query | API_KEY provider (HEADER/QUERY_PARAMETER + prefix) |
| **3 direct-oauth** | remote HTTPS, machine OAuth / SigV4 | OAUTH (CLIENT_CREDENTIALS) or GATEWAY_IAM_ROLE |
| **4a adapter-3lo** | interactive OAuth / dynamic client reg | host a Runtime adapter holding the downstream token |
| **4b adapter-stdio** | no HTTPS endpoint at all | containerize behind mcp-proxy, then target |

## Verified status legend
- **live** — real MCP `initialize`+`tools/list` (and for AWS Knowledge, a full
  Gateway `tools/call`) confirmed from this repo with **no vendor credentials**.
- **docs** — endpoint + auth confirmed from the vendor's official documentation.

---

## Tier 1 — direct, no credentials (live-verified handshakes)

| MCP | Endpoint | Status | Example tools |
|-----|----------|--------|---------------|
| **AWS Knowledge MCP** | `https://knowledge-mcp.global.api.aws` | **live (full E2E through a real Gateway)** | search_documentation, read_documentation, list_regions |
| DeepWiki | `https://mcp.deepwiki.com/mcp` | live handshake | read_wiki_structure, read_wiki_contents, ask_question |
| Cloudflare Docs | `https://docs.mcp.cloudflare.com/mcp` | live handshake | search_cloudflare_documentation |
| Shopify Storefront | `https://{store}.myshopify.com/api/mcp` | docs (per-store, no auth) | search_catalog, get_cart, update_cart |

## Tier 2 — direct, static API key / bearer / query param

| MCP | Endpoint | Auth (location) | Status |
|-----|----------|-----------------|--------|
| Exa Search | `https://mcp.exa.ai/mcp` | `x-api-key` header (free tier keyless) | live handshake |
| Firecrawl | `https://mcp.firecrawl.dev/v2/mcp` | `Authorization: Bearer` (free tier keyless) | live handshake |
| Tavily | `https://mcp.tavily.com/mcp/` | `?tavilyApiKey=` query param | docs |
| GitHub | `https://api.githubcopilot.com/mcp/` | `Authorization: Bearer <PAT>` | docs |
| Stripe | `https://mcp.stripe.com` | `Authorization: Bearer rk_...` | docs |
| Sentry | `https://mcp.sentry.dev/mcp` | `Authorization: Sentry-Bearer` | docs |
| Linear | `https://mcp.linear.app/mcp` | `Authorization: Bearer` | docs |
| monday.com | `https://mcp.monday.com/mcp` | `Authorization: Bearer` | docs |
| Datadog | `https://mcp.datadoghq.com/api/unstable/mcp-server/mcp` | Bearer or DD-API-KEY headers | docs |
| Intercom | `https://mcp.intercom.com/mcp` | `Authorization: Bearer` | docs |
| PayPal | `https://mcp.paypal.com/http` | `Authorization: Bearer` | docs |
| Elasticsearch (Agent Builder) | `https://{project}/api/agent_builder/mcp` | `Authorization: ApiKey` | docs |

## Tier 3 — direct, machine OAuth (client-credentials) / SigV4

| MCP | Endpoint | Auth | Status |
|-----|----------|------|--------|
| Databricks managed | `https://{workspace}/api/2.0/mcp/{genie\|functions\|vector-search\|sql}` | OAuth2 client-credentials (service principal) | docs |
| Snowflake Cortex | `https://{account}/api/v2/.../mcp-servers/{name}` | OAuth2 / PAT bearer | docs |
| AWS MCP (preview) | `https://aws-mcp.us-east-1.api.aws/mcp` | IAM SigV4 | docs |

## Tier 4a — 3LO / dynamic client registration → adapter required

Remote + hosted, but auth is **interactive browser consent / DCR**, which the
Gateway's OAUTH provider (client-credentials / token-exchange) can't perform.
Wire via a platform-hosted Runtime adapter that completes the 3LO once and
injects the token: **Notion, Atlassian (Jira/Confluence), Salesforce, HubSpot,
Asana, Box, Figma, GitLab, Supabase, Square, Vercel**.

## Tier 4b — stdio-only → containerize behind mcp-proxy

No HTTPS endpoint. Host behind `mcp-proxy`/streamable-HTTP on Runtime/Lambda,
then target the hosted URL (it then becomes Tier 1/2/3 by its own auth):
**~58 AWS-labs servers** (bedrock-agentcore, cloudwatch, cost, dynamodb, eks,
ecs, iam, redshift…), **SAP** dev tooling, **BigQuery Toolbox, MongoDB, Postgres,
ClickHouse, Brave, Perplexity, Kagi, PagerDuty, Grafana, Slack, Zendesk,
Airtable, Zoom, Canva**.

---

## How to add one to a Gateway (Tier 1–3)

```python
from app.services.gateway_deployer import deploy_external_mcp_target
from app.services.mcp_catalog import get_mcp_server

entry = get_mcp_server("aws-knowledge")           # Tier 1, no creds
deploy_external_mcp_target(agentcore_ctrl, gateway_id=gid, catalog_entry=entry)

entry = get_mcp_server("exa")                      # Tier 2, static key
deploy_external_mcp_target(agentcore_ctrl, gateway_id=gid, catalog_entry=entry,
                           secret_arn="<secrets-manager-arn-holding-the-key>")

entry = get_mcp_server("databricks")               # Tier 3, machine OAuth
deploy_external_mcp_target(agentcore_ctrl, gateway_id=gid, catalog_entry=entry,
                           endpoint="https://myws.cloud.databricks.com/api/2.0/mcp/sql",
                           oauth_provider_arn="<oauth-provider-arn>", oauth_scopes=["sql"])
```

## Live verification

Re-runnable end-to-end proof (creates a real Gateway + target, invokes, tears down):

```bash
AWS_REGION=us-west-2 python3 scripts/verify-external-mcp.py aws-knowledge
```

Proven on `166827918465`/us-west-2 on 2026-07-16: the Gateway exposed
`mcp-aws-knowledge___aws___search_documentation` and a `tools/call` returned real
AWS documentation for "Amazon Bedrock AgentCore Gateway". All resources torn down.
