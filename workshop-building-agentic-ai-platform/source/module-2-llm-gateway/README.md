# Module 2: LLM Gateway (LiteLLM proxy)

Enterprise LLM Gateway deployed on AWS ECS Fargate using [LiteLLM Proxy](https://docs.litellm.ai/), providing governed, unified access to Amazon Bedrock foundation models with virtual keys, spend tracking, Bedrock Guardrails, and native Strands Agents integration.

## Architecture

- **API Gateway HTTP API** — HTTPS front door (public endpoint)
- **Internal ALB** — routes to LiteLLM via VPC Link (private, not publicly accessible)
- **LiteLLM Proxy** (`litellm-database:v1.84.0`, pinned for supply-chain reproducibility) on ECS Fargate — port 4000
- **PostgreSQL 16.7 sidecar** (Debian, pinned tag) for virtual keys, teams, and spend tracking
- **EFS** for PostgreSQL data persistence
- **IAM Task Role** for Amazon Bedrock + Guardrails access (no API keys needed)
- **Secrets Manager** for auto-generated admin key and database password
- **CloudFormation** for infrastructure provisioning

## Quick start

### Deploy

```bash
cd source/module-2-llm-gateway

# Deploy the stack
bash scripts/deploy.sh workshop-llm-gateway-stack

# Wait for LiteLLM to be healthy
bash scripts/wait_for_ready.sh workshop-llm-gateway-stack

# Create teams and virtual keys
pip install -r requirements.txt
python scripts/setup_keys.py --stack-name workshop-llm-gateway-stack
```

### Test

```bash
export LLM_GATEWAY_URL=<proxy-url-from-outputs>
export LLM_GATEWAY_API_KEY=<virtual-key-from-setup-script>

python scripts/test_gateway.py
```

### Strands agent integration

```python
from strands import Agent
from strands.models.litellm import LiteLLMModel

model = LiteLLMModel(
    # Connection settings belong in client_args (splatted into
    # litellm.acompletion); params is for inference parameters only.
    client_args={
        "api_base": os.environ["LLM_GATEWAY_URL"],  # HTTPS API Gateway endpoint
        "api_key": "<virtual-key>",
    },
    model_id="openai/claude-sonnet",  # "openai/" = OpenAI-compatible proxy path
)

agent = Agent(model=model)
result = agent("Analyse this and create a report.")
```

### Cleanup

```bash
bash scripts/destroy.sh workshop-llm-gateway-stack
```

## Directory structure

```
├── reference/
│   └── litellm-config.yaml        # LiteLLM model catalog, for reference only —
│                                  # the live catalog is registered by scripts/setup_keys.py
│   # Note: the CloudFormation template lives at
│   # static/cfn/llm-gateway/workshop-llm-gateway-stack.yaml (single source of truth)
├── scripts/
│   ├── deploy.sh                   # Deploy wrapper
│   ├── destroy.sh                  # Teardown wrapper
│   ├── wait_for_ready.sh           # Health check poller
│   ├── setup_keys.py               # Create teams + virtual keys
│   ├── create_api_key.py           # Create a single virtual key
│   └── test_gateway.py             # 8-step test suite
├── llm_gateway_client/
│   ├── __init__.py
│   ├── client.py                   # Python client (LiteLLM proxy API)
│   └── models.py                   # Pydantic response models
├── tests/
│   ├── conftest.py
│   └── unit/
│       ├── test_gateway_client.py  # 19 client tests
│       ├── test_cfn_template.py    # 40 template tests
│       └── test_bedrock_region.py  # 18 region/model-resolution tests
├── notebooks/                      # Step-by-step notebooks (one per module step)
│   ├── step-1-architecture.ipynb
│   ├── step-2-deploy.ipynb
│   ├── step-3-virtual-keys.ipynb
│   ├── step-4-test-models.ipynb
│   ├── step-5-guardrails.ipynb
│   ├── step-6-spend-tracking.ipynb
│   └── step-7-cleanup.ipynb
├── walkthrough.ipynb               # Jupyter notebook walkthrough (end-to-end)
├── requirements.txt
└── requirements-dev.txt
```

## Python client

```python
from llm_gateway_client import LLMGatewayClient

client = LLMGatewayClient(
    proxy_url=os.environ["LLM_GATEWAY_URL"],  # HTTPS API Gateway endpoint
    api_key="<virtual-key>"
)

# Chat completion
response = client.chat("Hello!", model="claude-sonnet")

# List models
models = client.list_models()

# Create virtual key (requires admin key)
key = client.create_key(models=["claude-sonnet"], max_budget=5.0)

# View spend
logs = client.get_spend_logs()
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

77 tests (19 client + 40 template + 18 region resolution) covering the Python
client, the CloudFormation template structure, and per-region model resolution.
The counts are what `pytest --collect-only -q` reports, so they include
parametrized cases and are higher than a `def test_` grep.

## Available models

Two catalogs, and they are deliberately different sizes.

**What the deployed gateway serves (17 aliases).** `scripts/setup_keys.py` registers these through
`/model/new`; the proxy runs with `STORE_MODEL_IN_DB=True` and no mounted config file, so this table
*is* the live catalog. Each entry is resolved against the deploy region at registration time and
skipped if the region has no invocable flavor.

| Provider | Aliases |
|----------|---------|
| Anthropic Claude | `claude-opus`, `claude-sonnet`, `claude-haiku`, `claude-opus-4.6`, `claude-sonnet-4.6`, `claude-opus-4.5`, `claude-sonnet-4.5`, `claude-haiku-4.5` |
| Amazon Nova | `nova-pro`, `nova-lite`, `nova-2-lite` |
| Meta Llama | `llama3.3-70b`, `llama3.1-70b` |
| Mistral AI | `mistral-large-3`, `mistral-large` |
| DeepSeek | `deepseek-r1`, `deepseek-v3` |

**The wider reference catalog (55 aliases).** `reference/litellm-config.yaml` is documentation only —
it is not mounted into the container. Use it as a menu when you want to add models to the table above.

| Provider | Models |
|----------|--------|
| Anthropic Claude | Opus 4.6, Sonnet 4.6, Opus 4.5, Sonnet 4.5, Haiku 4.5 (plus the `claude-opus`/`claude-sonnet`/`claude-haiku` rolling aliases) |
| Amazon Nova | Pro, Lite, 2 Lite, 2 Sonic |
| Meta Llama | 4 Scout, 4 Maverick, 3.3 70B, 3.2 (90B/11B/3B/1B), 3.1 (70B/8B), 3 (70B/8B) |
| Mistral AI | Large 3, Devstral 2, Magistral Small, Ministral (14B/8B/3B), Large, Mixtral 8x7B, 7B |
| DeepSeek | R1, v3, v3.2 |
| Writer | Palmyra X5, X4 |
| Google | Gemma 3 (27B/12B/4B) |
| NVIDIA | Nemotron Nano (30B/12B/9B) |
| Qwen | Qwen3 Coder 480B, 235B, 32B, Coder 30B |
| MiniMax | M2.1, M2 |
| Moonshot | Kimi K2 Thinking, K2.5 |
| Zhipu AI | GLM 4.7, GLM 4.7 Flash |
| OpenAI (OSS) | GPT OSS 120B, 20B |

Cohere Command R and Command R+ are absent from both catalogs on purpose: they reach end of life on
Bedrock on 2026-08-19, so registering either would hand participants an endpoint that stops working
during the workshop's lifetime. Amazon Titan Text and AI21 Jamba are absent for the same reason —
check the [Bedrock model lifecycle page](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html)
before adding any model to either table.
