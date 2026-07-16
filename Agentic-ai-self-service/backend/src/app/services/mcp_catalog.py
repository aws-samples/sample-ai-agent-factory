"""Verified external MCP-server catalog for AgentCore Gateway `mcpServer` targets.

This is the **read-only** catalog of external Model Context Protocol servers that
can be wired as a Gateway ``mcp.mcpServer`` target. It is the counterpart to
``connectors_catalog`` (which describes REST/OpenAPI connectors) — this one
describes **remote MCP endpoints**.

Grounded in the live ``bedrock-agentcore-control`` API model: an ``mcpServer``
target requires only ``endpoint`` (an ``https://`` URL); a credential provider is
OPTIONAL. Supported outbound auth: ``GATEWAY_IAM_ROLE``, ``OAUTH``, ``API_KEY``,
``CALLER_IAM_CREDENTIALS``, ``JWT_PASSTHROUGH``. See
``docs/MCP_GATEWAY_INTEGRATION.md`` for the four integration tiers.

Every entry is classified into a **tier** that determines how the deploy path
wires it:

  * ``tier``:
    - ``direct-none``   — remote HTTPS, no auth (no credential provider)
    - ``direct-apikey`` — remote HTTPS, static key/bearer/query-param (API_KEY provider)
    - ``direct-oauth``  — remote HTTPS, client-credentials OAuth / SigV4 (OAUTH/IAM provider)
    - ``adapter-3lo``   — needs interactive 3LO/DCR; host an adapter on Runtime (Tier 4a)
    - ``adapter-stdio`` — stdio-only; containerize behind streamable-HTTP (Tier 4b)

Only ``direct-*`` tiers are wireable as a Gateway target *as-is*; ``adapter-*``
require the platform to host an MCP proxy first (documented, not auto-built).

``verified``:
    - ``live``   — MCP ``initialize``+``tools/list`` handshake confirmed from the
                   platform with NO vendor credentials (see the live-test harness).
    - ``docs``   — endpoint/auth confirmed from the vendor's official documentation.
    - ``community`` — only a community/self-host server exists.

The module is import-safe: no boto3 / no AWS clients at import. The catalog holds
NO secrets — only public endpoints and the *shape* of the credential a user must
supply (mirrors ``connectors_catalog``'s tenant model).

Bug-11 note: when a target's tools are exposed they become ``<target>___<tool>``;
AWS Knowledge MCP already returns ``aws___search_documentation`` etc., so target
names are kept short to stay under the 64-char AgentCore qualified-name limit.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

# ---------------------------------------------------------------------------
# API-key credential descriptor helpers (mirror the API_KEY provider fields:
# credentialLocation HEADER|QUERY_PARAMETER, credentialParameterName, credentialPrefix)
# ---------------------------------------------------------------------------


def _header_key(param_name: str, prefix: str = "") -> dict:
    return {"location": "HEADER", "parameter_name": param_name, "prefix": prefix}


def _query_key(param_name: str) -> dict:
    return {"location": "QUERY_PARAMETER", "parameter_name": param_name, "prefix": ""}


def _bearer() -> dict:
    return _header_key("Authorization", "Bearer ")


# ---------------------------------------------------------------------------
# Catalog. Each entry:
#   id            slug (^[a-z0-9][a-z0-9_-]*$)
#   display_name  human label
#   publisher     who ships it
#   category      grouping for the picker UI
#   endpoint      remote HTTPS MCP URL (may contain {placeholders} the UI fills)
#   tier          integration tier (see module docstring)
#   verified      live | docs | community
#   auth_type     none | api_key | oauth2_client_credentials | oauth2_3lo | iam_sigv4
#   api_key_descriptor  (api_key tier only) how the key is sent (location/name/prefix)
#   oauth_descriptor    (oauth tiers) grant type + scope hints
#   credentials_needed  human string: exactly what a user must supply
#   example_tools list of representative tool names
#   live_testable bool — end-to-end testable with NO vendor creds
# ---------------------------------------------------------------------------

MCP_SERVERS: dict[str, dict] = {
    # ---- Tier 1: direct, no credentials (live-verified) --------------------
    "aws-knowledge": {
        "id": "aws-knowledge",
        "display_name": "AWS Knowledge MCP",
        "publisher": "AWS",
        "category": "Knowledge & Docs",
        "endpoint": "https://knowledge-mcp.global.api.aws",
        "tier": "direct-none",
        "verified": "live",
        "auth_type": "none",
        "credentials_needed": "None — public, unauthenticated (rate-limited).",
        "example_tools": [
            "search_documentation",
            "read_documentation",
            "list_regions",
            "get_regional_availability",
            "retrieve_skill",
        ],
        "live_testable": True,
    },
    "deepwiki": {
        "id": "deepwiki",
        "display_name": "DeepWiki MCP",
        "publisher": "Cognition (Devin)",
        "category": "Knowledge & Docs",
        "endpoint": "https://mcp.deepwiki.com/mcp",
        "tier": "direct-none",
        "verified": "live",
        "auth_type": "none",
        "credentials_needed": "None — free, public, no auth (public GitHub repos).",
        "example_tools": ["read_wiki_structure", "read_wiki_contents", "ask_question"],
        "live_testable": True,
    },
    "cloudflare-docs": {
        "id": "cloudflare-docs",
        "display_name": "Cloudflare Docs MCP",
        "publisher": "Cloudflare",
        "category": "Knowledge & Docs",
        "endpoint": "https://docs.mcp.cloudflare.com/mcp",
        "tier": "direct-none",
        "verified": "live",
        "auth_type": "none",
        "credentials_needed": "None — public documentation server.",
        "example_tools": ["search_cloudflare_documentation", "migrate_pages_to_workers_guide"],
        "live_testable": True,
    },
    "shopify-storefront": {
        "id": "shopify-storefront",
        "display_name": "Shopify Storefront MCP",
        "publisher": "Shopify",
        "category": "Commerce",
        "endpoint": "https://{store_domain}/api/mcp",
        "tier": "direct-none",
        "verified": "docs",
        "auth_type": "none",
        "credentials_needed": "A public Shopify store domain (e.g. shop.myshopify.com). No auth.",
        "example_tools": ["search_shop_policies_and_faqs", "get_cart", "update_cart", "search_catalog"],
        "live_testable": True,
    },
    # ---- Tier 2: direct, static API key / bearer / query param -------------
    "exa": {
        "id": "exa",
        "display_name": "Exa Search MCP",
        "publisher": "Exa Labs",
        "category": "Search & Web",
        "endpoint": "https://mcp.exa.ai/mcp",
        "tier": "direct-apikey",
        "verified": "live",  # default search tools work key-free (rate-limited)
        "auth_type": "api_key",
        "api_key_descriptor": _header_key("x-api-key"),
        "credentials_needed": "Optional Exa API key (x-api-key header) for higher limits + agent tools.",
        "example_tools": ["web_search_exa", "web_fetch_exa", "web_search_advanced_exa"],
        "live_testable": True,
    },
    "firecrawl": {
        "id": "firecrawl",
        "display_name": "Firecrawl MCP",
        "publisher": "Firecrawl (Mendable)",
        "category": "Search & Web",
        "endpoint": "https://mcp.firecrawl.dev/v2/mcp",
        "tier": "direct-apikey",
        "verified": "live",  # keyless free tier (limited tools)
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "Optional Firecrawl API key (Authorization: Bearer fc-...) for all tools.",
        "example_tools": ["firecrawl_scrape", "firecrawl_search", "firecrawl_map", "firecrawl_extract"],
        "live_testable": True,
    },
    "tavily": {
        "id": "tavily",
        "display_name": "Tavily Search MCP",
        "publisher": "Tavily",
        "category": "Search & Web",
        "endpoint": "https://mcp.tavily.com/mcp/",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _query_key("tavilyApiKey"),
        "credentials_needed": "Tavily API key (sent as ?tavilyApiKey= query parameter).",
        "example_tools": ["tavily-search", "tavily-extract"],
        "live_testable": False,
    },
    "github": {
        "id": "github",
        "display_name": "GitHub MCP",
        "publisher": "GitHub",
        "category": "Developer Tools",
        "endpoint": "https://api.githubcopilot.com/mcp/",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "GitHub Personal Access Token (Authorization: Bearer <PAT>).",
        "example_tools": ["get_file_contents", "create_pull_request", "search_code", "list_dependabot_alerts"],
        "live_testable": False,
    },
    "stripe": {
        "id": "stripe",
        "display_name": "Stripe MCP",
        "publisher": "Stripe",
        "category": "Payments",
        "endpoint": "https://mcp.stripe.com",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "Stripe restricted API key (Authorization: Bearer rk_...). OAuth also supported.",
        "example_tools": ["stripe_api_read", "stripe_api_write", "create_refund", "search_stripe_documentation"],
        "live_testable": False,
    },
    "sentry": {
        "id": "sentry",
        "display_name": "Sentry MCP",
        "publisher": "Sentry",
        "category": "Observability",
        "endpoint": "https://mcp.sentry.dev/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _header_key("Authorization", "Sentry-Bearer "),
        "credentials_needed": "Sentry access token (Authorization: Sentry-Bearer <token>). OAuth also supported.",
        "example_tools": ["search_events", "search_issues", "use_sentry", "triage"],
        "live_testable": False,
    },
    "linear": {
        "id": "linear",
        "display_name": "Linear MCP",
        "publisher": "Linear",
        "category": "Project Management",
        "endpoint": "https://mcp.linear.app/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "Linear access token (Authorization: Bearer <token>). OAuth 2.1 also supported.",
        "example_tools": ["create_issue", "update_issue", "list_issues", "create_comment"],
        "live_testable": False,
    },
    "monday": {
        "id": "monday",
        "display_name": "monday.com MCP",
        "publisher": "monday.com",
        "category": "Project Management",
        "endpoint": "https://mcp.monday.com/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "monday.com API token (Authorization: Bearer <token>). OAuth also supported.",
        "example_tools": ["create_item", "create_board", "change_item_column_values", "get_board_schema"],
        "live_testable": False,
    },
    "datadog": {
        "id": "datadog",
        "display_name": "Datadog MCP (Bits AI)",
        "publisher": "Datadog",
        "category": "Observability",
        "endpoint": "https://mcp.datadoghq.com/api/unstable/mcp-server/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "Datadog token (Authorization: Bearer) or DD-API-KEY + DD-APPLICATION-KEY headers. Region-specific host.",
        "example_tools": ["search_logs", "query_metrics", "list_monitors", "get_dashboard"],
        "live_testable": False,
    },
    "intercom": {
        "id": "intercom",
        "display_name": "Intercom MCP",
        "publisher": "Intercom",
        "category": "Support",
        "endpoint": "https://mcp.intercom.com/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "Intercom access token (Authorization: Bearer). US-hosted workspaces only. OAuth also supported.",
        "example_tools": ["search_conversations", "get_conversation", "search_contacts", "list_articles"],
        "live_testable": False,
    },
    "paypal": {
        "id": "paypal",
        "display_name": "PayPal MCP",
        "publisher": "PayPal",
        "category": "Payments",
        "endpoint": "https://mcp.paypal.com/http",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _bearer(),
        "credentials_needed": "PayPal Bearer access token (Authorization: Bearer). Sandbox at mcp.sandbox.paypal.com.",
        "example_tools": ["create_invoice", "send_invoice", "create_order", "list_transactions"],
        "live_testable": False,
    },
    # ---- Tier 3: direct, machine OAuth (client-credentials) / SigV4 --------
    "databricks": {
        "id": "databricks",
        "display_name": "Databricks Managed MCP",
        "publisher": "Databricks",
        "category": "Data & Analytics",
        "endpoint": "https://{workspace_hostname}/api/2.0/mcp/{service}",
        "tier": "direct-oauth",
        "verified": "docs",
        "auth_type": "oauth2_client_credentials",
        "oauth_descriptor": {
            "grant_type": "CLIENT_CREDENTIALS",
            "token_url": "https://{workspace_hostname}/oidc/v1/token",
            "service_paths": ["genie/{space_id}", "functions/{catalog}/{schema}", "vector-search/{catalog}/{schema}", "sql"],
        },
        "credentials_needed": "Databricks service-principal client ID + OAuth secret (M2M). Unity Catalog governs data.",
        "example_tools": ["genie_query", "uc_function", "vector_search", "databricks_sql"],
        "live_testable": False,
    },
    "snowflake-cortex": {
        "id": "snowflake-cortex",
        "display_name": "Snowflake Cortex MCP",
        "publisher": "Snowflake",
        "category": "Data & Analytics",
        "endpoint": "https://{account_url}/api/v2/databases/{database}/schemas/{schema}/mcp-servers/{name}",
        "tier": "direct-oauth",
        "verified": "docs",
        "auth_type": "oauth2_client_credentials",
        "oauth_descriptor": {"grant_type": "CLIENT_CREDENTIALS", "note": "OAuth 2.0 or PAT-as-Bearer; non-streaming responses"},
        "credentials_needed": "Snowflake OAuth client or Programmatic Access Token, scoped to a least-priv role.",
        "example_tools": ["cortex_search", "cortex_analyst", "execute_sql", "cortex_agent_run"],
        "live_testable": False,
    },
    "elasticsearch": {
        "id": "elasticsearch",
        "display_name": "Elasticsearch Agent Builder MCP",
        "publisher": "Elastic",
        "category": "Data & Analytics",
        "endpoint": "https://{elastic_project}/api/agent_builder/mcp",
        "tier": "direct-apikey",
        "verified": "docs",
        "auth_type": "api_key",
        "api_key_descriptor": _header_key("Authorization", "ApiKey "),
        "credentials_needed": "Elasticsearch API key (Authorization: ApiKey <key>). Elastic 9.2+/Serverless.",
        "example_tools": ["search", "esql", "list_indices", "get_mappings"],
        "live_testable": False,
    },
    "aws-mcp": {
        "id": "aws-mcp",
        "display_name": "AWS MCP (preview)",
        "publisher": "AWS",
        "category": "Cloud Ops",
        "endpoint": "https://aws-mcp.us-east-1.api.aws/mcp",
        "tier": "direct-oauth",
        "verified": "docs",
        "auth_type": "iam_sigv4",
        "oauth_descriptor": {"iam_service": "bedrock-agentcore", "note": "SigV4-signed; managed AWS-hosted MCP"},
        "credentials_needed": "AWS credentials (SigV4). IAM permissions for the AWS APIs invoked.",
        "example_tools": ["call_aws", "suggest_aws_commands", "get_execution_plan"],
        "live_testable": False,
    },
    # ---- Tier 4a: 3LO / dynamic client registration → adapter required -----
    "notion": {
        "id": "notion", "display_name": "Notion MCP", "publisher": "Notion",
        "category": "Productivity", "endpoint": "https://mcp.notion.com/mcp",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Notion workspace OAuth (browser consent). No bearer — needs an adapter for headless.",
        "example_tools": ["search", "fetch", "create-pages", "update-page"], "live_testable": False,
    },
    "atlassian": {
        "id": "atlassian", "display_name": "Atlassian (Jira+Confluence) MCP", "publisher": "Atlassian",
        "category": "Project Management", "endpoint": "https://mcp.atlassian.com/v1/mcp",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Atlassian Cloud OAuth (browser consent). Needs an adapter for headless Gateway use.",
        "example_tools": ["search", "fetch", "createJiraIssue", "createConfluencePage"], "live_testable": False,
    },
    "salesforce": {
        "id": "salesforce", "display_name": "Salesforce MCP", "publisher": "Salesforce",
        "category": "CRM", "endpoint": "https://api.salesforce.com/platform/mcp/v1/{server}",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Salesforce OAuth 2.0 + PKCE (per-user). Needs an adapter for headless Gateway use.",
        "example_tools": ["sobject_read", "soql_query", "describe_object", "apex_action"], "live_testable": False,
    },
    "hubspot": {
        "id": "hubspot", "display_name": "HubSpot MCP", "publisher": "HubSpot",
        "category": "CRM", "endpoint": "https://mcp.hubspot.com",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "HubSpot OAuth 2.0 + PKCE (browser consent). Needs an adapter for headless Gateway use.",
        "example_tools": ["get_crm_object", "search_crm", "summarize_tickets", "get_engagements"], "live_testable": False,
    },
    "asana": {
        "id": "asana", "display_name": "Asana MCP", "publisher": "Asana",
        "category": "Project Management", "endpoint": "https://mcp.asana.com/v2/mcp",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Asana OAuth (browser consent). Needs an adapter for headless Gateway use.",
        "example_tools": ["create_task", "search_tasks", "list_sections", "get_project_status"], "live_testable": False,
    },
    "box": {
        "id": "box", "display_name": "Box MCP", "publisher": "Box",
        "category": "Storage", "endpoint": "https://mcp.box.com",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Box OAuth 2.0 (admin-enabled). Needs an adapter for headless Gateway use.",
        "example_tools": ["search_files_keyword", "get_file_content", "upload_file", "ai_qa_single_file"], "live_testable": False,
    },
    "gitlab": {
        "id": "gitlab", "display_name": "GitLab MCP", "publisher": "GitLab",
        "category": "Developer Tools", "endpoint": "https://gitlab.com/api/v4/mcp",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "GitLab OAuth 2.0 (DCR, browser consent). Beta. Needs an adapter for headless Gateway use.",
        "example_tools": ["get_project", "get_issue", "get_merge_request", "create_issue"], "live_testable": False,
    },
    "figma": {
        "id": "figma", "display_name": "Figma Dev Mode MCP", "publisher": "Figma",
        "category": "Design", "endpoint": "https://mcp.figma.com/mcp",
        "tier": "adapter-3lo", "verified": "docs", "auth_type": "oauth2_3lo",
        "credentials_needed": "Figma OAuth (browser consent). Needs an adapter for headless Gateway use.",
        "example_tools": ["get_code", "get_variable_defs", "get_image", "add_to_canvas"], "live_testable": False,
    },
    # ---- Tier 4b: stdio-only → containerize behind streamable-HTTP ---------
    "aws-labs-stdio": {
        "id": "aws-labs-stdio",
        "display_name": "AWS Labs MCP servers (stdio family)",
        "publisher": "AWS Labs",
        "category": "Cloud Ops",
        "endpoint": None,
        "tier": "adapter-stdio",
        "verified": "docs",
        "auth_type": "iam_sigv4",
        "credentials_needed": "AWS credentials. ~58 servers (bedrock-agentcore, cloudwatch, cost, dynamodb, eks...) are stdio-only; host behind mcp-proxy on Runtime.",
        "example_tools": ["call_aws", "get_metric_data", "get_cost_and_usage", "describe_tables"],
        "live_testable": False,
    },
    "brave-search": {
        "id": "brave-search", "display_name": "Brave Search MCP", "publisher": "Brave",
        "category": "Search & Web", "endpoint": None, "tier": "adapter-stdio",
        "verified": "docs", "auth_type": "api_key",
        "credentials_needed": "Brave Search API key. stdio-only; host behind mcp-proxy to target.",
        "example_tools": ["brave_web_search", "brave_local_search", "brave_news_search"], "live_testable": False,
    },
    "perplexity": {
        "id": "perplexity", "display_name": "Perplexity MCP", "publisher": "Perplexity",
        "category": "Search & Web", "endpoint": None, "tier": "adapter-stdio",
        "verified": "docs", "auth_type": "api_key",
        "credentials_needed": "Perplexity API key. stdio-only; host behind mcp-proxy to target.",
        "example_tools": ["perplexity_search", "perplexity_ask", "perplexity_research"], "live_testable": False,
    },
}


# ---------------------------------------------------------------------------
# Helpers (mirror connectors_catalog's copy-on-read contract)
# ---------------------------------------------------------------------------


def get_mcp_server(server_id: str) -> Optional[dict]:
    """Return a deep copy of one MCP entry, or ``None`` if unknown."""
    entry = MCP_SERVERS.get(server_id)
    return deepcopy(entry) if entry is not None else None


def list_mcp_servers() -> list[dict]:
    """Return deep copies of all MCP entries (catalog order)."""
    return [deepcopy(entry) for entry in MCP_SERVERS.values()]


def list_by_tier(tier: str) -> list[dict]:
    """All entries in a given integration tier."""
    return [deepcopy(e) for e in MCP_SERVERS.values() if e["tier"] == tier]


def live_testable_servers() -> list[dict]:
    """Entries that can be end-to-end tested with NO vendor credentials."""
    return [deepcopy(e) for e in MCP_SERVERS.values() if e.get("live_testable")]
