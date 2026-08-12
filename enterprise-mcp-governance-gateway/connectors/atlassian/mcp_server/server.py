# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Atlassian (Jira + Confluence) MCP server — hosted on an AgentCore Runtime.

It is a **thin, stateless pass-through**: the per-user Atlassian OAuth token (3LO)
is minted + vaulted by AgentCore Identity and **injected by the AgentCore Gateway
mcpServer target onto the inbound `Authorization` header**. Each tool reads that
header and calls the Atlassian Cloud REST API *as that user*.

Security model (why it's built this way):
  * The server holds **no credentials** — not the OAuth client secret, not a user
    token. It only ever sees the short-lived bearer the gateway injects per request,
    so a compromise of this container leaks no durable secret.
  * Because the call uses the *user's* token, **Atlassian enforces that user's own
    permissions** server-side — defence-in-depth beneath our Cedar policy.
  * Runs in **stateless-http** mode (required by AgentCore Runtime): every request is
    self-contained, so the runtime can route to any microVM.

Transport: AgentCore Runtime hosts MCP over Streamable HTTP on :8000 at /mcp, with a
/ping health check.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import Context, FastMCP
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The site (cloudId) this connector targets. Resource-level 3LO token is bound to
# the site the user consented to; we call that site's REST API explicitly.
CLOUD_ID = os.environ["ATLASSIAN_CLOUD_ID"]
JIRA_BASE = f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/rest/api/3"
CONFLUENCE_BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki"

# nosec B104 - binding 0.0.0.0 is REQUIRED inside the AgentCore Runtime container: the
# service reaches the server through the container's mapped port, so listening on
# localhost only would make it unreachable. The container is not internet-exposed —
# inbound access is via the gateway/Runtime endpoint, which validates a JWT.
mcp = FastMCP("atlassian-mcp-server", host="0.0.0.0", port=8000, stateless_http=True)  # nosec B104


# --- HTTP + auth helpers ----------------------------------------------------
def _bearer(ctx: Context) -> str:
    """Per-user Atlassian token from the inbound Authorization header.

    The gateway's OAUTH mcpServer target injects it; we never store it.
    """
    req = getattr(ctx.request_context, "request", None)
    auth = req.headers.get("authorization", "") if req is not None else ""
    if not auth:
        # Fail closed: without the injected user token there is nothing to act as.
        raise RuntimeError("Missing Authorization bearer (gateway did not inject a user token).")
    return auth[7:] if auth.lower().startswith("bearer ") else auth


def _request(method: str, url: str, bearer: str, body=None) -> dict:
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Only allow https:// — refuse file:/, custom, or plaintext schemes (B310).
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise ValueError(f"Refusing non-HTTPS Atlassian URL: {url!r}")
    try:
        with urllib.request.urlopen(req) as resp:  # nosec B310 - scheme checked above
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        # Log the status ONLY — no response body. Atlassian error responses can echo back
        # user content (issue descriptions, page bodies, comments), so nothing from the
        # body is written to CloudWatch. The length is logged for triage.
        logger.error("Atlassian %s %s -> HTTP %s (body suppressed, %d chars)",
                     method, url, e.code, len(detail))
        # The caller gets the status only. The body is never propagated either — it can
        # echo user content, and an MCP tool error surfaces to the model/user verbatim.
        raise RuntimeError(f"Atlassian API {method} {url}: HTTP {e.code}") from e


def _adf(text: str) -> dict:
    """Minimal Atlassian Document Format wrapper (Jira/Confluence rich-text fields)."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


# --- Jira: read -------------------------------------------------------------
@mcp.tool()
def getJiraIssue(issueKey: str, ctx: Context = None) -> dict:
    """Get a Jira issue by key (e.g. PROJ-123): summary, status, assignee, type."""
    d = _request("GET", f"{JIRA_BASE}/issue/{urllib.parse.quote(issueKey)}", _bearer(ctx))
    f = d.get("fields", {})
    return {"key": d.get("key"), "summary": f.get("summary"),
            "status": (f.get("status") or {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "issueType": (f.get("issuetype") or {}).get("name"),
            "created": f.get("created"), "updated": f.get("updated")}


@mcp.tool()
def searchJiraIssuesUsingJql(jql: str, maxResults: int = 25, ctx: Context = None) -> dict:
    """Search Jira issues with a JQL query. Returns key, summary, status, assignee."""
    qs = urllib.parse.urlencode({"jql": jql, "maxResults": int(maxResults),
                                 "fields": "summary,status,assignee"})
    d = _request("GET", f"{JIRA_BASE}/search/jql?{qs}", _bearer(ctx))
    issues = [{"key": i["key"],
               "summary": (i.get("fields") or {}).get("summary"),
               "status": ((i.get("fields") or {}).get("status") or {}).get("name"),
               "assignee": ((i.get("fields") or {}).get("assignee") or {}).get("displayName")}
              for i in d.get("issues", [])]
    return {"issues": issues, "count": len(issues), "nextPageToken": d.get("nextPageToken")}


@mcp.tool()
def getVisibleJiraProjects(maxResults: int = 50, ctx: Context = None) -> dict:
    """List Jira projects the authenticated user can see."""
    d = _request("GET", f"{JIRA_BASE}/project/search?maxResults={int(maxResults)}", _bearer(ctx))
    return {"projects": [{"key": p["key"], "name": p["name"]} for p in d.get("values", [])]}


# --- Jira: write (role-gated by Cedar) --------------------------------------
@mcp.tool()
def createJiraIssue(projectKey: str, summary: str, issueType: str = "Task",
                    description: str = "", ctx: Context = None) -> dict:
    """Create a Jira issue in a project."""
    body = {"fields": {"project": {"key": projectKey}, "summary": summary,
                       "issuetype": {"name": issueType}}}
    if description:
        body["fields"]["description"] = _adf(description)
    d = _request("POST", f"{JIRA_BASE}/issue", _bearer(ctx), body=body)
    return {"key": d.get("key"), "id": d.get("id")}


@mcp.tool()
def editJiraIssue(issueKey: str, summary: str = "", description: str = "",
                  ctx: Context = None) -> dict:
    """Update an existing Jira issue's summary and/or description."""
    fields = {}
    if summary:
        fields["summary"] = summary
    if description:
        fields["description"] = _adf(description)
    _request("PUT", f"{JIRA_BASE}/issue/{urllib.parse.quote(issueKey)}", _bearer(ctx),
             body={"fields": fields})
    return {"key": issueKey, "status": "updated"}


@mcp.tool()
def addCommentToJiraIssue(issueKey: str, comment: str, ctx: Context = None) -> dict:
    """Add a comment to a Jira issue."""
    d = _request("POST", f"{JIRA_BASE}/issue/{urllib.parse.quote(issueKey)}/comment",
                 _bearer(ctx), body={"body": _adf(comment)})
    return {"id": d.get("id"), "created": d.get("created")}


@mcp.tool()
def transitionJiraIssue(issueKey: str, transitionId: str, ctx: Context = None) -> dict:
    """Move a Jira issue to a new workflow state by transition id."""
    _request("POST", f"{JIRA_BASE}/issue/{urllib.parse.quote(issueKey)}/transitions",
             _bearer(ctx), body={"transition": {"id": str(transitionId)}})
    return {"key": issueKey, "status": "transitioned"}


# --- Confluence: read -------------------------------------------------------
@mcp.tool()
def getConfluencePage(pageId: str, ctx: Context = None) -> dict:
    """Get a Confluence page by id (title + storage-format body)."""
    d = _request("GET", f"{CONFLUENCE_BASE}/api/v2/pages/{urllib.parse.quote(pageId)}?body-format=storage",
                 _bearer(ctx))
    return {"id": d.get("id"), "title": d.get("title"), "spaceId": d.get("spaceId"),
            "body": ((d.get("body") or {}).get("storage") or {}).get("value")}


@mcp.tool()
def searchConfluenceUsingCql(cql: str, limit: int = 25, ctx: Context = None) -> dict:
    """Search Confluence content with a CQL query."""
    qs = urllib.parse.urlencode({"cql": cql, "limit": int(limit)})
    d = _request("GET", f"{CONFLUENCE_BASE}/rest/api/search?{qs}", _bearer(ctx))
    return {"results": [{"title": (r.get("content") or {}).get("title"),
                         "id": (r.get("content") or {}).get("id"),
                         "type": (r.get("content") or {}).get("type")}
                        for r in d.get("results", [])], "count": d.get("size")}


@mcp.tool()
def getConfluenceSpaces(limit: int = 25, ctx: Context = None) -> dict:
    """List Confluence spaces the user can access."""
    d = _request("GET", f"{CONFLUENCE_BASE}/api/v2/spaces?limit={int(limit)}", _bearer(ctx))
    return {"spaces": [{"id": s["id"], "key": s.get("key"), "name": s.get("name")}
                       for s in d.get("results", [])]}


# --- Confluence: write (role-gated by Cedar) --------------------------------
@mcp.tool()
def createConfluencePage(spaceId: str, title: str, body: str, ctx: Context = None) -> dict:
    """Create a Confluence page (storage-format body) in a space."""
    payload = {"spaceId": spaceId, "status": "current", "title": title,
               "body": {"representation": "storage", "value": body}}
    d = _request("POST", f"{CONFLUENCE_BASE}/api/v2/pages", _bearer(ctx), body=payload)
    return {"id": d.get("id"), "title": d.get("title")}


@mcp.tool()
def updateConfluencePage(pageId: str, title: str, body: str, version: int,
                         ctx: Context = None) -> dict:
    """Update a Confluence page. `version` must be the current version number + ... (Confluence requires the next version)."""
    payload = {"id": pageId, "status": "current", "title": title,
               "body": {"representation": "storage", "value": body},
               "version": {"number": int(version)}}
    d = _request("PUT", f"{CONFLUENCE_BASE}/api/v2/pages/{urllib.parse.quote(pageId)}",
                 _bearer(ctx), body=payload)
    return {"id": d.get("id"), "version": (d.get("version") or {}).get("number")}


# --- Health -----------------------------------------------------------------
@mcp.custom_route("/ping", methods=["GET"])
async def ping(_request):
    return JSONResponse({"status": "Healthy"})


if __name__ == "__main__":
    logger.info("Atlassian MCP server (streamable-http) on 0.0.0.0:8000/mcp")
    logger.info("Jira base: %s | Confluence base: %s", JIRA_BASE, CONFLUENCE_BASE)
    mcp.run(transport="streamable-http")
