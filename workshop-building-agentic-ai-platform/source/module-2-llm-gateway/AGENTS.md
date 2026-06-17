# Module 2: LLM Gateway — Agent Integration Guide

## How Agents Use the LLM Gateway

The LLM Gateway (LiteLLM Proxy) provides an OpenAI-compatible HTTPS endpoint (via API Gateway) that agents call for model access. Authentication is via **virtual keys** (not provider keys). The proxy routes requests to Amazon Bedrock using the ECS task role.

### Strands Agents (Recommended — Native Provider)

```python
from strands import Agent
from strands.models.litellm import LiteLLMModel

model = LiteLLMModel(
    model_id="claude-sonnet",                    # Friendly name from model registry
    api_base=os.environ["LLM_GATEWAY_URL"],      # HTTPS API Gateway endpoint
    api_key=os.environ["LLM_GATEWAY_API_KEY"],   # Virtual key (not admin key)
)

agent = Agent(model=model, tools=[...])
result = agent("Analyse the data and file a ticket.")
```

`LiteLLMModel` is the recommended provider because:
- **Tool calling works natively** — LiteLLM speaks the Bedrock Converse API, so tool definitions and tool call responses flow without translation
- **Cost is tracked** — Every LLM call is attributed to the virtual key
- **Guardrails are enforced** — Bedrock Guardrails apply centrally at the proxy
- **Models are swappable** — Change `model_id` without code changes

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["LLM_GATEWAY_API_KEY"],
    base_url=os.environ["LLM_GATEWAY_URL"]
)

response = client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Hello"}]
)
```

### Direct HTTP

```bash
curl "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Gateway Endpoints

All endpoints are on port 4000 (single port).

| Path | Method | Purpose |
|------|--------|---------|
| `/chat/completions` | POST | Chat completions (OpenAI-compatible) |
| `/models` | GET | List available models |
| `/health/liveliness` | GET | Health check (no auth required) |
| `/health` | GET | Model health (requires auth) |
| `/key/generate` | POST | Create virtual key (requires admin key) |
| `/key/info` | GET | Get key info and spend |
| `/team/new` | POST | Create team (requires admin key) |
| `/spend/logs` | GET | Get spend logs |
| `/ui` | GET | LiteLLM Admin UI (browser) |

## Model Names

Use the **friendly names** from `litellm-config.yaml`, not full Bedrock model IDs:

| Friendly Name | Bedrock Model ID (Inference Profile) |
|--------------|--------------------------------------|
| `claude-sonnet` | `us.anthropic.claude-sonnet-4-6` |
| `claude-opus` | `us.anthropic.claude-opus-4-6-v1` |
| `claude-haiku` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `nova-pro` | `us.amazon.nova-pro-v1:0` |
| `nova-lite` | `us.amazon.nova-lite-v1:0` |
| `llama4-scout` | `us.meta.llama4-scout-17b-instruct-v1:0` |
| `mistral-large-3` | `us.mistral.mistral-large-3-675b-instruct` |
| `deepseek-r1` | `us.deepseek.r1-v1:0` |

See `cfn/litellm-config.yaml` for the full list of 70+ models.

## Environment Variables for Agents

```bash
export LLM_GATEWAY_URL=https://<api-id>.execute-api.<region>.amazonaws.com  # HTTPS (from CFN output: ProxyUrl)
export LLM_GATEWAY_API_KEY=<virtual-key>           # Virtual key (from setup_keys.py)
export LLM_GATEWAY_ADMIN_KEY=<admin-key>           # Admin only (from Secrets Manager)
```

## Virtual Key Hierarchy

```
Admin Key (admin — create teams, keys, view all spend)
  ├── Team: platform-team (budget: $10)
  │     └── sk-platform-admin-key
  └── Team: workload-team (budget: $5)
        ├── sk-agent-alpha-key (for Agent Alpha)
        └── sk-agent-beta-key  (for Agent Beta)
```

Each agent gets its own virtual key. The platform tracks per-key spend, enforces per-key budgets, and rate-limits independently.

## Cross-Module Integration (Module 5)

In Module 5, the agent combines all platform services:

```python
from strands import Agent
from strands.models.litellm import LiteLLMModel

# Model access via Module 2 (LLM Gateway)
model = LiteLLMModel(
    model_id="claude-sonnet",
    api_base=os.environ["LLM_GATEWAY_URL"],
    api_key=os.environ["LLM_GATEWAY_API_KEY"],
)

# Tool discovery via Module 3 (MCP Registry) + Module 4 (Tools Gateway)
tools = discover_tools_from_gateway(os.environ["TOOLS_GATEWAY_URL"])

agent = Agent(model=model, tools=tools)
```
