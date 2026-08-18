---
title: "Virtual Keys & Teams"
weight: 33
---

The LLM Gateway is running with an admin key, but in an enterprise solution you don't give every agent and team the admin key. Instead, you create **teams** with budgets and issue **virtual keys** that are scoped to those teams.

::alert[This step provides both a CLI walkthrough and a Jupyter notebook walkthrough. You can follow either approach — both achieve the same result.]{type="info"}

## CLI walkthrough

As the platform engineer, you will now create teams with budgets and issue virtual keys — this is how you control which agents can access which models and how much they can spend.

## How virtual keys work

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
- Is scoped to specific models (e.g., only `claude-sonnet` and `nova-2-lite`)
- Has a max budget — requests are rejected when the budget is exhausted
- Tracks spend independently — per-key cost attribution
- Can be revoked instantly without affecting other keys

::alert[Team budgets define an upper spending boundary. In this workshop, the setup script intentionally creates each virtual key with a budget matching its team's total budget — but in a real platform, you can scope multiple keys to a single team, where their individual budgets roll up to the team limit.]{type="info"}

## 3.1 Run the setup script

The `setup_keys.py` script creates teams and virtual keys automatically. First, return to `/workshop` and set up a Python virtual environment:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop
# `requirements.txt` pins strands-agents, which requires Python >= 3.10. The IDE's
# default `python3` is 3.9, and building the venv with it makes the installs in the
# next block resolve to "No matching distribution found" — after which
# setup_keys.py dies with "ModuleNotFoundError: No module named 'boto3'".
PY=""
for CAND in python3.13 python3.12 python3.11 python3.10; do
  command -v "$CAND" >/dev/null 2>&1 && { PY="$CAND"; break; }
done
[ -z "$PY" ] && PY=python3
echo "Building .venv with $PY ($($PY -V 2>&1))"
$PY -m venv --clear .venv
source .venv/bin/activate
python -V
:::

Then install dependencies and run the script:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/source/module-2-llm-gateway
pip install --upgrade pip==24.0 --quiet
pip install -r requirements.txt --quiet
python3 scripts/setup_keys.py --stack-name workshop-llm-gateway-stack
:::

::alert[Model registration is idempotent — re-running the script re-registers the same models without error. Team and key creation are **not**: re-running creates **additional** teams and brand-new virtual keys. If a run fails partway through, you can simply re-run and use the most recently printed keys (the earlier ones remain but are harmless in the sandbox).]{type="info"}

This script will:
1. Read the proxy URL and admin key from CloudFormation / Secrets Manager
2. Register 17 of the most commonly used Bedrock models in LiteLLM's database (Claude, Nova, Llama, Mistral, and DeepSeek) — `reference/litellm-config.yaml` lists 55 aliases you could add, but the script registers this curated subset with friendly aliases
3. Create two teams: `platform-team` ($10 budget) and `workload-team` ($5 budget)
4. Create virtual keys for each team
5. Test a chat completion with the virtual key

On success, the script prints a summary showing the proxy URL and the two keys it created (masked — the full values go to a `0600` file you source in the next step):

:::code{language=text showCopyAction=false showLineNumbers=false}
=======================================================
  LLM Gateway — Set Up Models, Keys & Teams
=======================================================

[1/6] Reading CloudFormation stack outputs...
  Proxy URL:   https://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com
  Admin Key:  (retrieved from Secrets Manager)
  Cognito Pool: us-west-2_XXXXXXXXX (from workshop-CognitoUserPoolId export)

[2/6] Checking proxy health...
  Proxy is healthy!

[3/6] Registering 17 Bedrock models...
  Registered 17/17 models successfully.

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

  Two credentials are now set up for the rest of this module.
  LLM_GATEWAY_API_KEY is the 'workshop-dev-key' virtual key —
  the scoped, developer-facing key the workshop uses for all
  chat calls. LLM_GATEWAY_ADMIN_KEY is the administrative key
  used for spend + key management endpoints only.

  Load them into any terminal with:
    source /workshop/.llm-gateway-env

    LLM_GATEWAY_URL        https://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com
    LLM_GATEWAY_API_KEY    sk-xxxx...xxxx   # = workshop-dev-key
    LLM_GATEWAY_ADMIN_KEY  sk-xxxx...xxxx   # administrative key

  /workshop/.llm-gateway-env is mode 0600 and holds the full values.

  Identity mapping (virtual key → Cognito group):
    workshop-admin-key → Cognito group 'admins'
    workshop-dev-key   → Cognito group 'developers'
    Cognito User Pool: us-west-2_XXXXXXXXX

  This means LLM spend is attributable to Cognito identities
  across both the LLM Gateway and the Tools Gateway.
:::

## 3.2 Load the environment variables

The script wrote the three values to `/workshop/.llm-gateway-env` (mode `0600`) instead of printing the keys in full. **Load them by sourcing that file** — you'll need them for the rest of this module:

:::code{showCopyAction=true showLineNumbers=false language=bash}
source /workshop/.llm-gateway-env
echo "URL:       $LLM_GATEWAY_URL"
echo "Dev key:   ${LLM_GATEWAY_API_KEY:0:7}...${LLM_GATEWAY_API_KEY: -4}"
echo "Admin key: ${LLM_GATEWAY_ADMIN_KEY:0:7}...${LLM_GATEWAY_ADMIN_KEY: -4}"
:::

::alert[Sourcing the file — rather than copy-pasting `sk-...` values off the screen — keeps live API keys out of your terminal scrollback and shell history. It is also the more reliable flow: environment variables live only in the shell you set them in, so **if you open a new terminal at any point in this module, run the `source` command again** before continuing.]{type="info"}

::alert[The `LLM_GATEWAY_URL` is now an HTTPS API Gateway endpoint (e.g. `https://xxxxxxxxxx.execute-api.<region>.amazonaws.com`). The ALB is internal and not publicly accessible — all traffic flows through API Gateway, which provides TLS encryption automatically.]{type="info"}

Store the gateway URL and virtual key in SSM Parameter Store so Module 4 (agent) can retrieve them at runtime:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

# Guard against writing empty parameters: if the exports were never pasted in,
# `put-parameter` fails with a ValidationException that is easy to scroll past,
# and Module 4 then reads back a blank key.
if [ -z "$LLM_GATEWAY_URL" ] || [ -z "$LLM_GATEWAY_API_KEY" ]; then
  echo "ERROR: LLM_GATEWAY_URL / LLM_GATEWAY_API_KEY are not set." >&2
  echo "  Run: source /workshop/.llm-gateway-env" >&2
else
  aws ssm put-parameter \
    --name "/workshop/llm-gateway-url" \
    --value "$LLM_GATEWAY_URL" \
    --type String --overwrite --region $REGION

  aws ssm put-parameter \
    --name "/workshop/llm-gateway-key" \
    --value "$LLM_GATEWAY_API_KEY" \
    --type String --overwrite --region $REGION

  echo "Stored in SSM: /workshop/llm-gateway-url and /workshop/llm-gateway-key"
fi
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
  | python3 -m json.tool
:::

## 3.4 Check key info and spend

View the current spend for a key:

:::code{showCopyAction=true showLineNumbers=false language=bash}
curl -s "${LLM_GATEWAY_URL}/key/info" \
  -H "Authorization: Bearer ${LLM_GATEWAY_ADMIN_KEY}" \
  -G --data-urlencode "key=${LLM_GATEWAY_API_KEY}" \
  | python3 -m json.tool
:::

You'll see fields like `spend`, `max_budget`, `models`, and `team_id`.

::alert[Virtual keys are the foundation of the platform's cost governance model. In Module 4, each agent will get its own virtual key — the platform tracks every token and dollar spent per agent.]{type="info"}

## 3.5 Identity mapping: connecting keys to Cognito

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
  | python3 -m json.tool | grep -A5 metadata
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

## Notebook walkthrough (optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-2-llm-gateway/notebooks/` and open the corresponding notebook.

Open **`step-3-virtual-keys.ipynb`**. This notebook performs the same operations as the `setup_keys.py` script but breaks them into individual cells so you can inspect results at each stage:

1. **Load state** — Reads the gateway URL and admin key from `.state.json` (saved by the Step 2 notebook).
2. **Register Bedrock models** — Registers 9 models covering the Anthropic Claude, Amazon Nova, and DeepSeek families — a smaller set than the 17 `setup_keys.py` registers, because the notebook keeps the cell readable. Each model gets a friendly alias (e.g., `claude-sonnet`) mapped to its full Bedrock model ID.
3. **Create teams** — Creates `platform-team` ($10 budget) and `workload-team` ($5 budget) with scoped model access. Pay attention to how team budgets provide an outer spending boundary.
4. **Create virtual keys** — Issues `workshop-admin-key` and `workshop-dev-key`, each scoped to a team. Notice how the key budget and team budget work together for layered cost control.
5. **Test chat completion** — Sends a request through the gateway using the new virtual key to verify end-to-end connectivity.
6. **Query key info** — Inspects the key metadata including spend, budget, and allowed models.
7. **Save state** — Persists the API key for use in subsequent notebooks.

::alert[If you ran `setup_keys.py` in the CLI walkthrough, the notebook will create additional teams and keys. This is fine — you can use either set of keys for later steps.]{type="info"}
