# Module 2: LLM Gateway (LiteLLM Proxy)

Enterprise LLM Gateway deployed on AWS ECS Fargate using [LiteLLM Proxy](https://docs.litellm.ai/), providing governed, unified access to 70+ Amazon Bedrock foundation models with virtual keys, spend tracking, Bedrock Guardrails, and native Strands Agents integration.

## Architecture

- **API Gateway HTTP API** — HTTPS front door (public endpoint)
- **Internal ALB** — routes to LiteLLM via VPC Link (private, not publicly accessible)
- **LiteLLM Proxy** (`litellm-database:v1.83.3-stable`, pinned for supply-chain reproducibility) on ECS Fargate — port 4000
- **PostgreSQL 16.6 sidecar** (Debian, pinned tag) for virtual keys, teams, and spend tracking
- **EFS** for PostgreSQL data persistence
- **IAM Task Role** for Amazon Bedrock + Guardrails access (no API keys needed)
- **Secrets Manager** for auto-generated admin key and database password
- **CloudFormation** for infrastructure provisioning

## Quick Start

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

### Strands Agent Integration

```python
from strands import Agent
from strands.models.litellm import LiteLLMModel

model = LiteLLMModel(
    model_id="claude-sonnet",
    api_base=os.environ["LLM_GATEWAY_URL"],  # HTTPS API Gateway endpoint
    api_key="<virtual-key>",
)

agent = Agent(model=model)
result = agent("Analyse this and create a report.")
```

### Cleanup

```bash
bash scripts/destroy.sh workshop-llm-gateway-stack
```

## Directory Structure

```
├── cfn/
│   └── litellm-config.yaml        # LiteLLM proxy config (70+ models)
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
│       ├── test_gateway_client.py  # 17 client tests
│       └── test_cfn_template.py    # 40 template tests
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

## Python Client

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

## Running Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

59 tests covering the Python client and CloudFormation template structure.

## Available Models (70+)

| Provider | Models |
|----------|--------|
| Anthropic Claude | Opus 4.6, Sonnet 4.6, Opus 4.5, Sonnet 4.5, Haiku 4.5, Opus 4.1, Sonnet 4, 3.5 Haiku |
| Amazon Nova | Premier, Pro, Lite, Micro, Sonic, 2 Lite, 2 Sonic |
| Amazon Titan | Text Premier, Express, Lite |
| Meta Llama | 4 Scout, 4 Maverick, 3.3 70B, 3.2 (90B/11B/3B/1B), 3.1 (405B/70B/8B), 3 (70B/8B) |
| Mistral AI | Large 3, Devstral 2, Magistral Small, Ministral (14B/8B/3B), Large, Small, Mixtral 8x7B, 7B |
| Cohere | Command R+, Command R |
| AI21 Labs | Jamba 1.5 Large, Jamba 1.5 Mini |
| DeepSeek | R1, v3, v3.2 |
| Writer | Palmyra X5, X4 |
| Google | Gemma 3 (27B/12B/4B) |
| NVIDIA | Nemotron Nano (30B/12B/9B) |
| Qwen | Qwen3 Coder 480B, 235B, 32B, Coder 30B |
| MiniMax | M2.1, M2 |
| Moonshot | Kimi K2 Thinking, K2.5 |
| Zhipu AI | GLM 4.7, GLM 4.7 Flash |
| OpenAI (OSS) | GPT OSS 120B, 20B |
