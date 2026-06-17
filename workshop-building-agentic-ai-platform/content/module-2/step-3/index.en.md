---
title: "Virtual Keys & Teams"
weight: 33
---

The LLM Gateway is running with an admin key, but in an enterprise solution you don't give every agent and team the admin key. Instead, you create **teams** with budgets and issue **virtual keys** that are scoped to those teams.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI Walkthrough

As the platform engineer, you will now create teams with budgets and issue virtual keys — this is how you control which agents can access which models and how much they can spend.

## How Virtual Keys Work

```
Admin Key (admin only)
  │
  ├── Team: platform-team (budget: $10)
  │     ├── sk-platform-admin-key (budget: $10)
  │     └── sk-platform-ci-key   (budget: $2)
  │
  └── Team: workload-team (budget: $5)
        ├── sk-agent-alpha-key   (budget: $3)
        └── sk-agent-beta-key    (budget: $2)
```

Each virtual key:
- Is scoped to specific models (e.g., only `claude-sonnet` and `nova-lite`)
- Has a max budget — requests are rejected when the budget is exhausted
- Tracks spend independently — per-key cost attribution
- Can be revoked instantly without affecting other keys

::alert[Team budgets define an upper spending boundary. In this workshop, the setup script intentionally creates each virtual key with a budget matching its team's total budget — but in a real platform, you can scope multiple keys to a single team, where their individual budgets roll up to the team limit.]{type="info"}

## 3.1 Run the setup script

The `setup_keys.py` script creates teams and virtual keys automatically. First, return to `/workshop` and set up a Python virtual environment:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop
# Use Python 3.13 explicitly. The workshop tools (strands-agents) require
# Python >= 3.10, and Amazon Linux 2023 ships 3.9 as the default `python3`,
# so name the interpreter directly rather than relying on a shell alias.
PYBIN=$(command -v python3.13 || command -v python3)
"$PYBIN" -m venv .venv
source .venv/bin/activate
:::

Then install dependencies and run the script:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/source/module-2-llm-gateway
pip install --upgrade pip==24.0 --quiet
pip install -r requirements.txt --quiet
python scripts/setup_keys.py --stack-name workshop-llm-gateway-stack
:::

::alert[Model registration is idempotent — re-running the script re-registers the same models without error. Team and key creation are **not**: re-running creates **additional** teams and brand-new virtual keys. If a run fails partway through, you can simply re-run and use the most recently printed keys (the earlier ones remain but are harmless in the sandbox).]{type="info"}

This script will:
1. Read the proxy URL and admin key from CloudFormation / Secrets Manager
2. Register 23 of the most commonly used Bedrock models in LiteLLM's database (Claude, Nova, Llama, Mistral, Cohere, and DeepSeek) — the gateway config supports 65+ models, but the script registers a curated subset with friendly aliases
3. Create two teams: `platform-team` ($10 budget) and `workload-team` ($5 budget)
4. Create virtual keys for each team
5. Test a chat completion with the virtual key

On success, the script prints a summary showing the proxy URL, two virtual keys, and the admin key — plus the `export` commands to copy into your terminal for the next step:

:::code{language=text showCopyAction=false showLineNumbers=false}
=======================================================
  LLM Gateway — Set Up Models, Keys & Teams
=======================================================

[1/6] Reading CloudFormation stack outputs...
  Proxy URL:   https://xxxxxxxxxx.execute-api.<region>.amazonaws.com
  Admin Key:  (retrieved from Secrets Manager)
  Cognito Pool: <region>_XXXXXXXXX (from workshop-CognitoUserPoolId export)

[2/6] Checking proxy health...
  Proxy is healthy!

[3/6] Registering 23 Bedrock models...
  Registered 23/23 models successfully.

[4/6] Creating workshop teams...
  Created team 'platform-team' (id=xxxxxxxx-xxxx..., budget=$10.0)
  Created team 'workload-team' (id=xxxxxxxx-xxxx..., budget=$5.0)

[5/6] Creating virtual keys...
  Created key 'workshop-admin-key' = sk-xxxxxxxx... (budget=$10.0) → Cognito 'admins'
  Created key 'workshop-dev-key'   = sk-xxxxxxxx... (budget=$5.0) → Cognito 'developers'

[6/6] Testing chat completion with virtual key...
  Model response: Hello there, how are you?

  Bedrock is working through the LiteLLM Proxy!

=======================================================
  Setup complete!
=======================================================

  Export these for use in other scripts and the notebook:

    export LLM_GATEWAY_URL=https://xxxxxxxxxx.execute-api.<region>.amazonaws.com
    export LLM_GATEWAY_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxx
    export LLM_GATEWAY_ADMIN_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

  Identity mapping (virtual key → Cognito group):
    workshop-admin-key → Cognito group 'admins'
    workshop-dev-key   → Cognito group 'developers'
    Cognito User Pool: <region>_XXXXXXXXX

  This means LLM spend is attributable to Cognito identities
  across both the LLM Gateway and the Tools Gateway.
:::

## 3.2 Export environment variables

The script prints export commands at the end. **Copy the three `export` lines from your terminal output** (they will look like the template below, with real values substituted) — you'll need them for the rest of this module:

:::code{showCopyAction=false showLineNumbers=false language=text}
export LLM_GATEWAY_URL=<proxy-url-from-script>
export LLM_GATEWAY_API_KEY=<your-virtual-key-from-script>
export LLM_GATEWAY_ADMIN_KEY=<your-admin-key-from-script>
:::

::alert[The block above is a template, not a copyable command. Copy the three real `export` lines the script printed (they include a full HTTPS URL and `sk-...` key values) and paste them into your terminal.]{type="info"}

::alert[The `LLM_GATEWAY_URL` is now an HTTPS API Gateway endpoint (e.g. `https://xxxxxxxxxx.execute-api.<region>.amazonaws.com`). The ALB is internal and not publicly accessible — all traffic flows through API Gateway, which provides TLS encryption automatically.]{type="info"}

Store the gateway URL and virtual key in SSM Parameter Store so Module 4 (agent) can retrieve them at runtime:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

aws ssm put-parameter \
  --name "/workshop/llm-gateway-url" \
  --value "$LLM_GATEWAY_URL" \
  --type String --overwrite --region $REGION

aws ssm put-parameter \
  --name "/workshop/llm-gateway-key" \
  --value "$LLM_GATEWAY_API_KEY" \
  --type String --overwrite --region $REGION

echo "Stored in SSM: /workshop/llm-gateway-url and /workshop/llm-gateway-key"
:::

## 3.3 Verify key restrictions

The `workshop-dev-key` was created with an **allowlist** of `claude-sonnet`, `claude-haiku`, and `nova-2-lite` — `llama3.3-70b` was deliberately excluded even though the gateway has it registered. Call `llama3.3-70b` with the dev key first and confirm the gateway rejects it:

You should **expect an `HTTP 403` response with `"type": "key_model_access_denied"`** in the body — the dev key is not allowed to call llama3.3-70b, so the gateway returns an access-denied error instead of a model response:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Negative test — llama3.3-70b is registered on the gateway but NOT on
# the dev key's allowlist. Expect HTTP 403 with "key_model_access_denied".
curl -s -w "\nHTTP %{http_code}\n" "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "llama3.3-70b", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 10}'
:::

Now confirm the same key works for a model it *is* allowed to call:

:::code{showCopyAction=true showLineNumbers=false language=bash}
# Positive test — claude-sonnet IS on the dev key's allowlist.
curl -s "${LLM_GATEWAY_URL}/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LLM_GATEWAY_API_KEY}" \
  -d '{"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 10}' \
  | python -m json.tool
:::

## 3.4 Check key info and spend

View the current spend for a key:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/key/info" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  -G --data-urlencode "key=${LLM_GATEWAY_API_KEY}" \
  | python -m json.tool
:::

You'll see fields like `spend`, `max_budget`, `models`, and `team_id`.

::alert[Virtual keys are the foundation of the platform's cost governance model. In Module 4, each agent will get its own virtual key — the platform tracks every token and dollar spent per agent.]{type="info"}

## 3.5 Identity Mapping: Connecting Keys to Cognito

::alert[This section requires the Module 3a Registry stack to be deployed (it creates the Cognito User Pool). If you see `"metadata": {}` on your keys, that is expected — re-run `setup_keys.py` after completing Module 3a to populate the identity mapping.]{type="warning"}

A centralized platform needs a **unified identity** across all its components. The MCP Gateway & Registry stack (pre-deployed in your workshop environment) created a **Cognito User Pool** as part of its data layer. The setup script automatically detects this pool and links each virtual key to a Cognito group via key metadata, so LLM spend can be correlated with Cognito identities.

::alert[The Cognito User Pool is created by the registry stack's data layer (`data-stack.yaml`), not a separate `platform-identity` stack. It is shared across all modules — Module 2 (LLM Gateway), Module 3a (MCP Registry + Tools Gateway), Module 3b (AgentCore Registry), and Module 4 (Agent Builder) all reference the same pool.]{type="info"}

```
Cognito User Pool (created by registry data-stack)
  │
  ├── Group: mcp-registry-admin → LiteLLM team: platform-team  → workshop-admin-key
  └── Group: developers         → LiteLLM team: workload-team  → workshop-dev-key
```

Verify the identity mapping on your virtual key:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/key/info" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  -G --data-urlencode "key=${LLM_GATEWAY_API_KEY}" \
  | python -m json.tool | grep -A5 metadata
:::

You should see:

```json
"metadata": {
    "cognito_group": "developers",
    "cognito_user_pool_id": "<your-pool-id>",
    "identity_provider": "cognito"
}
```

This means the platform can answer: **"The LLM call that cost $0.50 was made by a user in the 'developers' Cognito group"** — the same identity used for tool access in Module 3a/3b.

---

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-3-virtual-keys.ipynb`**. This notebook performs the same operations as the `setup_keys.py` script but breaks them into individual cells so you can inspect results at each stage:

1. **Load state** — Reads the gateway URL and admin key from `.state.json` (saved by the Step 2 notebook).
2. **Register Bedrock models** — Registers 23 models covering Anthropic Claude, Amazon Nova, Meta Llama, Mistral, Cohere, and DeepSeek families. Each model gets a friendly alias (e.g., `claude-sonnet`) mapped to its full Bedrock model ID.
3. **Create teams** — Creates `platform-team` ($10 budget) and `workload-team` ($5 budget) with scoped model access. Pay attention to how team budgets provide an outer spending boundary.
4. **Create virtual keys** — Issues `workshop-admin-key` and `workshop-dev-key`, each scoped to a team. Notice how the key budget and team budget work together for layered cost control.
5. **Test chat completion** — Sends a request through the gateway using the new virtual key to verify end-to-end connectivity.
6. **Query key info** — Inspects the key metadata including spend, budget, and allowed models.
7. **Save state** — Persists the API key for use in subsequent notebooks.

::alert[If you ran `setup_keys.py` in the CLI walkthrough, the notebook will create additional teams and keys. This is fine — you can use either set of keys for later steps.]{type="info"}
