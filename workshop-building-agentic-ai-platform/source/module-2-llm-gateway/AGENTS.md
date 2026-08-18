# Module 2: LLM Gateway — agent integration guide

## How agents use the LLM Gateway

The LLM Gateway (LiteLLM Proxy) provides an OpenAI-compatible HTTPS endpoint (via API Gateway) that agents call for model access. Authentication is via **virtual keys** (not provider keys). The proxy routes requests to Amazon Bedrock using the ECS task role.

### Strands Agents (recommended — native provider)

```python
import os

from strands import Agent
from strands.models.litellm import LiteLLMModel

model = LiteLLMModel(
    # Connection settings go in client_args, which LiteLLMModel splats straight
    # into litellm.acompletion(). `params` is for inference parameters
    # (max_tokens, temperature, ...) and will not route your calls.
    client_args={
        "api_base": os.environ["LLM_GATEWAY_URL"],     # HTTPS API Gateway endpoint
        "api_key": os.environ["LLM_GATEWAY_API_KEY"],  # Virtual key (not admin key)
    },
    model_id="openai/claude-sonnet",  # "openai/" selects the OpenAI-compatible
                                      # proxy path; the alias is the friendly
                                      # name from the model registry
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
import os

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

## Gateway endpoints

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

## Model names

Use the **friendly names** the gateway registers, not full Bedrock model IDs.
The table below is a **representative sample**, not the full registry: `scripts/setup_keys.py`
registers 17 aliases across Anthropic Claude, Amazon Nova, Meta Llama, Mistral AI, and DeepSeek.
`reference/litellm-config.yaml` is a wider 55-alias *reference* catalog (it adds Writer, Google
Gemma, NVIDIA Nemotron, Qwen, MiniMax, Moonshot, Z.ai, and OpenAI OSS) — it is documentation, not
mounted into the container, so it is a menu rather than a description of the live gateway.

| Friendly Name | Bedrock Model ID (as registered in `us-west-2`) |
|--------------|--------------------------------------|
| `claude-sonnet` | `global.anthropic.claude-sonnet-4-6` |
| `claude-opus` | `global.anthropic.claude-opus-4-6-v1` |
| `claude-haiku` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `nova-pro` | `us.amazon.nova-pro-v1:0` |
| `nova-lite` | `us.amazon.nova-lite-v1:0` |
| `nova-2-lite` | `global.amazon.nova-2-lite-v1:0` |
| `llama3.3-70b` | `us.meta.llama3-3-70b-instruct-v1:0` |
| `mistral-large-3` | `mistral.mistral-large-3-675b-instruct` |
| `deepseek-r1` | `us.deepseek.r1-v1:0` |

Only the **friendly name** is a stable contract — the Bedrock ID column is what
`scripts/setup_keys.py` resolves in `us-west-2`, and the prefix is chosen per
region at registration time: `global.` for models that publish a global
inference profile, a geo prefix (`us.` / `eu.`) for profile-only models, and no
prefix at all for bare on-demand models such as Mistral. A model with
no invocable flavor in the deploy region is skipped rather than registered, so
the exact set present on your gateway is region-dependent. `WORKSHOP_MODELS` in
`scripts/setup_keys.py` is the authoritative list; query the live gateway with
`GET /model/info` to see what was actually registered.

## Environment variables for agents

```bash
export LLM_GATEWAY_URL=https://<api-id>.execute-api.<region>.amazonaws.com  # HTTPS (from CFN output: ProxyUrl)
export LLM_GATEWAY_API_KEY=<virtual-key>           # Virtual key (from setup_keys.py)
export LLM_GATEWAY_ADMIN_KEY=<admin-key>           # Admin only (from Secrets Manager)
```

## Virtual key hierarchy

```
Admin Key (admin — create teams, keys, view all spend)
  ├── Team: platform-team (budget: $10)
  │     └── sk-platform-admin-key
  └── Team: workload-team (budget: $5)
        ├── sk-agent-alpha-key (for Agent Alpha)
        └── sk-agent-beta-key  (for Agent Beta)
```

Each agent gets its own virtual key. The platform tracks per-key spend, enforces per-key budgets, and rate-limits independently.

## Cross-module integration (Module 5)

In Module 5, the agent combines all platform services:

```python
import os

from strands import Agent
from strands.models.litellm import LiteLLMModel

# Model access via Module 2 (LLM Gateway)
model = LiteLLMModel(
    client_args={
        "api_base": os.environ["LLM_GATEWAY_URL"],
        "api_key": os.environ["LLM_GATEWAY_API_KEY"],
    },
    model_id="openai/claude-sonnet",
)

# Tool discovery via Module 3 (MCP Registry) + Module 4 (Tools Gateway)
tools = discover_tools_from_gateway(os.environ["TOOLS_GATEWAY_URL"])

agent = Agent(model=model, tools=tools)
```
