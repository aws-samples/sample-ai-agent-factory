# Local Development & Testing

Running the platform locally, the full test-suite matrix, and the tech stack.

[← Back to README](../README.md)

## Quick start (Makefile)

```bash
make install   # backend + infra + frontend deps
make dev       # both dev servers (UI on :5173, API on :8000)
make test      # backend + infra + frontend test suites
make lint      # ruff + eslint
make typecheck # pyright + tsc
```

Run `make help` for the full target list.

## Local Development

For contributors who want to run the platform locally without deploying to AWS. The backend falls back to in-memory storage when `DYNAMODB_TABLE_NAME` is not set **and** the process is not running inside Lambda (detected via `AWS_LAMBDA_FUNCTION_NAME`). Inside Lambda the missing table env var raises `RuntimeError` at module load, so a misconfigured deploy fails to initialize rather than silently dropping writes.

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,deploy]"
cp .env.example .env
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend
npm install
cp .env.example .env
npm run dev
```

The UI opens at `http://localhost:5173`. The backend API runs at `http://localhost:8000`.

Note: Local mode uses in-memory storage (workflows are lost on restart) and requires AWS credentials for agent deployment features.

## Running Tests

### Unit and Property-Based Tests

```bash
# Backend (property-based tests with Hypothesis)
cd backend
pip install -e ".[dev]"
pytest
```

Property-based tests use Hypothesis with `@settings(max_examples=100)` to verify correctness properties across randomly generated inputs (workflow CRUD round-trips, serialization, validation consistency, IAM scoping, etc.).

### CDK Infrastructure Tests

```bash
cd infra
pip install -r requirements.txt
pytest tests/ -v
```

Verifies the synthesized CloudFormation template contains expected serverless resources (API Gateway, Lambda, Step Functions, DynamoDB) and does NOT contain removed resources (VPC, ECS, ALB, ECR, CodeBuild).

### Integration Tests

Integration tests perform real AWS API calls with zero mocking. They require:
- Valid AWS credentials with permissions for API Gateway, Lambda, Step Functions, DynamoDB, AgentCore, IAM, Cognito
- A deployed stack (run `./scripts/deploy.sh` first)
- Environment variables: `API_GATEWAY_URL` and `AWS_REGION`

```bash
cd backend

# Set required environment variables
export API_GATEWAY_URL="https://XXXXXXXXXX.execute-api.us-east-1.amazonaws.com"
export AWS_REGION="us-east-1"

# Run integration tests only
pytest -m integration -v

# Run a specific integration test
pytest -m integration tests/integration/test_deployment_lifecycle.py -v
pytest -m integration tests/integration/test_template_deployments.py -v
```

Integration tests deploy each of the 7 built-in templates, invoke the deployed runtimes, verify responses, and clean up all resources.

### Frontend Tests

```bash
cd frontend
npm install
npm test
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 19, @xyflow/react 12, Zustand 5, Tailwind CSS 4, Vite |
| Backend | FastAPI, Mangum, Pydantic 2, boto3 |
| Agent Framework | Strands Agents SDK (strands-agents, strands-agents-tools) |
| Agent Runtime | BedrockAgentCoreApp (bedrock-agentcore) |
| Model Providers | Bedrock (default), OpenAI, Anthropic, Gemini, Mistral, Ollama, Groq, DeepSeek, Together, LiteLLM, SageMaker, Writer, LlamaAPI |
| Multi-Agent | strands.multiagent (Graph, Swarm, Workflow patterns) |
| Orchestration | AWS Step Functions (Standard Workflows) |
| Testing | Pytest + Hypothesis (backend properties), Pytest + real AWS (integration), Vitest + fast-check (frontend), CDK assertions (infra) |
| Deployment Target | AWS Bedrock AgentCore (Runtime, Gateway, Knowledge Base, Memory, Evaluation, Policy, Browser, Identity, Observability) |
| Platform Infrastructure | AWS CDK (Python), API Gateway HTTP API, Lambda, Step Functions, DynamoDB, S3, CloudFront, SSM, Cognito |

## Future Enhancements

- **Additional tool types** -- Add tools like `code_executor`, `slack_notifier`, `s3_file_reader` to the dynamic tool registry.
- **Tool composition** -- Allow chaining tools as a single Gateway target with an orchestration Lambda.
- **Multi-target Gateway** -- Deploy different tools as separate Lambda targets for isolation and independent scaling.
- **Container deployments** -- Support deploying agents as containers.
- **Real-time logs** -- Stream CloudWatch logs from deployed agents into the test panel UI.
- **Versioned deployments** -- Deployment history per workflow with rollback support.
- **Collaborative editing** -- WebSocket-based real-time collaboration on the canvas.
- **Custom domain** -- Route 53 + ACM certificate support for custom domain names on CloudFront.
- **CI/CD pipeline** -- Automate deployments via CodePipeline or GitHub Actions on push to main.
- **Tool marketplace** -- Share and discover AI-generated tools across teams.
- **Multi-turn tool refinement** -- Iteratively refine AI-generated tools with conversation context in the Tool Generator.
