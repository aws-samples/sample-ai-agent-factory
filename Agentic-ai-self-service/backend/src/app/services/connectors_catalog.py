"""Phase 3 Gap 3E — pre-built connector catalog (pure data).

This module is a **read-only** catalog of ~12 pre-baked connectors (Slack,
GitHub, Jira, Notion, Salesforce, Google Drive, Gmail, Confluence, PagerDuty,
HubSpot, Stripe, SendGrid). Each entry describes the connector's identity, the
credential fields a wiring UI must collect, and a list of gateway-tool schemas
(shaped exactly like ``services.gateway_deployer.GATEWAY_TOOL_SCHEMAS`` entries)
that the MCP Gateway can advertise unchanged.

IMPORTANT — scope of this gap:
  * This is **catalog-only**. The ``tool_schemas`` *describe* gateway targets
    so the picker UI can preview a connector's capabilities and so a follow-up
    can register them as real gateway targets. **Per-connector Lambda execution
    does NOT exist yet** — registering these schemas advertises a target shape,
    it does not make Slack/GitHub/etc. calls actually run.
  * The catalog carries **no tenant data** — it is identical for every caller.
    The router therefore auth-gates but does NOT owner-scope these endpoints.
  * Credential storage (writing an owner-scoped secret to Secrets Manager under
    ``agentcore-connector/{connector_id}/{safe_owner}-{uuid}``) is a documented
    follow-up hook, NOT built in this module.

Bug-10 invariant: every ``inputSchema`` (recursively, including nested
``properties`` and ``items``) uses ONLY the gateway-allowed JSON-Schema keys
``{type, properties, required, items, description}``. No ``enum``/``default``/
``format``/``additionalProperties`` — the gateway rejects those (Bug 10).

Bug-11 note: when these tool names later become real gateway targets, the
``<target>___<tool>`` qualified name must stay under the 64-char AgentCore
limit. Connector tool names are kept short and unprefixed here; the follow-up
that registers them as targets owns the qualification.

The module is import-safe: no boto3 / no AWS clients are created at import.
"""

from __future__ import annotations

from copy import deepcopy

# JSON-Schema keys the gateway accepts on a tool inputSchema (Bug 10).
ALLOWED_SCHEMA_KEYS: frozenset[str] = frozenset({"type", "properties", "required", "items", "description"})


# ---------------------------------------------------------------------------
# Catalog data
# ---------------------------------------------------------------------------
# Each entry:
#   id               slug (matches ^[a-z0-9][a-z0-9_-]*$)
#   display_name     human label
#   icon             frontend icon key (mirrored in frontend/src/data/connectors.ts)
#   category         grouping for the picker UI
#   auth_type        'oauth' | 'api_key'  (how the wiring UI collects creds)
#   credential_schema  the fields the wiring UI collects (Bug-10-shaped object)
#   tool_schemas     list of GATEWAY_TOOL_SCHEMAS-shaped dicts to advertise
#   capabilities     short human-readable capability tags

CONNECTORS: dict[str, dict] = {
    "slack": {
        "id": "slack",
        "display_name": "Slack",
        "icon": "slack",
        "category": "Communication",
        "auth_type": "oauth",
        "credential_schema": {
            "type": "object",
            "properties": {
                "bot_token": {
                    "type": "string",
                    "description": "Slack bot OAuth token (xoxb-...).",
                },
            },
            "required": ["bot_token"],
        },
        "tool_schemas": [
            {
                "name": "send_message",
                "description": "Post a message to a Slack channel.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel ID or name."},
                        "text": {"type": "string", "description": "Message text."},
                    },
                    "required": ["channel", "text"],
                },
            },
            {
                "name": "list_channels",
                "description": "List channels the bot can see.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max channels to return."},
                    },
                },
            },
        ],
        "capabilities": ["send messages", "list channels"],
    },
    "github": {
        "id": "github",
        "display_name": "GitHub",
        "icon": "github",
        "category": "Developer Tools",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "access_token": {
                    "type": "string",
                    "description": "GitHub personal access token (repo scope).",
                },
            },
            "required": ["access_token"],
        },
        "tool_schemas": [
            {
                "name": "create_issue",
                "description": "Open a new issue in a repository.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name of the repo."},
                        "title": {"type": "string", "description": "Issue title."},
                        "body": {"type": "string", "description": "Issue body."},
                    },
                    "required": ["repo", "title"],
                },
            },
            {
                "name": "list_pull_requests",
                "description": "List open pull requests for a repository.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name of the repo."},
                    },
                    "required": ["repo"],
                },
            },
        ],
        "capabilities": ["create issues", "list pull requests"],
    },
    "jira": {
        "id": "jira",
        "display_name": "Jira",
        "icon": "jira",
        "category": "Project Management",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "Jira site base URL."},
                "email": {"type": "string", "description": "Account email."},
                "api_token": {"type": "string", "description": "Jira API token."},
            },
            "required": ["base_url", "email", "api_token"],
        },
        "tool_schemas": [
            {
                "name": "create_ticket",
                "description": "Create a Jira issue in a project.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_key": {"type": "string", "description": "Project key, e.g. ENG."},
                        "summary": {"type": "string", "description": "Issue summary."},
                        "description": {"type": "string", "description": "Issue description."},
                    },
                    "required": ["project_key", "summary"],
                },
            },
            {
                "name": "search_issues",
                "description": "Search issues using a JQL query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jql": {"type": "string", "description": "JQL query string."},
                    },
                    "required": ["jql"],
                },
            },
        ],
        "capabilities": ["create tickets", "search issues"],
    },
    "notion": {
        "id": "notion",
        "display_name": "Notion",
        "icon": "notion",
        "category": "Productivity",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "integration_token": {
                    "type": "string",
                    "description": "Notion internal integration token.",
                },
            },
            "required": ["integration_token"],
        },
        "tool_schemas": [
            {
                "name": "create_page",
                "description": "Create a page under a parent page or database.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "parent_id": {"type": "string", "description": "Parent page/database ID."},
                        "title": {"type": "string", "description": "Page title."},
                        "content": {"type": "string", "description": "Page body text."},
                    },
                    "required": ["parent_id", "title"],
                },
            },
            {
                "name": "search",
                "description": "Search Notion pages and databases by text.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text."},
                    },
                    "required": ["query"],
                },
            },
        ],
        "capabilities": ["create pages", "search workspace"],
    },
    "salesforce": {
        "id": "salesforce",
        "display_name": "Salesforce",
        "icon": "salesforce",
        "category": "CRM",
        "auth_type": "oauth",
        "credential_schema": {
            "type": "object",
            "properties": {
                "instance_url": {"type": "string", "description": "Salesforce instance URL."},
                "access_token": {"type": "string", "description": "OAuth access token."},
            },
            "required": ["instance_url", "access_token"],
        },
        "tool_schemas": [
            {
                "name": "create_lead",
                "description": "Create a Lead record.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "last_name": {"type": "string", "description": "Lead last name."},
                        "company": {"type": "string", "description": "Lead company."},
                        "email": {"type": "string", "description": "Lead email."},
                    },
                    "required": ["last_name", "company"],
                },
            },
            {
                "name": "query",
                "description": "Run a SOQL query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "soql": {"type": "string", "description": "SOQL query string."},
                    },
                    "required": ["soql"],
                },
            },
        ],
        "capabilities": ["create leads", "run SOQL queries"],
    },
    "google_drive": {
        "id": "google_drive",
        "display_name": "Google Drive",
        "icon": "google_drive",
        "category": "Storage",
        "auth_type": "oauth",
        "credential_schema": {
            "type": "object",
            "properties": {
                "access_token": {"type": "string", "description": "Google OAuth access token."},
            },
            "required": ["access_token"],
        },
        "tool_schemas": [
            {
                "name": "list_files",
                "description": "List files in Drive, optionally filtered by query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Drive search query."},
                    },
                },
            },
            {
                "name": "download_file",
                "description": "Download a file's contents by file ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_id": {"type": "string", "description": "Drive file ID."},
                    },
                    "required": ["file_id"],
                },
            },
        ],
        "capabilities": ["list files", "download files"],
    },
    "gmail": {
        "id": "gmail",
        "display_name": "Gmail",
        "icon": "gmail",
        "category": "Communication",
        "auth_type": "oauth",
        "credential_schema": {
            "type": "object",
            "properties": {
                "access_token": {"type": "string", "description": "Google OAuth access token."},
            },
            "required": ["access_token"],
        },
        "tool_schemas": [
            {
                "name": "send_email",
                "description": "Send an email message.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient address."},
                        "subject": {"type": "string", "description": "Email subject."},
                        "body": {"type": "string", "description": "Email body."},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
            {
                "name": "search_messages",
                "description": "Search the mailbox with a Gmail query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Gmail search query."},
                    },
                    "required": ["query"],
                },
            },
        ],
        "capabilities": ["send email", "search messages"],
    },
    "confluence": {
        "id": "confluence",
        "display_name": "Confluence",
        "icon": "confluence",
        "category": "Productivity",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "Confluence site base URL."},
                "email": {"type": "string", "description": "Account email."},
                "api_token": {"type": "string", "description": "Confluence API token."},
            },
            "required": ["base_url", "email", "api_token"],
        },
        "tool_schemas": [
            {
                "name": "create_page",
                "description": "Create a Confluence page in a space.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "space_key": {"type": "string", "description": "Space key."},
                        "title": {"type": "string", "description": "Page title."},
                        "body": {"type": "string", "description": "Page body (storage format)."},
                    },
                    "required": ["space_key", "title"],
                },
            },
            {
                "name": "search_pages",
                "description": "Search pages using a CQL query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cql": {"type": "string", "description": "CQL query string."},
                    },
                    "required": ["cql"],
                },
            },
        ],
        "capabilities": ["create pages", "search pages"],
    },
    "pagerduty": {
        "id": "pagerduty",
        "display_name": "PagerDuty",
        "icon": "pagerduty",
        "category": "Incident Management",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "api_token": {"type": "string", "description": "PagerDuty REST API token."},
            },
            "required": ["api_token"],
        },
        "tool_schemas": [
            {
                "name": "create_incident",
                "description": "Trigger a new incident on a service.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string", "description": "PagerDuty service ID."},
                        "title": {"type": "string", "description": "Incident title."},
                        "urgency": {"type": "string", "description": "high or low."},
                    },
                    "required": ["service_id", "title"],
                },
            },
            {
                "name": "list_incidents",
                "description": "List incidents, optionally filtered by status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "Incident status filter."},
                    },
                },
            },
        ],
        "capabilities": ["create incidents", "list incidents"],
    },
    "hubspot": {
        "id": "hubspot",
        "display_name": "HubSpot",
        "icon": "hubspot",
        "category": "CRM",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "access_token": {"type": "string", "description": "HubSpot private app token."},
            },
            "required": ["access_token"],
        },
        "tool_schemas": [
            {
                "name": "create_contact",
                "description": "Create a CRM contact.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Contact email."},
                        "first_name": {"type": "string", "description": "First name."},
                        "last_name": {"type": "string", "description": "Last name."},
                    },
                    "required": ["email"],
                },
            },
            {
                "name": "search_contacts",
                "description": "Search CRM contacts by text.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text."},
                    },
                    "required": ["query"],
                },
            },
        ],
        "capabilities": ["create contacts", "search contacts"],
    },
    "stripe": {
        "id": "stripe",
        "display_name": "Stripe",
        "icon": "stripe",
        "category": "Payments",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "secret_key": {"type": "string", "description": "Stripe secret API key (sk_...)."},
            },
            "required": ["secret_key"],
        },
        "tool_schemas": [
            {
                "name": "create_customer",
                "description": "Create a Stripe customer.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "Customer email."},
                        "name": {"type": "string", "description": "Customer name."},
                    },
                    "required": ["email"],
                },
            },
            {
                "name": "list_charges",
                "description": "List recent charges.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max charges to return."},
                    },
                },
            },
        ],
        "capabilities": ["create customers", "list charges"],
    },
    "sendgrid": {
        "id": "sendgrid",
        "display_name": "SendGrid",
        "icon": "sendgrid",
        "category": "Communication",
        "auth_type": "api_key",
        "credential_schema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "description": "SendGrid API key."},
            },
            "required": ["api_key"],
        },
        "tool_schemas": [
            {
                "name": "send_email",
                "description": "Send a transactional email.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient address."},
                        "from_email": {"type": "string", "description": "Verified sender address."},
                        "subject": {"type": "string", "description": "Email subject."},
                        "body": {"type": "string", "description": "Email body."},
                    },
                    "required": ["to", "from_email", "subject", "body"],
                },
            },
        ],
        "capabilities": ["send transactional email"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_connector(connector_id: str) -> dict | None:
    """Return a deep copy of one connector entry, or ``None`` if unknown.

    A copy is returned so callers can never mutate the module-level catalog.
    """
    entry = CONNECTORS.get(connector_id)
    return deepcopy(entry) if entry is not None else None


def list_connectors() -> list[dict]:
    """Return deep copies of all connector entries (catalog order)."""
    return [deepcopy(entry) for entry in CONNECTORS.values()]
