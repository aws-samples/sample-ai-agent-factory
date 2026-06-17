---
title: "Register MCP Servers"
weight: 43
---

In this step you register two MCP servers — **Flights** and **Hotels** — so that agents can discover and call them through the governed gateway.

## What You Are Registering

The Tools Gateway stack (`workshop-tools-gateway-stack`) pre-deployed two Lambda-backed MCP servers:

| MCP Server | Tools | Description |
|-----------|-------|-------------|
| **Flights MCP** | `search_flights`, `get_flight_details`, `search_flights_by_budget` | Search flights by route, date, and budget using mock airline data |
| **Hotels MCP** | `search_hotels`, `get_hotel_details`, `search_hotels_by_budget` | Search hotels by city, dates, and price using mock hotel data |

Both Lambdas are already deployed and running. You are registering them in the Registry so agents can discover them via semantic search and invoke them through the MCP Gateway.

::alert[The Tools Gateway stack is pre-provisioned by Workshop Studio — you do not need to deploy it yourself.]{type="info"}

## Retrieve the Lambda Function URLs

Get the function URLs from your CloudFormation outputs:

:::code{showCopyAction=true showLineNumbers=false language=bash}
export FLIGHTS_MCP_URL=$(aws cloudformation describe-stacks \
  --stack-name workshop-tools-gateway-stack \
  --query "Stacks[0].Outputs[?OutputKey=='FlightsMcpFunctionUrl'].OutputValue" \
  --output text)

export HOTELS_MCP_URL=$(aws cloudformation describe-stacks \
  --stack-name workshop-tools-gateway-stack \
  --query "Stacks[0].Outputs[?OutputKey=='HotelsMcpFunctionUrl'].OutputValue" \
  --output text)

echo "Flights MCP: $FLIGHTS_MCP_URL"
echo "Hotels MCP:  $HOTELS_MCP_URL"
:::

## Register the Flights MCP Server

In the Registry UI, select **Register Server** from the navigation menu.

Fill in the registration form:

| Field | Value |
|-------|-------|
| **Server Name** | `workshop-flights-mcp` |
| **Path** | `/workshop-flights-mcp/` (auto-generated) |
| **Proxy URL** | The value of `$FLIGHTS_MCP_URL` |
| **Description** | `Flight search tools — search by route, date, and budget` |
| **Tags** | `workshop, flights, travel, lambda` |

![Flights MCP registration form filled in](/static/img/module-3/register-flights-mcp-form.png)

Select **Register** to submit. Once registered, look for `workshop-flights-mcp` under the **MCP Servers** tab or **All**.

The server will appear as **disabled** with a `security-pending` tag — this is expected. Toggle it to **enabled** using the switch in the server list:

![Flights MCP registered and enabled](/static/img/module-3/register-flights-mcp-success.png)

::alert[The Lambda function URLs use AWS_IAM authentication, so the Registry cannot reach them directly. You will see a `security-pending` tag and a "Scan Failed: Access denied" message — this is expected and does not affect functionality. The tools will be called through the AgentCore Gateway in the Tools Gateway section later in this module, which signs requests with IAM credentials.]{type="info"}

::::expand{header="What the scan details look like (and why each field is fine)"}
If you click into the scan details in the Registry UI you will see a JSON payload like the one below. Everything here is **expected** for workshop MCP servers:

:::code{showCopyAction=false showLineNumbers=false language=json}
{
  "server_url": "https://...lambda-url.<region>.on.aws/mcp",
  "is_safe": false,
  "scan_failed": true,
  "critical_issues": 0,
  "high_severity": 0,
  "medium_severity": 0,
  "low_severity": 0,
  "error_message": "Security scanner failed: ... Access denied to MCP server ..."
}
:::

| Field | What it means here |
|-------|--------------------|
| `is_safe: false` | A **conservative default** the Registry sets whenever a scan cannot complete. It is not a real security finding. |
| `scan_failed: true` | The scanner could not reach the Lambda function URL. Lambda URLs in this workshop use `AWS_IAM` auth (sigv4), and the Registry's scanner does not sign its probes — so the URL rejects it. |
| `critical_issues / high / medium / low = 0` | **Zero actual findings.** This is the line that matters for functionality. |
| `error_message: "... Access denied ..."` | The verbose form of the same thing — an `AWS_IAM`-protected endpoint rejecting an unsigned probe. |

Once you toggle the server to **enabled**, tool calls flow through the AgentCore Gateway's Lambda target (Module 3a's Tools Gateway section), which signs requests with IAM credentials and invokes the Lambda URL correctly. Nothing about this scan state blocks normal tool invocation.
::::

Verify the registration via the API — confirm the server is registered and enabled:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "$REGISTRY_URL/api/servers" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -c "
import sys, json
servers = json.load(sys.stdin).get('servers', [])
flights = [s for s in servers if 'workshop-flights-mcp' in s['path']]
if flights:
    s = flights[0]
    print(f'Name:    {s[\"display_name\"]}')
    print(f'Enabled: {s[\"is_enabled\"]}')
    print(f'URL:     {s[\"proxy_pass_url\"]}')
else:
    print('NOT FOUND')
"
:::

You should see the server listed with `Enabled: True`.

## Register the Hotels MCP Server

Repeat the process for the Hotels MCP server:

| Field | Value |
|-------|-------|
| **Server Name** | `workshop-hotels-mcp` |
| **Path** | `/workshop-hotels-mcp/` (auto-generated) |
| **Proxy URL** | The value of `$HOTELS_MCP_URL` |
| **Description** | `Hotel search tools — search by city, dates, and nightly budget` |
| **Tags** | `workshop, hotels, travel, lambda` |

Select **Register**, then toggle the server to **enabled** in the server list (same as Flights).

Verify:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "$REGISTRY_URL/api/servers" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -c "
import sys, json
servers = json.load(sys.stdin).get('servers', [])
hotels = [s for s in servers if 'workshop-hotels-mcp' in s['path']]
if hotels:
    s = hotels[0]
    print(f'Name:    {s[\"display_name\"]}')
    print(f'Enabled: {s[\"is_enabled\"]}')
    print(f'URL:     {s[\"proxy_pass_url\"]}')
else:
    print('NOT FOUND')
"
:::

You should see `Enabled: True`.

::alert[The Registry stores the server metadata and makes it discoverable. The actual tool calls (e.g. `search_flights`) will go through the AgentCore Gateway in the Tools Gateway section later in this module, which has IAM credentials to invoke the Lambda function URLs.]{type="info"}

Both MCP servers are registered and enabled. Next, register the Travel Agent card.
