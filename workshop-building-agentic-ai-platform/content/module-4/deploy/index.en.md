---
title: "Deploy FAST"
weight: 72
---

In this step you will clone the FAST repository, create a travel agent pattern, and deploy the full stack with CDK.

::alert[**About stack naming.** The platform foundations in Modules 2 and 3 use `Workshop-*-Stack` names because Workshop Studio provisions them for you from `contentspec.yaml`. Module 4 is different — you deploy FAST yourself from the IDE using CDK, which creates a stack called `FAST-stack` by convention from the FAST repository. Both naming schemes are intentional.]{type="info"}

## Clone the Repository

The IDE opens at `/workshop`, but previous steps may have left your terminal in a subdirectory. Return to `/workshop` first so the clone lands in the right place:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop
git clone https://github.com/awslabs/fullstack-solution-template-for-agentcore.git fast-agent
cd fast-agent
# Pin to the FAST release this workshop is built and tested against.
# Tracking main is not safe: upstream FAST evolves (e.g. a newer main builds an
# x86_64 CedarPolicyLambda that requires cross-arch Docker emulation on the
# arm64 IDE), which can break the deploy. This matches the notebook walkthrough.
git checkout v0.4.1
:::

Verify the clone succeeded and you are on the expected commit:

:::code{showCopyAction=true showLineNumbers=false language=bash}
git -C /workshop/fast-agent rev-parse --short HEAD && ls -d infra-cdk patterns
:::

Expected output: a short commit SHA on the first line, then `infra-cdk  patterns` on the second. If either directory is missing, re-run the clone.

## Create the Travel Agent Pattern

FAST ships with a generic `strands-single-agent` pattern. You will create a `strands-travel-agent` pattern with a travel-specific system prompt.

Copy the baseline pattern. The `patterns/` directory lives at the **FAST repo root**, so make sure you are in `/workshop/fast-agent` (not `infra-cdk/`) — the `cd` below guarantees it:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
cp -r patterns/strands-single-agent patterns/strands-travel-agent
mv patterns/strands-travel-agent/basic_agent.py patterns/strands-travel-agent/travel_agent.py
:::

Your new pattern directory:

```text
patterns/strands-travel-agent/
├── travel_agent.py          # Agent code (you'll replace this next)
├── Dockerfile               # Container build instructions
├── requirements.txt         # Python dependencies
└── tools/
    ├── gateway.py           # MCP client for AgentCore Gateway (OAuth2 via Token Vault)
    └── code_interpreter.py  # AgentCore Code Interpreter wrapper
```

Replace the agent code with the travel agent. This is the only file you need to write — everything else (gateway auth, memory, code interpreter) is inherited from FAST:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
cat > patterns/strands-travel-agent/travel_agent.py << 'PYEOF'
"""Travel Agent — Strands agent with Gateway MCP tools, Memory, and Code Interpreter."""

import json
import logging
import os

from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client
from utils.auth import extract_user_id_from_context
from utils.ssm import get_ssm_parameter
from tools.code_interpreter import StrandsCodeInterpreterTools

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """\
You are a helpful travel planning assistant. You have access to the following
tools through an AgentCore Gateway:

Flight tools:
- search_flights(origin, destination, date, max_results?) – search flights by route and date
- get_flight_details(flight_id) – get full details for a specific flight
- search_flights_by_budget(origin, destination, date, max_price) – search within a budget

Hotel tools:
- search_hotels(city, checkin_date?, checkout_date?, guests?, max_results?) – search hotels by city
- get_hotel_details(hotel_id) – get full details for a specific hotel
- search_hotels_by_budget(city, checkin_date, checkout_date, max_price_per_night) – search within a budget

You also have access to a Code Interpreter for calculations and data analysis.

When planning a trip:
1. Parse the user's request for origin, destination, dates, budget, and preferences.
2. Search for flights matching the route and dates.
3. Search for hotels in the destination city for the stay dates.
4. If a budget is provided, use the budget-filtered search tools.
5. Compare options and recommend the best combination of flight + hotel.
6. Present a clear, structured itinerary with prices and key details.

Use IATA airport codes (e.g. LAX, LHR, SFO, TYO) for flight searches.
Use city names (e.g. London, Tokyo, Paris) for hotel searches.
Dates should be in YYYY-MM-DD format.
"""


def _create_model():
    """Create the model — LiteLLM Gateway if configured, else direct Bedrock.

    Model ID matches the one Module 2 step-2 primes in a fresh Workshop Studio account
    (Claude Sonnet 4.6). The `global.` cross-region inference profile is used so the agent
    works in any deploy region without per-region geo-prefix derivation. Using a different
    Anthropic model here would hit a Bedrock Marketplace subscription gate the first time
    the agent calls it.
    """
    stack_name = os.environ.get("STACK_NAME", "")
    try:
        gateway_url = get_ssm_parameter(f"/{stack_name}/llm_gateway_url")
        gateway_key = get_ssm_parameter(f"/{stack_name}/llm_gateway_key")
        if gateway_url and gateway_key:
            from strands.models.litellm import LiteLLMModel
            logger.info("[MODEL] Using LiteLLM Gateway: %s", gateway_url)
            return LiteLLMModel(
                model_id="bedrock/global.anthropic.claude-sonnet-4-6",
                api_base=gateway_url,
                api_key=gateway_key,
            )
    except Exception as e:
        logger.info("[MODEL] LLM Gateway not configured (%s), using direct Bedrock", e)
    return BedrockModel(
        model_id="global.anthropic.claude-sonnet-4-6", temperature=0.1
    )


def _create_session_manager(user_id, session_id):
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")
    config = AgentCoreMemoryConfig(
        memory_id=memory_id, session_id=session_id, actor_id=user_id
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
    )


def create_travel_agent(user_id, session_id):
    model = _create_model()
    session_manager = _create_session_manager(user_id, session_id)
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    code_tools = StrandsCodeInterpreterTools(region)
    gateway_client = create_gateway_mcp_client()
    return Agent(
        name="travel_agent",
        system_prompt=SYSTEM_PROMPT,
        tools=[gateway_client, code_tools.execute_python_securely],
        model=model,
        session_manager=session_manager,
        trace_attributes={"user.id": user_id, "session.id": session_id},
    )


@app.entrypoint
async def invocations(payload, context: RequestContext):
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")
    if not all([user_query, session_id]):
        yield {"status": "error", "error": "Missing required fields: prompt or runtimeSessionId"}
        return
    try:
        user_id = extract_user_id_from_context(context)
        agent = create_travel_agent(user_id, session_id)
        async for event in agent.stream_async(user_query):
            yield json.loads(json.dumps(dict(event), default=str))
    except Exception as e:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(e)}

if __name__ == "__main__":
    app.run()
PYEOF
:::

Verify the file was created correctly:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
python3 -c "import ast; ast.parse(open('patterns/strands-travel-agent/travel_agent.py').read()); print('✓ Syntax OK')"
:::

Let's break down the key parts of this agent (no code to run here — this is a walkthrough of the file you just wrote):

**Model factory — `_create_model`.** The function looks up two SSM Parameter Store values under `/FAST-stack/`: the LLM Gateway URL and a virtual key. If both are present, the agent's model client is wired to the LLM Gateway (Module 2), so every chat completion is governed — budget-tracked, guardrail-filtered, and attributed to the virtual key's Cognito group. If either parameter is missing, the factory falls back to calling Amazon Bedrock directly. You will write those two SSM parameters in the next step, flipping the agent onto the gateway.

**Agent assembly.** The Strands `Agent` is constructed with two tool sources. First, an MCP client returned by `create_gateway_mcp_client()` — this is the live connection to the AgentCore Gateway, which exposes the flight and hotel tools synced from Module 3a's Registry. Second, a Code Interpreter wrapper that lets the agent run small Python snippets for calculations. Tool discovery is dynamic: at startup the agent calls `tools/list` on the gateway, so any tool added to the Registry afterwards becomes available on the next restart without changing agent code.

## Update the Dockerfile

The Dockerfile needs to reference `travel_agent.py` instead of `basic_agent.py`:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
sed -i.bak 's|strands-single-agent|strands-travel-agent|g; s|basic_agent|travel_agent|g' \
  patterns/strands-travel-agent/Dockerfile
rm patterns/strands-travel-agent/Dockerfile.bak
:::

## Add LiteLLM Dependency

Add the `litellm` package to the travel agent's requirements:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
# Pinned for supply-chain reproducibility. litellm==1.83.0 matches the Module 2
# gateway version; strands-agents==1.32.0 matches the version FAST v0.4.1 already
# pins in this pattern's requirements.txt (the [litellm] extra adds no version drift).
echo "litellm==1.83.0" >> patterns/strands-travel-agent/requirements.txt
echo "strands-agents[litellm]==1.32.0" >> patterns/strands-travel-agent/requirements.txt
:::

## Configure FAST

Update `config.yaml` to use the travel agent pattern:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
cat > infra-cdk/config.yaml << 'EOF'
stack_name_base: FAST-stack
admin_user_email:

backend:
  pattern: strands-travel-agent
  deployment_type: docker
  network_mode: PUBLIC
EOF
:::

::alert[This workshop uses `PUBLIC` network mode for simplicity. In production, set `network_mode: VPC` and deploy the AgentCore Runtime into private subnets with VPC endpoints. See the [FAST Deployment Guide](https://github.com/awslabs/fullstack-solution-template-for-agentcore/blob/main/docs/DEPLOYMENT.md) for VPC configuration details.]{type="warning"}

## Bootstrap and Deploy

Install CDK dependencies:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent/infra-cdk
# `npm ci` installs the exact versions from FAST v0.4.1's committed
# package-lock.json (reproducible, supply-chain-safe) instead of
# re-resolving ranges like `npm install` would.
npm ci
:::

Verify `node_modules/` was populated and the CDK CLI is on PATH:

:::code{showCopyAction=true showLineNumbers=false language=bash}
test -d node_modules && npx cdk --version
:::

Expected: a single line like `2.x.y (build ...)`. If `node_modules/` is missing or `cdk --version` errors out, re-run `npm ci`.

Bootstrap CDK in your account (only needed once per account/region). Use `npx cdk` so the deploy uses the project-local CDK CLI (matching `aws-cdk-lib` in `node_modules/`) rather than any older globally-installed `cdk`:

:::code{showCopyAction=true showLineNumbers=false language=bash}
npx cdk bootstrap
:::

Verify the bootstrap stack reached `CREATE_COMPLETE` (or already existed):

:::code{showCopyAction=true showLineNumbers=false language=bash}
aws cloudformation describe-stacks --stack-name CDKToolkit \
  --query "Stacks[0].StackStatus" --output text --region $(aws configure get region)
:::

Expected output: `CREATE_COMPLETE` or `UPDATE_COMPLETE`. If the command errors with `Stack with id CDKToolkit does not exist`, the bootstrap did not complete — re-run `npx cdk bootstrap`.

Authenticate Docker with ECR (required to push the agent container):

:::code{showCopyAction=true showLineNumbers=false language=bash}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region)
echo '{}' > ~/.docker/config.json
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
:::

You should see `Login Succeeded` on the last line. If instead you see `Error saving credentials` or a permission error, confirm the Docker daemon is running (`docker info`) and retry.

::alert[If using Finch instead of Docker, run `export CDK_DOCKER=finch` in your terminal before deploying.]{type="info"}

Deploy the stack:

:::code{showCopyAction=true showLineNumbers=false language=bash}
npx cdk deploy --require-approval never
:::

::alert[The deployment takes approximately 5–10 minutes. CDK builds the agent container, pushes it to ECR, creates the Cognito User Pool, provisions AgentCore Runtime, Memory, and Gateway.]{type="info"}

You should see output ending with:

```text
 ✅ FAST-stack

Outputs:
FAST-stack.AmplifyUrl = https://main.xxxxxxxxxx.amplifyapp.com
FAST-stack.CognitoUserPoolId = us-west-2_xxxxxxxxx
FAST-stack.RuntimeArn = arn:aws:bedrock-agentcore:...
...
```

## Deploy the Frontend

Return to the repository root and deploy the React frontend to Amplify:

:::code{showCopyAction=true showLineNumbers=false language=bash}
cd /workshop/fast-agent
python3 scripts/deploy-frontend.py
:::

The output will include the application URL:

```text
ℹ App URL: https://main.xxxxxxxxxx.amplifyapp.com
```

Note the App URL — you will open this in your browser after creating a user.

## Create a Cognito User

Create a user so you can log in to the frontend:

:::code{showCopyAction=true showLineNumbers=false language=bash}
REGION=$(aws configure get region)

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name FAST-stack \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text --region $REGION)

# Generate a random temporary password that satisfies Cognito's default policy
# (>=8 chars, upper + lower + number + symbol). You will rotate it on first
# login — this just avoids shipping a well-known hardcoded value.
# Single-quote the literal suffix so bash does not try history-expand `!`.
TEMP_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=')"'!Aa1'

aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username workshop@example.com \
  --temporary-password "$TEMP_PASSWORD" \
  --user-attributes Name=email,Value=workshop@example.com Name=email_verified,Value=true \
  --region $REGION

echo ""
echo "Temporary password set. You will change it on first login."
:::

You will use these credentials to log in:

| Field | Value |
|-------|-------|
| Email | `workshop@example.com` |
| Temporary password | printed above — copy it from the command output |

::alert[You will be prompted to set a new password on first login.]{type="info"}

## Test the Baseline

Open the Amplify URL in your browser and log in. Send a test message:

> Search for flights from SFO to Tokyo for 2026-09-15

The agent will attempt to call `search_flights` but **fail** — it will either return an error or fall back to the Code Interpreter. This proves the travel tools are listed in the system prompt but not actually connected to the gateway yet.

You will wire them in the **Connect to Tools Gateway** step.

::alert[If the agent does not respond at all, check the AgentCore Runtime logs in CloudWatch under `/aws/bedrock-agentcore/runtimes/`.]{type="warning"}

## Notebook Walkthrough (Optional alternative)

> This notebook covers the same material as the CLI section above — follow *either* path, you do not need to do both.
>
> **How to run it:** open the notebook from the path below, then execute every cell top-to-bottom (click the cell and press `Shift+Enter`, or use the *Run All* button).
>
> **Kernel:** when VS Code prompts, pick **`Python 3 (workshop)`** from the kernel picker. If you see `ModuleNotFoundError`, the wrong kernel is selected — switch it from the kernel name in the top-right.
>
> Navigate to `source/module-4b-fast/notebooks/` and open **`02-deploy-fast.ipynb`**.
