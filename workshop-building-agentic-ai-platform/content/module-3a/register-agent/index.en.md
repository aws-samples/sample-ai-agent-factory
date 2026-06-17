---
title: "Register an A2A Agent"
weight: 44
---

The AI/ML team is building a **Travel Agent** — an assistant that searches flights and hotels to plan trips. Before the agent is built (Module 4), you register its **agent card** in the registry so other agents can discover it by capability.

## What Is an Agent Card?

An agent card is a structured metadata document from the [A2A protocol](https://github.com/a2aproject/A2A) that describes an agent's capabilities, endpoint, and skills. It is the agent equivalent of an API specification — it tells other agents what this agent can do and how to talk to it.

For the Travel Agent, the card looks like this:

```json
{
  "name": "workshop-travel-agent",
  "description": "Plans trips by searching flights and hotels, comparing options, and recommending itineraries",
  "url": "https://<agent-endpoint-set-in-module-4>",
  "version": "1.0.0",
  "tags": ["workshop", "travel", "agentcore", "strands"],
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "plan_trip",
      "name": "Plan Trip",
      "description": "Plans a complete trip with flights and hotels for given dates and destinations"
    },
    {
      "id": "search_flights",
      "name": "Search Flights",
      "description": "Searches available flights between two cities on a given date"
    },
    {
      "id": "search_hotels",
      "name": "Search Hotels",
      "description": "Searches available hotels in a city for given dates and budget"
    }
  ]
}
```

::alert[The endpoint URL is a placeholder. In Module 4, after deploying the agent on AgentCore, you will update the card with the real endpoint. For now, the card establishes the agent's identity and discoverability in the registry.]{type="info"}

## Register the Agent via the API

Save the agent card JSON and register it via the API:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$REGISTRY_URL/api/agents/register?skip_validation=true" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "workshop-travel-agent",
    "description": "Plans trips by searching flights and hotels, comparing options, and recommending itineraries",
    "url": "'"$REGISTRY_URL"'/api/agents/workshop-travel-agent",
    "version": "1.0.0",
    "tags": ["workshop", "travel", "agentcore", "strands"],
    "visibility": "public",
    "capabilities": {"streaming": false, "pushNotifications": false},
    "skills": [
      {"id": "plan_trip", "name": "Plan Trip", "description": "Plans a complete trip with flights and hotels for given dates and destinations", "tags": ["travel", "planning"]},
      {"id": "search_flights", "name": "Search Flights", "description": "Searches available flights between two cities on a given date", "tags": ["flights", "search"]},
      {"id": "search_hotels", "name": "Search Hotels", "description": "Searches available hotels in a city for given dates and budget", "tags": ["hotels", "search"]}
    ]
  }' | python3 -m json.tool
:::

You should see `"message": "Agent registered successfully"` with the agent path and skill count.

::alert[We use `skip_validation=true` because the endpoint URL is a placeholder — the real AgentCore endpoint will be set in Module 4 after you deploy the agent. The `url` points back to the Registry itself as a temporary reachable address.]{type="info"}

Confirm the agent appears in the Registry UI under the **A2A Agents** tab:

![Travel Agent registered and visible in the A2A Agents tab](/static/img/module-3/register-agent-success.png)

## Verify the Agent Card

Confirm the agent is registered and its card is accessible:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "$REGISTRY_URL/api/agents/workshop-travel-agent" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -m json.tool
:::

The response should show the full agent card including skills, endpoint, and metadata.

## Test Semantic Discovery

Agents discover other agents by describing what they need in natural language — not by knowing the agent's name in advance.

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s -X POST "$REGISTRY_URL/api/agents/discover/semantic?query=agent+that+can+plan+travel&max_results=3" \
  -H "Authorization: Bearer $REGISTRY_TOKEN" \
  | python3 -m json.tool
:::

Your Travel Agent should appear at the top of the results. This is how agents in Module 4 will find each other at runtime.

The agent card is registered. Next, create the service account that the AgentCore-hosted Travel Agent will use to authenticate.
