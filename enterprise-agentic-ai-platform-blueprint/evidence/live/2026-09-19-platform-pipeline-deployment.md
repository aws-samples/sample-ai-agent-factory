# Live evidence — Platform pipeline deployment

- **Date:** 2026-09-19
- **Status:** PASS for deployment, read-only control-plane assertions, Cognito M2M, model discovery, and Strands inference
- **Region:** `us-west-2`
- **Initial deployment Git HEAD:** `f037b4ed8d6852325b0eb3585eed31bd7670286f`
- **Endpoint-fix Git HEAD:** `2ca82729af98f5c851d1921046f64ca8462622f8`
- **Target-route Git HEAD:** `0ef7f50fefd7700cf990cf8b57fdedd95d55c1c1`
- **Pipeline executions:** `08ed2063-7dd5-4c5b-96a3-15493c387c09`, `b208bf20-345a-4515-8945-1a1274ebca7e`, `6215988e-b8e6-40c9-908d-9910e9555ea0`
- **Validation topology:** one Management/Governance account, one Platform account representing both environments for this test, and one Workstream sender account

This file is a sanitized summary. It contains no AWS account IDs, access keys,
client secrets, JWTs, authorization headers, or model response text.

## Pipeline result

The initial pipeline completed with status `Succeeded` on exact commit `f037b4e`.

| Stage | Result |
|---|---|
| Source | Passed; revision matched `f037b4e` |
| Synth | Passed; dependency install, TypeScript build, Jest suite, and strict CDK synthesis completed |
| SelfMutate | Passed |
| File assets | Passed |
| Nonprod | Audit, Log Archive, Guardrail, Registry, and Inference Gateway passed |
| SecurityReview | Explicitly approved for this test execution |
| Prod | Guardrail, Registry, and Inference Gateway passed |

Two follow-up executions also traversed Source, Synth, SelfMutate, assets, all
Nonprod actions, fresh explicit production approval, and all Prod actions:

| Execution | Revision | Result |
|---|---|---|
| `b208bf20-345a-4515-8945-1a1274ebca7e` | `2ca8272` | Passed; corrected the Cognito hosted-domain token endpoint |
| `6215988e-b8e6-40c9-908d-9910e9555ea0` | `0ef7f50` | Passed; exported the inference target name used for model routing |

For both follow-ups, Guardrail and Registry were no-op deployments. Only the
Inference Gateway stack output contract changed.

The consolidated validation topology deliberately mapped Platform nonproduction
and production to one account. The production stage reused the stable
`AgenticAI-GuardrailAdmin` role and created a separately named regional baseline
guardrail. Normal deployments use separate Platform accounts and retain the
stable unsuffixed names in each account.

## Management/Governance assertions

The live `Nonprod-LogArchive` stack reached `CREATE_COMPLETE` with 18 resources.
Read-only service checks verified:

1. The CloudWatch Logs destination policy names the Workstream sender as a
   12-digit account ID, not an IAM root ARN.
2. The destination targets `agenticai-central-logs` and uses
   `AgenticAI-LogArchive-CWLDestinationRole`.
3. The role trust is limited to `logs.amazonaws.com` with sender and recipient
   `aws:SourceArn` conditions.
4. Its inline policy grants only `kinesis:ListShards`, `PutRecord`, and
   `PutRecords` on the named stream, plus KMS encrypt/data-key/re-encrypt actions
   on the stack key.
5. The stream is `ACTIVE`, `ON_DEMAND`, KMS-encrypted, and retains records for
   24 hours.
6. CloudTrail and CUR buckets use the same customer-managed key; public access
   is blocked on the archive bucket.
7. KMS automatic rotation is enabled with a 365-day period.

## Production Platform assertions

Read-only service checks verified:

1. `Prod-Guardrail` reached `CREATE_COMPLETE` without creating a duplicate IAM
   admin role.
2. The production guardrail is named `agenticai-guardrail-baseline-prod` and is
   `READY`.
3. `Prod-InferenceGateway` reached `CREATE_COMPLETE` with native
   `AWS::BedrockAgentCore::Gateway`, `GatewayTarget`, and `GatewayRateLimit`
   resources plus Cognito M2M resources.
4. Gateway `agenticai-inference-prod-5pguxqqepp` is `READY` with `CUSTOM_JWT`
   inbound authorization.
5. Inference target `2CIAUE6YEK` is `READY` and uses `GATEWAY_IAM_ROLE` outbound
   authorization.
6. The production rate-limit resource reached `CREATE_COMPLETE`.
7. `Prod-Registry` reached `CREATE_COMPLETE` with the current DynamoDB-backed
   registry implementation; replacing it with the real AgentCore Registry
   remains an explicit release gate.

A pinned Boto3 verifier independently read the native rate limit as `ACTIVE`,
closing the control-plane gap left by the host AWS CLI.

## Pipeline-owned Gateway invocation

The non-mutating verifier ran against the production stack deployed from exact
commit `0ef7f50`. It verified, in order:

1. The stack was `UPDATE_COMPLETE` and emitted a Cognito managed-domain token
   endpoint ending in `amazoncognito.com`.
2. The Gateway and target were `READY`, the authorizer was `CUSTOM_JWT`, all five
   allocation tags were present, and the target used `GATEWAY_IAM_ROLE`.
3. The native rate limit was `ACTIVE`.
4. The Cognito client allowed only the expected client-credentials flow and
   Gateway OAuth scope; the client ID was recorded only as a SHA-256 hash.
5. The target name matched the `InferenceTargetName` stack output.
6. Model discovery returned HTTP 200 with 49 models and included the resolved
   target-qualified route
   `agenticai-inference-prod-bedrock/openai.gpt-oss-120b`.
7. Strands `LiteLLMModel` 1.44.0 succeeded in non-streaming mode.
8. Strands `LiteLLMModel` 1.44.0 succeeded in streaming mode.

The verifier used Boto3 and Botocore 1.43.97 and LiteLLM 1.89.1. Client secrets
and access tokens remained in process memory. Evidence contains request IDs,
statuses, counts, a client-ID hash, and public resource/model identifiers; it
contains no credentials, authorization headers, prompt text, or model response
text.

The exact HTTP 429 negative twin was not repeated against the production-owned
rate limit because this verifier is intentionally non-mutating. The isolated
2026-09-18 spike remains the live proof for the same rate-limit contract.

## Defects found and closed during live deployment

1. Cross-account pipeline creation requires target deploy-role `PassRole` scoped
   to CodePipeline and target CloudFormation execution-role `PassRole` scoped to
   CloudFormation.
2. Root pipeline artifact buckets needed explicit destroy/auto-delete behavior
   to avoid rollback residue.
3. CodeBuild requires each BuildSpec command to be independently valid, and a
   nested checkout must publish `cdk.out` back to the artifact root.
4. Management CloudFormation requires read access to its CDK bootstrap version
   parameter.
5. Log Archive previously referenced a role and Kinesis stream it never created.
6. CDK's nonproduction S3 auto-delete provider requires stack-prefixed IAM/Lambda
   lifecycle permissions and Lambda-only `iam:PassRole`.
7. CloudWatch Logs destination policies require account IDs; the service rejects
   IAM root ARNs in `Principal.AWS`.
8. A consolidated Platform test account must share the fixed Guardrail admin role
   and isolate the regional production guardrail name.
9. Cognito hosted domains use `amazoncognito.com`; constructing the token endpoint
   with the AWS API URL suffix produces an unresolvable hostname.
10. AgentCore model discovery prefixes each model with the configured Gateway
    target name, not the inference connector ID; rate-limit dimensions remain
    provider-qualified and omit that target prefix.

Failed attempts were inventoried and removed before each retry. KMS keys created
by failed CloudFormation attempts remain only in AWS-managed pending-deletion
states with no active aliases.

## Not yet proven

This deployment does **not** prove:

- An exact HTTP 429 negative twin against the pipeline-owned production rate
  limit. The rate limit is `ACTIVE` and positive calls passed, while exact 429
  behavior remains proven by the isolated compatibility spike.
- Pipeline-owned AgentCore Gateway PolicyEngine enforcement or Bedrock Guardrail
  attachment on every inference call. The isolated PolicyEngine API contract is
  proven separately in
  [`2026-09-19-policyengine-compatibility-spike.md`](2026-09-19-policyengine-compatibility-spike.md).
- Product-integrated per-developer `sub` authorization; the isolated
  subject/group matrix passed, while the deployed wrapper remains group-based.
- Workstream Tool Gateway, Runtime, Memory, generated-agent deployment, or
  pipeline-only Workstream delivery.
- SCPs 01–12 through an organization sandbox soak.
- Full adversarial, canary, rollback, and final teardown gates.
- Gateway OTEL rate-limit span correlation, which remains a reproduced blocker
  in `us-west-2`.

These remain explicit release blockers rather than implied successes.
