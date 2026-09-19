# @agenticai/platform-inference-gateway

Pipeline-owned central inference path for Amazon Bedrock AgentCore. It replaces
the former NLB/LiteLLM placeholder with a real AgentCore Gateway, a Bedrock
Mantle inference target, Cognito machine-to-machine authentication, and native
Gateway model rate limits.

## Resources

The construct emits:

1. An `AWS::BedrockAgentCore::Gateway` using MCP protocol version `2025-11-25`.
2. An `AWS::BedrockAgentCore::GatewayTarget` whose inference connector is
   `bedrock-mantle` and whose outbound credential provider is
   `GATEWAY_IAM_ROLE`.
3. An `AWS::BedrockAgentCore::GatewayRateLimit` with one explicit RPM/TPM entry
   per configured provider-qualified model and a final zero-request wildcard.
4. A Cognito User Pool, resource server, confidential client-credentials app
   client, and Cognito domain for `CUSTOM_JWT` inbound authentication.
5. A Gateway role with only `bedrock-mantle:ListModels` and
   `bedrock-mantle:CreateInference`. Its trust policy requires the same account
   and the named Gateway ARN pattern.

The Cognito client secret is managed by Cognito and is never emitted as a
CloudFormation output. The stack outputs the client ID, token endpoint, OAuth
scope, Gateway URL, target ID, target name, and rate-limit ID.

## Model identifiers

Invocation and rate-limit identifiers are intentionally different. AgentCore
prefixes every discovered model ID with the Gateway **target name**, not the
connector ID:

```text
InferenceTargetName output: agenticai-inference-prod-bedrock
LiteLLMModel route:         agenticai-inference-prod-bedrock/openai.gpt-oss-120b
Rate-limit key:             openai.gpt-oss-120b
```

Construct the route as `<InferenceTargetName>/<qualifiedModelId>` or select the
exact ID returned by `/inference/v1/models`; never hard-code `bedrock-mantle` as
the route prefix unless that is the target's actual name. Passing a target-prefixed
route as `qualifiedModelId` fails synthesis. Duplicate models, missing allocations,
fractional rates, zero positive rates, and rates above the service maximum also
fail synthesis.

## CDK usage

```ts
new PlatformInferenceGatewayConstruct(this, 'InferenceGateway', {
  envName: 'nonprod',
  applicationId: 'platform-inference',
  agentId: 'shared',
  tenantId: 'shared',
  costCentre: 'platform',
  modelRateLimits: [
    {
      qualifiedModelId: 'openai.gpt-oss-120b',
      requestsPerMinute: 10,
      tokensPerMinute: 10_000,
    },
  ],
});
```

The Platform CDK app requires the same allocation through context:

```bash
MODEL_LIMITS='[{"qualifiedModelId":"openai.gpt-oss-120b","requestsPerMinute":10,"tokensPerMinute":10000}]'

npx cdk synth --strict \
  --context stage=platform \
  --context agenticai/organizationId=o-example123 \
  --context agenticai/platformAccountId=111111111111 \
  --context agenticai/pipelineRoleArn=arn:aws:iam::111111111111:role/ExamplePipelineRole \
  --context agenticai/inferenceModelRateLimits="$MODEL_LIMITS"
```

Use placeholder values only for offline synthesis. Real deployment belongs to
the Platform pipeline, which creates separate nonproduction and production
Gateway stacks. Workstream builders must not deploy this stack or deploy
anything directly into a workstream account.

## Agent configuration

Generated Strands agents configure `LiteLLMModel` with:

```python
provider_model_id = "openai.gpt-oss-120b"
model_route = f"{inference_target_name}/{provider_model_id}"

LiteLLMModel(
    model_id=f"openai/{model_route}",
    client_args={
        "api_base": f"{gateway_url}/inference/v1",
        "api_key": access_token,
    },
)
```

`inference_target_name` comes from the `InferenceTargetName` stack output. The
access token comes from the output Cognito token endpoint using the client
credentials grant and the output OAuth scope. The endpoint is derived from
CDK's `UserPoolDomain.baseUrl()` so managed domains use the required
`amazoncognito.com` suffix rather than the AWS service API suffix. Secrets and
access tokens stay in process memory and must not be written to evidence or
logs.

## Security semantics

Rate limiting runs before AgentCore Policy and is fail-open. The wildcard
zero-rate entry is a normal-operation traffic control, not an authorization
boundary. Authentication, Gateway Policy in `ENFORCE` mode, Guardrails, IAM,
and SCP controls remain required before production release.

Every taggable resource receives exactly these allocation tags:
`application-id`, `agent-id`, `tenant-id`, `cost-centre`, and `environment`.
Gateway targets and rate limits currently expose no tag property in their
CloudFormation resource contracts.

## Test

```bash
npm run build
npx jest --runInBand tests/conformance/phase-9-platform-inference-gateway.test.ts
npm run lint
npm run scrub
```

The compatibility spike under `scripts/live-agentcore-gateway-spike/` is the
live proof for Cognito JWT, `LiteLLMModel`, streaming, non-streaming, HTTP 429,
and cleanup. OTEL span correlation remains explicitly blocked in `us-west-2`;
do not interpret resource synthesis as observability proof.

## Cleanup

For a standalone nonproduction stack, set the same context used at deployment
and run:

```bash
export AGENTICAI_INFERENCE_MODEL_RATE_LIMITS="$MODEL_LIMITS"
bash scripts/teardown.sh \
  --stack AgenticAI-Platform-InferenceGatewayStack
```

The teardown script is fail-closed: it refuses to synthesize this stack without
`AGENTICAI_PLATFORM_ACCOUNT_ID`, `AGENTICAI_PIPELINE_ROLE_ARN`,
`AGENTICAI_ORGANIZATION_ID`, and `AGENTICAI_INFERENCE_MODEL_RATE_LIMITS`.
Pipeline-created stage stacks are removed through the pipeline/CloudFormation
lifecycle rather than an out-of-band workstream deployment.
