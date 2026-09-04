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

> **`tools/list` working does not mean `tools/call` works.** The public AWS Knowledge
> endpoint (`https://knowledge-mcp.global.api.aws/mcp`) answers `initialize` and
> `tools/list` over plain streamable HTTP and then refuses every `tools/call` with
> `HTTP 400 {"success":false,"error":"Http operation is not supported for gateway
> protocol type MCP"}` — under both protocol versions, with and without the session
> header, and with nothing else in the request path. So an agent wired to it lists its
> tools and fails to use them. Confirmed by probing the endpoint directly, which is
> what separates an upstream limitation from a platform defect; **DeepWiki**
> (`https://mcp.deepwiki.com/mcp`) serves `tools/call` unauthenticated and is the
> better choice for a first end-to-end canary. The readiness gate deliberately stops
> at discovery — calling arbitrary tools to probe them would fire real side effects.

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

## Bring your own gateway — LiteLLM MCP Gateway as a second provider

The four tiers above all describe targets *behind* an **AgentCore Gateway**, which
remains the default and is unchanged. A canvas Gateway node can instead point at a
**LiteLLM MCP Gateway** the customer already runs, which then owns target
registration and outbound auth itself — so the tier model does not apply to it.

Select it per agent with `gatewayProvider: "agentcore" | "litellm"` on the Gateway
node (`"agentcore"` is the default, so every existing canvas and stored flow keeps
working with no migration). A platform-wide default lives in a
`SETTING#gateway_provider` row; the per-agent field wins when set.

| | AgentCore Gateway (default) | LiteLLM MCP Gateway |
|---|---|---|
| Required config | `targetType` + `targetConfig` | `litellmBaseUrl` (+ optional `litellmApiKey`, `litellmServers`) |
| Who registers targets | This platform, per the four tiers above | LiteLLM (its Admin UI or `config.yaml`) |
| Inbound auth the agent uses | Cognito OAuth2 client-credentials | A static **virtual key** |
| AWS resources created | Gateway, targets, credential providers, Cognito | One Secrets Manager secret |

**Auth wiring.** LiteLLM authenticates with a static virtual key rather than the
client-credentials exchange AgentCore Gateway uses. The deployer returns
`client_info["provider"] = "litellm"`, which `runtime_configure_step` branches on
to emit `GATEWAY_AUTH_MODE=static_bearer`; the generated agent then skips the token
exchange and sends `x-litellm-api-key` instead. Pinned server aliases ride the
`x-mcp-servers` header — omit them and the agent gets LiteLLM's aggregate endpoint.

The header **value** carries a `Bearer ` prefix, and that detail is load-bearing.
Verified against a live proxy: on the REST probe paths (`/v1/mcp/server`,
`/mcp-rest/tools/list`) a bare key and `Bearer <key>` both return `200`, but on the
MCP protocol endpoint `/mcp/` — the one the agent actually speaks — a bare key
falls through to a virtual-key DB lookup and fails while `Bearer <key>` returns a
valid `initialize` result. LiteLLM strips the prefix server-side
(`proxy/auth/user_api_key_auth.py::_get_bearer_token`), so prefixing costs nothing
on the paths that already worked, and it is the form LiteLLM's own `mcp_debug.py`
prints. The probe and the agent therefore send the identical value — probing with a
form the agent cannot use would produce a green deploy and a toolless agent.

`x-litellm-api-key` is LiteLLM's documented primary MCP header
(`LITELLM_API_KEY_HEADER_NAME_PRIMARY`); `Authorization` is only its secondary
fallback, which is why the provider path uses the former and the target path below
defaults to the latter.

### Or as a target *inside* an AgentCore Gateway

The two rows above are alternatives, but they are not the only two options: a
LiteLLM proxy is also a perfectly ordinary **Tier 2** external MCP server, so it can
be wired as an `mcpServer` target on a normal AgentCore Gateway alongside Lambda and
OpenAPI targets. All three shapes are supported; nothing about this one is
LiteLLM-specific.

Use the `Custom endpoint…` MCP target with `auth_type: api_key`, endpoint
`https://<proxy>/mcp/` (or `https://<proxy>/<alias>/mcp` to pin one server, since a
Gateway target cannot send `x-mcp-servers`), and an `api_key_descriptor`.

That descriptor is what decides the outbound header, and a custom endpoint is the
only path that has to *default* it — curated catalog entries ship their own. The
default is `{location: HEADER, parameter_name: Authorization, prefix: "Bearer"}`,
because a bare `Authorization: <key>` carries no auth scheme, is invalid per
RFC 7235, and is refused by LiteLLM. Override `parameter_name` to
`x-litellm-api-key` if something in front of the proxy consumes `Authorization`, and
send an explicit `prefix: ""` — distinct from omitting `prefix` — for a server that
wants a raw value. The canvas exposes both as **API Key Header** and **Key Format**.

**`credentialPrefix` takes no trailing space.** AgentCore joins the prefix to the
key with its own single space, so `"Bearer "` is transmitted as `Bearer  <key>`
(two spaces) and refused. This is not a guess: three otherwise-identical
`mcpServer` targets were created on one live gateway against the same LiteLLM
proxy, differing only in this field —

| `credentialPrefix` | Target status |
|---|---|
| `"Bearer"` | **READY** |
| `"Bearer "` | FAILED — *"returned HTTP 400 to the initialize handshake"* |
| omitted (raw key) | FAILED — could not fetch tools |

`_mcp_api_key_cred_config` therefore right-strips the prefix, which fixes every
caller at once: the curated catalog (whose bearer entries all used to end in a
space), a custom canvas endpoint, and an OpenAPI target's user-typed prefix. A
prefix that is only whitespace collapses to empty, which is the same "send the raw
key" meaning as `""`.

Unlike the provider path, this one does not probe LiteLLM's REST endpoints for a
tool count — but it is **not** unverified. `_wait_for_mcp_target_ready` polls the
new target until it leaves `CREATING` and fails the deploy on `FAILED`, surfacing
AgentCore's `statusReasons` verbatim (they name the remote status code, which is
the whole diagnostic). Without that gate this path had the exact failure the
readiness rule exists to prevent: `create_gateway_target` returns while the
handshake is still in flight, so a wrong endpoint, key, or prefix recorded a
`succeeded` deployment and left the agent with no tools.

**Credential providers are account-global, so their names must be gateway-scoped.**
The AgentCore token vault is one flat namespace per account, but every name this
deployer derives comes from a catalog id, a connector id, or a user-typed target
label — none of them per-tenant. Two users who both wire the same MCP server
therefore landed on the same provider name (`mcp-mcp-exa`, say), the second deploy
took the "already exists" branch, and **that user's target authenticated with the
first user's API key** while their own freshly minted secret was never read. The
same branch also made rotation a silent no-op: reuse returned the existing provider
without repointing it at the new secret.

Only a live account showed this. Two deployments of one custom MCP target minted two
new secrets and both reused a provider from an *earlier* run whose `lastUpdatedTime`
had never left its `createdTime` — which is why a deliberately invalid key still
produced a `READY` target: the invalid key was never the one being sent.

`_scoped_provider_name` folds the gateway id in as a short digest (appended raw, a
gateway id would push long target names past the 64-char cap, where blind truncation
could re-collide the very names being separated), `_ensure_*_credential_provider`
repoints an existing provider whose secret differs — fatally for API keys, because
handing back a provider still bound to the previous secret is the silent failure the
branch exists to close — and teardown records the **scoped** name so a rollback
deletes the provider it actually created instead of orphaning it. This needs
`bedrock-agentcore:Update{ApiKey,Oauth2}CredentialProvider`, added to the gateway and
harness step roles.

**Composed tool names must fit Bedrock's 64-character cap.** A Gateway serves every
tool as `<targetName>___<toolName>`. When the upstream already namespaces its own
tools — LiteLLM prefixes each with its server alias — the composed name overruns the
limit Bedrock enforces on `toolConfig.tools[].toolSpec.name`, and Bedrock rejects the
**entire** `toolConfig`: every invocation fails, including ones that never touch the
offending tool. Seen live on a `READY` LiteLLM target whose six tools discovered
correctly and whose every invoke returned 500 on
`mcp-custom-litellm-proxy___aws_knowledge-aws___get_regional_availability` (72 chars).

The generated agent's `_fit_tool_names_for_bedrock` aliases over-long names to
`<leaf tool name>_<8-hex digest of the full name>`: the leaf is what tells the model
what the tool does, and the digest keeps two targets that expose the same leaf
distinct. Dropping the over-long tools instead would be the silent-toolless-agent
failure again. Emitted in **both** duplicated generator bodies;
`tests/test_gateway_tool_name_limit.py` extracts and runs the emitted text from each.

**Which attribute to rename is version-dependent, and the first fix got it wrong in
production.** `strands-agents` is deliberately unpinned, so the deployed image
installs the latest while this repo's environment resolves an older one, and the two
disagree about which name the model sees:

| | `tool_name` / `toolSpec.name` reads | `stream()` sends upstream |
|---|---|---|
| strands 1.9 | `mcp_tool.name` | `self.tool_name` |
| strands 1.54 | `_agent_tool_name` (set at construction, what `name_override` writes) | `mcp_tool.name` |

Renaming `mcp_tool.name` therefore fixed 1.9 and did nothing for 1.54 — it changed
the name sent *upstream* and left the 72-char name in the spec. The redeployed agent
logged all three renames and still returned the identical `ValidationException`. The
fitter now sets the model-facing attribute if the tool has one, falls back to
`mcp_tool.name` otherwise, **re-reads `tool_name` to confirm the rename took effect**,
and maps the outbound call back only on the generation where the rename also moved the
wire name. A name that cannot be shortened is logged at `ERROR` rather than left to
reject the whole `toolConfig` silently.

The unit tests exercise both generations plus whatever version is actually installed,
because the broken fix passed a test that asserted on `mcp_tool.name` — the attribute
Bedrock does not read. `/tmp`-style ad-hoc checks are not enough here either: the fix
was confirmed against a real `MCPAgentTool` from 1.54 in a throwaway venv *before*
redeploying, and then against the live agent.

The live proof needed a callable upstream. With AWS Knowledge pinned, both paths
reached HTTP 200 and listed their tools correctly while every call failed — and failed
identically for renamed and unrenamed tools, which cleared the alias but proved
nothing. Repointed at DeepWiki behind the same proxy, with a target name long enough
that all three tools were aliased, a deployed agent called
`deepwiki-ask_question_e49b0dd9` and `deepwiki-read_wiki_structure_abc04085` and
returned real answers, while the ordinary Lambda-backed `DynamicTools___get_weather`
on the same agent kept working untouched.

**Secret hygiene and validation.** The raw virtual key is minted into Secrets
Manager and popped from the payload before the Step Functions event is re-emitted,
the same discipline the connector and external-MCP paths already follow. The base
URL goes through the existing SSRF guard (https-only, 21-network private-IP
denylist, DNS resolved up-front), optionally narrowed by `OUTBOUND_HOST_ALLOWLIST`.

**Readiness is still proven, not assumed.** In place of
`_wait_for_gateway_to_serve_tools`, the deployer probes `GET /v1/mcp/server` then
`GET /mcp-rest/tools/list` and **fails loud on zero tools** — same rule as the
AgentCore path, for the same reason: an agent that silently has no tools looks like
a passing deploy.

### The two wire shapes those probes parse

These are **not symmetrical**, and neither is guessable from the LiteLLM docs — a
parser written for one shape silently returns zero items for the other, which is
exactly the empty-tool-plane failure the readiness gate exists to catch:

| Endpoint | Returns |
|---|---|
| `GET /v1/mcp/server` | a **bare JSON list** of server records |
| `GET /mcp-rest/tools/list` | an **object** with a `tools` key |

Also note **enablement may not be reported at all**. On the release verified below,
server records carry `status: null` and no `enabled` / `disabled` / `active` field,
so *presence in the list* is the operative approval signal. `_server_is_enabled`
therefore checks `enabled`/`is_enabled`/`active`, then `disabled`/`is_disabled`,
then `status`, and defaults to enabled — a release that *does* report a flag is
honored, and one that doesn't still works.

### Live verification

The unit suites for this path are necessarily mock-based: they assert what we
*believe* LiteLLM returns. `scripts/verify-litellm.py` asserts what it actually
returns, against a real proxy — the two shapes above, the parsers, the readiness
gate's fail-loud behavior, the registry projection, the governance gate, and the
sidecar merge:

```bash
# see the script's docstring for the no-Docker proxy setup recipe
AGENT_REGISTRY_TABLE_NAME=<deployed-registry-table> APP_AWS_REGION=us-east-1 \
  python3 scripts/verify-litellm.py http://127.0.0.1:4000 sk-verify-1234
```

Proven on 2026-09-03 against LiteLLM's own proxy fronting the real public
`https://knowledge-mcp.global.api.aws/mcp`: 5 tools discovered end-to-end, the gate
raising on both an unknown pinned server alias and a rejected virtual key (LiteLLM
answers **400**, not 401), and a real merged catalog of one mutable sidecar entry
plus one read-only LiteLLM projection. Omit `AGENT_REGISTRY_TABLE_NAME` to skip
just the merge step; only reads and refused writes are issued, so it is safe to
point at a live table.

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
