---
title: "Test Model Access"
weight: 34
---

Now that you have virtual keys, let's test model access through the gateway using multiple methods — including a Strands Agent with tool calling.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

::alert[This walkthrough uses `LLM_GATEWAY_URL` and `LLM_GATEWAY_API_KEY` environment variables set in Step 3. If they are not set (e.g. you opened a new terminal), re-run the `export` commands from Step 3.]{type="info"}

With teams and virtual keys in place, verify that model access works through the gateway. You will test with curl, try multiple models, and preview the Strands Agent integration that Module 4 uses.

## 4.1 Test with curl

Send a chat completion request using the OpenAI-compatible API:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{
    "model": "claude-sonnet",
    "messages": [
      {"role": "user", "content": "What are three benefits of using an LLM Gateway?"}
    ],
    "max_tokens": 200
  }' | python -m json.tool
:::

Notice the model name is `claude-sonnet` (the friendly alias), not the full Bedrock model ID. LiteLLM maps it to `bedrock/us.anthropic.claude-sonnet-4-6` automatically via the model registry.

::alert[**Prompt Caching via the LLM Gateway.** When you send identical requests through the gateway (same model, messages, and parameters), LiteLLM caches the response. For Claude Sonnet and Opus models on Bedrock, prompt caching goes deeper — the Bedrock Converse API natively caches the prompt tokens themselves, reducing latency and cost for subsequent requests with the same context. In this workshop, the gateway's cache is per-container, so you may not always see a hit; in production, add a Redis backend (`cache_params:` in litellm-config.yaml) to share cache hits across all gateway tasks. To use it, just make identical API calls — no code changes needed.]{type="info"}

## 4.2 Test with the OpenAI Python SDK

Because the gateway is OpenAI-compatible, the standard `openai` package works:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Pinned for supply-chain reproducibility; matches the floor required by litellm==1.83.0 in the venv
pip install openai==2.8.0 --quiet

python3 << 'PYEOF'
import os
from openai import OpenAI

# The OpenAI-compatible interface is a convenience for existing code — every
# request is routed to Amazon Bedrock foundation models by the LLM Gateway,
# with Bedrock's managed security and governance controls applied.
client = OpenAI(
    api_key=os.environ["LLM_GATEWAY_API_KEY"],
    base_url=os.environ["LLM_GATEWAY_URL"]
)

response = client.chat.completions.create(
    model="claude-sonnet",
    messages=[{"role": "user", "content": "Hello from the LLM Gateway!"}],
    max_tokens=100
)
print(response.choices[0].message.content)
PYEOF
:::

::alert[This is the power of the OpenAI-compatible API — your existing code works with Amazon Bedrock models just by changing the base URL and model name.]{type="info"}

## 4.3 Multi-model routing

Switch between Bedrock models by changing the `model` parameter — no code changes needed:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Claude Sonnet (Anthropic)
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "What makes you unique? One sentence."}], "max_tokens": 50}' \
  | python -m json.tool

# Amazon Nova Lite
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "nova-2-lite", "messages": [{"role": "user", "content": "What makes you unique? One sentence."}], "max_tokens": 50}' \
  | python -m json.tool

# Claude Haiku
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "claude-haiku", "messages": [{"role": "user", "content": "What makes you unique? One sentence."}], "max_tokens": 50}' \
  | python -m json.tool
:::

## 4.4 Strands Agent with LiteLLMModel

This is the **critical integration point** — the single place where an agent's model client meets the platform's governance layer. Everything you built in Module 2 only matters if agents actually route their LLM calls through it. The `LiteLLMModel` class is how that happens: when you pass `api_base=<gateway URL>` and `api_key=<virtual key>` into a Strands `Agent`, every model call (chat completions, tool calls, streaming) goes through the gateway instead of directly to Bedrock. From the participant's point of view the change is **three lines of Python** — but the implications are:

- **Cost attribution** — Every call is attributed to the virtual key, which ties back to a team and a Cognito identity. Finance can query spend per team per day from the gateway's spend logs.
- **Budget enforcement** — If the key's `max_budget` is exhausted, the gateway rejects the call with `BudgetExceededError`. The agent sees an LLM error, not a Bedrock throttling event, and can degrade gracefully.
- **Guardrails** — Any Bedrock Guardrail attached at the gateway (see Module 2 step-5) is applied to *every* call from *every* agent, transparently. Agent code does not need to import or configure guardrails.
- **Model swapping** — Changing from `claude-sonnet` to `nova-2-lite` is a one-line change (the `model_id`). No other code changes. Useful for A/B evaluation and for failing over to a different model when one is throttling.

In Module 4, the FAST travel agent uses exactly this pattern — see `patterns/strands-travel-agent/travel_agent.py`. Here's the shape of the integration you will use there:

:::code{showCopyAction=true showLineNumbers=false language=bash}
pip install strands-agents[litellm]==0.1.5 --quiet

python3 << 'PYEOF'
import os
from strands import Agent
from strands.models.litellm import LiteLLMModel

model = LiteLLMModel(
    model_id="openai/claude-sonnet",
    params={
        "api_base": os.environ["LLM_GATEWAY_URL"],
        "api_key": os.environ["LLM_GATEWAY_API_KEY"],
    },
)

agent = Agent(model=model)
result = agent("What is 15 * 23? Think step by step.")
print(result)
PYEOF
:::

::alert[Tool calling works natively through this path. LiteLLM speaks the Bedrock Converse API, so when Strands sends tool definitions and the model responds with tool calls, everything flows through without translation issues.]{type="info"}

## 4.5 Run the full test script

For a comprehensive test covering health checks, model listing, completions, caching, multi-model routing, spend tracking, and Strands Agent integration:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/source/module-2-llm-gateway
python scripts/test_gateway.py
:::

The test script runs 8 tests and prints results for each. If `strands-agents[litellm]` is not installed, the Strands test is skipped gracefully.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-4-test-models.ipynb`**. This notebook demonstrates four different ways to interact with the LLM Gateway, with inline output so you can compare approaches side by side:

1. **Health check** — Quick liveness probe to confirm the gateway is reachable.
2. **List available models** — Queries the OpenAI-compatible `/models` endpoint and prints every configured model alias.
3. **Chat completion via `requests`** — A plain HTTP POST showing the raw request/response cycle.
4. **OpenAI SDK compatibility** — Demonstrates that the standard `openai` Python package works unchanged against the gateway. Pay attention to how only `base_url` and `api_key` differ from a standard OpenAI setup.
5. **Multi-model routing** — Sends the same prompt to Claude Sonnet, Claude Haiku, Nova Lite, and Titan Text. Compare the responses to see how different models handle identical prompts.
6. **Caching demonstration** — Sends two identical requests with `temperature=0` and measures latency. The proxy runs with `cache: true` but no shared backend, so cache hits are per-ECS-task — if both requests land on different tasks, expect similar latency. The exercise is here to show the API contract, not to produce a reliable speedup in this workshop environment.
7. **Strands Agent integration** — Creates a Strands `Agent` using `LiteLLMModel` pointed at the gateway. This is the pattern Module 4 agents will use in production.
8. **Workshop client library** — Shows the `LLMGatewayClient` convenience wrapper for quick interactions.

::alert[The Strands Agent cell (section 7) requires `strands-agents[litellm]` to be installed. The notebook installs it automatically in the first cell.]{type="info"}
